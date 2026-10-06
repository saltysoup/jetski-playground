#!/bin/bash
# Poll reservation + nodes through the maintenance window. Appends to maintenance-watch.log
OUT=/home/user/reliability-demo/maintenance-watch.log
for i in $(seq 1 240); do
  TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
  NOW=$(date -u +%H:%M:%S)
  RES=$(curl -s -H "Authorization: Bearer $TOKEN" \
    "https://compute.googleapis.com/compute/v1/projects/gpu-launchpad-playground/zones/europe-west4-b/reservations/nvidia-b200-6bsoymep8ylww" \
    | python3 -c "
import json,sys
d=json.load(sys.stdin); rs=d.get('resourceStatus',{}); m=rs.get('reservationMaintenance',{})
print('health=%-9s pending=%s ongoing=%s' % (rs.get('healthInfo',{}).get('healthStatus'), m.get('maintenancePendingCount'), m.get('maintenanceOngoingCount')))" 2>/dev/null)
  VMS=""
  for N in gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3 gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3; do
    S=$(curl -s -H "Authorization: Bearer $TOKEN" \
      "https://compute.googleapis.com/compute/v1/projects/gpu-launchpad-playground/zones/europe-west4-b/instances/$N" \
      | python3 -c "
import json,sys
d=json.load(sys.stdin)
um=d.get('resourceStatus',{}).get('upcomingMaintenance',{})
print('%s:%s/%s' % (d['name'][-4:], d.get('status'), um.get('maintenanceStatus','-')))" 2>/dev/null)
    VMS="$VMS $S"
  done
  NODES=$(kubectl get nodes 2>/dev/null | grep a4-highgpu | awk '{printf "%s=%s ", substr($1,length($1)-3), $2}')
  PODS=$(kubectl get pods 2>/dev/null | grep -c "ray-cluster.*Running")
  echo "$NOW | $RES |$VMS | k8s: $NODES| rayRunning=$PODS" >> $OUT
  sleep 55
done
