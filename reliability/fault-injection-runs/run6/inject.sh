#!/bin/bash
# Run 6 stimulus: synthetic Xid 79 on GPU 7 of node -34t3, plus the kill that makes the
# training job actually die.
#
# T0 is stamped INSIDE the pod, on the node's own clock, immediately before the kmsg
# write. Run 1 stamped it outside and could only report M1 as "<= 4.4 s"; run 3 moved it
# inside and got 176 ms. Every latency in this run is measured from that stamp, so the
# ordering below is deliberate:
#
#   1. resolve the GPU-7 PIDs first   (a query, not an event — keep it out of the window)
#   2. stamp T0
#   3. write the Xid line             (starts M1, M2, M3, M4)
#   4. kill the PIDs                  (starts the job-death clock)
#
# GPU 7 is PCI 0000:cc:00.0 on this node, verified against nvidia-smi. The PCI address in
# the message has to be the real one or the platform correlates the fault to nothing.
#
# This writes a log line and kills processes. It does not touch the GPU. The node stays
# healthy; everything here is reversible until the perform-maintenance label at B8.
set -uo pipefail

POD=xid-inject-run6
NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
BUS=00000000:CC:00.0
XID=79
SMI=/home/kubernetes/bin/nvidia/bin/nvidia-smi
HERE=$(dirname "$(readlink -f "$0")")

if [ "${1:-}" != "--go" ]; then
  echo "This injects a fault into $NODE and kills the running job."
  echo "Re-run with --go once the poller is up and the job is at baseline."
  exit 2
fi

echo "=== pre-injection state ==="
kubectl exec "$POD" -- nsenter -t 1 -m -p -- \
  "$SMI" --query-compute-apps=pid,gpu_bus_id,used_memory --format=csv,noheader

kubectl exec "$POD" -- nsenter -t 1 -m -p -- sh -c "
  # 1. GPU-7 PIDs, resolved before the clock starts
  PIDS=\$($SMI --query-compute-apps=pid,gpu_bus_id --format=csv,noheader \
          | grep '$BUS' | cut -d, -f1 | tr -d ' ' | tr '\n' ' ')
  echo \"GPU7_PIDS=\$PIDS\"

  # 2. T0, on the host clock, to ms
  T0=\$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  echo \"T0=\$T0\"

  # 3. the fault signal. Format matches the real driver message so that the serial console
  #    exporter, the log-based alert filter (\"NVRM: Xid\") and GKE's health signals all
  #    treat it exactly as they would a genuine Xid 79.
  echo '[  0.000000] NVRM: Xid (PCI:0000:cc:00): $XID, pid=0, name=CLAUDE-SIM-RUN6, GPU has fallen off the bus.' \
    > /dev/kmsg && echo 'KMSG_WROTE_OK' || echo 'KMSG_WRITE_FAILED'

  # 4. and the consequence. A GPU that has fallen off the bus takes its processes with it.
  if [ -n \"\$PIDS\" ]; then
    kill -9 \$PIDS 2>/dev/null && echo \"KILLED \$PIDS\" || echo \"KILL_FAILED \$PIDS\"
  else
    echo 'NO_GPU7_PIDS -- job was not running; M1/M3/M4 still valid, job-death clock is not'
  fi
" 2>&1 | tee "$HERE/inject-result.txt"

grep -E '^T0=' "$HERE/inject-result.txt" | sed 's/^T0=//' > "$HERE/t0.txt"
echo
echo "=== T0 recorded: $(cat "$HERE/t0.txt") -> run6/t0.txt ==="
echo "Confirm the poller was started with this T0."
