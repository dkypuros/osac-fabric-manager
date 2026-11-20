#!/bin/bash
# Leak check for the mimicked side (fabric, DPUs, NAT, SNAP, DNS, bm VM power). Single implementation:
# tests/common/components.py leaks_mimic(), which ignores the persistent baseline in /etc/osac-fabd/baseline.json.
cd "$(dirname "$0")/../tests/common" && python3 -c '
import sys; sys.path.insert(0, ".")
import components as c
leaks = c.leaks_mimic(None)
print("LEAK CHECK: clean" if not leaks else "\n".join("LEAK " + l for l in leaks))
sys.exit(1 if leaks else 0)'
