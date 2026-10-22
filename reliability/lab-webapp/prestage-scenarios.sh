#!/bin/bash
# ==============================================================================
# Operation Goodput: 3-Day CE Lab Pre-Staging Script (ikwak-reliability)
# ==============================================================================
# Pre-stages all real breadcrumbs in Cloud Logging & Kubernetes (namespace: default)
# for the 3-Day GPU Reliability & Observability CE Lab:
#
#   - Day 1 (Observability, Goodput & Silent Brownout Straggler):
#       1) `dapo-gemma3-straggler-driver`: NeMo-RL job log showing baseline step
#          time of 116.4s/step (Steps 1-5) degrading to 167.2s/step (Steps 6-12,
#          30.4% Goodput loss) while GPU_UTIL stays ~100% and XID count = 0.
#       2) `gpu-straggler-diag`: Per-GPU hardware & DCGM diagnostic dump across
#          both a4-highgpu-8g nodes (-34t3 and -6df3) showing GPU 7 (0000:cc:00.0)
#          on node -34t3 throttled from PCIe Gen5 x16 (19.77 GB/s) to PCIe Gen1 x16
#          (1.54 GB/s) with Tensor Core pipe active dropping from 98.2% to 68.1%.
#
#   - Day 2 (Troubleshooting Failures: CUDA OOM vs. One-Off XID 48 vs. Recurring XID 48 + Veritas):
#       1) `dapo-gemma3-run1-driver`: Application-level `torch.OutOfMemoryError: CUDA out of memory`.
#       2) `dapo-gemma3-run2-driver`: Crash from a one-off transient `XID 48` on node -6df3
#          (instance_id=921428950843228374, PCI 0000:89:00) -> remediated via GPU reset / reboot.
#       3) `dapo-gemma3-run2b-recurring-driver` + 5x recurring `XID 48` entries on node -34t3
#          (instance_id=174490015860863227, physical machine `yugrqvu-tl3`, PCI 0000:cc:00)
#          persisting across reboots -> requires R&R (`report-faulty-host`).
#
#   - Day 3 (Emergent Maintenance, Hard XID Failures & End-to-End Node Replacement + Ray Recovery):
#       1) `dapo-gemma3-run3-driver`: Job crash at 2026-09-24T20:31:46Z (+9.4s after XID 79/154/145/45).
#       2) `dapo-run11-recovery-orchestrator`: Full Run 11 recovery & Ray job resumption log
#          correlating the 2026-09-24 Cloud Logging timeline (20:31:38Z fault -> 20:57:46Z
#          upcomingMaintenance PENDING -> 20:57:59Z perform-maintenance=true -> 21:31:18Z
#          VM automaticRestart -> 21:33:24Z Node Ready [35m 25s] -> 21:37:42Z Ray 2/2 Ready
#          [39m 43s] -> 21:49:55Z Step 1/500 resumed [12m 13s after Ray healthy / 51m 56s
#          after label / 1h 18m 18s total from fault]).
#       3) `day3-maintenance-snapshot` ConfigMap preserving the captured B4/B5 JSON payloads.
# ==============================================================================
set -euo pipefail

NODE_34T3="${NODE_34T3:-gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3}"
NODE_6DF3="${NODE_6DF3:-gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3}"

echo "[Day 1] Staging dapo-gemma3-straggler-driver & gpu-straggler-diag pods..."
kubectl delete pod dapo-gemma3-straggler-driver gpu-straggler-diag -n default --ignore-not-found

cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-straggler-driver
  namespace: default
  labels:
    app: nemo-rl-driver
    day: "1"
    run: dapo-gemma3-straggler
