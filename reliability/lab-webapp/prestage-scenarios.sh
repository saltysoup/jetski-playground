#!/bin/bash
# ==============================================================================
# Operation Goodput: Hands-Off Pre-Staging Script (Run ONCE Before the CE Lab)
# ==============================================================================
# This script pre-generates all 5 Missions (Days 1-5) of forensic artifacts on
# the GKE cluster (`ikwak-reliability`) and Cloud Logging so that the instructor
# does NOT need to inject faults live during the session, and CEs can work
# through Mission 1 -> 2 -> 3 -> 4 -> 5 at their own pace with 100% Read-Only
# cluster/project access (zero compute creation or write permissions required).
#
# What it stages in namespace `default`:
#   - Mission 1 (Day 1 - Baseline, In-Band vs. Out-of-Band & Checkpointing):
#       * ConfigMap `day1-baseline-telemetry` (step time 116.39s, t_rm 721.5s)
#       * ConfigMap `day1-telemetry-audit` (GKE DCGM missing field 319 vs. GCE/CLAM)
#       * ConfigMap `day1-checkpoint-audit` (container overlay + tmp_step_10 defect)
#       * Pod `dapo-gemma3-run1-driver` (Error: PyTorch CUDA OutOfMemoryError)
#   - Mission 2 (Day 2 - Crashes, NCCL Hang, HostError RCA & R&R API):
#       * Pod `dapo-gemma3-run2-nccl-stall` (Error: eth3 RDMA link down -> NCCL timeout)
#       * ConfigMap `day2-hosterror-rca` (compute.instances.hostError + Veritas/CLAM)
#       * ConfigMap `day2-rr-api-reference` (report-faulty-host / reportHostAsFaulty)
#   - Mission 3 (Day 3 - HotGPU Thermal, XID 48/63/79 & Emergent Maintenance):
#       * ConfigMap `day3-hotgpu-telemetry` (GPU 3 on -6df3 at 89C / 540 MHz straggler)
#       * Pod `dapo-gemma3-run2-driver` + one-shot `/dev/kmsg` XID 48 on `-6df3`
#       * ConfigMap `day3-xid-matrix` (XID 48 vs. 3x XID 63 vs. XID 79 empirical rules)
#       * Pod `dapo-gemma3-run3-driver` + ConfigMap `mission3-maintenance-snapshot`
#   - Mission 4 (Day 4 - Planned Maintenance 30-60d, Readiness Traps & CHS/NCCL):
#       * ConfigMap `day4-planned-maintenance` (schedulingType: GROUPED, PLANNED_UPDATE)
#       * ConfigMap `day4-readiness-traps` (-5.5m HEALTHY trap, +4h windowEndTime trap)
#       * ConfigMap `day4-chs-nccl-scan` (Cluster Health Scanner + 365.36 GB/s NCCL PASS)
#   - Mission 5 (Day 5 - SLA/SLOs, Raw vs. Managed, GKE vs. Slurm & Capstone):
#       * ConfigMap `day5-architecture-matrix` (Run 7 vs. Run 11, Raw/Managed, K8s/Slurm)
#       * ConfigMap `day5-capstone-incident` (INC-500: 3x XID 63 + XID 64 bank exhaustion)
# ==============================================================================
set -euo pipefail

NODE_34T3="${NODE_34T3:-gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3}"
NODE_6DF3="${NODE_6DF3:-gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3}"

echo "[1/5] Staging Mission 1 (Day 1: Baseline, Telemetry Audit, Checkpoint Audit & CUDA OOM)..."
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: day1-baseline-telemetry
  namespace: default
data:
  workload: "NeMo-RL v0.6.0 DAPO on google/gemma-3-27b-it (FSDP2 across 16x B200 GPUs)"
  step_time_mean_sec: "116.39"
  step_time_p50_sec: "118.08"
  step_time_stdev_sec: "8.02"
  checkpoint_save_blocking_sec: "49.14"
  worker_init_t_rm_warm_sec: "721.5 (12m 01s: vLLM init 428.9s + FSDP2 policy init 93.7s + setup 195.9s)"
  cold_image_pull_sec: "450.0 (20.1 GB nvcr.io/nvidia/nemo-rl:v0.6.0)"
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: day1-telemetry-audit
  namespace: default
