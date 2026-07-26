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
export WANDB_API_KEY=$WANDB_API_KEY
export WANDB_MODE=offline
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
cat << 'CONFIG_EOF' > examples/configs/recipes/llm/dapo-gemma4-26ba4b-it-2n8g-fsdp2-automodel.yaml
defaults: ../../grpo_math_1B.yaml
grpo:
  batch_multiplier: 3
  use_leave_one_out_baseline: false
  val_period: 5
  max_val_samples: 960
  val_batch_size: 960
  use_dynamic_sampling: true
  reward_scaling:
    enabled: true
    target_min: -1.0
  reward_shaping:
    enabled: true
    overlong_buffer_length: 512
    max_response_length: 4096
loss_fn:
  reference_policy_kl_penalty: 0.0
  use_importance_sampling_correction: true
  truncated_importance_sampling_type: tis
  truncated_importance_sampling_ratio: 2
  ratio_clip_max: 0.28
  ratio_clip_c: 10
checkpointing:
  checkpoint_dir: results/dapo-gemma4-26ba4b-it-2n8g-fsdp2-automodel
  save_period: 5
policy:
  model_name: google/gemma-4-26B-A4B-it
  train_micro_batch_size: 1
  logprob_batch_size: 1
  max_total_sequence_length: 6144
  logprob_chunk_size: 4096
  optimizer:
    name: transformer_engine.pytorch.optimizers.fused_adam.FusedAdam
    kwargs:
      lr: 1.0e-06
      weight_decay: 0.1
      master_weights: true
      store_param_remainders: true
      exp_avg_dtype: torch.bfloat16
      exp_avg_sq_dtype: torch.bfloat16
  scheduler:
  - name: torch.optim.lr_scheduler.LinearLR
    kwargs:
      start_factor: 1.0e-08
      end_factor: 1.0
      total_iters: 10
  - name: torch.optim.lr_scheduler.ConstantLR
    kwargs:
      factor: 1.0
      total_iters: 10000000000
  - milestones:
    - 10
  dtensor_cfg:
    expert_parallel_size: 16
    activation_checkpointing: true
    automodel_kwargs:
      backend:
        _target_: nemo_automodel.components.models.common.utils.BackendConfig
        attn: te
        linear: te
        rms_norm: te
        experts: gmm
        dispatcher: deepep
        fake_balanced_gate: false
        rope_fusion: false
        enable_hf_state_dict_adapter: true
      freeze_config:
        freeze_vision_tower: true
        freeze_audio_tower: true
        freeze_language_model: false
  sequence_packing:
    enabled: false
  dynamic_batching:
    enabled: true
  make_sequence_length_divisible_by: 8
  generation:
    max_new_tokens: 4096
    vllm_cfg:
      tensor_parallel_size: 4
      gpu_memory_utilization: 0.4
    vllm_kwargs: {}
data:
  max_input_seq_length: 2048
  train:
    dataset_name: DAPOMath17K
  validation:
    dataset_name: DAPOMathAIME2024
  default:
    prompt_file: null
env:
  math:
    num_workers: 16
    math_verify_impl: dapo_math_verify
logger:
  wandb_enabled: false
cluster:
  gpus_per_node: 8
  num_nodes: 2
CONFIG_EOF

### ----- Launch Gemma 4 26B DAPO workload on 2 nodes (16 GPUs) -----
python3 examples/run_grpo.py \
  --config examples/configs/recipes/llm/dapo-gemma4-26ba4b-it-2n8g-fsdp2-automodel.yaml

echo "--- Job Finished ---"
EOF
)

# --- Step 4: Execute the Job ---
echo "Submitting job to $HEAD_POD_NAME..."
echo "$JOB_SCRIPT" | tr -d '\r' | kubectl exec -i $HEAD_POD_NAME -c ray-head -- /bin/bash

echo ""
echo "Job submission complete."
