"""Checks and evidence collectors. Vendor-neutral parts (OSAC, hub, workstation, DNS, the fabric manager API) live here;
every look at the equipment below the fabric manager goes through the probe set (probes.py, OSAC_PROBES=mimic|dell).
Each check raises StepFailed or returns truthy; each snapshot writes files into a dir."""
import json
import os
import shlex
import urllib.request

from runner import StepFailed, sh
import probes as _probes

P = _probes.active()          # the vendor-specific looks below the fabric manager (OSAC_PROBES=mimic|dell|...)
ProbeError = _probes.ProbeError

FABD = "http://127.0.0.1:7480"
TOKEN = sh("sudo cat /etc/osac-fabd/token").stdout.strip()
HUB_KUBECONFIG = os.path.expanduser("~/src/osac-sheldon/hub/cfg/auth/kubeconfig")
WS = "student@198.51.100.10"
SSH = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5"
FABRIC_NODES = ["edge", "border", "spine-1", "spine-2", "leaf-1", "leaf-2", "dpu-0", "dpu-1", "dpu-st"]
PERMANENT_VNIS = {"19999", "29999"}   # SNAP storage network


def baseline():
    """Objects that persist between tests (ExternalIPPool, each tenant's default networking): /etc/osac-fabd/baseline.json."""
    try:
        return json.loads(sh("sudo cat /etc/osac-fabd/baseline.json").stdout or "{}")
    except json.JSONDecodeError:
        return {}


def permanent_vnis():
    b, st = baseline(), fabd("/v1/state")
    v = set(PERMANENT_VNIS)
    v |= {str(st["vnets"][i]["l3_vni"]) for i in b.get("vnets", []) if i in st["vnets"]}
    v |= {str(st["subnets"][i]["l2_vni"]) for i in b.get("subnets", []) if i in st["subnets"]}
    return v


