#!/bin/bash
# Run 10 stimulus: synthetic Xid 79 on GPU 7 of node -34t3, plus the kill that makes the
# training job actually die.
#
# THE FORMAT IS DELIBERATELY RUN 7's, TELLS AND ALL.
#
# run8/inject63.sh stripped two things from run 7's line as "cheap tells" a sanitiser
# could key on: the leading fake '[  0.000000]' kernel timestamp, and the
# 'pid=0, name=CLAUDE-SIM-...' marker. Both are back here on purpose.
#
# Run 10 is not trying to write a convincing Xid. It is a controlled replication of run 7,
# which injected this exact string on 2026-08-24 and reached DEGRADED in 35.6 s. The GCE
# engineer told us that filtering of Xids with fake PCIe addresses was added "recently",
# possibly inside the 08-24 -> 08-27 window separating run 7 from run 8a's null. Testing
# that means changing ONE variable -- the date. A better-crafted line would confound the
# only thing we are measuring. The clean format is attempt #2, and the difference between
# the two attempts is the filter's signature.
#
# Ordering matters, same as run 7, so the latency window stays clean:
#   1. resolve the GPU-7 PIDs first   (a query, not an event -- keep it out of the window)
#   2. stamp T0 on the node's own clock
#   3. write the Xid line             (starts M1, M2, M3, M4)
#   4. kill the PIDs                  (starts the job-death clock, B2)
#
# GPU 7 is PCI 0000:cc:00.0 on this node. The PCI address is real; only the fault is not.
#
# This writes a log line and kills processes. It does NOT touch the GPU and does NOT apply
# the perform-maintenance label. Raising emergent maintenance is the point of the run and
# is authorised; the B7 label is a separate, explicitly approved step.
set -uo pipefail

ATTEMPT=${1:?usage: inject79.sh <attempt: 1=run7 format, 2=clean format>}
NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
BUS=00000000:CC:00.0
PCI=0000:cc:00
SMI=/home/kubernetes/bin/nvidia/bin/nvidia-smi
HERE=$(dirname "$(readlink -f "$0")")

case "$ATTEMPT" in
  1) LINE='[  0.000000] NVRM: Xid (PCI:'"$PCI"'): 79, pid=0, name=CLAUDE-SIM-RUN10, GPU has fallen off the bus.'
     KILL=yes ;;
  2) # Byte-authentic: one kernel timestamp (the kernel's own), no marker, no pid/name.
     LINE='NVRM: Xid (PCI:'"$PCI"'): 79, GPU has fallen off the bus.'
     KILL=no ;;   # job is already dead from attempt 1; nothing to kill
  *) echo "attempt must be 1 or 2"; exit 2 ;;
esac

POD=$(kubectl get pods -l app=xid-inject-run8 \
        --field-selector spec.nodeName="$NODE",status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "INJECT_FAILED no running xid-inject pod on $NODE"
  exit 1
fi

# Real ECC and row-remap counters, so the write-up can state with numbers that no GPU was
# harmed by this attempt. Expected to stay at zero -- run 10 is a log-level simulation.
# Run 11 is the one that actually breaks something.
kubectl exec "$POD" -- nsenter -t 1 -m -p -- \
  "$SMI" -i 7 --query-gpu=index,pci.bus_id,ecc.errors.uncorrected.volatile.total,remapped_rows.uncorrectable \
  --format=csv,noheader 2>/dev/null | sed "s/^/HW_BEFORE_A${ATTEMPT} /"

kubectl exec "$POD" -- nsenter -t 1 -m -p -- sh -c "
  PIDS=\$($SMI --query-compute-apps=pid,gpu_bus_id --format=csv,noheader 2>/dev/null \
          | grep '$BUS' | cut -d, -f1 | tr -d ' ' | tr '\n' ' ')
  echo \"GPU7_PIDS=\$PIDS\"

  T0=\$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  echo \"T0=\$T0\"

  echo '$LINE' > /dev/kmsg && echo 'KMSG_WROTE_OK' || echo 'KMSG_WRITE_FAILED'

  if [ '$KILL' = 'yes' ]; then
    if [ -n \"\$PIDS\" ]; then
      kill -9 \$PIDS 2>/dev/null && echo \"KILLED \$PIDS\" || echo \"KILL_FAILED \$PIDS\"
    else
      echo 'NO_GPU7_PIDS -- M1/M3/M4 still valid, job-death clock is not'
    fi
  else
    echo 'KILL_SKIPPED attempt 2'
  fi
" 2>&1 | tee "$HERE/inject79-a${ATTEMPT}.txt"

grep -E '^T0=' "$HERE/inject79-a${ATTEMPT}.txt" | sed 's/^T0=//' > "$HERE/t79-a${ATTEMPT}.txt"
echo "=== attempt $ATTEMPT injected at $(cat "$HERE/t79-a${ATTEMPT}.txt") ==="
echo "=== line: $LINE"
