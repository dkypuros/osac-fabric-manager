"""Suite BM steps BM1–BM18 + outside access (X) + teardown, added by suite_bm.py after the tenant network (N).

The order is placed as tenant1 through OSAC's public API (osac CLI). SNAP storage is driven through the fabric
agent (fabd) because OSAC at 0dd5177 has no volume hook for bare metal (storage controllers off); the report
marks those steps "(outside OSAC)".
"""
import json
import os
import shlex

import components as c
from runner import StepFailed, sh

USER_DATA = os.path.join(os.path.dirname(__file__), "user-data.yaml")
OPT = "-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=8"
JUMP = f'-o ProxyCommand="ssh {OPT} -W %h:%p student@198.51.100.10"'


def guest(ip, cmd, timeout=60):
    p = sh(f"ssh {OPT} {JUMP} ubuntu@{ip} {shlex.quote(cmd)}", timeout=timeout)  # single-quoted: no local expansion
    if p.returncode == 255:
        raise StepFailed("traffic-blocked", f"cannot SSH to {ip} through the outside workstation: {p.stderr.strip()[-120:]}")
    return p


def fabd_put(path, body):
    import urllib.request
    r = urllib.request.Request(c.FABD + path, method="PUT", data=json.dumps(body).encode(),
                               headers={"Authorization": f"Bearer {c.TOKEN}", "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=120).read())
    except urllib.error.HTTPError as e:
        raise StepFailed("api-refused", f"fabd {path}: {e.read().decode()[:200]}")


def fabd_del(path):
    import urllib.request
    r = urllib.request.Request(c.FABD + path, method="DELETE", headers={"Authorization": f"Bearer {c.TOKEN}"})
    return json.loads(urllib.request.urlopen(r, timeout=120).read() or b"{}")


def add(s, osac, osac_json, get, state_is, gone, vn, sn, sg):
    ctx = s.ctx
    bmi = f"{ctx['run']}-bm"

    def ids(_):
        ctx["subnet_id"] = get("subnet", sn)["id"]
        ctx["sg_id"] = get("securitygroup", sg)["id"]
        st = c.fabd_state()
        ctx["l2_vni"] = st["subnets"][ctx["subnet_id"]]["l2_vni"]
        return True
    s.step("N8", "fabric_manager", "subnet + SG known to the fabric (fabd state)", ids)

    # ---------------- BM1–BM12: order and provision
    def order(_):
        osac(f"create baremetalinstance --name {bmi} --catalog-item bm-ubuntu --instance-type bm-small "
             f"--disk-image ubuntu-24-04-oci --ssh-key sheldon-key --external-ip-attachment "
             f"--network-attachment subnet={ctx['subnet_id']},interface=data,primary,security-groups={ctx['sg_id']} "
             f"--user-data \"$(cat {USER_DATA})\"")
        return True
    s.step("BM1", "api", "BareMetalInstance create accepted", order)
    s.on_teardown("BMT4", "metal3", "hosts back to available, no consumer", c.bmh_available(["bm-0", "bm-1"]), budget=1200)
    s.on_teardown("BMT1", "api", "delete BareMetalInstance (deprovision + offboard)",
                  lambda _: (get("baremetalinstance", bmi) is None or osac(f"delete baremetalinstance {bmi}") is not None)
                  and gone("baremetalinstance", bmi)(_), budget=1500)

    def claimed(_):
        p = c.hub("-n osac-inventory get bmh -o json")
        for h in json.loads(p.stdout)["items"]:
            if (h["spec"].get("consumerRef") or {}).get("kind") == "BareMetalInstance":
                ctx["host"] = h["metadata"]["name"]
                return h
        return None
    s.step("BM3", "metal3", "BMFO claims a BareMetalHost (consumerRef set)", claimed, budget=300)
    s.step("BM6", "metal3", "host provisioned by Ironic (OCI image)",
           lambda _: c.hub(f"-n osac-inventory get bmh {ctx['host']} -o jsonpath={{.status.provisioning.state}}").stdout == "provisioned",
           budget=1200, interval=15, also=("sushy",))
    s.step("BM12", "api", "BareMetalInstance RUNNING", state_is("baremetalinstance", bmi, "RUNNING"), budget=900, interval=15,
           also=("metal3", "dpu", "fabric"))
    s.step("BM8", "dpu", "server's DPU port is in the run's tenant VNI", lambda _: c.dpu_port_bound(ctx["host"], ctx["l2_vni"])(_))

    def lease(_):
        a = c.attachment_for(ctx["host"])
        if not a:
            raise StepFailed("stuck-state", "no fabric attachment for the host")
        ctx["private_ip"] = a["ip"]
        return True
    s.step("BM10", "fabric_manager", "fabric DHCP lease for the data NIC", lease)
    def type2(_):
        h = json.loads(c.hub(f"-n osac-inventory get bmh {ctx['host']} -o json").stdout)
        mac = json.loads(h["metadata"]["annotations"]["osac.openshift.io/interface-macs"])["data"]
        return c.evpn_type2_seen(mac)(_)
    s.step("BM9", "fabric", "server MAC/IP advertised in EVPN (type-2)", type2, budget=60)

    # ---------------- X: outside access
    def ext_ip(_):
        # table columns: TENANT DELETING PROJECT ID NAME POOL STATE ADDRESS ATTACHED
        rows = [l.split() for l in osac("get externalips").splitlines() if "auto-eip" in l and "ALLOCATED" in l]
        if not rows:
            return None
        ctx["external_ip"] = rows[-1][7]
        return True
    s.step("X1", "api", "automatic ExternalIP allocated", ext_ip, budget=120)
    s.step("X2", "fabric", "border DNAT external → private",
           lambda _: c.P.nat_present(ctx["private_ip"]))
    s.step("X5", "workstation", "workstation reaches the server's SSH through the border", lambda _: c.ws_reaches(ctx["external_ip"])(_),
           budget=180, also=("fabric", "dpu"))
    s.step("BM11", "guest", "SSH login via the outside workstation; data NIC has the leased IP",
           lambda _: ctx["private_ip"] in guest(ctx["external_ip"], "ip -4 -br addr show enp5s0").stdout, budget=120)
    s.step("X6", "fabric", "isolation: workstation cannot reach the private IP", lambda _: c.ws_cannot_reach(ctx["private_ip"])(_))

    # ---------------- BM13–BM18: SNAP storage (outside OSAC)
    vol = f"{ctx['run']}-vol"
    s.step("BM13", "snap", "(outside OSAC) create 5 GB volume on the array", lambda _: fabd_put(f"/volumes/{vol}", {"size_gb": 5}))
    s.on_teardown("BMT0b", "storage_array", "(outside OSAC) delete volume", lambda _: fabd_del(f"/volumes/{vol}") is not None, budget=120)
    s.step("BM16", "snap", "(outside OSAC) NVMe/TCP connect + hot-plug into the server",
           lambda _: fabd_put(f"/volume-attach/{vol}a", {"volume": vol, "host": ctx["host"]}))
    s.on_teardown("BMT0a", "snap", "(outside OSAC) detach volume + disconnect", lambda _: fabd_del(f"/volume-attach/{vol}a") is not None, budget=120)

    def disk_io(_):
        serial = f"snap-{vol}"[:20]
        out = guest(ctx["external_ip"],
                    f'D=$(for b in /sys/block/vd*; do [ "$(cat $b/serial 2>/dev/null)" = "{serial}" ] && basename $b; done | head -1); [ -n "$D" ] || exit 3; '
                    f'sudo dd if=/dev/urandom of=/tmp/blk bs=1M count=16 2>/dev/null; '
                    f'sudo dd if=/tmp/blk of=/dev/$D bs=1M count=16 oflag=direct 2>/dev/null; '
                    f'echo W=$(md5sum < /tmp/blk | cut -c1-32) R=$(sudo dd if=/dev/$D bs=1M count=16 iflag=direct 2>/dev/null | md5sum | cut -c1-32)')
        if out.returncode == 3:
            raise StepFailed("stuck-state", f"no disk with serial {serial} in the guest yet")
        w, r = [x.split("=")[1] for x in out.stdout.split()]
        if w != r:
            raise StepFailed("assertion", f"read-back checksum {r} != written {w}")
        ctx["disk_md5"] = w
        return True
    s.step("BM17", "guest", "guest sees the SNAP disk; write/read-back checksum matches", disk_io, budget=120)
    s.step("BM18", "storage_array", "data landed on the array",
           lambda _: ctx["disk_md5"] in sh(f"sudo ssh -i /etc/osac-fabd/array_key {OPT} student@10.251.0.40 "
                                           f"'sudo dd if=/srv/vols/vol-{vol}.img bs=1M count=16 2>/dev/null | md5sum'").stdout)
