# Kimi-K3 (moonshotai/Kimi-K3) 2-Node Low-Latency Distributed Serving on GKE (NVIDIA B200 GPUs)

This directory contains production-ready Kubernetes manifests for deploying **MoonshotAI Kimi-K3** (`moonshotai/Kimi-K3`) with **DSpark Speculative Decoding** (`RadixArk/Kimi-K3-DSpark`) across 2 nodes (**16 × NVIDIA B200 GPUs**) on Google Kubernetes Engine (GKE) using **SGLang (`docker.io/lmsysorg/sglang:kimi-k3`)** and **Google gIB RDMA (`nccl-plugin-gib:v1.1.0`)** over 100 Gbps networking.

---

## 1. Architecture & Storage Caching

* **Low-Latency Strategy (`TP=16, PP=1, DP=1`):**
  * Spans 16 × NVIDIA B200 GPUs across two `a4-highgpu-8g-a4` worker nodes (`--tp-size 16 --nnodes 2`).
  * Head Node (`pod-index: 0`) coordinates distributed initialization over headless Service `sglang-master-pod-k3:20000` and exposes the API serving endpoint on port `30000`.
* **DSpark Speculative Decoding (`RadixArk/Kimi-K3-DSpark`):**
  * Uses `--speculative-algorithm DSPARK --speculative-draft-model-path /root/.cache/huggingface/Kimi-K3-DSpark --speculative-dspark-block-size 7`.
  * Enables Linear Replay SSM verification via `--enable-linear-replayssm-spec` for high-accuracy state space model draft validation.
* **Cross-Node RDMA (`gIB` over RoCEv2) & Multi-Networking:**
  * Uses pod annotations `networking.gke.io/interfaces` (`rdma-0..7` mapped to `eth2..eth9`) to attach 8 secondary RoCEv2 network interfaces to each pod (1-to-1 ConnectX-7 NIC pairing for all 8 NVIDIA B200 GPUs on an `a4-highgpu-8g` node for full 800 Gbps / 3.2 Tbps GPUDirect RDMA bandwidth).
  * Mounts host RDMA and driver libraries (`gib` from `/home/kubernetes/bin/gib`, `nvidia` from `/home/kubernetes/bin/nvidia`, and `lib64` from `/lib64`) directly into `/usr/local/gib`, `/usr/local/nvidia`, and `/lib64`, eliminating the need for `privileged: true` or `hostNetwork: true`.
  * Sources `/usr/local/gib/scripts/set_nccl_env.sh` and exports `LD_LIBRARY_PATH="/usr/local/gib/lib64:/usr/local/nvidia/lib64:/lib64:$LD_LIBRARY_PATH"` for RoCEv2 GPU-to-GPU transfers.
  * Explicitly exports `NCCL_TUNER_CONFIG_PATH=/usr/local/gib/configs/tuner_config_default.txtpb` after sourcing `set_nccl_env.sh` (preventing NCCL from defaulting to `tuner_config_a4.txtpb`, which expects bare-metal unmanaged PCIe bus numbering and crashes under containerized GKE Dataplane V2 RoCEv2 multi-networking).
  * Pins bootstrap and NCCL interfaces explicitly:
    ```bash
    export GLOO_SOCKET_IFNAME=eth0
    export NCCL_SOCKET_IFNAME=eth0
    export NCCL_IB_GID_INDEX=3
    export NCCL_IB_ROCE_VERSION_NUM=2
    export SGLANG_HOST_IP=$(hostname -I | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)
    ```
* **12 TB NVMe RAID 0 Caching (`/dev/md0`):**
  * Uses `hostPath: /mnt/stateful_partition/kube-ephemeral-ssd/huggingface_cache` mounted to `/root/.cache/huggingface`.
  * The init-container `pull-model-from-gcs` executes `gcloud storage rsync` from `gs://ikwak-models-gpu-launchpad-playground/Kimi-K3` and downloads `RadixArk/Kimi-K3-DSpark`, caching both checkpoints locally on disk for 0.01-second pod restarts.
  * Sets `SGLANG_USE_RUNAI_MODEL_STREAMER: "true"` in the container environment so SGLang streams model weights concurrently from the GCS rapid cache.

