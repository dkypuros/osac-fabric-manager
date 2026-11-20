#!/bin/bash
# fabd smoke test through the northbound API v1 (same scenario as smoke.sh): tenant VPC + subnet + ACL, two fake servers
# (netns on br-dpu0/br-dpu1) attached by DPU port, DHCP lease (202 Pending -> Ready/Leased), east-west, gateway, a public
# address NAT'd from outside, egress through the NAT gateway. Cleans up; ends with a cascade delete and a state check.
T=$(sudo cat /etc/osac-fabd/token)
f() { curl -s -o /tmp/smoke-v1.out -w '%{http_code}' -X "$1" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' "http://127.0.0.1:7480/v1$2" ${3:+-d "$3"}; echo -n " $1 $2 "; python3 -c 'import json,sys
try: d=json.load(open("/tmp/smoke-v1.out"))
except Exception: print(""); sys.exit()
st=d.get("status",{}) if isinstance(d,dict) else {}
c=st.get("conditions",[{}])[0] if st else {}
print({k:v for k,v in st.items() if k!="conditions"} if st else d.get("message",""), c.get("reason",""))'; }
mk() { sudo ip netns add $1; sudo ip link add $1-h type veth peer name $1-v; sudo ip link set $1-v netns $1
       sudo ip netns exec $1 ip link set $1-v address $3 name eth0; sudo ip netns exec $1 ip link set eth0 up; sudo ip netns exec $1 ip link set lo up
       sudo ip link set $1-h master $2 up; }
rm_() { sudo ip netns pids $1 2>/dev/null | xargs -r sudo kill; sudo ip netns del $1 2>/dev/null; sudo ip link del $1-h 2>/dev/null; }
cleanup() { echo "--- cleanup (cascade delete of the VPC removes subnet, ACL, attachments, NAT, NAT gateway)"
  f DELETE "/vpcs/v1smoke?cascade=true"; f DELETE /externalips/e1; f DELETE /externalips/gw; f DELETE /pools/p1; rm_ t0; rm_ t1; }
[ "$1" = cleanup ] && { cleanup; exit; }
echo "--- create"
f PUT /vpcs/v1smoke '{"spec":{"cidr":"172.16.10.0/24"},"metadata":{"labels":{"osac/tenant":"smoke"}}}'
f PUT /subnets/s1 '{"spec":{"vpc":"v1smoke","cidr":"172.16.10.0/24","gateway":"172.16.10.1","physicalCidrs":["172.16.10.128/26"],"lbVipCidrs":["172.16.10.192/26"]}}'
f PUT /acls/sg1 '{"spec":{"vpc":"v1smoke","ingress":[{"protocol":"all","sourceCidr":"0.0.0.0/0"}],"egress":[{"protocol":"all","destinationCidr":"0.0.0.0/0"}]}}'
mk t0 br-dpu0 52:54:00:5e:2d:00; mk t1 br-dpu1 52:54:00:5e:2d:01
f PUT /attachments/a0 '{"spec":{"subnet":"s1","host":"bm-0","mac":"52:54:00:5e:2d:00","acls":["sg1"]}}'
f PUT /attachments/a1 '{"spec":{"subnet":"s1","host":"bm-1","mac":"52:54:00:5e:2d:01","acls":["sg1"]}}'
echo "--- identical re-put (must be 200 and change nothing)"; f PUT /attachments/a0 '{"spec":{"subnet":"s1","host":"bm-0","mac":"52:54:00:5e:2d:00","acls":["sg1"]}}'
for n in t0 t1; do sudo ip netns exec $n timeout 20 busybox udhcpc -i eth0 -n -q -t 5 -s /bin/true >/dev/null 2>&1 || true; done
echo "--- after DHCP: Ready/Leased expected"; f GET /attachments/a0; f GET /attachments/a1
for p in "t0 a0" "t1 a1"; do set -- $p; ip=$(curl -s -H "Authorization: Bearer $T" http://127.0.0.1:7480/v1/attachments/$2 | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["address"])')
  sudo ip netns exec $1 ip addr add $ip/24 dev eth0 && sudo ip netns exec $1 ip route add default via 172.16.10.1; done
echo "--- east-west t0 -> t1"; sudo ip netns exec t0 ping -c2 -W2 172.16.10.130 | tail -1
echo "--- gateway"; sudo ip netns exec t0 ping -c1 -W2 172.16.10.1 | tail -1
echo "--- north-south"
f PUT /pools/p1 '{"spec":{"cidr":"203.0.113.0/24"}}'; f PUT /externalips/e1 '{"spec":{"pool":"p1"}}'; f PUT /externalips/gw '{"spec":{"pool":"p1"}}'
E=$(curl -s -H "Authorization: Bearer $T" http://127.0.0.1:7480/v1/externalips/e1 | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["address"])'); echo "ext=$E"
f PUT /nats/n1 "{\"spec\":{\"externalIp\":\"e1\",\"destination\":{\"kind\":\"attachment\",\"address\":\"172.16.10.129\",\"vpc\":\"v1smoke\"}}}"
f PUT /natgateways/g1 '{"spec":{"vpc":"v1smoke","externalIp":"gw"}}'
sleep 3; echo "outside(host on virbr-out) -> $E:"; ping -c2 -W2 -I 198.51.100.53 $E | tail -1
echo "t0 -> outside:"; sudo ip netns exec t0 ping -c2 -W2 198.51.100.53 | tail -1
echo "--- dependents"; f DELETE /vpcs/v1smoke
echo "--- events"; curl -s -H "Authorization: Bearer $T" http://127.0.0.1:7480/v1/attachments/a0/events | python3 -c 'import json,sys;print([e["what"] for e in json.load(sys.stdin)])'
cleanup
echo "--- gone?"; f GET /vpcs/v1smoke; f DELETE /vpcs/v1smoke
