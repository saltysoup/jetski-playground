#!/bin/bash
# Run 8f driver: five rounds of (inject Xid 63 -> force GPU 7 reset -> verify -> restore),
# one per minute. The run 8e experiment, retried now that we know what actually blocked it.
#
# THE QUESTION. Run 8d proved three consecutive Xid 63s degrade the block in 16.7 s and
# produce a maintenance reason in 9 m 41 s, with no reset in between. But a real row-remap
# is followed by a GPU reset -- the driver message says "reset gpu to activate", and the
# reset is what makes the remapping take effect. Does the platform still escalate when the
# operator performs the prescribed remediation between faults, or does the reset clear the
# hit counter? If it clears it, an operator following NVIDIA's guidance suppresses the very
# signal that gets the host repaired, and a genuinely failing GPU keeps limping.
#
# ORDER MATTERS, AND THIS IS THE MAIN DESIGN CHOICE. Each round injects FIRST, with the
# whole GPU management stack still up, and only then takes the holders down for the reset
# window and puts them straight back. The tempting simplification -- stop the daemons once,
# run all five rounds, restore at the end -- was rejected: if managed DCGM or the device
# plugin is part of the platform's detection path, a null result would be indistinguishable
# from "we broke the detector". Keeping them up at injection time means a null is
# interpretable. It costs wall clock and five container restarts; that is the right trade.
#
# CADENCE. Target is one round per 60 s measured from round start. A round takes longer
# than that in practice (drain + reset + device-plugin recovery), so the effective spacing
# lands around 80-100 s. Run 8d triggered at ~85 s spacing, so this is in the proven range;
# the actual gaps are logged rather than assumed.
#
# ABORT. If round 1 achieves no successful reset -- neither GPU 7 alone nor all eight --
# stop. Without the reset this is just run 8d again, and repeating 8d would book another
# 4.5-hour host repair for no new information. Later rounds tolerate a failed reset but
# the result is caveated.
#
# This is expected to provoke emergent maintenance. That is the point and it is authorised.
# It does NOT apply the perform-maintenance label.
set -uo pipefail

NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
HERE=$(dirname "$(readlink -f "$0")")
ITERS=${ITERS:-5}
PERIOD=${PERIOD:-60}          # target seconds between round starts
OBSERVE=${OBSERVE:-1800}
PROBE_EVERY=${PROBE_EVERY:-60}
RECOVER=${RECOVER:-90}        # max seconds to wait for the stack to come back each round
export ROW_BASE=${ROW_BASE:-0x1026b99c40}

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# --- guard --------------------------------------------------------------------------
READY=$(kubectl get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
GPUS=$(kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
GUARD=$(python3 "$HERE/probe.py" maint)
log "guard ready=$READY gpus=$GPUS"
log "guard maint=$GUARD"

[ "$READY" = "True" ] || { log "ABORT: node not Ready"; exit 1; }
[ "$GPUS" = "8" ]     || { log "ABORT: node does not have 8 allocatable GPUs (got '$GPUS')"; exit 1; }
echo "$GUARD" | grep -q '"vm": "RUNNING"' || { log "ABORT: VM not RUNNING"; exit 1; }
echo "$GUARD" | grep -q '"upcomingMaintenance": null' || {
  log "ABORT: a maintenance event is still present -- 8f needs a clean baseline"; exit 1; }
echo "$GUARD" | grep -q '"res_health": "HEALTHY"' || {
  log "ABORT: reservation not HEALTHY -- cannot attribute a new degradation"; exit 1; }

log "################ RUN 8F: $ITERS x (Xid 63 + forced GPU 7 reset), ${PERIOD}s period ################"
log "row base $ROW_BASE"

RESET_OK=0
RESET_FAIL=0

for I in $(seq 1 "$ITERS"); do
  T_START=$(date +%s)
  log "---- round $I / $ITERS : inject (management stack up) ----"
  bash "$HERE/inject63.sh" "$I" 2>&1 | sed 's/^/    /'

  log "---- round $I / $ITERS : forced reset of GPU 7 ----"
  if bash "$HERE/force-reset-gpu7.sh" "8f-i${I}" 2>&1 | sed 's/^/    /'; then
    log "round $I reset OK"
    RESET_OK=$((RESET_OK + 1))
  else
    log "round $I RESET FAILED"
    RESET_FAIL=$((RESET_FAIL + 1))
    if [ "$I" -eq 1 ]; then
      log "ABORT: first reset failed even with the holders stopped. Continuing would just"
      log "       repeat run 8d and book another host repair for no new information."
      log "       See reset-8f-i1.txt for who still held the device."
      exit 1
    fi
    log "continuing -- later rounds tolerate a failed reset, but the result is caveated"
  fi

  # --- wait for the management stack to come back before the next injection ---------
  # kubelet restarts the killed containers on its own; this just refuses to inject into a
  # half-recovered node, so every injection happens under the same conditions as round 1.
  log "---- round $I / $ITERS : waiting for stack recovery ----"
  W=0
  while [ "$W" -lt "$RECOVER" ]; do
    A=$(kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
    R=$(kubectl get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    if [ "$A" = "8" ] && [ "$R" = "True" ]; then
      log "  stack back after ${W}s (allocatable=$A ready=$R)"
      break
    fi
    sleep 5
    W=$((W + 5))
  done
  [ "$W" -ge "$RECOVER" ] && log "  WARN: stack not fully back after ${RECOVER}s (allocatable=${A:-?})"

  # --- hold the cadence -------------------------------------------------------------
  if [ "$I" -lt "$ITERS" ]; then
    EL=$(( $(date +%s) - T_START ))
    SLEEP=$(( PERIOD - EL ))
    if [ "$SLEEP" -gt 0 ]; then
      log "round $I took ${EL}s; sleeping ${SLEEP}s to hold the ${PERIOD}s period"
      sleep "$SLEEP"
    else
      log "round $I took ${EL}s, over the ${PERIOD}s period -- starting next round immediately"
    fi
  fi
done

log "all $ITERS rounds complete: ${RESET_OK} resets OK, ${RESET_FAIL} failed"

sleep 90
log "checking Cloud Logging for 8f's injected lines"
ROW_BASE=$ROW_BASE python3 "$HERE/logscan63.py" 2>&1 | sed 's/^/    /'

# --- observe ----------------------------------------------------------------------------
log "observing for ${OBSERVE}s (watch8f continues past this)"
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

log "################ RUN 8F COMPLETE; found=${FOUND:-none} ################"
