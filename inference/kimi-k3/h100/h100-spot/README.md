# MoonshotAI Kimi-K3 (`moonshotai/Kimi-K3`): Deploying on Spot H100 GPUs with GKE Multi-Cluster Elastic Cross-Region High Availability

This guide walks through deploying the 671B MoE **MoonshotAI Kimi-K3** model with **DSpark Speculative Decoding** (`RadixArk/Kimi-K3-DSpark`) across **NVIDIA H100 80GB SXM5 Spot GPUs** on Google Kubernetes Engine (GKE) A3 Mega (`a3-megagpu-8g`).

By combining Google Cloud Console **Capacity Advisor**, a **US Multi-Region GCS Bucket**, **GKE Custom Compute Classes for Elastic Cross-Region High Availability**, and the **GKE Multi-Cluster Inference Gateway (`llm-d`)**, you can capture ultra-low Spot GPU pricing while protecting your production serving endpoint against Spot capacity preemption.

---

## 1. Finding Spot GPU Capacity Using Capacity Advisor

Before provisioning GPU clusters, use Google Cloud Console's **[Capacity Advisor](https://console.cloud.google.com/compute/capacityAdvisor)** to identify regions and zones with high Spot VM availability, compare historical preemption rates, and evaluate hourly Spot pricing for 4 × `a3-megagpu-8g` (32 × H100 GPUs total).

### Analyzing Console Insights (Example Trade-off: `us-west1` vs. `us-east4`)

When selecting a 4-node A3 Mega Spot footprint (`4 × a3-megagpu-8g` = 32 GPUs), Capacity Advisor reveals significant regional pricing and stability trade-offs:

#### 1. `us-west1` Spot Capacity Advisor (High Stability: `0-5%` Preemption Rate — `$53.35 / VM / hr`)
![Capacity Advisor - us-west1 Spot VM Availability, 0-5% Preemption Rate, $53.35/hr](images/capacity-advisor-us-west1.png)

#### 2. `us-east4` Spot Capacity Advisor (Cost-Optimized: `6-10%` Preemption Rate — `$21.70 / VM / hr`)
![Capacity Advisor - us-east4 Spot VM Availability, 6-10% Preemption Rate, $21.70/hr](images/capacity-advisor-us-east4.png)

Include example of spot advisor API for next level info on capacity assurance/planning  
- Filter on minimum duration of estimated run time eg. 1 hour
- Show example of request and response and highlight key insights to support above console experience

### Summary of Regional Trade-offs

| Region | Available Zonal Capacity | Historical Preemption Rate | Total Hourly Cost (4 × `a3-megagpu-8g`) | Cost per VM Hour | Strategic Recommendation |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **`us-west1`** | `us-west1-a` (High)<br>`us-west1-b` (Limited) | **`0 - 5%`** *(Very Low)* | **`$213.41` / hour** (`$53.352/VM`) | `$53.352 / hr` | **High-Stability Backup Fleet**: Low preemption risk, ideal for elastic failover. |
| **`us-east4`** | `us-east4-a` (High)<br>`us-east4-c` (High) | **`6 - 10%`** *(Moderate)* | **`$86.80` / hour** (`$21.70/VM`) | `$21.70 / hr` | **Cost-Optimized Primary Fleet**: **59.3% cheaper hourly cost** (`$21.70/hr` vs `$53.35/hr`), ideal for primary serving. |

> [!TIP]
> **The Multi-Region Elastic Strategy**: Instead of choosing between price and stability, deploy a **Multi-Cluster Inference Gateway** that spans both `us-east4` (primary cost-optimized fleet at `$21.70/VM/hr`) and `us-west1` (elastic failover fleet at `$53.352/VM/hr`). If Spot capacity in `us-east4` is reclaimed, incoming requests seamlessly fail over to `us-west1` with zero downtime.

---

## 2. Architecture & Multi-Region GCS Planning

To enable cross-region Spot elasticity without duplicating 671 GB model checkpoints across multiple regional storage buckets, we use a single **US Multi-Region Google Cloud Storage (GCS) Bucket**.

```
                           +------------------------------------------------------+
                           |          GKE Multi-Cluster Inference Gateway         |
                           |               (Global External HTTP/S)               |
                           +--------------------------+---------------------------+
                                                      |
                         +----------------------------+----------------------------+
                         | (Primary Routing - Cost)                                | (Failover Routing - Stability)
                         v                                                         v
         +-------------------------------+                         +-------------------------------+
         |     GKE Cluster: us-east4     |                         |     GKE Cluster: us-west1     |
         |  (4 × a3-megagpu-8g Spot VMs) |                         |  (4 × a3-megagpu-8g Spot VMs) |
         |   Price: $21.70/hr (6-10%)    |                         |   Price: $53.35/hr (0-5%)     |
         +---------------+---------------+                         +---------------+---------------+
                         |                                                         |
                         +----------------------------+----------------------------+
                                                      |
                                                      v
                           +------------------------------------------------------+
                           |         US Multi-Region GCS Storage Bucket           |
                           |       (gs://multi-region-kimi-k3-cache/bucket/)      |
                           |    Rapid Cache Enabled | Shared across US regions    |
                           +------------------------------------------------------+
```

