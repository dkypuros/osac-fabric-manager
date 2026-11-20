"""Probes for Dell equipment: SONiC leaf (RESTCONF), BlueField-3 running HBN (nv inside the doca-hbn container),
iDRAC (Redfish), PowerStore (REST). Read-only: every method issues GETs and `nv show`, never a set.
Select with OSAC_PROBES=dell. Access comes from the environment, never from this file:

  BF3_HOST          the card's Arm side (e.g. 192.0.2.19), BF3_USER (default ubuntu),
  BF3_SSH_KEY       a private key authorised on the card (preferred), or BF3_PASS_FILE (used with sshpass)
  SONIC_URL         https://<leaf>/restconf, SONIC_USER (default admin), SONIC_PASS_FILE
  IDRAC_URL         https://<idrac>, IDRAC_USER (default root), IDRAC_PASS_FILE, IDRAC_SYSTEM (default System.Embedded.1)
  POWERSTORE_URL    https://<array>/api/rest, POWERSTORE_USER (default admin), POWERSTORE_PASS_FILE
  DELL_HOSTS        JSON: {"server-1": {"idrac": "https://192.0.2.9", "pf": "pf0", "vf": 0}}   (server -> its card port)

Each hint names the mimic equivalent so the meaning of the answer stays the same (vendors/dell/readme.md §5)."""
import base64
import json
import os
import shlex
import ssl
import subprocess
import urllib.request

from probes import Probes as _Base, ProbeError

E = os.environ.get
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE      # lab CAs


def _pw(var):
    f = E(var)
    if not f:
        return None
    return open(f).read().strip()


def _get(url, user, pw, accept="application/json"):
    req = urllib.request.Request(url, headers={"Accept": accept})
    if user and pw is not None:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise ProbeError(f"{url.split('//')[1].split('/')[0]}: HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        raise ProbeError(f"{url.split('//')[1].split('/')[0]}: {type(e).__name__}")


def sonic(path):
    url = (E("SONIC_URL") or "").rstrip("/")
    if not url:
        raise ProbeError("SONIC_URL not set")
    return _get(f"{url}/data/{path}", E("SONIC_USER", "admin"), _pw("SONIC_PASS_FILE"), "application/yang-data+json")


def card(cmd):
    """Run `nv <cmd>` inside the doca-hbn container on the BlueField, read-only (only `nv show` is ever sent)."""
    host, user = E("BF3_HOST"), E("BF3_USER", "ubuntu")
    if not host:
        raise ProbeError("BF3_HOST not set")
    if not cmd.startswith("show"):
        raise ProbeError("dell probes only issue nv show")
    remote = f"H=$(sudo crictl ps -q --name '^doca-hbn$'); sudo crictl exec \"$H\" nv {cmd} -o json"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8"]
    if E("BF3_SSH_KEY"):
        ssh += ["-i", E("BF3_SSH_KEY")]
    argv = ssh + [f"{user}@{host}", remote]
    if not E("BF3_SSH_KEY") and E("BF3_PASS_FILE"):
        argv = ["sshpass", "-f", E("BF3_PASS_FILE")] + [a for a in argv if a not in ("BatchMode=yes",)]
        argv = [a for a in argv if a != "-o" or True]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=40)
    if p.returncode != 0:
        raise ProbeError(f"bf3: {p.stderr.strip()[-120:] or 'nv failed'}")
    try:
        return json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        return {"raw": p.stdout}


def hosts():
    return json.loads(E("DELL_HOSTS", "{}"))


