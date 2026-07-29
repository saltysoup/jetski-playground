# MoonshotAI Kimi-K3 Distributed Serving on GKE (NVIDIA H100 GPUs over GPUDirect TCPXO)

This directory contains declarative, single-file Kubernetes manifests and benchmarking suites for serving **MoonshotAI Kimi-K3** (`moonshotai/Kimi-K3`) with **DSpark Speculative Decoding** (`RadixArk/Kimi-K3-DSpark`) across **4 Nodes (32 × NVIDIA H100 80GB SXM5 GPUs)** on Google Kubernetes Engine (GKE) A3 Mega (`a3-megagpu-8g`).

---

## Architectural & Performance Overview

* **Cluster Environment**: GKE A3 Mega (`a3-megagpu-8g`) node pool with 8 × NVIDIA H100 80GB SXM5 GPUs and 8 × 200 Gbps dedicated TCPXO NICs (`eth1..eth8`) per node.
* **GPUDirect TCPXO Networking**: Low-latency, zero-copy GPU-to-GPU communication across nodes using Google's FasTrak network plugin (`libnccl-net.so`).
* **Speculative Decoding**: Hybrid target/draft serving with MoonshotAI Kimi-K3 MoE target model and DSpark draft model (`speculative_dspark_block_size: 7`), achieving **~34% draft acceptance rate** and **~48.09 tokens/sec** per sequence.
* **Benchmark Verified Throughput**:
  * **32-GPU AllReduce Bus Bandwidth**: **`187.08 GB/s`** (Algorithm Bandwidth: `96.56 GB/s`)
  * **16-GPU AllReduce Bus Bandwidth**: **`260.57 GB/s`** (Algorithm Bandwidth: `138.97 GB/s`)
  * **16-GPU AllGather Bus Bandwidth**: **`157.35 GB/s`** (Algorithm Bandwidth: `167.84 GB/s`)

---

## Directory Contents

| File | Description |
| :--- | :--- |
| [`sglang-kimi3-h100.yaml`](sglang-kimi3-h100.yaml) | Production-ready, declarative 4-Node (32 × H100) SGLang Kimi-K3 serving manifest with DSpark speculative decoding. Includes all 25 Google-recommended GPUDirect TCPXO environment variables in the `env:` block with zero startup scripts. |
| [`nccl-test-kimi-k3.yaml`](nccl-test-kimi-k3.yaml) | Declarative 4-Node PyTorch Distributed NCCL AllReduce and AllGather benchmark manifest over GPUDirect TCPXO. Verified to run 20 iterations cleanly across 32 GPUs without OOM or network errors. |

---

## Critical GKE A3 Mega & GPUDirect TCPXO Configuration Notes

To achieve zero-error PyTorch Distributed collective communication and SGLang serving over GPUDirect TCPXO on GKE A3 Mega, the manifests implement four mandatory architectural configurations:

### 1. Open-Source NCCL Library Override (`LD_PRELOAD`)
PyTorch 2.11 (`torch-2.11.0+cu130`) bundles an internal copy of NCCL (`2.28.9`). However, Google's TCPXO network plugin (`libnccl-net.so` / FasTrak) is compiled against open-source **NCCL 2.28.7-1**. Without overriding the library, an ABI/struct mismatch between 2.28.9 core and 2.28.7-1 plugin corrupts memory handle registration flags (`mhandle`), causing `INVALID_ARGUMENT: Attempted to Send/Recv from host buffer`.
```yaml
- name: LD_PRELOAD
  value: "/usr/local/nvidia/lib64/libnccl.so.2"
```

### 2. Privileged Container Access (`resource0_wc`)
Google's FasTrak network plugin requires read/write access to PCIe device files (`/sys/bus/pci/devices/.../resource0_wc`) for write-combining RDMA memory mapping. Without `privileged: true`, Kubernetes mounts `/sys` as read-only inside the container, causing FasTrak to abort with `PERMISSION_DENIED: Read-only file system`.
```yaml
securityContext:
  privileged: true
  capabilities:
    add:
      - NET_ADMIN
      - NET_RAW
      - SYS_ADMIN
```

### 3. Pod-Native `/sys` Filesystem View (PCIe Topology Discovery)
Kubernetes multi-NIC VFIO interfaces (`eth1..eth8`) exist only inside the pod's isolated network namespace (`/sys/class/net`). **Do not mount the host machine's `/sys` directory onto the container's `/sys`**. When native `/sys` is preserved, NCCL discovers that `eth1..eth8` share a PCIe `PIX` switch with GPUs 0..7 and automatically enables **GPUDirect RDMA (`GDR=PIX`)**.

### 4. Canonical 25 Google GPUDirect TCPXO Environment Variables
All manifests explicitly declare the 25 recommended TCPXO variables in the `env:` block:
* `NCCL_ALGO=Ring,Tree`: Prevents NCCL from negotiating `NVLS`, `CollNet`, or `PAT` algorithms unsupported on TCPXO.
* `NCCL_NVLS_ENABLE=0`: Critically disables NVLink SHARP collective acceleration across ranks; leaving NVLS enabled on NCCL 2.28+ causes memory handle registration failures over TCPXO.
* `NCCL_NET_GDR_LEVEL=PIX`: Enforces GPUDirect RDMA over PCIe PIX switches.
* `NCCL_DYNAMIC_CHUNK_SIZE=524288`: Sets 512 KB dynamic chunking across TCPXO network queues.
* `PYTORCH_CUDA_ALLOC_CONF="expandable_segments:False"`: Prevents PyTorch's virtual memory caching allocator from returning segmented pointers that `cuPointerGetAttribute` does not flag as device memory.

---

## Deployment & Verification

### 1. Run the PyTorch NCCL TCPXO Benchmark
To verify multi-node collective bandwidth across all 32 H100 GPUs before serving:
```bash
kubectl apply -f nccl-test-kimi-k3.yaml
```
Inspect the benchmark output from pod 0:
```bash
kubectl logs -f nccl-test-kimi-k3-0 -c main-container
```

### 2. Deploy 4-Node SGLang Kimi-K3 Serving
To deploy the declarative 4-Node (TP32/EP32) SGLang Kimi-K3 serving cluster:
```bash
kubectl apply -f sglang-kimi3-h100.yaml
```
Monitor server initialization and checkpoint loading from GCS (`/bucket/Kimi-K3`):
```bash
kubectl logs -f distributed-sglang-k3-0 -c sglang-container
```
Once the server prints `The server is fired up and ready to roll!`, test completions via port forwarding:
```bash
kubectl port-forward svc/sglang-serving-k3 30000:30000
```
```bash
curl http://localhost:30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "moonshotai/Kimi-K3",
    "messages": [{"role": "user", "content": "Explain the significance of speculative decoding for MoE models."}],
    "max_tokens": 128,
    "temperature": 0.6
  }'
```
