#!/bin/bash
# Plan 08 cluster checks (Suite CL shape), run by hand around one OSAC cluster order.
#   check08.sh order <name>     order as tenant1 with external IPs on
#   check08.sh fabric           worker link/VNI/Agent/ExternalIP/DNAT/DNS checks (run once the order is Progressing+)
#   check08.sh outside          from the workstation: resolve + reach API and console by name; isolation
#   check08.sh delete <name>    delete via OSAC, then teardown checks + leak check
export KUBECONFIG=~/.kube/osac-hub.yaml
R=~/src/osac-sheldon; O="env XDG_CONFIG_HOME=$R/secrets/cli-tenant1 $R/bin/osac-lab"
WS="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=5 student@198.51.100.10"
say() { printf '%-8s %s\n' "$1" "$2"; }
case "$1" in
order)
  $O create cluster --name $2 --template okd-sheldon-small --version okd-4-22 --pull-secret cluster-pull-secret \
    --ssh-public-key-file $HOME/.ssh/id_ed25519.pub --node-set name=workers,size=1,baremetal-instance-type=bm-small --external-ip-attachment; date ;;
fabric)
  CO=$(kubectl -n osac get clusterorders -o jsonpath='{.items[0].metadata.name}'); echo "order $CO"
  BMI=$(kubectl -n osac get baremetalinstances -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); HOST=$(kubectl -n osac get baremetalinstance $BMI -o jsonpath='{.status.hostRef.name}{.status.bareMetalHostRef.name}' 2>/dev/null)
  [ -z "$HOST" ] && HOST=$(kubectl -n osac-inventory get bmh -o jsonpath='{range .items[?(@.status.provisioning.state!="available")]}{.metadata.name}{end}')
  say host "$HOST"
  say link "$(sudo virsh dumpxml $HOST | grep -A6 "network='ocnet'" | grep -o "link state='[a-z]*'")  (want down after the port move)"
  say agent "$(kubectl -n osac get agents -o jsonpath='{range .items[*]}{.metadata.name} ips={range .status.inventory.interfaces[*]}{.ipV4Addresses}{end} approved={.spec.approved}{"\n"}{end}' | cut -c1-160)"
  NODE=$(sudo python3 -c "import json;print(json.load(open('/etc/osac-fabd/config.json'))['hosts']['$HOST']['node'])")
  say vni "$NODE: $(sudo podman exec $NODE ip -br link show master br10001 2>/dev/null | cut -d' ' -f1 | tr '\n' ' ') in br10001 (tenant L2 VNI)"
  say extips "$($O get externalips 2>/dev/null | grep -c ALLOCATED) allocated: $($O get externalips 2>/dev/null | awk 'NR>1{print $8}' | tr '\n' ' ')"
  say dnat "$(sudo podman exec border nft list chain ip osacnat pre | grep -c dnat) DNAT rules"
  say hc "$(kubectl get hc -A --no-headers 2>/dev/null | awk '{print $2, $5, $6}')"
  say co "$(kubectl -n osac get clusterorder $CO -o jsonpath='{.status.phase} api={.status.apiEndpoint} ingress={.status.ingressEndpoint}')"
  for n in api console-openshift-console.apps; do say "dns $n" "$(dig +short +time=2 @198.51.100.53 $n.$CO.tenants.osac.lab | tail -1)"; done ;;
outside)
  CO=$(kubectl -n osac get clusterorders -o jsonpath='{.items[0].metadata.name}')
  API=$($WS "dig +short +time=2 api.$CO.tenants.osac.lab | tail -1"); CON=$($WS "dig +short +time=2 console-openshift-console.apps.$CO.tenants.osac.lab | tail -1")
  say ws-dns "api=$API console=$CON (want 203.0.113.x)"
  say ws-api "HTTP $($WS "curl -sk -m 8 -o /dev/null -w '%{http_code}' https://api.$CO.tenants.osac.lab:6443/version")"
  say ws-con "HTTP $($WS "curl -sk -m 10 -o /dev/null -w '%{http_code}' https://console-openshift-console.apps.$CO.tenants.osac.lab/")"
  NS=$(kubectl get hc -A -o jsonpath='{.items[0].metadata.namespace}'); HC=$(kubectl get hc -A -o jsonpath='{.items[0].metadata.name}')
  kubectl -n $NS get secret $HC-admin-kubeconfig -o jsonpath='{.data.kubeconfig}' | base64 -d | sed "s#server: https://[^ ]*#server: https://api.$CO.tenants.osac.lab:6443#" > /tmp/claude-1000/ws.kubeconfig
  $WS "test -x /usr/local/bin/kubectl" 2>/dev/null || scp -q -o StrictHostKeyChecking=no $(which kubectl) student@198.51.100.10:/tmp/kubectl && $WS "sudo install -m755 /tmp/kubectl /usr/local/bin/kubectl" 2>/dev/null
  scp -q -o StrictHostKeyChecking=no /tmp/claude-1000/ws.kubeconfig student@198.51.100.10:/tmp/kc; rm -f /tmp/claude-1000/ws.kubeconfig
  say ws-nodes "$($WS "kubectl --kubeconfig /tmp/kc --insecure-skip-tls-verify get nodes --no-headers 2>&1 | head -2" | tr '\n' ';')"
  PRIV=$(kubectl -n osac get agents -o jsonpath='{range .items[0].status.inventory.interfaces[*]}{.ipV4Addresses[0]}{"\n"}{end}' | grep . | head -1 | cut -d/ -f1)
  say isolate "worker private $PRIV:22 from workstation: $($WS "timeout 3 bash -c '</dev/tcp/$PRIV/22' 2>/dev/null && echo OPEN-BAD || echo blocked-good"); LB 10.251.0.200:6443: $($WS "timeout 3 bash -c '</dev/tcp/10.251.0.200/6443' 2>/dev/null && echo OPEN-BAD || echo blocked-good")"
  $WS "rm -f /tmp/kc" ;;
delete)
  $O delete cluster $2; date
  for i in $(seq 40); do n=$(kubectl -n osac get clusterorders --no-headers 2>/dev/null | wc -l); h=$(kubectl get hc -A --no-headers 2>/dev/null | wc -l); b=$(kubectl -n osac-inventory get bmh -o jsonpath='{range .items[*]}{.metadata.name}:{.status.provisioning.state} {end}'); echo "$(date +%T) orders=$n hc=$h bmh=[$b]"; [ "$n$h" = 00 ] && echo "$b" | grep -qv provision && break; sleep 30; done
  for v in bm-0 bm-1; do say "link $v" "$(sudo virsh dumpxml $v | grep -A6 "network='ocnet'" | grep -o "link state='[a-z]*'")"; done
  say extips "$($O get externalips 2>/dev/null | grep -c ALLOCATED) allocated"; bash $R/fabd/leakcheck.sh ;;
*) sed -n 2,7p "$0" ;;
esac
