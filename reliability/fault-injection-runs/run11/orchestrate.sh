#!/bin/bash
# Run 11 end-to-end orchestrator: event -> label -> repair -> Ray whole -> job stepping.
#
# WHY THIS EXISTS. Run 10's platform repair took 34 m 44 s, but its end-to-end was polluted
# by ~6 h of operator latency: the pollers died with the session, the event sat unnoticed
# for ~4 h, and the resubmit waited another 1.5 h after Ray was whole. The question for
# run 11 is "can the whole loop finish in < 60 min", which is a platform question -- so the
# two human steps (B7 label, B14 resubmit) are automated here and fire the moment their
# precondition is observed. Both were explicitly approved for run 11.
#
# Runs detached (setsid) and writes one timestamped line per milestone to orchestrate.log.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
LOG=orchestrate.log
P=gpu-launchpad-playground; Z=europe-west4-b
N=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
U="https://compute.googleapis.com/compute/v1/projects/$P/zones/$Z/instances/$N"
HEAD=ray-cluster-kuberay-head-nt7b6
JOB=dapo-run11-recovered
DEADLINE=$(( $(date +%s) + ${TOTAL:-43200} ))

ts()  { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
log() { echo "$(ts) $*" >> "$LOG"; }
inst() {
  curl -s -m 30 -H "Authorization: Bearer $(gcloud auth application-default print-access-token 2>/dev/null)" "$U"
}
alive() { [ "$(date +%s)" -lt "$DEADLINE" ] || { log "DEADLINE reached, exiting"; exit 1; }; }

OLD_START=$(inst | python3 -c "import json,sys;print(json.load(sys.stdin).get('lastStartTimestamp',''))")
log "ORCH_START old_lastStart=$OLD_START"

# --- A. wait for the maintenance reason, then label immediately (B5 -> B7) --------------
while alive; do
  S=$(inst | python3 -c "
import json,sys
um=(json.load(sys.stdin).get('resourceStatus') or {}).get('upcomingMaintenance') or {}
print('%s|%s' % (um.get('maintenanceStatus',''), ','.join(um.get('maintenanceReasons') or [])))" 2>/dev/null)
  if [ "${S%%|*}" = "PENDING" ] && [ -n "${S#*|}" ]; then
    log "B5_REASON_SEEN status=${S%%|*} reasons=${S#*|}"
    inst | python3 -c "import json,sys;print(json.dumps(json.load(sys.stdin)['resourceStatus']['upcomingMaintenance']))" > event-11.json
    log "B7_LABEL_SENT"
    OUT=$(kubectl label node "$N" cloud.google.com/perform-maintenance=true 2>&1)
    log "B7_LABEL_ACK kubectl=$OUT"
    break
  fi
  sleep 5
done

# --- B. wait for the VM restart and the node to be Ready with 8 GPUs (B12, B13) ----------
SEEN_RUN=""
while alive; do
  LS=$(inst | python3 -c "import json,sys;print(json.load(sys.stdin).get('lastStartTimestamp',''))" 2>/dev/null)
  if [ -z "$SEEN_RUN" ] && [ -n "$LS" ] && [ "$LS" != "$OLD_START" ]; then
    log "B12_VM_RESTARTED lastStartTimestamp=$LS"; SEEN_RUN=1
  fi
  R=$(kubectl get node "$N" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}|{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
  if [ -n "$SEEN_RUN" ] && [ "$R" = "True|8" ]; then
    log "B13_NODE_READY_8GPU"; break
  fi
  sleep 5
done

# --- C. wait for Ray to be whole: both workers Running and Ready (B13') ------------------
while alive; do
  W=$(kubectl get pods -l ray.io/node-type=worker -o jsonpath='{range .items[*]}{.status.phase}/{.status.containerStatuses[*].ready}{"\n"}{end}' 2>/dev/null)
  if [ "$(grep -cE '^Running/true( true)*$' <<<"$W")" -eq 2 ]; then
    log "B13b_RAY_WHOLE workers=$(tr '\n' ' ' <<<"$W")"; break
  fi
  sleep 5
done

# --- D. B14: prep-pods (not optional) then resubmit ---------------------------------------
log "B14_PREP_START"
bash ../prep-pods.sh >> prep-pods-11.out 2>&1
log "B14_PREP_END rc=$? $(tail -1 prep-pods-11.out)"
bash ../submit_gemma3-27b-it.sh "$JOB" >> submit-11.out 2>&1
log "B14_SUBMITTED job=$JOB rc=$?"
setsid nohup kubectl exec "$HEAD" -c ray-head -- ray job logs -f "$JOB" > train.log 2>&1 < /dev/null &

# --- E. first training step --------------------------------------------------------------
while alive; do
  if grep -qE "Step 1/[0-9]+" train.log 2>/dev/null; then log "B14_FIRST_STEP_STARTED"; break; fi
  if grep -qE "Step 2/[0-9]+" train.log 2>/dev/null; then log "B14_STEP2_SEEN"; break; fi
  sleep 10
done
while alive; do
  if grep -qE "Step 2/[0-9]+" train.log 2>/dev/null; then log "B14_FIRST_STEP_COMPLETE (Step 2 began)"; break; fi
  sleep 10
done
log "ORCH_DONE"
