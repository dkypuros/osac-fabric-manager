#!/bin/bash
# Runs the contract role (this source tree, not the EE's installed copy) inside the lab execution environment against the
# live fabric manager, with a fake server in a network namespace on br-dpu0 so the DHCP lease is real. Cleans up.
# Usage: run-live.sh [ee-image]   (default 10.251.0.1:5001/osac/osac-aap-sheldon:1.0.6)
set -u
EE=${1:-10.251.0.1:5001/osac/osac-aap-sheldon:1.0.6}
C=$(cd "$(dirname "$0")/.." && pwd)
TOK=$(sudo cat /etc/osac-fabd/token)
LOG=${ROLE_LIVE_LOG:-/tmp/role-live-$$}; mkdir -p "$LOG"; echo "logs in $LOG"
play() { sudo podman run --rm --network host -v "$C":/usr/share/ansible/collections/ansible_collections/kypuros/fabric:ro,Z \
    -e SHELDON_FABRIC_URL=http://127.0.0.1:7480 -e SHELDON_FABRIC_TOKEN="$TOK" \
    "$EE" ansible-playbook -i localhost, -c local /usr/share/ansible/collections/ansible_collections/kypuros/fabric/tests/role-live.yml -e phase=$1 \
    > "$LOG/$1.log" 2>&1; rc=$?; grep -E '"msg":|msg:|fatal:|failed=' "$LOG/$1.log" | grep -v no_log | cut -c1-200 | sed 's/^/  /'; return $rc; }
mk() { sudo ip netns add t0; sudo ip link add t0-h type veth peer name t0-v; sudo ip link set t0-v netns t0
       sudo ip netns exec t0 ip link set t0-v address 52:54:00:5e:2e:00 name eth0; sudo ip netns exec t0 ip link set eth0 up; sudo ip link set t0-h master br-dpu0 up; }
rm_() { sudo ip netns pids t0 2>/dev/null | xargs -r sudo kill; sudo ip netns del t0 2>/dev/null; sudo ip link del t0-h 2>/dev/null; }
rc=0
echo "== create"; mk; play create || rc=1
echo "== lease before DHCP (must fail retryably)"; play lease && { echo "  UNEXPECTED: lease reported before the server asked"; rc=1; }
sudo ip netns exec t0 timeout 20 busybox udhcpc -i eth0 -n -q -t 5 -s /bin/true >/dev/null 2>&1 || true
echo "== lease after DHCP"; play lease || rc=1
echo "== public"; play public || rc=1
echo "== delete"; play delete || rc=1
rm_
echo "== leak check"; "$C"/../../../fabd/leakcheck.sh | tail -1 || rc=1
echo "RESULT: $([ $rc = 0 ] && echo PASS || echo FAIL)"; exit $rc