spec:
  restartPolicy: Never
  containers:
  - name: ray-job-driver
    image: busybox:1.36
    command: ["/bin/sh", "-c"]
    args:
    - |
      cat <<'LOG'
      [Day 1 Observability & DCGM Telemetry] Job: dapo-gemma3-straggler (Model: google/gemma-3-27b-it, Nodes: 2x a4-highgpu-8g = 16x NVIDIA B200 GPUs)
      [Baseline Reference (Steps 1-5)] step_time: 116.4s | DCGM_FI_DEV_GPU_UTIL: 99.4% | DCGM_FI_DEV_FB_USED: 145408 MiB / 183359 MiB | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 98.1% (0.981) | DCGM_FI_DEV_POWER_USAGE: 885 W | goodput: 100.0%
      [Step 1/50] step_time: 116.2s | DCGM_FI_DEV_GPU_UTIL: 99.4% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 98.2% | DCGM_FI_DEV_POWER_USAGE: 886 W | tokens/sec: 14,120 | goodput: 100.0% | status: HEALTHY
      [Step 2/50] step_time: 116.5s | DCGM_FI_DEV_GPU_UTIL: 99.6% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 98.0% | DCGM_FI_DEV_POWER_USAGE: 884 W | tokens/sec: 14,115 | goodput: 99.9%  | status: HEALTHY
      [Step 3/50] step_time: 116.3s | DCGM_FI_DEV_GPU_UTIL: 99.5% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 98.1% | DCGM_FI_DEV_POWER_USAGE: 885 W | tokens/sec: 14,122 | goodput: 100.0% | status: HEALTHY
      [Step 4/50] step_time: 116.6s | DCGM_FI_DEV_GPU_UTIL: 99.7% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 98.1% | DCGM_FI_DEV_POWER_USAGE: 887 W | tokens/sec: 14,110 | goodput: 99.8%  | status: HEALTHY
      [Step 5/50] step_time: 116.4s | DCGM_FI_DEV_GPU_UTIL: 99.5% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 98.1% | DCGM_FI_DEV_POWER_USAGE: 885 W | tokens/sec: 14,118 | goodput: 100.0% | status: HEALTHY
      [WARN] Step-time SLO breach detected starting at Step 6 (threshold > 130.0s/step)! Note that DCGM_FI_DEV_GPU_UTIL still reports 99.1% because 15 healthy GPUs spin-poll inside NCCL AllReduce wait kernels while waiting on 1 straggler GPU!
      [Step 6/50] step_time: 167.2s | DCGM_FI_DEV_GPU_UTIL: 99.1% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 68.1% (0.681) | DCGM_FI_DEV_POWER_USAGE: 612 W | tokens/sec: 9,830 | effective_goodput: 69.6% (goodput_loss: 30.4%, slowdown: 1.44x) | status: BROWNOUT_STRAGGLER
      [Step 7/50] step_time: 167.5s | DCGM_FI_DEV_GPU_UTIL: 99.2% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 68.0% (0.680) | DCGM_FI_DEV_POWER_USAGE: 610 W | tokens/sec: 9,825 | effective_goodput: 69.5% (goodput_loss: 30.5%, slowdown: 1.44x) | status: BROWNOUT_STRAGGLER
      [Step 8/50] step_time: 167.1s | DCGM_FI_DEV_GPU_UTIL: 99.0% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 68.2% (0.682) | DCGM_FI_DEV_POWER_USAGE: 613 W | tokens/sec: 9,832 | effective_goodput: 69.7% (goodput_loss: 30.3%, slowdown: 1.44x) | status: BROWNOUT_STRAGGLER
      [Step 9/50] step_time: 167.2s | DCGM_FI_DEV_GPU_UTIL: 99.1% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 68.1% (0.681) | DCGM_FI_DEV_POWER_USAGE: 611 W | tokens/sec: 9,828 | effective_goodput: 69.6% (goodput_loss: 30.4%, slowdown: 1.44x) | status: BROWNOUT_STRAGGLER
      [Step 10/50] step_time: 167.0s | DCGM_FI_DEV_GPU_UTIL: 99.1% | DCGM_FI_PROF_PIPE_TENSOR_ACTIVE: 68.1% (0.681) | DCGM_FI_DEV_POWER_USAGE: 612 W | tokens/sec: 9,830 | effective_goodput: 69.7% (goodput_loss: 30.3%, slowdown: 1.44x) | status: BROWNOUT_STRAGGLER
      [DCGM Interpretation Guide] Why is DCGM_FI_DEV_GPU_UTIL still 99.1% during a 30.4% Goodput drop? Because DCGM_FI_DEV_GPU_UTIL only measures whether any CUDA kernel (including an NCCL spin-wait polling loop) is active on the GPU. Inspect 'gpu-straggler-diag' to compare DCGM_FI_PROF_PIPE_TENSOR_ACTIVE, DCGM_FI_DEV_POWER_USAGE, and DCGM_FI_PROF_PCIE_RX_BYTES across all 16 GPUs!
      LOG
      exit 0
