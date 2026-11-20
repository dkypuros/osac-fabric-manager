# osac-fabric-manager

An **OSAC Fabric Manager that is not Netris**: an implementation of OSAC's provider-owned network contract
(OSAC-1433) where the enforcement point is the server's DPU (BlueField-3 style), the fabric is BGP EVPN/VXLAN,
north-south is NAT at a border leaf, and storage is presented to servers the SNAP way. It comes with a one-box
staging backend (a mimicked fabric) and conformance test suites that check every component along the path.

**Working name.** `osac-fabric-manager` is a placeholder until the project is named for the OEM that ships it.
The name registered inside OSAC today is `sheldon_fabric` (see [docs/naming.md](docs/naming.md): the
NetworkClass that refers to it cannot be changed after creation, so the real name should be chosen early).

**Status (2026-11-07, evening).** Proven on the Sheldon lab for bare metal (Suite BM, 3 × 41/41) and clusters
(Suite CL, 3 × 36/36), both with the contract role calling the manager's own API v1. The lab still *runs* from the operational repo
`osac-sheldon`; this repo is a curated snapshot of the fabric-manager pieces (`source-snapshot.txt` names the
commit) plus the documentation that describes them as one thing. Switching the lab to run from here is a later
step.

## How this repo relates to the others
| Repo | Role |
|---|---|
| `osac-sheldon` | the operational log: what was built, what broke, how it was fixed, how to rebuild the whole lab |
| `osac-walkthrough` | the demo: click-by-click follow-along with screenshots |
| `osac` (fork) | OSAC's source with the lab's small changes and the list of upstream bugs |
| **this repo** | the fabric manager as a product-shaped thing: contract, backends, shims, conformance |

## Bringing it to real equipment
A vendor replaces the mimic below the API and fills in eleven read-only probes so the suites can look at their
equipment; nothing above the API changes. Start at [vendors/readme.md](vendors/readme.md); the Dell guide
([vendors/dell/readme.md](vendors/dell/readme.md)) walks a lab that already has BlueField-3, SONiC, iDRAC and
PowerStore from a proven tenant floor to OSAC placing clusters in it.

## The three layers
```
  OSAC (hub)  ──network contract (OSAC-1433, Ansible role, 20 tasks)──▶  contract/     (vendor-neutral)
                                                                            │ HTTP/JSON
                                                                            ▼
                                                                       backends/     (the vendor part)
                                                                         mimic/      one box: containerlab FRR fabric, DPU containers, SNAP via NVMe/TCP
                                                                         <oem>/      real switches + real DPUs (not written yet)
  shims/        temporary workarounds for OSAC bugs, each tied to an upstream issue, deleted when fixed
  conformance/  Suite BM and Suite CL: end-to-end through OSAC's API, a check at every hop, leak check at the end
```
Details: [docs/architecture.md](docs/architecture.md), [docs/contract.md](docs/contract.md),
[docs/backends.md](docs/backends.md), [docs/shims.md](docs/shims.md), [docs/conformance.md](docs/conformance.md),
[docs/from-scratch.md](docs/from-scratch.md), [docs/conformance-harness.md](docs/conformance-harness.md), and the northbound API v1 in the Netris vocabulary, DPU-centric, which the
contract role calls and both suites have passed on: [docs/api.md](docs/api.md); how it is built:
[docs/api-implementation.md](docs/api-implementation.md). Sequence diagrams with the boundaries drawn:
[docs/sequence-diagrams.pdf](docs/sequence-diagrams.pdf).

## Layout
| Path | What |
|---|---|
| `contract/collection/` | Ansible collection `kypuros.fabric` 1.0.3 (role `manager`): the twenty contract tasks, each one call to the backend's API v1; `tests/run-live.sh` runs the role inside the EE against a live backend |
| `backends/mimic/fabd/` | `fabd.py`: the mimic backend (fabric agent) serving API v1; `dryrun-v1.sh`/`dryrun_v1.py` (46 checks, no device access), `smoke-v1.sh` (live), `leakcheck.sh`; `smoke.sh` for the original paths until they go |
| `conformance/reports/` | the Suite CL reports of the three passes on API v1 (2026-11-07) |
| `backends/mimic/fabric/` | the mimicked fabric: `gen.py` → containerlab topology + FRR configs, `host-up.sh`, DPU image |
| `shims/janitor/` | `osac-cl-janitor.py`: automatic workarounds for OSAC bugs #6 and #9; `teardown-unstick.sh` one-shot |
| `conformance/` | `common/` runner, component checks and the **probe interface** (`probes.py`; `probes_mimic.py` reference, `probes_dell.py` skeleton; `OSAC_PROBES=<name>`); `bm/` Suite BM; `cl/` Suite CL; `run3.sh` exit criteria |
| `vendors/` | onboarding guides for real equipment: what a vendor inherits, the API objects as obligations on their gear, networks, probes, storage, how to glue in and test the bottom, the order of proof. [`vendors/dell/`](vendors/dell/readme.md) first |
| `config/hub/` | the registration ConfigMap OSAC discovers the fabric manager by |
| `config/host/` | backend config (`config.json`, `baseline.json`), systemd units |

Lower-case file names throughout. No secrets in this repo (tokens live outside it, see `config/host/config.json` keys).

## Licence

Apache License 2.0; see [LICENSE](LICENSE).
