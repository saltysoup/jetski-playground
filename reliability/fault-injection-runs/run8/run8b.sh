#!/bin/bash
# Run 8b: the same five-iteration experiment as run 8a, with the injection method
# swapped from a synthetic kmsg line to DCGM field injection.
#   https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/dcgm-error-injection.html
#
# The hypothesis under test is that GKE's maintenance heuristic keys on a hit counter of
# real ECC events rather than on serial-console text, so a driver-level counter -- even a
# faked one -- might trip it where run 8a's log line did not.
#
# READ THIS BEFORE INTERPRETING A NULL RESULT ------------------------------------------
# Three independent reasons a null result here is the EXPECTED result. All three were
# established empirically on 2026-08-27, not assumed:
#
# 1. `dcgmi test --inject` writes into the DCGM FIELD CACHE of the host engine you are
#    connected to. dcgmi's own help says so: "--introspect  View values (injected and non
#    injected) in cache" / "--inject  Inject values into cache". It does not write to
#    NVML, the driver, or the GPU. Real nvidia-smi ECC counters stay 0 throughout.
#
# 2. GKE's managed dcgm-exporter runs DCGM in EMBEDDED mode -- its host engine is
#    in-process and unreachable; there is no nv-hostengine on the host and nothing
#    listening on 5555. The engine we start here is a SECOND, independent DCGM instance.
#
# 3. Measured directly against gke-managed-system/dcgm-exporter:9400, GKE's exporter
#    publishes 21 metrics and NOT ONE of them is an error field. It exports FB_FREE/USED/
#    TOTAL, GPU_TEMP, MEMORY_TEMP, GPU_UTIL, MEM_COPY_UTIL, SM_CLOCK, POWER_USAGE,
#    TOTAL_ENERGY_CONSUMPTION and eleven DCGM_FI_PROF_* profiling metrics. There is no
#    DCGM_FI_DEV_ECC_*, no XID_ERRORS, no ROW_REMAP, no RETIRED_PAGES. So even a REAL
#    double-bit ECC error on this node would not reach the platform down this path.
#
# That third point is the useful one for the maintenance-engineering meeting and it is
# true regardless of how this run comes out: the DCGM telemetry GKE collects cannot carry
# a GPU health fault. Whatever drives upcomingMaintenance, it is not this.
#
# It is still worth running the loop, because the platform may read Xid state by another
# route entirely -- an NVML poll in the guest agent, node-problem-detector on kmsg, or a
# host-side agent outside the guest. This is how we find out.
# ---------------------------------------------------------------------------------------
#
# FIELD IDS. The first draft of this script used 319/323 on the strength of the NVIDIA
# doc's worked example, labelled them DCGM_FI_DEV_ECC_DBE_VOL_TOTAL/AGG_TOTAL, and was
# wrong on both counts -- `dcgmi dmon -l` gives 319 = ecc_dbe_volatile_device and 323 =
# ecc_dbe_volatile_texture. That mattered: injecting 319+323 alone left DCGM's own health
# monitor reporting "Overall Health | Healthy". The health monitor reads the TOTALs. With
# the IDs below it reports "Overall Health | Failure ... Detected N volatile double-bit
# ECC error(s) in GPU 7. Drain the GPU and reset it or reboot the node."
#
# All six are injected together so the GPU presents as coherently sick rather than as one
# odd counter: an Xid, the volatile and aggregate DBE totals, the framebuffer-local DBE
# count Xid 48 would actually increment, and the two RMA-grade indicators.
F_XID=230        # xid_errors            -- direct analogue of run 8a's Xid 48
F_DBE_VOL=311    # ecc_dbe_volatile_total   <-- what the health monitor reads
F_DBE_AGG=313    # ecc_dbe_aggregate_total  <-- persistent lifetime count
F_DBE_DEV=319    # ecc_dbe_volatile_device  -- framebuffer, the Xid 48 site
F_RET_DBE=391    # retired_pages_dbe
F_UNC_ROW=393    # uncorrectable_remapped_rows
FIELDS="$F_XID $F_DBE_VOL $F_DBE_AGG $F_DBE_DEV $F_RET_DBE $F_UNC_ROW"
FLIST=$(echo $FIELDS | tr ' ' ',')
#
# SUSTAINED INJECTION. Introspect reports "Max Age (sec): 30" on an injected sample, but
# that number is a red herring once a health watch is active. Measured by injecting 99
# into field 311 and sampling every second: the value reads back for 2 seconds and is 0
# by the third. The health watcher polls NVML on its own schedule and overwrites the
# injected cache entry with the real counter, which is 0. So the practical lifetime of an
# injected fault is ~2 s, not 30, and a re-injection loop has to run at roughly 1 Hz to
# hold the GPU in a failed state at all. That is itself worth reporting: DCGM injection
# cannot express a persistent fault while anything is watching the field.
#
# Order differs from run 8a on purpose. 8a was inject -> reboot -> observe, because the
# reboot was part of the stimulus. Here the injected value lives only in the host engine's
# cache and dies with the reboot, so we observe FIRST and reboot afterwards. Default is
# REBOOT=0: five back-to-back reboots is exactly what destroyed the node at the end of 8a
# (read-only local SSD -> GKE auto-repair -> delete and recreate), and with REBOOT=0 the
# injected counters also accumulate monotonically, which is the better match for the
# hit-counter hypothesis.
set -uo pipefail

NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
GPU=7                          # PCI 0000:CC:00.0, the same GPU run 8a targeted
HERE=$(dirname "$(readlink -f "$0")")
ITERS=${ITERS:-5}
REBOOT=${REBOOT:-0}
OBSERVE=${OBSERVE:-900}
PROBE_EVERY=${PROBE_EVERY:-60}
REINJECT_EVERY=${REINJECT_EVERY:-1}    # measured injected-value lifetime is ~2 s, so 1 Hz
BOOT_TIMEOUT=${BOOT_TIMEOUT:-1500}
READY_TIMEOUT=${READY_TIMEOUT:-1500}

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
phase() { echo "$1" > "$HERE/phase.txt"; }
dpod() { kubectl get pods -l app=dcgm-inject-run8 \
           --field-selector spec.nodeName="$NODE",status.phase=Running \
           -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }

# The DCGM image is distroless-ish: no curl, no wget. bash's /dev/tcp is the only HTTP
# client on hand. The first draft curled `hostname -i`, which is our OWN pod IP with
# nothing on 9400 -- so that check silently returned empty for reasons unrelated to what
# it was meant to measure. Resolve the real exporter pod on THIS node instead.
exporter_ip() {
  kubectl -n gke-managed-system get pods \
    -l app.kubernetes.io/name=gke-managed-dcgm-exporter -o json 2>/dev/null \
    | python3 -c 'import json,sys
d=json.load(sys.stdin)
for p in d.get("items",[]):
    if p["spec"].get("nodeName")=="'"$NODE"'":
        print(p["status"].get("podIP","")); break' 2>/dev/null
}

exporter_grep() {
  local pod=$1 ip=$2
  [ -z "$ip" ] && { echo "    EXPORTER_IP_UNRESOLVED"; return; }
  kubectl exec "$pod" -- bash -c "
    exec 3<>/dev/tcp/$ip/9400 || { echo 'TCP_CONNECT_FAILED'; exit 1; }
    printf 'GET /metrics HTTP/1.0\r\nHost: $ip\r\n\r\n' >&3
    cat <&3 > /tmp/m.txt
    echo \"EXPORTER_BYTES=\$(wc -c </tmp/m.txt)\"
    echo -n 'EXPORTER_ERROR_METRICS='
    grep -icE 'ECC|XID|REMAP|RETIRED' /tmp/m.txt
    grep -iE 'ECC|XID|REMAP|RETIRED' /tmp/m.txt | grep 'gpu=\"$GPU\"' | head -5
  " 2>&1 | sed 's/^/    /'
}

