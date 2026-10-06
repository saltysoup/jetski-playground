#!/bin/bash
# Run 8 stimulus: synthetic Xid 48 (Double Bit ECC) on GPU 7 of node -34t3.
#
# Two deliberate differences from run 6/7's inject.sh:
#
#   1. No PID kill. Runs 1-7 killed the GPU-7 processes so the training job would die the
#      way it would under a real "GPU fell off the bus". Run 8 has no job running and is
#      not measuring job-death latency -- the only question is whether the platform turns
#      a repeated Xid 48 into a maintenance event, and a kill would just add noise.
#
#   2. It is called five times, once per iteration, with the iteration number baked into
#      the name= field so the five injections are distinguishable in Cloud Logging.
#
# The timestamp is stamped INSIDE the pod on the node's own clock immediately before the
# kmsg write, same as run 7, so the "how long until the reason updates" clock starts on
# the same clock the platform is reading from.
#
# GPU 7 is PCI 0000:cc:00.0 on this node. The PCI address has to be the real one or the
# platform correlates the fault to nothing.
#
# This writes a log line. It does not touch the GPU, does not kill anything, and is
# reversible. The reboot that follows it (loop.sh) is not destructive either -- it is a
# guest OS reboot, not the host repair that runs 1-7's perform-maintenance label booked.
set -uo pipefail

ITER=${1:?usage: inject48.sh <iteration-number>}
NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
BUS=00000000:CC:00.0
XID=48
SMI=/home/kubernetes/bin/nvidia/bin/nvidia-smi
HERE=$(dirname "$(readlink -f "$0")")

POD=$(kubectl get pods -l app=xid-inject-run8 \
        --field-selector spec.nodeName="$NODE",status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "INJECT_FAILED no running xid-inject-run8 pod on $NODE"
  exit 1
fi

# Real uncorrected-ECC counters, recorded every iteration. These are expected to stay at
# zero: this is a log-level simulation, not a real double bit error, and saying so with
# numbers is more useful than leaving the reader to wonder.
kubectl exec "$POD" -- nsenter -t 1 -m -p -- \
  "$SMI" --query-gpu=index,pci.bus_id,ecc.errors.uncorrected.volatile.total \
  --format=csv,noheader 2>/dev/null | sed "s/^/ECC_BEFORE_I${ITER} /"

kubectl exec "$POD" -- nsenter -t 1 -m -p -- sh -c "
  T=\$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  echo \"INJECT_T=\$T\"

  # Wording matches the real driver message for Xid 48 so that the serial console
  # exporter, the log-based alert filter (\"NVRM: Xid\") and GKE's health signals all
  # treat it exactly as they would a genuine uncorrectable ECC fault.
  echo '[  0.000000] NVRM: Xid (PCI:0000:cc:00): $XID, pid=0, name=CLAUDE-SIM-RUN8-I${ITER}, An uncorrectable double bit error (DBE) has been detected on GPU in the framebuffer at partition 3, subpartition 0.' \
    > /dev/kmsg && echo 'KMSG_WROTE_OK' || echo 'KMSG_WRITE_FAILED'
" 2>&1 | tee "$HERE/inject-i${ITER}.txt"

grep -E '^INJECT_T=' "$HERE/inject-i${ITER}.txt" | sed 's/^INJECT_T=//' > "$HERE/t-i${ITER}.txt"
echo "=== iteration $ITER injected at $(cat "$HERE/t-i${ITER}.txt") ==="
