# Kimi K2.6 NVFP4 Distributed Serving & Benchmarking on GKE (16 × NVIDIA B200 GPUs)

This directory contains production-ready Kubernetes manifests and client benchmarking workloads for deploying **NVIDIA Kimi K2.6 NVFP4** (`nvidia/Kimi-K2.6-NVFP4`) across 2 × A4 HighGPU nodes (16 × NVIDIA B200 GPUs total) on Google Kubernetes Engine (GKE) using **SGLang (`v0.5.10.post1`)** and **Google gIB RDMA (`nccl-plugin-gib:v1.1.2`)**.

---

## 1. Architecture & Networking

* **Hardware Allocation:** 2 × `a4-highgpu-8g-a4` worker nodes (`replicas: 2`, 16 × B200 GPUs total).
* **Distributed Parallelism:** 8-way Tensor Parallelism (`--tp-size 8`) per node + 2-way Pipeline Parallelism (`--pp-size 2`) across nodes + Data Parallelism (`--dp-size 8`, `--enable-dp-attention`).
* **Google gIB RDMA Acceleration:** 
  * Utilizes `us-docker.pkg.dev/gce-ai-infra/gpudirect-gib/nccl-plugin-gib:v1.1.2` init container to provide NCCL ABI support for CUDA 12.9 + NCCL 2.28.3.
  * Annotates Pods with 8 InfiniBand RDMA interfaces (`eth2` through `eth9`).
  * Executes `source /usr/local/gib/scripts/set_nccl_env.sh` prior to server launch (`NCCL_NET=gIB`, `NCCL_IB_GID_INDEX=3`, `/usr/local/gib/configs/tuner_config_a4.txtpb`).

---

## 2. Benchmark Comparison (16 × RTX Pro 6000 vs. 16 × NVIDIA B200 GPUs)

The reference performance baseline was evaluated across **2 × G4 VMs (8 × RTX Pro 6000 GPUs per node, 16 GPUs total)** and compared against our **GKE 2 × A4 HighGPU nodes (8 × NVIDIA B200 GPUs per node, 16 GPUs total)** using identical parameters (`BATCH_SIZE=512`, `INPUT_LEN=1024`, `OUTPUT_LEN=8192`):

| Metric | Reference Baseline (16 × RTX Pro 6000) | GKE B200 (`a4-highgpu-8g` — 16 × B200 GPUs) | Speedup / Difference |
| :--- | :--- | :--- | :--- |
| **Input (Prefill) Throughput** | `14,015.66 tok/s` | **`72,277.98 tok/s`** | **5.16× FASTER** 🚀 |
| **Average Generation Speed** | `487.57 tok/s per rank` | **`837.52 tok/s per rank`** | **1.72× FASTER** ⚡ |
| **Time to First Token (TTFT)** | *Not Reported* | **`7.25 s`** (524,288 input tokens) | *Instant Prefill* |
| **Output (Decode) Throughput** | `3,807.51 tok/s` | `3,232.75 tok/s` | `0.85×` |
| **Overall Token Throughput** | `4,142.77 tok/s` | `3,616.62 tok/s` | `0.87×` |
| **End-to-End Batch Latency** | `1,138.99 s` | `1,304.70 s` | 512 × 8192 tokens generated |

### Key Benchmark Insights
* **Massive Prefill Acceleration (5.16× Speedup):** NVIDIA B200 nodes combined with 100 Gbps Google gIB RDMA interfaces (`eth2`–`eth9`) achieve an extraordinary **`72,277.98 tok/s` prefill throughput**, processing 524,288 prompt tokens in just **7.25 seconds**.
* **High Per-Rank Generation Speed (1.72× Faster):** Each Data-Parallel rank generates at **`837.52 tok/s`**, vastly outperforming the RTX Pro 6000 baseline (`487.57 tok/s per rank`).

---

## 3. Deployment & Usage

### 1. Deploy SGLang Distributed Server
Ensure a Kubernetes Secret named `hf-secret` exists containing your Hugging Face token (`HF_TOKEN` key), then apply the StatefulSet:

```bash
kubectl apply -f sglang-kimi26-nvfp4-2node.yaml
```

### 2. Run the Client Benchmark
The client load generator runs on the GKE `system` node pool (`bench_one_batch.yaml`) without requiring GPU resources:

```bash
kubectl apply -f bench_one_batch.yaml
kubectl logs -f sglang-benchmark-batch-client
```
