# Deploying LLM inference on GKE using Fluid Compute

This guide walks through how you can architect a LLM serving platform using diverse GPU consumption types to maximize cost efficiency and increasing capacity obtainability, whilst minimizing workload disruptions through fault tolerant architecture.

For our example model, we will be using Thinking Machines Lab's new [Inkling-Small](https://huggingface.co/thinkingmachines/Inkling-Small-NVFP4) model in NVFP4 quantized weights. If you'd like to run in BF16 format or with larger context windows, it's highly recommended to run the model in multi-node deployment or on accelerators with larger HBM memory per VM such as H200, B200 or GB200.

> [!NOTE]
> This architectural guide will apply to any ML models being served on accelerators and recommendations are model agnostic

---

## 1. Finding Spot GPU Capacity Using Capacity Advisor

Before provisioning GPU clusters, use Google Cloud Console's **[Capacity Advisor](https://console.cloud.google.com/compute/capacityAdvisor)** to identify regions and zones with high Spot VM availability, compare historical preemption rates, and evaluate hourly Spot pricing for `a3-megagpu-8g` (8 × H100 SXM5 GPUs per node).

### Analyzing Console Insights (Example Trade-off: `us-west1` vs. `us-east4`)

When selecting an A3 Mega Spot footprint (`1 × a3-megagpu-8g` per cluster = 8 GPUs), Capacity Advisor reveals significant regional pricing and stability trade-offs:

#### 1. `us-west1` Spot Capacity Advisor (High Stability: `0-5%` Preemption Rate — `$53.35 / VM / hr`)
![Capacity Advisor - us-west1 Spot VM Availability, 0-5% Preemption Rate, $53.35/hr](images/capacity-advisor-us-west1.png)

#### 2. `us-east4` Spot Capacity Advisor (Cost-Optimized: `6-10%` Preemption Rate — `$21.70 / VM / hr`)
![Capacity Advisor - us-east4 Spot VM Availability, 6-10% Preemption Rate, $21.70/hr](images/capacity-advisor-us-east4.png)

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

### Key Architectural Components
- **Storage**: Combining multi-region buckets with Zonal Rapid Cache guarantees cross-region data consistency while accelerating model downloads. By serving models directly from local caches, this architecture delivers lower latency and higher concurrency to your compute nodes, significantly boosting scheduling efficiency and workload goodput. For peak download performance, we recommend using the *[Run:AI model streamer](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/run-ai-model-streamer)* or *GCSFuse with parallel downloads on Rapid Cache*. This setup achieves a *>90% read cache hit rate* once weights are cached, drastically reducing multi-region egress costs until the next model refresh.
- **GKE Platform**: By architecting our serving fleet with multi regional clusters, custom compute class and extended shutdown period for Spot VMs, we are able to increase elasticity, capacity obtainability and workload resiliency whilst achieving cost efficiency. By integrating inference gateway, we also optimize performance with KV cache aware routing and fault tolerance with automatic cluster failovers in case of compute node preemptions or capacity stock outs.
- **Fluid Compute**: By leveraging Capacity Advisor and Compute Advisor, we can plan and monitor our deployments through real-time obtainability insights and make better understand architectural tradeoffs between cost/perf efficiency vs historical preemption rates through the console UI or build programmatic workflows using [Capacity Advisor API](https://docs.cloud.google.com/compute/docs/reference/rest/beta/advice/capacityHistory). By leveraging custom compute class, we can increase capacity assurance through diversifying across multiple consumption types (reservation, spot, DWS flex, on demand) with fine grained customization on scale out priority and time to fall back to next priority order.

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
  --addons=GcsFuseCsiDriver \
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
  --addons=GcsFuseCsiDriver \
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
> **Integrating GKE Multi-Cluster Gateway with llm-d Well-Lit Paths (`GAIE` + `EPP Router`)**:
> This guide implements the **[llm-d Well-Lit Paths](https://llm-d.ai/docs/guides)** architecture on GKE using **Gateway API Inference Extension (GAIE)** (`InferencePool` + Endpoint Picker EPP Router pod) layered with GKE Multi-Cluster Gateway (`ServiceExport` / `ServiceImport`). This combines intelligent KV-cache utilization and queue-depth aware scheduling across model server pods with elastic cross-region Spot failover across our fleet.
> Additionally, due to organization policy restrictions on public IPs in this project, our Gateway uses `gatewayClassName: gke-l7-rilb-mc` (Regional Internal Application Load Balancer).

### 1. Configure Non-Colliding Regional Proxy-Only Subnets

To deploy a GKE Regional Internal Application Load Balancer (`gke-l7-rilb-mc`), we must create a proxy-only subnet in each region that hosts the managed Envoy proxy instances. We allocate these subnets in RFC1918 ranges outside `192.168.0.0/16` (`172.23.1.0/24` and `172.23.2.0/24`) to ensure clean routing across all regional cluster VPCs.

```bash
# Create non-colliding regional proxy-only subnets in us-west1 and us-east4
gcloud compute networks subnets create proxy-only-subnet-ikwak-west1 \
  --purpose=REGIONAL_MANAGED_PROXY --role=ACTIVE \
  --region=us-west1 --network=ikwak-a3m-spot-net --range=172.23.1.0/24

gcloud compute networks subnets create proxy-only-subnet-east4 \
  --purpose=REGIONAL_MANAGED_PROXY --role=ACTIVE \
  --region=us-east4 --network=default --range=172.23.2.0/24
```

### 2. Deploy llm-d Gateway API Inference Extension (`GAIE` + `EPP Router`)
Install the upstream GAIE custom resource definitions (`InferenceObjective` and `InferenceModelRewrite`) and deploy the `InferencePool` (`gaie`) with its Endpoint Picker (EPP Router) using Helm and `gaie-values.yaml`:

```bash
# 1. Install upstream GAIE CRDs
kubectl apply -f https://raw.githubusercontent.com/kubernetes-sigs/gateway-api-inference-extension/v1.2.0-rc.1/config/crd/bases/inference.networking.x-k8s.io_inferenceobjectives.yaml
kubectl apply -f https://raw.githubusercontent.com/kubernetes-sigs/gateway-api-inference-extension/v1.2.0-rc.1/config/crd/bases/inference.networking.x-k8s.io_inferencemodelrewrites.yaml

# 2. Deploy InferencePool and EPP Router via Helm
helm upgrade --install gaie oci://registry.k8s.io/gateway-api-inference-extension/charts/inferencepool \
  --version v1.2.0-rc.1 \
  --namespace default \
  -f gaie-values.yaml
```

### 3. Apply Multi-Cluster Gateway, HTTPRoute (Targeting InferencePool `gaie`), and Policies
Apply `multi-cluster-gateway.yaml`, `vllm-healthcheck-policy.yaml`, and `vllm-backend-policy.yaml` on your configuration cluster (`ikwak-a3m-spot`). Notice how `HTTPRoute` references `kind: InferencePool, name: gaie` so requests are intelligently scheduled by the EPP Router:

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
    - name: gaie
      kind: InferencePool
      group: inference.networking.k8s.io
      weight: 1
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

