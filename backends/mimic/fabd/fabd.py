#!/usr/bin/env python3
"""fabd: the fabric agent for the OSAC-on-Sheldon mimic.

Turns OSAC Fabric Manager operations into real changes on the containerlab FRR fabric:
  tenant VirtualNetwork -> VRF + L3 VNI on the border and every server DPU (symmetric IRB)
  Subnet                -> L2 VNI + anycast gateway (SVI) + DHCP scope on every server DPU
  physical attachment   -> server's DPU host port into the subnet bridge, DHCP reservation, SG binding
  SecurityGroup         -> nftables (bridge family, stateful) chains bound to DPU host ports
  ExternalIP / NAT GW   -> 1:1 DNAT / SNAT on the border with VRF route leaking
  cluster ingress IP    -> reservation from ipam.lb_vip_ipv4_cidrs
  class of service      -> DSCP rewrite on the DPU host port (used by the dpu app's real backend)
Also builds the static SNAP storage network (VRF "storage") at startup.

Every operation is idempotent and keyed by OSAC API IDs. VNIs and addresses are allocated once
and kept in the state file for the resource's life. Stdlib only. Runs as root on Sheldon.

API (JSON, Authorization: Bearer <token>; GET /health needs no token):
  GET  /health                       GET /state
  PUT  /vnets/<id>        {cidr}                          -> {l3_vni, vrf}
  DEL  /vnets/<id>
  PUT  /subnets/<id>      {vnet, cidr, gateway, physical_cidrs, lb_vip_cidrs} -> {l2_vni}
  DEL  /subnets/<id>
  PUT  /sgs/<id>          {vnet, ingress:[...], egress:[...]}
  DEL  /sgs/<id>
  PUT  /attachments/<id>  {subnet, host, mac, sgs:[...]}  -> {ip, node, port}
  DEL  /attachments/<id>
  GET  /attachments/<id>/lease                            -> {ip_address, mac_address} | 404
  PUT  /pools/<id>        {cidr}           DEL /pools/<id>
  PUT  /extips/<id>       {pool}           -> {address}    DEL /extips/<id>
  PUT  /extip-attach/<id> {extip, address, vnet}          DEL /extip-attach/<id>
  PUT  /natgws/<id>       {vnet, extip}                   DEL /natgws/<id>
  PUT  /ingress/<cluster_id> {subnet, lb_vip_cidrs}       -> {address}   DEL /ingress/<cluster_id>
  PUT  /cos/<host>        {cos: kept|rewritten|dropped}
  GET  /ports                                             -> per host port binding + counters
  POST /reconcile
"""
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF_PATH = os.environ.get("FABD_CONFIG", "/etc/osac-fabd/config.json")
CONF = json.load(open(CONF_PATH))
STATE_PATH = CONF.get("state_file", "/var/lib/osac-fabd/state.json")
TOKEN = open(CONF.get("token_file", "/etc/osac-fabd/token")).read().strip()
NODES = CONF["nodes"]                 # name -> {asn, lo}
BORDER = CONF["border"]               # "border"
SERVER_DPUS = CONF["server_dpus"]     # ["dpu-0", "dpu-1"]
HOSTS = CONF["hosts"]                 # "bm-0" -> {node, port}
EDGE_NH = CONF["edge_nexthop"]        # "192.168.254.0" (edge's address on border eth1)
UPLINK = CONF.get("border_uplink", "eth1")
# plan 08: the hub as a shared service on the border. {"port": "eth4", "address": "10.251.0.2",
#   "allow": [{"prefix": "10.251.0.60/32", "tcp": ["443", "30000-32767"]}, ...], "cut_lifecycle_nic": true}
HUB = CONF.get("hub")
# plan 08: the edge is the provider's internet gateway: masquerade toward the "internet" port and never let a
# tenant reach private/lab ranges through it. {"node": "edge", "internet_port": "eth0", "block": [cidrs]}
EDGE = CONF.get("edge")
TENANT_DNS = CONF.get("tenant_dns", "198.51.100.53")
def _pool(key, default):
    """VNI pools from the config (for example "30001-30090") so a site's numbering plan is honoured; the
    mimic's defaults otherwise."""
    v = CONF.get(key)
    if not v:
        return default
    a, b = str(v).split("-")
    return range(int(a), int(b) + 1)


L3_RANGE = _pool("l3_vni_pool", range(20001, 21000))
L2_RANGE = _pool("l2_vni_pool", range(10001, 11000))
DSCP = {"kept": 46, "rewritten": 0}
LOCK = threading.RLock()
# Dry run: FABD_DRY_RUN=1 records every device command in JOURNAL instead of running it, and answers the few
# reads the handlers need with canned data. Used by fabd/dryrun-v1.sh to test the API against a second, idle copy.
DRY_RUN = os.environ.get("FABD_DRY_RUN") == "1"
DRY_LEASE_READS = int(os.environ.get("FABD_DRY_LEASE_READS", "0"))   # dry run: lease reads per MAC before a lease appears
DRY_LEASE_SEEN = {}
JOURNAL = []                 # [{ts, target, cmd, ref}] in dry run; ref = the v1 object being handled, if any
CTX = {"ref": None}


def log(**kw):
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw}), flush=True)


# ---------------------------------------------------------------- state
def load_state():
    try:
        return json.load(open(STATE_PATH))
    except FileNotFoundError:
        return {"vnets": {}, "subnets": {}, "sgs": {}, "attachments": {}, "pools": {},
                "extips": {}, "extip_attach": {}, "natgws": {}, "ingress": {}, "cos": {}, "marks": {}}


S = load_state()
for k in ("vnets", "subnets", "sgs", "attachments", "pools", "extips", "extip_attach", "natgws", "ingress", "cos", "marks",
          "volumes", "volume_attach"):
    S.setdefault(k, {})


def save():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    json.dump(S, open(tmp, "w"), indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


class Fail(Exception):
    def __init__(self, status, msg, retryable=False):
        super().__init__(msg)
        self.status, self.msg, self.retryable = status, msg, retryable


# ---------------------------------------------------------------- exec in fabric nodes
def journal(target, cmd):
    """Dry run only. kind=read for the commands that only look (lease file, link stats, virsh listings)."""
    read = cmd.startswith(("cat ", "ip -j", "virsh domiflist", "virsh domstate", "virsh domblklist"))
    JOURNAL.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "target": target, "cmd": cmd, "ref": CTX["ref"],
                    "kind": "read" if read else "write"})


def dry_sh(node, script):
    """Canned answers for the reads the handlers make (dry run only)."""
    if "ip -j -s link show" in script:
        port = script.split()[-1]
        return json.dumps([{"ifname": port, "operstate": "UP", "master": None,
                            "stats64": {"rx": {"packets": 0, "dropped": 0}, "tx": {"packets": 0, "dropped": 0}}}])
    if "cat /var/lib/fab/leases" in script:  # an attachment gets its lease after DRY_LEASE_READS reads, as if the server booted
        out = []
        for a in S["attachments"].values():
            if a["node"] != node:
                continue
            DRY_LEASE_SEEN[a["mac"]] = DRY_LEASE_SEEN.get(a["mac"], 0) + 1
            if DRY_LEASE_SEEN[a["mac"]] > DRY_LEASE_READS:
                out.append(f"{int(time.time())+3600} {a['mac']} {a['ip']} * *\n")
        return "".join(out)
    return ""


def sh(node, script, check=True):
    if DRY_RUN:
        journal(node, script)
        return dry_sh(node, script)
    p = subprocess.run(["podman", "exec", node, "sh", "-c", script], capture_output=True, text=True, timeout=60)
    if check and p.returncode != 0:
        raise Fail(503, f"{node}: command failed: {p.stderr.strip()[-400:]}", retryable=True)
    return p.stdout


def vtysh(node, *cmds):
    args = " ".join("-c " + json.dumps(c) for c in ("conf t",) + cmds + ("end",))
    return sh(node, f"vtysh {args}")


def free(rng, used):
    for v in rng:
        if v not in used:
            return v
    raise Fail(409, "no free VNI left in the OSAC range")