# --- bring the DCGM host engine up on the target node -----------------------------
phase "8b-setup"
sed "s/__NODE__/$NODE/" "$HERE/dcgm-ds.yaml" | kubectl apply -f -
log "waiting for the DCGM pod"
for _ in $(seq 1 60); do [ -n "$(dpod)" ] && break; sleep 10; done
POD=$(dpod)
[ -z "$POD" ] && { log "ABORT: DCGM pod never became Ready on $NODE"; exit 1; }
log "DCGM pod = $POD"
EXP_IP=$(exporter_ip)
log "GKE dcgm-exporter on this node = ${EXP_IP:-UNRESOLVED}"

start_engine() {
  local p=$1
  # Group 0 is DCGM's built-in all-GPUs group. An earlier draft created a named group and
  # assumed it would be given ID 1, which is only true on a host engine that has never
  # made a group before -- and this engine is restarted every iteration.
  kubectl exec "$p" -- bash -c '
    pgrep -x nv-hostengine >/dev/null || nv-hostengine >/dev/null 2>&1
    sleep 4
    # Health watches. Without a watcher DCGM does not evaluate the fields, so an injected
    # value would sit in the cache with nothing looking at it.
    dcgmi health -g 0 -s a 2>&1 | tail -2
  ' 2>&1 | sed 's/^/    /'
}

for I in $(seq 1 "$ITERS"); do
  log "################ 8b ITERATION $I / $ITERS ################"
  POD=$(dpod)
  [ -z "$POD" ] && { log "ABORT: no DCGM pod at iteration $I"; phase "aborted"; exit 1; }

  phase "8b-i${I}-setup"
  start_engine "$POD"

  # --- inject -----------------------------------------------------------------------
  phase "8b-i${I}-inject"
  T_INJ=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  # Monotonically increasing so a hit-counter heuristic sees N accumulating events rather
  # than the same number restated five times. Xid stays 48 -- it is an error code, not a
  # count. Retired pages and remapped rows scale up faster; those are the RMA thresholds.
  VAL=$I
  log "injecting on gpu $GPU at $T_INJ: xid=48 dbe_vol=$VAL dbe_agg=$VAL dbe_dev=$VAL retired=$((VAL*16)) remapped=$((VAL*4))"

  # Kill any re-injector left over from the previous iteration before starting a new one.
  kubectl exec "$POD" -- bash -c 'pkill -f reinject-run8b >/dev/null 2>&1; true' 2>/dev/null

  kubectl exec "$POD" -- bash -c "
    cat > /tmp/reinject-run8b.sh <<'EOS'
#!/bin/bash
# Re-injects at ~1 Hz. The health watcher overwrites an injected value from NVML
# within ~2 s, so anything slower leaves the GPU reading healthy most of the time.
while true; do
  dcgmi test --inject --gpuid $GPU -f $F_XID     -v 48            >/dev/null 2>&1
  dcgmi test --inject --gpuid $GPU -f $F_DBE_VOL -v $VAL          >/dev/null 2>&1
  dcgmi test --inject --gpuid $GPU -f $F_DBE_AGG -v $VAL          >/dev/null 2>&1
  dcgmi test --inject --gpuid $GPU -f $F_DBE_DEV -v $VAL          >/dev/null 2>&1
  dcgmi test --inject --gpuid $GPU -f $F_RET_DBE -v $((VAL*16))   >/dev/null 2>&1
  dcgmi test --inject --gpuid $GPU -f $F_UNC_ROW -v $((VAL*4))    >/dev/null 2>&1
  sleep $REINJECT_EVERY
