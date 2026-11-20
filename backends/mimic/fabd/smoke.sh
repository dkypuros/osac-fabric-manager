#!/bin/bash
# fabd smoke test: tenant net + 2 fake servers (netns on br-dpu0/br-dpu1) + external IP from outside. Cleans up.
T=$(sudo cat /etc/osac-fabd/token); A="-s -H Authorization:Bearer\ $T -H Content-Type:application/json"
f() { curl -s -X "$1" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' "http://127.0.0.1:7480$2" ${3:+-d "$3"}; echo; }
mk() { sudo ip netns add $1; sudo ip link add $1-h type veth peer name $1-v; sudo ip link set $1-v netns $1
       sudo ip netns exec $1 ip link set $1-v address $3 name eth0; sudo ip netns exec $1 ip link set eth0 up; sudo ip netns exec $1 ip link set lo up
       sudo ip link set $1-h master $2 up; }
rm_() { sudo ip netns pids $1 2>/dev/null | xargs -r sudo kill; sudo ip netns del $1 2>/dev/null; sudo ip link del $1-h 2>/dev/null; }
cleanup() { echo "--- cleanup"; f DELETE /extip-attach/ea1; f DELETE /extips/e1; f DELETE /pools/p1
  f DELETE /attachments/a0; f DELETE /attachments/a1; f DELETE /sgs/sg1; f DELETE /subnets/s1; f DELETE /vnets/v1; rm_ t0; rm_ t1; }
[ "$1" = cleanup ] && { cleanup; exit; }
echo "--- create";  f PUT /vnets/v1 '{"cidr":"172.16.10.0/24"}'
f PUT /subnets/s1 '{"vnet":"v1","cidr":"172.16.10.0/24","gateway":"172.16.10.1","physical_cidrs":["172.16.10.128/26"],"lb_vip_cidrs":["172.16.10.192/26"]}'
f PUT /sgs/sg1 '{"vnet":"v1","ingress":[{"protocol":"all","sourceCidr":"0.0.0.0/0"}],"egress":[{"protocol":"all","destinationCidr":"0.0.0.0/0"}]}'
mk t0 br-dpu0 52:54:00:5e:2d:00; mk t1 br-dpu1 52:54:00:5e:2d:01
f PUT /attachments/a0 '{"subnet":"s1","host":"bm-0","mac":"52:54:00:5e:2d:00","sgs":["sg1"]}'
f PUT /attachments/a1 '{"subnet":"s1","host":"bm-1","mac":"52:54:00:5e:2d:01","sgs":["sg1"]}'
for n in t0 t1; do sudo ip netns exec $n timeout 20 busybox udhcpc -i eth0 -n -q -t 5 -s /bin/true >/dev/null 2>&1 || true; done
echo "--- leases"; f GET /attachments/a0/lease; f GET /attachments/a1/lease
# udhcpc -s /bin/true doesn't configure; apply the leased addresses like a DHCP client would
for p in "t0 a0" "t1 a1"; do set -- $p; ip=$(f GET /attachments/$2/lease | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ip_address",""))')
  [ -n "$ip" ] && sudo ip netns exec $1 ip addr add $ip/24 dev eth0 && sudo ip netns exec $1 ip route add default via 172.16.10.1; done
echo "--- east-west t0 -> t1 (dpu-0/leaf-1 -> spines -> leaf-2/dpu-1)"; sudo ip netns exec t0 ping -c2 -W2 172.16.10.130 | tail -1
echo "--- gateway"; sudo ip netns exec t0 ping -c1 -W2 172.16.10.1 | tail -1
echo "--- north-south: external IP for t0"
f PUT /pools/p1 '{"cidr":"203.0.113.0/24"}'; E=$(f PUT /extips/e1 '{"pool":"p1"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["address"])'); echo "ext=$E"
f PUT /extip-attach/ea1 "{\"extip\":\"e1\",\"address\":\"$E\",\"dest\":\"172.16.10.129\",\"vnet\":\"v1\"}"
sleep 3; echo "outside(host on virbr-out) -> $E:"; ping -c2 -W2 -I 198.51.100.53 $E | tail -1
echo "t0 -> outside:"; sudo ip netns exec t0 ping -c2 -W2 198.51.100.53 | tail -1
cleanup >/dev/null
