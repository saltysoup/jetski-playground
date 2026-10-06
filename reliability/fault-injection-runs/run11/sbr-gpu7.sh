#!/bin/bash
# Run 11 stimulus: a GENUINE Xid 79 on GPU 7 of node -34t3, by asserting a PCIe Secondary
# Bus Reset on the GPU's upstream switch port while the NVIDIA driver holds the device.
#
# Every run from 3 to 10 wrote a log line. This one does not write anything to the kernel
# log; the driver does, because the GPU really does disappear from under it. That makes the
# Xid unfilterable by construction, and it is the first time we observe the platform's
# reaction to a fault we did not simulate.
#
# Topology, mapped 2026-09-24:
#   0000:c8:00.0 Google root port -> 0000:c9:00.0 PLX PEX8796 upstream
#     -> 0000:ca:01.0 downstream port -> 0000:cc:00.0 GPU 7 (sole child)
# No AER/DPC on c9:00.0 or c8:00.0, so the link-down should not be contained upstream.
#
# setpci rather than /sys/.../reset_subordinate: the sysfs path goes through
# pci_reset_bus(), which calls the driver's reset_prepare/reset_done hooks -- a cooperative
# reset the driver recovers from quietly. Writing BRIDGE_CONTROL directly means the driver
# finds out the way it would in a real failure: the device stops answering.
#
# BRIDGE_CONTROL on ca:01.0 reads 0x0002 (SERR enable). 0x0042 adds bit 6 (SBR).
#
# This DOES break GPU 7 and kills the training job. The repair is the expected exit and is
# authorised for run 11. It does not apply the perform-maintenance label itself.
set -uo pipefail

NODE=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
BRIDGE=0000:ca:01.0
GPU=0000:cc:00.0
SMI=/home/kubernetes/bin/nvidia/bin/nvidia-smi
HERE=$(dirname "$(readlink -f "$0")")

if [ "${1:-}" != "--go" ]; then
  echo "This resets GPU 7's PCIe link on $NODE. Re-run with --go."; exit 2
fi

POD=$(kubectl get pods -l app=xid-inject-run8 \
        --field-selector spec.nodeName="$NODE",status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
[ -z "$POD" ] && { echo "SBR_FAILED no xid-inject pod on $NODE"; exit 1; }
echo "POD=$POD"

kubectl exec "$POD" -- nsenter -t 1 -m -p -- sh -c "
  BC=\$(setpci -s $BRIDGE BRIDGE_CONTROL)
  echo \"BRIDGE_CONTROL_BEFORE=\$BC\"
  [ \"\$BC\" = '0002' ] || { echo 'UNEXPECTED_BRIDGE_CONTROL -- aborting'; exit 3; }

  T0=\$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
  echo \"T0=\$T0\"
  setpci -s $BRIDGE BRIDGE_CONTROL=0042 && echo SBR_ASSERTED
  sleep 1
  setpci -s $BRIDGE BRIDGE_CONTROL=0002 && echo SBR_RELEASED
  echo \"T_RELEASE=\$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)\"
  echo \"BRIDGE_CONTROL_AFTER=\$(setpci -s $BRIDGE BRIDGE_CONTROL)\"

  sleep 5
  echo '--- GPU vendor/device readback (ffff = gone) ---'
  setpci -s $GPU VENDOR_ID 2>&1; setpci -s $GPU DEVICE_ID 2>&1
  echo '--- kernel log, last 40 lines ---'
  dmesg -T | tail -40
  echo '--- nvidia-smi (20 s timeout) ---'
  timeout 20 $SMI --query-gpu=index,pci.bus_id,ecc.errors.uncorrected.volatile.total --format=csv,noheader 2>&1 || echo \"NVIDIA_SMI_RC=\$?\"
" 2>&1 | tee "$HERE/sbr-result.txt"

grep -E '^T0=' "$HERE/sbr-result.txt" | sed 's/^T0=//' > "$HERE/t0.txt"
echo "=== T0 $(cat "$HERE/t0.txt") ==="
