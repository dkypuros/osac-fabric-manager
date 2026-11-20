# How API v1 is built

The companion to [api.md](api.md) (what the API is). This page is how it is implemented, how it is tested, and how
the remaining pieces will be built, against the code as it stands. Read this before changing `fabd.py` or the
contract role. Status line at the bottom.

## 1. Where the code is
One file, `backends/mimic/fabd/fabd.py` (the copy the lab runs from is `osac-sheldon/fabd/fabd.py`; the installed
agent is `/usr/local/sbin/osac-fabd`). Sections, in file order:

| Section | What it holds |
|---|---|
| config, state, `Fail` | `CONF`, `S` (the state dict, saved to `state_file` on every change), `Fail(status, msg, retryable)` |
| exec helpers | `sh(node, script)` = `podman exec` into a fabric container; `vtysh`; `host_run` = a host command (virsh, nvme); `array` = ssh to the storage VM. **Every device access goes through these four.** |
| dry run | `journal`, `dry_sh`: when `FABD_DRY_RUN=1` the four helpers record instead of execute (section 4) |
| legacy handlers | `vnet_put/del`, `subnet_put/del`, `sg_put/del`, `attach_put/del`, `lease_get`, `pool_*`, `extip_*`, `extip_attach_*`, `natgw_*`, `ingress_*`, `cos_put`, `volume_*`, `volume_attach_*`, `reconcile`, `ports_get` |
| **northbound API v1** | `V1` table, `ADOPT` table, `v1_adopt`, `v1_event`, `v1_conditions`, `v1_object`, `v1_list`, `v1_put`, `V1Conflict`, `v1_delete`, `v1_handle` |
| HTTP | `ROUTES` (old paths), class `H` (one handler; `/v1/...` is dispatched to `v1_handle` first), `main` |

## 2. The design in one paragraph
v1 programs nothing itself. Each v1 kind is one row of the `V1` table that names the legacy state table it fronts,
the legacy `put`/`delete` handler, a `to_legacy(spec)` function that turns a v1 spec into the body the legacy
handler takes, a `status(name, record)` function that reads the programmed facts out of the legacy record, the spec
field that scopes the object to a VPC, and a `deps(name)` function that lists dependents. The legacy tables remain
the programmed truth; `S["v1"][kind][name]` keeps only what the caller said (`spec`, `labels`), a `generation`, the
creation time and, if the record came from the adopt step, `adopted: true`. Because the handlers are the ones that
passed the conformance suites, a faithful translation is the whole correctness argument; the dry run checks the
translation by comparing device-command journals (section 4), and both suites have since passed on v1 itself.

## 3. Rules the code enforces
- **PUT** (`v1_put`): body is `{spec, metadata.labels}` or a bare spec. If an object exists with an identical spec
  and labels, return `200` and call nothing. Otherwise call the legacy `put` (the handlers are idempotent
  re-programmers), bump the generation, save, and return `201` (new) or `200` (changed), or `202` if the object is
  not yet `Ready` (today only attachments can be: no lease yet). `PATCH` merges into the stored spec first.
- **GET** returns `apiVersion, kind, name, metadata{vpc, labels, generation, created}, spec, status{observedGeneration,
  conditions, …facts}`. Conditions (`v1_conditions`): `Programmed` is always true once a handler returned without
  error (the handlers are synchronous); `Ready` is true except for an attachment without a DHCP lease (`Pending`,
  flips to `Leased` on a later read, message carries the address). A VPC's `metadata.vpc` is its own name.
- **DELETE** (`v1_delete`): missing → `204`. Dependents (`deps`) and no `?cascade=true` → `409` with the list.
  With cascade, dependents are deleted first, recursively. Then the legacy `delete`, then the v1 record goes.
  `cospolicies` cannot be deleted (replaced only, `405`). Legacy dependency errors (`409 … still has …`) cannot
  happen through v1 because `deps` is checked first; if one ever does, it is a bug in a `deps` function.