# ---------------------------------------------------------------- VRF (L3 VNI)
def vrf_name(l3):
    return f"vrf{l3}"


def hub_leak_routes():
    """FRR static routes (without the leading 'ip route') that leak the hub's service prefixes into a tenant VRF."""
    if not HUB:
        return []
    return [f"{a['prefix']} {HUB['port']} nexthop-vrf default" for a in HUB.get("allow", [])]


def ocnet_link(host, state):
    """Plan 08: a server whose port is in a tenant network must not keep its lifecycle (ocnet) NIC live.
    No flag = the running VM (virsh has no --live here); --config so a power cycle keeps it. Non-VM hosts are skipped."""
    if not HUB or not HUB.get("cut_lifecycle_nic", True):
        return
    host = host.split("/")[-1]  # OSAC names cluster workers "<namespace>/<bmh>"; libvirt knows the short name
    for l in host_run(["virsh", "domiflist", host], check=False).splitlines():
        f = l.split()
        if len(f) >= 5 and f[2] == "ocnet":
            host_run(["virsh", "domif-setlink", host, f[4], state], check=False)             # live, if running
            host_run(["virsh", "domif-setlink", host, f[4], state, "--config"], check=False)  # persistent
            log(op="ocnet-link", host=host, mac=f[4], state=state)


def ensure_vrf(node, l3):
    n, lo, asn = vrf_name(l3), NODES[node]["lo"], NODES[node]["asn"]
    sh(node, f"""
ip link show {n} >/dev/null 2>&1 || ip link add {n} type vrf table {l3}
ip link set {n} up
ip link show br{l3} >/dev/null 2>&1 || ip link add br{l3} type bridge
ip link set br{l3} master {n} addrgenmode none
ip link show vx{l3} >/dev/null 2>&1 || ip link add vx{l3} type vxlan id {l3} local {lo} dstport 4789 nolearning
ip link set vx{l3} master br{l3}
bridge link set dev vx{l3} neigh_suppress on learning off
ip link set vx{l3} up; ip link set br{l3} up""")
    cmds = [f"vrf {n}", f"vni {l3}", "exit-vrf", f"router bgp {asn} vrf {n}", f"bgp router-id {lo}",
            "address-family ipv4 unicast", "redistribute connected"]
    if node == BORDER:
        cmds += ["redistribute static"]
    cmds += ["exit-address-family", "address-family l2vpn evpn", "advertise ipv4 unicast",
             f"route-target import 0:{l3}", f"route-target export 0:{l3}", "exit-address-family"]
    vtysh(node, *cmds)
    if node == BORDER:  # leak a default route out of the VRF toward the edge (north-south)
        vtysh(node, f"vrf {n}", f"ip route 0.0.0.0/0 {EDGE_NH} nexthop-vrf default", "exit-vrf")
        for r in hub_leak_routes():  # plan 08: hub shared-service prefixes, via the border's hub port
            vtysh(node, f"vrf {n}", f"ip route {r}", "exit-vrf")


def delete_vrf(node, l3):
    n, asn = vrf_name(l3), NODES[node]["asn"]
    cfg = sh(node, "vtysh -c 'show running-config'")
    unbind = [f"vrf {n}", f"no vni {l3}"]
    if node == BORDER and f"ip route 0.0.0.0/0 {EDGE_NH} nexthop-vrf default" in cfg:
        unbind.append(f"no ip route 0.0.0.0/0 {EDGE_NH} nexthop-vrf default")
    if node == BORDER:
        unbind += [f"no ip route {r}" for r in hub_leak_routes() if f"ip route {r}" in cfg]
    if f"vrf {n}" in cfg:
        vtysh(node, *unbind, "exit-vrf")
    if f"router bgp {asn} vrf {n}" in cfg:
        vtysh(node, f"no router bgp {asn} vrf {n}")
    sh(node, f"ip link del vx{l3} 2>/dev/null; ip link del br{l3} 2>/dev/null; ip link del {n} 2>/dev/null; true")
    sh(node, f'vtysh -c "conf t" -c "no vrf {n}" -c end >/dev/null 2>&1; true', check=False)


# ---------------------------------------------------------------- Subnet (L2 VNI + anycast gw + DHCP)
def anycast_mac(l2):
    return "02:aa:%02x:%02x:%02x:01" % ((l2 >> 16) & 255, (l2 >> 8) & 255, l2 & 255)


def ensure_subnet_on(node, l3, l2, gw, plen):
    n, lo, asn = vrf_name(l3), NODES[node]["lo"], NODES[node]["asn"]
    sh(node, f"""
ip link show br{l2} >/dev/null 2>&1 || ip link add br{l2} type bridge
ip link set br{l2} master {n} address {anycast_mac(l2)}
ip addr replace {gw}/{plen} dev br{l2}
ip link show vx{l2} >/dev/null 2>&1 || ip link add vx{l2} type vxlan id {l2} local {lo} dstport 4789 nolearning
ip link set vx{l2} master br{l2}
bridge link set dev vx{l2} neigh_suppress on learning off
ip link set vx{l2} up; ip link set br{l2} up
nft list table bridge osacdhcp >/dev/null 2>&1 || nft -f - <<'EOF'
table bridge osacdhcp {{
  chain filt {{ type filter hook forward priority -10; policy accept; }}
}}
EOF
nft list chain bridge osacdhcp filt | grep -q 'vx{l2} ' || {{
  nft add rule bridge osacdhcp filt oifname "vx{l2}" udp dport {{67,68}} drop
  nft add rule bridge osacdhcp filt iifname "vx{l2}" udp dport {{67,68}} drop
}}""")
    vtysh(node, f"router bgp {asn}", "address-family l2vpn evpn", f"vni {l2}",
          f"route-target import 0:{l2}", f"route-target export 0:{l2}", "exit-vni", "exit-address-family")


def delete_subnet_on(node, l2):
    asn = NODES[node]["asn"]
    vtysh(node, f"router bgp {asn}", "address-family l2vpn evpn", f"no vni {l2}", "exit-address-family")
    sh(node, f"""
for h in $(nft -a list chain bridge osacdhcp filt 2>/dev/null | grep 'vx{l2} ' | sed -E 's/.*handle ([0-9]+)/\\1/'); do
  nft delete rule bridge osacdhcp filt handle $h; done
ip link del vx{l2} 2>/dev/null; ip link del br{l2} 2>/dev/null; rm -f /etc/fab/dnsmasq.d/sub-{l2}.conf; true""")


def dhcp_reload(node):
    """Rewrite the DPU's dnsmasq config from state and restart it (only its own physical MACs)."""
    files = {}
    for sid, sub in S["subnets"].items():
        net = ipaddress.ip_network(sub["cidr"])
        files[f"sub-{sub['l2_vni']}.conf"] = (
            f"interface=br{sub['l2_vni']}\n"
            f"dhcp-range=set:s{sub['l2_vni']},{net.network_address},static,{net.netmask},1h\n"
            f"dhcp-option=tag:s{sub['l2_vni']},option:router,{sub['gateway']}\n"
            f"dhcp-option=tag:s{sub['l2_vni']},option:dns-server,{TENANT_DNS}\n")
    for aid, att in S["attachments"].items():
        if att["node"] == node:
            sub = S["subnets"][att["subnet"]]
            files[f"host-{aid}.conf"] = f"dhcp-host={att['mac']},set:s{sub['l2_vni']},{att['ip']}\n"
    script = ["mkdir -p /etc/fab/dnsmasq.d /var/lib/fab", "rm -f /etc/fab/dnsmasq.d/*.conf",
              "printf 'port=0\\nbind-dynamic\\ndhcp-authoritative\\ndhcp-leasefile=/var/lib/fab/leases\\n"
              "conf-dir=/etc/fab/dnsmasq.d,*.conf\\nlog-dhcp\\n' > /etc/fab/dnsmasq.conf"]
    for name, body in files.items():
        script.append(f"cat > /etc/fab/dnsmasq.d/{name} <<'EOF'\n{body}EOF")
    script.append("pkill -x dnsmasq; sleep 0.3")
    if any(f.startswith("sub-") for f in files):
        script.append("dnsmasq --conf-file=/etc/fab/dnsmasq.conf --log-facility=/var/log/fab-dnsmasq.log")
    sh(node, "\n".join(script))


