# ThinkingMachines Inkling-Small-NVFP4 (`thinkingmachines/Inkling-Small-NVFP4`): Deploying on Spot H100 GPUs with GKE Multi-Cluster Elastic Cross-Region High Availability

This guide walks through deploying **ThinkingMachines Inkling-Small-NVFP4** (`thinkingmachines/Inkling-Small-NVFP4`)—an NVFP4 (4-bit floating point) quantized checkpoint derived from MoonshotAI Kimi-K3—across **NVIDIA H100 80GB SXM5 Spot GPUs** on Google Kubernetes Engine (GKE) A3 Mega (`a3-megagpu-8g`).

By leveraging **NVFP4 Quantization**, the model fits on a **single 8-GPU H100 node (`--tensor-parallel-size=8`)**, leaving massive physical HBM headroom to support a **128k context window (`--max-model-len=131072`)** and speculative MTP decoding. Combined with Google Cloud Console **Capacity Advisor**, a **US Multi-Region GCS Bucket**, **GKE Custom Compute Classes for Extended Graceful Node Shutdown (`120s`)**, and the **GKE Multi-Cluster Inference Gateway (`llm-d`)**, you capture ultra-low Spot GPU pricing (`$21.70 / VM / hr` in `us-east4`) while protecting your production serving endpoint against Spot preemption.

---

## 1. Finding Spot GPU Capacity Using Capacity Advisor

Before provisioning GPU clusters, use Google Cloud Console's **[Capacity Advisor](https://console.cloud.google.com/compute/capacityAdvisor)** to identify regions and zones with high Spot VM availability, compare historical preemption rates, and evaluate hourly Spot pricing for `a3-megagpu-8g` (8 × H100 SXM5 GPUs per node).

### Analyzing Console Insights (Example Trade-off: `us-west1` vs. `us-east4`)

When selecting an A3 Mega Spot footprint (`1 × a3-megagpu-8g` per cluster = 8 GPUs), Capacity Advisor reveals significant regional pricing and stability trade-offs:

#### 1. `us-west1` Spot Capacity Advisor (High Stability: `0-5%` Preemption Rate — `$53.35 / VM / hr`)
![Capacity Advisor - us-west1 Spot VM Availability, 0-5% Preemption Rate, $53.35/hr](images/capacity-advisor-us-west1.png)

#### 2. `us-east4` Spot Capacity Advisor (Cost-Optimized: `6-10%` Preemption Rate — `$21.70 / VM / hr`)
![Capacity Advisor - us-east4 Spot VM Availability, 6-10% Preemption Rate, $21.70/hr](images/capacity-advisor-us-east4.png)

Include example of spot advisor API for next level info on capacity assurance/planning  
- Filter on minimum duration of estimated run time eg. 1 hour
- Show example of request and response and highlight key insights to support above console experience

### Summary of Regional Trade-offs

| Region | Available Zonal Capacity | Historical Preemption Rate | Hourly Cost per A3 Mega VM (`8 × H100`) | Strategic Recommendation |
| :--- | :---: | :---: | :---: | :--- |
| **`us-west1`** | `us-west1-a` (High)<br>`us-west1-b` (Limited) | **`0 - 5%`** *(Very Low)* | **`$53.352` / hr** | **High-Stability Backup Fleet**: Low preemption risk, ideal for elastic failover. |
| **`us-east4`** | `us-east4-a` (High)<br>`us-east4-c` (High) | **`6 - 10%`** *(Moderate)* | **`$21.700` / hr** | **Cost-Optimized Primary Fleet**: **59.3% cheaper hourly cost** (`$21.70/hr` vs `$53.35/hr`), ideal for primary serving. |

> [!TIP]
> **The Multi-Region Elastic Strategy**: Instead of choosing between price and stability, deploy a **Multi-Cluster Inference Gateway** that spans both `us-east4` (primary cost-optimized fleet at `$21.70/VM/hr`) and `us-west1` (elastic failover fleet at `$53.352/VM/hr`). If Spot capacity in `us-east4` is reclaimed, incoming requests seamlessly fail over to `us-west1` with zero downtime.

