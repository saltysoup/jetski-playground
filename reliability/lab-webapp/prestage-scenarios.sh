#!/bin/bash
# ==============================================================================
# Operation Goodput: Hands-Off Pre-Staging Script (Run ONCE Before the CE Lab)
# ==============================================================================
# This script pre-generates all three forensic scenarios on the GKE cluster and
# Cloud Logging so that you (the instructor) do NOT need to inject faults live
# during the session, and CEs can work through Mission 1 -> 2 -> 3 at their own
# pace with Read-Only cluster/project access.
#
# What it stages:
#   - Mission 1: `dapo-gemma3-run1-driver` pod in `default` (Status: Error) with
#     realistic PyTorch FSDP2 `torch.OutOfMemoryError: CUDA out of memory` logs.
#   - Mission 2: `dapo-gemma3-run2-driver` pod in `default` (Status: Error) with
#     generic `RuntimeError: NCCL error: unhandled cuda error`, PLUS a one-shot
#     injection of `NVRM: Xid (PCI:0000:89:00): 48 (Double Bit ECC)` into
#     `/dev/kmsg` on node `-6df3` (which cleans up its injector pod immediately).
#   - Mission 3: `dapo-gemma3-run3-driver` pod in `default` (Status: Error) with
#     the 9.4s `ActorUnavailableError` crash, paired with node `-34t3`'s real
#     `XID 79` + `XID 154` serial console logs, a `mission3-maintenance-snapshot`
#     ConfigMap preserving the `PENDING` `FAILURE_GPU_XID` state, and the live
#     repaired node `-34t3` (`Ready=True`, `8` allocatable GPUs, updated
#     `lastStartTimestamp`).
# ==============================================================================
set -euo pipefail

NODE_34T3="${NODE_34T3:-gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3}"
NODE_6DF3="${NODE_6DF3:-gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3}"

echo "[1/4] Staging Mission 1: Job-Level CUDA OOM pod (dapo-gemma3-run1-driver)..."
kubectl delete pod dapo-gemma3-run1-driver -n default --ignore-not-found
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-run1-driver
  namespace: default
  labels:
    app: nemo-rl-driver
    mission: "1"
    run: dapo-gemma3-run1
spec:
  restartPolicy: Never
  containers:
  - name: ray-job-driver
    image: busybox:1.36
    command: ["/bin/sh", "-c"]
    args:
    - |
      cat <<'LOG'
      [2026-10-06 18:10:04 UTC] Submitting Ray job: dapo-gemma3-run1
      [2026-10-06 18:10:05 UTC] Cluster resources: 2 nodes, 16.0 GPUs (NVIDIA B200), 3000.0 GiB RAM
      [2026-10-06 18:11:42 UTC] vLLM Rollout Engine initialized across 16 GPUs (TP=2, max_model_len=32768)
      [2026-10-06 18:12:15 UTC] FSDP2 Policy initialized (google/gemma-3-27b-it, train_micro_batch_size=8)
      [Step 1/100] Starting rollout generation across 16 GPUs...
      (DTensorPolicyWorkerV2 pid=18422, ip=10.88.2.6) [Rank 0] Forward+Backward pass started
      Traceback (most recent call last):
        File "/opt/nemo-rl/examples/run_grpo.py", line 241, in main
          grpo_train(policy, rollout_workers, dataloader, config)
        File "/opt/nemo-rl/nemo_rl/algorithms/grpo.py", line 418, in grpo_train
          loss = policy.train_step(batch)
      torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 14.50 GiB. GPU 0 has a total capacity of 178.35 GiB of which 2.12 GiB is free. Including non-PyTorch memory, this process has 176.18 GiB memory in use. Of the allocated memory 168.40 GiB is allocated by PyTorch, and 4.12 GiB is reserved by PyTorch but unallocated.
      ray.exceptions.RayTaskError(OutOfMemoryError): ray::DTensorPolicyWorkerV2.train_step() (pid=18422, ip=10.88.2.6)
      [Job Status] dapo-gemma3-run1 FAILED (ExitCode: 1)
      LOG
      exit 1
EOF

echo "[2/4] Staging Mission 2: Transient XID 48 pod (dapo-gemma3-run2-driver) + Serial Console XID 48..."
kubectl delete pod dapo-gemma3-run2-driver -n default --ignore-not-found
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-run2-driver
  namespace: default
  labels:
    app: nemo-rl-driver
    mission: "2"
    run: dapo-gemma3-run2