---

## 2. File Overview

* **[`sglang-kimi3-2node.yaml`](sglang-kimi3-2node.yaml):**
  * Full multi-node deployment including:
    * `Service: sglang-master-pod-k3` (Headless clusterIP: None, port `20000`)
    * `Service: sglang-serving-k3` (ClusterIP, port `30000`, selecting `pod-index: 0`)
    * `StatefulSet: distributed-sglang-k3` (`replicas: 2`, `TP=16`)
* **[`kimi-k3-gcs-uploader-job.yaml`](kimi-k3-gcs-uploader-job.yaml):**
  * High-speed parallel downloader and GCS uploader Job (`hf_transfer`).
* **[`gke-inference-gateway-k3.yaml`](gke-inference-gateway-k3.yaml):**
  * GKE Inference Gateway (`llm-d`) with Regional Internal Application Load Balancer (`gke-l7-rilb`, VIP `http://192.168.0.10`) for KV-cache aware request routing via Envoy Ext-Proc.
* **[`inference-perf-k3-2node-direct.yaml`](inference-perf-k3-2node-direct.yaml):**
  * Standardized `inference-perf` benchmark client for 2-Node Direct Service (`ISL=1024, OSL=8192, BS=512`).
* **[`inference-perf-k3-2node-gateway.yaml`](inference-perf-k3-2node-gateway.yaml):**
  * Standardized `inference-perf` benchmark client for 2-Node GKE Inference Gateway (`ISL=1024, OSL=8192, BS=512`).

---

## 3. Cluster & Node Pool Prerequisites (Multi-Networking & RoCEv2)

When creating a new GKE cluster or B200 (`a4-highgpu-8g`) spot/reserved node pool for low-latency RDMA serving (refer to the [Google Cloud AI Hypercomputer RoCEv2 Multi-Networking Documentation](https://docs.cloud.google.com/ai-hypercomputer/docs/create/gke-ai-hypercompute-custom#create-cluster-and-node-pool-rdma-multi-net)):
1. **Attach 8 Secondary RoCEv2 RDMA Networks to the Node Pool:**
   You must pass 8 `--additional-node-network` flags during node pool creation (one per ConnectX-7 RoCEv2 RDMA NIC for each of the 8 B200 GPUs) so that GKE attaches all 8 secondary RoCEv2 NICs to each host VM and creates the `/dev/infiniband` (`uverbs0..7`) character devices in the Linux kernel:
   ```bash
   gcloud container node-pools create b200-spot-pool \
     --cluster=kimi-k3-uw8-cluster \
     --location=us-west8-c \
     --machine-type=a4-highgpu-8g \
     --num-nodes=2 \
     --spot \
     --additional-node-network=network=rdma-0,subnetwork=rdma-sub-0 \
     --additional-node-network=network=rdma-1,subnetwork=rdma-sub-1 \
     --additional-node-network=network=rdma-2,subnetwork=rdma-sub-2 \
     --additional-node-network=network=rdma-3,subnetwork=rdma-sub-3 \
     --additional-node-network=network=rdma-4,subnetwork=rdma-sub-4 \
     --additional-node-network=network=rdma-5,subnetwork=rdma-sub-5 \
     --additional-node-network=network=rdma-6,subnetwork=rdma-sub-6 \
     --additional-node-network=network=rdma-7,subnetwork=rdma-sub-7
   ```
2. **Deploy the Host `nccl-rdma-installer` DaemonSet:**
   New clusters require `nccl-rdma-installer-ds.yaml` (`nccl-plugin-gib:v1.1.0`) deployed to `kube-system` to disable IPv4 log martians and populate `/home/kubernetes/bin/nvidia/lib64` on worker nodes:
   ```bash
   kubectl apply -f nccl-rdma-installer-ds.yaml
   ```

---

## 4. Deployment & Usage

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