### Benefits of a Multi-Region Bucket for Spot Workloads
1. **One-Time Model Upload**: Upload `moonshotai/Kimi-K3` and `RadixArk/Kimi-K3-DSpark` once to `gs://${MULTI_REGION_BUCKET}/`.
2. **Zero Regional Replication Cost**: Both `us-east4` and `us-west1` GKE clusters mount the bucket root to `/bucket` via `gke-gcsfuse`.
3. **Rapid Cache Integration**: When combined with GKE Rapid Cache or local SSD/NVMe caching, replacement Spot pods in any US region initialize and stream checkpoints in seconds.

---

## 3. Creating a Multi-Region GCS Bucket & Pre-Uploading Checkpoints

### 1. Create the US Multi-Region Bucket (with Standard / Rapid Cache Storage Class)
```bash
export MULTI_REGION_BUCKET="multi-region-kimi-k3-cache-${USER}"

# Create a US multi-region GCS bucket
gcloud storage buckets create gs://${MULTI_REGION_BUCKET} \
  --location=US \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access
```

### 2. Download Models Locally & Upload to Bucket Root
To ensure `--model-path=/bucket/Kimi-K3` and `--speculative-draft-model-path=/bucket/Kimi-K3-DSpark` resolve correctly inside SGLang containers:

```bash
# 1. Download target and draft models locally without symlinks
huggingface-cli download moonshotai/Kimi-K3 \
  --local-dir ./Kimi-K3 \
  --local-dir-use-symlinks False

huggingface-cli download RadixArk/Kimi-K3-DSpark \
  --local-dir ./Kimi-K3-DSpark \
  --local-dir-use-symlinks False

# 2. Copy generation_config.json into draft model (omitted by RadixArk repository)
cp ./Kimi-K3/generation_config.json ./Kimi-K3-DSpark/

# 3. Upload directly to the GCS bucket root
gcloud storage cp -r ./Kimi-K3 gs://${MULTI_REGION_BUCKET}/Kimi-K3
gcloud storage cp -r ./Kimi-K3-DSpark gs://${MULTI_REGION_BUCKET}/Kimi-K3-DSpark
```

---

## 4. Provisioning Multi-Cluster Spot GPU Pools with Elastic Cross-Region High Availability

We configure two autonomous GKE clusters (`us-east4-kimi-k3` and `us-west1-kimi-k3`) using **GKE Custom Compute Classes**, **Extended Graceful Node Shutdown (`120s`)**, and **Elastic Cross-Region High Availability** ([GKE Documentation](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-elastic-cross-region-high-availability)).