EOF

cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: gpu-straggler-diag
  namespace: default
  labels:
    app: gpu-straggler-diag
    day: "1"
spec:
  restartPolicy: Never
  containers:
  - name: diag
    image: busybox:1.36
    command: ["/bin/sh", "-c"]
    args:
    - |
      cat <<'LOG'
      [GPU Platform Engineer DCGM Telemetry Breakdown] Cluster: ikwak-reliability (2x a4-highgpu-8g = 16x NVIDIA B200)
      [1] Cluster-Wide DCGM Metric Comparison: Steps 1-5 (Healthy Baseline) vs. Steps 6-10 (Silent Brownout)
          Metric Name                                 | Steps 1-5 (Healthy) | Steps 6-10 (Brownout) | How to Interpret What You See
          --------------------------------------------+---------------------+-----------------------+-------------------------------------------------------------------------
          DCGM_FI_DEV_GPU_UTIL (Coarse GPU Util %)    | 99.4%               | 99.1% (UNCHANGED!)    | Misleading during brownouts: counts NCCL spin-wait polling loops as 100% busy!
          DCGM_FI_DEV_FB_USED / FB_TOTAL (Memory MiB) | 145,408 / 183,359   | 145,408 / 183,359     | Model weights + KV/activations remain allocated in HBM3e (~142 GiB / 179 GiB).
          DCGM_FI_PROF_PIPE_TENSOR_ACTIVE (HMMA Math) | 98.1% (0.981)       | 68.1% (0.681) (-30%!) | True compute indicator: Tensor Cores are idle 30% of the step waiting on barrier!
          DCGM_FI_DEV_POWER_USAGE (Avg GPU Power W)   | 885 W               | 612 W (-273 W drop!)  | Spinning in NCCL wait draws much less power than Tensor Core FP16/BF16 GEMMs.
      [2] Per-Node & Per-GPU DCGM Breakdown During Steps 6-10 Brownout (Isolating the 1 Culprit GPU from the 15 Victim GPUs):
          Node: gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3 (instance_id=921428950843228374)
            GPUs 0..7 (PCI 0000:8f:00.0 .. 0000:cc:00.0): GPU_UTIL=99.1% | PIPE_TENSOR_ACTIVE=68.4% (0.684) | POWER_USAGE=615 W | PCIE_RX_BYTES=19.78 GB/s (PCIe Gen5 x16) | NVLINK_TX_BYTES=368.4 GB/s | STATUS=VICTIM_WAITING_IN_NCCL_BARRIER
          Node: gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3 (instance_id=174490015860863227, machine=yugrqvu-tl3)
            GPU 0 (PCI 0000:8f:00.0): GPU_UTIL=99.2% | PIPE_TENSOR_ACTIVE=68.2% (0.682) | POWER_USAGE=614 W | PCIE_RX_BYTES=19.77 GB/s (PCIe Gen5 x16) | NVLINK_TX_BYTES=368.4 GB/s | STATUS=VICTIM_WAITING_IN_NCCL_BARRIER
            GPU 1 (PCI 0000:90:00.0): GPU_UTIL=99.1% | PIPE_TENSOR_ACTIVE=68.3% (0.683) | POWER_USAGE=616 W | PCIE_RX_BYTES=19.76 GB/s (PCIe Gen5 x16) | NVLINK_TX_BYTES=368.5 GB/s | STATUS=VICTIM_WAITING_IN_NCCL_BARRIER
            GPU 2 (PCI 0000:96:00.0): GPU_UTIL=99.0% | PIPE_TENSOR_ACTIVE=68.1% (0.681) | POWER_USAGE=613 W | PCIE_RX_BYTES=19.79 GB/s (PCIe Gen5 x16) | NVLINK_TX_BYTES=368.3 GB/s | STATUS=VICTIM_WAITING_IN_NCCL_BARRIER
            GPU 3 (PCI 0000:97:00.0): GPU_UTIL=99.2% | PIPE_TENSOR_ACTIVE=68.2% (0.682) | POWER_USAGE=615 W | PCIE_RX_BYTES=19.75 GB/s (PCIe Gen5 x16) | NVLINK_TX_BYTES=368.4 GB/s | STATUS=VICTIM_WAITING_IN_NCCL_BARRIER
            GPU 4 (PCI 0000:c4:00.0): GPU_UTIL=99.1% | PIPE_TENSOR_ACTIVE=68.1% (0.681) | POWER_USAGE=614 W | PCIE_RX_BYTES=19.77 GB/s (PCIe Gen5 x16) | NVLINK_TX_BYTES=368.4 GB/s | STATUS=VICTIM_WAITING_IN_NCCL_BARRIER
            GPU 5 (PCI 0000:c5:00.0): GPU_UTIL=99.3% | PIPE_TENSOR_ACTIVE=68.4% (0.684) | POWER_USAGE=617 W | PCIE_RX_BYTES=19.78 GB/s (PCIe Gen5 x16) | NVLINK_TX_BYTES=368.5 GB/s | STATUS=VICTIM_WAITING_IN_NCCL_BARRIER
            GPU 6 (PCI 0000:cb:00.0): GPU_UTIL=99.0% | PIPE_TENSOR_ACTIVE=68.0% (0.680) | POWER_USAGE=612 W | PCIE_RX_BYTES=19.76 GB/s (PCIe Gen5 x16) | NVLINK_TX_BYTES=368.3 GB/s | STATUS=VICTIM_WAITING_IN_NCCL_BARRIER
            GPU 7 (PCI 0000:cc:00.0): GPU_UTIL=99.4% | PIPE_TENSOR_ACTIVE=68.1% (0.681) | POWER_USAGE=342 W | PCIE_RX_BYTES=1.54 GB/s  (PCIe Gen1 x16 - 12.8x SLOWER!) | NVLINK_TX_BYTES=368.2 GB/s | STATUS=ROOT_CAUSE_STRAGGLER
      [Root Cause Summary] While all 16 GPUs show ~99% DCGM_FI_DEV_GPU_UTIL and ~68.1% DCGM_FI_PROF_PIPE_TENSOR_ACTIVE (because synchronous NCCL barriers lock all ranks to the slowest rank), grouping DCGM_FI_PROF_PCIE_RX_BYTES and DCGM_FI_DEV_POWER_USAGE by GPU immediately isolates Node -34t3 GPU 7 (PCI 0000:cc:00.0): its PCIe link downgraded from Gen5 x16 (19.77 GB/s) to Gen1 x16 (1.54 GB/s, 342 W), bottlenecking host-to-device batch staging and slowing step time from 116.4s to 167.2s (30.4% Goodput loss).
      LOG
      exit 0
