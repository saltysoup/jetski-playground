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

## 2. Reference Baseline (Google Cloud G4 VMs — 16 × RTX Pro 6000 GPUs)

The reference performance baseline to compare against was evaluated across **2 × G4 VMs (8 × RTX Pro 6000 GPUs per node, 16 GPUs total)** using `BATCH_SIZE=512`, `INPUT_LEN=1024`, `OUTPUT_LEN=8192`:

| Metric | Reference Baseline (16 × RTX Pro 6000) | GKE B200 (`2n8g` — 16 × B200 GPUs) |
| :--- | :--- | :--- |
| **Output (Decode) Throughput** | `3,807.51 tok/s` | *Pending Benchmark* |
| **Input (Prefill) Throughput** | `14,015.66 tok/s` | *Pending Benchmark* |
| **Overall Token Throughput** | `4,142.77 tok/s` | *Pending Benchmark* |
| **Average Generation Speed** | `487.57 tok/s per rank` | *Pending Benchmark* |
| **End-to-End Batch Latency** | `1,138.99 s` (512 × 8192 tok) | *Pending Benchmark* |

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
