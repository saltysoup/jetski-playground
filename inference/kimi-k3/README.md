# Kimi-K3 (moonshotai/Kimi-K3) 2-Node Low-Latency Distributed Serving on GKE (NVIDIA B200 GPUs)

This directory contains production-ready Kubernetes manifests for deploying **MoonshotAI Kimi-K3** (`moonshotai/Kimi-K3`) with **DSpark Speculative Decoding** (`RadixArk/Kimi-K3-DSpark`) across 2 nodes (**16 × NVIDIA B200 GPUs**) on Google Kubernetes Engine (GKE) using **SGLang (`docker.io/lmsysorg/sglang:kimi-k3`)** and **Google gIB RDMA (`nccl-plugin-gib:v1.1.2`)** over 100 Gbps networking.

---

## 1. Architecture & Storage Caching

* **Low-Latency Strategy (`TP=16, PP=1, DP=1`):**
  * Spans 16 × NVIDIA B200 GPUs across two `a4-highgpu-8g-a4` worker nodes (`--tp-size 16 --nnodes 2`).
  * Head Node (`pod-index: 0`) coordinates distributed initialization over headless Service `sglang-master-pod-k3:20000` and exposes the API serving endpoint on port `30000`.
* **DSpark Speculative Decoding (`RadixArk/Kimi-K3-DSpark`):**
  * Uses `--speculative-algorithm DSPARK --speculative-draft-model-path /root/.cache/huggingface/Kimi-K3-DSpark --speculative-dspark-block-size 7`.
  * Enables Linear Replay SSM verification via `--enable-linear-replayssm-spec` for high-accuracy state space model draft validation.
* **Cross-Node RDMA & NIC Pinning (`eth0`):**
  * Configures `hostNetwork: true` and `dnsPolicy: ClusterFirstWithHostNet`.
  * Pins bootstrap and NCCL interfaces explicitly to avoid virtual bridge conflicts:
    ```bash
    export GLOO_SOCKET_IFNAME=eth0
    export NCCL_SOCKET_IFNAME=eth0
    export SGLANG_HOST_IP=$(hostname -i | awk '{print $1}')
    ```
* **12 TB NVMe RAID 0 Caching (`/dev/md0`):**
  * Uses `hostPath: /mnt/stateful_partition/kube-ephemeral-ssd/huggingface_cache` mounted to `/root/.cache/huggingface`.
  * The init-container `pull-model-from-gcs` executes `gcloud storage rsync` from `gs://ikwak-models-gpu-launchpad-playground/Kimi-K3` and downloads `RadixArk/Kimi-K3-DSpark`, caching both checkpoints locally on disk for 0.01-second pod restarts.

---

## 2. File Overview

* **[`sglang-kimi3-2node.yaml`](sglang-kimi3-2node.yaml):**
  * Full multi-node deployment including:
    * `Service: sglang-master-pod-k3` (Headless clusterIP: None, port `20000`)
    * `Service: sglang-serving-k3` (ClusterIP, port `30000`, selecting `pod-index: 0`)
    * `StatefulSet: distributed-sglang-k3` (`replicas: 2`, `TP=16`)
* **[`kimi-k3-gcs-uploader-job.yaml`](kimi-k3-gcs-uploader-job.yaml):**
  * High-speed parallel downloader and GCS uploader Job.
  * Downloads `moonshotai/Kimi-K3` using 16 parallel workers via `hf_transfer` and streams to `gs://ikwak-models-gpu-launchpad-playground/Kimi-K3`.

---

## 3. Deployment & Usage

### 1. Download Model to GCS (If Not Already Cached)
```bash
kubectl apply -f kimi-k3-gcs-uploader-job.yaml
kubectl logs -f job/kimi-k3-gcs-uploader -c download-hf
```

### 2. Deploy 2-Node Low-Latency Server (`16 × NVIDIA B200`)
```bash
kubectl apply -f sglang-kimi3-2node.yaml
```

### 3. Monitor Initialization & Serving
```bash
# Check pod scheduling and readiness:
kubectl get pods -l app=distributed-sglang-k3 -o wide

# Monitor GCS rsync and DSpark download on Head Node (Rank 0):
kubectl logs -f distributed-sglang-k3-0 -c pull-model-from-gcs

# Follow SGLang multi-node serving logs on Head Node (Rank 0):
kubectl logs -f distributed-sglang-k3-0 -c sglang-container

# Follow worker logs on Slave Node (Rank 1):
kubectl logs -f distributed-sglang-k3-1 -c sglang-container

# Test API health once serving starts on Port 30000:
kubectl exec -it distributed-sglang-k3-0 -c sglang-container -- curl -s http://localhost:30000/get_model_info

# Test generation with Kimi-K3 reasoning & DSpark speculative decoding:
kubectl exec -i distributed-sglang-k3-0 -c sglang-container -- \
  curl -s -X POST http://localhost:30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "moonshotai/Kimi-K3",
    "messages": [
      {"role": "user", "content": "What is Google Cloud AI Hypercomputer in 2 sentences."}
    ],
    "max_tokens": 256,
    "temperature": 0.7
  }' | jq .
```