EOF

echo "[Day 2] Staging CUDA OOM pod, One-Off XID 48 pod, and Recurring XID 48 pod + serial console logs..."
kubectl delete pod dapo-gemma3-run1-driver dapo-gemma3-run2-driver dapo-gemma3-run2b-recurring-driver -n default --ignore-not-found

cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-run1-driver
  namespace: default
  labels:
    app: nemo-rl-driver
    day: "2"
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
      [Submitting Ray Job] dapo-gemma3-run1 (Cluster: 2 nodes, 16x NVIDIA B200 GPUs)
      [Config] model=google/gemma-3-27b-it | rollout.max_model_len=32768 | train_micro_batch_size=8
      [Step 1/100] Starting rollout generation & FSDP2 forward/backward pass across 16 GPUs...
      (DTensorPolicyWorkerV2 pid=18422, ip=192.168.0.22, node=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3)
      Traceback (most recent call last):
        File "/opt/nemo-rl/examples/run_grpo.py", line 241, in main
          grpo_train(policy, rollout_workers, dataloader, config)
        File "/opt/nemo-rl/nemo_rl/algorithms/grpo.py", line 418, in grpo_train
          loss = policy.train_step(batch)
      torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 14.50 GiB. GPU 0 has a total capacity of 178.35 GiB of which 2.12 GiB is free. Including non-PyTorch memory, this process has 176.18 GiB memory in use.
      ray.exceptions.RayTaskError(OutOfMemoryError): ray::DTensorPolicyWorkerV2.train_step() (pid=18422)
      [Job Status] dapo-gemma3-run1 FAILED (ExitCode: 1) — Application-level CUDA OOM (0 XID hardware errors).
      LOG
      exit 1
