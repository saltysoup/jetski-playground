# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#!/bin/bash
WANDB_API_KEY="${WANDB_API_KEY:-YOUR_WANDB_API_KEY}"
HF_TOKEN="${HF_TOKEN:-YOUR_HF_TOKEN}"

# --- Step 1: Find the Ray Head Pod ---
echo "Finding Ray head pod..."
export HEAD_POD_NAME=$(kubectl get pods --selector=ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}')
if [ -z "$HEAD_POD_NAME" ]; then
    echo "Error: No running Ray head pod found. Please check your cluster."
    exit 1
fi
echo "Found head pod: $HEAD_POD_NAME"
echo ""

# --- Step 2: Verify custom container environment on Ray Head Pod ---
echo "Using custom container image: europe-west4-docker.pkg.dev/gpu-launchpad-playground/ikwak-nemo/nemo-rl:v26.06-custom"
echo ""

# --- Step 3: Define the Clean Job Script to Run on Head Pod ---
JOB_SCRIPT=$(cat <<EOF
set -ex

echo "--- Running on Ray Head Pod (\$HOSTNAME) ---"
cd /opt/nemo-rl

# Kill any lingering python processes
pkill -f run_grpo.py || true

echo "Setting environment variables..."
if [ -n "$WANDB_API_KEY" ] && [ "$WANDB_API_KEY" != "YOUR_WANDB_API_KEY" ]; then
  export WANDB_API_KEY=$WANDB_API_KEY
  export WANDB_MODE=online
  echo "WANDB_API_KEY detected: WANDB_MODE set to online"
else
  unset WANDB_API_KEY
  export WANDB_MODE=offline
  echo "WANDB_API_KEY not detected or placeholder: WANDB_MODE set to offline (use wandb sync later to upload)"
fi
export HF_TOKEN=$HF_TOKEN
export HF_HOME=/opt/nemo-rl/
export TORCH_CUDA_ARCH_LIST="9.0;10.0"

# Set Google GIB RDMA NCCL environment variables for A4/B200
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

# Write the recipe configuration file inside the pod
mkdir -p examples/configs/recipes/llm
cat << 'CONFIG_EOF' > examples/configs/recipes/llm/dapo-gemma3-27b-it-2n8g-fsdp2-automodel.yaml
defaults: ../../grpo_math_1B.yaml
grpo:
  num_prompts_per_step: 32
  num_generations_per_prompt: 16
  max_num_steps: 10
  val_period: 10
  val_at_start: true
  val_at_end: true
checkpointing:
  checkpoint_dir: results/dapo-gemma3-27b-it-2n8g
policy:
  model_name: google/gemma-3-27b-it
  tokenizer:
    name: google/gemma-3-27b-it
  train_micro_batch_size: 1
  max_total_sequence_length: 4096
  dtensor_cfg:
    activation_checkpointing: true
    tensor_parallel_size: 2
  dynamic_batching:
    enabled: true
  sequence_packing:
    enabled: false
  make_sequence_length_divisible_by: 8
  optimizer:
    name: "torch.optim.AdamW"
    kwargs:
      lr: 1.0e-06
  scheduler:
  - name: torch.optim.lr_scheduler.LinearLR
    kwargs:
      start_factor: 0.1
      end_factor: 1
      total_iters: 10
  - name: torch.optim.lr_scheduler.ConstantLR
    kwargs:
      factor: 1
      total_iters: 10000000000
  - milestones:
    - 10
  generation:
    max_new_tokens: 2048
    vllm_cfg:
      tensor_parallel_size: 4
      max_model_len: 4096
data:
  max_input_seq_length: 2048
  train:
    dataset_name: DAPOMath17K
  validation:
    dataset_name: DAPOMathAIME2024
logger:
  log_dir: logs/dapo-gemma3-27b-it-2n8g
  wandb_enabled: true
  tensorboard_enabled: true
  wandb:
    project: nemorl-gemma3
    name: dapo-gemma3-27b-it-2n8g
cluster:
  gpus_per_node: 8
  num_nodes: 2
CONFIG_EOF

### ----- Launch Gemma 3 27B IT workload on 2 nodes (16 GPUs) -----
python3 examples/run_grpo.py \
  --config examples/configs/recipes/llm/dapo-gemma3-27b-it-2n8g-fsdp2-automodel.yaml

echo "--- Job Finished ---"
EOF
)

# --- Step 4: Execute the Job ---
echo "Submitting job to $HEAD_POD_NAME..."
echo "$JOB_SCRIPT" | tr -d '\r' | kubectl exec -i $HEAD_POD_NAME -c ray-head -- /bin/bash

echo ""
echo "Job submission complete."
