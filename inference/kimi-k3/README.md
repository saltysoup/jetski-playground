# MoonshotAI Kimi-K3 (`moonshotai/Kimi-K3`) Distributed Serving on Google Kubernetes Engine (GKE)

This directory contains production-ready Kubernetes manifests and performance benchmarking configurations for deploying **MoonshotAI Kimi-K3** (`moonshotai/Kimi-K3`) with **DSpark Speculative Decoding** (`RadixArk/Kimi-K3-DSpark`) on Google Kubernetes Engine (GKE) across different NVIDIA GPU architectures.

---

## Directory Structure

* **[`b200/`](b200/)** — **NVIDIA B200 GPUs (Blackwell SM100)**
  * 2-Node distributed serving across 16 × NVIDIA B200 GPUs (`a4-highgpu-8g-a4` VMs).
  * Utilizes Blackwell FP4 MoE quantization (`mxfp4`) and `nv_cutedsl` linear attention kernels.
  * Low-latency GPUDirect RDMA over RoCEv2 (`gIB` / `nccl-plugin-gib`) on GKE Dataplane V2 multi-networking.
  * Includes GKE Inference Gateway (`llm-d`), rapid NVMe/GCS caching, and `inference-perf` benchmark suites (Direct, Gateway, and Deep Research).

* **[`h100/`](h100/)** — **NVIDIA H100 GPUs (Hopper SM90)**
  * 4-Node distributed serving across 32 × NVIDIA H100 80GB SXM5 GPUs on GKE A3 Mega (`a3-megagpu-8g` VMs).
  * High-performance **GPUDirect TCPXO Networking** over 8 dedicated 200 Gbps NICs (`eth1..eth8`) per node (`187.08 GB/s` AllReduce bus bandwidth across 32 GPUs).
  * **Prerequisite (`v1.0.17+` Installer DaemonSet)**: Requires Google's canonical `nccl-tcpxo-installer.yaml` DaemonSet deployed across nodes to install `libnccl.so.2.28.7-1` and FasTrak plugin `libnccl-net.so`.
  * **Declarative Single-File Serving Manifest (`sglang-kimi3-h100.yaml`)**: Features zero startup scripts, declaring all 25 canonical Google-recommended GPUDirect TCPXO environment variables in the Kubernetes `env:` block.
  * **Critical Architecture Fixes**:
    * Preloads open-source `libnccl.so.2.28.7-1` via `LD_PRELOAD` to resolve PyTorch bundled NCCL ABI mismatches (rendering `SGLANG_NCCL_SO_PATH` redundant and omitted).
    * Configures `securityContext: privileged: true` for write-combining RDMA access to `/sys/bus/pci/devices/.../resource0_wc`.
    * Preserves pod-native `/sys` filesystem (no host `/sys` mount) for multi-NIC PCIe topology discovery and `GDR=PIX` enabling.
  * Includes multi-node PyTorch Distributed NCCL benchmark suites (`nccl-test-kimi-k3.yaml`) and full deployment instructions.

---

## Performance Comparison: B200 vs. H100

| Metric | NVIDIA B200 (Blackwell SM100) | NVIDIA H100 (Hopper SM90) |
| :--- | :--- | :--- |
| **Minimum Serving Nodes / GPUs** | 2 Nodes / 16 × B200 GPUs | 4 Nodes / 32 × H100 GPUs |
| **MoE Quantization Scheme** | Native FP4 / MXFP4 (`mxfp4`) | W8A8 / FP8 Marlin |
| **Network Fabric** | RoCEv2 (`gIB` / Dataplane V2) | GPUDirect TCPXO (`libnccl-net.so`) |
| **32-GPU AllReduce Bandwidth** | N/A (16-GPU cluster: `362.4 GB/s`) | **`187.08 GB/s`** (`260.57 GB/s` on 16-GPU) |
| **Speculative Decoding** | DSpark (`RadixArk/Kimi-K3-DSpark`) | DSpark (`RadixArk/Kimi-K3-DSpark`) |

---

