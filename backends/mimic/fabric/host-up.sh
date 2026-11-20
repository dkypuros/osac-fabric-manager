#!/bin/bash
# Host-side wiring for the mimicked fabric. Idempotent; rerun after every containerlab deploy.
set -e
for m in vrf vxlan nf_conntrack_bridge br_netfilter nvme_tcp nvmet nvmet_tcp; do modprobe $m; done
# bridged traffic must not be pushed through the host's iptables (Cilium/libvirt rules)
sysctl -qw net.bridge.bridge-nf-call-iptables=0 net.bridge.bridge-nf-call-ip6tables=0 net.bridge.bridge-nf-call-arptables=0
br() { ip link show $1 >/dev/null 2>&1 || ip link add $1 type bridge; ip link set $1 up; }
for i in 0 1; do br br-dpu$i; ip link set dpu$i-host master br-dpu$i; ip link set dpu$i-host up; done
br br-storage; ip link set dpust-arr master br-storage; ip link set dpust-arr up
ip addr replace 192.168.250.10/24 dev dpu0-snap; ip link set dpu0-snap up
ip addr replace 192.168.250.11/24 dev dpu1-snap; ip link set dpu1-snap up
virsh net-info outside >/dev/null 2>&1 || { virsh net-define /home/student/src/osac-sheldon/fabric/outside-net.xml; virsh net-autostart outside; }
virsh net-info outside | grep -q 'Active:.*yes' || virsh net-start outside
ip link set edge-out master virbr-out; ip link set edge-out up
ip addr replace 198.51.100.53/24 dev virbr-out
# plan 08: border:eth4 <-> host:border-oc on virbr-oc (hub network). containerlab creates the veth on a fresh deploy;
# on a fabric deployed before plan 08 it is created here.
if ! ip link show border-oc >/dev/null 2>&1; then
  BPID=$(podman inspect -f '{{.State.Pid}}' border)
  ip link add border-oc type veth peer name eth4 netns $BPID
  nsenter -t $BPID -n ip link set eth4 up
fi
ip link set border-oc master virbr-oc; ip link set border-oc up
podman exec border ip addr replace 10.251.0.2/24 dev eth4
echo host-up done