# ---------------------------------------------------------------- Security groups + class of service
def rules_nft(direction, rules):
    out = []
    for r in rules:
        cidr = r.get("sourceCidr") if direction == "in" else r.get("destinationCidr")
        addr = f"ip {'saddr' if direction == 'in' else 'daddr'} {cidr} " if cidr and cidr != "0.0.0.0/0" else "ip protocol != 0 "
        p = r.get("protocol", "all")
        if p in ("tcp", "udp"):
            out.append(f"{addr}{p} dport {r['portFrom']}-{r['portTo']} accept")
        elif p == "icmp":
            out.append(f"{addr}ip protocol icmp accept")
        else:
            out.append(f"{addr}accept")
    return out


def apply_port_policy(node, port):
    """Rebuild the nft chains for one DPU host port from the SGs bound to it and its CoS."""
    sgs = []
    for att in S["attachments"].values():
        if att["node"] == node and att["port"] == port:
            sgs = [S["sgs"][g] for g in att.get("sgs", []) if g in S["sgs"]]
    host = next((h for h, v in HOSTS.items() if v["node"] == node and v["port"] == port), None)
    cos = S["cos"].get(host)
    lines = ["table bridge osacsg {", " chain filt { type filter hook forward priority 0; policy accept; }",
             " chain cos { type filter hook prerouting priority 0; policy accept; }", "}"]
    script = [f"nft list table bridge osacsg >/dev/null 2>&1 || nft -f - <<'EOF'\n" + "\n".join(lines) + "\nEOF"]
    for d in ("in", "out"):
        ch = f"{port}-{d}"
        script.append(f"nft list chain bridge osacsg {ch} >/dev/null 2>&1 && nft flush chain bridge osacsg {ch} || nft add chain bridge osacsg {ch}")
    # (re)build jump rules for this port
    script.append(f"""for h in $(nft -a list chain bridge osacsg filt | grep -E '"{port}" jump' | sed -E 's/.*handle ([0-9]+)/\\1/'); do
  nft delete rule bridge osacsg filt handle $h; done""")
    script.append(f"for h in $(nft -a list chain bridge osacsg cos | grep -E '\"{port}\" ' | sed -E 's/.*handle ([0-9]+)/\\1/'); do nft delete rule bridge osacsg cos handle $h; done")
    if sgs:
        ingress = [x for g in sgs for x in rules_nft("in", g.get("ingress", []))]
        egress = [x for g in sgs for x in rules_nft("out", g.get("egress", []))]
        base = ["ct state established,related accept", "ether type arp accept", "ether type != ip accept"]
        for d, rules, dhcp in (("in", ingress, "udp sport 67 udp dport 68 accept"),
                               ("out", egress, "udp sport 68 udp dport 67 accept")):
            for r in base + [dhcp] + rules + ["drop"]:
                script.append(f"nft add rule bridge osacsg {port}-{d} {r}")
        script.append(f'nft add rule bridge osacsg filt oifname "{port}" jump {port}-in')
        script.append(f'nft add rule bridge osacsg filt iifname "{port}" jump {port}-out')
    if cos in DSCP:
        script.append(f'nft add rule bridge osacsg cos iifname "{port}" ip dscp set {DSCP[cos]}')
    elif cos == "dropped":
        script.append(f'nft add rule bridge osacsg cos iifname "{port}" ip dscp != 0 drop')
    sh(node, "\n".join(script))


# ---------------------------------------------------------------- Border NAT
def mark_for(vnet_id):
    if vnet_id not in S["marks"]:
        S["marks"][vnet_id] = max([0x100] + list(S["marks"].values())) + 1
    return S["marks"][vnet_id]


def hub_policy_nft():
    """Plan 08: tenants may reach only the hub's listed prefixes/ports through the border's hub port."""
    hp = HUB["port"]
    out = ["table ip osachub {", " chain hubfilt { type filter hook forward priority 0; policy accept;",  # not fwd: nft keyword
           f'  iifname "{hp}" ct state established,related accept', f'  iifname "{hp}" drop',
           f'  oifname "{hp}" ct state established,related accept']
    for a in HUB.get("allow", []):
        ports = ", ".join(a.get("tcp", []))
        if ports:
            out.append(f'  oifname "{hp}" ip daddr {a["prefix"]} tcp dport {{ {ports} }} accept')
        out.append(f'  oifname "{hp}" ip daddr {a["prefix"]} icmp type {{ echo-request, destination-unreachable, time-exceeded }} accept')
    out += [f'  oifname "{hp}" drop', " }", "}"]
    return "\n".join(out)


def border_nat_reload():
    """Rebuild the border's NAT table + fwmark rules from state.

    Marks: every connection entering from a tenant VRF (or DNAT'd into one) gets ct mark = the VRF's mark;
    the mark is copied to the packet so `ip rule fwmark` routes it in the right table. VXLAN keeps the
    skb mark on the outer packet, so underlay loopback destinations are sent to the main table first.
    """
    lines = ["table ip osacnat {",
             " chain mangle { type filter hook prerouting priority -150; policy accept;"]
    for vid, v in S["vnets"].items():
        if HUB or any(a.get("vnet") == vid for a in S["extip_attach"].values()) or any(g["vnet"] == vid for g in S["natgws"].values()):
            lines.append(f'  iifname "br{v["l3_vni"]}" ct state new ct mark set {mark_for(vid)}')
    lines += ["  ct mark != 0 meta mark set ct mark", " }",
              " chain pre { type nat hook prerouting priority -100; policy accept;"]
    for aid, a in S["extip_attach"].items():
        if a.get("vnet") is None:  # provider_management: DNAT into the hub network (default VRF, out the hub port)
            lines.append(f'  iifname "{UPLINK}" ip daddr {a["address"]} dnat to {a["dest"]}')
            for vid, v in S["vnets"].items():  # hairpin from any tenant (e.g. a worker using its cluster's public API name)
                m = mark_for(vid)
                lines.append(f'  iifname "br{v["l3_vni"]}" ip daddr {a["address"]} meta mark set {m} ct mark set {m} dnat to {a["dest"]}')
            continue
        m = mark_for(a["vnet"])
        lines.append(f'  iifname "{UPLINK}" ip daddr {a["address"]} meta mark set {m} ct mark set {m} dnat to {a["dest"]}')
    lines += [" }", " chain post { type nat hook postrouting priority 100; policy accept;"]
    if HUB:  # plan 08: everything leaving the hub port is the border's address (no return route on the hub; hairpins stay symmetric)
        lines.append(f'  oifname "{HUB["port"]}" snat to {HUB["address"]}')
    for aid, a in S["extip_attach"].items():  # outbound from a DNAT'd tenant host leaves as its external address
        if a.get("vnet") is not None:
            lines.append(f'  oifname "{UPLINK}" ct mark {mark_for(a["vnet"])} ip saddr {a["dest"]} snat to {a["address"]}')
    for gid, g in S["natgws"].items():
        v = S["vnets"][g["vnet"]]
        lines.append(f'  oifname "{UPLINK}" ct mark {mark_for(g["vnet"])} ip saddr {v["cidr"]} snat to {g["address"]}')
    lines += [" }", "}"]
    rules = ["ip rule add pref 99 to 192.168.255.0/24 lookup main"]
    for vid, m in S["marks"].items():
        if vid in S["vnets"]:
            rules.append(f"ip rule add pref 100 fwmark {m} lookup {S['vnets'][vid]['l3_vni']}")
    pools = [p["cidr"] for p in S["pools"].values()]
    script = ["set -e", "nft delete table ip osacnat 2>/dev/null || true", "nft -f - <<'EOF'\n" + "\n".join(lines) + "\nEOF",
              "nft delete table ip osachub 2>/dev/null || true"] + (["nft -f - <<'EOF'\n" + hub_policy_nft() + "\nEOF"] if HUB else []) + [
              "while ip rule del pref 100 2>/dev/null; do :; done", "while ip rule del pref 99 2>/dev/null; do :; done", "true"] + rules
    sh(BORDER, "\n".join(script))
    asn = NODES[BORDER]["asn"]
    cfg = sh(BORDER, "vtysh -c 'show running-config'")
    for p in pools:
        if f"ip route {p} blackhole" not in cfg:
            vtysh(BORDER, f"ip route {p} blackhole", f"router bgp {asn}", "address-family ipv4 unicast",
                  f"network {p}", "exit-address-family")


