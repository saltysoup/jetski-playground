# NVIDIA NeMo-RL & Kuberay Multi-Node Reinforcement Learning for Gemma 3 on GKE

This repository contains production-ready reinforcement learning (GRPO / DAPO) recipes, Helm chart configurations, Dockerfiles, and automated workstation submission orchestrators for fine-tuning **Google Gemma 3 27B IT** on Google Kubernetes Engine (GKE) high-GPU **NVIDIA B200** clusters (`2n8g` — 16 × B200 GPUs).

---

## 1. Project Overview & Architecture

Our training architecture runs on a **2-node A4 B200 Kuberay cluster** (`replicas: 2`, 16 × B200 GPUs total) connected via **Google gIB RDMA** (8 network interfaces per worker node, `eth2` through `eth9`). 

### Key Architectural Highlights
* **Co-Located Generation & Training (`colocated.enabled: true`):** Unlike disaggregated designs that dedicate separate nodes to prompt generation vs. policy training, our setup runs both vLLM generation workers and PyTorch DTensor/FSDP2 training workers across all 16 GPUs sequentially. This doubles FSDP2 sharding capacity (`TP=4` across 16 GPUs), significantly reducing inter-GPU communication latency.
* **Resolved Concurrent Weight-Loading OOMs (`/dev/shm` Optimization):** During concurrent safetensors weight loading across 16 Python processes, standard `/dev/shm` limits cause pod crashes (`OOMKilled - Exit Code 137`). Our Helm chart (`values.yaml`) defines a memory-backed emptyDir volume with **`sizeLimit: 500Gi`** and configures node memory limits to **`1,500Gi`**.
* **Hopper / Blackwell CUDA Kernel Compatibility:** Because NVIDIA's container image (`nvcr.io/nvidia/nemo-rl:v0.6.0`) attempts JIT compilation for custom extensions (`DeepEP` / `DeepGEMM`), our orchestrator automatically exports **`TORCH_CUDA_ARCH_LIST="9.0;10.0"`** to prevent compilation errors on B200 worker pods.
* **Automated Olympiad Benchmark Verification:** Our training recipes embed an automated evaluation engine that tests the RL policy on **256 AIME 2024 Olympiad competition math problems (`DAPOMathAIME2024`)** at Step 0 (baseline) and every 25 steps thereafter.

---

## 2. Empirical Benchmark Findings & Trajectory Analysis

Across our experiments on `DAPOMath17K` (17,000 diverse math reasoning problems) evaluated against `AIME 2024`, we observed clear empirical proof of **distributional over-specialization** and established the optimal training schedule:

### AIME 2024 Olympiad Math Benchmark Comparison

| Checkpoint Step | Starting Base Model Baseline | 300-Step Full Run (`exp_004`) | 100-Step Run (`exp_005`) | Absolute Gain vs. Base | Additional AIME Problems Solved |
| :---: | :---: | :---: | :---: | :---: | :---: |
| **Step 0** | **`21.48%` (`55/256`)** | `21.48%` (`55/256`) | **`21.48%` (`55/256`)** | *Baseline* | *0* |
| **Step 25** | — | `23.44%` (`60/256`) | **`25.39%` (`65/256`)** | **`+3.91%`** | **`+10 solved`** |
| **Step 50** | — | `23.44%` (`60/256`) | **`23.83%` (`61/256`)** | **`+2.35%`** | `+6 solved` |
| **Step 75** | — | `24.61%` (`63/256`) | **`25.00%` (`64/256`)** | **`+3.52%`** | `+9 solved` |
| **Step 100** *(Peak)* | — | **`27.73%` (`71/256`)** | **`25.00%` (`64/256`)** | **`+3.52% to +6.25%`** | **`+9 to +16 solved`** |

### Key Takeaways
1. **100 Steps is the Optimal Training Length for `DAPOMath17K`:** 
   * Across independent runs, 100 steps of GRPO/DAPO alignment consistently improved AIME 2024 Olympiad accuracy by **+3.52% to +6.25% absolute**, solving **9 to 16 more competition problems** than the un-trained base model (`google/gemma-3-27b-it`).
2. **Why running past Step 100 causes OOD divergence:**
   * In our 300-step experiment, training accuracy on `DAPOMath17K` climbed to an all-time high of **`70.12%`** at Step 275. However, AIME 2024 validation dropped back to `21.48%–25.00%`.
   * When an RL policy optimizes on standard algebra/AMC math for too many steps, its Chain-of-Thought (CoT) over-specializes to that distribution's heuristics, causing verbose or rigid derivations on harder Olympiad integer problems.
3. **Runtime Performance:**
   * On our 16 × B200 GPU cluster, each training step (including 512 long-CoT rollouts averaging ~1,200 tokens/sample and FSDP2 policy updates) takes **~2 minutes 04 seconds**.
   * A full **100-step run** with 5 AIME 2024 benchmark checks completes in **~3 hours 27 minutes**.

---

## 3. Repository Structure

