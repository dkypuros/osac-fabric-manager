"""Probes for the one-box mimic: containerlab FRR nodes (podman), DPU containers, libvirt servers, Linux NVMe target."""
import json
import shlex

from probes import Probes as _Base, ProbeError
from runner import sh

FABRIC_NODES = ["edge", "border", "spine-1", "spine-2", "leaf-1", "leaf-2", "dpu-0", "dpu-1", "dpu-st"]
TENANT_FACING = ("border", "dpu-0", "dpu-1")
DPUS = ("dpu-0", "dpu-1")
HOST_PORT = {"bm-0": ("dpu-0", "eth2"), "bm-1": ("dpu-1", "eth2")}          # server -> (DPU container, host-facing port)
OCNET_MAC = {"bm-0": "52:54:00:5e:1d:00", "bm-1": "52:54:00:5e:1d:01"}      # lifecycle (ocnet) NIC of each server VM
ARRAY = "student@10.251.0.40"
SSH = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5"


def podexec(node, cmd):
    p = sh(f"sudo podman exec {node} sh -c {shlex.quote(cmd)}")
    if p.returncode != 0 and not p.stdout:
        raise ProbeError(f"{node}: {p.stderr.strip()[-120:] or 'command failed'}")
    return p.stdout


class Probes(_Base):
    name = "mimic"

    def fabric_problems(self):
        bad = []
        for n in FABRIC_NODES:
            try:
                d = json.loads(podexec(n, "vtysh -c 'show bgp summary json'"))
            except (json.JSONDecodeError, ProbeError) as e:
                raise ProbeError(f"{n}: cannot read BGP summary ({e})")
            for af in d.values():
                for peer, p in (af.get("peers") or {}).items():
                    if p.get("state") != "Established":
                        bad.append(f"{n}->{peer}:{p.get('state')}")
        return sorted(set(bad))

    def tenant_vnis(self):
        out = set()
        for n in TENANT_FACING:
            out |= set(podexec(n, "vtysh -c 'show evpn vni' | awk 'NR>1 && $1 ~ /^[0-9]+$/ {print $1}'").split())
        return out

    def segment_present(self, l2_vni):
        return all(f"br{l2_vni}" in podexec(n, "ip -br link") for n in DPUS)

    def mac_learned(self, mac, where="border"):
        return mac.lower() in podexec(where, "vtysh -c 'show bgp l2vpn evpn route type macip'").lower()

    def port_binding(self, host):
        node, port = HOST_PORT[host.split("/")[-1]]
        link = json.loads(podexec(node, f"ip -j link show {port}"))[0]
        master = link.get("master") or ""
        seg = int(master[2:]) if master.startswith("br") and master[2:].isdigit() else None
        return {"segment": seg, "operstate": link.get("operstate", "UNKNOWN"), "isolated": seg is not None}

    def lifecycle_port_state(self, host):
        h = host.split("/")[-1]
        p = sh(f"sudo virsh domif-getlink {h} {OCNET_MAC[h]}")
        if p.returncode != 0:
            raise ProbeError(f"{h}: virsh domif-getlink: {p.stderr.strip()[-80:]}")
        return "down" if p.stdout.strip().endswith(" down") else "up"

    def nat_present(self, inside_ip):
        return f"dnat to {inside_ip}" in podexec("border", "nft list table ip osacnat")

    def array_exports(self):
        p = sh(f"sudo ssh -i /etc/osac-fabd/array_key {SSH[4:]} {ARRAY} 'ls /sys/kernel/config/nvmet/subsystems | wc -l'")
        if p.returncode != 0:
            raise ProbeError("storage array unreachable")
        return int(p.stdout.strip() or 0)

    def host_storage_connections(self):
        return int(sh("ls /sys/class/nvme-subsystem 2>/dev/null | wc -l").stdout.strip() or 0)

    def server_power(self, host):
        return "on" if sh(f"sudo virsh domstate {host.split('/')[-1]}").stdout.strip() == "running" else "off"

    def snapshot(self, component, d):
        try:
            if component == "fabric":
                for n in ("border", "dpu-0", "dpu-1", "leaf-1", "leaf-2"):
                    open(f"{d}/{n}.txt", "w").write(podexec(n, "vtysh -c 'show bgp summary' -c 'show evpn vni' -c 'show bgp l2vpn evpn route' ; ip -br link; ip rule; nft list ruleset"))
            elif component == "dpu":
                for n in DPUS:
                    open(f"{d}/{n}-dhcp.txt", "w").write(podexec(n, "cat /etc/fab/dnsmasq.d/*.conf /var/lib/fab/leases /var/log/fab-dnsmasq.log 2>/dev/null | tail -80"))
            elif component in ("bmc", "sushy"):
                open(f"{d}/sushy.log", "w").write(sh("sudo journalctl -u sushy-emulator --no-pager -n 200").stdout)
                open(f"{d}/virsh.txt", "w").write(sh("sudo virsh list --all; for v in bm-0 bm-1; do sudo virsh domblklist $v; done").stdout)
            elif component in ("snap", "storage_array"):
                open(f"{d}/host.txt", "w").write(sh("sudo nvme list-subsys; lsblk").stdout)
                open(f"{d}/array.txt", "w").write(sh(f"sudo ssh -i /etc/osac-fabd/array_key {SSH[4:]} {ARRAY} 'ls -R /sys/kernel/config/nvmet/subsystems; ls -la /srv/vols'").stdout)
        except Exception as e:  # noqa: BLE001  evidence collection never fails a step
            open(f"{d}/{component}-snapshot-error.txt", "w").write(repr(e))
