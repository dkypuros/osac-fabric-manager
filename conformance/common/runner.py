"""Stage runner for the OSAC-on-Sheldon architecture suites (see test-suite.md).

Each suite is an ordered list of steps. A step names its component, runs a check with a time budget
(polling until it passes or the budget runs out), and on failure records the failure kind, a one-line
reason and evidence from that component's snapshot source, then stops the run. Teardown steps are
registered separately and always run (also after a failure), followed by the leak check.

Failure kinds follow upstream osac/tests/e2e/core/failures.py: api-refused, stuck-state, traffic-blocked,
test-ssh-failed, assertion.
"""
import json
import os
import subprocess
import time
import traceback
from datetime import datetime

KINDS = ("api-refused", "stuck-state", "traffic-blocked", "test-ssh-failed", "assertion")


class StepFailed(Exception):
    def __init__(self, kind, reason):
        assert kind in KINDS, kind
        super().__init__(reason)
        self.kind, self.reason = kind, reason


def sh(cmd, timeout=60, check=False):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise StepFailed("assertion", f"command failed: {cmd[:80]}: {p.stderr.strip()[-200:]}")
    return p


class Suite:
    def __init__(self, name, snapshots):
        self.name = name
        self.snapshots = snapshots          # component -> fn(dir) that writes evidence
        self.ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = os.path.join(os.path.dirname(__file__), "..", "reports", f"{self.ts}-{name}")
        os.makedirs(self.dir, exist_ok=True)
        self.lines, self.failed, self.teardown = [], None, []
        self.ctx = {}                       # values passed between steps (ids, addresses, ...)

    def _snapshot(self, components):
        for c in components:
            fn = self.snapshots.get(c)
            if not fn:
                continue
            d = os.path.join(self.dir, "snapshot", c)
            os.makedirs(d, exist_ok=True)
            try:
                fn(d, self.ctx)
            except Exception as e:  # evidence collection must never mask the failure
                open(os.path.join(d, "snapshot-error.txt"), "w").write(repr(e))

    def step(self, sid, component, title, fn, budget=60, interval=5, also=()):
        """Run fn(ctx) until it returns truthy (pass) or budget seconds pass. fn may raise StepFailed."""
        if self.failed:
            self.lines.append(f"SKIP {sid:6s} {component:16s} {title}")
            return None
        t0, last = time.time(), None
        while True:
            try:
                res = fn(self.ctx)
                if res:
                    dt = time.time() - t0
                    self.lines.append(f"PASS {sid:6s} {component:16s} {title}  ({dt:.0f}s)")
                    print(self.lines[-1], flush=True)
                    return res
                last = StepFailed("stuck-state", f"not reached after {budget}s")
            except StepFailed as e:
                last = e
                if e.kind != "stuck-state":
                    break
            except Exception as e:  # noqa: BLE001
                last = StepFailed("assertion", f"{type(e).__name__}: {e}")
                traceback.print_exc()
                break
            if time.time() - t0 > budget:
                break
            time.sleep(interval)
        self.failed = {"step": sid, "component": component, "check": title, "kind": last.kind, "reason": last.reason}
        self.lines.append(f"FAIL {sid:6s} {component:16s} \"{title}\"  {last.kind}  {last.reason}")
        print(self.lines[-1], flush=True)
        self._snapshot((component,) + tuple(also))
        return None

    def on_teardown(self, sid, component, title, fn, budget=300):
        self.teardown.insert(0, (sid, component, title, fn, budget))   # LIFO, like resource dependencies

    def finish(self, leak_check):
        failed_before = self.failed
        self.failed = None                  # teardown always runs
        for sid, comp, title, fn, budget in self.teardown:
            self.step(sid, comp, title, fn, budget=budget)
            self.failed = None              # a failed teardown step never stops the remaining teardown
        leaks = leak_check(self.ctx)
        open(os.path.join(self.dir, "leaks.md"), "w").write("\n".join(leaks) or "clean\n")
        self.lines.append("LEAK CHECK: " + ("clean" if not leaks else f"{len(leaks)} leak(s): " + "; ".join(leaks)))
        print(self.lines[-1], flush=True)
        ok = not failed_before and not leaks and not any(l.startswith("FAIL") for l in self.lines)
        summary = [f"# {self.name} {self.ts}: {'PASS' if ok else 'FAIL'}", ""] + self.lines
        open(os.path.join(self.dir, "summary.md"), "w").write("\n".join(summary) + "\n")
        if failed_before:
            json.dump({**failed_before, "test": self.name, "snapshot": "snapshot/"}, open(os.path.join(self.dir, "failure.json"), "w"), indent=1)
        print(f"report: {os.path.abspath(self.dir)}")
        return ok
