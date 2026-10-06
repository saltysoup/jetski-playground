#!/bin/bash
# Waits for -34t3 to come back healthy from the host repair booked at 22:38:03Z, then runs
# run 8e automatically.
#
# The repair window is 2026-08-31T22:38:27Z -> 2026-09-01T02:38:17Z, and runs 1-7 showed
# the node returns some time inside it. "Healthy" here means all five of:
#   node Ready, 8 allocatable GPUs, VM RUNNING, upcomingMaintenance null, reservation HEALTHY
# All five matter. Checking only node-Ready would fire 8e while the reservation was still
# DEGRADED, and a second degradation would then be unattributable.
#
# It also requires the state to hold for STABLE consecutive polls before firing, because
# during recovery these fields flap -- run 8a's node went RUNNING -> STOPPING ->
# PROVISIONING -> STAGING -> RUNNING inside two minutes.
#
# To stop this before it fires:  pkill -f wait-and-run-8e
set -uo pipefail

NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
HERE=$(dirname "$(readlink -f "$0")")
EVERY=${EVERY:-60}
BUDGET=${BUDGET:-28800}      # 8 h
STABLE=${STABLE:-3}          # consecutive clean polls required

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

log "waiting for $NODE to return healthy (repair window ends 2026-09-01T02:38:17Z)"
ok=0
END=$(( $(date +%s) + BUDGET ))
while [ "$(date +%s)" -lt "$END" ]; do
  READY=$(kubectl get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
  GPUS=$(kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
  M=$(python3 "$HERE/probe.py" maint 2>/dev/null)

  clean=1
  [ "$READY" = "True" ] || clean=0
  [ "$GPUS" = "8" ]     || clean=0
  echo "$M" | grep -q '"vm": "RUNNING"'                || clean=0
  echo "$M" | grep -q '"upcomingMaintenance": null'    || clean=0
  echo "$M" | grep -q '"res_health": "HEALTHY"'        || clean=0

  if [ "$clean" = "1" ]; then
    ok=$(( ok + 1 ))
    log "clean poll $ok/$STABLE  ready=$READY gpus=$GPUS"
  else
    [ "$ok" -gt 0 ] && log "state regressed, resetting stability counter"
    ok=0
    log "waiting  ready=$READY gpus=$GPUS maint=$M"
  fi

  if [ "$ok" -ge "$STABLE" ]; then
    log "################ NODE RECOVERED -- STARTING RUN 8E ################"
    bash "$HERE/run8e.sh" 2>&1 | tee -a "$HERE/run8e.log"
    exit 0
  fi
  sleep "$EVERY"
done

log "ABORT: node did not return healthy within ${BUDGET}s -- 8e NOT started"
exit 1
