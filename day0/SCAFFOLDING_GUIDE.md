# Day-0 5-Stage Inference Optimization Scaffolding: Architecture & GB300 Benchmarking Guide

This guide explains how the **Day-0 5-Stage Inference Optimization Scaffolding** works end-to-end and provides a step-by-step runbook for benchmarking a **new model** on a **new accelerator (e.g., NVIDIA GB300 NVL72 / B300)** across both **llm-d + vLLM** and **NVIDIA Dynamo + SGLang**.

---

## 1. Architecture Overview: How the 5-Stage Scaffolding Works

When a new frontier model (`GLM-5.3`, `DeepSeek-V4.1-Flash`, `Kimi-K3`, or a future Day-0 release) lands in upstream **vLLM** and **SGLang**, our goal is to measure—under strictly controlled, apples-to-apples conditions—the exact throughput (`tok/s/GPU`) and interactivity (`tok/s/user`, `P90 TTFT`) gains of each infrastructure optimization layer on multi-turn agentic workloads (`393` real Claude Code / agentic coding trajectories via `AIPerf`).

### 1.1 The Golden Rule: Zero Config Drift Across Stages
Across all 5 stages for a given stack (`llm-d + vLLM` or `NVIDIA Dynamo + SGLang`), the following are held **100% constant**:
- **Total GPU Count & Parallelism**: e.g., `16x GPUs` (`2x TP=8` for `GLM-5.3`, `1x TP=16, DCP=16, EP=16` across 2 RDMA nodes for `Kimi-K3`) or `8x GPUs` (`2x TP=4` for `DeepSeek-V4.1-Flash`).
- **Quantization & Kernels**: e.g., `FP8` / `NVFP4` / `MXFP4`, `FlashInfer MLA`, `DeepEP` / `flashinfer_trtllm` / `flashinfer_cutedsl`.
- **Speculative Decoding**: e.g., `5-tok MTP` (`GLM-5.3`) or `DSpark MTP` (`DeepSeek-V4.1-Flash`, `Kimi-K3`).
- **Scheduler Batch Caps**: `--max-num-seqs`, `--max-num-batched-tokens`, `--max-running-requests`, `--chunked-prefill-size`, and `--gpu-memory-utilization` / `--mem-fraction-static`.

### 1.2 The 5 Optimization Stages

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                             AIPerf Benchmark Runner (393 Agentic Traces)                        │
│                  Concurrency Sweep: c = [16, 32, 64, 128, 256] (or [8..128]/inst)               │
└───────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                │ OpenAI SSE Streaming (/v1/chat/completions)
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│             Multi-Stage Orchestration Router (llmd_multistage_router / dynamo_multistage_router)│
│  • Stage 1: Stateless L7 Round-Robin + Cold First-Turn Prefix (measures multi-replica tax)      │
│  • Stage 2: Prefix-Hash / Session-ID ([rid:...]) KV-Cache-Aware Affinity Routing                │
│  • Stage 3: KV-Aware Routing + Decoupled P/D Admission Control (in-flight prefill byte scoring) │
│  • Stage 4: Stage 3 + Local Host DRAM KV Cache Tiering (PCIe Gen5 / NVLink-C2C DMA)             │
│  • Stage 5: Stage 4 + Global Mooncake Distributed Store backed by Same-Zone 1,000 MBps/TiB      │
│             Google Cloud Managed Lustre (/mnt/lustre_1000mbps)                                  │
└──────────────────────┬───────────────────────────────────────────────────┬──────────────────────┘
                       │                                                   │
                       ▼                                                   ▼
