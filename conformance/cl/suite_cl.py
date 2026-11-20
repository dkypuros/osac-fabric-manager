#!/usr/bin/env python3
"""Suite CL: cluster architecture, end to end through OSAC's API, measured against test-suite.md §3 and plan 08.
Flow: P (preflight) → CL1–CL13 (order, control plane, worker provisioned + handed into the tenant space, joins,
cluster READY) → X (public addresses, split-horizon DNS, customer access from the workstation, isolation) →
teardown (always; the janitor unsticks the OSAC deletion loop) → L (leak check).
Uses the tenant's default network, as the portal does. Needs osac-cl-janitor running (bugs #6 and #9).
Usage: KUBECONFIG=~/.kube/osac-hub.yaml python3 suite_cl.py
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
import components as c  # noqa: E402
from runner import StepFailed, Suite, sh  # noqa: E402

OSAC = os.path.expanduser("~/src/osac-sheldon/bin/osac-lab")
TENANT_CFG = os.path.expanduser("~/src/osac-sheldon/secrets/cli-tenant1")
PASSWORD_FILE = os.path.expanduser("~/src/osac-sheldon/secrets/tenant-user.password")
RUN = time.strftime("cl%m%d%H%M%S")
NAME = RUN
OCNET_MAC = {"bm-0": "52:54:00:5e:1d:00", "bm-1": "52:54:00:5e:1d:01"}


def login():
    p = sh(f"XDG_CONFIG_HOME={TENANT_CFG} {OSAC} login --flow password --user tenant1_admin "
           f"--password-file {PASSWORD_FILE} --insecure fulfillment-api-osac.apps.hub.osac.lab:443 </dev/null", timeout=60)
    if p.returncode != 0:
        raise StepFailed("api-refused", f"tenant1 login failed: {(p.stderr or p.stdout).strip()[-200:]}")
    return True


def osac(args, timeout=120):
    cmd = f"XDG_CONFIG_HOME={TENANT_CFG} {OSAC} {args} </dev/null"
    try:
        p = sh(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        login()
        p = sh(cmd, timeout=timeout)
    if p.returncode != 0:
        raise StepFailed("api-refused", f"osac {args.split()[0]}: {(p.stderr or p.stdout).strip()[-240:]}")
    return p.stdout


def cluster():
    try:
        o = json.loads(osac(f"get cluster {NAME} -o json"))
    except StepFailed:
        return None
    if isinstance(o, list):
        o = next((x for x in o if (x.get("metadata") or {}).get("name") == NAME), None)
    return o or None


def hub_json(args):
    p = c.hub(args + " -o json")
    return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else {}


def order(ctx):
    items = hub_json("-n osac get clusterorders").get("items", [])
    cid = ctx.get("cluster_id", "")
    return next((o for o in items if cid and cid in json.dumps(o)), None)


def cond(obj, typ):
    for cd in ((obj or {}).get("status") or {}).get("conditions") or []:
        if cd.get("type") == typ:
            return cd
    return {}


def main():
    s = Suite("suite-cl", c.SNAPSHOTS)
    ctx = s.ctx
    ctx["run"] = RUN

    # ---------------- P: preflight (the lab must be idle and healthy)
    s.step("P0", "api", "tenant1 logs in", lambda _: login())
    s.step("P1", "api", "OSAC API answers as tenant1; no cluster exists", lambda _: "no objects" in osac("get clusters").lower() or None)
    s.step("P2", "hub", "no ClusterOrder / HostedCluster left on the hub",
           lambda _: not hub_json("-n osac get clusterorders").get("items") and not hub_json("get hostedclusters -A").get("items"))
    s.step("P4", "fabric", "all BGP sessions Established", c.fabric_bgp_established, budget=60)
    s.step("P5", "dpu_app", "fabd healthy", c.fabd_healthy)
    s.step("P5b", "hub", "osac-cl-janitor running (bugs #6/#9 workarounds)", lambda _: sh("systemctl is-active osac-cl-janitor").stdout.strip() == "active" or sh("pgrep -f osac-cl-janitor.py").returncode == 0)
    s.step("P6", "metal3", "bm-0, bm-1 available with no consumer", c.bmh_available(["bm-0", "bm-1"]), budget=120)
    s.step("P6b", "libvirt", "both servers' lifecycle NICs are up (restored by the last teardown)",
           lambda _: all(c.P.lifecycle_port_state(h) in ("up", "n/a") for h in OCNET_MAC))
    s.step("P8", "dns", "workstation resolves through the outside resolver", c.dns_roundtrip)
    s.step("P9", "workstation", "workstation default route via the edge", c.workstation_up)

    # ---------------- CL: order
    def create(_):
        out = osac(f"create cluster --name {NAME} --template okd-sheldon-small --version okd-4-22 --pull-secret cluster-pull-secret "
                   f"--ssh-public-key-file {os.path.expanduser('~/.ssh/id_ed25519.pub')} "
                   f"--node-set name=workers,size=1,baremetal-instance-type=bm-small --external-ip-attachment")
        m = re.search(r"Created cluster '([0-9a-f-]+)'", out)
        if not m:
            raise StepFailed("api-refused", out.strip()[-200:])
        ctx["cluster_id"] = m.group(1)
        return True
    s.step("CL1", "api", "cluster create accepted (catalog template, 1 × bm-small, auto external IPs)", create)

    def co_present(_):
        o = order(ctx)
        if o:
            ctx["order"] = o["metadata"]["name"]
        return bool(o)
    s.step("CL2", "hub", "ClusterOrder created on the hub", co_present, budget=120)
    s.step("CL3", "awx", "osac-create-hosted-cluster launched (provision job)",
           lambda _: any(j.get("type") == "provision" for j in ((order(ctx) or {}).get("status") or {}).get("provisioningJobs") or []), budget=180)
    s.step("CL4", "hypershift", "HostedCluster Available (control plane pods on the hub)",
           lambda _: any(cond(h, "Available").get("status") == "True" for h in hub_json("get hostedclusters -A").get("items", [])), budget=600)

    def lb_ip(_):
        for svc in hub_json("get svc -A -l app=kube-apiserver").get("items", []) or hub_json("get svc -A").get("items", []):
            if svc["metadata"]["name"] == "kube-apiserver" and svc["metadata"]["namespace"].startswith("osac-order-"):
                ing = ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
                if ing and ing[0].get("ip"):
                    ctx["lb_ip"] = ing[0]["ip"]
                    return True
        return False
    s.step("CL5", "hub", "kube-apiserver LoadBalancer address assigned (MetalLB)", lb_ip, budget=300)
    s.step("CL6", "assisted", "InfraEnv ready with a discovery ignition URL",
           lambda _: any(((ie.get("status") or {}).get("bootArtifacts") or {}).get("discoveryIgnitionURL") for ie in hub_json("get infraenvs -A").get("items", [])), budget=300)

    def bmi(_):
        items = hub_json("-n osac get baremetalinstances").get("items", [])
        if items:
            ctx["bmi"] = items[0]["metadata"]["name"]
            return True
        return False
    s.step("CL7", "hub", "worker BareMetalInstance created by the baremetalworker controller", bmi, budget=300)

    def provisioned(_):
        for b in hub_json("-n osac-inventory get bmh").get("items", []):
            st = ((b.get("status") or {}).get("provisioning") or {}).get("state")
            if st == "provisioned":
                ctx["host"] = b["metadata"]["name"]
                return True
            if (b.get("status") or {}).get("errorMessage"):
                raise StepFailed("assertion", f"{b['metadata']['name']}: {b['status']['errorMessage'][:160]}")
        return False
    s.step("CL8", "metal3", "worker host provisioned by Ironic over the lifecycle network", provisioned, budget=1200)

    def handoff(_):
        att = c.attachment_for(ctx["host"])
        if not att:
            return False
        ctx["worker_ip"], ctx["dpu"] = att["ip"], att["node"]
        link = c.P.lifecycle_port_state(ctx["host"])
        if link == "up":
            raise StepFailed("stuck-state", f"port moved but the lifecycle NIC is still {link.split()[-1]}")
        return True
    s.step("CL9", "fabric_manager", "HANDOFF: port bound in the tenant VNI on the DPU and the lifecycle NIC cut", handoff, budget=600, also=("fabric", "dpu"))

    def agent(_):
        for a in hub_json("-n osac get agents").get("items", []):
            ifs = ((a.get("status") or {}).get("inventory") or {}).get("interfaces") or []
            ips = [ip for i in ifs for ip in (i.get("ipV4Addresses") or [])]
            if not ips:
                return False
            if any(ip.startswith("10.251.") for ip in ips):
                raise StepFailed("assertion", f"Agent reports a lifecycle-network address: {ips}")
            if (a.get("spec") or {}).get("approved"):
                ctx["agent"] = a["metadata"]["name"]
                return True
        return False
    s.step("CL10", "assisted", "Agent registered through the fabric with only a tenant address, and approved", agent, budget=900)
    s.step("CL11", "assisted", "Agent bound to the cluster and installing",
           lambda _: any(((a.get("spec") or {}).get("clusterDeploymentName") or {}).get("name") for a in hub_json("-n osac get agents").get("items", [])), budget=600)

    def node_ready(_):
        hcs = hub_json("get hostedclusters -A").get("items", [])
        if not hcs:
            return False
        ns, hc = hcs[0]["metadata"]["namespace"], hcs[0]["metadata"]["name"]
        kc = f"/tmp/claude-1000/{RUN}.kubeconfig"
        if not os.path.exists(kc):
            p = c.hub(f"-n {ns} get secret {hc}-admin-kubeconfig -o jsonpath={{.data.kubeconfig}}")
            if p.returncode != 0 or not p.stdout:
                return False
            import base64
            open(kc, "w").write(base64.b64decode(p.stdout).decode())
            os.chmod(kc, 0o600)
        p = sh(f"kubectl --kubeconfig {kc} get nodes -o json", timeout=40)
        if p.returncode != 0:
            return False
        for n in json.loads(p.stdout).get("items", []):
            addrs = [a["address"] for a in (n.get("status") or {}).get("addresses", []) if a["type"] == "InternalIP"]
            if cond(n, "Ready").get("status") == "True":
                if not any(a.startswith("10.200.") for a in addrs):
                    raise StepFailed("assertion", f"node Ready but its address is not a tenant address: {addrs}")
                ctx["node_ip"] = addrs[0]
                return True
        return False
    s.step("CL12", "hypershift", "worker node joined and Ready with its tenant address (through the hub boundary)", node_ready, budget=1500)

    def ready(_):
        cl = cluster()
        st = ((cl or {}).get("status") or {}).get("state", "")
        if st.endswith("FAILED"):
            raise StepFailed("assertion", f"cluster {st}")
        if st.endswith("READY"):
            ctx["api_url"], ctx["console_url"] = cl["status"].get("api_url"), cl["status"].get("console_url")
            ctx["domain"] = re.sub(r"^https://api\.", "", ctx["api_url"]).split(":")[0]
            return True
        return False
    s.step("CL13", "api", "cluster READY with API and console URLs", ready, budget=900)

    # ---------------- X: public addresses, DNS, customer access, isolation
    def api_attached(_):
        for e in hub_json("-n osac get externalipattachments").get("items", []):
            if (e["metadata"].get("labels") or {}).get("osac.openshift.io/auto-created-endpoint") == "api":
                conds = (e.get("status") or {}).get("conditions") or []
                if conds and conds[-1].get("reason") == "Failed":
                    raise StepFailed("assertion", f"api attachment failed: {conds[-1].get('message', '')[:160]}")
                return bool(conds) and conds[-1].get("reason") == "Succeeded"
        return False
    s.step("X1", "fabric_manager", "automatic API ExternalIP attached (provider_management via the border's hub port)", api_attached, budget=300)
    s.step("X2", "fabric", "border DNAT for the API (public → LB) present",
           lambda _: c.P.nat_present(ctx["lb_ip"]), budget=60)
    s.step("X3", "fabric_manager", "worker public address attached (janitor workaround for OSAC bug #6)",
           lambda _: c.P.nat_present(ctx["worker_ip"]), budget=300)

    def public_dns(_):
        api = sh(f"dig +short +time=2 @198.51.100.53 api.{ctx['domain']} | tail -1").stdout.strip()
        apps = sh(f"dig +short +time=2 @198.51.100.53 console-openshift-console.apps.{ctx['domain']} | tail -1").stdout.strip()
        if api.startswith("203.0.113.") and apps.startswith("203.0.113."):
            ctx["api_pub"], ctx["apps_pub"] = api, apps
            return True
        return False
    s.step("X4", "dns", "outside resolver answers public addresses for api and *.apps (split horizon)", public_dns, budget=120)
    s.step("X5", "workstation", "workstation resolves both names to the public addresses",
           lambda _: c.ws_resolves(f"api.{ctx['domain']}", ctx["api_pub"])(_) and c.ws_resolves(f"console-openshift-console.apps.{ctx['domain']}", ctx["apps_pub"])(_), budget=60)
    s.step("X6", "workstation", "API /version answers through the border (HTTP 200)",
           lambda _: c.ws(f"curl -sk -m 8 -o /dev/null -w '%{{http_code}}' https://api.{ctx['domain']}:6443/version").stdout.strip() == "200", budget=120)
    s.step("X7", "workstation", "console answers through the border (HTTP 200)",
           lambda _: c.ws(f"curl -sk -m 10 -o /dev/null -w '%{{http_code}}' https://console-openshift-console.apps.{ctx['domain']}/").stdout.strip() == "200", budget=180)
    s.step("X8", "fabric", "isolation: workstation cannot reach the worker's private address, the LB or the hub",
           lambda _: c.ws_cannot_reach(ctx["worker_ip"])(_) and c.ws_cannot_reach(ctx["lb_ip"], 6443)(_) and c.ws_cannot_reach("10.251.0.60", 443)(_))

    # ---------------- teardown (the runner runs these last-registered-first: CLT1 delete runs first, CLT5 last)
    s.on_teardown("CLT5", "api", "no cluster and only the tenant NAT address left in OSAC",
                  lambda _: cluster() is None and sh(f"XDG_CONFIG_HOME={TENANT_CFG} {OSAC} get externalips </dev/null").stdout.count("ALLOCATED") == 1, budget=300)
    s.on_teardown("CLT4", "libvirt", "lifecycle NICs restored and servers off",
                  lambda _: all(c.P.lifecycle_port_state(h) in ("up", "n/a") and c.P.server_power(h) == "off" for h in OCNET_MAC), budget=300)
    s.on_teardown("CLT3", "metal3", "hosts back to available, no consumer", c.bmh_available(["bm-0", "bm-1"]), budget=600)
    s.on_teardown("CLT2", "hub", "ClusterOrder gone (janitor unsticks OSAC bug #9)", lambda _: order(ctx) is None, budget=1800)
    s.on_teardown("CLT1", "api", "delete cluster via OSAC", lambda _: osac(f"delete cluster {NAME}") or True, budget=120)

    try:
        login()
    except StepFailed as e:
        print("re-login before teardown failed:", e.reason)
    ok = s.finish(lambda ctx: c.leaks_mimic(ctx))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