data:
  gke_managed_dcgm_present_21_families: |
    DCGM_FI_DEV_SM_CLOCK, DCGM_FI_DEV_MEMORY_TEMP, DCGM_FI_DEV_GPU_TEMP,
    DCGM_FI_DEV_POWER_USAGE, DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION,
    DCGM_FI_DEV_GPU_UTIL, DCGM_FI_DEV_MEM_COPY_UTIL,
    DCGM_FI_DEV_FB_TOTAL, DCGM_FI_DEV_FB_FREE, DCGM_FI_DEV_FB_USED,
    DCGM_FI_PROF_GR_ENGINE_ACTIVE, DCGM_FI_PROF_SM_ACTIVE,
    DCGM_FI_PROF_PIPE_TENSOR_ACTIVE, DCGM_FI_PROF_DRAM_ACTIVE,
    DCGM_FI_PROF_PCIE_TX_BYTES, DCGM_FI_PROF_PCIE_RX_BYTES,
    DCGM_FI_PROF_NVLINK_TX_BYTES, DCGM_FI_PROF_NVLINK_RX_BYTES
  gke_managed_dcgm_MISSING_failure_fields: |
    - DCGM_FI_DEV_XID_ERRORS (Field ID 319) -> OMITTED!
    - ECC Counters (SBE/DBE volatile & aggregate) -> OMITTED!
    - Row Remap Counters (remapped rows / bank exhaustion) -> OMITTED!
    - NVLink Error Counters (CRC / recovery / replay errors) -> OMITTED!
  inband_vs_outofband_summary: |
    - In-Band (GKE / Guest VM DCGM): High-frequency SM/Tensor/NVLink throughput, but dies or freezes if the Guest OS hangs or GPU falls off the PCIe bus (XID 79).
    - Out-of-Band (GCE Host / Serial Console / CLAM / Reservation Health): Survives guest VM kernel panics and PCIe bus drops; feeds Google Host Health & Emergent Maintenance.
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: day1-checkpoint-audit
  namespace: default
data:
  head_pod_step_10: "/opt/nemo-rl/results/step_10/ -> 28 KB (config.yaml, training_info.json, train_dataloader.pt ONLY)"
  worker_a_34t3: "/opt/nemo-rl/results/tmp_step_10/ -> 152 GB (policy/weights/model/shard-*.safetensors)"
  worker_b_6df3: "/opt/nemo-rl/results/tmp_step_10/ -> 152 GB (policy/optimizer/optim/*.distcp)"
  defect_1_ephemeral_storage: "/opt/nemo-rl/results is the container OVERLAY filesystem (not a PersistentVolume, GCS FUSE, or Lustre mount). Pod restart or node repair destroys all 304 GB of weights!"
  defect_2_incomplete_rename: "tmp_step_10 -> step_10 atomic rename never completed on workers (and on non-HNS GCS FUSE buckets, directory rename fails with Errno 24 Too many open files unless mountOptions includes implicit-dirs,rename-dir-limit=200000)."
  defect_3_blocking_save: "Async mode disabled (PyTorch < 2.9.0); each checkpoint save blocks all 16 GPUs for 49.14s."
EOF

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

echo "[2/5] Staging Mission 2 (Day 2: NCCL Collective Hang, HostError RCA & R&R API)..."
kubectl delete pod dapo-gemma3-run2-nccl-stall -n default --ignore-not-found
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-run2-nccl-stall
  namespace: default
  labels:
    app: nemo-rl-driver
    mission: "2"
    run: dapo-gemma3-run2-nccl-stall
spec:
  restartPolicy: Never
  containers:
  - name: ray-job-driver
    image: busybox:1.36
    command: ["/bin/sh", "-c"]
    args:
    - |
      cat <<'LOG'
      [2026-10-06 14:20:11 UTC] Step 11/100 complete | step_time: 116.20s
      [2026-10-06 14:22:08 UTC] Step 12/100 in progress (FSDP2 AllReduce across 16 GPUs over gIB RDMA eth2-eth9)...
      [2026-10-06 14:22:12 UTC] gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3:204112:204189 [1] NCCL WARN NET/IB : mlx5_1:1 (interface eth3, IP 10.88.2.7) port 1 state DOWN / Retry counter exceeded on QP 0x14a8
      [2026-10-06 14:32:12 UTC] [Rank 9] Watchdog caught collective operation timeout: WorkNCCL(SeqNum=4182, OpType=ALLREDUCE, NumelIn=67108864, Timeout(ms)=600000) ran for 600084 ms before timing out.
      [2026-10-06 14:32:13 UTC] TORCH_NCCL_HEARTBEAT_TIMEOUT_SEC triggered dump -> aborting communicator on Rank 9 (node -6df3, GPU 1, NIC eth3).
      LOG
      exit 1
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: day2-hosterror-rca
  namespace: default
