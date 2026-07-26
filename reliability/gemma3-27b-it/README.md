# Run 2-Node DAPO/GRPO for Gemma4-26B-A4B-it Workloads on A4 GKE Node Pools with Nvidia NeMo RL Framework

This recipe outlines the steps for running a Gemma4-26B-A4B-it DAPO/GRPO workload on [A4 GKE Node pools](https://cloud.google.com/kubernetes-engine) using 2 nodes (16 x B200 GPUs) and the [NVIDIA NeMo RL framework](https://github.com/NVIDIA-NeMo/RL).

## Orchestration and deployment tools

For this recipe, the following setup is used:

- Orchestration - [Google Kubernetes Engine (GKE)](https://cloud.google.com/kubernetes-engine)
- NemoRL configuration and deployment - A Helm chart is used to configure and deploy
  the Ray cluster on GKE using KubeRay Operator, managing the execution of the
  [NeMo RL workload](https://github.com/NVIDIA-NeMo/RL).

## Test environment

This recipe has been tested with the following configuration:

- GKE cluster
    - [A regional standard cluster](https://cloud.google.com/kubernetes-engine/docs/concepts/configuration-overview) version: 1.31.7-gke.1265000 or later.
    - A GPU node pool with 2 nodes of
    [a4-highgpu-8g](https://cloud.google.com/compute/docs/accelerator-optimized-machines#a4-high-vms) (16 x B200 GPUs).
    - [Workload Identity Federation for GKE](https://cloud.google.com/kubernetes-engine/docs/concepts/workload-identity) enabled.
    - [Cloud Storage FUSE CSI driver for GKE](https://cloud.google.com/kubernetes-engine/docs/concepts/cloud-storage-fuse-csi-driver) enabled.
    - [DCGM metrics](https://cloud.google.com/kubernetes-engine/docs/how-to/dcgm-metrics) enabled.
    - [KubeRay Operator](https://ray-project.github.io/kuberay-helm/) installed.

## Training dataset

This recipe uses the DAPO Math datasets (`DAPOMath17K` for training and `DAPOMathAIME2024` for validation).

## Docker container image

This recipe uses the following docker image:
`us-central1-docker.pkg.dev/deeplearning-images/reproducibility/pytorch-gpu-nemo-nccl:nemo-rl-nemo25.04`.

This image is based on NVIDIA NeMo 25.04 and contains the NCCL gIB plugin
v1.0.5, bundling all NCCL binaries validated for use with A4 GPUs.

## Run the recipe

From your client workstation, complete the following steps:

### Install KubeRay Operator
Nemo RL uses Ray as an orchestrator on top of GKE. In your terminal, run the following to install the KubeRay operator:
```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update
helm install kuberay-operator kuberay/kuberay-operator
```

To check if your KubeRay operator is installed, run:

```bash
kubectl get pods | grep kuberay-operator
```

You should see a pod in the `Running` state.

### Configure environment settings

Set the environment variables to match your environment:

```bash
export PROJECT_ID=<PROJECT_ID>
export CLUSTER_REGION=<CLUSTER_REGION>
export CLUSTER_NAME=<CLUSTER_NAME>
```

Set the default project:

```bash
gcloud config set project $PROJECT_ID
```

### Get cluster credentials

```bash
gcloud container clusters get-credentials $CLUSTER_NAME --region $CLUSTER_REGION
```

### Configure and submit a NemoRL Gemma4 Job

#### 1. Start a Ray cluster

To start a 2-node Ray cluster for 16 GPUs:

```bash
source launch-ray-cluster.sh
```

To check the status of the Ray cluster:

```bash
kubectl get pods | grep ray-cluster
```

You should see all pods (`ray-cluster-kuberay-head-...` and 2 worker pods) in `Running` state.

#### 2. Launch Gemma 4 26B DAPO workload

Edit `submit_gemma4-26ba4b-it.sh` to fill in your `WANDB_API_KEY` and `HF_TOKEN` (or export them in your environment).

Submit the training job:

```bash
source submit_gemma4-26ba4b-it.sh
```

### Monitor the job

To check the status of pods in your job:

```bash
kubectl get pods | grep ray-cluster
```

To get the logs from the head pod or worker pods:

```bash
kubectl logs <HEAD_POD_NAME> -c ray-head
```

### Analyze results

Training results (loss, rewards, timing) will be printed in the Ray head pod logs and logged to Weights & Biases (WandB) if enabled.

### Uninstall the Helm release

To stop and delete the Ray cluster:

```bash
helm uninstall ray-cluster
```
