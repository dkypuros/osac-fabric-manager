#!/usr/bin/env python3
"""osac-cl-janitor: automatic workarounds for two OSAC bugs around tenant clusters on Sheldon.

  bug #6  the automatic *ingress* ExternalIPAttachment never attaches (subnet looked up by ID, order stores
          the name) -> once the cluster is READY, give the worker a public address of its own: a manual
          ExternalIP from the pool attached to the worker's BareMetalInstance (admin API, tenant-scoped).
  bug #9  cluster deletion loops on the automatic ExternalIPs (operator deletes the CRs, fulfillment-controller
          re-creates them) -> pause deploy/fulfillment-controller in two phases: until the deprovision job
          exists (then resume so the worker can be released), and again once the control plane and the
          worker are gone, until the ClusterOrder CR disappears (then resume: it finalizes the leftovers).

Loop every 20 s. State: ~/.local/state/osac-cl-janitor/state.json. JSON log lines on stdout (journal).
"""
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
KUBE = f"{HOME}/.kube/osac-hub.yaml"
SEC = f"{HOME}/src/osac-sheldon/secrets/osac-admin.secret"
API = "https://fulfillment-internal-api-osac.apps.hub.osac.lab/api/private/v1"
KC = "https://keycloak-keycloak.apps.hub.osac.lab/realms/osac/protocol/openid-connect/token"
STATE_DIR = f"{HOME}/.local/state/osac-cl-janitor"
STATE = f"{STATE_DIR}/state.json"
PAUSE_MAX = 900          # never keep the controller paused longer than this
INTERVAL = 20
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
OWNER = "osac.openshift.io/owner-reference"
EP = "osac.openshift.io/auto-created-endpoint"
FOR = "osac.openshift.io/auto-created-for"


def log(**kw):
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw}), flush=True)


def kubectl(args, timeout=60):
    return subprocess.run(["kubectl", "--kubeconfig", KUBE] + args, capture_output=True, text=True, timeout=timeout)


def kget(args):
    p = kubectl(args + ["-o", "json"])
    if p.returncode != 0 or not p.stdout.strip():
        return {"items": []}
    return json.loads(p.stdout)


_tok = {"v": None, "t": 0}


def token():
    if _tok["v"] and time.time() - _tok["t"] < 240:
        return _tok["v"]
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": "osac-admin",
                                   "client_secret": open(SEC).read().strip()}).encode()
    with urllib.request.urlopen(urllib.request.Request(KC, data=data), timeout=15, context=CTX) as r:
        _tok["v"] = json.load(r)["access_token"]
    _tok["t"] = time.time()
    return _tok["v"]


