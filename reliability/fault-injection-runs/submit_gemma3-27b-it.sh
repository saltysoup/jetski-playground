#!/bin/bash
# Submit the 2-node DAPO RL fine-tune of Gemma 3 27B IT to the Ray cluster.
#
# v2 — detached submission. v1 ran run_grpo.py in the foreground of a `kubectl exec`,
# so the driver died with the calling shell (this killed the 500-step run on 19 Aug at
# 04:03Z, 25 min in). `ray job submit --no-wait` hands the job to the Ray job manager:
# it survives client disconnect, and logs are retained and retrievable afterwards,
# which driver-type jobs are not.
#
# v3 — secrets moved out of the pod filesystem. They previously lived at /root/.nemo-secrets,
# which was ephemeral: recreating the head pod on 19 Aug destroyed them (and the custom recipe
# config with them). They are now the `nemo-secrets` Kubernetes Secret, mounted read-only at
# /etc/nemo-secrets/. Still sourced with tracing off, so they never reach the job log or
# Cloud Logging.
set -euo pipefail

NS=default
SUBMISSION_ID="${1:-dapo-$(date -u +%Y%m%dT%H%M%SZ)}"

echo "Finding Ray head pod..."
HEAD=$(kubectl get pods -n "$NS" -l ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}')
echo "Found head pod: $HEAD"
echo "Submitting job '$SUBMISSION_ID' to $HEAD (detached)..."

kubectl exec -i -n "$NS" "$HEAD" -c ray-head -- bash -s <<REMOTE
set -euo pipefail
echo "--- Running on Ray Head Pod (\$(hostname)) ---"
cd /opt/nemo-rl
pkill -f run_grpo.py || true
sleep 2

ray job submit \
  --address http://localhost:8265 \
  --submission-id "$SUBMISSION_ID" \
  --no-wait \
  -- bash -c '
      set -euo pipefail
      cd /opt/nemo-rl
      # --- secrets: never traced ---
      . /etc/nemo-secrets/nemo-secrets

      export WANDB_MODE=online
      export HF_HOME=/opt/nemo-rl/
      export TORCH_CUDA_ARCH_LIST="9.0;10.0"

      # GPUDirect-TCPXO / gIB fabric tuning for A4 (B200)
      export NCCL_NET=gIB
      export NCCL_CROSS_NIC=0
      export NCCL_NET_GDR_LEVEL=PIX
      export NCCL_P2P_NET_CHUNKSIZE=131072
      export NCCL_P2P_PCI_CHUNKSIZE=131072
      export NCCL_P2P_NVL_CHUNKSIZE=524288
      export NCCL_NVLS_CHUNKSIZE=524288
      export NCCL_IB_GID_INDEX=3
      export NCCL_IB_ADAPTIVE_ROUTING=1
      export NCCL_IB_QPS_PER_CONNECTION=4
      export NCCL_IB_TC=52
      export NCCL_IB_FIFO_TC=84
      export NCCL_TUNER_CONFIG_PATH=/usr/local/gib/configs/tuner_config_a4.txtpb

      exec python3 examples/run_grpo.py \
        --config examples/configs/recipes/llm/dapo-gemma3-27b-it-2n8g-fsdp2-automodel.yaml
    '
REMOTE

echo
echo "Submitted detached. Follow with:"
echo "  kubectl exec -n $NS $HEAD -c ray-head -- ray job logs -f $SUBMISSION_ID"
echo "  kubectl exec -n $NS $HEAD -c ray-head -- ray job status $SUBMISSION_ID"
echo "$SUBMISSION_ID" > /home/user/reliability-demo/run6/current-submission-id.txt
