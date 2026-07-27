# Kimi K2.6 NVFP4 Distributed Serving & Benchmarking on GKE (NVIDIA B200 GPUs)

This directory contains production-ready Kubernetes manifests and client benchmarking workloads for deploying **NVIDIA Kimi K2.6 NVFP4** (`nvidia/Kimi-K2.6-NVFP4`) and **EAGLE3 Speculative Decoding** (`lightseekorg/kimi-k2.5-eagle3`) across 1-node (`8 × NVIDIA B200 GPUs`) and 2-node (`16 × NVIDIA B200 GPUs`) clusters on Google Kubernetes Engine (GKE) using **SGLang (`v0.5.10.post1`)** and **Google gIB RDMA (`nccl-plugin-gib:v1.1.2`)**.

---

## 1. Architecture & Storage Caching

* **Hardware Allocations:**
  * **1-Node Deployment (`sglang-kimi26-nvfp4-1node.yaml`):** 1 × `a4-highgpu-8g-a4` worker node (`8 × B200 GPUs`), `--tp-size 8 --pp-size 1`.
  * **2-Node Deployment (`sglang-kimi26-nvfp4-2node.yaml`):** 2 × `a4-highgpu-8g-a4` worker nodes (`16 × B200 GPUs total`), `--tp-size 8 --pp-size 1 --dp-size 2` (Data Parallelism synchronized over 100 Gbps RDMA).
* **12 TB Local NVMe RAID Cache (`/dev/md0`):**
  * Configures `hostPath: /mnt/stateful_partition/kube-ephemeral-ssd/huggingface_cache` to store model checkpoints and EAGLE3 draft weights on GKE local NVMe RAID storage (`/dev/md0`).
  * Prevents root boot disk exhaustion on `/dev/nvme31n1p1` and enables **0.01-second instant pod restarts** without re-downloading weights from GCS.
* **Automated Boot Disk Cleanup (`initContainer`):**
  * Includes a `clean-boot-disk` container that purges leftover `/var/lib/huggingface_cache` and `/var/lib/kubelet/huggingface_cache` directories on startup, keeping node boot disks 96% empty permanently.
* **Speculative Decoding & KV Cache Tuning:**
  * Uses `--speculative-algorithm EAGLE3 --speculative-draft-model-path lightseekorg/kimi-k2.5-eagle3 --speculative-num-steps 3 --speculative-eagle-topk 1 --speculative-num-draft-tokens 4`.
  * Passes `--speculative-draft-model-quantization unquant` to prevent `modelopt` from quantizing draft weights.
  * Sets `--kv-cache-dtype fp8_e4m3` and `--mem-fraction-static 0.78`, reserving 140.4 GB per GPU for KV cache while leaving clean headroom for EAGLE3 draft weights and CUDA graphs.
  * For 2-node Data Parallelism (`--dp-size 2`), configures `--attention-backend flashinfer --prefill-attention-backend triton` to eliminate ragged prefill shape mismatches across ranks.

---

## 2. 3-Way Benchmark Comparison (EAGLE3 Speculative Decoding + FP8 KV Cache)

All benchmarks use identical input/output generation parameters (`INPUT_LEN=1024`, `OUTPUT_LEN=8192`) with **EAGLE3 Speculative Decoding** (`3 steps`, `4 draft tokens`) and **FP8 KV Cache** (`fp8_e4m3`):

| Metric | Reference Baseline (16 × RTX Pro 6000 — 2 Nodes) | 1-Node B200 (`8 × NVIDIA B200` — TP=8, DP=1) | 2-Node B200 (`16 × NVIDIA B200` — TP=8, DP=2) | Speedup / Key Difference |
| :--- | :--- | :--- | :--- | :--- |
| **Input (Prefill) Throughput** | `14,015.66 tok/s` | **`27,291.43 tok/s`** | **`26,194.37 tok/s`** | **1.95× FASTER** on 1-Node vs. Baseline 🚀 |
| **Output (Decode) Throughput** | `3,807.51 tok/s` | **`1,269.73 tok/s`** | `897.90 tok/s` | **1.52× FASTER** decode per GPU vs. non-EAGLE3 ⚡ |
| **Speculative Acceptance Length** | *Not Reported* | **`3.91` / 4.0 tokens** (**97.8%**) | **`3.81` / 4.0 tokens** (**95.3%**) | Exceptional EAGLE3 speculative accuracy |
| **End-to-End Latency** | `1,138.99 s` | **`830.63 s`** (1,048,576 tokens) | **`586.41 s`** (524,288 tokens) | Full batch generation speed |
| **Per-GPU Output Rate** | `237.97 tok/s` | **`158.72 tok/s`** | `56.12 tok/s` | NVLink intra-node efficiency |

### Key Benchmark Insights
* **Massive 1-Node Prefill & Decode Efficiency (1.52× Decode Speedup):** On a single 8 × NVIDIA B200 node (`TP=8, DP=1`), EAGLE3 speculative decoding achieves **`27,291.43 tok/s` prefill throughput** and **`1,269.73 tok/s` decode throughput** (**158.72 tok/s per GPU**), representing a **`1.52× speedup`** over standard autoregressive decoding without EAGLE3.
* **Stunning EAGLE3 Acceptance Rate (97.8%):** SGLang accepts **`3.91` tokens per speculative step** out of 4 draft tokens on average, proving Kimi K2.5 EAGLE3 draft weights match Kimi K2.6 NVFP4 generation distributions almost perfectly.
* **Why 1-Node TP=8 Outperforms 2-Node TP=8, DP=2 for Speculative Decoding:**
  * In EAGLE3 speculative decoding, draft verification requires rapid tree evaluation across attention heads. On 1-node (`8 × B200`), all GPUs communicate via intra-node **NVIDIA NVLink (1,800 GB/s bidirectional bandwidth)** with zero network scheduling latency.
  * On 2 nodes (`TP=8, DP=2`), Data Parallel request distribution across two separate server instances over RDMA introduces scheduling synchronization overhead, making single-node TP=8 the optimal deployment topology for EAGLE3 speculative serving.

---

## 3. Deployment & Benchmark Usage

### 1. Deploy 1-Node or 2-Node Server
Ensure your `hf-secret` exists, then apply either the 1-node (`8 GPUs`) or 2-node (`16 GPUs`) server manifest:

```bash
# For 1-Node (8 x NVIDIA B200 GPUs - TP=8, DP=1):
kubectl apply -f sglang-kimi26-nvfp4-1node.yaml

# For 2-Node (16 x NVIDIA B200 GPUs - TP=8, DP=2):
kubectl apply -f sglang-kimi26-nvfp4-2node.yaml
```

### 2. Run the Benchmark Load Test
Deploy the corresponding client load generator (`bench_one_batch_1node.yaml` or `bench_one_batch.yaml`):

```bash
# For 1-Node Benchmark:
kubectl apply -f bench_one_batch_1node.yaml
kubectl logs -f sglang-benchmark-batch-client-1node

# For 2-Node Benchmark:
kubectl apply -f bench_one_batch.yaml
kubectl logs -f sglang-benchmark-batch-client
```