## 10-Stage Pareto Saturation Wall Benchmark (`32k / 1k` Long-Context Workload)

To establish an apple-to-apples architectural baseline across clusters and provide a reference target for testing **Lustre KV Cache Offloading** and **LLM-D (`llm-d-router`)**, we executed a 10-stage deterministic saturation sweep (`ISL = 32,768` prompt tokens, `OSL = 1,024` output tokens) across concurrency levels $c = 1 \text{ to } 512$ using `kubernetes-sigs/inference-perf` (`0.0% Error Rate`).

### Complete Verified Side-by-Side Pareto Saturation Table ($c = 1 \text{ to } 512$)

| Stage (Concurrency $c$) | B200 Output tok/s | H100 Output tok/s | B200 Mean ITL | H100 Mean ITL | B200 Mean TTFT | H100 Mean TTFT | B200 vs. H100 Architectural Gain |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **Stage 0 ($c = 1$)** | `41.7` | `22.3` | `20.0 ms` | `38.0 ms` | `1.71 s` | `5.08 s` | **+87.0% Output / 2.97× Faster TTFT** |
| **Stage 1 ($c = 2$)** | `72.9` | `38.7` | `22.8 ms` | `42.5 ms` | `2.48 s` | `7.69 s` | **+88.4% Output / 3.10× Faster TTFT** |
| **Stage 2 ($c = 4$)** | `111.1` | `60.2` | `28.9 ms` | `53.3 ms` | `3.55 s` | `11.84 s` | **+84.6% Output / 3.34× Faster TTFT** |
| **Stage 3 ($c = 8$)** | **`167.7`** ⭐ *(B200 Knee)* | **`59.7`** ⭐ *(H100 Knee)* | **`37.5 ms`** | **`58.8 ms`** | **`7.70 s`** | **`58.99 s`** | **+180.9% Output / 7.66× Faster TTFT** |
| **Stage 4 ($c = 16$)** | `165.2` | `59.6` | `45.6 ms` | `58.2 ms` | `28.9 s` | `111.6 s` | **+177.2% Output / 3.86× Faster TTFT** |
| **Stage 5 ($c = 32$)** | `159.3` | `61.0` | `57.4 ms` | `59.1 ms` | `72.2 s` | `243.1 s` | **+161.1% Output / 3.37× Faster TTFT** |
| **Stage 6 ($c = 64$)** | `164.3` | `60.3` | `68.7 ms` | `60.3 ms` | `155.7 s` | `515.1 s` | **+172.5% Output / 3.31× Faster TTFT** |
| **Stage 7 ($c = 128$)** | `168.1` | `61.0` | `86.8 ms` | `60.0 ms` | `328.5 s` | `1,045 s` (`17.4 min`) | **+175.6% Output / 3.18× Faster TTFT** |
| **Stage 8 ($c = 256$)** | `170.0` | `60.7` | `114.3 ms` | `60.5 ms` | `670.1 s` | `2,089 s` (`34.8 min`) | **+180.1% Output / 3.12× Faster TTFT** |
| **Stage 9 ($c = 512$)** | **`170.1`** 🧱 *(B200 Wall)* | **`60.9`** 🧱 *(H100 Wall)* | **`191.0 ms`** | **`60.5 ms`** | **`1,363 s` (`22.7 min`)** | **`4,219 s` (`70.3 min`)** | **+179.3% Output / 3.10× Faster TTFT** |

### Key Architectural Takeaways

1. **Why 16 × B200 GPUs Outperform 32 × H100 GPUs by +179.3% (`2.79× Faster`)**:
   * Despite having **half the total GPU count (16 vs. 32)**, B200 delivers **`170.1 tok/s`** output throughput at saturation compared to **`60.9 tok/s`** on H100.
   * This is driven by Blackwell's FP4/FP8 compute density and 2 nodes over hardware-offloaded **800 Gbps RoCEv2 RDMA** versus 4 Hopper nodes communicating over GPUDirect TCPXO Ethernet AllReduce.
