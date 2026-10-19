# Operation Goodput: 5-Day GPU Reliability & Observability CE Breadcrumb Lab Guide

> **Aligned with:** [GPU Reliability and Observability Training - Agenda 09/2026](https://docs.google.com/spreadsheets/d/1PlKrUgABTZogirTUPgnPNHAliJRlGeCFSz4gzH1RMgQ/edit?gid=1288298866#gid=1288298866)
> **Interactive Web App:** `http://injaekwak.c.googlers.com:8787` (`/usr/local/google/home/ikwak/jetski-playground/reliability/lab-webapp/index.html`)
> **Structure:** **5 Lab Sections (Day 1 Lab – Day 5 Lab)** · **16 Breadcrumb Clues** · **16,000 Max Points** (`1,000 pts` per clue with 10-minute countdown timer)
> **Instructor Password:** `ilovejensen` (unlocks `🔒 Instructor Preview`, per-question `🔓 Reveal Answer (0 pts)`, and `🔒 Reset All Scores`)

---

## 1. 5-Day Training Agenda → Lab Mapping Overview

Each of the **5 Lab Sections** directly mirrors the topics taught on that day of the **GPU Reliability and Observability Training (09/2026)** curriculum:

| Lab Section | Training Agenda Modules Covered | Clues | Max Points | Core Detective & Remediation Skills |
| :--- | :--- | :---: | :---: | :--- |
| **Day 1 Lab** | `[Day 1.1]` End-to-End GPU Reliability Primer & Software Resiliency<br>`[Day 1.2]` Monitoring GPU Job Execution & ML Diagnostics<br>`[Day 1 Lab]` Exploring Job Execution Metrics & Dashboards | **3 Clues**<br>(`1.1` – `1.3`) | **3,000 pts** | • In-band GKE DCGM (`DCGM_FI_DEV_*`, missing Field `319` `DCGM_FI_DEV_XID_ERRORS`) vs. Out-of-band GCE telemetry<br>• Catching silent **PCIe Gen5 → Gen1 brownout** (`116.4s/step` → `167.2s/step`, `30.4%` Goodput loss, `0` XIDs)<br>• Minimizing $t_{cp}$ & $t_{rw}$ via async/hierarchical checkpointing |
| **Day 2 Lab** | `[Day 2.1]` Monitoring & Troubleshooting Job Crashes from Infra Issues<br>`[Day 2.2]` Troubleshooting a Faulty Node & Reporting (R&R API)<br>`[Day 2.3]` Root Cause Analysis (RCA) of Failures (Veritas + CLAM) | **3 Clues**<br>(`2.1` – `2.3`) | **3,000 pts** | • Separating application crashes (`torch.OutOfMemoryError: CUDA out of memory`) from infrastructure faults<br>• Tracing unplanned VM terminations via Cloud Audit Logs (`cloudaudit.googleapis.com/system_event` → `compute.instances.hostError` / `HOST_ERROR`) & Veritas/CLAM<br>• Cordoning/draining faulty nodes + calling the **Report & Replace (R&R) API (`gcloud compute instances report-faulty-host`)** |
| **Day 3 Lab** | `[Day 3.1]` Debugging Common GPU Failures: Hot GPU, CLAM & XIDs<br>`[Day 3.2]` Emergent Maintenance Notifications & Customer-Triggered Repairs<br>`[Day 3 Lab]` Troubleshooting GPU Failures & Triggering Maintenance | **4 Clues**<br>(`3.1` – `3.4`) | **4,000 pts** | • Querying `serialconsole.googleapis.com/serial_port_1_output` (`"NVRM: Xid"`, ~2s MTTD)<br>• Distinguishing transient `XID 48` (Double-Bit ECC) & `HotGPU` from fatal hardware faults; remediating `XID 48` via `nvidia-smi --gpu-reset` or `cloud.google.com/perform-reboot=true`<br>• Tracing `XID 79` (`GPU has fallen off the bus`) + `XID 154` into a 7-day `UNSCHEDULED` / `PENDING` **Emergent Maintenance** event (`FAILURE_GPU_XID`, `canReschedule: true`)<br>• Pulling host repair forward immediately via `kubectl label node <NODE> cloud.google.com/perform-maintenance=true` |
| **Day 4 Lab** | `[Day 4.1]` Overall Cluster Health & Planning for Upcoming Maintenance<br>`[Day 4 Lab]` Auditing Upcoming Maintenance Events & Cluster Health Scanner | **3 Clues**<br>(`4.1` – `4.3`) | **3,000 pts** | • Auditing 30–60 day upcoming maintenance & choosing **synchronized/grouped maintenance at checkpoints** for synchronous training vs. **staggered rolling upgrades** for inference<br>• Verifying ~35-minute **in-place host repair** via GCE `lastStartTimestamp` + K8s `Ready=True` (`8` GPUs) instead of waiting 4 hours for `windowEndTime` (`ONGOING`) or trusting early Reservation `HEALTHY`<br>• Qualifying repaired nodes with **Cluster Health Scanner (CHS)** & multi-node NCCL `all_reduce_perf` (`368.4 GB/s` `PASS`) |
| **Day 5 Lab** | `[Day 5.1]` GPU Reliability Best Practices: SLA/SLOs, Raw vs. Managed Mode & K8s vs. Slurm<br>`[Day 5 Lab]` End-to-End Troubleshooting Review & Wrap-Up | **3 Clues**<br>(`5.1` – `5.3`) | **3,000 pts** | • Comparing **HA (Managed) Mode** vs. **All-Capacity (Raw) Mode** + `N + K` hot-spares across `a3-ultragpu` / `a4-highgpu-8g` & `a4x` NVL72<br>• Comparing **GKE (Kubernetes)** vs. **Slurm** fault isolation & recovery primitives<br>• **Master 4-Incident Capstone Dispatch** across `XID 31`, `XID 48`, `3× XID 63`, and `XID 79` |

---

## 2. Complete Answer Key & Facilitator Walkthrough (Day 1 – Day 5)

### Day 1 Lab: GPU Reliability Primer, Job Execution Metrics & Brownout Stragglers

#### Breadcrumb 1.1 (`d1-c1`) — In-Band DCGM vs. Out-of-Band GCE Metrics (`[Day 1.2]`)
- **Question:** How many total B200 GPUs are in our 2-node cluster, and which critical XID field (Field ID or metric name) is **NOT exported by default** (`0 series`) in GKE Managed DCGM unless you customize the collector?
- **Accepted Answers:** `319`, `DCGM_FI_DEV_XID_ERRORS`, `xid_errors`, `0`, `not exported`, `16`
- **Key Takeaway:** Default GKE Managed DCGM exports 168 series per 8-GPU node (`DCGM_FI_DEV_GPU_UTIL`, `FB_USED`, `SM_CLOCK`, `GPU_TEMP`, `PCIE_REPLAY_COUNTER`), but omits Field `319` (`DCGM_FI_DEV_XID_ERRORS`) and ECC counters (`255`). Furthermore, if a VM freezes or a GPU drops off the PCIe bus (`VENDOR_ID=ffff`), in-band collectors lose visibility—requiring out-of-band GCE and serial console telemetry.

#### Breadcrumb 1.2 (`d1-c2`) — Diagnosing a Silent "Brownout" Straggler (`[Day 1.1]` & `[Day 1.2]`)
- **Question:** In Run 6, GPU 7 (`0000:cc:00.0`) on node `-34t3` degraded from PCIe Gen5 (`19.77 GB/s`) to PCIe Gen1 (`1.54 GB/s`) with 0 XIDs. What did the per-step training time slow down to (from `116.4s/step`), OR what percentage of cluster Goodput was silently lost?
- **Accepted Answers:** `167.2s`, `167.2`, `30.4%`, `30.4`, `1.44x`, `Gen1`, `1.54`
- **Key Takeaway:** Synchronous distributed training moves at the speed of the slowest rank. While GPU 7 took `12.8×` longer over PCIe Gen1, the other 15 B200 GPUs spun inside NCCL wait loops showing ~100% `GPU_UTIL`. Step time degraded from `116.4s/step` → `167.2s/step` (`30.4%` Goodput loss across all 16 GPUs).

#### Breadcrumb 1.3 (`d1-c3`) — Software-Layer Resiliency & Checkpointing (`[Day 1.1]`)
- **Question:** Which software-layer resiliency architecture best minimizes both $t_{cp}$ (checkpoint pause time) and $t_{rw}$ (lost training steps on failure)?
- **Correct Choice:** **Option C** — *Use asynchronous / hierarchical checkpointing (fast device-to-host RAM staging + background flush to Parallelstore/Lustre or GCS via Orbax/TorchSnapshot) paired with frequent checkpoint intervals and automated step-time watchdog alerts.*

---

### Day 2 Lab: Tracing Job Crashes, Veritas/CLAM RCA & Faulty Node Reporting (R&R API)

#### Breadcrumb 2.1 (`d2-c1`) — Crash #1 Triage: App Crash vs. Infra Crash (`[Day 2.1]`)
- **Question:** What is the exact Python/CUDA exception in `dapo-gemma3-run1-driver` that killed `dapo-gemma3-run1` at Step 1 (proving this is an application config issue and *not* a faulty node)?
- **Accepted Answers:** `CUDA out of memory`, `torch.OutOfMemoryError`, `OutOfMemoryError`, `OOM`
- **Key Takeaway:** Calling `report-faulty-host` on a `CUDA out of memory` crash wastes ~35 minutes replacing healthy hardware. Always check pod logs first.

#### Breadcrumb 2.2 (`d2-c2`) — Crash #2 RCA: HostError & Cloud Audit Logs (`[Day 2.1]` & `[Day 2.3]`)
- **Question:** What is the exact GCE system event `methodName` in `cloudaudit.googleapis.com/system_event` that proves an unplanned VM termination was caused by a physical host failure?
- **Accepted Answers:** `compute.instances.hostError`, `hostError`, `HOST_ERROR`, `FAILURE_GPU_HARDWARE`
- **Key Takeaway:** When a job aborts with a generic NCCL collective timeout across 16+ ranks, `cloudaudit.googleapis.com/system_event` immediately pinpoints which `instance_id` suffered `compute.instances.hostError`. For deeper post-incident RCA, CEs and SWEs correlate this with **Veritas** and **CLAM** (`go/gpu-clam-list`).

#### Breadcrumb 2.3 (`d2-c3`) — Faulty Node Isolation & Report & Replace (R&R) API (`[Day 2.2]`)
- **Question:** Select the correct operational workflow when a node is failing diagnostics or hanging NCCL and Google has *not* yet scheduled `upcomingMaintenance`.
- **Correct Choice:** **Option B** — *First isolate the node (`kubectl cordon <NODE>` and `kubectl drain <NODE> --ignore-daemonsets`), then invoke the Report & Replace API (`gcloud compute instances report-faulty-host <INSTANCE> --zone=<ZONE> --fault-reasons=behavior=...,description=...`) to stop the VM and trigger physical host repair/replacement.*

---

### Day 3 Lab: Debugging Hot GPUs, CLAM Metrics, XID Errors & Emergent Maintenance

#### Breadcrumb 3.1 (`d3-c1`) — Serial Console Forensics & XID 48 (`[Day 3.1]`)
- **Question:** Query `serialconsole.googleapis.com/serial_port_1_output` for node `-6df3`. What numeric XID error code fired on `PCI:0000:89:00`?
- **Accepted Answers:** `48`, `XID 48`, `Double Bit ECC`, `DBE`
- **Key Takeaway:** Always query `log_id("serialconsole.googleapis.com/serial_port_1_output")` (`0.06s` kernel emit → `2.07s` Cloud Logging ingestion), never `compute.googleapis.com/serial_port_1_output` (0 results).

#### Breadcrumb 3.2 (`d3-c2`) — XID 48 & HotGPU Remediation Playbook (`[Day 3.1]`)
- **Question:** What is the recommended Customer Action to recover from an isolated `XID 48` on a GKE node?
- **Correct Choice:** **Option B** — *Stop workloads on the node and reset the GPUs (`sudo nvidia-smi --gpu-reset` / GKE `gpu-reset-tool`) or reboot the GKE node VM (`kubectl label nodes <NODE_NAME> cloud.google.com/perform-reboot=true`) to clear the volatile ECC counter and apply any pending row remap.*

#### Breadcrumb 3.3 (`d3-c3`) — Catastrophic XID 79 & 7-Day Emergent Maintenance (`[Day 3.1]` & `[Day 3.2]`)
- **Question:** Enter either the fatal numeric XID code that dropped GPU 7 (`0000:cc:00.0`) off the PCIe bus on node `-34t3`, OR the exact reason string inside `upcomingMaintenance.maintenanceReasons`.
- **Accepted Answers:** `79`, `XID 79`, `FAILURE_GPU_XID`, `fallen off the bus`
- **Key Takeaway:** With Emergent Maintenance enabled (`--enable-emergent-maintenance`), GCE schedules an `UNSCHEDULED` / `PENDING` window **7+ days out** (`canReschedule: true`, `maintenanceReasons: ["FAILURE_GPU_XID", "FAILURE_GPU"]`). For non-fatal `3× XID 63` (Run 8d), the 7-day buffer lets your job finish; for fatal `XID 79` (Run 10/11), you pull the repair forward immediately!

#### Breadcrumb 3.4 (`d3-c4`) — Customer-Triggered Maintenance (`[Day 3.2]` & `[Day 3 Lab]`)
- **Question:** What GKE node label (`key=value`) or command do you apply to node `-34t3` to start the emergent host repair immediately?
- **Accepted Answers:** `cloud.google.com/perform-maintenance=true`, `perform-maintenance=true`, `perform-maintenance`

---

### Day 4 Lab: Planned Maintenance Windows, Repair Readiness & Cluster Health Scanner

#### Breadcrumb 4.1 (`d4-c1`) — Auditing Planned Maintenance & Upgrade Strategies (`[Day 4.1]`)
- **Question:** Which maintenance and node-pool upgrade strategy should you recommend for **Workload A (Synchronous 64-node Training)** vs. **Workload B (64-replica Stateless Inference)**?
- **Correct Choice:** **Option B** — *For Workload A (Synchronous Training), use GKE Maintenance Exclusions during critical runs and coordinate synchronized/grouped maintenance windows aligned with job checkpoints; for Workload B (Inference), use staggered rolling maintenance with surge/max-unavailable budgets.*

#### Breadcrumb 4.2 (`d4-c2`) — Verifying Node Repair & Avoiding the 4-Hour Trap (`[Day 4 Lab]`)
- **Question:** Is the GKE node repaired **in-place** (keeping the same Node Name & Instance ID) or replaced with a new Node Name, and what fields should your automation check instead of waiting 4 hours for `windowEndTime`?
- **Accepted Answers:** `in-place`, `lastStartTimestamp`, `Ready`, `8`, `unchanged`, `same`
- **Key Takeaway:** GCE repairs the host in-place in **35m 25s** (same Node Name `-34t3` and Instance ID `174490015860863227`), whereas `upcomingMaintenance.maintenanceStatus` stays `ONGOING` for the full 4-hour reservation window (`windowEndTime` — 3.5 hours too late!) and Reservation `healthStatus` flips `HEALTHY` 5.5 minutes *before* the VM finishes booting. Always gate on `lastStartTimestamp` advancing + K8s `Ready=True` with `8` GPUs.

#### Breadcrumb 4.3 (`d4-c3`) — Post-Maintenance Cluster Health Scanner (`[Day 4.1]` & `[Day 4 Lab]`)
- **Question:** What was the measured 16-GPU `all_reduce_perf` **bus bandwidth** (in GB/s) or final health check result in the Cluster Health Scanner output that qualifies node `-34t3` for production training?
- **Accepted Answers:** `368.4`, `368.45`, `PASS`, `196.51`

---

### Day 5 Lab: GPU Reliability Best Practices (SLA/SLOs, Raw vs. Managed, GKE vs. Slurm) & Capstone

#### Breadcrumb 5.1 (`d5-c1`) — HA (Managed) Mode vs. All-Capacity (Raw) Mode (`[Day 5.1]`)
- **Question:** Why do large frontier-model training teams often pair **All-Capacity (Raw) Mode** (or customer-managed hot-spare pools `N + K`) with **Emergent Maintenance** on tightly coupled clusters?
- **Correct Choice:** **Option B** — *Because it gives the customer access to 100% of their reserved physical blocks and lets their orchestrator swap in an already-warmed hot-spare node in seconds (slashing job recovery $t_{re}$ from 35.5 minutes down to ~2 minutes while the faulty node repairs in the background).*

#### Breadcrumb 5.2 (`d5-c2`) — GKE (Kubernetes) vs. Slurm Operations (`[Day 5.1]`)
- **Question:** How do the native isolation, maintenance-trigger, and job-resubmission primitives compare between **GKE** and **Slurm**?
- **Correct Choice:** **Option A** — *In GKE, you use `kubectl cordon`/`drain`, inspect `node.gke.io/upcoming-maintenance`, apply `cloud.google.com/perform-maintenance=true` (or `perform-reboot=true`), and rely on JobSet / KubeRay elastic controllers; in Slurm, you use Node Health Check (`NHC`) Prolog/Epilog scripts, `scontrol update NodeName=<node> State=DRAIN Reason=...`, GCE maintenance/R&R APIs, and `sbatch --requeue`.*

#### Breadcrumb 5.3 (`d5-c3`) — End-to-End 5-Day Capstone Challenge (`[Day 5 Lab]`)
- **Question:** Four alerts fire simultaneously across Pool A (`XID 31`), Pool B (`1× XID 48`), Pool C (`3× XID 63` with 2h left in training), and Pool D (`XID 79` + `8× XID 154`). Select the exact 4-part remediation dispatch that maximizes cluster Goodput.
- **Correct Choice:** **Option B**:
  - **Pool A (`XID 31`):** Fix user kernel bug & resubmit immediately ($t_{re}=0$, no node reboot/repair).
  - **Pool B (`1× XID 48`):** Reset GPU (`nvidia-smi --gpu-reset`) or reboot VM (`cloud.google.com/perform-reboot=true`).
  - **Pool C (`3× XID 63`):** Let the healthy training run finish its remaining 2 hours (or reach its next checkpoint) before triggering `perform-maintenance=true`.
  - **Pool D (`XID 79`):** Cordon the node, pull host repair forward immediately (`perform-maintenance=true`), and resume once `lastStartTimestamp` advances, K8s is `Ready=True` (8 GPUs), and Cluster Health Scanner passes.
