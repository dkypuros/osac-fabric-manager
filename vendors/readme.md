# Vendors: bringing this fabric manager to real equipment

Everything above the fabric manager's API is built and proven: OSAC installed and driving the contract role, the API
with its semantics settled, the hub shared-service design, split-horizon public DNS, the workarounds for OSAC's
known bugs, and two conformance suites that define "done" (Suite BM, 41 steps; Suite CL, 36 steps; three unattended
passes each). The one-box mimic under it is the reference implementation, not the product.

A vendor replaces the mimic. The work has two seams and one finish line:

| Seam | What it is | Where |
|---|---|---|
| **Backend** | serve API v1 against your switches, DPUs, border and array | `docs/api.md`, `docs/api-implementation.md`, the mimic in `backends/mimic/` to read |
| **Probes** | the eleven read-only looks the suites take at the equipment | `conformance/common/probes.py` (interface), `probes_mimic.py` (reference), `probes_<vendor>.py` (yours) |
| **Finish line** | conformance harness → live smoke → Suite BM ×3 → Suite CL ×3 | `docs/conformance-harness.md` (`dryrun_v1.py --target <your backend>`), `conformance/*/run3.sh` |

Vendor folders hold the onboarding guide for one vendor's equipment: what they inherit, how each API object maps to
their components, the network roles and reachability, the probe hints, storage, and the order of proof.

- [dell/](dell/readme.md) — BlueField-3, SONiC, iDRAC, PowerStore with SNAP.