EOF

cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-run2-driver
  namespace: default
  labels:
    app: nemo-rl-driver
    day: "2"
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
      [Submitting Ray Job] dapo-gemma3-run2 (train_micro_batch_size=2, max_model_len=8192)
      [Step 1/100] step_time: 116.1s | reward_mean: 0.241 | status: OK
      ...
      [Step 17/100] step_time: 115.8s | reward_mean: 0.289 | status: OK
      [Step 18/100] Training aborted on host gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3 (instance_id=921428950843228374):
      RuntimeError: NCCL error in: /pytorch/torch/csrc/distributed/c10d/ProcessGroupNCCL.cpp:1842, unhandled cuda error (run with NCCL_DEBUG=INFO for details), NCCL version 2.26.2
      ncclUnhandledCudaError: Call to CUDA function failed.
      [Job Status] dapo-gemma3-run2 FAILED (ExitCode: 1) — Check serialconsole.googleapis.com/serial_port_1_output on node -6df3 (921428950843228374) for NVRM: Xid!
      LOG
      exit 1
EOF

cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-run2b-recurring-driver
  namespace: default
  labels:
    app: nemo-rl-driver
    day: "2"
    run: dapo-gemma3-run2b-recurring
spec:
  restartPolicy: Never
  containers:
  - name: ray-job-driver
    image: busybox:1.36
    command: ["/bin/sh", "-c"]
    args:
    - |
      cat <<'LOG'
      [Recurring Fault Investigation — Node -34t3 (instance_id=174490015860863227, Physical Machine: yugrqvu-tl3)]
      [Iteration 1] Job crashed with XID 48 on PCI:0000:cc:00 -> Operator applied cloud.google.com/perform-reboot=true -> Node rebooted (bootID changed) -> upcomingMaintenance: null
      [Iteration 2] Job crashed AGAIN with XID 48 on PCI:0000:cc:00 (+22m later) -> Operator applied perform-reboot=true -> upcomingMaintenance: null
      [Iteration 3] Job crashed AGAIN with XID 48 on PCI:0000:cc:00 (+22m later) -> Operator applied perform-reboot=true -> upcomingMaintenance: null
      [Iteration 4] Job crashed AGAIN with XID 48 on PCI:0000:cc:00 (+22m later) -> Operator applied perform-reboot=true -> upcomingMaintenance: null
      [Iteration 5] Job crashed a 5th time with XID 48 on PCI:0000:cc:00 on physical machine yugrqvu-tl3!
      [Veritas & Cloud Logging Correlation]
        - Physical Machine ID in Veritas: yugrqvu-tl3
        - Instance ID: 174490015860863227 (gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3)
        - Observation: Unlike node -6df3 (which had a single one-off XID 48 fixed by a GPU reset/reboot), node -34t3 (machine yugrqvu-tl3) exhibits RECURRING XID 48 errors on GPU 7 (0000:cc:00) across 5 consecutive runs, while GCE upcomingMaintenance remains null.
        - Action Required: Cordon & drain node -34t3 and invoke Report & Replace (R&R) API (`gcloud compute instances report-faulty-host`).
      LOG
      exit 1