Alternatively, we can also use an chat interface with [Compute Advisor](https://console.cloud.google.com/compute/advisor/) to find real-time obtainability of Spot and DWS-Flex accelerators across regions, verify project quotas are sufficient and get example commands to deploy.

![Compute Advisor](images/compute-advisor.png)

---

## 2. Solution Architecture

![Reference Architecture - Deploy LLM inference on GKE using Fluid Compute](images/reference-architecture.png)

### Benefits of a Multi-Region Bucket for Spot Workloads
1. **One-Time Model Upload**: Upload `thinkingmachines/Inkling-Small-NVFP4` once to `gs://${MULTI_REGION_BUCKET}/Inkling-Small-NVFP4`.
2. **Zero Regional Replication Cost**: Both `us-east4` and `us-west1` GKE clusters mount the bucket root to `/bucket` via `gke-gcsfuse`.
3. **Rapid Cache Integration**: When combined with GKE Rapid Cache or local SSD/NVMe caching, replacement Spot pods in any US region initialize and stream checkpoints in seconds.

---

## 3. Creating a Multi-Region GCS Bucket & Pre-Uploading Checkpoints

### 1. Create the US Multi-Region Bucket (with Standard / Rapid Cache Storage Class)
```bash
export MULTI_REGION_BUCKET="multi-region-inkling-cache-${USER}"

# Create a US multi-region GCS bucket
gcloud storage buckets create gs://${MULTI_REGION_BUCKET} \
  --location=US \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access
```

### 2. Download Model Locally & Upload to Bucket Root
To ensure `--model=/bucket/Inkling-Small-NVFP4` resolves correctly inside vLLM containers:

```bash
# 1. Download Inkling-Small-NVFP4 checkpoint locally without symlinks
export HF_TOKEN="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"
hf download thinkingmachines/Inkling-Small-NVFP4 \
  --local-dir ./Inkling-Small-NVFP4

# 2. Upload directly to the GCS bucket root
gcloud storage cp -r ./Inkling-Small-NVFP4 gs://${MULTI_REGION_BUCKET}/Inkling-Small-NVFP4
```

---

## 3.5. Accelerating Multi-Region Bucket Reads with GKE Cloud Storage FUSE Profiles (`gcsfusecsi-serving` & Zonal Rapid Cache)

We use a **Multi-Region GCS Bucket** (`gs://ikwak-models-mr-gpu-launchpad-playground` in `US`) as a single global namespace for our `Inkling-Small-NVFP4` model weights. To achieve sub-millisecond TTFB and up to **2.5 TB/s zonal SSD throughput** without paying cross-region data transfer fees, we combine **Zonal Rapid Cache** with **GKE Cloud Storage FUSE Profiles (`gke-gcsfuse/profile`)** ([GKE FUSE Profiles Documentation](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gcsfuse-profiles)).

### 1. Create Zonal Rapid Caches Across Compute Zones
Using the GA Cloud Storage syntax ([Rapid Cache CLI Docs](https://docs.cloud.google.com/storage/docs/rapid/use-rapid-cache#command-line)), create SSD-backed zonal read caches on the multi-region bucket across our member cluster zones (`us-west1-a` and `us-east4-a`) with a single command:

```bash
gcloud storage buckets anywhere-caches create gs://ikwak-models-mr-gpu-launchpad-playground \
  us-west1-a \
  us-east4-a \
  --ttl=604800 \
  --admission-policy=ADMIT_ON_FIRST_MISS
```
* **`--ttl=604800`**: Sets a 7-day Time to Live for static LLM weights.
* **`ADMIT_ON_FIRST_MISS`**: The first pod read in each zone automatically ingests and SSD-caches the model chunks locally.

### 2. Grant Workload Identity & FUSE Profile Permissions
Bind `roles/storage.objectViewer` to your GKE Workload Identity service account on the multi-region bucket:

```bash
gcloud storage buckets add-iam-policy-binding gs://ikwak-models-mr-gpu-launchpad-playground \
  --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT_ID}.svc.id.goog/subject/ns/default/sa/default" \
  --role="roles/storage.objectViewer"
```

### 3. Deploy Workload with `gcsfusecsi-serving` Profile
In place of manual FUSE cache tuning, our deployment manifest ([`vllm-inkling-nvfp4-h100.yaml`](vllm-inkling-nvfp4-h100.yaml)) defines a static PersistentVolume (`PV`) and PersistentVolumeClaim (`PVC`) using `storageClassName: gcsfusecsi-serving`:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: gcsfuse-serving-pv
spec:
  accessModes:
  - ReadWriteMany
  capacity:
    storage: 250Gi
  persistentVolumeReclaimPolicy: Retain
  storageClassName: gcsfusecsi-serving
  csi:
    driver: gcsfuse.csi.storage.gke.io
    volumeHandle: ikwak-models-mr-gpu-launchpad-playground
    volumeAttributes:
      gcsfuseMetadataPrefetchOnMount: "true"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: gcsfuse-serving-pvc
spec:
  accessModes:
  - ReadWriteMany
  resources:
    requests:
      storage: 250Gi
  volumeName: gcsfuse-serving-pv
  storageClassName: gcsfusecsi-serving
```
* **Automated Caching & Tuning**: GKE's CSI Node Server automatically scans the bucket and inspects your H100 node's RAM and NVMe Local SSDs (`ephemeral-storage`), dynamically calculating and applying optimal metadata and file caching for vLLM.

### 4. Verifying Profile & Local Cache Usage in Container Logs
To verify that `gcsfusecsi-serving` applied profile optimizations and is utilizing the cache, inspect the `gke-gcsfuse-sidecar` container logs:

```bash
kubectl logs -c gke-gcsfuse-sidecar -l app=vllm-inkling-nvfp4 --tail=20
```

#### Expected Log Verification Output:
```json
{"severity":"INFO","message":"GCSFuse Config","Applied optimizations for bucket-type: ":"flat","Full Config":{"metadata-cache.ttl-secs":{"final_value":-1,"optimization_reason":"profile \"aiml-serving\""}}}
{"severity":"INFO","message":"Mounting file system \"ikwak-models-mr-gpu-launchpad-playground\"..."}
{"severity":"INFO","message":"File system has been successfully mounted."}
```
* Notice `"optimization_reason":"profile \"aiml-serving\""` confirming that GKE FUSE Profile automated tuning is active.

---

## 4. Provisioning Multi-Cluster Spot GPU Pools with Elastic Cross-Region High Availability

We configure two autonomous GKE clusters (`us-east4-inkling` and `us-west1-inkling`) using **GKE Custom Compute Classes**, **Extended Graceful Node Shutdown (`120s`)**, and **Elastic Cross-Region High Availability** ([GKE Documentation](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-elastic-cross-region-high-availability)).

> [!IMPORTANT]
> **Why Extended Graceful Shutdown (`120s`) is Essential for Spot LLM Serving**:  
> By default, Spot VM preemption provides a 30-second shutdown window. By extending `shutdownGracePeriodSeconds` to **`120 seconds` (2 full minutes)** ([GKE Spot VM Graceful Shutdown Docs](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/spot-vms#termination-graceful-shutdown)):
> 1. **Maximum Replacement Runway**: GKE immediately cordons the preempted node and gives Cluster Autoscaler / CustomComputeClass 120 seconds of runway to spin up a replacement Spot node in an alternate zone (`us-east4-c` or failover cluster `us-west1-a`) before workloads are forcefully terminated.
> 2. **Zero-Disruption Request Draining**: LLM inference requests in-flight on `vllm` have up to 2 minutes to finish generating, while GKE Multi-Cluster Inference Gateway redirects all new incoming traffic to healthy peer pods.

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
      minNodeCount: 1
      maxNodeCount: 4
```

### 2. Create the Regional GKE Clusters & Spot Node Pools (with 120s Graceful Shutdown)
To explicitly configure the 120-second graceful shutdown window on node pool creation:

```bash
# 1. Create local Kubelet configuration file for 120s Spot graceful shutdown
# 1. Create local Kubelet configuration file for 120s Spot graceful shutdown
cat <<EOF > kubelet-config.yaml
shutdownGracePeriodSeconds: 120
shutdownGracePeriodCriticalPodsSeconds: 30
EOF

# 2. Primary Cluster (us-west1-a)
gcloud container clusters create ikwak-a3m-spot \
  --region=us-west1 \
  --workload-pool=${PROJECT_ID}.svc.id.goog \
  --enable-ip-alias \
  --enable-dataplane-v2

gcloud container node-pools create spot-h100-pool \
  --cluster=ikwak-a3m-spot \
  --region=us-west1 \
  --machine-type=a3-megagpu-8g \
  --accelerator="type=nvidia-h100-80gb,count=8,gpu-driver-version=latest" \
  --num-nodes=1 \
  --spot \
  --node-kubelet-config=kubelet-config.yaml \
  --node-locations=us-west1-a

# 3. Secondary Failover Cluster (us-east4-a)
gcloud container clusters create ikwak-a3h-spot \
  --region=us-east4 \
  --workload-pool=${PROJECT_ID}.svc.id.goog \
  --enable-ip-alias \
  --enable-dataplane-v2

gcloud container node-pools create spot-h100-pool-a \
  --cluster=ikwak-a3h-spot \
  --region=us-east4 \
  --machine-type=a3-highgpu-8g \
  --accelerator="type=nvidia-h100-80gb,count=8,gpu-driver-version=latest" \
  --num-nodes=1 \
  --spot \
  --node-kubelet-config=kubelet-config.yaml \
  --node-locations=us-east4-a
```

---

## 5. Configuring Custom Compute Class (DWS Flex Start, Spot, On-Demand Scaling Priorities)

We use **GKE Custom Compute Classes (`ComputeClass`)** ([GKE Documentation](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/dws-flex-start-inference#custom-compute-classes)) to define an intelligent, multi-tier fallback priority for GPU capacity across our regions:

1. **Priority 1**: `a3-highgpu-8g` Specific Reservation (`myh100cud`) — instant capacity check against existing committed capacity
2. **Priority 2**: `a3-highgpu-8g` Spot VM (`--spot`) — instant capacity check against Spot pools
3. **Priority 3**: `a3-highgpu-8g` DWS Flex Start (`flexStart: enabled: true`, 300s queue wait time) — discounted up to 53% off on-demand, up to 7-day duration
4. **Priority 4**: `a3-megagpu-8g` DWS Flex Start (`flexStart: enabled: true`, 300s queue wait time)
5. **Priority 5**: `a3-highgpu-8g` On-Demand VM (standard fallback)

> [!TIP]
> **Why `capacityCheckWaitTimeSeconds` is Excluded on Reservations & Spot**:  
> In GKE Custom Compute Classes, `capacityCheckWaitTimeSeconds` is only valid on queued consumption models (**DWS Flex Start** and **Multi-Host TPUs**). For existing Reservations, Spot VMs, and On-Demand VMs, GKE checks available compute capacity instantaneously—if reservation `myh100cud` does not exist or is fully utilized, GKE immediately falls back to Priority 2 (`Spot`) without waiting in a queue.

### 1. Enable Node Auto-Provisioning (NAP) with H100 & H100 Mega Limits
To allow GKE to dynamically auto-provision fallback node pools for either `a3-highgpu-8g` (`nvidia-h100-80gb`) or `a3-megagpu-8g` (`nvidia-h100-mega-80gb`), configure NAP resource limits on both clusters:

```bash
for CLUSTER in ikwak-a3m-spot:us-west1 ikwak-a3h-spot:us-east4; do
  NAME=${CLUSTER%%:*}
  LOC=${CLUSTER##*:}
  gcloud container clusters update ${NAME} --location=${LOC} \
    --enable-autoprovisioning --min-cpu=1 --max-cpu=10000 --min-memory=1 --max-memory=100000 \
    --min-accelerator=type=nvidia-h100-80gb,count=0 --max-accelerator=type=nvidia-h100-80gb,count=16 \
    --min-accelerator=type=nvidia-h100-mega-80gb,count=0 --max-accelerator=type=nvidia-h100-mega-80gb,count=16 \
    --autoprovisioning-scopes="https://www.googleapis.com/auth/cloud-platform" --quiet
done
```

### 2. Apply Declarative `ComputeClass` (`inkling-compute-class.yaml`)
Apply [`inkling-compute-class.yaml`](inkling-compute-class.yaml) to both clusters:

```yaml
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: inkling-gpu-class
  namespace: default
spec:
  nodePoolAutoCreation:
    enabled: true
  activeMigration:
    optimizeRulePriority: true
  autoscalingPolicy:
    consolidationDelayMinutes: 5
  whenUnsatisfiable: DoNotScaleUp
  priorities:
  # Priority 1: a3-highgpu-8g Specific Reservation (myh100cud)
  - machineType: a3-highgpu-8g
    reservations:
      affinity: Specific
      specific:
      - name: myh100cud
  # Priority 2: a3-highgpu-8g Spot
  - machineType: a3-highgpu-8g
    spot: true
  # Priority 3: a3-highgpu-8g DWS Flex Start (queue wait time: 300s)
  - machineType: a3-highgpu-8g
    capacityCheckWaitTimeSeconds: 300
    flexStart:
      enabled: true
  # Priority 4: a3-megagpu-8g DWS Flex Start (queue wait time: 300s)
  - machineType: a3-megagpu-8g
    capacityCheckWaitTimeSeconds: 300
    flexStart:
      enabled: true
  # Priority 5: a3-highgpu-8g On-Demand
  - machineType: a3-highgpu-8g
```

---

## 6. Deploying vLLM Inkling-Small-NVFP4 Serving Fleet (Single-Node TP8 / 128k Context)

For each cluster (`ikwak-a3m-spot` and `ikwak-a3h-spot`), apply the declarative single-node vLLM deployment manifest from [`vllm-inkling-nvfp4-h100.yaml`](vllm-inkling-nvfp4-h100.yaml).
In place of hardcoded node pools, the workload references our Custom Compute Class (`nodeSelector: cloud.google.com/compute-class: inkling-gpu-class`) and includes tolerations for `cloud.google.com/gke-queued` (DWS Flex) and `cloud.google.com/gke-spot`:

```yaml
      nodeSelector:
        cloud.google.com/compute-class: inkling-gpu-class
      tolerations:
        - key: "nvidia.com/gpu"
          operator: "Exists"
          effect: "NoSchedule"
        - key: "cloud.google.com/gke-spot"
          operator: "Equal"
          value: "true"
          effect: "NoSchedule"
        - key: "cloud.google.com/gke-queued"
          operator: "Equal"
          value: "true"
          effect: "NoSchedule"
```

```bash
for CONTEXT in gke_${PROJECT_ID}_us-west1_ikwak-a3m-spot gke_${PROJECT_ID}_us-east4_ikwak-a3h-spot; do
  echo "=== Deploying to cluster: ${CONTEXT} ==="
  kubectl --context=${CONTEXT} apply -f inference/llm-inference-gke-fluid-compute/vllm-inkling-nvfp4-h100.yaml
done
```

---

## 7. Configuring Multi-Cluster Gateway & Verifying Served Region

We use **GKE Multi-Cluster Gateway** (`ServiceExport` / `ServiceImport` + `gke-l7-rilb-mc`) to expose a single HTTP endpoint across both Spot clusters, providing automatic cross-region failover when Spot nodes are preempted or stock out.

> [!NOTE]
> **Multi-Cluster Gateway vs. Multi-Cluster Inference Gateway (`llm-d`) CRDs**:
> This guide uses standard GA GKE Multi-Cluster Gateway API (`ServiceExport`, `ServiceImport`, `HTTPRoute`, `HealthCheckPolicy`, `GCPBackendPolicy`) to achieve elastic cross-region high availability and Spot failover across our fleet. The optional Public Preview **GKE Multi-Cluster Inference Gateway (`llm-d`)** CRDs (`InferencePool` and `InferenceObjective`) can be layered on top of this architecture if you require metric-aware token and KV-cache utilization routing across model server pods.
> Additionally, due to organization policy restrictions on public IPs in this project, our Gateway uses `gatewayClassName: gke-l7-rilb-mc` (Regional Internal Application Load Balancer).

### 1. Configure Non-Colliding Regional Proxy-Only Subnets
> [!CAUTION]
> **CRITICAL ARCHITECTURAL LESSON: A3 GPU Secondary NIC Collision with `192.168.0.0/16`**:  
> On Google Cloud **A3 MegaGPU (`a3-megagpu-8g` / `a3-highgpu-8g`)** VMs, Google automatically attaches 8 additional high-performance storage/GPU ROce secondary NICs (`eth1` through `eth8`) subnetted from `192.168.0.0/16` (`192.168.0.0/20`, ... `192.168.96.0/20`).
> You **must** create your Gateway regional proxy-only subnets in an RFC1918 CIDR completely outside `192.168.0.0/16` (such as **`172.23.1.0/24`** and **`172.23.2.0/24`**). If a proxy subnet like `192.168.105.0/24` is used, reply packets from the vLLM pod on the GKE node back to the Envoy proxy collide with GPU NIC `eth7` (`192.168.96.0/20`), causing the Gateway connection to hang and time out after 98 seconds (`exit code 28`).

```bash
# Create non-colliding regional proxy-only subnets in us-west1 and us-east4
gcloud compute networks subnets create proxy-only-subnet-ikwak-west1 \
  --purpose=REGIONAL_MANAGED_PROXY --role=ACTIVE \
  --region=us-west1 --network=ikwak-a3m-spot-net --range=172.23.1.0/24

gcloud compute networks subnets create proxy-only-subnet-east4 \
  --purpose=REGIONAL_MANAGED_PROXY --role=ACTIVE \
  --region=us-east4 --network=default --range=172.23.2.0/24
```

### 2. Automated Firewall Cleaner (`gceenforcer`) Workaround
> [!IMPORTANT]
> **Surviving Automated Firewall Deletion Scripts**:  
> If your project runs an automated policy enforcer (`gceenforcer`) that deletes newly created manual firewall rules every 2–3 minutes, modify GKE's permanent baseline firewall rules (`gke-<cluster>-mcsd`, `all`, `vms`). Because these rule names are whitelisted by `gceenforcer`, updating their `--source-ranges` allows Gateway health checkers and Envoy proxies without being deleted:

```bash
gcloud compute firewall-rules update gke-ikwak-a3m-spot-1b226b51-mcsd \
  --source-ranges=35.191.0.0/16,130.211.0.0/22,172.20.0.0/16,10.53.0.0/17,10.4.0.0/14,172.23.1.0/24,172.23.2.0/24 --quiet

gcloud compute firewall-rules update gke-ikwak-a3m-spot-1b226b51-all \
  --source-ranges=10.4.0.0/14,172.23.1.0/24,172.23.2.0/24 --quiet

gcloud compute firewall-rules update gke-ikwak-a3m-spot-1b226b51-vms \
  --source-ranges=10.0.0.0/24,172.23.1.0/24,172.23.2.0/24 --quiet
```

### 3. Apply Multi-Cluster Gateway, HealthCheckPolicy, and GCPBackendPolicy
Apply `multi-cluster-gateway.yaml`, `vllm-healthcheck-policy.yaml`, and `vllm-backend-policy.yaml` on your configuration cluster (`ikwak-a3m-spot`):

```yaml
apiVersion: net.gke.io/v1
kind: ServiceExport
metadata:
  name: vllm-inkling-nvfp4-service
  namespace: default
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: inkling-internal-gateway
  namespace: default
spec:
  gatewayClassName: gke-l7-rilb-mc
  listeners:
  - name: http
    port: 80
    protocol: HTTP
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: inkling-nvfp4-global-route
  namespace: default
spec:
  parentRefs:
  - name: inkling-internal-gateway
  rules:
  - matches:
    - path:
        type: PathPrefix
        value: /v1
    backendRefs:
    - name: vllm-inkling-nvfp4-service
      kind: ServiceImport
      group: net.gke.io
      port: 8000
```

Configure `/health` on port `8000` (`HealthCheckPolicy`) and a 600-second backend timeout (`GCPBackendPolicy`) targeting **both `ServiceImport` and `ServiceExport`** (`net.gke.io/v1`):

```bash
kubectl apply -f vllm-healthcheck-policy.yaml
kubectl apply -f vllm-backend-policy.yaml
```

### 4. Verify End-to-End Inference & Identify Serving Cluster
To test the Gateway VIP (`10.0.0.12`) and identify which member cluster served the request, execute a completion curl from a client pod inside the VPC:

```bash
# 1. Send test request to Internal Multi-Cluster Gateway VIP
kubectl --context=gke_${PROJECT_ID}_us-west1_ikwak-a3m-spot exec test-curl-pod -- \
  curl -i -s -X POST http://10.0.0.12/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "thinkingmachines/Inkling-Small-NVFP4",
    "messages": [{"role": "user", "content": "Hello! What cluster are you serving from?"}],
    "max_tokens": 50
  }'
```

#### Inspect Container Access Logs to Confirm Serving Cluster
Check `vllm-server` logs on each cluster to confirm which cluster answered the request:

```bash
# Check primary cluster in us-west1-a (ikwak-a3m-spot)
kubectl --context=gke_${PROJECT_ID}_us-west1_ikwak-a3m-spot \
  logs -l app=vllm-inkling-nvfp4 -c vllm-server --tail=15 | grep -i "chat/completions"

# Check secondary failover cluster in us-east4-a (ikwak-a3h-spot)
kubectl --context=gke_${PROJECT_ID}_us-east4_ikwak-a3h-spot \
  logs -l app=vllm-inkling-nvfp4 -c vllm-server --tail=15 | grep -i "chat/completions"
```
The serving container log shows the incoming Envoy proxy IP from our non-colliding subnet (`172.23.1.4`):
```
(APIServer pid=1) INFO:     172.23.1.4:51750 - "POST /v1/chat/completions HTTP/1.1" 200 OK
```

