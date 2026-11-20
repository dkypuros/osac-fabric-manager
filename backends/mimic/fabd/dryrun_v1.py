#!/usr/bin/env python3
"""Dry run of the northbound API v1 against an idle copy of fabd (FABD_DRY_RUN=1: device commands are journaled,
not executed). Nothing here touches the live fabric agent, the fabric or any VM.

What it proves:
  1. the OSAC cluster-order call sequence works through /v1 with the semantics in docs/api.md
     (201 create, 200 identical re-put with no device commands, 204 delete of a missing object,
     409 with the dependents list, cascade delete, Ready/Programmed conditions, labels, events)
  2. the device commands v1 would issue are exactly the ones the legacy API issues for the same scenario
     (two fresh dry-run instances, journals compared)
  3. the fabric is left clean (every state table empty)
Usage: fabd/dryrun-v1.sh                       (the mimic: starts two idle copies, compares journals)
       fabd/dryrun_v1.py --target URL --token T --host bm-1 [--host2 bm-0] [--mac 52:54:00:..] [--live]
                                               (API conformance of ANY backend: the same checks, no journal
                                                comparison; objects are named v1conf-* and removed again.
                                                Without --live the harness refuses a backend whose /v1/health
                                                does not report dryRun, so a real fabric is never touched by
                                                accident; with --live it runs against real equipment.)
Exit code 0 = every check passed. Documentation: osac-fabric-manager/docs/conformance-harness.md
"""
import argparse
import json, os, secrets, subprocess, sys, tempfile, time, urllib.request, urllib.error, collections

ARGS = argparse.Namespace(target=None, token=None, host="osac-inventory/bm-1", host2="bm-0", mac="52:54:00:d7:01:02", live=False, lease_budget=600)
EXTERNAL = False      # True when --target is given: no mimic started, no journal checks
HOSTNAME = "bm-1"     # the attached server's short name (from --host at run time); the checks above read it
PFX = "v1conf-"       # every object this harness creates starts with this

HERE = os.path.dirname(os.path.abspath(__file__))


def find_config():
    """The exported backend config: osac-sheldon keeps it at config/host/etc/osac-fabd/config.json,
    osac-fabric-manager at config/host/config.json. Walk up from this file until one is found."""
    d = HERE
    for _ in range(5):
        for rel in ("config/host/etc/osac-fabd/config.json", "config/host/config.json"):
            c = os.path.join(d, rel)
            if os.path.exists(c):
                return c
        d = os.path.dirname(d)
    raise SystemExit("no exported fabd config found above " + HERE)


CONFIG = os.environ.get("FABD_DRY_CONFIG") or find_config()
WORK = tempfile.mkdtemp(prefix="fabd-dry-")
TOKEN = secrets.token_hex(16)
open(os.path.join(WORK, "token"), "w").write(TOKEN)
MAC = ARGS.mac


def start(port, env=None):
    conf = json.load(open(CONFIG))
    conf.update({"bind": "127.0.0.1", "port": port, "state_file": os.path.join(WORK, f"state-{port}.json"),
                 "token_file": os.path.join(WORK, "token")})
    cp = os.path.join(WORK, f"config-{port}.json")
    json.dump(conf, open(cp, "w"))
    log = open(os.path.join(WORK, f"fabd-{port}.log"), "w")
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "fabd.py")], env={**os.environ, "FABD_DRY_RUN": "1", "FABD_CONFIG": cp, **(env or {})},
                         stdout=log, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            if json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)).get("dryRun"):
                return p
        except Exception:
            time.sleep(0.2)
    p.kill()
    raise SystemExit(f"fabd dry-run instance on {port} did not come up; see {log.name}")


class Api:
    def __init__(self, port=None, base=None, token=None):
        self.base = base or f"http://127.0.0.1:{port}"
        self.token = token or TOKEN

    def __call__(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None)

    def journal(self, writes_only=True):
        if EXTERNAL:
            return []          # a foreign backend has no journal; journal-based checks are skipped (jcheck)
        j = self("GET", "/v1/journal")[1]
        return [x for x in j if x["kind"] == "write"] if writes_only else j