EOF

echo "[Day 2] Emitting 5x recurring XID 48 lines to /dev/kmsg on node -34t3 (174490015860863227 / yugrqvu-tl3) so they are visible in Cloud Logging alongside the 1x one-off XID 48 on -6df3..."
kubectl delete pod xid48-recurring-stage -n default --ignore-not-found
kubectl run xid48-recurring-stage -n default --rm -i --restart=Never \
  --image=busybox:1.36 \
  --overrides="{
    \"spec\": {
      \"nodeName\": \"$NODE_34T3\",
      \"hostPID\": true,
      \"tolerations\": [{\"operator\": \"Exists\"}],
      \"containers\": [{
        \"name\": \"stage\",
        \"image\": \"busybox:1.36\",
        \"securityContext\": {\"privileged\": true},
        \"command\": [\"nsenter\", \"-t\", \"1\", \"-m\", \"-p\", \"--\", \"sh\", \"-c\", \"for i in 1 2 3 4 5; do echo \\\"NVRM: Xid (PCI:0000:cc:00): 48, pid=0, name=CLAUDE-SIM-RUN8-I\${i}, An uncorrectable double bit error (DBE) has been detected on GPU in the framebuffer at partition 3, subpartition 0.\\\" > /dev/kmsg; sleep 1; done\"]
      }]
    }
  }" || echo "Note: Skipping kmsg write if node is unavailable."

echo "[Day 3] Staging dapo-gemma3-run3-driver & dapo-run11-recovery-orchestrator pods..."
kubectl delete pod dapo-gemma3-run3-driver dapo-run11-recovery-orchestrator -n default --ignore-not-found

cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-run3-driver
  namespace: default
  labels:
    app: nemo-rl-driver
    day: "3"
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
      [2026-09-24 20:15:00 UTC] Submitting Ray job: dapo-run10-recovered (2 nodes, 16x B200 GPUs)
      [2026-09-24 20:29:40 UTC] Step 6/100 complete | step_time: 116.41s | reward_mean: 0.258
      [2026-09-24 20:31:37.078 UTC] Hardware fault occurred on gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3 (instance_id=174490015860863227, PCI 0000:cc:00)
      [2026-09-24 20:31:38.086 UTC] Serial Console: NVRM: Xid (PCI:0000:cc:00): 79, GPU has fallen off the bus + Xid 154 (Node Reboot Required) + Xid 145 (NVLink) + Xid 45
      [2026-09-24 20:31:46.469 UTC] ray.exceptions.ActorUnavailableError: The actor died unexpectedly before finishing this task (rpc_code: 14 Socket closed, +9.4s after fault)
      [Job Status] FAILED — Query Cloud Logging on 2026-09-24T20:30:00Z..21:55:00Z for instance_id=174490015860863227 to trace the Emergent Maintenance & Node Replacement timeline!
      LOG
      exit 1
EOF

cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-run11-recovery-orchestrator
  namespace: default
  labels:
    app: nemo-rl-orchestrator
    day: "3"
    run: dapo-run11-recovered
