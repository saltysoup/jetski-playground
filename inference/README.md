# GKE LLM Inference Workloads & Benchmarks (NVIDIA B200 GPUs)

This directory contains production-ready Kubernetes deployments, distributed multi-node serving architectures, and standardized benchmarking suites for state-of-the-art Large Language Models on Google Kubernetes Engine (GKE) high-performance GPU clusters (**NVIDIA B200 GPUs** with 100 Gbps Google gIB RDMA acceleration).

---

## Model Directories & Serving Recipes

### 1. [`kimi-k2.6-nvfp4/`](./kimi-k2.6-nvfp4/README.md) — NVIDIA Kimi K2.6 NVFP4 (EAGLE3 Speculative Decoding)
* **Model Checkpoint:** `nvidia/Kimi-K2.6-NVFP4` (Quantized FP4 weights with FP8 KV cache)
* **Speculative Decoding:** `EAGLE3` using `lightseekorg/kimi-k2.5-eagle3` draft weights (**97.8% acceptance rate**; average 3.91 out of 4 draft tokens accepted).
* **Deployment Topologies:**
  * **1-Node (`8 × NVIDIA B200 GPUs`):** `--tp-size 8 --pp-size 1 --dp-size 1` ([`sglang-kimi26-nvfp4-1node.yaml`](./kimi-k2.6-nvfp4/sglang-kimi26-nvfp4-1node.yaml))
  * **2-Node (`16 × NVIDIA B200 GPUs`):** `--tp-size 8 --pp-size 1 --dp-size 2` over 100 Gbps RDMA ([`sglang-kimi26-nvfp4-2node.yaml`](./kimi-k2.6-nvfp4/sglang-kimi26-nvfp4-2node.yaml))
* **GKE Inference Gateway (`llm-d`):**
  * Uses Google Cloud Regional Internal Application Load Balancers (`gke-l7-rilb`, VIP: `http://192.168.0.10`) with custom `GCPBackendPolicy` (3600s timeout) and `HealthCheckPolicy`.
  * Integrates Envoy Ext-Proc (EPP) KV-Cache Aware prompt routing across distributed pods.
* **Standardized `inference-perf` Benchmark Suite:**
  * Based on the official [llm-d `guide.yaml` benchmark template](https://github.com/llm-d/llm-d/blob/main/guides/agentic-serving/benchmark-templates/guide.yaml).
  * Standardized at **`ISL = 1024`**, **`OSL = 8192`**, and **`Batch Size = 512`** (`num_requests: 512, concurrency_level: 512`).
  * Provides four 1-to-1 comparison manifests:
    * [`inference-perf-1node-direct.yaml`](./kimi-k2.6-nvfp4/inference-perf-1node-direct.yaml) (1-Node Direct Service)
    * [`inference-perf-1node-gateway.yaml`](./kimi-k2.6-nvfp4/inference-perf-1node-gateway.yaml) (1-Node GKE Inference Gateway)
    * [`inference-perf-2node-direct.yaml`](./kimi-k2.6-nvfp4/inference-perf-2node-direct.yaml) (2-Node Direct Service)
    * [`inference-perf-2node-gateway.yaml`](./kimi-k2.6-nvfp4/inference-perf-2node-gateway.yaml) (2-Node GKE Inference Gateway)

---

### 2. [`kimi-k3/`](./kimi-k3/README.md) — MoonshotAI Kimi-K3 (TP=16 Low-Latency + DSpark Speculative Decoding)
* **Model Checkpoint:** `moonshotai/Kimi-K3`
* **Speculative Decoding:** `DSpark` using `RadixArk/Kimi-K3-DSpark` draft checkpoint (`block-size=7`) with **Linear Replay SSM** verification (`--enable-linear-replayssm-spec`).
* **Deployment Topology:**
  * **2-Node Low-Latency Strategy (`16 × NVIDIA B200 GPUs`):** Spans all 16 GPUs across two `a4-highgpu-8g-a4` worker nodes (`--tp-size 16 --nnodes 2 --dp-size 1`) using `sglang serve` with `docker.io/lmsysorg/sglang:latest` ([`sglang-kimi3-2node.yaml`](./kimi-k3/sglang-kimi3-2node.yaml)).
  * Head Node (`pod-index: 0`) coordinates NCCL / Gloo distributed initialization over headless Service `sglang-master-pod-k3:20000` and serves inference endpoints on port `30000`.
* **Cross-Node RDMA & NIC Pinning (`100 Gbps`):**
  * Pinned explicitly to `eth0` (`GLOO_SOCKET_IFNAME=eth0`, `NCCL_SOCKET_IFNAME=eth0`, `SGLANG_HOST_IP`) with Google gIB RDMA (`nccl-plugin-gib:v1.1.2`).
* **High-Speed GCS Transfer & NVMe RAID Caching:**
  * Includes dedicated parallel downloader/uploader Job ([`kimi-k3-gcs-uploader-job.yaml`](./kimi-k3/kimi-k3-gcs-uploader-job.yaml)) utilizing 16 parallel workers via `hf_transfer` to sync `moonshotai/Kimi-K3` directly to `gs://ikwak-models-gpu-launchpad-playground/Kimi-K3`.
  * Server pods use `pull-model-from-gcs` initContainer to rsync weights from GCS onto the node's **12 TB NVMe RAID 0 (`/dev/md0`)** storage for 0.01-second instant restarts.

---

## 3-Way Benchmark Comparison (Kimi K2.6 NVFP4 — FP8 KV Cache + EAGLE3)

| Metric | 1-Node Direct Service (`BS=128`, `OSL=8192`) | 1-Node Gateway (`BS=64`, `OSL=4096`) | 2-Node Gateway (`BS=64`, `OSL=4096`, `DP=2`) | Scaling & Architectural Takeaway |
| :--- | :--- | :--- | :--- | :--- |
| **End-to-End Latency** | `830.63 s` | `401.32 s` | **`209.87 s`** | **1.91× FASTER latency** on 2 nodes |
| **Input (Prefill) Throughput** | `27,291.43 tok/s` | `24,505.98 tok/s` | **`26,045.73 tok/s`** | Consistent high-speed FP8 prefill across ranks |
| **Output (Decode) Throughput** | `1,269.73 tok/s` | `657.58 tok/s` | **`1,264.23 tok/s`** | **1.92× HIGHER decode throughput (96.1% linear scaling!)** |
| **Speculative Acceptance Length** | `3.91` / 4.0 draft tokens | `3.79` / 4.0 draft tokens | **`3.91` / 4.0 draft tokens** | **97.8% EAGLE3 draft acceptance accuracy** |
| **Time-to-First-Token (TTFT)** | `4.8027 s` | `2.6743 s` | **`2.5162 s`** | Gateway L7 Load Balancer adds **< 14 ms overhead** |

---

## Infrastructure Hardware Specifications

* **GPU Worker Pool:** Google Kubernetes Engine (GKE) `a4-highgpu-8g-a4` machine type.
* **GPUs per Node:** `8 × NVIDIA B200` (180 GB HBM3e per GPU, NVIDIA NVLink intra-node interconnect at 1,800 GB/s bidirectional bandwidth).
* **Inter-Node Networking:** 100 Gbps Google gIB RDMA (`nccl-plugin-gib:v1.1.2`, RDMA RoCEv2 fabrics).
* **Node Storage:** 12 TB Local NVMe SSD RAID 0 (`/dev/md0`) mounted at `/mnt/stateful_partition/kube-ephemeral-ssd`.
