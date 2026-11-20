# Shims: temporary workarounds for OSAC bugs

A shim is code this fabric manager has to carry because OSAC is not finished, not because the design needs it.
Each one names the upstream bug it covers (numbers as in `osac-sheldon/docs/plan-documentation/06-risks-and-decisions.md`
and `osac` fork `sheldon-changes.md`) and is meant to be deleted the day the fix lands.

| Shim | Covers | What it does | Remove when |
|---|---|---|---|
| `shims/janitor/osac-cl-janitor.py` (part 1) | OSAC bug #6: the automatic *ingress* ExternalIPAttachment never attaches (`ClusterOrder.spec.networkAttachment.subnetRef` holds a name, `findSubnet` looks up an ID) | when a cluster is READY and its ingress attachment is stuck, allocates a public address and attaches it to the worker's BareMetalInstance through the admin API; removes it before deletion | upstream resolves the subnet by name, or stores the ID |
| `shims/janitor/osac-cl-janitor.py` (part 2), `shims/teardown-unstick.sh` | OSAC bug #9: cluster deletion loops on automatic ExternalIPs (operator deletes the CRs, fulfillment-controller re-creates them) | pauses `fulfillment-controller` until the deprovision job exists, resumes it, pauses again when the control plane and worker are gone until the ClusterOrder CR disappears, resumes | the operator and the controller agree on who deletes automatic ExternalIPs |
| provider playbooks in the `osac` fork (`move-network-attachment`, dual-input `query-dhcp-lease`) | bugs #1, #2 | job templates BMFO calls but upstream doesn't ship; input shape mismatch | upstream ships them |

API v1 (2026-11-07) does not change this table: v1 absorbs repeated and re-issued *fabric* calls, while the janitor's
two jobs are OSAC-internal (bugs #6 and #9). Only an upstream fix removes a shim.

Not shims (part of the design, keep): the lifecycle-NIC cut, the hub shared-service route and policy, split-horizon
DNS, the edge masquerade/private-drop, the template settings for OKD (`olmCatalogPlacement: guest`).

Known gaps that no shim covers: no tenant kubeconfig download (bug #10); a failed automatic attachment is terminal
and blocks its endpoint slot (bugs #7, #8) — the only remedy is a fresh order.
