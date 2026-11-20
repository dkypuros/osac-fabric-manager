# Architecture and boundaries

```
 OUTSIDE            │ HUB (OSAC's management domain)        │ FABRIC MANAGER     │ TENANT SPACE
 customer,          │ OSAC fulfillment + operator, AWX,     │ contract role (AWX)│ DPU per server (VXLAN endpoint, isolation,
 DNS as the         │ hosted control planes, assisted,      │   ↓ HTTP           │ CoS, DHCP), leaf/spine/border EVPN fabric,
 customer sees it   │ Metal3/Ironic + BMC                   │ backend (fabd)     │ the server, SNAP storage over a storage VNI
                    │                                       │                    │
                    │ LIFECYCLE NETWORK: BMC + Ironic ↔ the server's install NIC (creation and deletion only)
```

Five boundaries matter; the sequence diagrams (`sequence-diagrams.pdf`) draw them.

1. **Tenant space.** Everything a tenant's workload can touch. The DPU is the enforcement point: the server's port
   lives in the tenant's VRF/VNI on the DPU, security groups are nftables rules on that port, DHCP is served by the
   DPU and never crosses VXLAN. The fabric (leafs, spines, border) relays EVPN; only DPUs and the border are VXLAN
   endpoints (symmetric IRB, route targets `0:<VNI>`).
2. **The handoff.** A server is created on the lifecycle network (BMC, virtual media, Ironic writing the disk). When
   OSAC moves its port into the tenant network, the backend binds the DPU port and **cuts the lifecycle NIC**; from
   then on the server exists only in the tenant space, like a host behind a real BlueField. The NIC is restored at
   deletion so Ironic can wipe the disk.
3. **The hub boundary.** Cluster workers must reach three hub services. The hub is attached to the border as a shared
   service; each tenant VRF gets a narrow, policed, SNAT'd route to those services only. The crossing point is the
   border leaf, never the DPU.
4. **The outside boundary.** Public addresses are DNAT/SNAT at the border; the outside resolver answers public
   addresses (split horizon); tenants egress through a NAT gateway and the edge's masquerade. A customer cannot
   reach a tenant's private addresses, the hub, or other tenants.
5. **The lifecycle boundary.** BMC and Ironic traffic stays on its own network and is never reachable from a tenant.

## Why this shape
- It matches NVIDIA's host-based networking: FRR on the DPU as the VXLAN endpoint, leafs as plain IP routers, the
  management cluster as another routed destination behind a shared-services boundary.
- It matches OSAC's contract exactly where the contract is specific (VNIs, route targets, attachments, external
  IPs, NAT gateways) and fills the gaps the contract leaves to the provider (hub reachability, public names, egress).
- It keeps the vendor-specific part small: everything below the backend's API v1 ([api.md](api.md)) is replaceable.

## Deviations of the mimic backend (what a real backend would do differently)
- Tenant→hub traffic is SNAT'd to the border's hub-port address instead of routed back (avoids touching the hub).
- SNAP volumes appear as virtio-blk (libvirt has no NVMe bus); the NVMe/TCP path behind them is real.
- The DPU is a container running FRR/dnsmasq/nftables, not a BlueField running its own stack.
- A fake server is a VM with a second, lifecycle NIC that a real BlueField host would not have (hence the cut).
