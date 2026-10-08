# Day-0 Model Optimization Playbook: Stage 1 → Stage 5 (`llm-d + vLLM` & `NVIDIA Dynamo + SGLang`)

This folder contains the **repeatable Day-0 Model Optimization Guide**, reusable Jetski prompt template, benchmark data (`JSON`/`CSV`), interactive Pareto HTML charts, and Slide Ops presentation decks for optimizing newly released models (`zai-org/GLM-5.3` on `16x B200` and `deepseek-ai/DeepSeek-V4.1-Flash` on `8x B200`) across a **5-stage multi-replica progression** on **vLLM (`llm-d`)** and **SGLang (`NVIDIA Dynamo`)** on Google Cloud GPU clusters (`a4-highgpu-8g`).

---

## 1. The 5-Stage Multi-Replica Optimization Progression

When a new model lands with Day-0 support in **vLLM** or **SGLang**, running naive multi-replica load balancing leaves 2.7x–3.8x throughput and interactivity on the table for multi-turn agentic workloads. We systematically optimize every Day-0 model through five strictly controlled stages (holding model weights, quantization, tensor/expert parallelism, and total GPU count constant):

| Stage | Name | Architecture | Why It Improves Performance (1-Sentence Mechanism) |
| :--- | :--- | :--- | :--- |
| **Stage 1** | **Naive L7 Round-Robin** *(Baseline)* | `2x TP=8` (`16x B200` for GLM-5.3) or `2x TP=4` (`8x B200` for DeepSeek-V4.1-Flash) behind a stateless L7 load balancer; HBM-only KV cache. | Stateless round-robin scatters consecutive turns of the same multi-turn session across replicas, destroying prefix cache locality and forcing redundant prefill recomputation. |
| **Stage 2** | **KV-Cache-Aware Routing** | `llm-d` Endpoint Picker (`EPP`) or `NVIDIA Dynamo` KV Router with prefix-hash session affinity; colocated P/D. | Prefix-hash affinity pins multi-turn sessions to the replica holding their KV blocks in HBM, eliminating cross-replica cache misses and redundant prefill compute. |
| **Stage 3** | **KV Routing + Disaggregated P/D (HBM Only)** | `1x Prefill` replica + `1x Decode` replica with RDMA/NIXL (`vLLM`) or Mooncake RDMA (`SGLang`) KV transfer + KV-aware routing. | Decoupling compute-bound prefill bursts from bandwidth-bound decode steps eliminates chunked-prefill stalls on active decode batches. |
| **Stage 4** | **Host DRAM KV Cache Tiering + Disagg P/D** | Stage 3 + **vLLM Native `OffloadingConnector`** (`CPUOffloadingSpec` `/dev/shm` Host DRAM tier, or `SimpleCPUOffloadConnector`) / **SGLang `HiCache`** (`--enable-hierarchical-cache --hicache-ratio 2.0 --hicache-mem-layout page_first --hicache-io-backend kernel`). | Offloading evicted HBM KV blocks to local pinned Host DRAM over PCIe Gen5 DMA (`~56 GB/s` per GPU) eliminates within-node HBM capacity evictions and yields ultra-low P90 TTFT at low-to-medium concurrency (`c=16..64`). |
| **Stage 5** | **Mooncake + Same-Zone `1,000 MBps/TiB` Lustre KV Tier** | Stage 4 + **Mooncake Distributed Store** (`MooncakeStoreConnector` in vLLM / `--hicache-storage-backend mooncake` in SGLang) backed by **Same-Zone `1,000 MBps/TiB` Google Cloud Managed Lustre**. | Persisting evicted KV blocks in a global Mooncake + Managed Lustre pool prevents both HBM and single-node DRAM thrashing at high concurrency (`c=128..256`) and shares warm prefixes across all nodes in the cluster. |

---

## 2. Verified 5-Stage Day-0 Benchmark Results (`zai-org/GLM-5.3` on `16x NVIDIA B200`)