data:
  cloud_audit_log_system_event.json: |
    {
      "logName": "projects/gpu-launchpad-playground/logs/cloudaudit.googleapis.com%2Fsystem_event",
      "resource": {
        "type": "gce_instance",
        "labels": {
          "instance_id": "174490015860863227",
          "zone": "europe-west4-b"
        }
      },
      "protoPayload": {
        "methodName": "compute.instances.hostError",
        "status": {
          "message": "Instance terminated by Compute Engine due to a physical host error; automaticRestart triggered."
        }
      },
      "timestamp": "2026-10-05T03:14:09.112Z"
    }
  veritas_and_clam_rca_notes: |
    - Customer-Visible Signal: cloudaudit.googleapis.com/system_event with methodName="compute.instances.hostError" followed by "compute.instances.automaticRestart".
    - Internal CE/Eng RCA (Veritas & CLAM): Correlates physical hostId, out-of-band BMC/PCIe/NVSwitch telemetry (CLAM), and repair history to verify whether the defective physical host was quarantined (Outkast) so it cannot become a repeat "bad magnet".
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: day2-rr-api-reference
  namespace: default
data:
  when_to_use_rr_vs_perform_maintenance: |
    1. Customer-Detected Fault (upcomingMaintenance is NULL):
       Use the Report Faulty Host (R&R) API (`gcloud compute instances report-faulty-host` / REST `instances.reportHostAsFaulty` or GKE node label `cloud.google.com/report-faulty-host`).
    2. Google-Detected Fault (upcomingMaintenance is PENDING with canReschedule=true):
       Use Customer-Triggered Maintenance (`kubectl label node <NODE> cloud.google.com/perform-maintenance=true` or `gcloud compute instances perform-maintenance`).
  gcloud_rr_command_example: |
    gcloud compute instances report-faulty-host gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3 \
      --zone=europe-west4-b \
      --Async \
      --disruption-schedule=IMMEDIATE \
      --fault-reasons=behavior=PERFORMANCE,description="Persistent gIB RDMA eth3 QP retry timeout stalling NCCL AllReduce"
  supported_fault_reason_behaviors: |
    - PERFORMANCE (straggler / severe network or GPU degradation)
    - SILENT_DATA_CORRUPTION (SDC / NaN / gradient corruption tied to specific host)
    - UNRECOVERABLE_GPU_ERROR (XID / NVLink / PCIe fault not yet auto-flagged)
    - BEHAVIOR_UNSPECIFIED
EOF

echo "[3/5] Staging Mission 3 (Day 3: HotGPU Thermal Straggler, XID 48/63/79 & Emergent Maintenance)..."
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: day3-hotgpu-telemetry
  namespace: default
data:
  observed_step_time_sec: "167.2s (vs 116.4s baseline -> 1.44x slowdown across all 16 GPUs)"
  node_34t3_gpus_0_to_7: "GPU_TEMP: 57C..60C | SM_CLOCK: 1980 MHz | THROTTLE_REASON: 0x0 (None)"
  node_6df3_gpus_0_1_2_4_5_6_7: "GPU_TEMP: 58C..61C | SM_CLOCK: 1980 MHz | THROTTLE_REASON: 0x0 (None)"
  node_6df3_gpu_3_ANOMALY: |
    PCI: 0000:8a:00.0 (GPU 3 on gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3)
    DCGM_FI_DEV_GPU_TEMP: 89 C ( Memory Temp: 94 C )
    DCGM_FI_DEV_SM_CLOCK: 540 MHz (throttled down from 1980 MHz!)
    DCGM_FI_DEV_CLOCK_THROTTLE_REASONS: HW_THERMAL_SLOWDOWN | SW_THERMAL_SLOWDOWN
  triage_rule_node_vs_rack: |
    - Single GPU at 89C while sibling GPUs on the same baseboard are 58-61C = Isolated GPU heatsink/TIM/coldplate defect (Cordon & report-faulty-host on -6df3).
    - All 8 GPUs across multiple nodes in the same rack rising >80C together = Facility/Rack CDU coolant flow or inlet temperature excursion.
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: day3-xid-matrix
  namespace: default