FAILS = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{(': ' + str(detail)[:200]) if detail and not ok else ''}")
    if not ok:
        FAILS.append(label)


def jcheck(label, ok, detail=""):
    """A check that reads the mimic's command journal: skipped (not failed) against a foreign backend."""
    if EXTERNAL:
        print(f"  skip {label} (journal: mimic only)")
    else:
        check(label, ok, detail)


def scenario_v1(api):
    print("v1: the cluster-order sequence")
    c, o = api("PUT", f"/v1/vpcs/{PFX}vpc-t1", {"spec": {"cidr": "10.200.0.0/24"}, "metadata": {"labels": {"osac/tenant": "t1"}}})
    check("create VPC -> 201 with l3vni", c == 201 and o["status"]["l3vni"] and o["status"]["conditions"][0]["status"] == "True", (c, o))
    n0 = len(api.journal())
    c2, o2 = api("PUT", f"/v1/vpcs/{PFX}vpc-t1", {"spec": {"cidr": "10.200.0.0/24"}, "metadata": {"labels": {"osac/tenant": "t1"}}})
    check("identical re-put -> 200, same generation", c2 == 200 and o2["metadata"]["generation"] == 1, (c2, o2 and o2["metadata"]))
    jcheck("identical re-put issued no device commands", len(api.journal()) == n0, len(api.journal()) - n0)
    c, o = api("PUT", f"/v1/subnets/{PFX}sub-1", {"spec": {"vpc": PFX+"vpc-t1", "cidr": "10.200.0.0/24", "gateway": "10.200.0.1",
                                                     "physicalCidrs": ["10.200.0.0/26"], "lbVipCidrs": ["10.200.0.192/26"]}})
    check("create subnet -> 201 with l2vni", c == 201 and o["status"]["l2vni"], (c, o))
    c, o = api("PUT", f"/v1/acls/{PFX}sg-default", {"spec": {"vpc": PFX+"vpc-t1", "ingress": [{"protocol": "all", "sourceCidr": "0.0.0.0/0"}],
                                                       "egress": [{"protocol": "all", "destinationCidr": "0.0.0.0/0"}]}})
    check("create ACL -> 201", c == 201, (c, o))
    c, o = api("PUT", f"/v1/pools/{PFX}public", {"spec": {"cidr": "203.0.113.0/24"}})
    check("create pool -> 201", c == 201, (c, o))
    c, oa = api("PUT", f"/v1/externalips/{PFX}api-ip", {"spec": {"pool": PFX+"public"}})
    c2, ob = api("PUT", f"/v1/externalips/{PFX}apps-ip", {"spec": {"pool": PFX+"public"}})
    c3, og = api("PUT", f"/v1/externalips/{PFX}gw-ip", {"spec": {"pool": PFX+"public"}})
    check("three external IPs from the pool, distinct", c == c2 == c3 == 201 and len({oa["status"]["address"], ob["status"]["address"], og["status"]["address"]}) == 3, (c, c2, c3))
    c, o = api("PUT", f"/v1/attachments/{PFX}att-w1", {"spec": {"subnet": PFX+"sub-1", "host": ARGS.host, "mac": MAC, "acls": [PFX+"sg-default"]},
                                                 "metadata": {"labels": {"osac.openshift.io/owner": "ClusterOrder/order-dry"}}})
    ready = o and o["status"]["conditions"][0]
    check("attach worker -> 202 Pending (no DHCP lease yet), port and lifecycle port in status",
          c == 202 and ready["reason"] == "Pending" and o["status"]["port"].startswith(HOSTNAME + "/") and o["status"]["lifecyclePort"] in ("down", "n/a"), (c, o))
    c, o = api("GET", f"/v1/attachments/{PFX}att-w1")
    jcheck("poll once more -> still Pending", c == 200 and o["status"]["conditions"][0]["reason"] == "Pending", (c, o))
    deadline = time.time() + (ARGS.lease_budget if EXTERNAL else 0)
    while True:
        c, o = api("GET", f"/v1/attachments/{PFX}att-w1")
        if (o and o["status"]["conditions"][0]["reason"] == "Leased") or time.time() > deadline:
            break
        time.sleep(5)
    check("poll until Ready/Leased with the address (the server got its lease)", c == 200 and o["status"]["conditions"][0]["reason"] == "Leased"
          and o["status"]["address"] in o["status"]["conditions"][0]["message"], (c, o and o["status"]))
    cut = [j for j in api.journal() if "domif-setlink bm-1" in j["cmd"] and " down" in j["cmd"]]
    jcheck("lifecycle NIC cut was journaled for the host (live + --config)", len(cut) == 2 and all(HOSTNAME in j["cmd"] for j in cut), cut)
    check("attachment reports its lifecycle port", o["status"].get("lifecyclePort") in ("down", "up", "n/a"), o["status"].get("lifecyclePort"))
    n0 = len(api.journal())
    c, o = api("PUT", f"/v1/attachments/{PFX}att-w1", {"spec": {"subnet": PFX+"sub-1", "host": ARGS.host, "mac": MAC, "acls": [PFX+"sg-default"]},
                                                 "metadata": {"labels": {"osac.openshift.io/owner": "ClusterOrder/order-dry"}}})
    check("OSAC re-issues the attach -> 200, generation unchanged (re-issued calls absorbed)", c == 200 and o["metadata"]["generation"] == 1, (c, o and o["metadata"]))
    jcheck("re-issued attach issued no device commands", len(api.journal()) == n0, len(api.journal()) - n0)
    c, o = api("GET", "/v1/attachments?label=osac.openshift.io/owner=ClusterOrder/order-dry")
    check("list attachments by owner label -> 1", c == 200 and len(o["items"]) == 1, (c, o))
    c, o = api("PUT", f"/v1/nats/{PFX}api", {"spec": {"externalIp": PFX+"api-ip", "destination": {"kind": "providerManagement", "address": "10.251.0.200"}}})
    check("NAT to the hosted API on the hub (providerManagement) -> 201", c == 201 and o["status"]["routingDomain"] == "provider_management", (c, o))
    c, o = api("PUT", f"/v1/nats/{PFX}apps", {"spec": {"externalIp": PFX+"apps-ip", "destination": {"kind": "attachment", "address": "10.200.0.2", "vpc": PFX+"vpc-t1"}}})
    check("NAT to the worker (apps) -> 201", c == 201 and o["status"]["publicAddress"] == ob["status"]["address"], (c, o))
    c, o = api("PUT", f"/v1/natgateways/{PFX}gw-t1", {"spec": {"vpc": PFX+"vpc-t1", "externalIp": PFX+"gw-ip"}})
    check("NAT gateway -> 201", c == 201, (c, o))
    c, o = api("PATCH", f"/v1/acls/{PFX}sg-default", {"spec": {"ingress": [{"protocol": "tcp", "sourceCidr": "0.0.0.0/0", "portFrom": 22, "portTo": 22}]}})
    check("PATCH ACL -> 200, generation 2, re-applied on the bound port", c == 200 and o["metadata"]["generation"] == 2 and o["status"]["boundPorts"], (c, o))
    c, o = api("GET", "/v1/ports")
    check(f"ports list shows {HOSTNAME} attached, isolation enforced", c == 200 and any(p["host"] == HOSTNAME and p["attachment"] == PFX+"att-w1" and p["isolation"] == "enforced" for p in o["items"]), (c, o))
    c, o = api("GET", f"/v1/attachments/{PFX}att-w1/events")
    check("events for the attachment", c == 200 and o and o[-1]["what"] == "created", (c, o))
    c, o = api("GET", f"/v1/vpcs?vpc={PFX}vpc-t1")
    check("list VPCs filtered", c == 200 and [i["name"] for i in o["items"]] == [PFX+"vpc-t1"], (c, o))

    print("v1: deletes")
    c, o = api("DELETE", f"/v1/vpcs/{PFX}vpc-t1")
    check("delete VPC with dependents -> 409 listing them", c == 409 and set(o["dependents"]) >= {f"subnets/{PFX}sub-1", f"acls/{PFX}sg-default", f"natgateways/{PFX}gw-t1", f"nats/{PFX}apps"}, (c, o))
    c, o = api("DELETE", f"/v1/externalips/{PFX}api-ip")
    check("delete external IP in use -> 409", c == 409 and o["dependents"] == [f"nats/{PFX}api"], (c, o))
    for path in (f"/v1/nats/{PFX}api", f"/v1/nats/{PFX}apps", f"/v1/natgateways/{PFX}gw-t1", f"/v1/externalips/{PFX}api-ip", f"/v1/externalips/{PFX}apps-ip",
                 f"/v1/externalips/{PFX}gw-ip", f"/v1/pools/{PFX}public", f"/v1/attachments/{PFX}att-w1", f"/v1/acls/{PFX}sg-default", f"/v1/subnets/{PFX}sub-1", f"/v1/vpcs/{PFX}vpc-t1"):
        c, _ = api("DELETE", path)
        check(f"delete {path} -> 204", c == 204, c)
    restored = [j for j in api.journal() if "domif-setlink bm-1" in j["cmd"] and " up" in j["cmd"]]
    jcheck("lifecycle NIC restore journaled on detach", len(restored) == 2, restored)
    end_of_teardown = len(api.journal())
    c, _ = api("DELETE", f"/v1/attachments/{PFX}att-w1")
    check("delete again (OSAC's deletion loop, bug #9) -> 204", c == 204, c)
    c, _ = api("DELETE", f"/v1/vpcs/{PFX}never-existed")
    check("delete something that never existed -> 204", c == 204, c)

    print("v1: cascade")
    api("PUT", f"/v1/vpcs/{PFX}vpc-t2", {"spec": {"cidr": "10.201.0.0/24"}})
    api("PUT", f"/v1/subnets/{PFX}sub-2", {"spec": {"vpc": PFX+"vpc-t2", "cidr": "10.201.0.0/24", "gateway": "10.201.0.1", "physicalCidrs": ["10.201.0.0/26"]}})
    api("PUT", f"/v1/attachments/{PFX}att-x", {"spec": {"subnet": PFX+"sub-2", "host": ARGS.host2, "mac": "52:54:00:d7:00:01"}})
    c, _ = api("DELETE", f"/v1/vpcs/{PFX}vpc-t2?cascade=true")
    check("cascade delete of a VPC with subnet + attachment -> 204", c == 204, c)

    if EXTERNAL:
        left = []
        for kind in ("vpcs", "subnets", "acls", "attachments", "pools", "externalips", "nats", "natgateways"):
            c, o = api("GET", f"/v1/{kind}")
            left += [f"{kind}/{i['name']}" for i in (o or {}).get("items", []) if i["name"].startswith(PFX)]
        check("none of the harness's objects remain", not left, left)
    else:
        c, st = api("GET", "/v1/state")
        leaks = {k: list(v) for k, v in st.items() if isinstance(v, dict) and v and k not in ("v1", "v1_events")}
        leaks.update({f"v1/{k}": list(v) for k, v in st["v1"].items() if v})
        check("fabric state clean afterwards (no leaked objects)", not leaks, leaks)
    return end_of_teardown