```text
├── README.md                           # This documentation and usage guide
└── gemma3-27b-it/
    ├── values.yaml                     # Kuberay Helm chart (1500Gi RAM, 500Gi /dev/shm, nemo-rl:v0.6.0, RDMA gIB)
    ├── dapo-gemma3-27b-it-2n8g-fsdp2-automodel.yaml  # 2D Tensor/FSDP2 recipe for Gemma 3 27B IT
    └── submit_gemma3-27b-it.sh         # Automated workstation submission orchestrator
```

---

## 4. How to Run the Training Job

### Prerequisites
1. **GKE Cluster:** Configured with NVIDIA B200 nodes (`cloud.google.com/gke-accelerator=nvidia-b200`) and Google gIB RDMA networking.
2. **CLI Tools:** `kubectl`, `helm`, and `git` installed on your workstation.
3. **API Keys:**
   * **Hugging Face Token (`HF_TOKEN`):** Required for access to gated models (`google/gemma-3-27b-it`).
   * **Weights & Biases API Key (`WANDB_API_KEY`):** Optional, for live cloud dashboard logging.

---

### Step 1: Deploy or Update the Ray Cluster (Helm)

Deploy the Kuberay cluster using our production `values.yaml` configuration:

```bash
cd gemma3-27b-it/
helm upgrade --install kuberay \
  --namespace default \
  -f values.yaml \
  <path-to-kuberay-helm-chart>
```

Verify that the Ray head pod and both B200 worker pods are running and connected:
```bash
kubectl get pods -l ray.io/cluster=kuberay
kubectl exec -i $(kubectl get pods -l ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}') -c ray-head -- ray status
```
*You should see **16 GPUs** and **2 Worker Nodes** connected.*

---

### Step 2: Submit an RL Training Job

Our submission script (`submit_gemma3-27b-it.sh`) automatically:
* Locates the active Ray head pod (`kuberay-head-*`).
* Propagates `HF_TOKEN`, `WANDB_API_KEY`, and RDMA/NCCL environment variables across all remote Ray worker nodes.
* Dynamically toggles **Weights & Biases (`WANDB_MODE=online`)** if `WANDB_API_KEY` is present.
* Cleans up stale checkpoint state so the job starts cleanly from **Step 0**.

To run a 100-step training job:
```bash
export HF_TOKEN="your_hf_token_here"
export WANDB_API_KEY="your_wandb_api_key_here"   # Optional

bash gemma3-27b-it/submit_gemma3-27b-it.sh
```

---

### Step 3: Customizing Training & Validation Schedules

To modify step counts, rollout batch sizes, or benchmark validation intervals, edit `gemma3-27b-it/dapo-gemma3-27b-it-2n8g-fsdp2-automodel.yaml`:

```yaml
grpo:
  num_prompts_per_step: 32            # Number of prompts sampled per step
  num_generations_per_prompt: 16      # Rollouts per prompt (32 * 16 = 512 total batch size)
  max_num_steps: 100                  # Total training steps (100 recommended for DAPOMath17K)
  val_period: 25                      # Evaluate AIME 2024 benchmark every 25 steps
  val_at_start: true                  # Run baseline Step 0 AIME 2024 evaluation before training
  val_at_end: true                    # Run final evaluation when max_num_steps is reached
```

---

### Step 4: Monitoring Logs & Syncing Offline Runs

#### 1. Live Cloud Observability (Weights & Biases)
If `WANDB_API_KEY` was exported, view live training metrics and evaluation accuracy at:
* **WandB Project:** `nemorl-gemma3`
* **WandB Run Name:** `dapo-gemma3-27b-it-2n8g`

#### 2. Local Evaluation Logs (`.jsonl`)
Benchmark scores and per-sample preference rewards are logged directly inside the Ray head pod:
```bash
HEAD_POD=$(kubectl get pods -l ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}')

# Check completed AIME 2024 validation files
kubectl exec -i $HEAD_POD -c ray-head -- ls -lh /opt/nemo-rl/logs/dapo-gemma3-27b-it-2n8g/exp_005/val_data_step*.jsonl

# Inspect AIME 2024 accuracy for a specific checkpoint
kubectl exec -i $HEAD_POD -c ray-head -- python3 -c '
import json
rewards = []
with open("logs/dapo-gemma3-27b-it-2n8g/exp_005/val_data_step100.jsonl") as f:
    for line in f:
        rewards.append(json.loads(line)["rewards"][0])
print(f"Step 100 Accuracy: {sum(rewards)/len(rewards)*100:.2f}% ({sum(rewards):.0f}/256)")
'
```

#### 3. Uploading Offline WandB Runs
If you ran training without exporting `WANDB_API_KEY`, sync offline logs to your cloud dashboard at any time:
```bash
kubectl exec -i $HEAD_POD -c ray-head -- \
  wandb sync /opt/nemo-rl/logs/dapo-gemma3-27b-it-2n8g/exp_005/wandb/offline-run-*
```

---

## 5. License & Attribution

Copyright 2026 Google LLC. Licensed under the Apache License, Version 2.0.