# ---------------------------------------------------------------- operations
def vnet_put(vid, body):
    v = S["vnets"].get(vid)
    if not v:
        used = {x["l3_vni"] for x in S["vnets"].values()} | {x["l2_vni"] for x in S["subnets"].values()}
        v = {"cidr": body["cidr"], "l3_vni": free(L3_RANGE, used)}
        S["vnets"][vid] = v
        save()
    for node in [BORDER] + SERVER_DPUS:
        ensure_vrf(node, v["l3_vni"])
    return {"l3_vni": v["l3_vni"], "vrf": vrf_name(v["l3_vni"])}


def vnet_del(vid):
    v = S["vnets"].get(vid)
    if not v:
        return {}
    if any(s["vnet"] == vid for s in S["subnets"].values()):
        raise Fail(409, "virtual network still has subnets", retryable=True)
    for node in [BORDER] + SERVER_DPUS:
        delete_vrf(node, v["l3_vni"])
    S["vnets"].pop(vid)
    S["marks"].pop(vid, None)
    save()
    border_nat_reload()
    return {}


def subnet_put(sid, body):
    s = S["subnets"].get(sid)
    if body["vnet"] not in S["vnets"]:
        raise Fail(409, "virtual network not created yet", retryable=True)
    if not s:
        used = {x["l3_vni"] for x in S["vnets"].values()} | {x["l2_vni"] for x in S["subnets"].values()}
        s = {"vnet": body["vnet"], "cidr": body["cidr"], "gateway": body["gateway"],
             "physical_cidrs": body.get("physical_cidrs", []), "lb_vip_cidrs": body.get("lb_vip_cidrs", []),
             "l2_vni": free(L2_RANGE, used)}
        S["subnets"][sid] = s
        save()
    l3 = S["vnets"][s["vnet"]]["l3_vni"]
    plen = ipaddress.ip_network(s["cidr"]).prefixlen
    for node in SERVER_DPUS:
        ensure_subnet_on(node, l3, s["l2_vni"], s["gateway"], plen)
        dhcp_reload(node)
    return {"l2_vni": s["l2_vni"]}


def subnet_del(sid):
    s = S["subnets"].get(sid)
    if not s:
        return {}
    if any(a["subnet"] == sid for a in S["attachments"].values()):
        raise Fail(409, "subnet still has attachments", retryable=True)
    if any(i["subnet"] == sid for i in S["ingress"].values()):
        raise Fail(409, "subnet still has cluster ingress reservations", retryable=True)
    for node in SERVER_DPUS:
        delete_subnet_on(node, s["l2_vni"])
    S["subnets"].pop(sid)
    save()
    for node in SERVER_DPUS:
        dhcp_reload(node)
    return {}


def sg_put(gid, body):
    S["sgs"][gid] = {"vnet": body.get("vnet"), "ingress": body.get("ingress", []), "egress": body.get("egress", [])}
    save()
    for a in S["attachments"].values():  # rule update: re-apply on every bound port
        if gid in a.get("sgs", []):
            apply_port_policy(a["node"], a["port"])
    return {}


def sg_del(gid):
    if any(gid in a.get("sgs", []) for a in S["attachments"].values()):
        raise Fail(409, "security group still bound", retryable=True)
    S["sgs"].pop(gid, None)
    save()
    return {}


def host_of(name):
    short = name.split("/")[-1]
    if short not in HOSTS:
        raise Fail(422, f"unknown bare-metal host '{name}' (fabd knows {', '.join(HOSTS)})")
    return HOSTS[short]


def attach_put(aid, body):
    if body["subnet"] not in S["subnets"]:
        raise Fail(409, "subnet not created yet", retryable=True)
    sub = S["subnets"][body["subnet"]]
    h = host_of(body["host"])
    mac = body["mac"].lower()
    a = S["attachments"].get(aid)
    if not a:
        taken = {x["ip"] for x in S["attachments"].values() if x["subnet"] == body["subnet"]}
        net = ipaddress.ip_network(sub["cidr"])
        ip = None
        for c in sub["physical_cidrs"] or [sub["cidr"]]:
            for cand in ipaddress.ip_network(c).hosts():
                s_ = str(cand)
                if s_ not in taken and cand not in (net.network_address, net.broadcast_address) and s_ != sub["gateway"]:
                    ip = s_
                    break
            if ip:
                break
        if not ip:
            raise Fail(409, "no free address in the subnet's physical range for this MAC")
        a = {"subnet": body["subnet"], "host": body["host"], "node": h["node"], "port": h["port"],
             "mac": mac, "ip": ip, "sgs": body.get("sgs", [])}
        S["attachments"][aid] = a
    else:
        a["sgs"] = body.get("sgs", a["sgs"])
    save()
    dhcp_reload(a["node"])
    apply_port_policy(a["node"], a["port"])  # bind SGs before moving the port
    sh(a["node"], f"ip link set {a['port']} nomaster 2>/dev/null; ip link set {a['port']} master br{sub['l2_vni']}; ip link set {a['port']} up")
    ocnet_link(a["host"], "down")
    return {"ip": a["ip"], "node": a["node"], "port": a["port"]}


def attach_del(aid):
    a = S["attachments"].get(aid)
    if not a:
        return {}
    ocnet_link(a["host"], "up")
    sh(a["node"], f"ip link set {a['port']} nomaster; sed -i '/ {a['mac']} /d' /var/lib/fab/leases 2>/dev/null; true")
    S["attachments"].pop(aid)
    save()
    apply_port_policy(a["node"], a["port"])
    dhcp_reload(a["node"])
    return {}


def lease_get(aid):
    a = S["attachments"].get(aid)
    if not a:
        raise Fail(404, "no such attachment")
    out = sh(a["node"], "cat /var/lib/fab/leases 2>/dev/null; true")
    hits = [l.split() for l in out.splitlines() if len(l.split()) >= 3 and l.split()[1].lower() == a["mac"]]
    if not hits:
        raise Fail(404, f"no active DHCP lease yet for {a['mac']}", retryable=True)
    if len(hits) > 1:
        raise Fail(409, f"more than one active lease for {a['mac']}", retryable=True)
    return {"ip_address": hits[0][2], "mac_address": a["mac"]}


def pool_put(pid, body):
    S["pools"][pid] = {"cidr": str(ipaddress.ip_network(body["cidr"]))}
    save()
    border_nat_reload()
    return {}


def pool_del(pid):
    if any(e["pool"] == pid for e in S["extips"].values()):
        raise Fail(409, "pool still has external IPs", retryable=True)
    p = S["pools"].pop(pid, None)
    save()
    if p:
        asn = NODES[BORDER]["asn"]
        vtysh(BORDER, f"no ip route {p['cidr']} blackhole", f"router bgp {asn}", "address-family ipv4 unicast",
              f"no network {p['cidr']}", "exit-address-family")
    return {}


def extip_put(eid, body):
    e = S["extips"].get(eid)
    if not e:
        if body["pool"] not in S["pools"]:
            raise Fail(409, "pool not created yet", retryable=True)
        net = ipaddress.ip_network(S["pools"][body["pool"]]["cidr"])
        taken = {x["address"] for x in S["extips"].values()}
        hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
        addr = next((str(h) for h in hosts if str(h) not in taken), None)
        if not addr:
            raise Fail(409, "the external IP pool is full")
        e = {"pool": body["pool"], "address": addr}
        S["extips"][eid] = e
        save()
    return {"address": e["address"]}


