This guide walks through how you can architect a LLM serving platform using diverse GPU consumption types to maximize cost efficiency and increasing capacity obtainability, whilst minimizing workload disruptions through fault tolerant architecture.

For our example model, we will be using Thinking Machines Lab's new [Inkling-Small](https://huggingface.co/thinkingmachines/Inkling-Small-NVFP4) model in NVFP4 quantized weights. If you'd like to run in BF16 format or with larger context windows, it's highly recommended to run the model in multi-node deployment or on accelerators with larger HBM memory per node such as A3 Ultra (H200), A4 High (B200), A4X (GB200) or A5X (GB300).

**Note**
This architectural guide is model agnostic and the pattern can be applied to any LLMs

## **1. Finding GPU Capacity Using Capacity Advisor**

Before provisioning our GPU clusters, we can use Google Cloud Console's **[Capacity Advisor](https://console.cloud.google.com/compute/capacityAdvisor)** to plan our deployment by identifying regions and zones with available accelerator resources. In this example, we will be using Spot VM as the primary method to scale our cluster (complemented by other consumption types as fallbacks for capacity diversification), where we can use Capacity Advisor to see the capacity availability, compare historical preemption rates, and evaluate hourly Spot pricing.

### **Analyzing Console Insights (Example Trade-off: `us-west1` vs. `us-east4`)**

When selecting an A3 Mega Spot footprint (`1 × a3-megagpu-8g` per cluster = 8 GPUs), Capacity Advisor reveals significant regional pricing and stability trade-offs:

