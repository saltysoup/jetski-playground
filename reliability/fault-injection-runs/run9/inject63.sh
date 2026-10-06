#!/bin/bash
# Run 8d stimulus: synthetic Xid 63 (row remapper) on GPU 7 of node -34t3.
#
# Per the GCE engineer, 2026-08-27: three consecutive Xid 63s trigger the PSIS symptom
# and emergent maintenance. They also warned that Xids carrying a fake PCIe address are
# now filtered out -- filtering they added recently, possibly inside the 2026-08-24 ->
# 2026-08-27 window that separates run 7 (Xid 79, worked) from run 8a (Xid 48, nothing).
#
# So this differs from run 8a's inject48.sh in four ways, three of them about looking
# authentic to that filter:
#
#   1. Xid 63, not 48. Row remapping is the Ampere+ DRAM error-recovery path and is on
#      the trigger list; Xid 48 apparently is not.
#
#   2. No leading '[  0.000000]'. run 8a prefixed a fake kernel timestamp, and since the
#      kernel prepends its own, every injected line landed in Cloud Logging with two:
#        [ 1435.333385] [  0.000000] NVRM: Xid ...
#      A real driver message has one. Cheap tell, removed.
#
#   3. No 'pid=0, name=CLAUDE-SIM-...' marker. run 8a used it to tell iterations apart in
#      Cloud Logging, but the real row-remapper message has no pid/name field at all, and
#      a literal 'CLAUDE-SIM' in the payload is the most obvious thing a sanitiser could
#      key on. Correlation now rides on the remapped-row address instead (see below), so
#      the line stays byte-authentic and still identifies itself.
#
#   4. No process kill. Runs 6/7 killed the GPU-7 PIDs so the training job would die;
#      there is no job running here and run 8d measures only whether the platform reacts.
#
# The row address is derived from the iteration number, so each of the five lines is
# distinguishable while remaining a plausible physical address:
#   i1 -> 0x0000001026b7ac40, i2 -> 0x0000001026b7bc40, ...
# Base value is the one from the engineer's own example.
#
# GPU 7 is PCI 0000:cc:00 on this node -- verified against nvidia-smi on 2026-08-27:
#   7, 00000000:CC:00.0, GPU-fedb4924-48ce-b6b7-d709-a5e1372c1559
# This is a real address, so the fake-PCIe filter should not apply to run 8a either; that
# remains a question for the engineer rather than a known cause.
#
# This writes a log line. It does not touch the GPU and kills nothing. It is expected to
# provoke emergent maintenance -- that is the point of the run, and it is authorised.
# It does NOT apply the perform-maintenance label.
set -uo pipefail

ITER=${1:?usage: inject63.sh <iteration-number>}
NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
PCI=0000:cc:00
XID=63
SMI=/home/kubernetes/bin/nvidia/bin/nvidia-smi
HERE=$(dirname "$(readlink -f "$0")")

# ROW_BASE lets a later run occupy a distinct address range, so logscan can tell run 8d's
# lines from run 8e's without a synthetic marker string.
ROW_BASE=${ROW_BASE:-0x1026b79c40}
ROW=$(printf '0x%016x' $(( ROW_BASE + ITER * 4096 )))

POD=$(kubectl get pods -l app=xid-inject-run8 \
        --field-selector spec.nodeName="$NODE",status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "INJECT_FAILED no running xid-inject-run8 pod on $NODE"
  exit 1
fi

# Real row-remap and ECC counters, recorded every iteration. Expected to stay at zero:
# this is a log-level simulation, and saying so with numbers beats leaving the reader to
# wonder whether we actually damaged a GPU.
kubectl exec "$POD" -- nsenter -t 1 -m -p -- \
  "$SMI" -i 7 --query-gpu=index,pci.bus_id,ecc.errors.uncorrected.volatile.total,remapped_rows.uncorrectable \
  --format=csv,noheader 2>/dev/null | sed "s/^/HW_BEFORE_I${ITER} /"

kubectl exec "$POD" -- nsenter -t 1 -m -p -- sh -c "
  T=\$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  echo \"INJECT_T=\$T\"
  echo \"ROW=$ROW\"

  # Byte-for-byte the format the engineer supplied, with only the PCI address and row
  # address substituted.
  echo 'NVRM: Xid (PCI:$PCI): $XID, Row Remapper: New row ($ROW) marked for remapping, reset gpu to activate.' \
    > /dev/kmsg && echo 'KMSG_WROTE_OK' || echo 'KMSG_WRITE_FAILED'
" 2>&1 | tee "$HERE/inject63-i${ITER}.txt"

grep -E '^INJECT_T=' "$HERE/inject63-i${ITER}.txt" | sed 's/^INJECT_T=//' > "$HERE/t63-i${ITER}.txt"
echo "=== iteration $ITER injected at $(cat "$HERE/t63-i${ITER}.txt") row=$ROW ==="