def fabd(path):
    r = urllib.request.Request(FABD + path, headers={"Authorization": f"Bearer {TOKEN}"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read())


def podexec(node, cmd):
    return sh(f"sudo podman exec {node} sh -c {shlex.quote(cmd)}").stdout


def hub(args, timeout=60):
    return sh(f"kubectl --kubeconfig {HUB_KUBECONFIG} {args}", timeout=timeout)


def ws(cmd, timeout=30):
    p = sh(f"{SSH} {WS} {shlex.quote(cmd)}", timeout=timeout)
    if p.returncode == 255:
        raise StepFailed("test-ssh-failed", f"cannot reach the outside workstation: {p.stderr.strip()[-120:]}")
    return p


# ------------------------------------------------------------------ preflight checks
def fabric_bgp_established(_ctx):
    try:
        bad = P.fabric_problems()
    except ProbeError as e:
        raise StepFailed("assertion", str(e))
    if bad:
        raise StepFailed("stuck-state", "BGP sessions not established: " + ", ".join(bad[:6]))
    return True


def fabric_no_tenant_state(_ctx):
    left = sorted(v for v in P.tenant_vnis() if v not in permanent_vnis())
    if left:
        raise StepFailed("assertion", "tenant VNIs left from an earlier run: " + ", ".join(f"vni{v}" for v in left))
    return True


def segment_present(l2_vni):
    def check(_ctx):
        return P.segment_present(l2_vni)
    return check


def lifecycle_port(host, want):
    def check(_ctx):
        got = P.lifecycle_port_state(host)
        if got not in (want, "n/a"):
            raise StepFailed("stuck-state", f"{host} lifecycle port is {got} (want {want})")
        return True
    return check


def nat_present(inside_ip):
    def check(_ctx):
        return P.nat_present(inside_ip)
    return check


def fabd_healthy(_ctx):
    h = json.loads(urllib.request.urlopen(FABD + "/health", timeout=5).read())
    if not h.get("ok"):
        raise StepFailed("assertion", "fabd not healthy")
    return True


def sushy_lists(hosts):
    def check(_ctx):
        systems = json.loads(urllib.request.urlopen("http://10.251.0.1:8000/redfish/v1/Systems", timeout=5).read())
        if len(systems.get("Members", [])) < len(hosts):
            raise StepFailed("assertion", f"sushy lists {len(systems.get('Members', []))} systems, expected >= {len(hosts)}")
        return True
    return check


def bmh_available(hosts, ns="osac-inventory"):
    def check(_ctx):
        p = hub(f"-n {ns} get bmh -o json")
        if p.returncode != 0:
            raise StepFailed("api-refused", p.stderr.strip()[-200:])
        items = {i["metadata"]["name"]: i for i in json.loads(p.stdout)["items"]}
        bad = []
        for h in hosts:
            i = items.get(h)
            if not i:
                bad.append(f"{h}:missing")
                continue
            st = i.get("status", {})
            if st.get("provisioning", {}).get("state") != "available" or st.get("operationalStatus") != "OK" or i["spec"].get("consumerRef"):
                bad.append(f"{h}:{st.get('provisioning', {}).get('state')}/{st.get('operationalStatus')}/consumer={bool(i['spec'].get('consumerRef'))}")
        if bad:
            raise StepFailed("stuck-state", "hosts not available: " + ", ".join(bad))
        return True
    return check


def array_clean(_ctx):
    try:
        n = P.array_exports()
    except ProbeError as e:
        raise StepFailed("stuck-state", str(e))
    if n:
        raise StepFailed("assertion", f"{n} volumes still exported by the array")
    return True


def dns_roundtrip(_ctx):
    """The workstation resolves through the outside resolver (fed by fake Route 53)."""
    p = ws("dig +short +time=2 quay.io | tail -1")
    if not p.stdout.strip():
        raise StepFailed("stuck-state", "workstation cannot resolve through 198.51.100.53")
    return True


def workstation_up(_ctx):
    p = ws("ip route | grep -c 'default via 198.51.100.1'")
    if p.stdout.strip() != "1":
        raise StepFailed("assertion", "workstation default route is not via the edge router")
    return True


# ------------------------------------------------------------------ workload-path checks
def fabd_state():
    return fabd("/v1/state")


def attachment_for(host):
    """The fabric manager's attachment record for this host (through the API, vendor-neutral) or None."""
    st = fabd("/v1/state")
    return next((dict(a, id=k) for k, a in st["attachments"].items() if a["host"].split("/")[-1] == host.split("/")[-1]), None)


def dpu_port_bound(host, l2_vni):
    """The server's DPU host port is a member of the tenant segment (port moved to the VNI)."""
    def check(_ctx):
        b = P.port_binding(host)
        if b.get("segment") != int(l2_vni):
            raise StepFailed("stuck-state", f"{host} DPU port segment={b.get('segment')} (want {l2_vni})")
        return b
    return check


def evpn_type2_seen(mac, node="border"):
    def check(_ctx):
        if not P.mac_learned(mac, node):
            raise StepFailed("stuck-state", f"no EVPN type-2 route for {mac} on {node}")
        return True
    return check


def ws_resolves(name, want_ip):
    def check(_ctx):
        got = ws(f"dig +short +time=2 {name} | tail -1").stdout.strip()
        if got != want_ip:
            raise StepFailed("stuck-state", f"workstation resolves {name} -> '{got}' (want {want_ip})")
        return True
    return check


def ws_reaches(ip, port=22):
    """Retried within the step budget: one refused connect is not proof of blocking (sshd may still be starting)."""
    def check(_ctx):
        p = ws(f"timeout 5 bash -c '</dev/tcp/{ip}/{port}' && echo open")
        if "open" not in p.stdout:
            raise StepFailed("stuck-state", f"traffic-blocked: workstation cannot open {ip}:{port} through the border")
        return True
    return check


def ws_cannot_reach(ip, port=22):
    def check(_ctx):
        p = ws(f"timeout 3 bash -c '</dev/tcp/{ip}/{port}' && echo open")
        if "open" in p.stdout:
            raise StepFailed("assertion", f"isolation broken: workstation reached private {ip}:{port}")
        return True
    return check


# ------------------------------------------------------------------ snapshots (evidence)
def snap_fabric(d, _ctx):
    P.snapshot("fabric", d)


def snap_dpu(d, _ctx):
    json.dump(fabd("/v1/ports"), open(f"{d}/ports.json", "w"), indent=1)
    json.dump(fabd("/v1/state"), open(f"{d}/fabd-state.json", "w"), indent=1)
    P.snapshot("dpu", d)


def snap_metal3(d, _ctx):
    open(f"{d}/bmh.yaml", "w").write(hub("-n osac-inventory get bmh -o yaml").stdout)
    open(f"{d}/bmo.log", "w").write(hub("-n baremetal-operator-system logs deploy/baremetal-operator-controller-manager --tail=300").stdout)


def snap_sushy(d, _ctx):
    P.snapshot("bmc", d)


def snap_dns(d, _ctx):
    open(f"{d}/records.conf", "w").write(sh("cat /etc/osac-dns/records.conf").stdout)
    open(f"{d}/sync.log", "w").write(sh("sudo journalctl -u osac-r53-sync --no-pager -n 50").stdout)


def snap_snap(d, _ctx):
    P.snapshot("snap", d)


SNAPSHOTS = {"fabric": snap_fabric, "dpu": snap_dpu, "metal3": snap_metal3, "sushy": snap_sushy, "dns": snap_dns,
             "snap": snap_snap, "storage_array": snap_snap}


# ------------------------------------------------------------------ leak check (mimic side)
def leaks_mimic(_ctx):
    leaks = []
    st, b = fabd("/v1/state"), baseline()
    v1_keys = {"vpcs": "vnets", "subnets": "subnets", "acls": "sgs", "attachments": "attachments", "pools": "pools",
               "externalips": "extips", "nats": "extip_attach", "natgateways": "natgws", "loadbalancers": "ingress",
               "volumes": "volumes", "volumeattachments": "volume_attach"}   # cospolicies persist per host: not checked
    for kind, objs in (st.get("v1") or {}).items():  # plan 09: a leftover v1 record is a leak too (baseline excluded)
        if kind not in v1_keys:
            continue
        for n in objs:
            if n not in b.get(v1_keys[kind], []):
                leaks.append(f"fabd v1 {kind}/{n} still recorded")
    for k in ("vnets", "subnets", "sgs", "attachments", "extips", "extip_attach", "natgws", "ingress", "volumes", "volume_attach"):
        extra = [i for i in st.get(k, {}) if i not in b.get(k, [])]
        if extra:
            leaks.append(f"fabd {k}: {extra[:3]}")
    try:
        fabric_no_tenant_state(None)
    except StepFailed as e:
        leaks.append("fabric: " + e.reason)
    try:
        array_clean(None)
    except StepFailed as e:
        leaks.append("storage array: " + e.reason)
    recs = "\n".join(l for l in sh("cat /etc/osac-dns/records.conf").stdout.splitlines()
                      if l.strip() and ".hub.osac.lab," not in l)   # hub routes published for GUI access are permanent
    if recs:
        leaks.append(f"dns records: {recs.splitlines()[:3]}")
    if P.host_storage_connections():
        leaks.append("host still has NVMe-oF connections")
    for v in ("bm-0", "bm-1"):
        if P.server_power(v) == "on":
            leaks.append(f"{v} left powered on")
    return leaks
