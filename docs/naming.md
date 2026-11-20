# Naming

Three names are in play and only one of them is cheap to change.

| Name | Where it lives | Changing it |
|---|---|---|
| Repo name `osac-fabric-manager` | Gitea | rename any time |
| Collection name `kypuros.fabric` / role `manager` | `contract/collection/galaxy.yml`, the execution environment, the registration's `ansible_role` | rebuild the EE and re-register; a few hours |
| **Fabric manager name `sheldon_fabric`** | the registration ConfigMap, and the **NetworkClass** of every OSAC install that uses it | the NetworkClass is immutable: it can only be replaced while no tenant networks exist (test-then-delete makes that a window between runs, not a wall), and OSAC's installation settings tie the address plan to it |

Recommendation: choose the OEM-facing name before the next OSAC install that will live for a while; keep
`sheldon_fabric` for the mimic lab if you like, and give the real backend its own registered name (OSAC allows
several fabric managers to be registered; the NetworkClass picks one).

The contract also forbids `osac.*` as the collection namespace for provider-owned managers, so the collection
keeps a vendor namespace.
