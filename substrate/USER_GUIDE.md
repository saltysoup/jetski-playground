# User guide: build, redeploy and tear down the demo

This guide does four things:
- builds the Agent Substrate × llm-d keynote demo from an empty Google Cloud project;
- brings back any part that breaks;
- updates one component at a time;
- removes everything again.

The [README](./README.md) has what the demo shows, the measured results, the architecture, the [pre-show health check](./README.md#7-pre-show-health-check) and the [stage runbook](./README.md#8-stage-runbook). The [Hermes Agent variant](./hermes/README.md) builds on Steps 0–10 of this guide.

> [!WARNING]
> This builds a demo, not a production system:
> - Postgres runs with `fsync=off`.
> - A privileged DaemonSet remounts the worker nodes' `/var` without write barriers.
> - The gVisor flags weaken isolation.
> - A file server inside `postgres-0` exposes the Postgres data directory on the pod network.
>
> Read [README §9](./README.md#9-known-issues-and-disclosures) before you reuse any of it.

**Contents:** [0. Where to start](#0-where-to-start) · [1. What you deploy](#1-what-you-deploy) · [2. Build it, step by step](#2-build-it-step-by-step) · [3. Check the whole stack](#3-check-the-whole-stack) · [4. Redeploy and recover](#4-redeploy-and-recover) · [5. Update one component](#5-update-one-component) · [6. Tear it down](#6-tear-it-down) · [7. How this guide was verified](#7-how-this-guide-was-verified)

---

## 0. Where to start

| You want to | Go to |
|---|---|
| Build everything in a new project | [Steps 0–12](#2-build-it-step-by-step), then [§3](#3-check-the-whole-stack) and the [pre-show health check](./README.md#7-pre-show-health-check) |
| Rehearse the dashboard without a cluster | [Step 12](#step-12-open-the-dashboard-or-rehearse-offline): `python3 dashboard/mock_server.py 8765` needs only Python 3 |
| Check that a running stack is healthy | [§3](#3-check-the-whole-stack) (read-only), then the [pre-show health check](./README.md#7-pre-show-health-check) |
| Fix something that broke: a vLLM or EPP restart, a lost driver pod, a `postgres-0` restart, a recreated node, a RECONNECTING dashboard | [§4](#4-redeploy-and-recover) |
| Ship a new dashboard, driver build, driver flag, gateway or llm-d config | [§5](#5-update-one-component) |
| Run the Hermes Agent variant | Steps 0–10, then [hermes/README.md §4](./hermes/README.md#4-reproduce-it) |
| Remove everything | [§6](#6-tear-it-down) |

> [!TIP]
> Every later command reads the variables set in [Step 0](#step-0-tools-quota-and-variables). In a new shell, run the Step 0 `export` block again first.

## 1. What you deploy

| Component | Where | Created in | If it breaks |
|---|---|---|---|
| VPC, subnet and firewall rule shared by both clusters | project | [Step 1](#step-1-shared-vpc) | – |
| Substrate cluster with Agent Substrate v0.1.0 (`ate-system`: ate-api, Postgres, atelet, atenet, podcertificate-controller) and the agents' ActorTemplate `sandbox-dense` | Substrate cluster | [Step 2](#step-2-substrate-cluster-and-agent-substrate) | – |
| Node pools `substrate-c4-pool` (25 × c4-standard-4) and `keynote-driver-pool` (4 × n2-standard-8); control-plane tuning; the 1,600-worker WorkerPool; the node tuner | Substrate cluster | [Step 4](#step-4-size-and-tune-the-substrate-cluster) | [§4.5](#45-a-substrate-node-was-recreated) |
| Patched ate-api and atelet, and the file server `http_srv` inside `postgres-0` that serves their binaries | Substrate, `ate-system` | [Steps 3](#step-3-build-the-patched-binaries-and-the-driver) and [5](#step-5-run-the-patched-ate-api-and-atelet) | [§4.4](#44-postgres-0-restarted) |
| TPU cluster: 1 × c4-standard-4 CPU node, 2 × ct6e-standard-4t (TPU v6e, Spot) | TPU cluster | [Step 6](#step-6-tpu-cluster) | [§4.1](#41-vllm-or-epp-pods-restarted) |
| vLLM image (vllm-torchtpu @ `3eb7abb5`, unmodified) | Artifact Registry | [Step 7](#step-7-vllm-image) | – |
| vLLM `pod-1` and `pod-2` (Gemma 4 12B, TP=4) with their PVCs, the Hugging Face token Secret and the render Service | TPU, `default` | [Step 8](#step-8-vllm-pods) | [§4.1](#41-vllm-or-epp-pods-restarted) |
| llm-d: the GAIE v1.0.1 CRDs, 3 InferenceObjectives, the endpoint picker (EPP; Helm release `gaie-pd`), the Envoy gateway and its headless Service `gemma4-12b-vllm-pods` | TPU, `default` | [Step 9](#step-9-llm-d-crds-priorities-endpoint-picker-gateway) | [§4.1](#41-vllm-or-epp-pods-restarted), [§4.3](#43-flow-control-saturation-stays-above-0-while-idle) |
| Keynote driver: dashboard and API on port 8090 | Substrate, `keynote-demo` | [Step 10](#step-10-keynote-driver-and-dashboard) | [§4.2](#42-driver-pod-deleted), [§4.6](#46-dashboard-says-reconnecting) |
| 1,000 agents, `agent-0001` … `agent-1000` | Substrate, atespace `ate-demo-sandbox` | [Step 11](#step-11-create-the-1000-agents-and-warm-them-up) | [§4.7](#47-wake-or-suspend-all-misbehaves) |

**Where the state lives, and what loses it:**
- **Postgres** (`postgres-0`, on a persistent volume): actors, templates and workers. It survives a pod restart, but `http_srv` does not ([§4.4](#44-postgres-0-restarted)).
- **Each worker node's local disk:** the paused agents' snapshots. They are lost when the node is recreated ([§4.5](#45-a-substrate-node-was-recreated)).
- **The snapshot bucket:** the template's golden snapshot, which new agents restore from.
- **The driver pod's `/work`** (an `emptyDir`): the driver binary, its flags, the dashboard page, run records, and the Hermes driver's files. They are lost when the pod is deleted ([§4.2](#42-driver-pod-deleted)).
- **The vLLM PVCs:** model weights and compile cache. A restarted vLLM pod reuses them.

---

## 2. Build it, step by step

Until 2026-10-04 these steps were README §6.0–§6.12, and they keep their numbers: former §6.N is Step N. Only Step 10 changed: one script now replaces the manual block. Each step ends with a **Check** and what it printed on the demo clusters (internal addresses replaced by placeholders).

The measured setup used:
- project `tpu-launchpad-playground`;
- VPC `ikwak-ane1-net` and subnet `ikwak-ane1-subnet`;
- zone `asia-northeast1-b`;
- clusters `ikwak-substrate-ane1` (Substrate) and `ikwak-tpu-v6e-ane1` (TPU).

Replace them with your own in Step 0.

### Step 0: Tools, quota and variables

**Tools:**
- `gcloud`, `kubectl`, `helm` 3, `git`, `python3`, `envsubst`, `gzip`, `curl`;
- Go 1.21 or newer. The upstream `go.mod` requires Go 1.27.0, and with the default `GOTOOLCHAIN=auto` the `go` command downloads it (the demo builds ran on go1.27.0);
- `gcc` with static glibc (the demo used gcc 15.2.0, Debian);
- `docker`, to build the vLLM image;
- for the mock screenshots only: Node 22 and Google Chrome.

**Quota** in one zone:
- 104 C4 vCPUs (25 × c4-standard-4 for workers, 1 × c4-standard-4 in the TPU cluster);
- 8 C3 vCPUs, only while Steps 2–4 run (`setup-gcp` creates 2 × c3-standard-4, and Step 4 deletes them);
- 32 N2 vCPUs (4 × n2-standard-8);
- 8 Spot TPU v6e chips (2 × ct6e-standard-4t);
- Hermes variant only: 288 C4D vCPUs (18 × c4d-standard-16).

**Hugging Face:** a token with access to `google/gemma-4-12B-it`.

```bash
git clone https://github.com/saltysoup/jetski-playground.git
cd jetski-playground/substrate
export REPO_DIR="$PWD"

export PROJECT_ID="<your-project>"
export PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
export REGION="asia-northeast1" ZONE="asia-northeast1-b"
export VPC_NAME="<vpc>" SUBNET_NAME="<subnet>"
export SUBSTRATE_CLUSTER="<substrate-cluster>" TPU_CLUSTER="<tpu-cluster>"
export BUCKET_NAME="ate-snapshots-${PROJECT_ID}-${SUBSTRATE_CLUSTER}"   # Substrate snapshot bucket
export SUBSTRATE_SRC="$HOME/src/substrate"    # upstream Agent Substrate checkout
export BIN_DIR="$HOME/src/keynote-bin"        # build outputs
export CTX_SUB="gke_${PROJECT_ID}_${ZONE}_${SUBSTRATE_CLUSTER}"
export CTX_TPU="gke_${PROJECT_ID}_${ZONE}_${TPU_CLUSTER}"
export PATH="${BIN_DIR}:${PATH}"              # kubectl-ate, built in Step 2, lands here
read -rsp "Hugging Face token: " HF_TOKEN && export HF_TOKEN && echo
mkdir -p "${BIN_DIR}"
```

### Step 1: Shared VPC

Both clusters share one subnet, so the agents' sandboxes can reach the gateway's node IP directly.

```bash
gcloud compute networks create "${VPC_NAME}" --project="${PROJECT_ID}" --subnet-mode=custom
gcloud compute networks subnets create "${SUBNET_NAME}" --project="${PROJECT_ID}" \
  --network="${VPC_NAME}" --region="${REGION}" --range=172.24.0.0/20 \
  --secondary-range=pods=172.28.0.0/14,services=172.24.16.0/20 \
  --enable-private-ip-google-access
gcloud compute firewall-rules create "${VPC_NAME}-allow-internal" --project="${PROJECT_ID}" \
  --network="${VPC_NAME}" --direction=INGRESS --action=ALLOW --rules=tcp,udp,icmp \
  --source-ranges=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10
```

**Check:**

```bash
gcloud compute networks subnets describe "${SUBNET_NAME}" --project="${PROJECT_ID}" --region="${REGION}" \
  --format='value(ipCidrRange,privateIpGoogleAccess,secondaryIpRanges[].rangeName.list())'
```

Expect `172.24.0.0/20  True  pods,services`. After Step 2 the list also has a range that GKE added for the Substrate cluster's pods (`gke-<cluster>-pods-<id>`).

### Step 2: Substrate cluster and Agent Substrate

```bash
git clone https://github.com/agent-substrate/substrate.git "${SUBSTRATE_SRC}"
cd "${SUBSTRATE_SRC}" && git checkout fa6d949685a6318940a9a0195c867c864009b820   # tag v0.1.0

gcloud auth application-default login          # setup-gcp uses Application Default Credentials
GCE_REGION="${REGION}" CLUSTER_LOCATION="${ZONE}" CLUSTER_NAME="${SUBSTRATE_CLUSTER}" \
NETWORK="${VPC_NAME}" SUBNETWORK="${SUBNET_NAME}" GVISOR_NODE_MACHINE_TYPE=c3-standard-4 \
  go run ./tools/setup-gcp bootstrap           # APIs, cluster (2 nodes), bucket, IAM, dashboards
# The 2 x c3-standard-4 bootstrap pool is temporary: Step 4 moves the workers to 25 x c4-standard-4 and deletes it.
# setup-gcp doesn't set a boot disk type, so it keeps its default (C3) machine type here.
gcloud container clusters get-credentials "${SUBSTRATE_CLUSTER}" --zone="${ZONE}" --project="${PROJECT_ID}"

export KUBECTL_CONTEXT="${CTX_SUB}"
go run ./cmd/ate-setup deploy ate-system --no-dev-env \
  --image-repo us-docker.pkg.dev/gke-substrate-release/substrate --image-tag v0.1.0-gke.1
go run ./cmd/ate-setup deploy demo sandbox --no-dev-env \
  --image-repo us-docker.pkg.dev/gke-substrate-release/substrate --image-tag v0.1.0-gke.1

go build -o "${BIN_DIR}/kubectl-ate" ./cmd/kubectl-ate && export PATH="${BIN_DIR}:${PATH}"
envsubst < "${REPO_DIR}/manifests/substrate/sandbox-dense-template.yaml.tmpl" \
  | kubectl ate --context="${CTX_SUB}" create actor-template -f -
```

> [!CAUTION]
> `setup-gcp create cluster` **deletes and recreates** an existing cluster whose network or subnet differs from `NETWORK` / `SUBNETWORK`. Never re-run it against a live cluster with different values.

Upstream Agent Substrate also warns about worker node pools:
- Turn node **auto-upgrade** off on the pools that run workers: `gcloud container node-pools update substrate-c4-pool --cluster "${SUBSTRATE_CLUSTER}" --location "${ZONE}" --no-enable-autoupgrade`.
- Don't use Spot nodes for workers.
- An actor that is awake when its worker pod is killed ends up `CRASHED`.
- Here, a paused actor also loses its node-local snapshot when its node is recreated.

The demo clusters still have auto-upgrade on (see [README §9](./README.md#9-known-issues-and-disclosures)).

**Check:**

```bash
kubectl --context="${CTX_SUB}" -n ate-system get pods --no-headers | awk '{print $3}' | sort | uniq -c
```

Expect only `Running`. The count depends on the number of nodes: the demo cluster, with its Hermes pool, has 98.

### Step 3: Build the patched binaries and the driver

These commands run in the upstream checkout.

```bash
cd "${SUBSTRATE_SRC}"
git apply "${REPO_DIR}/patches/ateapi-atelet-fast-wake.patch"
gcc -O3 -static -s -o cmd/atelet/runsc_fast "${REPO_DIR}/patches/runsc_fast_sync.c"   # embedded into atelet
mkdir -p cmd/keynote_driver && cp "${REPO_DIR}"/substrate-bench/keynote_driver/*.go cmd/keynote_driver/

export CGO_ENABLED=0
go build -buildvcs=false -trimpath -ldflags="-s -w" -o "${BIN_DIR}/ateapi" ./cmd/ateapi
go build -buildvcs=false -trimpath -ldflags="-s -w" -o "${BIN_DIR}/atelet" ./cmd/atelet
go build -buildvcs=false -trimpath -o "${BIN_DIR}/keynote_driver" ./cmd/keynote_driver
gzip -9n -c "${BIN_DIR}/ateapi" > "${BIN_DIR}/bin_ateapi.gz"
gzip -9n -c "${BIN_DIR}/atelet" > "${BIN_DIR}/bin_atelet.gz"
(cd "${REPO_DIR}/manifests/substrate" && go build -trimpath -ldflags="-s -w" -o "${BIN_DIR}/http_srv" http_srv.go)
sha256sum cmd/atelet/runsc_fast "${BIN_DIR}"/{ateapi,atelet,keynote_driver,bin_ateapi.gz,bin_atelet.gz,http_srv}
```

**Check:** with go1.27.0 (linux/amd64) and gcc 15.2.0 these builds are byte-for-byte reproducible and match what runs in the demo cluster. Rebuilt from a fresh clone on 2026-10-04:

| File | sha256 (prefix) |
|---|---|
| `runsc_fast` | `403b8d3d` |
| `ateapi` | `c53a6b41` |
| `atelet` | `c24d7425` |
| `keynote_driver` | `1e92f74b` (source as of 2026-10-03 late evening: retries follow the current stage) |
| `bin_ateapi.gz` | `a41a1821` |
| `bin_atelet.gz` | `1f6c88b3` |
| `http_srv` | `7211d4ce` |

Other toolchains produce different bytes but the same code. The light driver in the cluster runs `1e92f74b`. The Hermes driver there still runs `a709458a`, built from the source as of the C4 move (before the repair). Its 1,080 workers (namespace `keynote-hermes`) showed 0 restarts on 2026-10-02, when the light fleet was repaired.

### Step 4: Size and tune the Substrate cluster

```bash
cd "${REPO_DIR}"
PROJECT_ID="${PROJECT_ID}" ZONE="${ZONE}" SUBSTRATE_CLUSTER="${SUBSTRATE_CLUSTER}" \
  DRY_RUN=1 ./manifests/substrate/scale-control-plane.sh     # shows what would change
PROJECT_ID="${PROJECT_ID}" ZONE="${ZONE}" SUBSTRATE_CLUSTER="${SUBSTRATE_CLUSTER}" \
  ./manifests/substrate/scale-control-plane.sh
kubectl --context="${CTX_SUB}" apply -f manifests/substrate/ate-node-tuner.yaml   # used for the measured results; see README §9
```

**What `scale-control-plane.sh` does:** every step is idempotent. It:
1. creates `substrate-c4-pool` (25 × c4-standard-4, hyperdisk-balanced, worker label) and `keynote-driver-pool` (4 × n2-standard-8, label + taint), and cordons the bootstrap `substrate-node-pool`;
2. keeps `keynote-driver-pool` on N2, because C4 can't attach Postgres's pd-balanced volume;
3. labels the worker nodes `ate.dev/substrate-version=v0.1.0-gke.1`;
4. tunes and pins Postgres;
5. sets 1 ate-api replica with DB pool 160/64;
6. sets 4 + 4 atenet replicas;
7. sets podcert `WORKERS_PER_SIGNER=16`;
8. applies the 1,600-worker WorkerPool, then deletes the bootstrap `substrate-node-pool`.

Run it **before** the next step. It may restart `postgres-0`, and the next step starts a file server inside that pod.

**Check:**

```bash
kubectl --context="${CTX_SUB}" get nodes -L cloud.google.com/gke-nodepool --no-headers | awk '{print $NF, $2}' | sort | uniq -c
kubectl --context="${CTX_SUB}" get workerpools -A
```

Expected (the demo cluster also lists its Hermes pool and a `counter` pool):

```text
      4 keynote-driver-pool Ready
     25 substrate-c4-pool Ready
NAMESPACE          NAME                 DESIRED   REPLICAS   READY   AGE
ate-demo-sandbox   sandbox-workerpool   1600      1600       1600    …
```

On a finished cluster, a dry run of the script reports every item `unchanged`.

### Step 5: Run the patched ate-api and atelet

```bash
cd "${REPO_DIR}"
BIN_DIR="${BIN_DIR}" CTX_SUB="${CTX_SUB}" DRY_RUN=1 ./manifests/substrate/deploy-patched-binaries.sh
BIN_DIR="${BIN_DIR}" CTX_SUB="${CTX_SUB}" ./manifests/substrate/deploy-patched-binaries.sh
```

**How it works:**
1. The script starts `http_srv` inside `postgres-0`. It serves the Postgres data directory on `:18888`, which is a security problem (see [README §9](./README.md#9-known-issues-and-disclosures)).
2. It uploads `bin_ateapi.gz` / `bin_atelet.gz`, verifying the sha256.
3. It adds `fetch-bin` init containers that download them into ate-api (an emptyDir) and atelet (the node's `/var/lib/ateom-gvisor`).
4. It restarts only what changed.

After `postgres-0` restarts, re-run the script before any ate-api or atelet pod restarts. The download URL uses the pod IP ([§4.4](#44-postgres-0-restarted)).

**Check:** run the dry run again. On an up-to-date cluster it prints:

```text
=== file server: http://<postgres-0 pod IP>:18888 (inside postgres-0)
    http_srv already running (left as is)
    bin_ateapi.gz: unchanged (a41a1821…)
    bin_atelet.gz: unchanged (1f6c88b3…)
    deploy/ate-api-server spec: unchanged
    ds/atelet-v0-1-0-gke-1 spec: unchanged
=== done
```

### Step 6: TPU cluster

```bash
gcloud container clusters create "${TPU_CLUSTER}" --project="${PROJECT_ID}" --zone="${ZONE}" \
  --release-channel=regular --network="${VPC_NAME}" --subnetwork="${SUBNET_NAME}" \
  --enable-ip-alias --cluster-secondary-range-name=pods --services-secondary-range-name=services \
  --machine-type=c4-standard-4 --disk-type=hyperdisk-balanced --num-nodes=1 --gateway-api=standard
for pool in tpu-v6e-spot tpu-v6e-spot-decode; do
  gcloud container node-pools create "${pool}" --project="${PROJECT_ID}" --zone="${ZONE}" \
    --cluster="${TPU_CLUSTER}" --machine-type=ct6e-standard-4t --tpu-topology=2x2 --num-nodes=1 \
    --spot --disk-type=hyperdisk-balanced --disk-size=100
done
gcloud container clusters get-credentials "${TPU_CLUSTER}" --zone="${ZONE}" --project="${PROJECT_ID}"
```

GKE adds the `google.com/tpu=present:NoSchedule` taint to TPU nodes by itself. The live TPU pools also show `transparentHugepageEnabled: ALWAYS` in their Linux node config. The commands above don't set it, and we didn't confirm whether it is a GKE default.

**Check:**

```bash
kubectl --context="${CTX_TPU}" get nodes -L cloud.google.com/gke-nodepool --no-headers | awk '{print $NF, $2}' | sort | uniq -c
```

Expect one `Ready` node in each of `default-pool` (named `cpu-c4-pool` in the demo cluster), `tpu-v6e-spot` and `tpu-v6e-spot-decode`.

### Step 7: vLLM image

The image is upstream vllm-torchtpu, unmodified.

```bash
export VLLM_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/<repo>/vllm-torchtpu:3eb7abb5"
git clone https://github.com/vllm-project/vllm-torchtpu.git "$HOME/src/vllm-torchtpu"
cd "$HOME/src/vllm-torchtpu" && git checkout 3eb7abb5cc6ff4bae816e988010cf7bee6e9225c
./docker/build_image.sh -t "${VLLM_IMAGE}" --target prod
docker push "${VLLM_IMAGE}"
```

**Check:** `gcloud artifacts docker images describe "${VLLM_IMAGE}" --format='value(image_summary.digest)'` prints the image digest. The demo image is `sha256:699c7ccf…`; your build will differ. Step 8 puts `${VLLM_IMAGE}` into the Deployments as written. To pin your build by digest, set `VLLM_IMAGE` to `…/vllm-torchtpu:3eb7abb5@sha256:<digest>` first.

### Step 8: vLLM pods

```bash
cd "${REPO_DIR}"
kubectl --context="${CTX_TPU}" create secret generic llm-d-hf-token --from-literal=HF_TOKEN="${HF_TOKEN}"
kubectl --context="${CTX_TPU}" apply -f manifests/tpu/prereqs.yaml
sed "s|asia-northeast1-docker.pkg.dev/tpu-launchpad-playground/ikwak-vllm-torchtpu/vllm-torchtpu:3eb7abb5@sha256:699c7ccfce3a007298675dd8991950004b137171dac4c5738408599eadfec846|${VLLM_IMAGE}|" \
  manifests/tpu/gemma4-12b-torchtpu-deployments.yaml | kubectl --context="${CTX_TPU}" apply -f -
kubectl --context="${CTX_TPU}" apply -f manifests/tpu/gemma4-12b-render-svc.yaml
kubectl --context="${CTX_TPU}" rollout status deploy/gemma4-12b-torchtpu-1 --timeout=1800s
kubectl --context="${CTX_TPU}" rollout status deploy/gemma4-12b-torchtpu-2 --timeout=1800s
```

The first start downloads the weights into the PVC and compiles, which takes several minutes. Later restarts reuse the cache.

**Check:**

```bash
kubectl --context="${CTX_TPU}" get pods -l app=gemma4-12b-torchtpu -L llm-d.ai/replica
```

```text
NAME                                     READY   STATUS    RESTARTS   AGE     REPLICA
gemma4-12b-torchtpu-1-<hash>             1/1     Running   0          …       pod-1
gemma4-12b-torchtpu-2-<hash>             1/1     Running   0          …       pod-2
```

### Step 9: llm-d: CRDs, priorities, endpoint picker, gateway

```bash
cd "${REPO_DIR}"
kubectl --context="${CTX_TPU}" apply -f \
  https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v1.0.1/manifests.yaml
kubectl --context="${CTX_TPU}" apply -f manifests/tpu/inference-objectives.yaml
helm upgrade --install gaie-pd oci://registry.k8s.io/gateway-api-inference-extension/charts/inferencepool \
  --version v1.2.0 --kube-context "${CTX_TPU}" -n default -f manifests/tpu/gaie-values-flowctl.yaml
kubectl --context="${CTX_TPU}" rollout status deploy/gaie-pd-epp --timeout=300s

EPP_SVC_IP=$(kubectl --context="${CTX_TPU}" get svc gaie-pd-epp -o jsonpath='{.spec.clusterIP}')
sed "s|172.24.18.142|${EPP_SVC_IP}|g" manifests/tpu/llmd-envoy-gateway.yaml | kubectl --context="${CTX_TPU}" apply -f -
kubectl --context="${CTX_TPU}" rollout status deploy/llmd-envoy-gateway --timeout=120s
```

The demo cluster ran exactly these CRDs: GAIE **v1.0.1**. With the Gateway API enabled, GKE's addon manager also manages the v1 `InferencePool` CRD and upgraded it to its own newer revision (v1.4.0 here). Expect that one CRD to differ.

The gateway manifest holds the EPP Service's cluster IP from the demo cluster (`172.24.18.142`); the `sed` swaps in yours. To change a running install, see [§5.4](#54-envoy-gateway) and [§5.5](#55-epp-values).

**Check:**

```bash
kubectl --context="${CTX_TPU}" get inferenceobjectives
kubectl --context="${CTX_TPU}" get pods -l 'app in (gemma4-12b-torchtpu,llmd-envoy-gateway)'
kubectl --context="${CTX_TPU}" get pods -l inferencepool=gaie-pd-epp
```

```text
NAME                  INFERENCE POOL   PRIORITY   AGE
best-effort-traffic   gaie-pd          -10        …
premium-traffic       gaie-pd          100        …
standard-traffic      gaie-pd          0          …
```

The other two commands should show the two vLLM pods, `llmd-envoy-gateway-<hash>` and `gaie-pd-epp-<hash>`, all `1/1 Running`. Step 10 then sends a test request through the gateway.

### Step 10: Keynote driver and dashboard

One script deploys the driver and the dashboard, and re-runs safely: [`manifests/substrate/deploy-driver.sh`](./manifests/substrate/deploy-driver.sh).

```bash
cd "${REPO_DIR}"
DRY_RUN=1 ./manifests/substrate/deploy-driver.sh   # shows what it would do
./manifests/substrate/deploy-driver.sh             # reads CTX_SUB, CTX_TPU and BIN_DIR from Step 0
```

**What it does:**
1. Applies [`keynote-driver.yaml`](./manifests/substrate/keynote-driver.yaml) if it differs from the cluster: namespace `keynote-demo`, permission to mint ate-api client tokens, and the pod. Then it waits for the pod.
2. Looks up the gateway's node IP and the pod IPs of `pod-1`, `pod-2` and the EPP. It expects exactly one Running pod each; otherwise it stops and says so.
3. Builds the driver's flags from them (listed in the script) and compares them with the pod's `/work/args`.
4. Copies `dashboard/index.html` if it differs. The driver serves the page from disk, so this needs no restart.
5. Copies `${BIN_DIR}/keynote_driver` if it differs. Without `BIN_DIR` it keeps the pod's binary.
6. Restarts the driver only if its binary or flags changed, and waits until it answers.
7. Prints the driver's state.

Every copy is checked by sha256 before it replaces the old file, which is kept as `<file>.prev-<sha8>`. On an up-to-date pod the script changes nothing and restarts nothing. A re-run on the demo cluster printed:

```text
=== 1. Pod keynote-demo/keynote-driver
    keynote-driver.yaml: unchanged
=== 2. TPU cluster addresses
    gateway <node IP>:8080 · pod-1 <pod IP>:8000 · pod-2 <pod IP>:8000 · epp <pod IP>:9090
=== 3. Driver args
    /work/args: unchanged
=== 4. Dashboard (dashboard/index.html)
    unchanged (4f79b32b)
=== 5. Driver binary
    unchanged (1e92f74b)
=== 6. Driver process
    nothing changed: not restarted (pid 5113)
=== 7. Driver state
    phase idle · agents {'0': 1000} · vLLM: pod-1 up, pod-2 up · stage flow
=== done
```

On a first run, step 1 prints the objects it created. Steps 3–5 print `changing` (with every flag) and `copied …`, and step 6 ends with `restarted: pid none -> <pid>`.

**Check: one request through the gateway, from inside the driver pod.** This is the network path the agents use.

```bash
GATEWAY_IP=$(kubectl --context="${CTX_TPU}" get pod -l app=llmd-envoy-gateway -o jsonpath='{.items[0].status.hostIP}')
kubectl --context="${CTX_SUB}" -n keynote-demo exec keynote-driver -c driver -- wget -qO- -T 30 \
  --header='Content-Type: application/json' \
  --post-data='{"model":"google/gemma-4-12B-it","messages":[{"role":"user","content":"hi"}],"max_tokens":8}' \
  "http://${GATEWAY_IP}:8080/v1/chat/completions"
```

Expect a chat completion: `{"id":"chatcmpl-…","object":"chat.completion",…,"model":"google/gemma-4-12B-it",…}` with 8 completion tokens. On 2026-10-04 it answered "Hello! How can I help you today".

**For the Hermes variant**, which needs the same addresses in its own flags ([hermes §4.5](./hermes/README.md#45-driver-in-hermes-mode)):

```bash
POD1_IP=$(kubectl --context="${CTX_TPU}" get pod -l app=gemma4-12b-torchtpu,llm-d.ai/replica=pod-1 -o jsonpath='{.items[0].status.podIP}')
POD2_IP=$(kubectl --context="${CTX_TPU}" get pod -l app=gemma4-12b-torchtpu,llm-d.ai/replica=pod-2 -o jsonpath='{.items[0].status.podIP}')
EPP_IP=$(kubectl --context="${CTX_TPU}" get pod -l inferencepool=gaie-pd-epp -o jsonpath='{.items[0].status.podIP}')
echo "gateway=${GATEWAY_IP} pod-1=${POD1_IP} pod-2=${POD2_IP} epp=${EPP_IP}"
```

**Notes:**
- `EXTRA_ARGS` adds driver flags ([§5.3](#53-driver-flags)).
- `WORKER_NODES`, `WORKER_NODE_TYPE` and `WORKER_NODE_VCPUS` (defaults 25, c4-standard-4, 4) only change the dashboard's grid and density labels.
- The driver pod is a bare Pod with an `emptyDir` at `/work`. If its node is drained, nothing re-creates it ([§4.2](#42-driver-pod-deleted)).
- Copies go through `kubectl exec -i` instead of `kubectl cp`. Each exec prints the checksum of what it wrote, because on the demo workstation an exec whose command printed nothing lost its input ([implementation.md §6](./implementation.md#6-incidents-and-lessons)).
- The script never touches the Hermes driver, which runs in the same pod.

### Step 11: Create the 1,000 agents and warm them up

First check that the template has its golden snapshot, which new agents restore from:

```bash
kubectl ate --context="${CTX_SUB}" get actor-template -a ate-demo-sandbox sandbox-dense -o json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); d=d.get("actorTemplates",[d])[0] if isinstance(d,dict) else d[0]; print(((d.get("status") or {}).get("goldenSnapshotStatus") or {}).get("goldenSnapshot",{}).get("snapshotUri","NOT READY"))'
```

It prints `gs://<bucket>/ate-demo-sandbox-dense/atespaces/ate-golden/actors/<id>/snapshots/<id>` once the snapshot exists, and `NOT READY` before. Then:

```bash
kubectl --context="${CTX_SUB}" -n keynote-demo port-forward pod/keynote-driver 8090:8090 &
curl -s -X POST localhost:8090/api/reconcile -d '{}'            # creates agent-0001..1000 from sandbox-dense
until curl -s localhost:8090/api/state | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin)["phase"]!="idle")'; do sleep 5; done
curl -s localhost:8090/api/state | python3 -c 'import json,sys; print(json.load(sys.stdin).get("note"))'
```

Then do the [health check](./README.md#7-pre-show-health-check) once. A new actor's first wake restores from the template's golden snapshot in GCS, which is slower: 33.6 s for 1,000 agents after the C4 move. Pausing it afterwards leaves a node-local snapshot, which is what makes later wakes fast.

**Check:** after the health check, ate-api reports all 1,000 agents at rest:

```bash
kubectl ate --context="${CTX_SUB}" get actors -a ate-demo-sandbox -o json \
  | python3 -c 'import json,sys,collections; d=json.load(sys.stdin); d=d if isinstance(d,list) else d.get("actors",[]); print(collections.Counter(a["status"]["state"] for a in d))'
```

Expect `Counter({'ACTOR_STATE_PAUSED': 1000})`.

### Step 12: Open the dashboard, or rehearse offline

- **Live:** keep the port-forward running and open `http://localhost:8090/`.
- **Offline rehearsal:** run `python3 dashboard/mock_server.py 8765` and open `http://localhost:8765/`. The mock simulates every number, including the three stages:
  - It runs the light fleet at 80% idle, like the driver (`--fleet-idle-pct=N` changes it; `--harness hermes` defaults to 90).
  - It offers the same 200 req/s in every stage. `--overload-rate=N` adds N req/s to every stage, like the driver flag; the Hermes harness keeps its extra 200 req/s in Stage 3.
  - Its pool model was fitted to the 80% live runs on 2026-10-03, so served req/s, queues and per-pod latency come out roughly as on the cluster. The page doesn't show TTFT (since 2026-10-06). In `api/state` the mock's TTFT comes out about 1.3× lower with llm-d; on the live pods on 2026-10-05 it was 1.2× higher ([README §2](./README.md#2-results)).
- **Screenshots of the mock:** with the mock running, `node dashboard/shoot.mjs --out=./shots` clicks through the demo in headless Chrome, Stages 1 to 3 included, and saves 25 PNGs in about a minute and a half.
  - `--scenario=edge` adds 5 error states.
  - `--scenario=offline` adds 2 reconnect states and asks you to stop and restart the mock.
  - It needs Node 22 and Google Chrome.

---

## 3. Check the whole stack

These commands only read; nothing restarts. Run them before a rehearsal or after any repair. The table shows what they printed on the demo clusters on 2026-10-04.

```bash
cd "${REPO_DIR}"
kubectl --context="${CTX_SUB}" -n ate-system get pods --no-headers | awk '{print $3}' | sort | uniq -c
kubectl --context="${CTX_SUB}" -n ate-system exec postgres-0 -c postgres -- pidof http_srv
kubectl --context="${CTX_SUB}" get workerpools -A
kubectl ate --context="${CTX_SUB}" get actors -a ate-demo-sandbox -o json \
  | python3 -c 'import json,sys,collections; d=json.load(sys.stdin); d=d if isinstance(d,list) else d.get("actors",[]); print(collections.Counter(a["status"]["state"] for a in d))'
kubectl --context="${CTX_TPU}" get pods -l 'app in (gemma4-12b-torchtpu,llmd-envoy-gateway)'
kubectl --context="${CTX_TPU}" get pods -l inferencepool=gaie-pd-epp
kubectl --context="${CTX_SUB}" -n keynote-demo exec keynote-driver -c driver -- wget -qO- -T 5 http://127.0.0.1:8090/api/state \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); print("phase", s["phase"], "saturation", ((s.get("llmd") or {}).get("flow") or {}).get("saturation"))'
DRY_RUN=1 ./manifests/substrate/deploy-driver.sh
```

| Check | Healthy | If not |
|---|---|---|
| `ate-system` pods | only `Running` (98 on the demo cluster) | `kubectl -n ate-system get pods` and the pod's events |
| `pidof http_srv` | one pid | [§4.4](#44-postgres-0-restarted) |
| WorkerPools | `sandbox-workerpool   1600   1600   1600` | – |
| Agents | `Counter({'ACTOR_STATE_PAUSED': 1000})` while nobody uses the demo | [§4.7](#47-wake-or-suspend-all-misbehaves) |
| TPU pods | both vLLM pods, `llmd-envoy-gateway` and `gaie-pd-epp` all `1/1 Running` | [§4.1](#41-vllm-or-epp-pods-restarted) |
| Driver state | `phase idle saturation 0` with no traffic | saturation above 0: [§4.3](#43-flow-control-saturation-stays-above-0-while-idle) |
| `deploy-driver.sh` dry run | `unchanged` everywhere and `nothing changed: not restarted` | new `-vllm` or `-epp` addresses under `/work/args: changing`: [§4.1](#41-vllm-or-epp-pods-restarted) |

Then send the test request from [Step 10](#step-10-keynote-driver-and-dashboard). The [pre-show health check](./README.md#7-pre-show-health-check) is the end-to-end test: it wakes and suspends all 1,000 agents, so it is not read-only.

---

## 4. Redeploy and recover

Each recipe says when it applies, what to run, and how to check the result. Recovery steps for the agents themselves are in [README §7](./README.md#7-pre-show-health-check).

### 4.1 vLLM or EPP pods restarted

**When:** any of these:
- a Spot TPU node was preempted;
- you restarted the EPP or changed the vLLM Deployments ([§5.5](#55-epp-values), [§5.6](#56-vllm-pods));
- the dashboard shows a pod `down`;
- `deploy-driver.sh` reports new addresses.

**Why:** the driver is given the pods' IPs, and a restarted pod has a new one. A vLLM pod that moved to a new node takes minutes to load. The gateway's address is its node's IP, so it changes only if the CPU node is replaced.

```bash
kubectl --context="${CTX_TPU}" rollout status deploy/gemma4-12b-torchtpu-1 --timeout=1800s   # and -2, or deploy/gaie-pd-epp
cd "${REPO_DIR}" && ./manifests/substrate/deploy-driver.sh
```

The script shows the changed `-vllm` or `-epp` flag under `/work/args: changing`, restarts the driver, and prints `vLLM: pod-1 up, pod-2 up`. If it runs while a pod is still starting, it stops with `expected 1 Running pod … found 0` or notes `not Ready yet`; wait and run it again.

**Hermes variant:** set the address variables (Step 10) and run the [hermes §4.5](./hermes/README.md#45-driver-in-hermes-mode) start block again. It restarts the Hermes driver with the new addresses.

### 4.2 Driver pod deleted

**When:** `kubectl --context="${CTX_SUB}" -n keynote-demo get pod keynote-driver` returns `NotFound`. Its node was drained, upgraded or repaired, or someone deleted it. It is a bare Pod, so nothing re-creates it.

**What is lost:**
- Gone with the pod: everything in `/work` (driver binary, flags, dashboard page, run records) and the Hermes driver's files.
- Not affected: the agents and their snapshots. They live in ate-api, Postgres and on the worker nodes. If the worker nodes were recreated too (a cluster upgrade), the snapshots are gone: see [§4.8](#48-gke-upgraded-the-cluster-every-node-recreated).

```bash
cd "${REPO_DIR}" && BIN_DIR="${BIN_DIR}" ./manifests/substrate/deploy-driver.sh   # re-creates the pod and copies everything
kubectl --context="${CTX_SUB}" -n keynote-demo port-forward pod/keynote-driver 8090:8090 &
```

Without `BIN_DIR` the script stops at step 5 with `the pod has no driver yet`. Then select Stage 1 and run the [pre-show health check](./README.md#7-pre-show-health-check).

**Hermes variant:** the Hermes driver needs its API key and its binary back, in that order.

1. **Its API key.** The `hermes-dense` template holds the key as `API_SERVER_KEY`. This copies it into the pod without printing it, and prints only its length (48):

   ```bash
   kubectl ate --context="${CTX_SUB}" get actor-template -a keynote-hermes hermes-dense -o json \
     | python3 -c 'import json,sys; t=json.load(sys.stdin); t=t.get("actorTemplates",[t])[0]; print(next(e["value"] for c in t["containers"] for e in c.get("env",[]) if e["name"]=="API_SERVER_KEY"), end="")' \
     | kubectl --context="${CTX_SUB}" -n keynote-demo exec -i keynote-driver -c driver -- \
         sh -c 'umask 077; cat > /work/hermes_api_key && wc -c < /work/hermes_api_key'
   ```

2. **Its binary and its start.** Follow [hermes §4.5](./hermes/README.md#45-driver-in-hermes-mode): its copy lines put your Step 3 build in place, and its start block (with the Step 10 addresses) starts the driver and its `port-forward`.

Its memory bookkeeping (`/work/runs-hermes/hermes_memory.json`) is gone too. Teach the agents again as in [hermes §5](./hermes/README.md#5-before-the-show).

### 4.3 Flow-control saturation stays above 0 while idle

**When:** with no traffic, the dashboard's flow card or `api/state` (`llmd.flow.saturation`) stays above 0 (0.99 was seen). The endpoint picker's in-flight count leaked, and llm-d now sheds or queues calls ([README §9](./README.md#9-known-issues-and-disclosures)).

```bash
kubectl --context="${CTX_TPU}" rollout restart deployment/gaie-pd-epp
kubectl --context="${CTX_TPU}" rollout status deploy/gaie-pd-epp --timeout=300s
cd "${REPO_DIR}" && ./manifests/substrate/deploy-driver.sh        # the EPP pod IP changed
```

**Check:** the §3 driver-state line prints `saturation 0`. Hermes variant: as in [§4.1](#41-vllm-or-epp-pods-restarted).

### 4.4 postgres-0 restarted

**When:** `pidof http_srv` in §3 prints nothing, or `postgres-0` shows a recent start. Two things break:
- `http_srv` ran inside the old container, so it is gone.
- The pod probably has a new IP, and ate-api's and atelet's init containers download their binaries from the old one.

Nothing fails until one of those pods restarts. ate-api cannot start without its download.

```bash
cd "${REPO_DIR}"
BIN_DIR="${BIN_DIR}" CTX_SUB="${CTX_SUB}" DRY_RUN=1 ./manifests/substrate/deploy-patched-binaries.sh
BIN_DIR="${BIN_DIR}" CTX_SUB="${CTX_SUB}" ./manifests/substrate/deploy-patched-binaries.sh
```

The script starts `http_srv` again. The binaries are still on the Postgres volume, so they stay `unchanged`. If the IP changed, the script updates the init containers, which rolls ate-api and every atelet. When it last rolled them (for a new build) that took 46 s.

Do this with all agents at rest, then run the [pre-show health check](./README.md#7-pre-show-health-check). The first wake after an atelet restart is slower ([README §2.1](./README.md#21-agent-substrate-wake-duty-cycle-suspend)).

### 4.5 A Substrate node was recreated

**When:** an upgrade, repair or maintenance event replaced a worker node, or the health check's wake fails for some agents. The driver's note shows one of two errors:
- `ResourceExhausted desc = no free workers available`: the agent's snapshot was on a node that no longer exists. A recreated node comes back under a new name, and a paused agent's wake is pinned to the node that holds its snapshot, so this agent can never wake again. It is still `PAUSED` in ate-api, so `post reconcile` alone doesn't touch it.
- `runsc restore` errors: the snapshot is on the node but can't be restored.

**What happens:**
- Agents paused on that node lost their snapshots and must be re-created.
- Agents awake on it go `CRASHED`.
- The new node gets the worker label from its node pool, atelet and the node tuner from their DaemonSets, and fresh workers from the WorkerPool.

**Fix:** with the port-forward running and README §7's `post` helper defined:
1. Run the [health check](./README.md#7-pre-show-health-check)'s wake. Failed agents show in the driver's note. If the driver is left in `running`, run `post suspend`: it reconciles only when idle.
2. Re-create the broken agents with [`ops/recreate_lost.py`](./ops/recreate_lost.py). It deletes the agents whose snapshot is on a node that no longer exists, then runs the driver's reconcile. The reconcile re-creates every missing agent from the template, every agent whose restore failed, and every agent that is not at rest:

   ```bash
   DRY_RUN=1 CTX_SUB="${CTX_SUB}" python3 ops/recreate_lost.py   # lists them
   CTX_SUB="${CTX_SUB}" python3 ops/recreate_lost.py
   ```

   It ends with the reconcile's note, for example `re-created 1000 (0 failed); at T0: 1000 suspended, 0 paused, 0 not at rest`. The 2026-10-05 recovery ran the same delete and reconcile by hand; the script was written afterwards and has only been dry-run ([§7](#7-how-this-guide-was-verified)).
3. Run the health check again. Re-created agents restore from the golden snapshot, so their first wake is slower: 7.0 s for all 1,000 on 2026-10-05.
4. Even out the placement: `CTX_SUB="${CTX_SUB}" python3 ops/rebalance.py 40 10`. It wakes and suspends the fleet every round. On 2026-10-02 it took 10 rounds, about 80 s. On 2026-10-05, after all 1,000 agents were re-created, it took 10 rounds, about 130 s, and left 36–41 agents per node. Then run the health check once more; Wake 1,000 took 1.77 s.

**Hermes variant:** the same script, pointed at the Hermes fleet and driver: `ATESPACE=keynote-hermes DRIVER_URL=http://localhost:8092/ CTX_SUB="${CTX_SUB}" python3 ops/recreate_lost.py`. Re-created Hermes agents have no memory: teach them again ([hermes §5](./hermes/README.md#5-before-the-show)).

**Prevention:** turn off auto-upgrade on the worker pools, and add a maintenance exclusion through the show (Step 2, [README §9](./README.md#9-known-issues-and-disclosures)).

### 4.6 Dashboard says RECONNECTING

**When:** the page shows RECONNECTING, usually after the driver restarted. A `kubectl port-forward` can hang: it keeps running but logs `error creating forwarding stream … Timeout`.

**Fix:** stop it and start it again, then check that it answers:

```bash
kubectl --context="${CTX_SUB}" -n keynote-demo port-forward pod/keynote-driver 8090:8090 &
curl -m 6 -s localhost:8090/api/state | head -c 80; echo
```

To restart it on its own whenever it exits, run it in a loop: `while true; do kubectl --context="${CTX_SUB}" -n keynote-demo port-forward pod/keynote-driver 8090:8090; sleep 1; done`. With the loop, killing a hung `kubectl` process brings up a new one.

### 4.7 Wake or Suspend all misbehaves

Use [README §7](./README.md#7-pre-show-health-check) when:
- Wake 1,000 stops short of 1,000;
- Wake 1,000 gets slower than about 2.1 s;
- Suspend all hangs, or ends with "Suspend incomplete · N still up".

It covers unrestorable snapshots, stuck agents (OOM-restarted workers or failed checkpoints), leftover sandboxes, bloated snapshots and uneven placement, with the commands for each. After Simulate Traffic has run for hours, do its full fix before the next show (README §9).

### 4.8 GKE upgraded the cluster (every node recreated)

**When:** GKE auto-upgraded the clusters. Every node in `kubectl get nodes` is young, and you see some of: the driver pod `NotFound`, `postgres-0` `Pending`, atelet pods crash-looping in their init container, the dashboard stuck on RECONNECTING. On 2026-10-04 an auto-upgrade to 1.35.8-gke.1380001 recreated every node of both clusters and caused all of these. Nothing came back on its own.

**Why §4.2 is not enough:**
- `postgres-0` and `ate-api-server` are pinned to one keynote-driver-pool node by hostname ([Step 4](#step-4-size-and-tune-the-substrate-cluster)). The upgrade renamed every node, so they can't be scheduled.
- Postgres comes back with a new IP, so ate-api's and atelet's init containers download their binaries from an address that is gone ([§4.4](#44-postgres-0-restarted)).
- Every agent's snapshot was on a node that is gone ([§4.5](#45-a-substrate-node-was-recreated)).
- The vLLM, EPP and gateway pods have new IPs ([§4.1](#41-vllm-or-epp-pods-restarted)).

**Fix, in this order:**
1. **Control plane.** Re-run Step 4's `scale-control-plane.sh`, dry run first. It pins `postgres-0` and `ate-api-server` to a current keynote-driver-pool node. A StatefulSet doesn't replace a pod that is stuck `Pending`: if `postgres-0` stays `Pending`, delete it with `kubectl --context="${CTX_SUB}" -n ate-system delete pod postgres-0`. Its volume re-attaches with the data, including the patched binaries.
2. **Patched binaries.** [§4.4](#44-postgres-0-restarted): `deploy-patched-binaries.sh` starts `http_srv` again and points the init containers at Postgres's new IP, which rolls ate-api and every atelet. On 2026-10-05 the atelet rollout took about 3 minutes.
3. **Driver pod.** [§4.2](#42-driver-pod-deleted): `deploy-driver.sh` with `BIN_DIR`. It also picks up the TPU cluster's new pod IPs. Then send Step 10's one request through the gateway.
4. **Agents of both fleets.** [§4.5](#45-a-substrate-node-was-recreated): `recreate_lost.py`, the health check, `rebalance.py`, the health check again.
5. **Hermes driver.** §4.2's Hermes variant, then teach the agents ([hermes §5](./hermes/README.md#5-before-the-show)).
6. **Warm up both vLLM pods** before you measure or rehearse: run Stage 1 traffic for a minute. A pod that has served little traffic since it restarted is slow at first. On 2026-10-05, after the Hermes teach had gone to pod-1 only, pod-2 queued 149 requests at a 7.4 s TTFT during the first ~30 s of Stage 1, and one agent's request timed out.

Then check the whole stack with [§3](#3-check-the-whole-stack).

---

## 5. Update one component

Make the change on a quiet stack: Suspend all first, and finish with the [pre-show health check](./README.md#7-pre-show-health-check) before the next show.

### 5.1 Dashboard

Edit [`dashboard/index.html`](./dashboard/index.html) and check it on the mock ([Step 12](#step-12-open-the-dashboard-or-rehearse-offline)). Then run:

```bash
cd "${REPO_DIR}" && ./manifests/substrate/deploy-driver.sh
```

Only the page is copied; the old one is kept as `index.html.prev-<sha8>`. The driver serves the page from disk, so it doesn't restart: reload the browser. The Hermes dashboard on port 8092 serves the same file.

### 5.2 Driver build

Rebuild with the Step 3 lines, then deploy:

```bash
cd "${SUBSTRATE_SRC}"
cp "${REPO_DIR}"/substrate-bench/keynote_driver/*.go cmd/keynote_driver/
CGO_ENABLED=0 go build -buildvcs=false -trimpath -o "${BIN_DIR}/keynote_driver" ./cmd/keynote_driver
cd "${REPO_DIR}" && BIN_DIR="${BIN_DIR}" ./manifests/substrate/deploy-driver.sh
```

The script copies the binary, keeps the old one as `keynote_driver.prev-<sha8>`, and restarts the driver. If the new build doesn't answer within 30 s, the script prints the end of `/work/driver.log` and fails. To roll back, point `BIN_DIR` at a directory with the previous build and run the script again. After a restart the port-forward may need a restart too ([§4.6](#46-dashboard-says-reconnecting)).

### 5.3 Driver flags

The script rebuilds the whole flag list on every run, from the TPU addresses plus the fixed flags in the script. Add flags with `EXTRA_ARGS`:

```bash
cd "${REPO_DIR}" && EXTRA_ARGS='-fleet-idle-pct=90' ./manifests/substrate/deploy-driver.sh
```

It prints `> -fleet-idle-pct=90` under `/work/args: changing` and restarts the driver. Pass the same `EXTRA_ARGS` on every later run. A run without it removes the flag again, and a dry run shows that as `< -fleet-idle-pct=90`. The flags and their defaults are defined in [`main.go`](./substrate-bench/keynote_driver/main.go); [README §4](./README.md#4-changes-from-stock) explains the demo-relevant ones.

### 5.4 Envoy gateway

Edit [`manifests/tpu/llmd-envoy-gateway.yaml`](./manifests/tpu/llmd-envoy-gateway.yaml), then:

```bash
cd "${REPO_DIR}"
EPP_SVC_IP=$(kubectl --context="${CTX_TPU}" get svc gaie-pd-epp -o jsonpath='{.spec.clusterIP}')
sed "s|172.24.18.142|${EPP_SVC_IP}|g" manifests/tpu/llmd-envoy-gateway.yaml | kubectl --context="${CTX_TPU}" diff -f -    # review
sed "s|172.24.18.142|${EPP_SVC_IP}|g" manifests/tpu/llmd-envoy-gateway.yaml | kubectl --context="${CTX_TPU}" apply -f -
kubectl --context="${CTX_TPU}" rollout restart deploy/llmd-envoy-gateway     # Envoy reads its config only at start
kubectl --context="${CTX_TPU}" rollout status deploy/llmd-envoy-gateway --timeout=120s
```

The gateway uses the `Recreate` strategy, because its hostPort 8080 can't be held by two pods on one node. Requests fail for the few seconds it is down. Then send the Step 10 test request.

### 5.5 EPP values

Edit [`manifests/tpu/gaie-values-flowctl.yaml`](./manifests/tpu/gaie-values-flowctl.yaml). To review the change, compare the rendered chart with the deployed release. On an unchanged install, the only difference is one trailing empty line:

```bash
cd "${REPO_DIR}"
diff <(helm template gaie-pd oci://registry.k8s.io/gateway-api-inference-extension/charts/inferencepool --version v1.2.0 \
         --kube-context "${CTX_TPU}" -n default -f manifests/tpu/gaie-values-flowctl.yaml) \
     <(helm get manifest gaie-pd --kube-context "${CTX_TPU}" -n default)
```

Then apply it and restart the EPP, so that it starts with the new values:

```bash
helm upgrade --install gaie-pd oci://registry.k8s.io/gateway-api-inference-extension/charts/inferencepool \
  --version v1.2.0 --kube-context "${CTX_TPU}" -n default -f manifests/tpu/gaie-values-flowctl.yaml
kubectl --context="${CTX_TPU}" rollout restart deploy/gaie-pd-epp
kubectl --context="${CTX_TPU}" rollout status deploy/gaie-pd-epp --timeout=300s
./manifests/substrate/deploy-driver.sh       # the EPP pod IP changed
```

Hermes variant: as in [§4.1](#41-vllm-or-epp-pods-restarted).

### 5.6 vLLM pods

Edit [`manifests/tpu/gemma4-12b-torchtpu-deployments.yaml`](./manifests/tpu/gemma4-12b-torchtpu-deployments.yaml) and re-run Step 8's `sed … | kubectl apply` line. Both Deployments use the `Recreate` strategy, so each pod stops before its replacement starts and loads the model, which takes minutes. Wait for `rollout status` on both, then run `./manifests/substrate/deploy-driver.sh` ([§4.1](#41-vllm-or-epp-pods-restarted)).

The Hermes variant needs `--max-model-len` of at least 65536 (64k tokens). [README §9](./README.md#9-known-issues-and-disclosures) explains what that setting costs in speed.

### 5.7 Patched ate-api and atelet

Rebuild with Step 3, then run Step 5's script, dry run first. It uploads only the changed `.gz` and restarts only what changed: ate-api, every atelet, or both. With a new build of both, that took 46 s. Do it with all agents at rest; the first wake afterwards is slower.

### 5.8 Agent template

> [!NOTE]
> Not run on the demo clusters in this form.

`kubectl ate` cannot update a template. To change `sandbox-dense`:
1. Delete all 1,000 agents, one call each (slow).
2. Delete the template. The server also deletes the template's golden actor and golden snapshot.
3. Create the template again.

```bash
cd "${REPO_DIR}"
for i in $(seq 1 1000); do
  kubectl ate --context="${CTX_SUB}" delete actor "$(printf 'agent-%04d' "$i")" --any-state -a ate-demo-sandbox
done
kubectl ate --context="${CTX_SUB}" delete actor-template sandbox-dense -a ate-demo-sandbox
envsubst < manifests/substrate/sandbox-dense-template.yaml.tmpl | kubectl ate --context="${CTX_SUB}" create actor-template -f -
```

Then run Step 11: wait for the golden snapshot, reconcile, and do the health check. Finally run `ops/rebalance.py` ([§4.5](#45-a-substrate-node-was-recreated)).

### 5.9 Substrate sizing and tuning

Edit [`scale-control-plane.sh`](./manifests/substrate/scale-control-plane.sh) and re-run Step 4, dry run first. It may restart `postgres-0`; if it does, follow [§4.4](#44-postgres-0-restarted) before anything else restarts. Changing the WorkerPool's memory recreates all 1,600 workers ([README §9](./README.md#9-known-issues-and-disclosures)).

---

## 6. Tear it down

> [!CAUTION]
> None of this was run on the demo clusters. Deleting a cluster deletes every agent and snapshot on it.

**To keep the clusters but undo the demo's changes:**

```bash
# Stop the node tuner (its mount/sysctl changes persist until the nodes are recreated)
kubectl --context="${CTX_SUB}" -n ate-system delete ds ate-node-tuner
# Stop the file server inside postgres-0 and remove the uploaded binaries
kubectl --context="${CTX_SUB}" -n ate-system exec postgres-0 -c postgres -- sh -c \
  'kill $(pidof http_srv); rm -f /var/lib/postgresql/data/bin_*.gz*'
# Back to stock ate-api/atelet: the init containers were added by patch, so delete the objects and redeploy
kubectl --context="${CTX_SUB}" -n ate-system delete deploy/ate-api-server ds/atelet-v0-1-0-gke-1
(cd "${SUBSTRATE_SRC}" && git stash && KUBECTL_CONTEXT="${CTX_SUB}" go run ./cmd/ate-setup deploy ate-system \
  --no-dev-env --image-repo us-docker.pkg.dev/gke-substrate-release/substrate --image-tag v0.1.0-gke.1)
```

The Postgres settings from Step 4 (`fsync off`, `full_page_writes off`) stay until you set them back.

**To delete everything:**

```bash
gcloud container clusters delete "${SUBSTRATE_CLUSTER}" --zone="${ZONE}" --project="${PROJECT_ID}"
gcloud container clusters delete "${TPU_CLUSTER}" --zone="${ZONE}" --project="${PROJECT_ID}"
gcloud storage rm --recursive "gs://${BUCKET_NAME}"
gcloud artifacts docker images delete "${VLLM_IMAGE}" --delete-tags     # and the Hermes image, if you built it
gcloud compute firewall-rules delete "${VPC_NAME}-allow-internal" --project="${PROJECT_ID}"
gcloud compute networks subnets delete "${SUBNET_NAME}" --region="${REGION}" --project="${PROJECT_ID}"
gcloud compute networks delete "${VPC_NAME}" --project="${PROJECT_ID}"
```

`setup-gcp bootstrap` (Step 2) also set up APIs, IAM and monitoring dashboards, and this guide doesn't remove them. The `setup-gcp` source at `fa6d949` shows what it created.

---

## 7. How this guide was verified

**2026-09-26:** every step of what was then README §6 was checked against the live clusters:
- The builds, the deploy scripts, the driver deploy, reconcile and the health check were run as written.
- Cluster, VPC and TPU creation were checked read-only.

The table is in [README §10](./README.md#10-how-this-guide-was-verified).

**2026-10-04, for this guide** (live clusters, nothing recreated):
- **Step 3:** a fresh clone of upstream at `fa6d949` with the patch, built with go1.27.0 and gcc 15.2.0. All seven files in the Step 3 table were byte-identical to what runs in the cluster.
- **Steps 4 and 5:** both scripts' dry runs, with that build, reported every item `unchanged`.
- **Step 2:** the rendered `sandbox-dense` template matches the live one field for field, and the golden snapshot is present.
- **Steps 8 and 9:** `kubectl diff` of every manifest showed no difference in any spec. The vLLM Deployments differ only in their `deployment.kubernetes.io/revision` annotation, from a stale last-applied annotation. The rendered EPP chart equals the deployed release, apart from one trailing empty line.
- **Step 10 on the live driver pod:** `deploy-driver.sh` (dry run, then a real run) changed nothing. The driver kept its pid, and the Hermes driver was not touched. The gateway test request returned a chat completion.
- **Step 10 change paths,** on a throwaway pod in namespace `keynote-guidetest`, with a stand-in binary that serves `/api/state` (namespace deleted afterwards). Each run did what it should:
  - first deploy into a new namespace;
  - a re-run with no change;
  - a new binary: copied, previous kept, restarted;
  - an added flag;
  - a dry run;
  - a binary that exits at once: reported with the log tail, and the script failed;
  - recovery.
- **Hermes key recovery (§4.2):** the value the command extracts from the template matched `/work/hermes_api_key` (compared by checksum; the file adds a trailing newline). The write pattern was tested on the throwaway pod with a dummy value.
- **Every check command in §2 and §3** was run on the demo clusters, and the expected outputs come from those runs.

**Not run:**
- creating the VPC, the clusters and the node pools;
- `setup-gcp` and `ate-setup`;
- building the vLLM image;
- Step 11's creation of new agents (all 1,000 already existed);
- the template change (§5.8);
- the Hermes driver's copy and start ([hermes §4.5](./hermes/README.md#45-driver-in-hermes-mode)), so that the running Hermes driver keeps its build. Its copy prints the checksum from the same exec, as `deploy-driver.sh` does. They ran for real on 2026-10-05 (below);
- the teardown (§6).

**2026-10-05, after the GKE upgrade:** the §4 situations happened for real. On 2026-10-04 an auto-upgrade recreated every node of both clusters, which restarted the vLLM, EPP and gateway pods, deleted the driver pod, left `postgres-0` `Pending` and lost every agent's snapshot. The stack was recovered in the order of [§4.8](#48-gke-upgraded-the-cluster-every-node-recreated):
- **Control plane:** Step 4's `scale-control-plane.sh` re-pinned `postgres-0` and `ate-api-server`, and the `Pending` `postgres-0` was deleted. It came back with its data and the patched binaries.
- **[§4.4](#44-postgres-0-restarted):** `deploy-patched-binaries.sh` started `http_srv` and pointed the init containers at Postgres's new IP. ate-api and the 43 atelets (25 light-fleet nodes and 18 Hermes nodes) rolled in about 3 minutes.
- **[§4.2](#42-driver-pod-deleted):** `deploy-driver.sh` with `BIN_DIR` re-created the driver pod with the same build and the TPU pods' new IPs. §4.2's key command copied the Hermes key (it printed 48). The Hermes driver got its build (`a709458a`) back and was started with the same flags.
- **[§4.5](#45-a-substrate-node-was-recreated):** all 1,000 agents of each fleet were deleted by hand (`kubectl ate … delete actor --any-state`) and re-created by the driver's reconcile, which printed `re-created 1000 (0 failed)` for both. The light fleet's first wake took 7.0 s. After `rebalance.py` (10 rounds) the health check woke all 1,000 in 1.77 s and suspended them in about 1.5 s, with 0 failed.
  - [`ops/recreate_lost.py`](./ops/recreate_lost.py), which does the delete and the reconcile in one step, was written afterwards and has not run for real.
  - Its dry run found 0 lost agents in both recovered fleets. Its selection, run offline on the actor lists saved before the recovery, picked all 1,000 in each.
- **Hermes:** memory reset, and the agents taught again ([hermes §5](./hermes/README.md#5-before-the-show)): 1,000 of 1,000, with at most 100 taught at a time.
- **Warm-up:** 70 s of Stage 1 traffic on both vLLM pods (§4.8 step 6), then one run of the three stages ([README §2](./README.md#2-results)).

Not needed, so not exercised: §4.3 (saturation stuck above 0).
