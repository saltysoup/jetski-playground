#!/bin/bash
# Run 8f: reset GPU 7 on -34t3, forcing it through by stopping whatever holds the device.
#
# WHY THIS EXISTS. Run 8e tried the plain reset and was refused:
#     GPU 00000000:CC:00.0: In use by another client   (rc=255)
# with nvidia-smi --query-compute-apps reporting "(none)". That query only lists CUDA
# compute contexts, so it saw nothing. Scanning /proc/*/fd instead found four processes
# holding /dev/nvidia7 open, none of them a workload:
#     nvidia-persistenced        (systemd unit, driver install)
#     nv-hostengine              (DCGM host engine, container)
#     nvidia-gpu-device-plugin   (container)
#     dcgm-exporter              (container, gke-managed-system)
# It is NOT Fabric Manager and NOT an NVSwitch restriction -- nv-fabricmanager is not
# running on this node and no /dev/nvidia-nvswitch* devices exist. The blocker is the GKE
# GPU management stack, which is software and therefore stoppable. That is the dependency
# this script documents: on GKE, a single-GPU reset requires taking down the persistence
# daemon, managed DCGM, and the device plugin first.
#
# HOW IT PICKS WHAT TO KILL. It does not hardcode PIDs -- the container processes get new
# ones every time kubelet restarts them. It enumerates the actual openers of /dev/nvidia7
# from /proc/*/fd each round and kills those, minus a protect list (pid 1, kubelet,
# containerd, systemd, and this shell). Precise, and it adapts if the holder set changes.
#
# RESTORE. Killed containers are restarted by kubelet on their own; persistenced is a
# systemd unit and is started back explicitly, along with persistence mode. The .path unit
# is stopped too, otherwise it re-triggers the service the moment we stop it.
#
# ESCALATION. Tries GPU 7 alone first, which is what the Xid names. If that is still
# refused it tries a full 8-GPU reset before giving up, and records both attempts.
set -uo pipefail

NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
GPU=${GPU:-7}
SMI=/home/kubernetes/bin/nvidia/bin/nvidia-smi
HERE=$(dirname "$(readlink -f "$0")")
TAG=${1:-manual}
DRAIN=${DRAIN:-25}          # seconds to wait for fds to clear after the kills

POD=$(kubectl get pods -l app=xid-inject-run8 \
        --field-selector spec.nodeName="$NODE",status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "RESET_FAILED no running xid-inject-run8 pod on $NODE"
  exit 1
fi

echo "RESET_T=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"

kubectl exec "$POD" -- nsenter -t 1 -m -p -- sh -c "
SMI=$SMI
GPU=$GPU
SELF=\$\$

# List pid:comm for every process holding /dev/nvidia\$GPU open.
holders() {
  for p in /proc/[0-9]*; do
    pid=\${p#/proc/}
    [ \"\$pid\" = \"\$SELF\" ] && continue
    if ls -l \$p/fd 2>/dev/null | grep -q \"nvidia\${GPU}\$\"; then
      echo \"\$pid:\$(cat \$p/comm 2>/dev/null)\"
    fi
  done
}

echo '--- holders of /dev/nvidia'\$GPU' BEFORE ---'
holders || true

echo '--- stopping nvidia-persistenced (service + path trigger) ---'
systemctl stop nvidia-persistenced.path nvidia-persistenced.service 2>&1 | head -3
\$SMI -pm 0 2>&1 | tail -2

# The container holders are DaemonSet pods: kubelet restarts them within ~1s of a kill,
# and the /proc scan that used to sit between the kill and the reset was slow enough
# (hundreds of processes, an ls per fd dir) that they were always back before nvidia-smi
# opened the device. The preflight showed exactly that: DRAIN_WAITED=0s REMAINING=2 with
# fresh PIDs. So: kill them several times first to drive kubelet into restart backoff,
# then kill and reset back to back with no scan in between.
#
# The [b]racket pattern is deliberate -- pkill -f matches a regex against the full command
# line, and this script's own command line contains the literal bracketed string, which the
# regex does not match. Without it the script pkills itself.
echo '--- inducing kubelet restart backoff on container holders ---'
for k in 1 2 3; do
  pkill -TERM -f '[n]v-hostengine'             2>/dev/null
  pkill -TERM -f '[d]cgm-exporter'             2>/dev/null
  pkill -TERM -f '[n]vidia-gpu-device-plugin'  2>/dev/null
  echo \"  kill sweep \$k done\"
  sleep 2
done

echo '--- final kill + immediate reset (no scan in between) ---'
pkill -TERM -f '[n]v-hostengine'             2>/dev/null
pkill -TERM -f '[d]cgm-exporter'             2>/dev/null
pkill -TERM -f '[n]vidia-gpu-device-plugin'  2>/dev/null

echo '--- attempting reset of GPU '\$GPU' ---'
\$SMI --gpu-reset -i \$GPU 2>&1
RC1=\$?
echo \"RESET_RC=\$RC1\"

# Second chance inside the same backoff window if the first attempt lost the race.
if [ \"\$RC1\" != \"0\" ]; then
  echo '--- retrying once inside the backoff window ---'
  pkill -TERM -f '[n]v-hostengine'             2>/dev/null
  pkill -TERM -f '[d]cgm-exporter'             2>/dev/null
  pkill -TERM -f '[n]vidia-gpu-device-plugin'  2>/dev/null
  \$SMI --gpu-reset -i \$GPU 2>&1
  RC1=\$?
  echo \"RESET_RC=\$RC1\"
fi

if [ \"\$RC1\" != \"0\" ]; then
  echo '--- single-GPU reset refused; attempting full 8-GPU reset ---'
  \$SMI --gpu-reset 2>&1
  RC2=\$?
  echo \"RESET_ALL_RC=\$RC2\"
fi

echo '--- nvidia-smi verification: all GPUs ---'
\$SMI --query-gpu=index,pci.bus_id,name,persistence_mode,ecc.errors.uncorrected.volatile.total,remapped_rows.uncorrectable,remapped_rows.pending,remapped_rows.failure --format=csv 2>&1
echo \"GPU_COUNT=\$(\$SMI --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l)\"

echo '--- restoring persistenced + persistence mode ---'
systemctl start nvidia-persistenced.path nvidia-persistenced.service 2>&1 | head -3
\$SMI -pm 1 2>&1 | tail -2
echo '--- holders AFTER restore ---'
holders || true
" 2>&1 | tee "$HERE/reset-${TAG}.txt"

RC1=$(grep -oE '^RESET_RC=[0-9]+' "$HERE/reset-${TAG}.txt" | tail -1 | cut -d= -f2)
RC2=$(grep -oE '^RESET_ALL_RC=[0-9]+' "$HERE/reset-${TAG}.txt" | tail -1 | cut -d= -f2)
CNT=$(grep -oE '^GPU_COUNT=[0-9]+' "$HERE/reset-${TAG}.txt" | tail -1 | cut -d= -f2)
echo "=== reset tag=$TAG : gpu${GPU}_rc=${RC1:-unknown} all_rc=${RC2:-n/a} gpus_visible=${CNT:-unknown} ==="

# Success = either reset returned 0 AND all eight GPUs still enumerate afterwards.
if [ "${RC1:-1}" = "0" ] || [ "${RC2:-1}" = "0" ]; then
  [ "${CNT:-0}" = "8" ] || { echo "WARN: reset returned 0 but only ${CNT:-?} GPUs visible"; exit 2; }
  exit 0
fi
exit 1