def extip_del(eid):
    if any(a["extip"] == eid for a in S["extip_attach"].values()) or any(g["extip"] == eid for g in S["natgws"].values()):
        raise Fail(409, "external IP still in use", retryable=True)
    S["extips"].pop(eid, None)
    save()
    return {}


def extip_attach_put(aid, body):
    rd = body.get("routing_domain", "tenant_virtual_network")
    if rd == "provider_management":  # plan 08: destination on the hub network, via the border's hub port
        if not HUB:
            raise Fail(422, "provider_management destinations need the hub port (config 'hub')")
        S["extip_attach"][aid] = {"extip": body["extip"], "address": body["address"], "dest": body["dest"], "vnet": None, "rd": rd}
        save()
        border_nat_reload()
        return {}
    if body["vnet"] not in S["vnets"]:
        raise Fail(409, "virtual network not created yet", retryable=True)
    S["extip_attach"][aid] = {"extip": body["extip"], "address": body["address"], "dest": body["dest"], "vnet": body["vnet"], "rd": rd}
    save()
    border_nat_reload()
    return {}


def extip_attach_del(aid):
    S["extip_attach"].pop(aid, None)
    save()
    border_nat_reload()
    return {}


def natgw_put(gid, body):
    if body["vnet"] not in S["vnets"]:
        raise Fail(409, "virtual network not created yet", retryable=True)
    S["natgws"][gid] = {"vnet": body["vnet"], "extip": body["extip"], "address": body["address"]}
    save()
    border_nat_reload()
    return {}


def natgw_del(gid):
    S["natgws"].pop(gid, None)
    save()
    border_nat_reload()
    return {}


def ingress_put(cid, body):
    i = S["ingress"].get(cid)
    if not i:
        sub = S["subnets"].get(body["subnet"])
        if not sub:
            raise Fail(409, "subnet not created yet", retryable=True)
        net = ipaddress.ip_network(sub["cidr"])
        taken = {x["address"] for x in S["ingress"].values() if x["subnet"] == body["subnet"]}
        addr = None
        for c in body.get("lb_vip_cidrs") or sub["lb_vip_cidrs"]:
            for cand in ipaddress.ip_network(c):
                if str(cand) not in taken and cand != net.broadcast_address and cand != net.network_address:
                    addr = str(cand)
                    break
            if addr:
                break
        if not addr:
            raise Fail(409, "no free address in the subnet's LoadBalancer VIP range")
        i = {"subnet": body["subnet"], "address": addr}
        S["ingress"][cid] = i
        save()
    return {"address": i["address"]}


def ingress_del(cid):
    S["ingress"].pop(cid, None)
    save()
    return {}


def cos_put(host, body):
    h = host_of(host)
    if body.get("cos") not in ("kept", "rewritten", "dropped"):
        raise Fail(422, "cos must be kept, rewritten or dropped")
    S["cos"][host.split("/")[-1]] = body["cos"]
    save()
    apply_port_policy(h["node"], h["port"])
    return {}


def ports_get():
    out = []
    for host, h in HOSTS.items():
        raw = sh(h["node"], f"ip -j -s link show {h['port']}")
        link = json.loads(raw)[0]
        att = next((dict(a, id=k) for k, a in S["attachments"].items() if a["node"] == h["node"] and a["port"] == h["port"]), None)
        out.append({"host": host, "node": h["node"], "port": h["port"], "master": link.get("master"),
                    "operstate": link.get("operstate"),
                    "counters": {"rxPackets": link["stats64"]["rx"]["packets"], "txPackets": link["stats64"]["tx"]["packets"],
                                 "dropped": link["stats64"]["rx"]["dropped"] + link["stats64"]["tx"]["dropped"]},
                    "attachment": att, "cos": S["cos"].get(host, "rewritten"),
                    "isolation": "enforced" if att else "not-enforced"})
    return out


# ---------------------------------------------------------------- SNAP storage (BlueField SNAP mimic)
ARRAY = CONF.get("array", {})   # {ssh: "student@10.251.0.40", key: "/etc/osac-fabd/array_key", nqn_prefix, addr, port}


def host_run(args, check=True, timeout=60):
    if DRY_RUN:
        journal("host", " ".join(args))
        if args[:2] == ["virsh", "domiflist"]:
            return "vnet0 network ocnet virtio 52:54:00:0c:0c:01\n"
        if args[:2] == ["virsh", "domstate"]:
            return "running\n"
        if args[:2] == ["virsh", "domblklist"]:
            return "Target Source\n vda /var/lib/libvirt/images/dry.qcow2\n"
        return ""
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise Fail(503, f"{args[0]}: {p.stderr.strip()[-300:]}", retryable=True)
    return p.stdout


def array(script):
    if DRY_RUN:
        journal("array", script)
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")
    return subprocess.run(
        ["ssh", "-i", ARRAY["key"], "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", ARRAY["ssh"], "sudo bash -s"],
        input=script, capture_output=True, text=True, timeout=60)


def array_ok(script):
    p = array("set -e\n" + script)
    if p.returncode != 0:
        raise Fail(503, f"storage array: {p.stderr.strip()[-300:]}", retryable=True)
    return p.stdout


def vol_nqn(vid):
    return f"{ARRAY['nqn_prefix']}:vol-{vid}"


def volume_put(vid, body):
    v = S["volumes"].get(vid)
    if not v:
        v = {"size_gb": int(body.get("size_gb", 5)), "nqn": vol_nqn(vid)}
        S["volumes"][vid] = v
        save()
    hostnqn = "nqn.2014-08.org.nvmexpress:uuid:dry-run" if DRY_RUN else open("/etc/nvme/hostnqn").read().strip()
    array_ok(f"""S=/sys/kernel/config/nvmet; N={v['nqn']}; F=/srv/vols/vol-{vid}.img
[ -f $F ] || truncate -s {v['size_gb']}G $F
mkdir -p $S/subsystems/$N/namespaces/1
echo 0 > $S/subsystems/$N/attr_allow_any_host
mkdir -p $S/hosts/{hostnqn}; [ -e $S/subsystems/$N/allowed_hosts/{hostnqn} ] || ln -s $S/hosts/{hostnqn} $S/subsystems/$N/allowed_hosts/
[ "$(cat $S/subsystems/$N/namespaces/1/enable)" = 1 ] || {{ echo $F > $S/subsystems/$N/namespaces/1/device_path; echo 1 > $S/subsystems/$N/namespaces/1/enable; }}
[ -e $S/ports/1/subsystems/$N ] || ln -s $S/subsystems/$N $S/ports/1/subsystems/$N""")
    return {"nqn": v["nqn"], "size_gb": v["size_gb"]}


def volume_del(vid):
    if any(a["volume"] == vid for a in S["volume_attach"].values()):
        raise Fail(409, "volume still attached", retryable=True)
    v = S["volumes"].get(vid)
    if not v:
        return {}
    array_ok(f"""S=/sys/kernel/config/nvmet; N={v['nqn']}
rm -f $S/ports/1/subsystems/$N
if [ -d $S/subsystems/$N ]; then
  [ -d $S/subsystems/$N/namespaces/1 ] && {{ echo 0 > $S/subsystems/$N/namespaces/1/enable; rmdir $S/subsystems/$N/namespaces/1; }}
  rm -f $S/subsystems/$N/allowed_hosts/*; rmdir $S/subsystems/$N
fi
rm -f /srv/vols/vol-{vid}.img""")
    S["volumes"].pop(vid)
    save()
    return {}


def snap_dev(nqn):
    import glob
    if DRY_RUN:
        return "/dev/nvme-dry0" if nqn in {v["nqn"] for v in S["volumes"].values()} else None
    for sd in glob.glob("/sys/class/nvme-subsystem/*"):
        if open(sd + "/subsysnqn").read().strip() == nqn:
            ns = sorted(glob.glob(sd + "/nvme*n*"))
            if ns:
                return "/dev/" + os.path.basename(ns[0])
    return None


