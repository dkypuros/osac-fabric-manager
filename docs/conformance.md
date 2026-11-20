# Conformance: the suites

Both suites drive OSAC through its public API as a tenant, check every component along the path in order (the first
failure names the stage and the component and saves evidence), always tear down, and end with a leak check across the
fabric, DPUs, DNS, storage and server power. They are the acceptance test for any backend.

| Suite | Flow | Steps | Exit criterion |
|---|---|---|---|
| **BM** `conformance/bm/suite_bm.py` | preflight → tenant network → order a server → provisioned → handoff into the tenant VNI → DHCP lease → SSH over the tenant network → SNAP volume (write/read checksum) → public address → reached from the outside workstation → isolation → teardown → leak check | 41 | `run3.sh`: 3 consecutive passes |
| **CL** `conformance/cl/suite_cl.py` | preflight → order a cluster (1 worker, auto external IPs) → control plane → LB → InfraEnv → worker provisioned → **handoff** (port in the VNI, lifecycle NIC cut) → Agent registers through the fabric with a tenant address only → bound → node Ready with a tenant address → cluster READY → automatic API public address → worker public address (shim) → split-horizon DNS → workstation resolves and reaches API and console → isolation → delete → ClusterOrder gone → hosts free → NICs restored → OSAC clean → leak check | 36 | `run3.sh`: 3 consecutive passes |

Results on the mimic backend (2026-11-05 … 07): Suite BM 41/41 on every run after each platform change; Suite CL
36/36 three consecutive unattended passes (reports in `osac-sheldon/tests/reports/`). **On API v1 (2026-11-07,
collection 1.0.3 / EE 1.0.7): Suite BM 3 × 41/41 (13:24–14:10) and Suite CL 3 × 36/36 (14:12–16:45), leak checks
clean; the Suite CL reports are in `conformance/reports/`.**

**Probes (2026-11-07).** Every look the suites take at the equipment below the fabric manager goes through
`conformance/common/probes.py`: eleven read-only methods with the meaning of each answer in its docstring.
`probes_mimic.py` is the reference, `probes_dell.py` the skeleton; `OSAC_PROBES=<name>` selects. The vendor-neutral
checks (OSAC's API, the hub, the outside workstation, DNS, the manager's own API) stay in `components.py`.

The step ids map onto `osac-sheldon/test-suite.md` §2 and §3. OSAC's own §4.11 contract conformance tests are an open
item (not yet run against this manager).