spec:
  restartPolicy: Never
  containers:
  - name: ray-job-driver
    image: busybox:1.36
    command: ["/bin/sh", "-c"]
    args:
    - |
      cat <<'LOG'
      [2026-10-06 18:38:10 UTC] Submitting Ray job: dapo-gemma3-run2 (train_micro_batch_size=2, max_model_len=8192)
      [2026-10-06 18:50:11 UTC] Step 1/100 complete | step_time: 116.12s | reward_mean: 0.241
      ...
      [2026-10-06 19:12:25 UTC] Step 17/100 complete | step_time: 115.84s | reward_mean: 0.289
      [2026-10-06 19:14:22 UTC] Step 18/100 in progress...
      (DTensorPolicyWorkerV2 pid=218402, host=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3)
      RuntimeError: NCCL error in: /pytorch/torch/csrc/distributed/c10d/ProcessGroupNCCL.cpp:1842, unhandled cuda error (run with NCCL_DEBUG=INFO for details), NCCL version 2.26.2
      ncclUnhandledCudaError: Call to CUDA function failed.
      [Job Status] dapo-gemma3-run2 FAILED (ExitCode: 1)
      LOG
      exit 1
EOF

# One-shot XID 48 emission into /dev/kmsg on NODE_6DF3 (deletes pod immediately so no spoiler remains)
kubectl run xid48-stage-oneshot --rm -i --restart=Never \
  --image=busybox:1.36 \
  --overrides="{
    \"spec\": {
      \"nodeName\": \"$NODE_6DF3\",
      \"hostPID\": true,
      \"containers\": [{
        \"name\": \"stage\",
        \"image\": \"busybox:1.36\",
        \"securityContext\": {\"privileged\": true},
        \"command\": [\"nsenter\", \"-t\", \"1\", \"-m\", \"-p\", \"--\", \"sh\", \"-c\", \"UP=\$(awk '{printf \\\"[%12.6f]\\\", \$1}' /proc/uptime); echo \\\"\$UP NVRM: Xid (PCI:0000:89:00): 48, pid=218402, name=ray::DTensorPolicy, An uncorrectable double bit error (DBE) has been detected on GPU (0000:89:00).\\\" > /dev/kmsg\"]
      }]
    }
  }" || echo "Note: Skipping live kmsg write if GPU node is scaled down; XID logs already in Cloud Logging."

echo "[3/4] Staging Mission 3: Catastrophic XID 79 crash pod (dapo-gemma3-run3-driver) & Maintenance Snapshot..."
kubectl delete pod dapo-gemma3-run3-driver -n default --ignore-not-found
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-run3-driver
  namespace: default
  labels:
    app: nemo-rl-driver
    mission: "3"
    run: dapo-gemma3-run3
spec:
  restartPolicy: Never
  containers:
  - name: ray-job-driver
    image: busybox:1.36
    command: ["/bin/sh", "-c"]
    args:
    - |
      cat <<'LOG'
      [2026-09-24 20:15:00 UTC] Submitting Ray job: dapo-gemma3-run3
      [2026-09-24 20:29:40 UTC] Step 6/100 complete | step_time: 116.41s | reward_mean: 0.258
      [2026-09-24 20:31:38 UTC] ERROR: Worker died on node gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3 (instance_id=174490015860863227)
      ray.exceptions.ActorUnavailableError: The actor died unexpectedly before finishing this task.
        exit_type: SYSTEM_ERROR
        rpc_code: 14 (Socket closed)
      [Job Status] dapo-gemma3-run3 FAILED at 20:31:46.469Z (+9.4s after hardware fault)
      LOG
      exit 1
EOF

# Create a ConfigMap in default holding the exact captured B5 upcomingMaintenance JSON
# so CEs can inspect it via kubectl even after the repair has completed!
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: mission3-maintenance-snapshot
  namespace: default
data:
  reservation_health_at_b4.yaml: |
    reservation: nvidia-b200-6bsoymep8ylww
    observedAt: "2026-09-24T20:32:07.466Z (+30.4s after XID 79)"
    resourceStatus:
      healthInfo:
        healthStatus: DEGRADED
        degradedBlockCount: 1
        healthyBlockCount: 0
  instance_upcoming_maintenance_at_b5.json: |
    {
      "instance": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3",
      "instanceId": "174490015860863227",
      "observedAt": "2026-09-24T20:57:49.444Z (+26m 12s after XID 79)",
      "upcomingMaintenance": {
        "canReschedule": true,
        "latestWindowStartTime": "2026-10-02T00:00:01Z",
        "maintenanceReasons": [
          "FAILURE_GPU_XID",
          "FAILURE_GPU"
        ],
        "maintenanceStatus": "PENDING",
        "type": "UNSCHEDULED",
        "windowStartTime": "2026-10-02T00:00:00Z",
        "windowEndTime": "2026-10-02T04:00:00Z"
      }
    }
EOF

echo "[4/4] All 3 Forensic Missions staged cleanly in namespace 'default'!"