Both stacks were benchmarked on `16x NVIDIA B200` (`2x a4-highgpu-8g` nodes in `europe-west4-b`) using the `semianalysis_cc_traces_weka_062126` multi-turn agentic coding workload (`AIPerf`, `393` trajectories, `c = 16, 32, 64, 128, 256`).

### 2.1 `zai-org/GLM-5.3` — `llm-d + vLLM` (`2x TP=8 = 16x B200`, FP8 + 5-tok MTP)

| Stage | Avg KV Hit Rate | `c=16` P90 TTFT (`ms`) | `c=64` Tput (`tok/s/GPU`) | `c=128` Tput (`tok/s/GPU`) | `c=256` Tput (`tok/s/GPU`) | `c=256` P90 TTFT (`ms`) | P90 Interactivity (`c=64 \| Peak tok/s/u`) | Gain vs. Stage 1 (`c=256`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1. Naive L7 Round-Robin** | `42.2%` | `1,007.1` | `85.77` | `93.89` | `69.14` | `15,346.5` | `33.32 \| 72.71` | `1.00x Base` |
| **2. KV-Cache-Aware Routing** | `66.1%` | `907.2` | `107.80` | `108.40` | `94.71` | `11,484.1` | `43.37 \| 76.74` | `1.37x Tput \| 1.3x Int` |
| **3. KV Routing + P/D Disagg (HBM)** | `80.8%` | `564.7` | `134.07` | `143.95` | `131.07` | `6,961.8` | `57.14 \| 91.59` | `1.90x Tput \| 2.1x Int` |
| **4. vLLM Native `OffloadingConnector` (Host DRAM)** | `89.4%` | **`251.6`** | `121.32` | `140.29` | `159.16` | `3,575.5` | `63.57 \| 110.25` | `2.30x Tput \| 2.7x Int` |
| **5. Mooncake + `1,000 MBps/TiB` Lustre KV Tier** | **`93.0%`** | `253.0` | **`129.42`** | **`157.18`** | **`203.27`** | **`883.3`** | **`68.78 \| 110.13`** | **`2.94x Tput \| 4.6x Int`** |

---

### 2.2 `zai-org/GLM-5.3` — `NVIDIA Dynamo + SGLang` (`2x TP=8, EP=8, DP=8 = 16x B200`, FP8 + DeepEP + EAGLE MTP)

| Stage | Avg KV Hit Rate | `c=16` P90 TTFT (`ms`) | `c=64` Tput (`tok/s/GPU`) | `c=128` Tput (`tok/s/GPU`) | `c=256` Tput (`tok/s/GPU`) | `c=256` P90 TTFT (`ms`) | P90 Interactivity (`c=64 \| Peak tok/s/u`) | Gain vs. Stage 1 (`c=256`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1. Naive L7 Round-Robin** | `13.2%` | `1,552.2` | `31.19` | `44.49` | `57.15` | `9,421.6` | `20.69 \| 62.45` | `1.00x Base` |
| **2. Dynamo KV-Cache-Aware Routing** | `65.3%` | `810.7` | `57.17` | `79.01` | `81.92` | `4,926.1` | `44.77 \| 80.33` | `1.43x Tput \| 1.6x Int` |
| **3. Dynamo P/D Disagg + KV Routing (HBM)** | `78.8%` | `493.5` | `63.64` | `86.01` | `101.60` | `3,192.7` | `49.10 \| 85.57` | `1.78x Tput \| 2.6x Int` |
| **4. SGLang `HiCache` Host DRAM Tier (`page_first`)** | **`97.0%`** | `359.9` | `63.64` | `89.99` | `109.22` | **`2,073.0`** | **`53.15 \| 85.23`** | `1.91x Tput \| 3.0x Int` |
| **5. Mooncake + `1,000 MBps/TiB` Lustre KV Tier** | `94.0%` | **`357.3`** | **`70.08`** | **`99.15`** | **`133.96`** | `2,297.0` | `50.60 \| 85.59` | **`2.34x Tput \| 2.6x Int`** |

---

## 3. Verified 5-Stage Day-0 Benchmark Results (`deepseek-ai/DeepSeek-V4.1-Flash` on `8x NVIDIA B200`)

### 3.1 `DeepSeek-V4.1-Flash` — `llm-d + vLLM` (`2x TP=4 = 8x B200`, MXFP4 + FP8 KV + 5-tok DSpark MTP)

| Stage | Avg KV Hit Rate | `c=64` Tput (`tok/s/GPU`) | `c=128` Tput (`tok/s/GPU`) | `c=256` Tput (`tok/s/GPU`) | `c=128` P90 TTFT (`ms`) | `c=256` P90 TTFT (`ms`) | P90 Interactivity (`c=64 \| Peak tok/s/u`) | Gain vs. Stage 1 (`c=256`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1. Naive L7 Round-Robin** | `96.9%` | `417.12` | `570.28` | `541.60` | `2,965.9` | `7,312.7` | `96.90 \| 96.90` | `1.00x Base` |
| **2. KV-Cache-Aware Routing** | `98.8%` | `462.43` | `678.70` | `821.88` | `1,951.3` | `4,952.3` | `129.83 \| 129.83` | `1.52x Tput \| 1.5x Int` |
| **3. KV Routing + P/D Disagg (HBM)** | `99.3%` | `503.30` | `742.66` | `853.61` | `1,184.7` | `2,947.1` | `144.94 \| 233.44*` | `1.58x Tput \| 2.4x Int` |
| **4. vLLM Native `OffloadingConnector` (Host DRAM)** | `98.8%` | `625.61` | `866.41` | `1,163.84` | `1,265.2` | `2,473.9` | `140.75 \| 242.15*` | `2.15x Tput \| 2.5x Int` |
| **5. Mooncake + `1,000 MBps/TiB` Lustre KV Tier** | **`99.7%`** | **`635.91`** | **`1,013.01`** | **`1,438.92`** | **`478.1`** | **`1,934.4`** | **`159.86 \| 251.28*`** | **`2.66x Tput \| 2.6x Int`** |

---

### 3.2 `DeepSeek-V4.1-Flash` — `NVIDIA Dynamo + SGLang` (`2x TEP=4 = 8x B200`, NVFP4 + FP8 KV + 5-tok DSpark MTP)

| Stage | Avg KV Hit Rate | `c=32` Tput (`tok/s/GPU`) | `c=64` Tput (`tok/s/GPU`) | `c=128` Tput (`tok/s/GPU`) | `c=32` P90 TTFT (`ms`) | `c=128` P90 TTFT (`ms`) | P90 Interactivity (`c=32 \| Peak tok/s/u`) | Gain vs. Stage 1 (`c=32`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1. Naive L7 Round-Robin** | `84.8%` | `63.00` | `105.00` | `123.00` | `16,276.4` | `22,354.5` | `33.86 \| 44.31` | `1.00x Base` |
| **2. Dynamo KV-Cache-Aware Routing** | `92.7%` | `166.38` | `177.12` | `193.00` | `7,190.4` | `22,452.4` | `36.14 \| 42.34` | `2.64x Tput (1.57x Pk)` |
| **3. Dynamo P/D Disagg + KV Routing (HBM)** | `97.6%` | `185.38` | `266.75` | `290.12` | `4,215.2` | `7,208.1` *(c=64)* | `44.50 \| 61.93` | `2.94x Tput (2.36x Pk)` |
| **4. SGLang `HiCache` Host DRAM Tier** | `97.6%` | `232.98` | `272.25` | `304.99` | `4,650.2` | `19,357.1` | `80.17 \| 136.69*` | `3.70x Tput (2.48x Pk)` |
| **5. Mooncake + `1,000 MBps/TiB` Lustre KV Tier** | **`99.6%`** | **`241.88`** | **`291.75`** | **`331.75`** | **`4,562.5`** | **`17,462.6`** | **`81.32 \| 137.46*`** | **`3.84x Tput (2.70x Pk)`** |

---

## 4. Verified 5-Stage Day-0 Benchmark Results (`moonshotai/Kimi-K3` NVFP4 on `16x NVIDIA B200` Multi-Host RDMA)

`moonshotai/Kimi-K3` is a **2.8T-parameter hybrid MoE** (`16/896` active experts, Kimi Delta Attention + Gated MLA, `1.46 TiB` in `NVFP4`) requiring **2 `a4-highgpu-8g` nodes (`16x NVIDIA B200` = `2,880 GB` HBM3e)** interconnected via **8x 400 Gb/s GPUDirect RDMA NICs (`453.4 GB/s` cross-node `all_reduce` busbw)**. Concurrency sweep: `c = [8, 16, 32, 64, 128]` (`393` multi-turn agentic traces).

### 4.1 `moonshotai/Kimi-K3` — `NVIDIA Dynamo + SGLang` (`TP=16, DCP=16, EP=16, nnodes=2 = 16x B200`, NVFP4 + FP8 KV + 3-tok DSpark MTP)

| Stage | Avg KV Hit Rate | `c=16` P90 TTFT (`ms`) | `c=16` Tput (`tok/s/GPU`) | `c=32` Tput (`tok/s/GPU`) | `c=128` Tput (`tok/s/GPU`) | `c=32` P90 TTFT (`ms`) | P90 Interactivity (`c=16 \| Peak tok/s/u`) | Gain vs. Stage 1 (`c=16`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1. Naive L7 Round-Robin** | `35.3%` | `4,283.8` | `3.51` | *Saturated* | *Saturated* | *Saturated* | `14.84 \| 42.13` | `1.00x Base` |
| **2. Dynamo KV-Cache-Aware Routing** | `54.6%` | `3,405.6` | `12.34` | `10.93` | `0.49` | `8,369.6` | `25.73 \| 41.80` | `3.52x Tput \| 1.7x Int` |
| **3. Multi-Node DCP=16 + EP=16 + Active Load** | `66.1%` | `2,689.9` | `11.47` | `15.35` | `3.92` | `5,065.3` | `27.97 \| 45.71` | `3.27x Tput (4.37x Pk)` |
| **4. SGLang `HiCache` Host DRAM Tier (`page_first`)** | **`91.2%`** | **`1,248.7`** | **`14.96`** | **`18.43`** | **`16.49`** | **`1,602.3`** | **`37.66 \| 51.93`** | **`4.26x Tput \| 2.5x Int`** |
| **5. Mooncake + `1,000 MBps/TiB` Lustre KV Tier** | `87.5%` | `1,280.7` | `13.89` | **`18.43`** | **`16.49`** | `1,648.3` | `33.52 \| 52.83` | **`3.96x Tput (5.25x Pk)`** |

---

## 5. Repository Contents (`day0/`)

- **[`SCAFFOLDING_GUIDE.md`](SCAFFOLDING_GUIDE.md)**: **Complete Architecture & Operational Runbook** explaining how the 5-Stage Day-0 Scaffolding works under the hood and step-by-step instructions for benchmarking a **new model on a new accelerator (`NVIDIA GB300 NVL72 / B300`)**.
- **[`DAY0_JETSKI_PROMPT.md`](DAY0_JETSKI_PROMPT.md)**: Copy-paste prompt template for Jetski to automatically run the 5-stage playbook on any new model (`GLM-5.3`, `DeepSeek-V4.1-Flash`, `Kimi-K3`, etc.) and accelerator (`gb300`, `b200`).
- [`scripts/generate_day0_scaffolding.py`](scripts/generate_day0_scaffolding.py): Automatic Day-0 upstream recipe parser & 5-stage GKE manifest generator (`--hw b200|gb300|b300`). Ingests official recipes from `recipes.vllm.ai` (`vllm-project/recipes`) and `docs.sglang.io/cookbook`, dynamically resolves GPU topology (`resolve_topology`), configures Stage 4 Host DRAM KV cache tiering (`vLLM OffloadingConnector` / `SGLang HiCache`) and Stage 5 Mooncake + same-zone `1,000 MBps/TiB` Managed Lustre KV cache offloading.
- [`scripts/generate_pareto_html.py`](scripts/generate_pareto_html.py): Generates interactive 5-Stage Pareto HTML benchmark charts across all model/stack combinations (`GLM-5.3`, `DeepSeek-V4.1-Flash`, and `Kimi-K3` on `llm-d + vLLM` and `NVIDIA Dynamo + SGLang`).
- [`scripts/sync_results_to_prism.py`](scripts/sync_results_to_prism.py): Automated uploader for **Prism** (`gs://ubench-logs/prism-results-store/`) and **uBench-Dash** (`ml-workload-benchmarks.benchmark_dataset_v2.inference_run_summary`).
- [`routers/dynamo_multistage_router.py`](routers/dynamo_multistage_router.py) & [`routers/llmd_multistage_router.py`](routers/llmd_multistage_router.py): Stage 1 → Stage 5 orchestration routers with persistent per-replica concurrency semaphores, prefix-hash KV affinity, P/D admission control, Stage 4 Host DRAM tiering, and Stage 5 Mooncake + Managed Lustre KV hydration.
- [`manifests/dynamo-sglang-kimik3-2node-16gpu-rdma.yaml`](manifests/dynamo-sglang-kimik3-2node-16gpu-rdma.yaml) & [`manifests/llmd-vllm-kimik3-2node-16gpu-rdma.yaml`](manifests/llmd-vllm-kimik3-2node-16gpu-rdma.yaml): Multi-host GPUDirect RDMA (`8x 400 Gb/s` `rdma-0..7`) K8s manifests for 2-node 16-GPU `moonshotai/Kimi-K3` (`NVFP4`).
- [`results/glm53_llmd_vllm_stage1_to_5_summary.json`](results/glm53_llmd_vllm_stage1_to_5_summary.json) & [`.csv`](results/glm53_llmd_vllm_stage1_to_5_summary.csv): Full 5-stage benchmark metrics for `zai-org/GLM-5.3` (`llm-d + vLLM`, `16x B200`).
- [`results/glm53_dynamo_sglang_stage1_to_5_summary.json`](results/glm53_dynamo_sglang_stage1_to_5_summary.json) & [`.csv`](results/glm53_dynamo_sglang_stage1_to_5_summary.csv): Full 5-stage benchmark metrics for `zai-org/GLM-5.3` (`NVIDIA Dynamo + SGLang`, `16x B200`).
- [`results/llmd_vllm_stage1_to_5_summary.json`](results/llmd_vllm_stage1_to_5_summary.json) & [`.csv`](results/llmd_vllm_stage1_to_5_summary.csv): Full 5-stage benchmark metrics for `deepseek-ai/DeepSeek-V4.1-Flash` (`llm-d + vLLM`, `8x B200`).
- [`results/dynamo_sglang_stage1_to_5_summary.json`](results/dynamo_sglang_stage1_to_5_summary.json) & [`.csv`](results/dynamo_sglang_stage1_to_5_summary.csv): Full 5-stage benchmark metrics for `deepseek-ai/DeepSeek-V4.1-Flash` (`NVIDIA Dynamo + SGLang`, `8x B200`).
- [`results/kimik3_dynamo_sglang_stage1_to_5_summary.json`](results/kimik3_dynamo_sglang_stage1_to_5_summary.json) & [`.csv`](results/kimik3_dynamo_sglang_stage1_to_5_summary.csv): Full 5-stage benchmark metrics for `moonshotai/Kimi-K3` (`NVIDIA Dynamo + SGLang`, `16x B200`).