┌──────────────────────────────────────────────┐   ┌──────────────────────────────────────────────┐
│           GPU Worker Pool (llm-d + vLLM)     │   │      GPU Worker Pool (Dynamo + SGLang)       │
│ • Stages 1–3: HBM3e KV Cache Only            │   │ • Stages 1–3: HBM3e RadixCache Only          │
│ • Stage 4: vLLM Native OffloadingConnector   │   │ • Stage 4: SGLang HiCache (Host DRAM Tier,   │
│   (CPUOffloadingSpec / SimpleCPUOffload)     │   │   --hicache-ratio 2.0, page_first)           │
│ • Stage 5: MooncakeStoreConnector + Lustre   │   │ • Stage 5: HiCache + Mooncake + Lustre       │
└──────────────────────────────────────────────┘   └──────────────────────────────────────────────┘
```

| Stage | Name | vLLM (`llm-d`) Implementation | SGLang (`NVIDIA Dynamo`) Implementation | What It Solves |
| :--- | :--- | :--- | :--- | :--- |
| **Stage 1** | **Naive L7 Round-Robin** *(Baseline)* | Stateless round-robin across workers; cold HBM KV cache between waves. | Stateless round-robin across workers; cold HBM `RadixCache` between waves. | Establishes the unoptimized Day-0 baseline where multi-turn sessions scatter across workers and suffer redundant prefill recomputation. |
| **Stage 2** | **KV-Cache-Aware Routing** | `llm-d` Endpoint Picker (`EPP`) prefix-hash affinity (`[rid:...]` / 64 KB prefix MD5). | `NVIDIA Dynamo` / `sglang-router --policy cache_aware` prefix affinity. | Pins all turns of a multi-turn trajectory to the worker holding its HBM KV prefix, eliminating cross-replica cache misses. |
| **Stage 3** | **KV Routing + Disaggregated P/D (HBM Only)** | `NixlConnector` P/D disaggregation + real-time prefill-byte & decode-slot load scoring (`in_flight_prefill_bytes / 128KB + decode_reqs`). | Mooncake RDMA P/D disaggregation (`--disaggregation-mode prefill/decode`) + active prefill/decode admission control. | Prevents long-context prefill bursts from stalling active decode batches (protecting P90 ITL and E2E interactivity). |
| **Stage 4** | **Host DRAM KV Cache Tiering** | **vLLM Native `OffloadingConnector`** (`CPUOffloadingSpec` to `/dev/shm` pinned DRAM, or `SimpleCPUOffloadConnector`). | **SGLang `HiCache`** (`--enable-hierarchical-cache --hicache-ratio 2.0 --hicache-mem-layout page_first --hicache-io-backend kernel`). | Extends HBM KV capacity into local Host DRAM (`512 GiB+` per node; up to `400 GB/s+` over NVLink-C2C on **GB300**), eliminating intra-node HBM evictions at low-to-medium concurrency. |
| **Stage 5** | **Mooncake + Same-Zone `1,000 MBps/TiB` Lustre KV Tier** | **`MooncakeStoreConnector`** (`mooncake_master` + `mooncake_client` over RDMA) backed by `/mnt/lustre_1000mbps/mooncake_kv_cache`. | **`HiCache + Mooncake`** (`--hicache-storage-backend mooncake`) backed by `/mnt/lustre_1000mbps/mooncake_kv_cache`. | Provides a cluster-wide, non-volatile multi-TiB KV cache tier on same-zone `1,000 MBps/TiB` Managed Lustre, eliminating both HBM and single-node DRAM thrashing at high concurrency (`c=128..256`). |

---

## 2. Directory Structure of `day0/`

```text
day0/
├── README.md                           # Executive summary & verified benchmark tables (GLM-5.3, DSV4.1-Flash, Kimi-K3)
├── SCAFFOLDING_GUIDE.md                # This architecture & GB300 operational guide
├── DAY0_JETSKI_PROMPT.md               # Copy-paste prompt for Jetski to run the 5-stage playbook on any new model/GPU
├── scripts/
│   ├── generate_day0_scaffolding.py    # Parses upstream vLLM YAML & SGLang Cookbook JS -> generates Stage 1-5 K8s manifests
│   ├── run_glm53_llmd_stages_4_and_5.py
│   ├── run_glm53_dynamo_stages_1_to_5.py
│   ├── run_dsv4_sequential_stages_4_and_5.py
│   ├── run_kimik3_dynamo_stages_1_to_5.py
│   ├── run_kimik3_llmd_stages_1_to_5.py
│   ├── generate_pareto_html.py         # Generates interactive Chart.js Pareto frontier HTML dashboards
│   └── sync_results_to_prism.py        # Uploads JSON/CSV summaries to GCS Prism & BigQuery uBench-Dash
├── routers/
│   ├── dynamo_multistage_router.py     # Stage 1-5 router for NVIDIA Dynamo + SGLang (port 8089)
│   └── llmd_multistage_router.py       # Stage 1-5 router for llm-d + vLLM (port 8088)
├── manifests/
│   ├── managed-lustre-1000mbps-pvc.yaml
│   ├── dynamo-sglang-*.yaml            # Generated Stage 1-3 (base), Stage 4 (DRAM), Stage 5 (Mooncake Lustre) manifests
│   ├── llmd-vllm-*.yaml                # Generated Stage 1-3 (base), Stage 4 (DRAM), Stage 5 (Mooncake Lustre) manifests
│   ├── dynamo-sglang-kimik3-2node-16gpu-rdma.yaml
│   └── llmd-vllm-kimik3-2node-16gpu-rdma.yaml
├── results/
│   ├── *_stage1_to_5_summary.json      # Structured stage-by-stage metrics (throughput, TTFT, ITL, E2E interactivity, KV hit%)
│   ├── *_stage1_to_5_summary.csv       # Flat CSV tables for spreadsheet/Prism ingestion
│   └── *_scaffold_benchmark_graphs.html# Interactive 4-panel HTML Pareto dashboards
└── slides/
    └── *.png                           # High-resolution Slide Ops canvas renders