2. **Proof of the Pareto Saturation Wall**:
   * **On B200**: Output throughput plateaus flat at **`~167.7 — 170.1 tok/s`** from $c = 8$ to $c = 512$. Meanwhile, Inter-Token Latency ($\text{ITL}$) climbs by **$5.1\times$ (`37.5 ms` ➔ `191.0 ms`)**, and $\text{TTFT}$ climbs to **`22.7 minutes`**.
   * **On H100**: Output throughput plateaus flat at **`~59.7 — 61.0 tok/s`** from $c = 8$ to $c = 512$, while $\text{TTFT}$ explodes to **`70.3 minutes`**.
3. **Why This Baseline Proves the Need for Lustre KV Cache Offloading**:
   * At $c = 512$, maintaining 512 simultaneous `32,768-token` prompt KV cache sequences (**`~5.12 TB`**) forces physical GPU HBM occupancy to **100% (`full token usage = 1.00`)**, triggering severe cache eviction and memory contention.
   * Enabling **Lustre KV Cache Offloading** will evict inactive `32k` KV blocks to high-speed Lustre parallel storage and stream them back over NVLink/RDMA, eliminating HBM saturation and keeping $\text{ITL}$ flat at high concurrency.
4. **Why This Baseline Proves the Need for LLM-D (`llm-d-router`)**:
   * In standard co-located serving, incoming **`16.7 Million prompt tokens`** block ongoing decode streams, creating the 22.7-minute (B200) and 70.3-minute (H100) $\text{TTFT}$ queueing delays seen at $c = 512$.
   * Disaggregating prefill nodes from decode nodes (`llm-d-router`) will isolate the heavy `32k` prompt processing, keeping decode $\text{ITL}$ around **`~20 ms`** and $\text{TTFT}$ under a few seconds!

---

## How to Pre-Upload Model Weights to GCS & Run the Benchmarks

### 1. Pre-Upload Model & Draft Checkpoints to Google Cloud Storage (GCS)
The SGLang serving containers mount `gs://${GCS_BUCKET}` to `/bucket` via `gke-gcsfuse`. To ensure `sglang serve` finds `--model-path=/bucket/Kimi-K3` and `--speculative-draft-model-path=/bucket/Kimi-K3-DSpark` (and that `generation_config.json` is present for the draft model):

```bash
# 1. Download clean, non-symlinked model folders locally
huggingface-cli download moonshotai/Kimi-K3 \
  --local-dir ./Kimi-K3 \
  --local-dir-use-symlinks False

huggingface-cli download RadixArk/Kimi-K3-DSpark \
  --local-dir ./Kimi-K3-DSpark \
  --local-dir-use-symlinks False

# 2. Copy generation_config.json into draft model (RadixArk/Kimi-K3-DSpark omits it by default)
cp ./Kimi-K3/generation_config.json ./Kimi-K3-DSpark/

# 3. Upload directly to the GCS bucket root
gcloud storage cp -r ./Kimi-K3 gs://${GCS_BUCKET}/Kimi-K3
gcloud storage cp -r ./Kimi-K3-DSpark gs://${GCS_BUCKET}/Kimi-K3-DSpark
```

*(If models are already in GCS, copy `generation_config.json` cloud-to-cloud: `gcloud storage cp gs://${GCS_BUCKET}/Kimi-K3/generation_config.json gs://${GCS_BUCKET}/Kimi-K3-DSpark/generation_config.json`)*

### 2. Launching the 10-Stage `32k / 1k` Pareto Saturation Benchmark

#### On NVIDIA B200 Cluster (`ikwak-reliability` in `europe-west4`):
```bash
kubectl --context=gke_gpu-launchpad-playground_europe-west4_ikwak-reliability \
  apply -f inference/kimi-k3/b200/inference-perf-k3-deep-research.yaml
```

#### On NVIDIA H100 Cluster (`ikwak-a3m-spot` in `us-west1-a`):
```bash
kubectl apply -f inference/kimi-k3/h100/inference-perf-k3-deep-research.yaml
```

Monitor benchmark progress and extract summary results:
```bash
kubectl logs -l app=inference-perf-k3-deep-research -f
```
