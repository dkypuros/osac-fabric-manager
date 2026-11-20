# Dell onboarding: from a proven tenant floor to OSAC placing clusters in it

**Who this is for.** A Dell lab that already has BlueField-3 doing the enforcement at the server, SONiC switches
carrying EVPN east-west and north-south, SNAP presenting PowerStore volumes to hosts, its own public DNS, and a
virtual machine proven to live in a tenant's space. That floor is yours and stays as it is.

**Where this ends.** OSAC orders a bare-metal server and hands it into that same tenant space; then OSAC builds a
one-worker OKD cluster there, publishes its public addresses and names, and deletes it cleanly; three unattended
passes of each, measured by the same suites we hold ourselves to.

**How the two meet.** Your fabric programming becomes a backend of this fabric manager's API; your equipment
becomes readable to the suites through eleven probes. Nothing above the API changes.

Chapters: [1 what you inherit](#1-what-you-inherit) · [2 the line](#2-the-line-api-objects-as-obligations-on-your-equipment) ·
[3 networks and reachability](#3-networks-and-reachability) · [4 your backend](#4-your-backend) ·
[5 your probes](#5-your-probes) · [6 storage](#6-storage-snap-on-powerstore) · [7 glue and test the bottom](#7-glue-into-the-bottom-and-test-it) ·
[8 the order of proof](#8-the-order-of-proof) · [9 first-day checklist](#9-first-day-checklist)

---

## 1. What you inherit

| Piece | Where | State |
|---|---|---|
| OSAC on an OKD hub (fulfillment, operator, UI, AWX as the AAP stand-in, HyperShift built at OSAC's commit, Metal3/Ironic, assisted service) | `osac-sheldon/docs/rebuild/09–16`, pinned in `versions.md` | installed and running orders daily |
| The contract role `kypuros.fabric.manager` (collection 1.0.3) — every OSAC network operation as one call to API v1 | `contract/collection/`, crosswalk in `docs/api.md` | proven by both suites |
| API v1: declarative, idempotent, Netris vocabulary, DPU-centric | `docs/api.md`, `docs/api-implementation.md` | proven by both suites |
| The design the contract leaves to the provider: hub reachability from tenants (leaked prefixes, policed, SNAT at the border), public names (split horizon), internet egress, the lifecycle-path cut on handoff | `docs/architecture.md`, `osac-sheldon/docs/plan-documentation/08` | proven |
| Ten OSAC bugs with symptoms and workarounds; the janitor that applies two of them automatically | `docs/shims.md`, `osac-sheldon/docs/plan-documentation/06` | in use |
| The suites and their reports; timings (bare-metal order ≈ 15 min, cluster ≈ 40 min to Ready, teardown ≈ 12 min) | `conformance/`, `conformance/reports/` | 3 × 41/41, 3 × 36/36 on 2026-11-07 |
| The one-box mimic (containerlab FRR fabric, DPU containers, libvirt servers, Linux NVMe target) | `backends/mimic/` | the reference implementation to read, and to run on a laptop |

You install the hub pieces as documented, register your fabric manager name (`config/hub/osac-fabric-registration.yaml`;
the NetworkClass that refers to it is immutable, choose the name once: `docs/naming.md`), and point the role at your
backend's URL and token (the networking instance group's ConfigMap and Secret). Everything else in this table is
finished.

## 2. The line: API objects as obligations on your equipment

Each object says what must be true on the equipment when the call returns. How you make it true is your expertise.
Reply shapes, idempotency and conditions are in `docs/api.md`; this table is the Dell reading of it.

| API object | Must accomplish | SONiC (leaf/spine/border) | BlueField-3 (HBN/DOCA) | Other |
|---|---|---|---|---|
| `vpcs/<id>` `{cidr}` | an isolated L3 domain for the tenant | VRF + L3 VNI + RT import/export on the border leaf (and any leaf that routes for it); a default route toward the edge; the hub's shared-service prefixes leaked in, policed (§3) | VRF + L3 VNI on every DPU that may host this tenant | reply `status.l3vni`, `status.vrf` |
| `subnets/<id>` `{vpc, cidr, gateway, physicalCidrs, lbVipCidrs}` | an L2 segment in that VRF with a gateway and DHCP for servers | L2 VNI mapped in the VRF; anycast gateway | L2 VNI + bridge/VLAN on every DPU; anycast gateway; DHCP scope handing out `physicalCidrs` with `gateway` and the tenant DNS | reply `status.l2vni` |
| `acls/<id>` `{vpc, ingress, egress}` | a stateful per-port filter | — | ACL/flow rules applied on the host port of every attachment that names it; DHCP always allowed | re-applied when rules change (PATCH) |
| `attachments/<id>` `{subnet, host, mac, acls}` | the server's data port is in the segment, filtered, and has an address; the server's lifecycle path is no longer a back door | — | bind the host port to the L2 VNI; apply the ACLs; DHCP reservation for `mac` from `physicalCidrs`; **cut or isolate the provisioning path** (§3) | `202` until the DHCP lease exists, then `Ready/Leased` with `status.address`; delete restores the lifecycle path |
| `pools/<id>` `{cidr}` | the public range is routable to the border | advertise the pool (BGP) and blackhole it at the border | — | |
| `externalips/<id>` `{pool}` | one address reserved | — | — | reply `status.address` |
| `nats/<id>` `{externalIp, destination{kind, address, vpc}}` | public → inside translation at the border | DNAT public→inside, SNAT inside→public, hairpin for the tenant's own traffic to its public address; `kind: providerManagement` targets the hub network (a hosted cluster's API) through the border's hub port | — | |
| `natgateways/<id>` `{vpc, externalIp}` | tenant egress leaves as the gateway address | SNAT of the VPC's ranges at the border | — | |
| `loadbalancers/<cluster>` | reserve a VIP in the subnet's `lbVipCidrs` | — | — | OSAC does not call it today; kept for conformance |
| `volumes/<id>` `{sizeGb}`, `volumeattachments/<id>` `{volume, host}` | a block device appears in the server | — | SNAP controller presents an NVMe namespace backed by NVMe-oF to PowerStore (§6) | PowerStore volume + host mapping |
| `cospolicies/<host>` `{cos}` | DSCP treatment on the host port | — | mark / rewrite / drop on the host port | |
| `ports` (read) | what the suites look at | — | host port → segment, link state, isolation | |

Semantics your backend must keep (the dry run checks them without hardware): identical `PUT` is a `200` no-op;
`DELETE` of a missing object is `204`; `DELETE` with dependents is `409` listing them unless `?cascade=true`; every
object carries `Ready`/`Programmed` conditions and an event list; labels are stored and filterable.

## 3. Networks and reachability

The mimic collapses networks that a data center keeps apart. Name yours and check each path before OSAC uses it.

| Role | What travels | Must reach | Trust |
|---|---|---|---|
| Out-of-band (iDRAC) | Redfish from Ironic; the agent ISO fetched by iDRAC as virtual media | the hub's Ironic/BMO endpoints; the hub ingress that serves the ISO (`assisted-image-service`) | iDRAC must trust the hub ingress CA for HTTPS media, or the ISO URL is served over HTTP inside the OOB network |
| Provisioning (server, before handoff) | PXE/virtual-media boot, agent → assisted service, Ironic agent → Ironic, ignition fetch | hub ingress `:443`, Ironic `:6385/:6180`, ignition server | the agent trusts the hub CA (Ironic CA bundle; `osac-sheldon/docs/rebuild/10` §10.5) |
| Tenant data (after handoff) | the server's only path: DPU → leaf → spine → border | its tenant only, plus the leaked hub prefixes below, plus the internet through the NAT gateway | tenant VRF; ACLs on the DPU |
| Hub shared service (from tenants) | a cluster worker reaching the assisted service, the ignition server, its own API server | `hub ingress/32 tcp 443, 30000–32767` and the hosted-API LoadBalancer range `tcp 6443`, nothing else | leaked into each VRF at the border, filtered, SNAT'd to the border's hub-port address so the hub needs no return routes (`docs/architecture.md`) |
| Storage VNI | NVMe-oF from the SNAP controller to PowerStore | the array's NVMe/TCP portals | its own VRF/VNI, never the tenant's |
| Public edge | NAT'd tenant traffic and inbound to public addresses; the outside resolver | the internet; your public DNS | the edge masquerades toward the internet and drops private/lab destinations |

**The lifecycle-path cut.** In our design a server that sits in a tenant segment has exactly one path, through its
DPU. The mimic enforces it by taking the provisioning NIC's link down at attach and up at delete (Ironic needs it
again for cleaning). On Dell, decide how you enforce the same statement: BlueField host-port isolation, a switch
port shutdown on the provisioning NIC, or a PXE-only VLAN that carries nothing after boot. Whatever you choose, the
probe `lifecycle_port_state(host)` must report `down` while attached and `up` afterwards (or `n/a` if the server has
no second path at all). Suite CL checks it at handoff and at teardown.

**Public names.** OSAC publishes inside addresses to DNS and never maps an external IP to a name. The mimic's DNS
sync (`osac-r53-sync`) gives the outside view the public address by reading the manager's NAT state. Your DNS needs
the same split: inside records as OSAC writes them, outside records rewritten through your NAT table.

## 4. Your backend

Read `backends/mimic/fabd/fabd.py` once, top to bottom (about 1,250 lines, standard-library Python): the handlers
are the order of operations for each object, the `V1` table is the API layer, `ADOPT` is how pre-existing objects
are taken over, `reconcile()` is how the whole desired state is re-applied at start. Keep the API layer and replace
the handlers, or write your own server in any language against `docs/api.md`; the role and the suites do not care.

Two things are design, not mimic convenience, and must survive the port: the lifecycle-path cut (§3) and the hub
shared-service leak with its policy and SNAT (§3). Two things are mimic convenience and should not be copied: the
border being an FRR container doing nftables NAT (yours is a switch or a gateway), and SNAP being libvirt hot-plug.

Switch-over is two settings: your registration ConfigMap on the hub (name, role, `network_types: evpn`,
capabilities) and the role's `SHELDON_FABRIC_URL`/`SHELDON_FABRIC_TOKEN` (rename the prefix with the registration
name) in the networking instance group.

## 5. Your probes

The suites take eleven read-only looks at the equipment: `conformance/common/probes.py` is the interface with one
docstring per look saying exactly what the answer means and which steps use it; `probes_mimic.py` is the reference;
`probes_dell.py` is the skeleton with a hint per method naming the SONiC, HBN, iDRAC or PowerStore source. Fill it in
and select it with `OSAC_PROBES=dell`. Everything vendor-neutral (OSAC's API, the hub, the outside workstation, DNS,
the fabric manager's own API) stays in `components.py` untouched.

| Probe | Answer | Dell source |
|---|---|---|
| `fabric_problems()` | BGP/EVPN peers not Established | SONiC `show bgp summary` (+ `l2vpn evpn`), HBN on each DPU |
| `tenant_vnis()` | VNIs on border + DPUs | SONiC VNI table, HBN |
| `segment_present(l2_vni)` | segment exists on every server-facing DPU | HBN bridge/VLAN |
| `mac_learned(mac)` | type-2 route seen at the border | SONiC EVPN MAC/IP table |
| `port_binding(host)` | the host port's segment, link, isolation | HBN port membership + ACL state |
| `lifecycle_port_state(host)` | `down` while attached | your enforcement point (§3) |
| `nat_present(inside_ip)` | DNAT installed at the border | your border's NAT table |
| `array_exports()` | tenant volumes exported | PowerStore REST: volumes mapped to hosts |
| `host_storage_connections()` | NVMe-oF sessions still held | SNAP controller |
| `server_power(host)` | `on`/`off` | iDRAC Redfish `PowerState` |
| `snapshot(component, dir)` | evidence on failure | SONiC/HBN show output, iDRAC lifecycle log, PowerStore volume list |

Rules: read only; a few seconds per answer; "not yet" is normal (the suite retries within each step's budget); raise
`ProbeError` with a short reason when a device cannot be reached.

## 6. Storage: SNAP on PowerStore

OSAC's contract has no tenant storage operation yet. This manager defines one anyway, `volumes` and
`volumeattachments` on a storage VNI, because the bare-metal design has a `storage` port role and the mimic proved
the path end to end (a Linux NVMe/TCP target standing in for the array). Suite BM uses it: create a 5 GB volume,
attach it to the ordered server, write and read back a checksum inside the server, detach, delete, confirm the array
is clean.

On Dell: `volumes/<id>` becomes a PowerStore volume; `volumeattachments/<id>` becomes the host mapping on PowerStore
plus the SNAP controller on the server's BlueField presenting the namespace (the server sees an NVMe disk, which is
better than the mimic's virtio disk). The reply carries the serial the suite looks for inside the guest
(`snap-<volume id>`, first 20 characters). `array_exports()` and `host_storage_connections()` are the two probes that
keep the leak check honest. This is a head start on the day OSAC adds storage to the contract, not a requirement to
pass the cluster suite (Suite CL has no storage steps).

## 7. Glue into the bottom, and test it

This is the order that keeps every step small and tells you exactly where you are.

**7.1 Semantics without hardware.** Start your backend in a dry-run mode (device commands logged, not executed;
`/v1/health` reports `dryRun: true`; the mimic's `FABD_DRY_RUN=1` is the pattern) and run the conformance harness at it:

```bash
backends/mimic/fabd/dryrun_v1.py --target http://<your-backend>:<port> --token <bearer> --host <server> --host2 <server>
```
It creates `v1conf-*` objects, plays the whole cluster-order sequence (create, identical re-put, 202 pending then
Ready, labels and lists, 409 with dependents, cascade, 204 on missing, events) and removes them; exit code 0 is a
pass. Full documentation, options, safety and what `--live` does: [docs/conformance-harness.md](../../docs/conformance-harness.md).
Fix semantics here; it costs seconds per iteration.

**7.2 One tenant by hand, live.** `backends/mimic/fabd/smoke-v1.sh` is the scenario: VPC, subnet, ACL, two servers
(the mimic uses network namespaces; you use two real servers or two VMs on hosts behind BlueFields), DHCP lease
flipping the attachment to `Ready/Leased`, east-west ping, gateway ping, a public address reached from outside
through your border NAT, egress through the NAT gateway, `409` with dependents, cascade delete, nothing left.
Rewrite the two `mk` lines for your servers; keep the rest. Then `OSAC_PROBES=dell python3 -c 'import probes; p=probes.active(); print(p.fabric_problems(), p.tenant_vnis())'`
from `conformance/common/` to prove the probes read your equipment.

**7.3 The role against your backend, no AWX.** `contract/collection/tests/run-live.sh <ee-image>` runs the twenty
tasks from source inside the execution environment against your backend URL with a fake server for the lease: create,
lease failing retryably before the server asks, lease after, NAT, provider-management NAT, NAT gateway, cluster
ingress, deletes in OSAC's order. Point `SHELDON_FABRIC_URL` at your backend and replace the `mk`/`rm_` lines.

**7.4 Switch OSAC over.** Register your name; set the role's URL and token; build the execution environment
(`osac-sheldon/docs/rebuild/12` §A.1, a few minutes) and pin AWX to it. Rollback is the previous tag.

**7.5 Suite BM on one server.** `conformance/bm/run3.sh` with `OSAC_PROBES=dell` and `OSAC_CTX` pointing at a
tenant's CLI context. 41 steps, about 15 minutes on the mimic; expect longer POST times on real servers and raise
the budgets in `suite_bm.py` if a step times out on time alone. The first failure names the stage, the component and
writes evidence under `conformance/reports/<timestamp>/`. The handoff step (`BM9`), the lease (`BM10`), the public
address from outside (`X5`) and isolation (`X6`) are where a real fabric differs from the mimic.

**7.6 Suite CL.** `conformance/cl/run3.sh`. The cluster suite adds the hub shared-service path (worker registers
through the fabric with only a tenant address), the lifecycle cut at handoff, the two public addresses (API via
`providerManagement`, apps via the janitor), split-horizon DNS and reaching the console from outside. Three passes.

**7.7 Keep the janitor running** on a host with hub access (`shims/janitor/`). Its two jobs are OSAC-internal bugs
that have nothing to do with your equipment; without it Suite CL's ingress and teardown steps fail on any fabric.

## 8. The order of proof

| Stage | Command | Passes when | Typical time |
|---|---|---|---|
| Conformance harness | `dryrun_v1.py --target <your backend>` | `PASS: 0 failed checks` | seconds |
| Live smoke | `smoke-v1.sh` adapted | every line prints a 2xx and the pings answer | minutes |
| Role test | `run-live.sh` | `RESULT: PASS` | 5 min |
| Suite BM | `conformance/bm/run3.sh` | `ALL 3 CONSECUTIVE RUNS PASSED` | 3 × ~20 min |
| Suite CL | `conformance/cl/run3.sh` | `ALL 3 CONSECUTIVE RUNS PASSED` | 3 × ~60 min |

When the third cluster pass is clean you have what the mimic has: OSAC placing real servers and real clusters inside
your tenant space, unattended, repeatable, with proof.

## 9. First-day checklist

Each item takes minutes and saves an hour inside a failing order.

- [ ] iDRAC: Redfish answers; virtual media insert of the hub's ISO URL works by hand (`curl` the URL from a host on
      the OOB network first, then insert through Redfish; one-time boot to it); license level allows it
- [ ] iDRAC trusts the hub ingress CA (or the ISO URL is HTTP inside the OOB network)
- [ ] Provisioning network reaches the hub ingress `:443`, Ironic, the ignition server; the agent ISO boots and the
      server appears as an Agent on the hub
- [ ] Hub shared-service prefixes leaked into one tenant VRF and policed; from a host in the tenant, hub `:443`
      answers, hub `:6443` outside the hosted-API range and the hub's SSH are blocked
- [ ] Border: a public address DNAT'd to a tenant host is reachable from outside; the tenant host's egress leaves as
      the NAT gateway address; private ranges are not reachable from outside
- [ ] Outside DNS: a name OSAC publishes with an inside address resolves outside to the public address
- [ ] PowerStore: a volume mapped to one host appears as an NVMe disk on that host through SNAP; unmapping removes it
- [ ] Probes: `OSAC_PROBES=dell` answers all eleven methods against the equipment in under ten seconds each
- [ ] Registration ConfigMap applied; the role's URL and token set; EE pinned
- [ ] Janitor running with hub access