data:
  serial_console_log_id: "serialconsole.googleapis.com/serial_port_1_output"
  empirical_xid_rules: |
    - XID 48 (Double-Bit ECC Error): 0/10 host repair triggers. Reset GPU (nvidia-smi --gpu-reset) or reboot GKE node (cloud.google.com/perform-reboot=true).
    - XID 63 (HBM Row Remap Recording): 1-2 events = local GPU reset; 3 consecutive XID 63 events = flips reservation DEGRADED in 16.6s and schedules FAILURE_GPU_XID host repair in ~11m.
    - XID 64 (Row Remap Failure / Bank Exhausted): Immediate host repair trigger.
    - XID 79 (GPU Fallen Off Bus): Single-shot host repair trigger (cascades to XID 154 across all 8 GPUs).
EOF

kubectl delete pod dapo-gemma3-run2-driver -n default --ignore-not-found
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: dapo-gemma3-run2-driver
  namespace: default
  labels:
    app: nemo-rl-driver
    mission: "3"
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
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: mission3-maintenance-snapshot
  namespace: default
data:
  serial_console_xid_burst: |
    [ 4182.104821] NVRM: Xid (PCI:0000:cc:00): 79, pid=0, name=, GPU has fallen off the bus.
    [ 4182.741902] NVRM: Xid (PCI:0000:04:00..cc:00): 154, GPU recovery action changed from 0x0 (None) to 0x1 (Node Reboot Required) [ALL 8 GPUs]
    [ 4183.110492] NVRM: Xid (PCI:0000:89:00): 145, NVLink: fatal error detected on link
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

echo "[4/5] Staging Mission 4 (Day 4: 30-60d Planned Maintenance, Readiness Traps & CHS/NCCL Validation)..."
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: day4-planned-maintenance
  namespace: default
data:
  reservation_describe.yaml: |
    name: nvidia-b200-6bsoymep8ylww
    zone: europe-west4-b
    enableEmergentMaintenance: true
    schedulingType: GROUPED
    specificReservation:
      count: "2"
      inUseCount: "2"
      assuredCount: "2"
    resourceStatus:
      healthInfo:
        healthStatus: HEALTHY
        healthyBlockCount: 1
        degradedBlockCount: 0
      reservationMaintenance:
        maintenanceOngoingCount: 0
        maintenancePendingCount: 1
        schedulingType: GROUPED
        upcomingGroupMaintenance:
          type: SCHEDULED
          maintenanceStatus: PENDING
          canReschedule: true
          windowStartTime: "2026-11-15T08:00:00Z"
          windowEndTime: "2026-11-15T12:00:00Z"
          maintenanceReasons:
          - PLANNED_UPDATE
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: day4-readiness-traps
  namespace: default
data:
  repair_mechanism: "IN-PLACE (Same K8s Node Name, Same K8s UID, Same GCE Instance ID 174490015860863227)"
  pre_repair_lastStartTimestamp: "2026-09-24T15:12:19.474Z"
  post_repair_lastStartTimestamp: "2026-09-24T21:31:26.374Z (+33m 27s after label: VM RUNNING)"
  k8s_node_ready_8_gpus_at: "+35m 25s (Ready=True | allocatable nvidia.com/gpu: 8)"
  trap_1_reservation_healthy_early: "+27m 58s (Reservation flipped HEALTHY 5m 29s BEFORE VM booted!)"
  trap_2_windowEndTime_ongoing_late: "+4h 00m 06s (upcomingMaintenance stayed ONGOING until windowEndTime — 3h 24m AFTER node was Ready!)"
  correct_readiness_gate: "Check (lastStartTimestamp != OLD_START) AND (Node Ready == True) AND (allocatable nvidia.com/gpu == 8)"
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: day4-chs-nccl-scan
  namespace: default
