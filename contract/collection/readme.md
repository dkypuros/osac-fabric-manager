# kypuros.fabric

`kypuros.fabric.manager` is an OSAC **Fabric Manager** (provider-owned, network contract OSAC-1433).
It implements the contract's Fabric tasks by calling **fabd**, the fabric agent on Sheldon, which drives a
containerlab FRR fabric (BGP EVPN / VXLAN, BlueField-3-style DPU VTEPs, border NAT).

Settings (instance group `<prefix>-networking-operations-ig`):
- ConfigMap `network-fulfillment-ig`: `SHELDON_FABRIC_URL` (e.g. `http://10.251.0.1:7480`)
- Secret `network-fulfillment-ig`: `SHELDON_FABRIC_TOKEN`

Registration: `name: sheldon_fabric`, `ansible_role: kypuros.fabric.manager`, `network_types: evpn`,
`capabilities: security_group_icmp`.

Documented no-ops: overlay `apply_network_attachment` / `delete_network_attachment` (the Kubernetes
Manager enforces VM policy). `create/delete_fabric_domain` are not implemented (no `eastWestEthernet`).
