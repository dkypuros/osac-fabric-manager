# The API conformance harness

`backends/mimic/fabd/dryrun_v1.py` answers one question about any backend: **does it behave the way this fabric
manager's API requires?** It plays the OSAC cluster-order sequence against the backend through `/v1` and checks
every rule in [api.md](api.md): create, identical re-put is a no-op, 202 until the lease then Ready, labels and list
filters, 409 with the dependents list, cascade delete, 204 on a missing object, events, adopt, nothing left behind.
About forty-five checks, a few seconds against a dry-run backend. It is the first gate in the order of proof
(vendors guide, chapter 7.1 and 8) and the thing to run after every change to a backend.

It has two modes.

## Mode 1: the mimic's own dry run (no arguments)

```bash
backends/mimic/fabd/dryrun-v1.sh
```
Starts two idle copies of the mimic backend on 127.0.0.1:7481 and :7482 in dry-run mode (device commands are
journaled, not executed), with a throwaway token and state file under a temp directory, and:
1. plays the sequence through v1 on :7481 (its DHCP lease appears on the third look, so the pending path is
   exercised) — 46 checks;
2. plays the same sequence through the mimic's original paths on :7482, then checks `POST /v1/adopt` turns those
   objects into v1 objects;
3. compares the two write journals: the device commands must be the same multiset.
The live agent on :7480 is never touched. This mode is for changing `fabd.py`.

## Mode 2: conformance of any backend (`--target`)

```bash
backends/mimic/fabd/dryrun_v1.py --target http://<backend>:<port> --token <bearer> \
    --host <a server the backend knows> [--host2 <another>] [--mac 52:54:00:..] [--live] [--lease-budget 600]
```
No mimic is started. The harness talks only to the URL given. It creates objects whose names all start with
`v1conf-`, drives them through the whole sequence, deletes them, and finally lists every kind to confirm none of its
objects remain. Exit code 0 means every check passed; the output names each check.

| Option | Meaning |
|---|---|
| `--target`, `--token` | the backend's base URL and bearer token |
| `--host` | a server the backend has a port for; the attachment under test binds it (default `osac-inventory/bm-1`) |
| `--host2` | a second server for the cascade test (default `bm-0`) |
| `--mac` | the MAC the attachment is created with; against a live backend, the MAC of a real NIC that will ask for DHCP |
| `--live` | required when the backend's `/v1/health` does not report `dryRun: true` (see safety) |
| `--lease-budget` | seconds to wait for the attachment to become `Ready/Leased` against a live backend (default 600) |

**Safety.** Without `--live` the harness refuses a backend that is not in dry-run mode, so a real fabric is never
touched by accident. With `--live` it will program real equipment: a VPC, a subnet, an ACL, a public pool
`203.0.113.0/24`, three addresses, one attachment on `--host`, two NATs (one `providerManagement` to `10.251.0.200`),
a NAT gateway, then a second VPC with a subnet and an attachment on `--host2` for the cascade test. Everything is
removed again; run it in a maintenance window with the two servers free, or point it at a staging instance.

**What is skipped against a foreign backend.** Four checks read the mimic's command journal (no device commands on an
identical re-put, the lifecycle NIC cut and restore commands, the intermediate "still Pending" look). They print as
`skip ... (journal: mimic only)`. Their observable equivalents still run: generation unchanged on re-put, the
attachment's `status.lifecyclePort`, and polling until `Ready/Leased`.

**The lease.** Against a dry-run backend the attachment becomes Ready on the backend's own schedule (the mimic's
`FABD_DRY_LEASE_READS` knob). Against a live backend the harness polls `GET /v1/attachments/<name>` every five
seconds up to `--lease-budget`; the server behind `--host` must actually request DHCP on `--mac` for that to happen,
so boot it (or a VM/netns behind its DPU) into a DHCP client first, as `smoke-v1.sh` does with network namespaces.

## Giving your backend a dry-run mode (recommended)

A dry-run mode makes mode 2 safe and fast and is what the mimic does: the few helpers that touch devices record the
command instead of running it, the reads the handlers need are answered from canned data, and `/v1/health` reports
`dryRun: true`. With it, every commit to your backend can run the harness in seconds with no hardware in the loop.
See `api-implementation.md` section 4 for the mimic's implementation (`journal`, `dry_sh`, `FABD_DRY_RUN`).

## What a failure looks like

```
  FAIL delete VPC with dependents -> 409 listing them: (204, None)
```
The label is the rule, the detail is what came back. The rules are stated in `api.md` ("Rules the code enforces" in
`api-implementation.md` section 3 has the precise semantics). Fix the backend, run again; the harness is idempotent
and cleans up after itself even when checks fail (deletes run regardless), except when the backend refuses the
deletes, in which case the final "none of the harness's objects remain" check names what is left.

## Adding a check

`check(label, condition, detail)` fails the run; `jcheck(...)` is the same but skipped for foreign backends. Add to
`scenario_v1` for a rule, and to `scenario_legacy` as well if the journal comparison should cover it. Every new API
kind needs a PUT/DELETE pair in both scenarios.