done
EOS
    chmod +x /tmp/reinject-run8b.sh
    setsid /tmp/reinject-run8b.sh </dev/null >/dev/null 2>&1 &
    sleep 6
    echo '--- reinjector ---'
    pgrep -fc reinject-run8b | sed 's/^/reinjector_procs=/'
    echo '--- dmon (xid, dbe_vol, dbe_agg, dbe_dev, retired, remapped) ---'
    dcgmi dmon -e $FLIST -i $GPU -c 3
    echo '--- health ---'
    dcgmi health -g 0 -c
    echo '--- real NVML counters (proof this is cache-only) ---'
    nvidia-smi --query-gpu=index,pci.bus_id,ecc.errors.uncorrected.volatile.total,retired_pages.double_bit.count \
      --format=csv,noheader 2>/dev/null | sed -n '8p'
  " 2>&1 | sed 's/^/    /' | tee "$HERE/8b-inject-i${I}.txt"

  # --- is it visible to anything outside our own host engine? -------------------------
  phase "8b-i${I}-confirm"
  sleep 60
  log "GKE dcgm-exporter's view of GPU $GPU (its embedded DCGM, not ours):"
  exporter_grep "$POD" "$EXP_IP"
  log "node conditions / GPU-related events:"
  kubectl get node "$NODE" -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}{"\n"}' \
    2>/dev/null | sed 's/^/    /'
  kubectl get events --field-selector involvedObject.name="$NODE" \
    --sort-by=.lastTimestamp -o wide 2>/dev/null | tail -5 | sed 's/^/    /'
  log "Cloud Logging scan since $T_INJ:"
  python3 "$HERE/probe.py" logscan "$T_INJ" 2>&1 | sed 's/^/    /'

  # --- observe -------------------------------------------------------------------------
  phase "8b-i${I}-observe"
  log "observing upcomingMaintenance for ${OBSERVE}s (fault held present throughout)"
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

  # Confirm the fault was still being held at the END of the window, so a null result
  # cannot be blamed on the stimulus having quietly lapsed halfway through.
  log "end-of-window health check:"
  kubectl exec "$POD" -- bash -c "
    pgrep -fc reinject-run8b | sed 's/^/reinjector_procs=/'
    dcgmi health -g 0 -c 2>&1 | grep -E 'Overall Health|Memory system' | head -3
  " 2>&1 | sed 's/^/    /'

  # --- reboot ---------------------------------------------------------------------------
  T_LBL=""; T_BOOT=""; T_READY=""
  if [ "$REBOOT" = "1" ] && [ "$I" -lt "$ITERS" ]; then
    phase "8b-i${I}-reboot"
    BOOT_BEFORE=$(kubectl get node "$NODE" -o jsonpath='{.status.nodeInfo.bootID}')
    T_LBL=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
    kubectl label node "$NODE" cloud.google.com/perform-reboot=true --overwrite
    log "perform-reboot label applied at $T_LBL"
    END=$(( $(date +%s) + BOOT_TIMEOUT ))
    while [ "$(date +%s)" -lt "$END" ]; do
      B=$(kubectl get node "$NODE" -o jsonpath='{.status.nodeInfo.bootID}' 2>/dev/null)
      [ -n "$B" ] && [ "$B" != "$BOOT_BEFORE" ] && { T_BOOT=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ); break; }
      sleep 15
    done
    [ -z "$T_BOOT" ] && { log "ABORT: bootID never changed"; phase "aborted"; exit 1; }
    log "bootID changed at $T_BOOT"
    END=$(( $(date +%s) + READY_TIMEOUT ))
    while [ "$(date +%s)" -lt "$END" ]; do
      R=$(kubectl get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
      G=$(kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
      [ "$R" = "True" ] && [ "$G" = "8" ] && { T_READY=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ); break; }
      sleep 15
    done
    [ -z "$T_READY" ] && { log "ABORT: node did not come back"; phase "aborted"; exit 1; }
    log "node Ready with 8 GPUs at $T_READY"
    for _ in $(seq 1 60); do [ -n "$(dpod)" ] && break; sleep 10; done
  fi

  {
    echo "8b iter=$I inject=$T_INJ val=$VAL label=${T_LBL:-skipped} boot=${T_BOOT:-skipped} ready=${T_READY:-skipped} found=${FOUND:-none}"
    echo "  final=$(python3 "$HERE/probe.py" maint)"
  } >> "$HERE/iterations-8b.txt"
  log "8b iteration $I done; found=${FOUND:-none}"
done

# Leave the node clean: stop the re-injector so nothing keeps writing a fake fault into a
# host engine after the experiment is over.
POD=$(dpod)
[ -n "$POD" ] && kubectl exec "$POD" -- bash -c 'pkill -f reinject-run8b >/dev/null 2>&1; true' 2>/dev/null
phase "8b-complete"
log "################ 8b COMPLETE ################"
cat "$HERE/iterations-8b.txt"