spec:
  restartPolicy: Never
  containers:
  - name: orchestrator
    image: busybox:1.36
    command: ["/bin/sh", "-c"]
    args:
    - |
      cat <<'LOG'
      === Run 11 End-to-End Emergent Maintenance, Node Replacement & Ray Job Resumption Log (2026-09-24) ===
      2026-09-24T20:30:51.882Z ORCH_START node=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3 instance_id=174490015860863227 old_lastStart=2026-09-24T15:12:19.474Z
      2026-09-24T20:31:37.078Z B0_FAULT_INJECTED PCIe SBR on GPU 7 (0000:cc:00.0)
      2026-09-24T20:31:38.086Z B1_XID_LOGGED NVRM: Xid (PCI:0000:cc:00): 79, GPU has fallen off the bus (+1.0s) & Xid 154 (Node Reboot Required) on all 8 GPUs
      2026-09-24T20:31:46.469Z B2_JOB_FAILED dapo-run10-recovered failed (+9.4s from fault)
      2026-09-24T20:32:07.466Z B4_RESERVATION_DEGRADED reservation=nvidia-b200-6bsoymep8ylww healthStatus=DEGRADED (+30.4s from fault)
      2026-09-24T20:57:46.921Z B5_AUDIT_LOG compute.instances.upcomingMaintenance: status=PENDING type=UNSCHEDULED canReschedule=true reasons=FAILURE_GPU_XID,FAILURE_GPU window=2026-10-02T00:00:00Z..04:00:00Z (+26m 12s from fault)
      2026-09-24T20:57:59.390Z B7_LABEL_ACK kubectl label node gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3 cloud.google.com/perform-maintenance=true (0m 00s of repair clock)
      2026-09-24T20:58:46.868Z B8_MAINT_ONGOING compute.instances.upcomingMaintenance: status=ONGOING canReschedule=false window=2026-09-24T20:58:11Z..2026-09-25T00:58:01Z (+51.2s from label)
      2026-09-24T21:12:22.597Z B10_NODE_DRAINED Node NotReady; KubeRay created replacement worker pod ray-cluster-kuberay-worker-grp-0-worker-lsfdm (+14m 23s from label)
      2026-09-24T21:14:47.407Z B11_VM_REPAIRING terminateOnHostMaintenance -> restartInPlaceAttempting -> restartOnANewMachineDueToRipTimeout (+17m 07s from label)
      2026-09-24T21:25:57.265Z RESERVATION_HEALTHY_EARLY reservation flipped DEGRADED->HEALTHY while VM still REPAIRING (+27m 58s from label — 5m 29s before VM boot!)
      2026-09-24T21:31:18.439Z B12_VM_RESTARTED compute.instances.automaticRestart | new lastStartTimestamp=2026-09-24T21:31:26.374Z (+33m 27s from label)
      2026-09-24T21:33:24.000Z B13_NODE_READY_8GPU Node Ready=True with 8 allocatable B200 GPUs; pod ray-cluster-kuberay-worker-grp-0-worker-lsfdm Scheduled (+35m 25s from label / +1h 01m 47s from fault)
      2026-09-24T21:37:42.000Z B13b_RAY_WHOLE Pod ray-cluster-kuberay-worker-grp-0-worker-lsfdm Ready after 3m07s image pull — Ray cluster 2/2 workers healthy (+39m 43s from label / +4m 18s after Node Ready / +1h 06m 05s from fault)
      2026-09-24T21:38:34.397Z B14_SUBMITTED prep-pods.sh complete (42s) & job=dapo-run11-recovered submitted (+40m 35s from label)
      2026-09-24T21:49:55.573Z B14_FIRST_STEP_STARTED Training Step 1/500 began (+12m 13s after Ray healthy / +51m 56s after perform-maintenance label / +1h 18m 18s total end-to-end from XID 79 fault!)
      2026-09-24T21:52:15.793Z B14_FIRST_STEP_COMPLETE Step 1 finished in 120.56s; Step 2/500 began
      2026-09-25T00:58:07.127Z WINDOW_END_TRAP upcomingMaintenance.maintenanceStatus finally cleared ONGOING->CLEAR (+4h 00m 08s after label — 3h 24m AFTER node was already Ready!)
      LOG
      exit 0
EOF

echo "[Done] All Day 1, Day 2, and Day 3 lab pods and serial console logs staged on ikwak-reliability!"
