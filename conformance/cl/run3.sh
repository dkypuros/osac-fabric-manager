#!/bin/bash
# Sprint 2 exit: Suite CL three times in a row, each with a clean leak check. Stops at the first failure.
cd "$(dirname "$0")"
for n in 1 2 3; do
  echo "===== consecutive run $n/3  $(date +%H:%M:%S)"
  KUBECONFIG=~/.kube/osac-hub.yaml timeout 7200 python3 suite_cl.py > /tmp/claude-1000/suite-cl-pass$n.log 2>&1
  rc=$?
  grep -E '^(FAIL|SKIP|LEAK|report)' /tmp/claude-1000/suite-cl-pass$n.log; echo "steps passed: $(grep -c '^PASS' /tmp/claude-1000/suite-cl-pass$n.log)  rc=$rc"
  [ $rc -ne 0 ] && { echo "STOP: run $n failed"; exit 1; }
done
echo "ALL 3 CONSECUTIVE RUNS PASSED"