```

---

## 3. Step-by-Step Guide: Benchmarking a New Model on `NVIDIA GB300` (or `B300`)

### 3.1 What Changes on `NVIDIA GB300` (`a4x` / `GB300 NVL72`) vs. `B200` (`a4-highgpu-8g`)?

| Parameter | `NVIDIA B200` (`a4-highgpu-8g`) | `NVIDIA GB300` (`GB300 NVL72` / `a4x`) | Scaffolding Impact |
| :--- | :--- | :--- | :--- |
| **HBM3e Capacity per GPU** | `180 GB` (`1,440 GB` per 8-GPU node) | **`288 GB`** (`1,152 GB` per 4-GPU compute tray; `20.7 TB` per 72-GPU rack) | **1.6x more VRAM per GPU**: `Kimi-K3` (`1.46 TiB` NVFP4) fits in **`8x GB300`** (`2,304 GB` HBM3e) instead of requiring `16x B200`! Even at `16x GB300` (`4,608 GB` HBM3e), free HBM for KV cache expands by **+1.7 TB**. |
| **FP4 Tensor Core Compute** | `9 PFLOPS` dense / `18 PFLOPS` sparse | **`15 PFLOPS` dense / `30 PFLOPS` sparse** (*Ultra Blackwell*) | **1.5x faster prefill & attention compute** for `NVFP4`/`MXFP4` recipes (`Kimi-K3`, `DeepSeek-V4.1-Flash`). |
| **CPU-GPU Interconnect (Stage 4 DRAM Tier)** | PCIe Gen5 x16 (`~64 GB/s` unidirectional per GPU) | **NVLink-C2C (`900 GB/s` bidirectional between Grace CPU & Blackwell Ultra GPU)** | **Game-changer for Stage 4 (`HiCache` / `vLLM OffloadingConnector`)**: Coherent Grace LPDDR5X memory (`up to 512 GB–1 TB` per Grace-Blackwell superchip) transfers KV blocks at **7x–10x higher bandwidth** than x86 PCIe Gen5! |
| **Scale-Up Domain** | `8 GPUs` per node (`1.8 TB/s` NVLink 5) | **`72 GPUs` per NVL72 rack (`1.8 TB/s` NVLink 5 all-to-all)** | Multi-node `TP=16` / `EP=16` / `EP=32` runs entirely over **NVLink 5** (`1.8 TB/s`) instead of cross-node RoCEv2 (`400 Gb/s`), eliminating the cross-node AllReduce/AllToAll bottleneck. |
| **Scale-Out RDMA NICs** | `8x 400 Gb/s` ConnectX-7 (`rdma-0..7`) | **`800 Gb/s` ConnectX-8 SuperNICs** (`rdma-0..3` or `rdma-0..7`) | Doubles inter-rack RDMA bandwidth for Stage 3 P/D disaggregation (`NixlConnector` / `Mooncake`) and Stage 5 Managed Lustre KV hydration. |

---

### 3.2 Step 1: Fetch Upstream Day-0 Recipes & Generate Stage 1–5 Manifests for `gb300`

`scripts/generate_day0_scaffolding.py` natively supports `--hw gb300` (`288 GB` HBM3e per GPU) and `--hw b300`.

1. **Download the upstream vLLM recipe YAML and SGLang Cookbook config**:
   ```bash
   # Example: Fetch upstream vLLM recipe YAML from vllm-project/recipes
   curl -sL https://raw.githubusercontent.com/vllm-project/recipes/main/models/<org>/<model>.yaml \
     -o /tmp/vllm_new_model.yaml

   # Example: Fetch SGLang Cookbook JS bundle for the model page
   # (Inspect the page source on https://docs.sglang.io/cookbook/... for the model's .js chunk)
   curl -sL https://docs.sglang.io/cookbook/assets/<model_chunk>.js \
     -o /tmp/sglang_new_model.js
   ```

2. **Run `generate_day0_scaffolding.py` with `--hw gb300`**:
   ```bash
   python3 day0/scripts/generate_day0_scaffolding.py \
     --model-id "<org>/<model_name>" \
     --model-path "/mnt/lustre_1000mbps/models/<org>/<model_name>" \
     --hw gb300 \
     --gpus-per-node 4 \
     --num-nodes 4 \
     --vllm-yaml /tmp/vllm_new_model.yaml \
     --sglang-js /tmp/sglang_new_model.js \
     --manifest-prefix "<short_model_tag>-gb300" \
     --output-dir day0/manifests
   ```
   This automatically generates **6 Kubernetes manifests** in `day0/manifests/`:
   - `llmd-vllm-<tag>-gb300-2x-tp<N>-gb300-upstream-recipe.yaml` *(Stages 1–3)*
   - `llmd-vllm-<tag>-gb300-2x-tp<N>-gb300-stage4-native-dram.yaml` *(Stage 4: vLLM `OffloadingConnector` / `SimpleCPUOffloadConnector`)*
   - `llmd-vllm-<tag>-gb300-2x-tp<N>-gb300-offload-lustre.yaml` *(Stage 5: `MooncakeStoreConnector` + Managed Lustre)*
   - `dynamo-sglang-<tag>-gb300-2x-tp<N>-gb300-upstream-recipe.yaml` *(Stages 1–3)*
   - `dynamo-sglang-<tag>-gb300-2x-tp<N>-gb300-stage4-hicache-dram.yaml` *(Stage 4: SGLang `HiCache` Host DRAM tier)*
   - `dynamo-sglang-<tag>-gb300-2x-tp<N>-gb300-hicache-lustre.yaml` *(Stage 5: SGLang `HiCache` + Mooncake + Managed Lustre)*

---

### 3.3 Step 2: Hardware, Storage & RDMA Pre-Flight Checklist on GKE

Before launching the pods on a new GKE cluster (`GB300` or `B200`), verify three mandatory infrastructure prerequisites:

1. **Same-Zone `1,000 MBps/TiB` Google Cloud Managed Lustre**:
   - Confirm GPU node zone:
     ```bash
     kubectl get nodes -L topology.kubernetes.io/zone,cloud.google.com/gke-accelerator
     ```
   - Ensure the Managed Lustre PVC (`day0/manifests/managed-lustre-1000mbps-pvc.yaml`) is provisioned in the **exact same zone** as the GPU nodes with `perUnitStorageThroughput: 1000` (`1,000 MBps/TiB`) and mounted at `/mnt/lustre_1000mbps`.
   - Stage model weights and draft/speculator checkpoints onto `/mnt/lustre_1000mbps/models/<org>/<model>` so multi-node workers read weights at `9–18 GB/s` instead of throttling on Hugging Face Hub or cross-region GCS.

2. **Multi-NIC GPUDirect RDMA Annotations (for Multi-Node TP/EP or P/D Disaggregation)**:
   - On GKE A4 (`B200`) and A4X (`GB300`), pods using multi-node NCCL or RDMA KV transfer (`NIXL` / `Mooncake`) must include the GKE multi-network annotations on the Pod template (see `day0/manifests/llmd-vllm-kimik3-2node-16gpu-rdma.yaml` and `day0/manifests/dynamo-sglang-kimik3-2node-16gpu-rdma.yaml`):
     ```yaml
     metadata:
       annotations:
         networking.gke.io/default-interface: 'eth0'
         networking.gke.io/interfaces: |
           [
             {"interfaceName":"eth0","network":"default"},
             {"interfaceName":"eth1","network":"gvnic-1"},
             {"interfaceName":"eth2","network":"rdma-0"},
             {"interfaceName":"eth3","network":"rdma-1"},
             {"interfaceName":"eth4","network":"rdma-2"},
             {"interfaceName":"eth5","network":"rdma-3"},
             {"interfaceName":"eth6","network":"rdma-4"},
             {"interfaceName":"eth7","network":"rdma-5"},
             {"interfaceName":"eth8","network":"rdma-6"},
             {"interfaceName":"eth9","network":"rdma-7"}
           ]
     ```
   - Mount `/usr/local/gib`, `/usr/local/nvidia`, and `/dev/infiniband` from the host, and initialize NCCL in the container entrypoint:
     ```bash
     export PATH=/usr/local/gib/bin:$PATH
     export LD_LIBRARY_PATH=/usr/local/gib/lib64:/usr/local/nvidia/lib64:$LD_LIBRARY_PATH
     source /usr/local/gib/scripts/set_nccl_env.sh
     export NCCL_NET=IB
     export NCCL_NET_PLUGIN=none
     export NCCL_TUNER_PLUGIN=none
     export NCCL_PROFILER_PLUGIN=none
     export GLOO_SOCKET_IFNAME=eth0
     export NCCL_SOCKET_IFNAME=eth0
     export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
     ```

3. **Critical Engine Stability Guardrails Discovered During Benchmarking**:
   - **Multi-Node Custom AllReduce**: Always pass `--disable-custom-all-reduce` when running multi-node `TP=16` (`nnodes >= 2`) in both SGLang and vLLM so collectives use GPUDirect RDMA NCCL (`453+ GB/s` busbw) instead of hanging on intra-node CUDA IPC handle exchange across restarts.
   - **Speculative Decoding + Hybrid Attention Memory Headroom**: When enabling `DSpark` or `EAGLE/MTP` speculative decoding on massive MoE models (`Kimi-K3`, `GLM-5.3`), set `--mem-fraction-static 0.84` (SGLang) or `--gpu-memory-utilization 0.84` (vLLM) with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`. Using `0.90` leaves insufficient scratchpad VRAM (`< 1.5 GiB`) after draft CUDA graph capture for `flashinfer::FP4BlockScaleLauncher::prepare_moe` (`1.92 GiB` workspace).
   - **Never Call `/abort_request` Mid-Decode on Hybrid Mamba/KDA + Speculative Requests**: Let each concurrency wave drain naturally via router drain polling (`in_flight_decode_reqs == 0`).