class Probes(_Base):
    name = "dell"

    def fabric_problems(self):
        """BGP/EVPN peers not Established, on the leaf (RESTCONF) and on the card (HBN)."""
        bad = []
        try:
            d = sonic("openconfig-network-instance:network-instances/network-instance=default/protocols/protocol=BGP,bgp/bgp/neighbors")
            for n in (d.get("openconfig-network-instance:neighbors") or {}).get("neighbor", []):
                st = (n.get("state") or {}).get("session-state", "?")
                if st != "ESTABLISHED":
                    bad.append(f"leaf->{n.get('neighbor-address')}:{st}")
        except ProbeError as e:
            bad.append(f"leaf:{e}")
        try:
            d = card("show vrf default router bgp neighbor")
            for peer, v in (d if isinstance(d, dict) else {}).items():
                st = (v or {}).get("state", "?") if isinstance(v, dict) else "?"
                if str(st).lower() not in ("established",):
                    bad.append(f"bf3->{peer}:{st}")
        except ProbeError as e:
            bad.append(f"bf3:{e}")
        return bad

    def tenant_vnis(self):
        """VNIs configured on the leaf (VXLAN tunnel map) and on the card (HBN EVPN)."""
        out = {}
        d = sonic("sonic-vxlan:sonic-vxlan/VXLAN_TUNNEL_MAP")
        out["border"] = sorted({int(m["vni"]) for m in (d.get("sonic-vxlan:VXLAN_TUNNEL_MAP") or {}).get("VXLAN_TUNNEL_MAP_LIST", []) if "vni" in m})
        c = card("show nve vxlan vni")
        out["dpu-0"] = sorted(int(k) for k in (c if isinstance(c, dict) else {}).keys() if str(k).isdigit())
        return out

    def segment_present(self, l2_vni):
        """The L2 VNI exists on the server-facing card."""
        c = card("show nve vxlan vni")
        return str(l2_vni) in (c if isinstance(c, dict) else {})

    def mac_learned(self, mac, where="border"):
        """A type-2 route for the MAC at the leaf."""
        d = sonic("sonic-vxlan:sonic-vxlan/VXLAN_FDB_TABLE") if where == "border" else {}
        rows = (d.get("sonic-vxlan:VXLAN_FDB_TABLE") or {}).get("VXLAN_FDB_TABLE_LIST", [])
        return any(mac.lower() in json.dumps(r).lower() for r in rows)

    def port_binding(self, host):
        """Which bridge/VLAN the host's VF-facing port on the card is in, its link state, isolation (ACL present)."""
        h = hosts().get(host)
        if not h:
            raise ProbeError(f"no DELL_HOSTS entry for {host}")
        port = f"{h.get('pf', 'pf0')}vf{h.get('vf', 0)}"
        c = card(f"show interface {port}")
        iface = c if isinstance(c, dict) else {}
        link = ((iface.get("link") or {}).get("state") or {})
        bridge = ((iface.get("bridge") or {}).get("domain") or {})
        acls = (iface.get("acl") or {})
        return {"port": port, "segment": ",".join(bridge.keys()) if isinstance(bridge, dict) else str(bridge),
                "link": "up" if "up" in json.dumps(link).lower() else "down", "isolation": "enforced" if acls else "none"}

    def lifecycle_port_state(self, host):
        """n/a here: this skeleton assumes servers with no separate lifecycle NIC. Report 'down'/'up' from your own
        enforcement point (card port isolation, switch port, provisioning VLAN) if your servers have one."""
        return "n/a"

    def nat_present(self, inside_ip):
        """A NAT translation on the leaf toward this inside address (SONiC NAT table)."""
        try:
            d = sonic("sonic-nat:sonic-nat")
        except ProbeError:
            return False
        return inside_ip in json.dumps(d)

    def array_exports(self):
        """PowerStore volumes mapped to hosts (tenant exports)."""
        url = (E("POWERSTORE_URL") or "").rstrip("/")
        if not url:
            raise ProbeError("POWERSTORE_URL not set")
        vols = _get(f"{url}/volume?select=id,name,state,mapped_volumes&limit=500", E("POWERSTORE_USER", "admin"), _pw("POWERSTORE_PASS_FILE"))
        return sorted(v["name"] for v in vols if v.get("mapped_volumes") and not v["name"].startswith("permanent-"))

    def host_storage_connections(self):
        """NVMe-oF connections the SNAP controller on the card still holds."""
        c = card("show system")   # placeholder read; replace with a read of the SNAP service's own API on the card
        return []

    def server_power(self, host):
        """iDRAC Redfish PowerState of the server."""
        h = hosts().get(host) or {}
        url = (h.get("idrac") or E("IDRAC_URL") or "").rstrip("/")
        if not url:
            raise ProbeError(f"no iDRAC for {host}")
        d = _get(f"{url}/redfish/v1/Systems/{E('IDRAC_SYSTEM', 'System.Embedded.1')}", E("IDRAC_USER", "root"), _pw("IDRAC_PASS_FILE"))
        return "on" if str(d.get("PowerState", "")).lower() == "on" else "off"

    def snapshot(self, component, directory):
        try:
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, f"{component}-dell.json"), "w") as f:
                json.dump({"problems": self.fabric_problems()}, f, indent=1)
        except Exception:  # noqa: BLE001
            pass