data:
  scanner_tool: "Google Cloud Cluster Health Scanner (CHS) + nccl-tests (all_reduce_perf)"
  post_repair_scan_node_34t3_and_6df3: |
    [1/4] GPU Inventory & Driver Check: 16/16 NVIDIA B200 GPUs online, Driver 570.124.06 -> PASS
    [2/4] ECC & Row Remap Check: 0 volatile DBEs, 0 pending row remaps, bank availability 100% -> PASS
    [3/4] PCIe & NVLink Topology Check: All 16 GPUs at PCIe Gen5 x16 (64 GB/s), 18/18 NVLink5 links ACTIVE -> PASS
    [4/4] Multi-Node 16-GPU NCCL all_reduce_perf (8 GiB payload over gIB RDMA eth2-eth9 + NVLink):
          size(B)      count        type   redop    time(us)   algbw(GB/s)   busbw(GB/s)   #wrong
          8589934592   2147483648   float  sum      44082      194.86        365.36        0
          Threshold: >= 320.0 GB/s busbw (Degraded PCIe Gen1 / bad NIC baseline: 24.8 GB/s)
          Result: PASS (365.4 GB/s busbw) -> Node taint removed, ready for job admission!
EOF

echo "[5/5] Staging Mission 5 (Day 5: SLA/SLOs, Raw vs. Managed, GKE vs. Slurm & Capstone INC-500)..."
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: day5-architecture-matrix
  namespace: default
data:
  slo_and_goodput_summary: |
    - Operational North Star SLO: Runtime Goodput / Effective Training Time Ratio (ETTR) >= 90%.
    - Run 7 (Naive Manual Approval Gates on ikwak-reliability): 16h 04m 00s total outage ($1,940 wasted on 2 nodes; $57,740 equivalent on 64 nodes).
    - Run 11 (Automated orchestrate.sh on genuine PCIe SBR XID 79): 1h 18m 18s (78 minutes, 92% outage reduction!).
  raw_vs_managed_mode: |
    - HA (Managed) Mode: Google-managed spare buffer for automated host swaps.
    - All Capacity (Raw) Mode: Exposes 100% of allocated physical capacity (including buffer nodes) to the customer for warm standby pools, topology-aware scheduling, and explicit maintenance control across 8-GPU A3U/A4 nodes and 72-GPU A4X NVL72 racks.
  gke_vs_slurm_mapping: |
    | Reliability Capability         | Slurm on GCP                                  | GKE (Kubernetes) Equivalent                                           |
    | ------------------------------ | --------------------------------------------- | --------------------------------------------------------------------- |
    | Gang Scheduling & Job Queueing | sbatch / Slurm Scheduler + TopologyPlugin     | Kueue + JobSet (or KubeRay / LeaderWorkerSet) + TAS                   |
    | Pre-Job Health Check (Prolog)  | Slurm Prolog script (NHC / DCGM / NCCL check) | Cluster Health Scanner (CHS) + InitContainers + Node Problem Detector |
    | Post-Job Cleanup (Epilog)      | Slurm Epilog script (GPU reset / shm clean)   | Job completion hooks / DaemonSet GPU reset tool                       |
    | Quarantine / Drain Bad Node    | scontrol update NodeName=X State=DRAIN        | kubectl cordon <NODE> + kubectl drain                                 |
    | Trigger Host Repair / R&R      | slurm-gcp auto-repair / gcloud perform-maint  | cloud.google.com/perform-maintenance=true or report-faulty-host       |
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: day5-capstone-incident
  namespace: default
data:
  incident_id: "INC-500 (dapo-gemma3-capstone aborted at Step 42/100)"
  last_durable_checkpoint: "gs://ikwak-reliability-ckpts/dapo-gemma3-capstone/step_40 (Async checkpoint on GCS FUSE)"
  layer1_serial_console_node_6df3: |
    [28104.112] NVRM: Xid (PCI:0000:89:00): 63, Row Remapper: Row remapping event (1/3)
    [28219.440] NVRM: Xid (PCI:0000:89:00): 63, Row Remapper: Row remapping event (2/3)
    [28341.809] NVRM: Xid (PCI:0000:89:00): 63, Row Remapper: Row remapping event (3/3)
    [28342.004] NVRM: Xid (PCI:0000:89:00): 64, Row Remapper: All reserved rows for bank are remapped (Row Remap Failure)
  layer2_reservation_health: "DEGRADED (degradedBlockCount: 1)"
  layer4_upcoming_maintenance_node_6df3: |
    maintenanceStatus: PENDING
    type: UNSCHEDULED
    canReschedule: true
    maintenanceReasons: ["FAILURE_GPU_XID", "FAILURE_GPU"]
    windowStartTime: "2026-10-14T00:00:00Z (+7 days)"
EOF

echo "All 5 Missions (20 Forensic Breadcrumbs) staged cleanly in namespace 'default'!"
