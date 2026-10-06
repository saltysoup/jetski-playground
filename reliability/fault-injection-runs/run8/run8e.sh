#!/bin/bash
# Run 8e driver: five rounds of (inject Xid 63 -> reset GPU 7), one per minute, no reboot.
#
# The question: run 8d showed that three consecutive Xid 63s degrade the reservation block
# in 16.7 s and produce a maintenance reason in 9 m 41 s. But a real row-remap event is
# followed by a GPU reset -- the driver message literally says "reset gpu to activate", and
# the reset is what makes the remapping take effect. So does the platform still escalate
# when the operator does the prescribed remediation between faults, or does a successful
# reset clear the hit counter?
#
# That distinction matters operationally: if resetting resets the counter, an operator
# dutifully following NVIDIA's guidance would suppress the very signal that gets the host
# repaired, and a genuinely failing GPU would keep limping instead of being replaced.
#
# Differences from run 8d:
#   * a GPU reset after each injection (reset-gpu7.sh)
#   * a distinct row-address range, so Cloud Logging can separate 8e's lines from 8d's
#   * a hard stop if the first reset fails -- without the reset this is just run 8d again,
#     and re-running 8d unattended would book a second host repair for no new information
set -uo pipefail

NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
HERE=$(dirname "$(readlink -f "$0")")
ITERS=${ITERS:-5}
GAP=${GAP:-60}
OBSERVE=${OBSERVE:-1800}
PROBE_EVERY=${PROBE_EVERY:-60}
export ROW_BASE=${ROW_BASE:-0x1026b89c40}

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
  log "ABORT: a maintenance event is still present -- 8e needs a clean baseline"; exit 1; }
echo "$GUARD" | grep -q '"res_health": "HEALTHY"' || {
  log "ABORT: reservation not HEALTHY -- cannot attribute a new degradation"; exit 1; }

log "################ RUN 8E: $ITERS x (Xid 63 + GPU 7 reset), ${GAP}s apart ################"
log "row base $ROW_BASE"

for I in $(seq 1 "$ITERS"); do
  log "---- round $I / $ITERS : inject ----"
  bash "$HERE/inject63.sh" "$I" 2>&1 | sed 's/^/    /'

  log "---- round $I / $ITERS : reset GPU 7 ----"
  if bash "$HERE/reset-gpu7.sh" "8e-i${I}" 2>&1 | sed 's/^/    /'; then
    log "round $I reset OK"
  else
    log "round $I RESET FAILED"
    if [ "$I" -eq 1 ]; then
      log "ABORT: first reset failed. Continuing would just repeat run 8d and book another"
      log "       host repair for no new information. See reset-8e-i1.txt for the error."
      exit 1
    fi
    log "continuing -- later rounds tolerate a failed reset, but the result is caveated"
  fi

  [ "$I" -lt "$ITERS" ] && sleep "$GAP"
done
log "all $ITERS rounds complete"

sleep 90
log "checking Cloud Logging for 8e's injected lines"
ROW_BASE=$ROW_BASE python3 "$HERE/logscan63.py" 2>&1 | sed 's/^/    /'

# --- observe ----------------------------------------------------------------------------
log "observing for ${OBSERVE}s (watch8d.py continues past this)"
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

log "################ RUN 8E COMPLETE; found=${FOUND:-none} ################"
