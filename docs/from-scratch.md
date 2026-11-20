# Starting from scratch

If you had only this repo and a fresh machine, this is what you would need and where it is.

1. **A hub with OSAC on it.** OKD (or OpenShift) with OSAC installed with `provisioningEnabled` and CaaS/BMaaS on,
   Metal3/Ironic, MetalLB, AWX, the assisted service and HyperShift. This repo does not install those; the exact,
   tested procedure (open source only) is `osac-sheldon/docs/rebuild/09–12` with pinned versions in `versions.md`.
2. **The backend.** For the one-box mimic: `backends/mimic/fabric/` (containerlab + FRR topology, DPU image, host
   wiring) and `backends/mimic/fabd/fabd.py` with `config/host/config.json`. Procedure: `osac-sheldon/docs/rebuild/04–08`.
   For real hardware: implement API v1 (`docs/api.md`, `docs/api-implementation.md`) against your switches and DPUs.
3. **Registration.** Apply `config/hub/osac-fabric-registration.yaml` (name, role, `network_types: evpn`,
   capabilities) and reference the name from the NetworkClass at OSAC install time (immutable afterwards).
4. **The contract role in AWX.** Build the collection in `contract/collection/` (1.0.3, calls API v1; test it first with
   `contract/collection/tests/run-live.sh`) into the execution environment AWX
   runs OSAC's playbooks with, and set the backend URL/token in the networking instance group (ConfigMap
   `network-fulfillment-ig` / Secret `network-fulfillment-ig`). Procedure: `osac-sheldon/docs/rebuild/12`.
5. **Shims.** Run `shims/janitor/osac-cl-janitor.py` on the host (systemd unit in `config/host/`) if clusters will
   be ordered; see `docs/shims.md` for what each covers and when to drop it.
6. **Prove it.** `conformance/bm/run3.sh`, then `conformance/cl/run3.sh`. Both need the outside workstation VM and
   the fake Route 53 / outside resolver from the lab (`osac-sheldon/docs/rebuild/06–07`), which are part of the
   mimic's "customer on the internet" story.

Not the mimic? Start at `vendors/readme.md` (the Dell guide is the worked example): the backend is yours, the probes
(`conformance/common/probes.py`) are yours, everything else above is inherited.

What this repo deliberately does not duplicate: the lab's VM definitions, DNS services, Gitea, Tailscale access,
the HyperShift build and the OKD tenant-cluster template. Those are lab infrastructure and OSAC-side changes,
recorded in `osac-sheldon` and the `osac` fork.