def scenario_legacy(api):
    """The same create/delete sequence through the lab-shaped API, for the journal comparison."""
    api("PUT", f"/vnets/{PFX}vpc-t1", {"cidr": "10.200.0.0/24"})
    api("PUT", f"/subnets/{PFX}sub-1", {"vnet": PFX+"vpc-t1", "cidr": "10.200.0.0/24", "gateway": "10.200.0.1", "physical_cidrs": ["10.200.0.0/26"], "lb_vip_cidrs": ["10.200.0.192/26"]})
    api("PUT", f"/sgs/{PFX}sg-default", {"vnet": PFX+"vpc-t1", "ingress": [{"protocol": "all", "sourceCidr": "0.0.0.0/0"}], "egress": [{"protocol": "all", "destinationCidr": "0.0.0.0/0"}]})
    api("PUT", f"/pools/{PFX}public", {"cidr": "203.0.113.0/24"})
    a = api("PUT", f"/extips/{PFX}api-ip", {"pool": PFX+"public"})[1]["address"]
    b = api("PUT", f"/extips/{PFX}apps-ip", {"pool": PFX+"public"})[1]["address"]
    g = api("PUT", f"/extips/{PFX}gw-ip", {"pool": PFX+"public"})[1]["address"]
    api("PUT", f"/attachments/{PFX}att-w1", {"subnet": PFX+"sub-1", "host": ARGS.host, "mac": MAC, "sgs": [PFX+"sg-default"]})
    api("PUT", f"/extip-attach/{PFX}api", {"extip": PFX+"api-ip", "address": a, "dest": "10.251.0.200", "vnet": None, "routing_domain": "provider_management"})
    api("PUT", f"/extip-attach/{PFX}apps", {"extip": PFX+"apps-ip", "address": b, "dest": "10.200.0.2", "vnet": PFX+"vpc-t1"})
    api("PUT", f"/natgws/{PFX}gw-t1", {"vnet": PFX+"vpc-t1", "extip": PFX+"gw-ip", "address": g})
    api("PUT", f"/sgs/{PFX}sg-default", {"vnet": PFX+"vpc-t1", "ingress": [{"protocol": "tcp", "sourceCidr": "0.0.0.0/0", "portFrom": 22, "portTo": 22}], "egress": [{"protocol": "all", "destinationCidr": "0.0.0.0/0"}]})
    api("GET", "/ports")
    print("adopt: objects created through the old API become v1 objects")
    c, o = api("GET", f"/v1/attachments/{PFX}att-w1")
    check("before adopt: v1 does not see the legacy attachment (404)", c == 404, (c, o))
    c, o = api("POST", "/v1/adopt")
    check("adopt -> lists every legacy object", c == 200 and set(o["adopted"]) >= {f"{k}/{PFX}{n}" for k, n in (("vpcs", "vpc-t1"), ("subnets", "sub-1"), ("acls", "sg-default"), ("attachments", "att-w1"),
          ("pools", "public"), ("externalips", "api-ip"), ("nats", "api"), ("nats", "apps"), ("natgateways", "gw-t1"))} and not o["dropped"], (c, o))
    c, o = api("GET", f"/v1/nats/{PFX}api")
    check("adopted NAT shows providerManagement destination and generation 1", c == 200 and o["spec"]["destination"]["kind"] == "providerManagement"
          and o["metadata"]["generation"] == 1 and o["status"]["publicAddress"] == a, (c, o))
    n0 = len(api.journal())
    c, o = api("PUT", f"/v1/attachments/{PFX}att-w1", {"spec": {"subnet": PFX+"sub-1", "host": ARGS.host, "mac": MAC, "acls": [PFX+"sg-default"]}})
    check("re-put of an adopted attachment with the role's body -> 200, no device commands", c == 200 and len(api.journal()) == n0, (c, len(api.journal()) - n0))
    c, o = api("DELETE", f"/v1/vpcs/{PFX}vpc-t1")
    check("adopted VPC delete -> 409 with dependents", c == 409 and f"subnets/{PFX}sub-1" in o["dependents"], (c, o))
    c, o = api("POST", "/v1/adopt")
    check("adopt again -> nothing new", c == 200 and not o["adopted"] and not o["dropped"], (c, o))
    for path in (f"/extip-attach/{PFX}api", f"/extip-attach/{PFX}apps", f"/natgws/{PFX}gw-t1", f"/extips/{PFX}api-ip", f"/extips/{PFX}apps-ip", f"/extips/{PFX}gw-ip",
                 f"/pools/{PFX}public", f"/attachments/{PFX}att-w1", f"/sgs/{PFX}sg-default", f"/subnets/{PFX}sub-1", f"/vnets/{PFX}vpc-t1"):
        api("DELETE", path)
    c, o = api("POST", "/v1/adopt")
    check("after legacy deletes, adopt drops the stale v1 records", c == 200 and len(o["dropped"]) == 11, (c, o))
    c, st = api("GET", "/v1/state")
    check("legacy instance state clean", not any(v for k, v in st["v1"].items()) and not st["vnets"] and not st["attachments"], st["v1"])


