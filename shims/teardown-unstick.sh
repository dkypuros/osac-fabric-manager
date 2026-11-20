#!/bin/bash
# Workaround for OSAC bug #9 (cluster deletion loops on automatic ExternalIPs). Run after `osac delete cluster`.
# Phase 1: pause fulfillment-controller so the operator's cleanup passes and the deprovision job launches;
#          resume it as soon as the deprovision job exists (the worker's BMI deletion needs the controller).
# Phase 2: once the HostedCluster is gone and the worker host is available, pause again until the
#          ClusterOrder CR is gone, then resume (the controller finalizes the leftover ExternalIP/Cluster objects).
export KUBECONFIG=~/.kube/osac-hub.yaml
say() { echo "$(date +%T) $*"; }
pause()  { kubectl -n osac scale deploy/fulfillment-controller --replicas=0 >/dev/null; say "fulfillment-controller paused"; }
resume() { kubectl -n osac scale deploy/fulfillment-controller --replicas=1 >/dev/null; kubectl -n osac rollout status deploy/fulfillment-controller --timeout=120s >/dev/null; say "fulfillment-controller resumed"; }
trap 'resume' EXIT
co() { kubectl -n osac get clusterorders --no-headers 2>/dev/null | wc -l; }
hc() { kubectl get hc -A --no-headers 2>/dev/null | wc -l; }
depro() { kubectl -n osac get clusterorder -o jsonpath='{.items[0].status.provisioningJobs[*].type}' 2>/dev/null | grep -c deprovision; }
hosts_free() { ! kubectl -n osac-inventory get bmh -o jsonpath='{range .items[*]}{.status.provisioning.state} {end}' | grep -qE 'provision|deprovision'; }
[ "$(co)" = 0 ] && { say "no ClusterOrder; nothing to do"; exit 0; }
if [ "$(depro)" = 0 ]; then
  say "phase 1: deprovision not started"; pause
  for i in $(seq 40); do [ "$(depro)" != 0 ] && break; [ "$(co)" = 0 ] && break; sleep 15; done
  say "deprovision job present: $(depro)"; resume
fi
say "phase 1b: waiting for the HostedCluster to go and the worker host to be released"
for i in $(seq 120); do [ "$(hc)" = 0 ] && hosts_free && break; [ "$(co)" = 0 ] && break; sleep 20; done
say "hc=$(hc) hosts=[$(kubectl -n osac-inventory get bmh -o jsonpath='{range .items[*]}{.metadata.name}:{.status.provisioning.state} {end}')]"
if [ "$(co)" != 0 ]; then
  say "phase 2: ClusterOrder still present"; pause
  for i in $(seq 60); do [ "$(co)" = 0 ] && break; sleep 10; done
  say "ClusterOrder count: $(co)"; resume
fi
sleep 60
say "api objects: $(cd ~/src/osac-sheldon && source hub/osac/lib-api.sh && priv GET external_ips | python3 -c 'import json,sys;print(" ".join(x["metadata"]["name"]+":"+x.get("status",{}).get("state","").replace("EXTERNAL_IP_STATE_","") for x in json.load(sys.stdin)["items"]))')"
say "done: orders=$(co) hc=$(hc) hosts=[$(kubectl -n osac-inventory get bmh -o jsonpath='{range .items[*]}{.metadata.name}:{.status.provisioning.state} {end}')]"
