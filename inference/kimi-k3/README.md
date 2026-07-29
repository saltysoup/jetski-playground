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
  * **Declarative Single-File Serving Manifest (`sglang-kimi3-h100.yaml`)**: Features zero startup scripts, declaring all 25 canonical Google-recommended GPUDirect TCPXO environment variables in the Kubernetes `env:` block.
  * **Critical Architecture Fixes**:
    * Preloads open-source `libnccl.so.2.28.7-1` via `LD_PRELOAD` to resolve PyTorch bundled NCCL ABI mismatches with Google's FasTrak network plugin (`libnccl-net.so`).
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