def domain_running(dom):
    return host_run(["virsh", "domstate", dom], check=False).strip() == "running"


def volume_attach_put(aid, body):
    vid, host = body["volume"], body["host"].split("/")[-1]
    if vid not in S["volumes"]:
        raise Fail(409, "volume not created yet", retryable=True)
    h = host_of(host)
    v = S["volumes"][vid]
    a = S["volume_attach"].get(aid)
    dev = snap_dev(v["nqn"])
    if not dev:
        host_run(["nvme", "connect", "-t", "tcp", "-a", ARRAY["addr"], "-s", str(ARRAY.get("port", 4420)),
                  "-n", v["nqn"], f"--host-iface={h['snap_iface']}"])
        for _ in range(20):
            dev = snap_dev(v["nqn"])
            if dev:
                break
            time.sleep(0.5)
        if not dev:
            raise Fail(503, "NVMe/TCP connected but no namespace appeared", retryable=True)
    if not a:
        used = host_run(["virsh", "domblklist", host]).split()
        target = next(f"vd{c}" for c in "bcdefghijklmnop" if f"vd{c}" not in used)
        a = {"volume": vid, "host": host, "target": target}
        S["volume_attach"][aid] = a
        save()
    blk = host_run(["virsh", "domblklist", host])
    if dev not in blk:
        flags = ["--persistent"] if domain_running(host) else ["--config"]
        host_run(["virsh", "attach-disk", host, dev, a["target"], "--targetbus", "virtio", "--sourcetype", "block",
                  "--cache", "none", "--serial", f"snap-{vid}"[:20]] + flags)
    a["device"] = dev
    save()
    return {"target": a["target"], "serial": f"snap-{vid}"[:20], "host_device": dev}


def volume_attach_del(aid):
    a = S["volume_attach"].get(aid)
    if not a:
        return {}
    v = S["volumes"].get(a["volume"], {})
    blk = host_run(["virsh", "domblklist", a["host"]], check=False)
    if a["target"] in blk.split():
        flags = ["--persistent"] if domain_running(a["host"]) else ["--config"]
        host_run(["virsh", "detach-disk", a["host"], a["target"]] + flags)
        time.sleep(1)
    if v and snap_dev(v["nqn"]):
        host_run(["nvme", "disconnect", "-n", v["nqn"]])
    S["volume_attach"].pop(aid)
    save()
    return {}


def storage_setup():
    """Static SNAP storage network: VRF 'storage' L3VNI 29999, segment L2VNI 19999 192.168.250.0/24."""
    st = CONF["storage"]
    for node, port in st["ports"].items():
        ensure_vrf(node, st["l3_vni"])
        ensure_subnet_on(node, st["l3_vni"], st["l2_vni"], st["gateway"], st["prefixlen"])
        sh(node, f"ip link set {port} master br{st['l2_vni']}; ip link set {port} up")


def edge_policy():
    if not EDGE:
        return
    ip, blocks = EDGE["internet_port"], ", ".join(EDGE.get("block", []))
    nft = ["table ip osacedge {", " chain egress { type filter hook forward priority 0; policy accept;"]
    if blocks:
        nft.append(f'  oifname "{ip}" ip daddr {{ {blocks} }} drop')
    nft += [" }", " chain post { type nat hook postrouting priority 100; policy accept;", f'  oifname "{ip}" masquerade', " }", "}"]
    sh(EDGE["node"], "nft delete table ip osacedge 2>/dev/null || true; nft -f - <<'EOF'\n" + "\n".join(nft) + "\nEOF")


def reconcile():
    storage_setup()
    edge_policy()
    for vid in list(S["vnets"]):
        vnet_put(vid, {})
    for sid in list(S["subnets"]):
        subnet_put(sid, S["subnets"][sid])
    for aid, a in list(S["attachments"].items()):
        attach_put(aid, a)
    for node in SERVER_DPUS:
        dhcp_reload(node)
    border_nat_reload()
    return {"ok": True}


def state_get():
    return S



# ================================================================ northbound API v1 (osac-fabric-manager docs/api.md)
# Declarative, idempotent, Netris vocabulary, DPU-centric. Each v1 kind wraps one legacy handler pair: the legacy state
# (S["vnets"] ...) stays the programmed truth; S["v1"] keeps the caller's spec, metadata and generation per object.
API_VERSION = "fabric/v1"
for k in ("v1", "v1_events"):
    S.setdefault(k, {})


def _camel(d, m):
    return {m.get(k, k): v for k, v in d.items()}


def _extip_addr(name):
    e = S["extips"].get(name)
    if not e:
        raise Fail(409, f"external ip '{name}' not created yet", retryable=True)
    return e["address"]


# kind -> {key: legacy state key, Kind: object kind, put/del: legacy fns, to_legacy: spec -> legacy body,
#          status: (name, legacy record) -> dict, vpc: spec field holding the VPC, deps: name -> [dependent refs]}
V1 = {
    "vpcs": dict(key="vnets", Kind="VPC", put=vnet_put, delete=vnet_del, vpc=None,
                 to_legacy=lambda sp: {"cidr": sp["cidr"]},
                 status=lambda n, r: {"l3vni": r["l3_vni"], "vrf": vrf_name(r["l3_vni"])},
                 deps=lambda n: [f"subnets/{k}" for k, x in S["subnets"].items() if x["vnet"] == n]
                 + [f"acls/{k}" for k, x in S["sgs"].items() if x.get("vnet") == n]
                 + [f"natgateways/{k}" for k, x in S["natgws"].items() if x["vnet"] == n]
                 + [f"nats/{k}" for k, x in S["extip_attach"].items() if x.get("vnet") == n]),
    "subnets": dict(key="subnets", Kind="Subnet", put=subnet_put, delete=subnet_del, vpc="vpc",
                    to_legacy=lambda sp: {"vnet": sp["vpc"], "cidr": sp["cidr"], "gateway": sp["gateway"],
                                          "physical_cidrs": sp.get("physicalCidrs", []), "lb_vip_cidrs": sp.get("lbVipCidrs", [])},
                    status=lambda n, r: {"l2vni": r["l2_vni"], "gateway": r["gateway"]},
                    deps=lambda n: [f"attachments/{k}" for k, x in S["attachments"].items() if x["subnet"] == n]
                    + [f"loadbalancers/{k}" for k, x in S["ingress"].items() if x["subnet"] == n]),
    "acls": dict(key="sgs", Kind="ACL", put=sg_put, delete=sg_del, vpc="vpc",
                 to_legacy=lambda sp: {"vnet": sp.get("vpc"), "ingress": sp.get("ingress", []), "egress": sp.get("egress", [])},
                 status=lambda n, r: {"boundPorts": sorted(f"{x['host']}/{x['port']}" for x in S["attachments"].values() if n in x.get("sgs", []))},
                 deps=lambda n: [f"attachments/{k}" for k, x in S["attachments"].items() if n in x.get("sgs", [])]),
    "attachments": dict(key="attachments", Kind="Attachment", put=attach_put, delete=attach_del, vpc=None,
                        to_legacy=lambda sp: {"subnet": sp["subnet"], "host": sp["host"], "mac": sp["mac"], "sgs": sp.get("acls", [])},
                        status=lambda n, r: {"port": f"{r['host'].split('/')[-1]}/{r['port']}", "dpu": r["node"], "address": r["ip"],
                                             "l2vni": S["subnets"][r["subnet"]]["l2_vni"],
                                             "l3vni": S["vnets"][S["subnets"][r["subnet"]]["vnet"]]["l3_vni"],
                                             "lifecyclePort": "down" if HUB and HUB.get("cut_lifecycle_nic", True) else "up"},
                        deps=lambda n: []),
    "pools": dict(key="pools", Kind="Pool", put=pool_put, delete=pool_del, vpc=None,
                  to_legacy=lambda sp: {"cidr": sp["cidr"]}, status=lambda n, r: {"cidr": r["cidr"], "advertisedBy": BORDER},
                  deps=lambda n: [f"externalips/{k}" for k, x in S["extips"].items() if x["pool"] == n]),
    "externalips": dict(key="extips", Kind="ExternalIP", put=extip_put, delete=extip_del, vpc=None,
                        to_legacy=lambda sp: {"pool": sp["pool"]}, status=lambda n, r: {"address": r["address"]},
                        deps=lambda n: [f"nats/{k}" for k, x in S["extip_attach"].items() if x["extip"] == n]
                        + [f"natgateways/{k}" for k, x in S["natgws"].items() if x["extip"] == n]),
    "nats": dict(key="extip_attach", Kind="NAT", put=extip_attach_put, delete=extip_attach_del, vpc=None,
                 to_legacy=lambda sp: {"extip": sp["externalIp"], "address": _extip_addr(sp["externalIp"]),
                                       "dest": sp["destination"]["address"], "vnet": sp["destination"].get("vpc"),
                                       "routing_domain": "provider_management" if sp["destination"]["kind"] == "providerManagement" else "tenant_virtual_network"},
                 status=lambda n, r: {"publicAddress": r["address"], "insideAddress": r["dest"], "at": BORDER,
                                      "routingDomain": r.get("rd", "tenant_virtual_network")},
                 deps=lambda n: []),
    "natgateways": dict(key="natgws", Kind="NATGateway", put=natgw_put, delete=natgw_del, vpc="vpc",
                        to_legacy=lambda sp: {"vnet": sp["vpc"], "extip": sp["externalIp"], "address": _extip_addr(sp["externalIp"])},
                        status=lambda n, r: {"address": r["address"], "at": BORDER}, deps=lambda n: []),
    "loadbalancers": dict(key="ingress", Kind="LoadBalancer", put=ingress_put, delete=ingress_del, vpc=None,
                          to_legacy=lambda sp: {"subnet": sp["subnet"], "lb_vip_cidrs": sp.get("lbVipCidrs", [])},
                          status=lambda n, r: {"address": r["address"]}, deps=lambda n: []),
    "volumes": dict(key="volumes", Kind="Volume", put=volume_put, delete=volume_del, vpc=None,
                    to_legacy=lambda sp: {"size_gb": sp.get("sizeGb", 5)}, status=lambda n, r: {"nqn": r["nqn"], "sizeGb": r["size_gb"]},
                    deps=lambda n: [f"volumeattachments/{k}" for k, x in S["volume_attach"].items() if x["volume"] == n]),
    "volumeattachments": dict(key="volume_attach", Kind="VolumeAttachment", put=volume_attach_put, delete=volume_attach_del, vpc=None,
                              to_legacy=lambda sp: {"volume": sp["volume"], "host": sp["host"]},
                              status=lambda n, r: {"target": r["target"], "hostDevice": r.get("device")}, deps=lambda n: []),
    "cospolicies": dict(key="cos", Kind="CoSPolicy", put=cos_put, delete=None, vpc=None,
                        to_legacy=lambda sp: {"cos": sp["cos"]}, status=lambda n, r: {"cos": r}, deps=lambda n: []),
}


