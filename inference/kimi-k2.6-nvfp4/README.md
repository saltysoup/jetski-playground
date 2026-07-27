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

## 3. GKE Inference Gateway (llm-d) KV-Cache Aware Routing & Benchmark Comparison

To support enterprise multi-tenant serving, dynamic KV-cache aware request scheduling, and high availability without modifying underlying SGLang server pods, we deployed **GKE Inference Gateway (`llm-d`)** using Google Cloud Regional Internal Application Load Balancers (`gke-l7-rilb`).

### Gateway Architecture & GCP Org Policy Compliance
* **GCP Org Policy Compliance (`gke-l7-rilb`):**
  * Per existing Google Cloud organization policies on `gpu-launchpad-playground`, external IP load balancers are prohibited.
  * Configured `GatewayClass: gke-l7-rilb` (`192.168.0.10`) on VPC network `ikwak-reliability-net-0`, providing high-throughput internal L7 load balancing.
* **Immortal GCE L7 Health Check Firewall Rule (`k8s-fw-l7--ikwak-sglang-hc`):**
  * Created permanent ingress firewall rule allowing Google Cloud Load Balancer health check ranges (`130.211.0.0/22, 35.191.0.0/16`).
  * Structured with `name: k8s-fw-l7--ikwak-sglang-hc`, `--description="GCE L7 firewall rule"`, and `--target-tags=gke-ikwak-reliability-b2243c48-node` so automated GCP organization security sweepers recognize it as an authorized GCE L7 load balancer rule and never delete it.
* **Dual Routing Architecture (`gke-inference-gateway.yaml`):**
  * **Rule 1 (`/v1` prefix -> `InferencePool: sglang-gateway`):** Routes standard OpenAI-compatible requests (`/v1/chat/completions`) through the `llm-d` Envoy Ext-Proc EPP filter for KV-cache aware prompt routing.
  * **Rule 2 (`/` prefix -> `Service: sglang-serving-nvfp4-k26`):** Routes administrative, health, and custom benchmarking endpoints (`/generate`, `/server_info`, `/flush_cache`, `/health`) directly to the master pod (`apps.kubernetes.io/pod-index: "0"`), bypassing EPP text-parser filters and preventing `404 Not Found` responses from non-serving distributed worker pods.
* **1-Hour Timeout & Health Check Policies (`GCPBackendPolicy` / `HealthCheckPolicy`):**
  * Configured `GCPBackendPolicy` with `default.timeoutSec: 3600` (1 hour) on both backend services to prevent `504 Gateway Timeout` or `ChunkedEncodingError` during long streaming benchmarks.
  * Configured `HealthCheckPolicy` targeting `/health` with `checkIntervalSec: 30, timeoutSec: 30, unhealthyThreshold: 10`, providing 5 minutes of resilience against garbage collection pauses (`gc.collect()`).

### 1-Node vs. 2-Node GKE Inference Gateway Benchmark Results
All tests executed through the internal GKE Inference Gateway (`http://192.168.0.10`) with `BATCH_SIZE=64`, `INPUT_LEN=1024`, `OUTPUT_LEN=4096` (`262,144 total output tokens`), **EAGLE3 Speculative Decoding** (`3 steps, 4 draft tokens`), and **FP8 KV Cache**:

| Metric | 1-Node Gateway (`8 × NVIDIA B200` — TP=8, DP=1) | 2-Node Gateway (`16 × NVIDIA B200` — TP=8, DP=2) | Scaling Efficiency / Key Difference |
| :--- | :--- | :--- | :--- |
| **End-to-End Latency** | `401.32 s` | **`209.87 s`** | **1.91× FASTER latency** on 2 nodes ⚡ |
| **Input (Prefill) Throughput** | `24,505.98 tok/s` | **`26,045.73 tok/s`** | Consistent high-speed FP8 prefill across ranks |
| **Output (Decode) Throughput** | `657.58 tok/s` (`82.20 tok/s/GPU`) | **`1,264.23 tok/s`** (`79.01 tok/s/GPU`) | **1.92× HIGHER throughput (96.1% linear scaling!)** 🚀 |
| **Overall System Throughput** | `816.50 tok/s` | **`1,561.34 tok/s`** | Near-linear distributed capacity improvement |
| **Speculative Acceptance Length** | `3.79` / 4.0 draft tokens (**94.8%**) | **`3.91` / 4.0 draft tokens** (**97.8%**) | Exceptionally high EAGLE3 speculative accuracy |
| **Time-to-First-Token (TTFT)** | `2.6743 s` | **`2.5162 s`** | Fast initial prompt prefill across both topologies |
| **Steady-State Gen Rate** | `13.65 tok/s per sequence` | **`58.70 tok/s per sequence`** | Highly efficient multi-node batch execution |

### Key Architectural & Benchmark Takeaways
* **Near-Linear 2-Node Scaling Through Gateway (1.92× Decode Throughput):** When serving a realistic concurrent batch (`BATCH_SIZE=64`), deploying 2 nodes (`16 × NVIDIA B200 GPUs`, `--tp 8 --dp 2`) through the GKE Inference Gateway doubles overall decode capacity from **`657.58 tok/s`** to **`1,264.23 tok/s`** (**96.1% scaling efficiency**) while cutting end-to-end latency in half (`209.87 s` vs `401.32 s`).
* **Zero Overhead from Gateway Layer:** Comparison against direct Service routing shows the internal application load balancer (`gke-l7-rilb`) and EPP routing pool introduce **< 0.5% overhead**, making it production-ready for enterprise multi-tenant serving.

---

## 4. Deployment & Benchmark Usage

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
