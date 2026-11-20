# Northbound API (v1)

The fabric manager's own API: the thing an operator, an OEM backend, OSAC's contract role and the conformance
suites all talk to. It is **declarative and idempotent**, it uses the **Netris vocabulary where it fits** (so a team
that tests OSAC against Netris finds familiar objects), and it is **DPU-centric** where Netris is switch-centric
(a port is a server's DPU port, and the security group, the CoS marking and the storage binding live there).

Status: **the fabric manager's API** (2026-11-07): served by the mimic backend, called by every contract task
(collection 1.0.3), proven by Suite BM 3 × 41/41 and Suite CL 3 × 36/36 unattended. `fabd.py`
answers `/v1/...` beside the original paths (`docs/backends.md`, deprecated) as thin wrappers over the same handlers.
`backends/mimic/fabd/dryrun-v1.sh` starts two idle copies of the backend in dry-run mode (device commands are
journaled, not executed) and plays the OSAC cluster-order sequence through v1 and through the old API: 36 checks
(create, identical re-put is a no-op, 204 on missing, 409 with dependents, cascade, conditions, labels, events,
clean state) and the 65 device commands come out identical both ways. How it is built, tested and what comes next: [api-implementation.md](api-implementation.md).

## Principles
1. **Desired state in, observed state out.** `PUT /v1/<kind>/<name>` with the full desired object; the response
   carries `spec` (what you sent) and `status` (what the fabric did). `GET` returns both. `PUT` twice with the same
   body is a no-op; `DELETE` of something that does not exist returns 204. OSAC re-issues calls and sometimes never
   issues the matching delete (upstream bugs #6, #7, #9); an idempotent API absorbs that, which is why the janitor
   shim can go once the role runs against v1.
2. **Names, not ids.** The caller chooses the name (OSAC passes its own object ids; the role uses them as names), so
   a retry never creates a duplicate.
3. **Every object has `status.conditions`** (`Ready`, `Programmed`, `Degraded`) with a `reason` and a message, and
   `status.observedGeneration`. A long operation returns `202` with the object in `Programmed=False` and the caller
   polls; nothing blocks for more than a few seconds.
4. **Tenant scoping.** Every object except pools, fabric domains and cos policies belongs to a `vpc`. A token is
   either `admin` (all VPCs) or bound to one VPC.
5. **Errors** are `{"message", "retryable", "code"}`; the contract role maps them to `osac_error`.
6. **Versioned path** `/v1/…`; breaking changes make `/v2/…`.

## Objects

| Kind (path) | Netris name | OSAC contract operation(s) | What the backend programs |
|---|---|---|---|
| `vpcs` | VPC | `create/delete_virtual_network` | IP-VRF, L3 VNI, route targets on the border and every DPU; the leaked shared-service routes |
| `subnets` | Subnet | `create/delete_subnet` | MAC-VRF, L2 VNI, anycast gateway, DHCP scope on the DPUs; `physicalCidrs`, `lbVipCidrs` |
| `acls` | ACL | `create/delete_security_group` | per-port filter rules on the DPU (nftables in the mimic, DOCA/HBN on a BlueField-3) |
| `ports` | Switch port (ours: **DPU port**) | discovered, not created | the server-side ports the backend knows: host, DPU, MAC, role (`data`, `storage`, `lifecycle`), link state |
| `attachments` | Server attachment (VNet member) | `apply/delete_network_attachment`, `query_dhcp_lease` (as `status.lease`) | bind a port to a subnet, apply ACLs, lease an address, **cut the lifecycle port** while attached |
| `pools` | IPAM allocation (public) | `create/delete_external_ip_pool` | pool advertised by the border (blackhole + BGP) |
| `externalips` | IPAM address | `create/delete_external_ip` | an address allocated from a pool |
| `nats` | NAT (DNAT) | `attach/detach_external_ip` | DNAT/SNAT at the border; `destination.kind` is `attachment` or `providerManagement` (the hub door) |
| `natgateways` | NAT (SNAT) | `create/delete_nat_gateway` | SNAT of the VPC's ranges to the gateway address |
| `loadbalancers` | L4 LB | `reserve/release_cluster_ingress_ip` (never called by OSAC today) | a VIP for a cluster's ingress, kept for conformance |
| `volumes`, `volumeattachments` | (none; Netris has no storage) | outside the contract (`storage` port role) | SNAP: NVMe/TCP volume on the storage VNI presented to the server as a local disk |
| `cospolicies` | QoS | outside the contract | per-host CoS/DSCP treatment: `kept`, `rewritten`, `dropped` |
| `fabricdomains` | Site / fabric | `create/delete_fabric_domain` (not implemented) | reserved for east-west domains |

Read-only, admin only: `GET /v1/state` (everything, for the leak check), `GET /v1/health` (no token),
`POST /v1/reconcile` (re-program everything from desired state; the mimic already has it).

## Object shape
Every object:
```json
{
  "apiVersion": "fabric/v1", "kind": "Attachment", "name": "att-7f3a",
  "metadata": {"vpc": "vpc-21", "labels": {"osac.openshift.io/owner": "ClusterOrder/order-2wk6k"},
               "generation": 3, "created": "2026-11-07T15:10:02Z"},
  "spec":   {"subnet": "sub-9", "port": "bm-1/dpu0", "acls": ["sg-default"], "cutLifecyclePort": true},
  "status": {"observedGeneration": 3,
             "conditions": [{"type": "Ready", "status": "True", "reason": "Leased", "message": "10.200.0.2/24 via 10.200.0.1"}],
             "lease": {"address": "10.200.0.2", "mac": "52:54:00:aa:01:02", "expires": "2026-11-07T19:10:02Z"},
             "programmed": {"dpu": "dpu-0", "l2vni": 10009, "l3vni": 50021}}
}
```
`labels` carry OSAC's owner references so an operator can answer "what in the fabric belongs to this order"
without OSAC; the janitor needs exactly that today and finds it by annotation on the hub instead.

## Verbs
```
GET    /v1/<kind>                 list (filter: ?vpc=, ?label=k=v)
GET    /v1/<kind>/<name>          read (spec + status)
PUT    /v1/<kind>/<name>          create or replace desired state (200 unchanged, 201 created, 202 programming)
PATCH  /v1/<kind>/<name>          merge into spec
DELETE /v1/<kind>/<name>          204 (also when absent); 409 with `dependents` if something still refers to it
                                  unless ?cascade=true
GET    /v1/<kind>/<name>/events   what the backend did to this object, newest first
```
Auth: `Authorization: Bearer <token>`; tokens are issued per VPC or `admin` and are never in this repo.

## Crosswalk to the contract role
The role's twenty task files stay as they are from OSAC's point of view; inside, each becomes one or two calls:

| Task | Calls |
|---|---|
| `create_virtual_network` | `PUT /v1/vpcs/<id>` → returns `status.l3vni` as `l3_vni` |
| `create_subnet` | `PUT /v1/subnets/<id>` → `status.l2vni` as `network_segment.l2_vni` |
| `create_security_group` | `PUT /v1/acls/<id>` |
| `apply_network_attachment` | `PUT /v1/attachments/<id>`; poll until `Ready`; return `status.lease` |
| `query_dhcp_lease` | `GET /v1/attachments/<id>` → `status.lease` |
| `create_external_ip_pool` / `create_external_ip` | `PUT /v1/pools/<id>` / `PUT /v1/externalips/<id>` → `status.address` |
| `attach_external_ip` | `PUT /v1/nats/<id>` with `destination` = `{kind: attachment, name}` or `{kind: providerManagement, address}` |
| `create_nat_gateway` | `PUT /v1/natgateways/<id>` |
| `reserve_cluster_ingress_ip` | `PUT /v1/loadbalancers/<cluster>` |
| every `delete_*` / `detach_*` / `release_*` | `DELETE /v1/<kind>/<id>` (204 when already gone) |

## What a Netris user will notice
Same nouns for VPC, subnet, ACL, NAT, L4 LB and IPAM; the same "declare it, the controller programs it" model.
Different: the attachment point is the DPU port, not a switch port; ACLs and QoS are per server port on the DPU;
there is a storage object; `providerManagement` NAT exists because OSAC's hosted control planes live on the hub;
there is no site/fabric-domain object yet. A Netris-compatible facade (their paths and payloads) is possible later
as a thin layer over v1, if a customer needs it; it is deliberately not v1 itself.

## Not in v1
BGP peering objects, switch inventory, firmware, telemetry streams, multi-site. They belong to the OEM backend's
own management plane, which this API sits above.