def v1_ref(kind, name):
    return f"{kind}/{name}"


# legacy record -> v1 spec, for objects that were created through the old paths (adopt step)
ADOPT = {
    "vpcs": lambda r: {"cidr": r["cidr"]},
    "subnets": lambda r: {"vpc": r["vnet"], "cidr": r["cidr"], "gateway": r["gateway"],
                          "physicalCidrs": r.get("physical_cidrs", []), "lbVipCidrs": r.get("lb_vip_cidrs", [])},
    "acls": lambda r: {"vpc": r.get("vnet"), "ingress": r.get("ingress", []), "egress": r.get("egress", [])},
    "attachments": lambda r: {"subnet": r["subnet"], "host": r["host"], "mac": r["mac"], "acls": r.get("sgs", [])},
    "pools": lambda r: {"cidr": r["cidr"]},
    "externalips": lambda r: {"pool": r["pool"]},
    "nats": lambda r: {"externalIp": r["extip"], "destination": (
        {"kind": "providerManagement", "address": r["dest"]} if r.get("rd") == "provider_management"
        else {"kind": "attachment", "address": r["dest"], "vpc": r.get("vnet")})},
    "natgateways": lambda r: {"vpc": r["vnet"], "externalIp": r["extip"]},
    "loadbalancers": lambda r: {"subnet": r["subnet"], "lbVipCidrs": []},
    "volumes": lambda r: {"sizeGb": r["size_gb"]},
    "volumeattachments": lambda r: {"volume": r["volume"], "host": r["host"]},
    "cospolicies": lambda r: {"cos": r},
}


def v1_adopt():
    """Give every legacy-created object a v1 record (spec rebuilt from the legacy record, generation 1, no labels),
    and drop v1 records whose legacy object is gone. Runs at startup and on POST /v1/adopt. Programs nothing."""
    adopted, dropped = [], []
    for kind, k in V1.items():
        S["v1"].setdefault(kind, {})
        for name, rec in S[k["key"]].items():
            if name not in S["v1"][kind]:
                spec = ADOPT[kind](rec)
                S["v1"][kind][name] = {"spec": spec, "labels": {}, "generation": 1, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       "vpc": spec.get(k["vpc"]) if k["vpc"] else None, "adopted": True}
                adopted.append(v1_ref(kind, name))
                v1_event(kind, name, "adopted")
        for name in [n for n in S["v1"][kind] if n not in S[k["key"]]]:
            S["v1"][kind].pop(name)
            dropped.append(v1_ref(kind, name))
    if adopted or dropped:
        save()
    return {"adopted": adopted, "dropped": dropped}


def v1_event(kind, name, what, **kw):
    S["v1_events"].setdefault(v1_ref(kind, name), []).insert(0, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "what": what, **kw})
    S["v1_events"][v1_ref(kind, name)] = S["v1_events"][v1_ref(kind, name)][:50]


def v1_conditions(kind, name, rec):
    """Ready/Programmed for one object. Attachments are Ready only once the DPU has leased the address."""
    ready, reason, msg = True, "Programmed", "desired state is programmed on the fabric"
    if kind == "attachments":
        try:
            l = lease_get(name)
            msg = f"{l['ip_address']} leased to {l['mac_address']}"
            reason = "Leased"
        except Fail as e:
            ready, reason, msg = False, "Pending", e.msg
    return [{"type": "Ready", "status": "True" if ready else "False", "reason": reason, "message": msg},
            {"type": "Programmed", "status": "True", "reason": "Applied", "message": "backend handlers ran without error"}]


def v1_object(kind, name):
    k = V1[kind]
    rec = S[k["key"]].get(name)
    meta = S["v1"].get(kind, {}).get(name)
    if rec is None or meta is None:
        return None
    return {"apiVersion": API_VERSION, "kind": k["Kind"], "name": name,
            "metadata": {"vpc": name if kind == "vpcs" else (meta["spec"].get(k["vpc"]) if k["vpc"] else meta.get("vpc")),
                         "labels": meta.get("labels", {}),
                         "generation": meta["generation"], "created": meta["created"]},
            "spec": meta["spec"],
            "status": {"observedGeneration": meta["generation"], "conditions": v1_conditions(kind, name, rec), **k["status"](name, rec)}}


def v1_list(kind, q):
    out = [v1_object(kind, n) for n in list(S["v1"].get(kind, {}))]
    out = [o for o in out if o]
    if q.get("vpc"):
        out = [o for o in out if o["metadata"]["vpc"] == q["vpc"]]
    if q.get("label"):
        lk, _, lv = q["label"].partition("=")
        out = [o for o in out if o["metadata"]["labels"].get(lk) == lv]
    return {"apiVersion": API_VERSION, "kind": V1[kind]["Kind"] + "List", "items": out}


