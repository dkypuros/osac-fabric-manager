# The contract, and what this fabric manager does with each operation

Source: OSAC enhancement proposal OSAC-1433 (network manager integration contract), §4.1–4.11. OSAC discovers a
fabric manager through a ConfigMap labelled `osac.openshift.io/network-fabric-manager: "true"` with `name`,
`ansible_role`, `network_types` and `capabilities` (ours: `config/hub/osac-fabric-registration.yaml`). Each
operation is one task file in the role, run as one AWX job. The task reads `osac_job_vars`, makes one call to the
backend's API v1 ([api.md](api.md) has the task-by-task crosswalk), and returns results with `set_stats`
(`osac_result`, read from the object's `status`) or an error (`osac_error` with `retryable`; any non-2xx). Since
collection 1.0.3 / EE 1.0.7 (2026-11-07). `query_dhcp_lease` reports a retryable error until the attachment is
`Ready/Leased`, so OSAC's own retry loop is what waits for the server to ask for an address.

| Operation | Status | What ours does (mimic backend) |
|---|---|---|
| `create_virtual_network` / `delete_virtual_network` | ✅ | IP-VRF + L3 VNI on the border and every DPU; route targets `0:<VNI>`; returns `l3_vni`. On the border: default route toward the edge, plus the leaked hub routes (see below). |
| `create_subnet` / `delete_subnet` | ✅ | MAC-VRF + L2 VNI, anycast gateway, DHCP scope on the DPUs; returns `network_segment.l2_vni`. |
| `create_security_group` / `delete_security_group` | ✅ | nftables rules per DPU port (ICMP capability declared). |
| `apply_network_attachment` / `delete_network_attachment` (physical) | ✅ | bind the server's DPU port to the subnet's VNI, apply its security groups, lease an address; **cut the server's lifecycle NIC** (restored on delete). Overlay (VM) attachments are documented no-ops: the Kubernetes manager enforces VM policy. |
| `query_dhcp_lease` | ✅ | the DPU's DHCP lease for the port's MAC. |
| `create_external_ip_pool` / `delete_external_ip_pool` | ✅ | pool advertised by the border (blackhole + BGP network). |
| `create_external_ip` / `delete_external_ip` | ✅ | address allocation from the pool. |
| `attach_external_ip` / `detach_external_ip` | ✅ | DNAT/SNAT at the border. Destinations: `tenant_virtual_network` (a server) and `provider_management` (a hosted cluster's API on the hub; via the border's hub port). |
| `create_nat_gateway` / `delete_nat_gateway` | ✅ | SNAT of the tenant's range to its gateway address at the border. |
| `reserve_cluster_ingress_ip` / `release_cluster_ingress_ip` | implemented, **never called by OSAC** | OSAC's CaaS contract points `*.apps` at the first worker and uses `attach_external_ip` instead. Kept for conformance. |
| `create_fabric_domain` / `delete_fabric_domain` | ✗ | not implemented (no east-west fabric domains yet). |

## What the contract leaves to the fabric manager, and how we filled it
- **Hub reachability from tenants.** A cluster worker must reach the assisted service, the ignition server and its
  API server on the hub. The contract assumes it; the mimic attaches the hub to the border as a shared service and
  leaks only those prefixes/ports into each tenant VRF, policed, with SNAT so the hub needs no routes back.
- **Public names.** OSAC publishes inside addresses and never maps an ExternalIP to a name. The mimic's DNS sync
  gives the outside view the public address (split horizon).
- **Internet egress.** The edge masquerades toward the internet and drops private/lab destinations.
- **Storage.** The contract has no storage operation; the bare-metal design defines a `storage` port role. The
  mimic attaches SNAP volumes (NVMe/TCP over a storage VNI, presented as a local disk) outside the contract.

## Conformance
OSAC's §4.11 conformance tests have not been run against this manager yet (open item). Our own conformance is
the end-to-end suites in `conformance/`, which test the contract *through OSAC* rather than in isolation.
