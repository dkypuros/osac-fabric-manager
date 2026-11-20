# Backends

The contract role never touches hardware. It calls a backend over HTTP/JSON with a bearer token; the backend owns
all state and all device access. **That API is v1, specified in [api.md](api.md)** (objects, verbs, crosswalk) and
built as described in [api-implementation.md](api-implementation.md). A vendor backend implements v1; the role and
the conformance suites do not change.

## Original paths (deprecated, kept until their last readers move)
The mimic's first API, still served beside v1 because the lab's DNS sync, leak check and component checks read
`/state` and `/ports`. Not called by OSAC since collection 1.0.3. Listed for reference; see api-implementation.md §5.5.

```
GET  /health  (no token)      GET /state      GET /ports      POST /reconcile
PUT/DELETE /vnets/<id> {cidr}                                            -> {l3_vni, vrf}
PUT/DELETE /subnets/<id> {vnet, cidr, gateway, physical_cidrs, lb_vip_cidrs} -> {l2_vni}
PUT/DELETE /sgs/<id> {vnet, ingress:[...], egress:[...]}
PUT/DELETE /attachments/<id> {subnet, host, mac, sgs}                    -> {ip, node, port};  GET /attachments/<id>/lease
PUT/DELETE /pools/<id> {cidr}    /extips/<id> {pool}                     -> {address}
PUT/DELETE /extip-attach/<id> {extip, address, dest, vnet, routing_domain}
PUT/DELETE /natgws/<id> {vnet, extip}      PUT/DELETE /ingress/<cluster_id> {subnet, lb_vip_cidrs}
PUT /cos/<host> {cos: kept|rewritten|dropped}
PUT/DELETE /volumes/<id>   PUT/DELETE /volume-attach/<id>     (SNAP, outside the OSAC contract)
```
Errors: `{"message", "retryable"}` with an HTTP status; the role maps them to `osac_error`.

## `mimic`: one box, everything fake but the packets
- containerlab + FRR 10.3: edge, border, 2 spines, 2 leafs, 3 DPU containers; eBGP underlay, BGP EVPN overlay.
- DPU containers: FRR VTEP, dnsmasq (DHCP per tenant subnet), nftables (security groups, CoS marks).
- Border: VRF-per-tenant NAT (DNAT/SNAT with conntrack marks and fwmark routing), hub port with leaked routes and
  policy, blackhole for the public pool, BGP advertisement of the pool.
- Edge: masquerade toward the internet port, drop private/lab destinations.
- SNAP: a storage-array VM with a Linux NVMe/TCP target on a storage VNI; the host connects and hot-plugs the volume
  into the server VM.
- Servers: libvirt VMs with a lifecycle NIC (cut/restored by the backend) and a data NIC into their DPU container.
Build and wiring: `backends/mimic/fabric/` (`gen.py`, `host-up.sh`); the full rebuild is in `osac-sheldon`
docs/rebuild 04–08.

## `<oem>`: what a real backend would implement (not written)
Same API v1; behind it, for example: Dell switches configured through their management interfaces for the VRFs,
VNIs and route targets; BlueField-3 cards programmed through DOCA/HBN for port binding, ACLs and DSCP; a storage
array's NVMe-oF for SNAP volumes; the border/NAT function on the switch OS or a gateway. Because the contract role
and the conformance suites don't change, a new backend is accepted when Suite BM and Suite CL pass against it.