> [!IMPORTANT]
> **Why Extended Graceful Shutdown (`120s`) is Essential for Spot LLM Serving**:  
> By default, Spot VM preemption provides a 30-second shutdown window. By extending `shutdownGracePeriodSeconds` to **`120 seconds` (2 full minutes)** ([GKE Spot VM Graceful Shutdown Docs](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/spot-vms#termination-graceful-shutdown)):
> 1. **Maximum Replacement Runway**: GKE immediately cordons the preempted node and gives Cluster Autoscaler / CustomComputeClass 120 seconds of runway to spin up a replacement Spot node in an alternate zone (`us-east4-c` or failover cluster `us-west1-a`) before workloads are forcefully terminated.
> 2. **Zero-Disruption Request Draining**: LLM inference requests in-flight on `sglang serve` have up to 2 minutes to finish generating, while GKE Multi-Cluster Inference Gateway redirects all new incoming traffic to healthy peer pods.

### 1. Create Custom Compute Class & Extended KubeletConfig for Spot A3 Mega
Apply this `CustomComputeClass` and `KubeletConfig` manifest in both clusters to define automatic zone/region fallback rules and enforce the **120-second graceful shutdown period**:

```yaml
apiVersion: node.gke.io/v1beta1
kind: KubeletConfig
metadata:
  name: spot-120s-grace-period
spec:
  shutdownGracePeriodSeconds: 120
  shutdownGracePeriodCriticalPodsSeconds: 30
---
apiVersion: cloud.google.com/v1
kind: CustomComputeClass
metadata:
  name: spot-a3-mega-class
spec:
  nodePools:
  - name: spot-a3-mega-pool
    machineType: a3-megagpu-8g
    spot: true
    scaling:
      minNodeCount: 4
      maxNodeCount: 8
```

### 2. Create the Regional GKE Clusters & Spot Node Pools (with 120s Graceful Shutdown)
To explicitly configure the 120-second graceful shutdown window on node pool creation:

```bash
# 1. Create local Kubelet configuration file for 120s Spot graceful shutdown
cat <<EOF > kubelet-config.yaml
shutdownGracePeriodSeconds: 120
shutdownGracePeriodCriticalPodsSeconds: 30
EOF

# 2. Primary Cost-Optimized Cluster ($21.70/hr — us-east4)
gcloud container clusters create us-east4-kimi-k3 \
  --region=us-east4 \
  --workload-pool=${PROJECT_ID}.svc.id.goog \
  --enable-ip-alias \
  --enable-dataplane-v2

gcloud container node-pools create spot-h100-pool \
  --cluster=us-east4-kimi-k3 \
  --region=us-east4 \
  --machine-type=a3-megagpu-8g \
  --num-nodes=4 \
  --spot \
  --node-kubelet-config=kubelet-config.yaml \
  --node-locations=us-east4-a,us-east4-c

# 3. Elastic Failover Cluster ($53.35/hr — us-west1)
gcloud container clusters create us-west1-kimi-k3 \
  --region=us-west1 \
  --workload-pool=${PROJECT_ID}.svc.id.goog \
  --enable-ip-alias \
  --enable-dataplane-v2

gcloud container node-pools create spot-h100-pool \
  --cluster=us-west1-kimi-k3 \
  --region=us-west1 \
  --machine-type=a3-megagpu-8g \
  --num-nodes=4 \
  --spot \
  --node-kubelet-config=kubelet-config.yaml \
  --node-locations=us-west1-a
```

---

## 5. Deploying the TCPXO DaemonSet & Kimi-K3 Serving Fleet

For each cluster (`us-east4-kimi-k3` and `us-west1-kimi-k3`), apply the canonical GPUDirect TCPXO installer DaemonSet and declarative 4-Node SGLang Kimi-K3 manifest from [`/inference/kimi-k3/h100`](https://github.com/saltysoup/jetski-playground/tree/main/inference/kimi-k3/h100):

```bash
for CONTEXT in gke_${PROJECT_ID}_us-east4_us-east4-kimi-k3 gke_${PROJECT_ID}_us-west1_us-west1-kimi-k3; do
  echo "=== Deploying to cluster: ${CONTEXT} ==="
  
  # 1. Deploy canonical TCPXO installer DaemonSet (v1.0.17+)
  kubectl --context=${CONTEXT} apply -f \
    https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/gpudirect-tcpxo/nccl-tcpxo-installer.yaml

  # 2. Apply SGLang Kimi-K3 4-Node serving manifest (TP32 / EP32)
  # Ensure GCS bucket annotation matches MULTI_REGION_BUCKET
  kubectl --context=${CONTEXT} apply -f inference/kimi-k3/h100/sglang-kimi3-h100.yaml
done
```

---

## 6. Configuring Multi-Cluster Inference Gateway & Verifying Served Region

We use GKE **Multi-Cluster Services (MCS)** and **Inference Gateway** ([Setup Guide](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/setup-multicluster-inference-gateway)) to expose a single global HTTP/S endpoint across both Spot clusters.

### 1. Export Multi-Cluster Service & HTTPRoute
Apply the `MultiClusterService` and `HTTPRoute` in your Gateway configuration cluster to unify `sglang-serving-k3` across `us-east4` and `us-west1`:

```yaml
apiVersion: net.gke.io/v1
kind: MultiClusterService
metadata:
  name: sglang-serving-k3-mcs
  namespace: default
spec:
  template:
    spec:
      selector:
        app: sglang-kimi3-h100
      ports:
      - name: http
        port: 30000
        targetPort: 30000
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: kimi-k3-global-route
  namespace: default
spec:
  parentRefs:
  - name: kimi-k3-external-gateway
  rules:
  - matches:
    - path:
        type: PathPrefix
        value: /v1
    backendRefs:
    - name: sglang-serving-k3-mcs
      port: 30000
```

### 2. Verify Deployment & See Which Regional Cluster Served the Request
To verify where the request is being served from (identifying whether `us-east4` or `us-west1` answered the request), send a completion request to the external Gateway IP with `curl -i` (to inspect HTTP response headers):

```bash
export GATEWAY_IP=$(kubectl get gateway kimi-k3-external-gateway -o jsonpath='{.status.addresses[0].value}')

# Send test request and inspect regional routing headers
curl -i -X POST http://${GATEWAY_IP}/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "moonshotai/Kimi-K3",
    "messages": [
      {"role": "user", "content": "Explain the significance of speculative decoding for MoE models in 1 sentence."}
    ],
    "max_tokens": 128,
    "temperature": 0.6
  }'
```

#### Identifying the Regional Cluster from Response Headers
When routed through GKE Multi-Cluster Inference Gateway / Cloud Load Balancing, inspect the response headers in the `curl -i` output:
* **`X-Google-Backend`**: Displays the exact regional backend service that served the request:
  * Primary Cost-Optimized Answer: `X-Google-Backend: us-east4-spot-h100-pool` (`$21.70/hr`)
  * Failover Backup Answer: `X-Google-Backend: us-west1-spot-h100-pool` (`$53.35/hr`)
* **`X-Served-By-Hostname`**: In SGLang, the generating pod's rank-0 hostname (`distributed-sglang-k3-0`) and cluster DNS suffix indicate the active cluster location.
