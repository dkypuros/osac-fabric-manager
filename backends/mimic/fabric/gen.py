#!/usr/bin/env python3
"""Generate the mimicked data-center fabric: containerlab topology + FRR base configs.

Topology (all FRR containers):
    edge (AS65000) ─ border (AS65100, VTEP) ─ spine-1/2 (AS65200) ─ leaf-1 (AS65201), leaf-2 (AS65202)
    leaf-1 ─ dpu-0 (AS65300, VTEP)           leaf-2 ─ dpu-1 (AS65301, VTEP), dpu-st (AS65302, VTEP)

Underlay: eBGP over numbered /31s in 192.168.254.0/24; loopbacks 192.168.255.x/32.
Overlay: BGP EVPN; only border and DPUs are VTEPs; spines/leafs relay EVPN with next-hop unchanged.
Tenant VRFs/VNIs are NOT here: the dpu backend / sheldon_fabric role create them at runtime.
Host-side ports (veth into the host netns), attached to Linux bridges by up.sh:
    dpu-0:eth2 → dpu0-host (br-dpu0, bm-0 data NIC)    dpu-0:eth3 → dpu0-snap (SNAP initiator side, host)
    dpu-1:eth2 → dpu1-host (br-dpu1, bm-1 data NIC)    dpu-1:eth3 → dpu1-snap
    dpu-st:eth2 → dpust-arr (br-storage, storage-array VM)
    edge:eth2  → edge-out  (virbr-out, the "internet")
    border:eth4 → border-oc (virbr-oc, the hub network: plan 08 shared-service port)
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGE = "localhost/osac-dpu:1"

NODES = {  # name: (asn, loopback, is_vtep)
    "edge":   (65000, "192.168.255.1",  False),
    "border": (65100, "192.168.255.2",  True),
    "spine-1": (65200, "192.168.255.11", False),
    "spine-2": (65200, "192.168.255.12", False),
    "leaf-1": (65201, "192.168.255.21", False),
    "leaf-2": (65202, "192.168.255.22", False),
    "dpu-0":  (65300, "192.168.255.30", True),
    "dpu-1":  (65301, "192.168.255.31", True),
    "dpu-st": (65302, "192.168.255.39", True),
}
# (a, a_if, b, b_if); /31s allocated in order from 192.168.254.0
LINKS = [
    ("edge", "eth1", "border", "eth1"),
    ("border", "eth2", "spine-1", "eth1"),
    ("border", "eth3", "spine-2", "eth1"),
    ("spine-1", "eth2", "leaf-1", "eth1"),
    ("spine-1", "eth3", "leaf-2", "eth1"),
    ("spine-2", "eth2", "leaf-1", "eth2"),
    ("spine-2", "eth3", "leaf-2", "eth2"),
    ("leaf-1", "eth3", "dpu-0", "eth1"),
    ("leaf-2", "eth3", "dpu-1", "eth1"),
    ("leaf-2", "eth4", "dpu-st", "eth1"),
]
HOST_PORTS = [  # (node, iface, host iface)
    ("dpu-0", "eth2", "dpu0-host"), ("dpu-0", "eth3", "dpu0-snap"),
    ("dpu-1", "eth2", "dpu1-host"), ("dpu-1", "eth3", "dpu1-snap"),
    ("dpu-st", "eth2", "dpust-arr"),
    ("edge", "eth2", "edge-out"),
    ("border", "eth4", "border-oc"),   # plan 08: hub "shared services" port (virbr-oc / ocnet)
]
HUB_PORT = ("eth4", "10.251.0.2/24")  # border's address on the hub network; leaked per tenant VRF by fabd
EXT_POOL = "203.0.113.0/24"   # ExternalIP pool, advertised by border
OUTSIDE = ("198.51.100.1/24", "198.51.100.0/24")  # edge's address on the outside net


def p2p():
    """Return {(node, iface): 'ip/31'} and a list of (node, iface, peer_ip, peer_asn)."""
    addr, peers = {}, []
    for i, (a, ai, b, bi) in enumerate(LINKS):
        base = 2 * i
        ia, ib = f"192.168.254.{base}", f"192.168.254.{base + 1}"
        addr[(a, ai)], addr[(b, bi)] = f"{ia}/31", f"{ib}/31"
        peers.append((a, ai, ib, NODES[b][0]))
        peers.append((b, bi, ia, NODES[a][0]))
    return addr, peers


def frr_conf(name, addr, peers):
    asn, lo, vtep = NODES[name]
    my = [(i, ip, pasn) for n, i, ip, pasn in peers if n == name]
    relay = not vtep and name != "edge"
    out = [f"frr version 10.3", "frr defaults datacenter", f"hostname {name}",
           "no ipv6 forwarding", "service integrated-vtysh-config", "!",
           "interface lo", f" ip address {lo}/32", "exit", "!"]
    for (n, i), a in sorted(addr.items()):
        if n == name:
            out += [f"interface {i}", f" ip address {a}", "exit", "!"]
    if name == "edge":
        out += ["interface eth2", f" ip address {OUTSIDE[0]}", "exit", "!"]
    if name == "border":
        out += ["interface " + HUB_PORT[0], f" ip address {HUB_PORT[1]}", "exit", "!",
                f"ip route {EXT_POOL} blackhole", "!"]
    out += [f"router bgp {asn}", f" bgp router-id {lo}", " no bgp ebgp-requires-policy",
            " no bgp default ipv4-unicast", " bgp bestpath as-path multipath-relax"]
    out += [" neighbor FABRIC peer-group", " neighbor FABRIC remote-as external"]
    for i, ip, pasn in my:
        out.append(f" neighbor {ip} peer-group FABRIC")
    out += [" !", " address-family ipv4 unicast", f"  network {lo}/32", "  neighbor FABRIC activate"]
    if name == "edge":
        out.append(f"  network {OUTSIDE[1]}")
    if name == "border":
        out.append(f"  network {EXT_POOL}")
    out += ["  maximum-paths 8", " exit-address-family"]
    if name != "edge":
        out += [" !", " address-family l2vpn evpn", "  neighbor FABRIC activate"]
        if relay:
            out.append("  neighbor FABRIC attribute-unchanged next-hop")
        if vtep:
            out.append("  advertise-all-vni")
        out.append(" exit-address-family")
    out += ["exit", "!", "end", ""]
    return "\n".join(out)


def topology():
    lines = ["name: osacfab", "prefix: \"\"", "mgmt:", "  network: osacfab-mgmt",
             "  ipv4-subnet: 192.168.253.0/24",
             "topology:", "  defaults:", "    kind: linux", f"    image: {IMAGE}",
             "  nodes:"]
    for n in NODES:
        lines += [f"    {n}:", "      binds:",
                  f"        - configs/{n}/frr.conf:/etc/frr/frr.conf",
                  "        - configs/daemons:/etc/frr/daemons",
                  "        - configs/vtysh.conf:/etc/frr/vtysh.conf",
                  "      exec:",
                  "        - sysctl -w net.ipv4.ip_forward=1",
                  "        - sysctl -w net.ipv4.conf.all.rp_filter=0",
                  "        - sysctl -w net.ipv4.tcp_l3mdev_accept=1",
                  "        - sysctl -w net.ipv4.udp_l3mdev_accept=1"]
    lines.append("  links:")
    for a, ai, b, bi in LINKS:
        lines.append(f"    - endpoints: [\"{a}:{ai}\", \"{b}:{bi}\"]")
    for n, i, h in HOST_PORTS:
        lines.append(f"    - endpoints: [\"{n}:{i}\", \"host:{h}\"]")
    return "\n".join(lines) + "\n"


DAEMONS = """zebra=yes
bgpd=yes
ospfd=no
ospf6d=no
ripd=no
ripngd=no
isisd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
pbrd=no
bfdd=yes
fabricd=no
vrrpd=no
pathd=no
vtysh_enable=yes
zebra_options="  -A 127.0.0.1 -s 90000000"
bgpd_options="   -A 127.0.0.1"
bfdd_options="   -A 127.0.0.1"
"""


def main():
    addr, peers = p2p()
    cfg = HERE / "configs"
    cfg.mkdir(exist_ok=True)
    (cfg / "daemons").write_text(DAEMONS)
    (cfg / "vtysh.conf").write_text("service integrated-vtysh-config\n")
    for n in NODES:
        d = cfg / n
        d.mkdir(exist_ok=True)
        (d / "frr.conf").write_text(frr_conf(n, addr, peers))
    (HERE / "osacfab.clab.yml").write_text(topology())
    print(f"wrote {len(NODES)} configs + osacfab.clab.yml")


if __name__ == "__main__":
    main()