---

### 3.4 Step 3: Register the New Model in the AIPerf 393-Trace Dataset Cache

Our benchmark uses `AIPerf` (`--scenario inferencex-agentx-mvp`) with the `393`-trajectory multi-turn coding dataset (`avg=5,634` input tokens, `max=19,896` input tokens, `avg=585` output tokens).

To avoid re-tokenizing 393 long trajectories on every concurrency point (which takes ~15 minutes per run if uncached), map the new model family in `/aiperf-env/src/src/aiperf/dataset/loader/semianalysis_cc_traces_weka.py` inside `pod/aiperf-runner` to use a pre-built mmap pickle cache on `/mnt/lustre_1000mbps/kv_cache_tier/`:
```python
# Inside /aiperf-env/src/src/aiperf/dataset/loader/semianalysis_cc_traces_weka.py:
tm = str(getattr(self.config, "tokenizer", "") or "")
if any(k in tm for k in ["GLM-5", "glm-5", "Kimi-K3", "kimi-k3", "<NEW_MODEL_KEY>"]):
    cache_path = "/mnt/lustre_1000mbps/kv_cache_tier/lustre_convs_cache_393_None_glm53.pkl"
```

---

### 3.5 Step 4: Run the 5-Stage Benchmark Sweep

We execute the 5 stages from **Stage 5 down to Stage 1** (or Stage 1 up to Stage 5) using the automated stage runner scripts in `day0/scripts/`:

1. **Start the Multi-Stage Router inside `pod/aiperf-runner`**:
   - For **NVIDIA Dynamo + SGLang** (listens on port `8089`):
     ```bash
     kubectl cp day0/routers/dynamo_multistage_router.py ubench-llmd/aiperf-runner:/tmp/dynamo_multistage_router.py
     kubectl exec -n ubench-llmd pod/aiperf-runner -- bash -c \
       "nohup /aiperf-env/venv/bin/python3 -u /tmp/dynamo_multistage_router.py > /tmp/dynamo_router.log 2>&1 &"
     ```
   - For **llm-d + vLLM** (listens on port `8088`):
     ```bash
     kubectl cp day0/routers/llmd_multistage_router.py ubench-llmd/aiperf-runner:/tmp/llmd_multistage_router.py
     kubectl exec -n ubench-llmd pod/aiperf-runner -- bash -c \
       "nohup /aiperf-env/venv/bin/python3 -u /tmp/llmd_multistage_router.py > /tmp/llmd_router.log 2>&1 &"
     ```

2. **Run the Automated 5-Stage Sweep Script**:
   - Each runner script (`run_kimik3_dynamo_stages_1_to_5.py`, `run_kimik3_llmd_stages_1_to_5.py`, `run_glm53_dynamo_stages_1_to_5.py`, etc.) automatically:
     1. Configures the router stage via `POST /admin/set_stage {"stage": S, "concurrency": C}`.
     2. Executes `aiperf profile` across the 5 concurrency levels (`c = [16, 32, 64, 128, 256]` for 2-instance deployments, or `c = [8, 16, 32, 64, 128]` for 2-node single-instance deployments).
     3. Collects Prometheus KV cache hit rate metrics (`/metrics`) before and after each wave.
     4. Writes incremental JSON and CSV summaries to `day0/results/<model>_<stack>_stage1_to_5_summary.json`.