def norm(journal, skip):
    return collections.Counter((j["target"], j["cmd"]) for j in journal[skip:])


def main_external():
    api = Api(base=ARGS.target.rstrip("/"), token=ARGS.token)
    c, h = api("GET", "/v1/health")
    if c != 200:
        raise SystemExit(f"{ARGS.target}/v1/health -> {c}: is the backend up and the token right?")
    print(f"target {ARGS.target}  backend reports dryRun={h.get('dryRun')}")
    if not h.get("dryRun") and not ARGS.live:
        raise SystemExit("the backend is not in dry-run mode; pass --live to run against real equipment (objects v1conf-* are created and removed)")
    scenario_v1(api)
    print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failed checks" + (f": {FAILS}" if FAILS else ""))
    return 1 if FAILS else 0


def main():
    if EXTERNAL:
        return main_external()
    print(f"work dir {WORK}")
    p1 = start(7481, {"FABD_DRY_LEASE_READS": "2"})   # the worker's DHCP lease appears on the third look
    p2 = start(7482)
    try:
        v1, legacy = Api(7481), Api(7482)
        base1, base2 = len(v1.journal()), len(legacy.journal())
        end1 = scenario_v1(v1)
        j1 = v1.journal()[base1:end1]          # the cascade block afterwards has no legacy counterpart
        scenario_legacy(legacy)
        j2 = legacy.journal()[base2:]
        print("device commands: v1 vs legacy for the same scenario")
        c1, c2 = norm(j1, 0), norm(j2, 0)
        only1, only2 = c1 - c2, c2 - c1
        check(f"same device commands ({sum(c1.values())} via v1, {sum(c2.values())} via legacy)", not only1 and not only2)
        for lab, d in (("only v1", only1), ("only legacy", only2)):
            for (t, cmd), n in list(d.items())[:8]:
                print(f"     {lab} x{n} [{t}] {cmd[:160]!r}")
        print("what the fabric would have been told (v1 run), by target:")
        for t, n in collections.Counter(j["target"] for j in j1).most_common():
            print(f"   {t:8} {n:4} commands")
        print(f"journals: {WORK}/journal-v1.json, {WORK}/journal-legacy.json")
        json.dump(v1.journal(False), open(f"{WORK}/journal-v1.json", "w"), indent=1)
        json.dump(legacy.journal(False), open(f"{WORK}/journal-legacy.json", "w"), indent=1)
    finally:
        p1.kill(); p2.kill()
    print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failed checks" + (f": {FAILS}" if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="API v1 conformance: the mimic dry run, or any backend by --target")
    ap.add_argument("--target", help="backend base URL, e.g. http://10.0.0.5:7480 (omit for the mimic dry run)")
    ap.add_argument("--token", help="bearer token for --target")
    ap.add_argument("--host", default="osac-inventory/bm-1", help="a server the backend knows (attachment under test)")
    ap.add_argument("--host2", default="bm-0", help="a second server (cascade test)")
    ap.add_argument("--mac", default="52:54:00:d7:01:02", help="MAC for the attachment under test")
    ap.add_argument("--live", action="store_true", help="allow a backend that is not in dry-run mode")
    ap.add_argument("--lease-budget", type=int, default=600, help="seconds to wait for Ready/Leased against a live backend")
    ARGS = ap.parse_args()
    HOSTNAME = ARGS.host.rsplit('/', 1)[-1]
    EXTERNAL = bool(ARGS.target)
    MAC = ARGS.mac
    if EXTERNAL and not ARGS.token:
        ap.error("--target needs --token")
    sys.exit(main())
