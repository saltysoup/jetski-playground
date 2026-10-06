#!/bin/bash
# Exit when -34t3 is back at testing baseline: VM restarted (lastStartTimestamp changed),
# node Ready, 8 allocatable GPUs, upcomingMaintenance null. Exits 1 at the deadline.
N=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
OLD="2026-09-03T23:08:34.252-07:00"
END=$(( $(date +%s) + ${1:-21600} ))
while [ "$(date +%s)" -lt "$END" ]; do
  P=$(tail -1 instance-10.jsonl)
  R=$(kubectl get node $N -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}|{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
  if ! grep -q "$OLD" <<<"$P" && grep -q '"upcomingMaintenance": null' <<<"$P" && [ "$R" = "True|8" ]; then
    echo "BASELINE_REACHED $(date -u +%Y-%m-%dT%H:%M:%SZ) node=$R probe=$P"; exit 0
  fi
  sleep 30
done
echo "DEADLINE $(date -u +%Y-%m-%dT%H:%M:%SZ) node=$R probe=$P"; exit 1