def priv(method, path, body=None):
    req = urllib.request.Request(f"{API}/{path}", data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            t = r.read().decode()
            return r.status, (json.loads(t) if t.strip() else {})
    except urllib.error.HTTPError as e:
        t = e.read().decode()
        try:
            return e.code, json.loads(t)
        except json.JSONDecodeError:
            return e.code, {"message": t[:200]}


def load():
    try:
        st = json.load(open(STATE))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("ingress", {})
    st.setdefault("teardown", {})
    return st


def save(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- fulfillment-controller pause/resume
def scale(n):
    kubectl(["-n", "osac", "scale", "deploy/fulfillment-controller", f"--replicas={n}"])


def pause(t):
    scale(0)
    t["paused_at"] = time.time()
    log(event="fulfillment-controller-paused", order=t.get("order"), phase=t.get("phase"))


def resume(t):
    scale(1)
    t.pop("paused_at", None)
    log(event="fulfillment-controller-resumed", order=t.get("order"), phase=t.get("phase"))


def paused_too_long(t):
    return "paused_at" in t and time.time() - t["paused_at"] > PAUSE_MAX


# ---------------------------------------------------------------- bug #6: worker public address
def worker_bmi(tenant, order):
    code, bmis = priv("GET", "baremetal_instances")
    for b in (bmis.get("items") or []) if code == 200 else []:
        md = b.get("metadata", {})
        if md.get("tenant") == tenant and (md.get("annotations") or {}).get(OWNER) == f"ClusterOrder/{order}":
            return b
    return None


def ensure_ingress(st, orders, eipas):
    for e in eipas:
        lb = e["metadata"].get("labels") or {}
        if lb.get(EP) != "ingress":
            continue
        cid = lb.get(FOR, "")
        co = next((o for o in orders if cid and cid in json.dumps(o)), None)
        if not co or co["metadata"].get("deletionTimestamp") or cid in st["ingress"]:
            continue
        conds = (e.get("status") or {}).get("conditions") or []
        if conds and conds[-1].get("reason") == "Succeeded":
            continue                                         # OSAC managed it (bug fixed upstream)
        code, cl = priv("GET", f"clusters/{cid}")
        if code != 200 or (cl.get("status") or {}).get("state") != "CLUSTER_STATE_READY":
            continue
        tenant, order = cl["metadata"]["tenant"], co["metadata"]["name"]
        bmi = worker_bmi(tenant, order)
        if not bmi:
            log(event="no-worker-bmi", order=order)
            continue
        code, pools = priv("GET", "external_ip_pools")
        if code != 200 or not pools.get("items"):
            continue
        name = f"janitor-ingress-{cid[-5:]}"
        code, eip = priv("POST", "external_ips", {"metadata": {"name": name, "tenant": tenant}, "spec": {"pool": {"id": pools["items"][0]["id"]}}})
        if code != 200:
            log(event="eip-create-failed", order=order, code=code, body=str(eip)[:200])
            continue
        st["ingress"][cid] = {"eip": eip["id"], "att": None, "name": name, "order": order, "tenant": tenant, "bmi": bmi["id"]}
        save(st)
        log(event="ingress-eip-created", order=order, eip=eip["id"])
    for cid, j in list(st["ingress"].items()):
        if j.get("att"):
            continue
        code, eip = priv("GET", f"external_ips/{j['eip']}")
        if code != 200 or (eip.get("status") or {}).get("state") != "EXTERNAL_IP_STATE_ALLOCATED":
            continue
        code, att = priv("POST", "external_ip_attachments", {"metadata": {"name": j["name"], "tenant": j["tenant"]},
                                                            "spec": {"external_ip": {"id": j["eip"]}, "baremetal_instance": {"id": j["bmi"]}}})
        if code == 200:
            j["att"] = att["id"]
            save(st)
            log(event="ingress-attached", order=j["order"], address=(eip.get("status") or {}).get("address"))
        else:
            log(event="attach-failed", order=j["order"], code=code, body=str(att)[:200])


def cleanup_ingress(st, cid, j):
    """Delete the janitor's attachment, then its ExternalIP. True once both are gone."""
    if j.get("att"):
        priv("DELETE", f"external_ip_attachments/{j['att']}")
        code, a = priv("GET", f"external_ip_attachments/{j['att']}")
        if code == 200 and a.get("id"):
            return False                                     # detach in progress
        j["att"] = None
        save(st)
        log(event="ingress-attachment-deleted", order=j["order"])
    priv("DELETE", f"external_ips/{j['eip']}")
    code, e = priv("GET", f"external_ips/{j['eip']}")
    if code == 200 and e.get("id"):
        return False
    st["ingress"].pop(cid, None)
    save(st)
    log(event="ingress-eip-deleted", order=j["order"])
    return True


# ---------------------------------------------------------------- bug #9: deletion loop
def unstick(st, o):
    name = o["metadata"]["name"]
    t = st["teardown"].setdefault(name, {"phase": "start", "order": name})
    for cid, j in list(st["ingress"].items()):          # the janitor's own objects go first
        if j["order"] == name and not cleanup_ingress(st, cid, j):
            return
    jobs = " ".join(x.get("type", "") for x in (o.get("status") or {}).get("provisioningJobs") or [])
    hc = len(kget(["get", "hostedclusters", "-A"])["items"])
    busy = any(((b.get("status") or {}).get("provisioning") or {}).get("state", "") not in ("available", "")
               for b in kget(["-n", "osac-inventory", "get", "bmh"])["items"])
    ph = t["phase"]
    if ph == "start":
        if "deprovision" in jobs:
            t["phase"] = "p1b"
        else:
            t["phase"] = "p1"
            pause(t)
    elif ph == "p1":
        if "deprovision" in jobs or paused_too_long(t):
            t["phase"] = "p1b"
            resume(t)
    elif ph == "p1b":
        if hc == 0 and not busy:
            t["phase"] = "p2"
            pause(t)
    elif ph == "p2":
        if paused_too_long(t):
            t["phase"] = "p2-timeout"
            resume(t)
    save(st)


def tick(st):
    orders = kget(["-n", "osac", "get", "clusterorders"])["items"]
    eipas = kget(["-n", "osac", "get", "externalipattachments"])["items"]
    ensure_ingress(st, orders, eipas)
    for o in orders:
        if o["metadata"].get("deletionTimestamp"):
            unstick(st, o)
    names = {o["metadata"]["name"] for o in orders}
    for name, t in list(st["teardown"].items()):
        if name not in names:
            if "paused_at" in t:
                resume(t)
            log(event="order-gone", order=name, phase=t["phase"])
            st["teardown"].pop(name)
            save(st)
    for cid, j in list(st["ingress"].items()):         # orphans (order gone without a teardown pass)
        if j["order"] not in names:
            cleanup_ingress(st, cid, j)


def main():
    st = load()
    if not any("paused_at" in t for t in st["teardown"].values()):
        scale(1)                                             # never start with the controller left paused
    log(event="start", ingress=len(st["ingress"]), teardowns=len(st["teardown"]))
    while True:
        try:
            tick(st)
        except Exception as e:  # noqa: BLE001
            log(event="tick-error", error=f"{type(e).__name__}: {e}"[:300])
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
