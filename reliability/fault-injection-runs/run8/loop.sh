#!/bin/bash
# Run 8 driver: five rounds of (inject Xid 48 -> guest OS reboot -> watch for a
# maintenance reason), on node -34t3.
#
# The question: runs 1-7 showed that a single Xid 79 reliably produces an
# upcomingMaintenance event with maintenanceReasons ["FAILURE_GPU_XID","FAILURE_GPU"],
# somewhere between 19 minutes and 5h36m after injection. Does an uncorrectable ECC
# error do the same, does it produce a different reason, and does it take repetition?
#
# Per iteration:
#   1. guard   -- node Ready, VM RUNNING, no maintenance already in flight
#   2. inject  -- inject48.sh writes the Xid 48 line to the host ring buffer
#   3. confirm -- the line is queryable in Cloud Logging (proof it left the guest)
#   4. reboot  -- cloud.google.com/perform-reboot=true, per the GKE docs
#   5. wait    -- bootID changes, then node Ready with 8 GPUs allocatable
#   6. observe -- probe upcomingMaintenance every 60 s for OBSERVE seconds
#
# watch8.py is running independently at 20 s throughout and is the source of record;
# the per-iteration probes here are a summary layer, so nothing is lost if a reason
# appears between iterations rather than inside an observation window.
#
# Nothing here is the irreversible step. perform-reboot is a guest OS reboot; it is not
# perform-maintenance, which is what booked the 4-hour host repair in runs 1-7.
set -uo pipefail

NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
HERE=$(dirname "$(readlink -f "$0")")
ITERS=${ITERS:-5}
LOGWAIT=${LOGWAIT:-90}        # let the serial console exporter catch up before querying
BOOT_TIMEOUT=${BOOT_TIMEOUT:-1500}
READY_TIMEOUT=${READY_TIMEOUT:-1500}
OBSERVE=${OBSERVE:-900}       # 15 min, the interval the reason is expected to appear in
PROBE_EVERY=${PROBE_EVERY:-60}

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
phase() { echo "$1" > "$HERE/phase.txt"; }

for I in $(seq 1 "$ITERS"); do
  log "################ ITERATION $I / $ITERS ################"

  # --- 1. guard ------------------------------------------------------------------
  phase "i${I}-guard"
  READY=$(kubectl get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')
  BOOT_BEFORE=$(kubectl get node "$NODE" -o jsonpath='{.status.nodeInfo.bootID}')
  GUARD=$(python3 "$HERE/probe.py" maint)
  log "guard ready=$READY bootID=$BOOT_BEFORE"
  log "guard maint=$GUARD"
  if [ "$READY" != "True" ]; then
    log "ABORT: node not Ready at the top of iteration $I"; phase "aborted"; exit 1
  fi
  if echo "$GUARD" | grep -q '"vm": "REPAIRING"' || echo "$GUARD" | grep -q '"maintenanceStatus": "ONGOING"'; then
    log "ABORT: host maintenance already in flight -- not injecting or rebooting into it"
    phase "aborted"; exit 1
  fi

  # --- 2. inject -----------------------------------------------------------------
  phase "i${I}-inject"
  bash "$HERE/inject48.sh" "$I" 2>&1 | sed 's/^/    /'
  T_INJ=$(cat "$HERE/t-i${I}.txt" 2>/dev/null)
  log "injected at ${T_INJ:-UNKNOWN}"
  if [ -z "$T_INJ" ]; then
    log "ABORT: injection produced no timestamp"; phase "aborted"; exit 1
  fi

  # --- 3. confirm the stimulus reached Cloud Logging ------------------------------
  phase "i${I}-confirm"
  sleep "$LOGWAIT"
  python3 "$HERE/probe.py" xidlog "$I" 2>&1 | sed 's/^/    /'

  # --- 4. reboot ------------------------------------------------------------------
  phase "i${I}-reboot"
  T_LBL=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  kubectl label node "$NODE" cloud.google.com/perform-reboot=true --overwrite
  log "perform-reboot label applied at $T_LBL"

  # --- 5. wait for the reboot to happen and finish --------------------------------
  # A guest OS reboot leaves the instance RUNNING and lastStartTimestamp unchanged, so
  # bootID is the only trustworthy "it really rebooted" signal.
  phase "i${I}-rebooting"
  T_BOOT=""; T_READY=""
  END=$(( $(date +%s) + BOOT_TIMEOUT ))
  while [ "$(date +%s)" -lt "$END" ]; do
    B=$(kubectl get node "$NODE" -o jsonpath='{.status.nodeInfo.bootID}' 2>/dev/null)
    if [ -n "$B" ] && [ "$B" != "$BOOT_BEFORE" ]; then
      T_BOOT=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
      log "bootID changed $BOOT_BEFORE -> $B at $T_BOOT"
      break
    fi
    sleep 15
  done
  if [ -z "$T_BOOT" ]; then
    log "ABORT: bootID never changed within ${BOOT_TIMEOUT}s -- label may not have been consumed"
    kubectl get node "$NODE" -o jsonpath='{.metadata.labels}' | tr ',' '\n' | grep -i reboot
    phase "aborted"; exit 1
  fi

  phase "i${I}-recovering"
  END=$(( $(date +%s) + READY_TIMEOUT ))
  while [ "$(date +%s)" -lt "$END" ]; do
    R=$(kubectl get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    G=$(kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
    if [ "$R" = "True" ] && [ "$G" = "8" ]; then
      T_READY=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
      log "node Ready with 8 GPUs at $T_READY"
      break
    fi
    sleep 15
  done
  if [ -z "$T_READY" ]; then
    log "ABORT: node did not come back Ready with 8 GPUs within ${READY_TIMEOUT}s"
    phase "aborted"; exit 1
  fi

  # --- 6. observe ------------------------------------------------------------------
  phase "i${I}-observe"
  log "observing upcomingMaintenance for ${OBSERVE}s"
  FOUND=""
  END=$(( $(date +%s) + OBSERVE ))
  while [ "$(date +%s)" -lt "$END" ]; do
    M=$(python3 "$HERE/probe.py" maint)
    log "  probe $M"
    if ! echo "$M" | grep -q '"upcomingMaintenance": null'; then
      FOUND=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
      log "  *** MAINTENANCE EVENT PRESENT at $FOUND ***"
      log "  $M"
      break
    fi
    sleep "$PROBE_EVERY"
  done

  {
    echo "iter=$I inject=$T_INJ label=$T_LBL boot=$T_BOOT ready=$T_READY found=${FOUND:-none}"
    echo "  final=$(python3 "$HERE/probe.py" maint)"
  } >> "$HERE/iterations.txt"
  log "iteration $I done; found=${FOUND:-none}"
done

phase "complete"
log "################ ALL $ITERS ITERATIONS COMPLETE ################"
cat "$HERE/iterations.txt"