#### **`us-west1` Spot Capacity Advisor (High Stability: `0-5%` Preemption Rate — `$53.35 / VM / hr`)**
![](upload://7UUYXbhhWx5Pqylri5rVz6WKxfJ.jpeg)

#### **`us-east4` Spot Capacity Advisor (Cost-Optimized: `6-10%` Preemption Rate — `$21.70 / VM / hr`)**
![](upload://ky0HYkRFSLCkeH9eukjXUzD35Hk.jpeg)

### **Summary of Regional Trade-offs**

| **Region** | **Available Zonal Capacity** | **Historical Preemption Rate** | **Hourly Cost per A3 Mega VM (`8 × H100`)** | **Strategic Recommendation** |
|----|----|----|----|----|
| **`us-west1`** | `us-west1-a` (High)<br>`us-west1-b` (Limited) | **`0 - 5%`** *(Very Low)* | **`$53.352` / hr** | **High-Stability Backup Fleet**: Low preemption risk, ideal for elastic failover. |
| **`us-east4`** | `us-east4-a` (High)<br>`us-east4-c` (High) | **`6 - 10%`** *(Moderate)* | **`$21.700` / hr** | **Cost-Optimized Primary Fleet**: **59.3% cheaper hourly cost** (`$21.70/hr` vs `$53.35/hr`), ideal for primary serving. |

Using this example, we can see that the us-east4 pricing represents a significant cost savings albeit at a higher preemption rate vs us-west1. However, we can get the best of both worlds by architecting our GKE inference platform to use multi-region elastic strategy to automatically failover between regions to achieve optimal cost efficiency whilst minimizing workload disruptions from Spot pre-emptions.

Alternatively, we can also use an chat interface with [Compute Advisor](https://console.cloud.google.com/compute/advisor/) to find real-time obtainability of Spot and DWS-Flex accelerators across regions, verify project quotas are sufficient and get example commands to deploy the accelerators.
![](upload://pZCWlf0LKz3QarE0nXVJhg6px9d.webp)

**Note**
If you require multi node serving for bigger LLMs, it's recommended to use VMs with higher east-west VM bandwidth such as A3 mega vs A3 high (both come with 8 x H100 SXM, but have 1600 Gbps and 800 Gbps respectively) for better throughput and latency


## **2. Solution Architecture**
![](upload://vdqgslSfE8W8c996BqzwaQDyWli.png)

### **Key Architectural Components**

* **Storage**: Combining multi-region buckets with Zonal Rapid Cache guarantees cross-region data consistency while accelerating model downloads. By serving models directly from local caches, this architecture delivers lower latency and higher concurrency to your compute nodes, significantly boosting scheduling efficiency and workload goodput. For peak download performance, we recommend using the **[Run:AI model streamer](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/run-ai-model-streamer)** or **GCSFuse with parallel downloads on Rapid Cache**. This setup achieves a **>90% read cache hit rate** once weights are cached, drastically reducing multi-region egress costs until the next model refresh.
* **GKE Platform**: By architecting our serving fleet with multi regional clusters, custom compute class and extended shutdown period for Spot VMs, we are able to increase elasticity, capacity obtainability and workload resiliency whilst achieving cost efficiency. By integrating inference gateway, we also optimize performance with KV cache aware routing and fault tolerance with automatic cluster failovers in case of compute node preemptions or capacity stock outs.
* **Fluid Compute**: By leveraging Capacity Advisor and Compute Advisor, we can plan and monitor our deployments through real-time obtainability insights and make better understand architectural tradeoffs between cost/perf efficiency vs historical preemption rates through the console UI or build programmatic workflows using [Capacity Advisor API](https://docs.cloud.google.com/compute/docs/reference/rest/beta/advice/capacityHistory). By leveraging custom compute class, we can increase capacity assurance through diversifying across multiple capacity pools via consumption types (reservation, spot, DWS flex, on demand) with fine grained customization on scale out priority and time to fall back to next priority order.

## **3. Creating a Multi-Region GCS Bucket & Pre-Uploading Checkpoints**

### **Create the US Multi-Region Bucket (with Standard / Rapid Cache Storage Class)**

```
export MULTI_REGION_BUCKET="multi-region-inkling-cache-${USER}"

# Create a US multi-region GCS bucket
gcloud storage buckets create gs://${MULTI_REGION_BUCKET} \
  --location=US \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access
```

### **Download Model Locally & Upload to Bucket Root**

To ensure `--model=/bucket/Inkling-Small-NVFP4` resolves correctly inside vLLM containers:

```
# Download Inkling-Small-NVFP4 checkpoint locally without symlinks
export HF_TOKEN="<yourHFToken>"
hf download thinkingmachines/Inkling-Small-NVFP4 \
  --local-dir ./Inkling-Small-NVFP4

# Upload directly to the GCS bucket root
gcloud storage cp -r ./Inkling-Small-NVFP4 gs://${MULTI_REGION_BUCKET}/Inkling-Small-NVFP4
```

## **4. Accelerating Multi-Region Bucket Reads with GKE Cloud Storage FUSE Profiles (`gcsfusecsi-serving` & Zonal Rapid Cache)**

We use a **Multi-Region GCS Bucket**  as a single global namespace for our `Inkling-Small-NVFP4` model weights with Zonal Caches to ensure our data is colocated in close proximity with our compute cluster for optimal performance. To achieve sub-millisecond TTFB and up to **2.5 TB/s zonal SSD throughput** without paying cross-region data transfer fees, we combine **Zonal Rapid Cache** with [**GKE Cloud Storage FUSE Profiles**](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gcsfuse-profiles) for optimal throughput configs out of the box.

### **Create Zonal Rapid Caches Across Compute Zones**

Using [gcloud cli](https://docs.cloud.google.com/storage/docs/rapid/use-rapid-cache#command-line), create SSD-backed zonal read caches on the multi-region bucket across our member cluster zones (`us-west1-a` and `us-east4-a`):
```
gcloud storage buckets anywhere-caches create gs://${MULTI_REGION_BUCKET} \
  us-west1-a \
  us-east4-a \
  --ttl=604800 \
  --admission-policy=ADMIT_ON_FIRST_MISS
```
* **`--ttl=604800`**: Sets a 7-day Time to Live for static LLM weights.
* **`ADMIT_ON_FIRST_MISS`**: The first pod read in each zone automatically ingests and SSD-caches the model chunks locally.

### **Grant Workload Identity & FUSE Profile Permissions**

Bind `roles/storage.objectViewer` to your GKE Workload Identity service account on the multi-region bucket:
```
gcloud storage buckets add-iam-policy-binding gs://${MULTI_REGION_BUCKET} \
  --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT_ID}.svc.id.goog/subject/ns/default/sa/default" \
  --role="roles/storage.objectViewer"
```

### **Deploy Workload with `gcsfusecsi-serving` Profile**

In place of manual FUSE cache tuning, our deployment manifest defines a static PersistentVolume (`PV`) and PersistentVolumeClaim (`PVC`) using `storageClassName: gcsfusecsi-serving`:

```
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
    volumeHandle: <yourGCSBucket>
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

### **Verifying Profile & Local Cache Usage in Container Logs**

To verify that `gcsfusecsi-serving` applied profile optimizations and is utilizing the cache, inspect the `gke-gcsfuse-sidecar` container logs:

```
kubectl logs -c gke-gcsfuse-sidecar -l app=vllm-inkling-nvfp4 --tail=20
```

#### **Expected Log Verification Output:**

```
{"severity":"INFO","message":"GCSFuse Config","Applied optimizations for bucket-type: ":"flat","Full Config":{"metadata-cache.ttl-secs":{"final_value":-1,"optimization_reason":"profile \"aiml-serving\""}}}
{"severity":"INFO","message":"Mounting file system \"myGCSBucket\"..."}
{"severity":"INFO","message":"File system has been successfully mounted."}
```

* Notice `"optimization_reason":"profile \"aiml-serving\""` confirming that GKE FUSE Profile automated tuning is active.

## **5. Provisioning Multi-Cluster GPU Pools with Elastic Cross-Region High Availability**

In this section, we will be creating multiple GKE clusters with the following primitives to increase our serving capacity elasticity, cost/perf efficency and minimize workload disruptions:

- [Custom Compute Class](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/about-custom-compute-classes): Allows GKE autoscaler to automate scaling behaviors via compute (nodepool) priorities
- [Extended Graceful Node Shutdown](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/spot-vms#termination-graceful-shutdown): Extends the default 30 seconds preemption signal for Spot VMs up to 120 seconds to allow more node replacement runway time for GKE to cordon the preempting node and scale up a replacement node defined in Custom Compute Class
- [Multi-Cluster GKE Inference Gateway](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/about-multi-cluster-inference-gateway): Allows a centralized traffic management and intelligent load balancing across our clusters with llm-d EPP router

### **Create Custom Compute Class & Extended KubeletConfig for Spot A3 Mega**

Apply this `CustomComputeClass` and `KubeletConfig` manifest in both clusters to define automatic zone/region fallback rules and enforce the **120-second graceful shutdown period**:

```
# kubelet-config.yaml
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
  name: inkling-gpu-class
spec:
  nodePools:
  - name: spot-a3-mega-pool # also apply to a3-highgpu-8g nodepool if configuring both types
    machineType: a3-megagpu-8g
    spot: true
    scaling:
      minNodeCount: 1
      maxNodeCount: 4
```

### **Create the Regional GKE Clusters & Spot Node Pools**

To explicitly configure the 120-second graceful shutdown window on node pool creation:

```
# Primary Cluster (us-east4-a) with lower price and higher preemption rate
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

# Secondary Cluster (us-west1-a) with higher spot price but lower preemption rate
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
```

## **6. Configuring Custom Compute Class on GKE**

We use GKE Custom Compute Classes to define an intelligent, multi-tier fallback priority for GPU capacity across our regions:

1. **Priority 1**: `a3-highgpu-8g` Specific Reservation (`myh100cud`) — instant capacity check against existing committed capacity
2. **Priority 2**: `a3-highgpu-8g` Spot VM (`--spot`) — instant capacity check against Spot pools
3. **Priority 3**: `a3-highgpu-8g` DWS Flex Start (`flexStart: enabled: true`, 300s queue wait time) — discounted up to 53% off on-demand, up to 7-day duration
4. **Priority 4**: `a3-megagpu-8g` DWS Flex Start (`flexStart: enabled: true`, 300s queue wait time)
5. **Priority 5**: `a3-highgpu-8g` On-Demand VM (standard fallback)

#### Apply the custom compute class config to both clusters to configure our scaling behaviors

```
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

### **Enable Node Auto-Provisioning (NAP) with H100 & H100 Mega Limits**

Optionally, we can allow GKE to dynamically auto-provision fallback node pools for either `a3-highgpu-8g` (`nvidia-h100-80gb`) or `a3-megagpu-8g` (`nvidia-h100-mega-80gb`) by configuring NAP resource limits on both clusters:

```
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
## **6. Deploying vLLM Inkling-Small-NVFP4 workload pods**

For each cluster, apply the deployment manifest to ensure pods are scheduled using our Custom Compute Class, our Zonal Cache enabled GCS buckets with the weights and tolerations for Spot and DWS-Flex.

```
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
    volumeHandle: <myGCSBucket>
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
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-inkling-nvfp4-h100
  namespace: default
  labels:
    app: vllm-inkling-nvfp4
    llm-d.ai/guide: spot-h100-nvfp4
spec:
  replicas: 1
  selector:
    matchLabels:
      app: vllm-inkling-nvfp4
  template:
    metadata:
      labels:
        app: vllm-inkling-nvfp4
      annotations:
        gke-gcsfuse/volumes: "true"
        gke-gcsfuse/memory-limit: "25000Mi"
    spec:
      serviceAccountName: default
      containers:
      - name: vllm-server
        image: vllm/vllm-openai:nightly
        imagePullPolicy: IfNotPresent
        command: ["python3", "-m", "vllm.entrypoints.openai.api_server"]
        args:
          - "--model=/bucket/Inkling-Small-NVFP4"
          - "--served-model-name=thinkingmachines/Inkling-Small-NVFP4"
          - "--trust-remote-code"
          - "--tokenizer-mode=inkling"
          - "--kernel-config.enable_flashinfer_autotune=False"
          - "--enable-expert-parallel"
          - "--tensor-parallel-size=8"
          - "--max-model-len=131072"
          - "--gpu-memory-utilization=0.85"
          - "--enable-auto-tool-choice"
          - "--tool-call-parser=inkling"
          - "--reasoning-parser=inkling"
          - "--host=0.0.0.0"
          - "--port=8000"
        env:
          - name: NCCL_TUNER_PLUGIN
            value: "none"
          - name: NCCL_NET_PLUGIN
            value: "none"
          - name: NCCL_NET
            value: "Socket"
          - name: NCCL_NET_GDR_LEVEL
            value: "0"
          - name: GLOO_SOCKET_IFNAME
            value: "eth0"
          - name: NCCL_SOCKET_IFNAME
            value: "eth0"
          - name: PYTHONUNBUFFERED
            value: "1"
          - name: PYTHONFAULTHANDLER
            value: "1"
          - name: VLLM_LOGGING_LEVEL
            value: "INFO"
          - name: VLLM_USE_V2_MODEL_RUNNER
            value: "1"
          - name: FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED
            value: "1"
          - name: HF_TOKEN
            valueFrom:
              secretKeyRef:
                name: hf-secret # Define your HF secret on your GKE clusters
                key: token
                optional: true
        ports:
          - containerPort: 8000
            name: http
        resources:
          limits:
            nvidia.com/gpu: 8
            memory: "1500Gi"
            cpu: "80"
          requests:
            nvidia.com/gpu: 8
            memory: "1000Gi"
            cpu: "60"
        securityContext:
          capabilities:
            add:
              - SYS_PTRACE
              - IPC_LOCK
        volumeMounts:
          - name: dshm
            mountPath: /dev/shm
          - name: gcs-fuse-csi-eph
            mountPath: /bucket
            readOnly: true
          - name: hf-cache
            mountPath: /root/.cache/huggingface
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 60
          periodSeconds: 15
          timeoutSeconds: 5
      volumes:
        - name: dshm
          emptyDir:
            medium: Memory
            sizeLimit: 250Gi
        - name: hf-cache
          hostPath:
            path: /mnt/stateful_partition/kube-ephemeral-ssd/huggingface_cache
            type: DirectoryOrCreate
        - name: gcs-fuse-csi-eph
          persistentVolumeClaim:
            claimName: gcsfuse-serving-pvc
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
        - key: "cloud.google.com/gke-queued" # This ensures DWS Flex nodes are provisioned
          operator: "Equal"
          value: "true"
          effect: "NoSchedule"
---
apiVersion: v1
kind: Service
metadata:
  name: vllm-inkling-nvfp4-service
  namespace: default
  labels:
    app: vllm-inkling-nvfp4
spec:
  type: ClusterIP
  ports:
    - port: 8000
      targetPort: 8000
      name: http
  selector:
    app: vllm-inkling-nvfp4
```

## **7. Configuring Multi-Cluster Gateway & Verifying Served Region**

Finally, we use **GKE Multi-Cluster Gateway** to expose a single HTTP endpoint across both Spot clusters, providing automatic cross-region failover when Spot or DWS nodes nodes are preempted or unable to scale up due to capacity stock out.

**Note:** This guide implements the **[llm-d Well-Lit Paths](https://llm-d.ai/docs/guides)** architecture on GKE using **Gateway API Inference Extension (GAIE)** (`InferencePool` + Endpoint Picker EPP Router pod) layered with GKE Multi-Cluster Gateway (`ServiceExport` / `ServiceImport`). This combines intelligent KV-cache utilization and queue-depth aware scheduling across model server pods with elastic cross-region Spot failover across our fleet. 

### **Configure Non-Colliding Regional Proxy-Only Subnets**

To deploy a GKE Regional Internal Application Load Balancer (`gke-l7-rilb-mc`), we must create a proxy-only subnet in each region that hosts the managed Envoy proxy instances. We allocate these subnets in RFC1918 ranges outside `192.168.0.0/16` (`172.23.1.0/24` and `172.23.2.0/24`) to ensure clean routing across all regional cluster VPCs.

Alternatively you can create an External Application Load Balancer if you want to expose this as a public endpoint.

```
# Create non-colliding regional proxy-only subnets in us-west1 and us-east4
gcloud compute networks subnets create proxy-only-subnet-ikwak-west1 \
  --purpose=REGIONAL_MANAGED_PROXY --role=ACTIVE \
  --region=us-west1 --network=ikwak-a3m-spot-net --range=172.23.1.0/24

gcloud compute networks subnets create proxy-only-subnet-east4 \
  --purpose=REGIONAL_MANAGED_PROXY --role=ACTIVE \
  --region=us-east4 --network=default --range=172.23.2.0/24
```

### **Deploy llm-d Gateway API Inference Extension (`GAIE` + `EPP Router`)**

Install the upstream GAIE custom resource definitions (`InferenceObjective` and `InferenceModelRewrite`) and deploy the `InferencePool` (`gaie`) with its Endpoint Picker (EPP Router) using Helm and `gaie-values.yaml`:

```
# 1. Install upstream GAIE CRDs
kubectl apply -f https://raw.githubusercontent.com/kubernetes-sigs/gateway-api-inference-extension/v1.2.0-rc.1/config/crd/bases/inference.networking.x-k8s.io_inferenceobjectives.yaml
kubectl apply -f https://raw.githubusercontent.com/kubernetes-sigs/gateway-api-inference-extension/v1.2.0-rc.1/config/crd/bases/inference.networking.x-k8s.io_inferencemodelrewrites.yaml

# 2. Deploy InferencePool and EPP Router via Helm
helm upgrade --install gaie oci://registry.k8s.io/gateway-api-inference-extension/charts/inferencepool \
  --version v1.2.0-rc.1 \
  --namespace default \
  -f gaie-values.yaml
```

### **Apply Multi-Cluster Gateway, HTTPRoute (Targeting InferencePool `gaie`), and Policies**

Apply `multi-cluster-gateway.yaml`, `vllm-healthcheck-policy.yaml`, and `vllm-backend-policy.yaml` on your configuration cluster (`ikwak-a3m-spot`). Notice how `HTTPRoute` references `kind: InferencePool, name: gaie` so requests are intelligently scheduled by the EPP Router:

```
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

```
apiVersion: networking.gke.io/v1
kind: HealthCheckPolicy
metadata:
  name: vllm-health-check-policy-import
  namespace: default
spec:
  default:
    config:
      type: HTTP
      httpHealthCheck:
        requestPath: /health
        port: 8000
  targetRef:
    group: net.gke.io
    kind: ServiceImport
    name: vllm-inkling-nvfp4-service
---
apiVersion: networking.gke.io/v1
kind: HealthCheckPolicy
metadata:
  name: vllm-health-check-policy-export
  namespace: default
spec:
  default:
    config:
      type: HTTP
      httpHealthCheck:
        requestPath: /health
        port: 8000
  targetRef:
    group: net.gke.io
    kind: ServiceExport
    name: vllm-inkling-nvfp4-service
```

```
apiVersion: networking.gke.io/v1
kind: GCPBackendPolicy
metadata:
  name: vllm-backend-policy-import
  namespace: default
spec:
  default:
    timeoutSec: 600
  targetRef:
    group: net.gke.io
    kind: ServiceImport
    name: vllm-inkling-nvfp4-service
---
apiVersion: networking.gke.io/v1
kind: GCPBackendPolicy
metadata:
  name: vllm-backend-policy-export
  namespace: default
spec:
  default:
    timeoutSec: 600
  targetRef:
    group: net.gke.io
    kind: ServiceExport
    name: vllm-inkling-nvfp4-service
```

### **8. Verify End-to-End Inference & Identify Serving Cluster**

To test the Gateway VIP (`10.0.0.12`) and identify which member cluster served the request, execute a completion curl from a client pod inside the VPC:

```
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
#### **Inspect Container Access Logs to Confirm Serving Cluster**

To verify which regional cluster answered the request, we can check the `vllm-server` logs on each cluster.

```
# Check primary cluster in us-west1-a (ikwak-a3m-spot)
kubectl --context=gke_${PROJECT_ID}_us-west1_ikwak-a3m-spot \
  logs -l app=vllm-inkling-nvfp4 -c vllm-server --tail=15 | grep -i "chat/completions"

# Check secondary failover cluster in us-east4-a (ikwak-a3h-spot)
kubectl --context=gke_${PROJECT_ID}_us-east4_ikwak-a3h-spot \
  logs -l app=vllm-inkling-nvfp4 -c vllm-server --tail=15 | grep -i "chat/completions"
```

The serving container log shows the incoming Envoy proxy IP from our non-colliding subnet (`172.23.1.4`), which is from the us-west1 regional cluster:

```
(APIServer pid=1) INFO:     172.23.1.4:51750 - "POST /v1/chat/completions HTTP/1.1" 200 OK

```