def v1_put(kind, name, body, merge=False):
    k = V1[kind]
    spec = body.get("spec", body if "spec" not in body and "metadata" not in body else {})
    labels = (body.get("metadata") or {}).get("labels", {})
    cur = S["v1"].setdefault(kind, {}).get(name)
    if merge and cur:
        spec = {**cur["spec"], **spec}
        labels = {**cur.get("labels", {}), **labels}
    if cur and cur["spec"] == spec and cur.get("labels", {}) == labels and name in S[k["key"]]:
        return 200, v1_object(kind, name)                       # idempotent: identical desired state, nothing to do
    CTX["ref"] = v1_ref(kind, name)
    try:
        k["put"](name, k["to_legacy"](spec))                    # legacy handlers re-program idempotently
    finally:
        CTX["ref"] = None
    gen = (cur["generation"] + 1) if cur else 1
    S["v1"][kind][name] = {"spec": spec, "labels": labels, "generation": gen,
                           "created": cur["created"] if cur else time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "vpc": spec.get(k["vpc"]) if k["vpc"] else (cur or {}).get("vpc")}
    save()
    obj = v1_object(kind, name)
    v1_event(kind, name, "programmed" if cur else "created", generation=gen)
    ready = all(c["status"] == "True" for c in obj["status"]["conditions"])
    return (200 if cur else 201) if ready else 202, obj


class V1Conflict(Exception):
    def __init__(self, deps):
        super().__init__("dependents")
        self.deps = deps


def v1_delete(kind, name, cascade=False):
    k = V1[kind]
    if k["delete"] is None:
        raise Fail(405, f"{k['Kind']} objects are replaced, not deleted")
    if name not in S[k["key"]] and name not in S["v1"].get(kind, {}):
        return 204, None                                         # already gone
    deps = k["deps"](name)
    if deps and not cascade:
        raise V1Conflict(deps)
    if deps:
        for d in deps:                                           # cascade: dependents first, deepest first
            dk, dn = d.split("/", 1)
            v1_delete(dk, dn, cascade=True)
    CTX["ref"] = v1_ref(kind, name)
    try:
        k["delete"](name)
    finally:
        CTX["ref"] = None
    S["v1"].get(kind, {}).pop(name, None)
    save()
    v1_event(kind, name, "deleted")
    return 204, None


def v1_handle(method, path, body, query):
    parts = path.split("/")[2:]                                  # ["vpcs", "name", ...]
    if method == "GET" and parts == ["health"]:
        return 200, {"ok": True, "apiVersion": API_VERSION, "dryRun": DRY_RUN}
    if method == "GET" and parts == ["state"]:
        return 200, S
    if method == "GET" and parts == ["journal"]:
        return 200, JOURNAL
    if method == "POST" and parts == ["reconcile"]:
        return 200, reconcile()
    if method == "POST" and parts == ["adopt"]:
        return 200, v1_adopt()
    if method == "GET" and parts == ["ports"]:
        return 200, {"apiVersion": API_VERSION, "kind": "PortList", "items": [
            {"name": f"{p['host']}/{p['port']}", "host": p["host"], "dpu": p["node"], "role": "data", "operstate": p["operstate"],
             "attachment": p["attachment"]["id"] if p["attachment"] else None, "cos": p["cos"], "isolation": p["isolation"]}
            for p in ports_get()]}
    if not parts or parts[0] not in V1:
        raise Fail(404, f"no route {method} {path}")
    kind = parts[0]
    if len(parts) == 1:
        if method != "GET":
            raise Fail(405, "list is GET only")
        return 200, v1_list(kind, query)
    name = parts[1]
    if len(parts) == 3 and parts[2] == "events" and method == "GET":
        return 200, S["v1_events"].get(v1_ref(kind, name), [])
    if len(parts) != 2:
        raise Fail(404, f"no route {method} {path}")
    if method == "GET":
        o = v1_object(kind, name)
        if not o:
            raise Fail(404, f"{V1[kind]['Kind']} '{name}' not found")
        return 200, o
    if method in ("PUT", "PATCH"):
        return v1_put(kind, name, body, merge=(method == "PATCH"))
    if method == "DELETE":
        try:
            return v1_delete(kind, name, cascade=query.get("cascade") == "true")
        except V1Conflict as c:
            return 409, {"message": f"{V1[kind]['Kind']} '{name}' still has dependents", "retryable": True, "dependents": c.deps}
    raise Fail(405, f"{method} not allowed on {path}")


ROUTES = [  # (method, prefix, fn, takes_id, takes_body)
    ("PUT", "/vnets/", vnet_put, True, True), ("DELETE", "/vnets/", vnet_del, True, False),
    ("PUT", "/subnets/", subnet_put, True, True), ("DELETE", "/subnets/", subnet_del, True, False),
    ("PUT", "/sgs/", sg_put, True, True), ("DELETE", "/sgs/", sg_del, True, False),
    ("PUT", "/attachments/", attach_put, True, True), ("DELETE", "/attachments/", attach_del, True, False),
    ("PUT", "/pools/", pool_put, True, True), ("DELETE", "/pools/", pool_del, True, False),
    ("PUT", "/extips/", extip_put, True, True), ("DELETE", "/extips/", extip_del, True, False),
    ("PUT", "/extip-attach/", extip_attach_put, True, True), ("DELETE", "/extip-attach/", extip_attach_del, True, False),
    ("PUT", "/natgws/", natgw_put, True, True), ("DELETE", "/natgws/", natgw_del, True, False),
    ("PUT", "/ingress/", ingress_put, True, True), ("DELETE", "/ingress/", ingress_del, True, False),
    ("PUT", "/cos/", cos_put, True, True),
    ("PUT", "/volumes/", volume_put, True, True), ("DELETE", "/volumes/", volume_del, True, False),
    ("PUT", "/volume-attach/", volume_attach_put, True, True), ("DELETE", "/volume-attach/", volume_attach_del, True, False),
]


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        if b:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        if b:
            self.wfile.write(b)

    def log_message(self, *a):
        pass

    def _handle(self, method):
        path = self.path.split("?")[0].rstrip("/")
        query = dict(q.split("=", 1) if "=" in q else (q, "") for q in self.path.partition("?")[2].split("&") if q)
        if method == "GET" and path in ("/health", "/v1/health"):
            return self._send(200, {"ok": True, "objects": {k: len(v) for k, v in S.items() if isinstance(v, dict)}, "dryRun": DRY_RUN})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"message": "missing or wrong token", "retryable": False})
        body = {}
        if method in ("PUT", "POST", "PATCH"):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        try:
            with LOCK:
                if path == "/v1" or path.startswith("/v1/"):
                    code, obj = v1_handle(method, path, body, query)
                    log(op=f"{method} {path}", code=code)
                    return self._send(code, obj) if obj is not None else self._send(code, None)
                if method == "GET" and path == "/state":
                    return self._send(200, state_get())
                if method == "GET" and path == "/ports":
                    return self._send(200, ports_get())
                if method == "POST" and path == "/reconcile":
                    return self._send(200, reconcile())
                if method == "GET" and path.startswith("/attachments/") and path.endswith("/lease"):
                    return self._send(200, lease_get(path.split("/")[2]))
                for m, pre, fn, tid, tb in ROUTES:
                    if m == method and path.startswith(pre) and path.count("/") == 2:
                        rid = path[len(pre):]
                        res = fn(rid, body) if tb else fn(rid)
                        log(op=f"{method} {pre}", id=rid, result=res)
                        return self._send(200, res)
            return self._send(404, {"message": f"no route {method} {path}", "retryable": False})
        except Fail as e:
            log(op=f"{method} {path}", error=e.msg)
            return self._send(e.status, {"message": e.msg, "retryable": e.retryable})
        except Exception as e:  # noqa: BLE001
            log(op=f"{method} {path}", error=repr(e))
            return self._send(500, {"message": f"fabd internal error: {e}", "retryable": True})

    def do_GET(self):
        self._handle("GET")

    def do_PUT(self):
        self._handle("PUT")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")


def main():
    with LOCK:
        try:
            log(event="adopted", **v1_adopt())
            reconcile()
            log(event="reconciled")
        except Fail as e:
            log(event="reconcile-failed", error=e.msg)
    srv = ThreadingHTTPServer((CONF.get("bind", "0.0.0.0"), CONF.get("port", 7480)), H)
    log(event="listening", port=CONF.get("port", 7480))
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