- **List** `GET /v1/<kind>?vpc=&label=k=v`. **Events** `GET /v1/<kind>/<name>/events`, newest first, at most 50.
- **Adopt** (`v1_adopt`): at startup and on `POST /v1/adopt`. For every legacy record without a v1 record, build the
  spec with the `ADOPT[kind]` function (the inverse of `to_legacy`), generation 1, no labels; drop v1 records whose
  legacy object is gone. Programs nothing. This is what makes installing v1 on a live agent safe: the demo cluster's
  objects become v1 objects at the restart, and the role's first re-put of them is a `200` no-op.
- **Errors** `{"message", "retryable"}` plus `dependents` on `409`; same as the old paths so `_call.yaml` needs no
  new error handling.
- **Auth**: one bearer token (`token_file`), same as the old paths. Per-VPC tokens are deferred (section 7).

Invariant to keep when adding a kind: `ADOPT[kind](legacy_record)` must equal the spec the contract role would send
for the same object, otherwise the first re-put after an adopt reprograms (harmless, but noisy) instead of being a
no-op. The harness checks this for attachments ("re-put of an adopted attachment with the role's body -> 200").

## 4. The dry run: how it works and how to extend it
(The same script is the API conformance harness for any backend: `dryrun_v1.py --target URL --token T`; see
[conformance-harness.md](conformance-harness.md).)
`FABD_DRY_RUN=1` makes `sh`, `host_run` and `array` append `{ts, target, cmd, ref, kind}` to `JOURNAL` and return
canned output for the few reads the handlers make (`dry_sh`: link stats for `/ports`; the DHCP lease file, where an
attachment's lease appears after `FABD_DRY_LEASE_READS` looks, default 0 = at once; virsh listings). `kind` is
`read` or `write` by prefix; comparisons use writes only. `ref` is the v1 object being handled (`CTX["ref"]`), so a
journal can be grouped per object. `GET /v1/journal` returns it.

`backends/mimic/fabd/dryrun-v1.sh` (`dryrun_v1.py`) starts two agents from the repo's exported config on
127.0.0.1:7481 (lease after two reads) and :7482, both with a throwaway token and state file under a temp dir, and:
1. plays the OSAC cluster-order sequence through v1 on :7481 with 46 checks (create, 200 no-op re-put, 202→Ready,
   labels and list filters, 409 dependents, cascade, 204 on missing, events, lifecycle NIC cut/restore, clean state),
2. plays the same scenario through the old paths on :7482 and checks `adopt` makes those objects v1 objects,
3. asserts the two write journals are the same multiset of `(target, command)`.
Nothing touches `:7480`. Add a check with `check(label, condition, detail)`; add a kind by adding its `V1` and
`ADOPT` rows and a PUT/DELETE pair to both scenarios so the journal comparison covers it. Run it before every
commit that touches `fabd.py`; it takes about ten seconds.

## 5. Building the rest (in order)
### 5.1 Install on the live agent (between runs only)
Precondition: no tenant objects (owner deleted the cluster; `conformance`'s leak check clean; `GET /state` shows the
baseline only). Then: copy `fabd.py` to `/usr/local/sbin/osac-fabd`, `systemctl restart osac-fabd`, check the log
for `adopted` (empty lists) and `reconciled`, `GET /v1/health` → `dryRun: false`, `GET /v1/state`. Rollback: copy
the previous file back and restart. The old paths keep working, so the role is unaffected by this step.

### 5.2 Re-point the contract role
`contract/collection/roles/manager/tasks/`: `_call.yaml` gains `201, 202, 204` in `status_code` and treats any 2xx
as success; the result mapping moves from the legacy response to `fabric_response.status`. Per task (the crosswalk
in `api.md`): `create_virtual_network` → `PUT /v1/vpcs/<id>` body `{spec:{cidr}}`, `osac_result.l3_vni` from
`status.l3vni`; `create_subnet` → `/v1/subnets/<id>`, `l2_vni` from `status.l2vni`; `create_security_group` →
`/v1/acls/<id>`; `apply_network_attachment` → `PUT /v1/attachments/<id>` with `metadata.labels` carrying
`osac.openshift.io/owner`, then **poll** `GET` until `Ready` (`until: … conditions[0].reason == 'Leased'`,
`retries: 40`, `delay: 15` = ten minutes, inside AWX's job timeout), result from `status.address`;
`query_dhcp_lease` → `GET /v1/attachments/<id>` → `status.address`/`spec.mac`; `create_external_ip_pool` →
`/v1/pools`; `create_external_ip` → `/v1/externalips`, `address` from status; `attach_external_ip` → `PUT /v1/nats/<id>`
with `destination.kind` = `providerManagement` when `routing_domain == provider_management`, else `attachment`;
`create_nat_gateway` → `/v1/natgateways`; `reserve_cluster_ingress_ip` → `/v1/loadbalancers/<cluster>`; every
`delete_*`/`detach_*`/`release_*` → `DELETE` (204 also when already gone; a `409` with `dependents` is returned to
OSAC as retryable, which is what OSAC expects while it tears down in order). Build EE 1.0.7
(`osac-sheldon/docs/rebuild/12`), point the networking instance group at it. Rollback: previous EE tag.

### 5.3 Prove it
Suite BM three passes, Suite CL three passes, unattended (`conformance/*/run3.sh`). Done 2026-11-07.

### 5.5 Retire the old paths
The old `ROUTES` stay until their last readers move to `/v1/state` and `/v1/ports`: the DNS sync (`osac-r53-sync`),
the leak check and component checks (`conformance/common/components.py`), `contract-test.py`, and the
owner's dpu app real-backend mode. Then delete `ROUTES`, the old `/state`, `/ports`, `/reconcile`,
`/attachments/<id>/lease` dispatch and `smoke.sh`, and drop the legacy scenario from the dry-run harness (its journal
comparison has done its job; keep the adopt check by seeding a legacy-shaped state file). The janitor is **not**
retired by v1: its jobs are OSAC-internal (bug #6's ingress address, bug #9's deletion loop).

### 5.4 Keep the suites honest (probe interface: done 2026-11-07 evening, see `conformance/common/probes.py`)
`conformance/common/components.py` `leaks_mimic` already reports a leftover `S["v1"]` record as a leak. When the
role is on v1, add a component check that `GET /v1/attachments/<id>` for the worker is `Ready/Leased` with the
tenant address (replaces the lease-file look in Suite CL's handoff step).

## 6. Conventions
Lower-case file names; one file for the agent; no secrets in the repo (the token file path is config); every change
to `fabd.py` runs `dryrun-v1.sh` first and, when it touches a handler, Suite BM before Suite CL. Commit messages say
which checks changed.

## 7. Deferred on purpose
Per-VPC tokens (needs a token table and `metadata.vpc` enforcement in `v1_handle`); asynchronous programming for a
real-hardware backend (`Programmed=False` + a worker; the handler interface would gain a `status()`); a
Netris-compatible facade; `fabricdomains`.

## Status
2026-11-07 12:50: sections 1–4 built and dry-run tested (46/46). **5.1 done:** the live agent runs v1 (adopted the
baseline at restart); `smoke-v1.sh` passed on the real fabric (real DHCP: 202 Pending → Ready/Leased, NAT from
outside, cascade, clean). **5.2 done 13:30:** collection 1.0.3 calls v1 for all twenty tasks (no polling in
`apply_network_attachment`: 202 is success and `query_dhcp_lease` keeps OSAC's retry loop); `contract/collection/tests/run-live.sh`
drives the role inside the EE against the live manager and passes; EE 1.0.7 built, AWX pinned to it. **5.3 done 16:45:** Suite BM 3 × 41/41, Suite CL 3 × 36/36, unattended, leak checks clean. v1 is the API.
**Open:** 5.5 below; 5.4's direct v1 component check.