---

### 3.6 Step 5: Generate Interactive Pareto Charts, Sync to Prism, and Update Slide Ops

Once both `llm-d + vLLM` and `NVIDIA Dynamo + SGLang` 5-stage JSON summaries are saved in `day0/results/`:

1. **Generate Interactive HTML Pareto Dashboards**:
   ```bash
   python3 day0/scripts/generate_pareto_html.py
   ```
   This produces interactive 4-panel HTML dashboards in `day0/results/*_scaffold_benchmark_graphs.html` plotting:
   - **Pareto Frontier**: `Output Token Throughput (tok/s/GPU)` vs. `P90 E2E Interactivity (tok/s/user)`
   - **Throughput Scaling**: `tok/s/GPU` across concurrency levels
   - **First-Token Latency**: `P90 TTFT (ms)` across concurrency levels
   - **KV Cache Efficiency**: `Prefix Cache Hit Rate (%)` across stages

2. **Sync Results to Prism & BigQuery uBench-Dash**:
   ```bash
   CLOUDSDK_AUTH_ACCESS_TOKEN_FILE="" python3 day0/scripts/sync_results_to_prism.py
   ```

3. **Commit & Push to GitHub**:
   ```bash
   git add day0/
   git commit -m "feat(day0): add 5-stage benchmark results and scaffolding for <MODEL> on <ACCELERATOR>"
   git push origin main
   ```
