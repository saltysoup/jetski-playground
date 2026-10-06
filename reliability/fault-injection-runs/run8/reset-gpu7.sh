#!/bin/bash
# Run 8e: reset GPU 7 on -34t3, the action the Xid 63 message itself asks for
# ("reset gpu to activate").
#
# NOTE ON SOURCING. The Google page the run was specified against --
# https://docs.cloud.google.com/compute/docs/troubleshooting/troubleshooting-gpus#reset-gpus
# -- says only "reset the GPUs or reboot the VM to recover and resume workloads". It does
# not give a command, prerequisites, or failure handling; I fetched it twice looking for
# them. So the command below is NVIDIA's standard one, not a Google-documented one, and
# the script is written to find out what actually happens rather than to assume.
#
# THIS IS THE FIRST STEP IN RUN 8 THAT TOUCHES HARDWARE. Every previous stimulus was a
# log line or a cache write. A GPU reset really does tear down and reinitialise the
# device, which means:
#   * it can fail outright on NVSwitch-connected platforms like A4/B200, where the GPU is
#     part of an NVLink domain and may not be individually resettable;
#   * it can fail if anything holds the device -- the gpu-device-plugin keeps NVML handles
#     open, and any GPU workload will block it;
#   * while GPU 7 is down the device plugin may mark the node's GPUs unhealthy, which is
#     itself a signal GKE can act on, up to and including node auto-repair.
# That last one is a real confound: unlike a synthetic Xid, a reset can generate genuine
# driver events, so anything the platform does afterwards is not cleanly attributable to
# the injected line. Recorded here so the write-up says so.
#
# The script does NOT escalate. If a single-GPU reset is refused it reports the exact
# error and exits non-zero; it will not fall back to resetting all eight or to a reboot.
set -uo pipefail

NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
GPU=${GPU:-7}
SMI=/home/kubernetes/bin/nvidia/bin/nvidia-smi
HERE=$(dirname "$(readlink -f "$0")")
TAG=${1:-manual}

POD=$(kubectl get pods -l app=xid-inject-run8 \
        --field-selector spec.nodeName="$NODE",status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "RESET_FAILED no running xid-inject-run8 pod on $NODE"
  exit 1
fi

echo "RESET_T=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"

kubectl exec "$POD" -- nsenter -t 1 -m -p -- sh -c "
  echo '--- processes holding GPU $GPU ---'
  $SMI --query-compute-apps=pid,gpu_bus_id,used_memory --format=csv,noheader 2>/dev/null \
    | grep -i 'cc:00' || echo '(none)'

  echo '--- persistence mode ---'
  $SMI -i $GPU --query-gpu=persistence_mode --format=csv,noheader 2>/dev/null

  echo '--- attempting reset of GPU $GPU ---'
  $SMI --gpu-reset -i $GPU 2>&1
  echo \"RESET_RC=\$?\"

  echo '--- post-reset state ---'
  $SMI -i $GPU --query-gpu=index,pci.bus_id,ecc.errors.uncorrected.volatile.total,remapped_rows.uncorrectable,remapped_rows.pending --format=csv,noheader 2>&1
" 2>&1 | tee "$HERE/reset-${TAG}.txt"

RC=$(grep -oE 'RESET_RC=[0-9]+' "$HERE/reset-${TAG}.txt" | tail -1 | cut -d= -f2)
echo "=== reset of GPU $GPU returned rc=${RC:-unknown} (tag=$TAG) ==="
[ "${RC:-1}" = "0" ] || exit 1
