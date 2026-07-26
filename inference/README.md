# Inference Workloads & Recipes

This directory is dedicated to high-throughput LLM serving configurations, inference engine recipes (SGLang, vLLM, TensorRT-LLM), and latency/throughput benchmarking across Google Kubernetes Engine (GKE) high-GPU clusters.

---

## Directory Overview

* **[`kimi-k2.6-nvfp4/`](./kimi-k2.6-nvfp4/README.md):** Distributed 2-node SGLang serving and client benchmarking (`bench_one_batch.yaml`) for **NVIDIA Kimi K2.6 NVFP4** (`nvidia/Kimi-K2.6-NVFP4`) across **16 × NVIDIA B200 GPUs** (`2n8g` pool) using Google gIB RDMA acceleration (`nccl-plugin-gib:v1.1.2`).
