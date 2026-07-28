# MoonshotAI Kimi-K3 (moonshotai/Kimi-K3) Distributed Serving on Google Kubernetes Engine (GKE)

This directory contains production-ready Kubernetes manifests and performance benchmarking configurations for deploying **MoonshotAI Kimi-K3** (`moonshotai/Kimi-K3`) with **DSpark Speculative Decoding** (`RadixArk/Kimi-K3-DSpark`) on Google Kubernetes Engine (GKE) across different NVIDIA GPU architectures.

---

## Directory Structure

* **[`b200/`](b200/)** — **NVIDIA B200 GPUs (Blackwell SM100)**
  * 2-Node distributed serving across 16 × NVIDIA B200 GPUs (`a4-highgpu-8g-a4` VMs).
  * Utilizes Blackwell FP4 MoE quantization (`mxfp4`) and `nv_cutedsl` linear attention kernels.
  * Low-latency GPUDirect RDMA over RoCEv2 (`gIB` / `nccl-plugin-gib`) on GKE Dataplane V2 multi-networking.
  * Includes GKE Inference Gateway (`llm-d`), rapid NVMe/GCS caching, and `inference-perf` benchmark suites (Direct, Gateway, and Deep Research).

* **[`h100/`](h100/)** — **NVIDIA H100 GPUs (Hopper SM90)**
  * Multi-node distributed serving across NVIDIA H100 80GB GPUs (`a3-highgpu-8g` / `a3-megagpu-8g` VMs).
  * Utilizes Hopper-optimized FlashInfer and Triton attention/MoE kernels.
