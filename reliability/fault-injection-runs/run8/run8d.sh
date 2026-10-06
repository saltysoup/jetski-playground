#!/bin/bash
# Run 8d driver: five Xid 63 row-remapper injections on GPU 7 of -34t3, one per minute,
# no reboot, then observe.
#
# The question: the GCE engineer reports that three consecutive Xid 63s trigger the PSIS
# symptom and emergent maintenance. Runs 1-7 showed a kmsg Xid 79 degrades the reservation
# block in 24.5-35.6 s; run 8a showed a kmsg Xid 48 does nothing at all in three hours.
# Xid 63 is the ECC-class code on the trigger list, so this is the run that decides whether
# an ECC fault can reach the platform.
#
# Design decisions, all deliberate:
#   * FIVE injections, not three. The engineer's threshold is three; five gives two spare
#     in case one is filtered, and matches the cadence run 8b used so the runs compare.
#   * 60 s apart. "Consecutive" was not defined -- a question is out to the engineer --
#     so a tight, regular cadence is the most defensible reading.
#   * No reboot. Run 8a rebooted between iterations and that is what left the node with a
#     read-only local SSD and triggered a GKE auto-repair recreate mid-run. Nothing here
#     needs a reboot; the Xid is the whole stimulus.
#   * No perform-maintenance label, ever. If emergent maintenance fires, it fires because
#     the platform decided to, which is the result we are measuring.
#
# watch8d.py is the source of record and polls the reservation BLOCK, which is where the
# engineer said to look. This script's own probes are a summary layer.
set -uo pipefail

NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
HERE=$(dirname "$(readlink -f "$0")")
ITERS=${ITERS:-5}
GAP=${GAP:-60}
OBSERVE=${OBSERVE:-1800}
PROBE_EVERY=${PROBE_EVERY:-60}

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# --- guard ------------------------------------------------------------------------
READY=$(kubectl get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')
GUARD=$(python3 "$HERE/probe.py" maint)
log "guard ready=$READY"
log "guard maint=$GUARD"
if [ "$READY" != "True" ]; then
  log "ABORT: node not Ready"; exit 1
fi
if echo "$GUARD" | grep -q '"vm": "REPAIRING"' || echo "$GUARD" | grep -q '"maintenanceStatus": "ONGOING"'; then
  log "ABORT: maintenance already in flight -- a pre-existing event would confound the result"
  exit 1
fi
if ! echo "$GUARD" | grep -q '"upcomingMaintenance": null'; then
  log "ABORT: upcomingMaintenance is already non-null -- cannot attribute a new event"
  exit 1
fi

# --- inject -------------------------------------------------------------------------
log "################ RUN 8D: $ITERS x Xid 63, ${GAP}s apart, no reboot ################"
for I in $(seq 1 "$ITERS"); do
  log "---- injection $I / $ITERS ----"
  bash "$HERE/inject63.sh" "$I" 2>&1 | sed 's/^/    /'
  if [ "$I" -lt "$ITERS" ]; then
    sleep "$GAP"
  fi
done
log "all $ITERS injections written"

# --- confirm the lines reached Cloud Logging -----------------------------------------
# 90 s for the serial console exporter to catch up. This is also the filter check: if the
# engineer's fake-PCIe filtering strips our lines, they will be missing here, and that is
# a different failure than "the platform saw them and did not care".
sleep 90
log "checking Cloud Logging for the injected lines"
python3 "$HERE/logscan63.py" 2>&1 | sed 's/^/    /'

# --- observe ---------------------------------------------------------------------------
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
  if ! echo "$M" | grep -q '"res_health": "HEALTHY"'; then
    log "  *** RESERVATION NO LONGER HEALTHY ***"
    log "  $M"
  fi
  sleep "$PROBE_EVERY"
done

log "################ RUN 8D INJECTION PHASE COMPLETE; found=${FOUND:-none} ################"
log "watch8d.py is still polling -- check timeline-8d.log for anything arriving later"
