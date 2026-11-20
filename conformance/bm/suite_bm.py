#!/usr/bin/env python3
"""Suite BM: bare-metal architecture, end to end through OSAC's API (osac CLI at the pinned commit).

Flow (test-suite.md §2): P (preflight) → N (tenant network) → BM1–BM12 (order + provision + network) →
BM13–BM18 (SNAP storage) → X (outside access) → teardown (always) → L (leak check).

Usage: suite_bm.py            (needs OSAC_CTX=<osac CLI context dir>, see tests/README)
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
import components as c  # noqa: E402
from runner import StepFailed, Suite, sh  # noqa: E402

OSAC = os.path.expanduser("~/src/osac-sheldon/bin/osac-lab")
RUN = time.strftime("bm%m%d%H%M%S")
HOST_MAC = {"bm-0": "52:54:00:5e:2d:00", "bm-1": "52:54:00:5e:2d:01"}


TENANT_CFG = os.path.expanduser("~/src/osac-sheldon/secrets/cli-tenant1")


PASSWORD_FILE = os.path.expanduser("~/src/osac-sheldon/secrets/tenant-user.password")


def login():
    """Fresh tenant1 login. The password flow stores no password, so once the session expires the CLI would wait
    for interactive input; the suite logs in at the start and before teardown instead."""
    p = sh(f"XDG_CONFIG_HOME={TENANT_CFG} {OSAC} login --flow password --user tenant1_admin "
           f"--password-file {PASSWORD_FILE} --insecure fulfillment-api-osac.apps.hub.osac.lab:443 </dev/null", timeout=60)
    if p.returncode != 0:
        raise StepFailed("api-refused", f"tenant1 login failed (rc={p.returncode}): {(p.stderr or p.stdout).strip()[-200:]}")
    return True


def osac(args, timeout=120):
    """Run the osac CLI as tenant1 (public API); never reads stdin; one re-login + retry on a hang."""
    import subprocess
    cmd = f"XDG_CONFIG_HOME={TENANT_CFG} {OSAC} {args} </dev/null"
    try:
        p = sh(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        login()
        p = sh(cmd, timeout=timeout)
    if p.returncode != 0:
        raise StepFailed("api-refused", f"osac {args.split()[0]} {args.split()[1] if len(args.split()) > 1 else ''}: "
                                        f"{(p.stderr or p.stdout).strip()[-240:]}")
    return p.stdout


def osac_json(args):
    return json.loads(osac(f"{args} -o json"))


def get(kind, name):
    """The object, or None. A missing object comes back from the CLI as [] with rc 0."""
    try:
        o = osac_json(f"get {kind} {name}")
    except StepFailed:
        return None
    if isinstance(o, list):
        o = next((x for x in o if (x.get("metadata") or {}).get("name") == name or x.get("id") == name), None)
    return o or None


def state_is(kind, name, want):
    def check(ctx):
        o = get(kind, name)
        st = ((o or {}).get("status") or {}).get("state", "")
        if st.endswith("FAILED"):
            raise StepFailed("assertion", f"{kind} {name} is {st}: {(o.get('status') or {}).get('message', '')}")
        return o if st.endswith(want) else None
    return check


def gone(kind, name):
    def check(_ctx):
        return get(kind, name) is None
    return check


def main():
    s = Suite("suite-bm", c.SNAPSHOTS)
    ctx = s.ctx
    ctx["run"] = RUN
    # ---------------- P: preflight
    s.step("P0", "api", "tenant1 logs in (Keycloak password flow)", lambda _: login())
    s.step("P1", "api", "OSAC API answers as tenant1", lambda _: osac("get subnets"))
    s.step("P4", "fabric", "all BGP sessions Established", c.fabric_bgp_established, budget=60)
    s.step("P4b", "fabric", "no tenant VNIs left from earlier runs", c.fabric_no_tenant_state)
    s.step("P5", "dpu_app", "fabd healthy", c.fabd_healthy)
    s.step("P6", "sushy", "sushy lists the servers", c.sushy_lists(["bm-0", "bm-1"]))
    s.step("P6b", "metal3", "bm-0, bm-1 available with no consumer", c.bmh_available(["bm-0", "bm-1"]), budget=120)
    s.step("P7", "storage_array", "array has no leftover namespaces", c.array_clean)
    s.step("P8", "dns", "workstation resolves through the outside resolver", c.dns_roundtrip)
    s.step("P9", "workstation", "workstation default route via the edge", c.workstation_up)

    # ---------------- N: tenant network
    vn, sn, sg = f"{RUN}-vn", f"{RUN}-sn", f"{RUN}-sg"
    s.step("N1", "api", "create VirtualNetwork", lambda _: osac(f"create virtualnetwork --name {vn} --ipv4-cidr 172.16.40.0/24") or True)
    s.on_teardown("NT3", "api", "delete VirtualNetwork", lambda _: (osac(f"delete virtualnetwork {vn}", timeout=60) or True) and gone("virtualnetwork", vn)(_), budget=600)
    s.step("N2", "fabric_manager", "VirtualNetwork READY (create_virtual_network succeeded)", state_is("virtualnetwork", vn, "READY"), budget=600, also=("awx",))
    s.step("N4", "api", "create Subnet", lambda _: osac(f"create subnet --name {sn} --virtual-network {vn} --ipv4-cidr 172.16.40.0/24") or True)
    s.on_teardown("NT2", "api", "delete Subnet", lambda _: (osac(f"delete subnet {sn}", timeout=60) or True) and gone("subnet", sn)(_), budget=600)
    s.step("N5", "fabric_manager", "Subnet READY (create_subnet succeeded)", state_is("subnet", sn, "READY"), budget=600, also=("awx", "fabric"))
    s.step("N6", "dpu", "subnet bridges and DHCP scope exist on the DPUs",
           lambda _: any(c.P.segment_present(v) for v in c.P.tenant_vnis() - c.permanent_vnis() if v.startswith("1")))
    s.step("N7", "api", "create SecurityGroup (ssh + icmp in, all out)",
           lambda _: osac(f"create securitygroup --name {sg} --virtual-network {vn} "
                          f"--ingress protocol=tcp,port-from=22,port-to=22,ipv4-cidr=0.0.0.0/0 --ingress protocol=icmp,ipv4-cidr=0.0.0.0/0 "
                          f"--egress protocol=all,ipv4-cidr=0.0.0.0/0") or True)
    s.on_teardown("NT1", "api", "delete SecurityGroup", lambda _: (osac(f"delete securitygroup {sg}", timeout=60) or True) and gone("securitygroup", sg)(_), budget=600)
    s.step("N7b", "fabric_manager", "SecurityGroup READY", state_is("securitygroup", sg, "READY"), budget=600)

    # ---------------- BM: order the server (steps filled in once the admin catalog exists)
    # BM1..BM18 and X1..X6 are added in tests/bm/steps_bm.py after the hub's admin setup (instance type,
    # catalog item, disk image, SSH key secret) is known.
    try:
        import steps_bm  # noqa: F401
        steps_bm.add(s, osac, osac_json, get, state_is, gone, vn, sn, sg)
    except ImportError:
        print("steps_bm.py not present yet: running network-only Suite BM", flush=True)

    s.failed_before_relogin = s.failed
    try:
        login()                               # the order can outlive the Keycloak session; teardown needs a live login
    except StepFailed as e:
        print("re-login before teardown failed:", e.reason)
    ok = s.finish(lambda ctx: c.leaks_mimic(ctx))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
