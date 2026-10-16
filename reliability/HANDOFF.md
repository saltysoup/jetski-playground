# Operation Goodput — GPU Cluster Reliability Blog & 5-Day Interactive CE Lab Handoff

> **For Collaborators Using Jetski / AI Coding Agents:**
> Paste this prompt into your Jetski session to pick up immediately:
> ```text
> Read reliability/HANDOFF.md, reliability/CE_BREADCRUMB_LAB_GUIDE.md, and reliability/fault-injection-runs/HANDOFF.md. Then inspect reliability/lab-webapp/index.html and reliability/lab-webapp/server.py so we can collaborate on the Operation Goodput CE Breadcrumb Lab and MTTR blog post.
> ```

---

## 1. Project Overview & Goals

This repository (`reliability/`) contains our empirical research, technical blog post, and gamified Customer Engineer (CE) training lab on **reducing Mean Time to Detect (MTTD) and Mean Time to Recover (MTTR)** on Google Cloud GPU training clusters (`a4-highgpu-8g` / NVIDIA B200 GPUs on GKE):

1. **Empirical Fault-Injection Study (`fault-injection-runs/`):**
   - 11 controlled fault-injection runs on cluster `ikwak-reliability` (`gpu-launchpad-playground`, `europe-west4-b`, 2 × `a4-highgpu-8g` = 16 × B200 GPUs, reservation `nvidia-b200-6bsoymep8ylww`).
   - Full timeline measurements, detection surface latencies, and host-repair mechanics are documented in [`fault-injection-runs/HANDOFF.md`](./fault-injection-runs/HANDOFF.md).

2. **Technical Blog Post ([`BLOG_MTTR_TRAINING_CLUSTERS.md`](./BLOG_MTTR_TRAINING_CLUSTERS.md)):**
   - *"From 16 Hours to 78 Minutes: How to Slash MTTD and MTTR on GKE B200 Training Clusters"*
   - Covers the Goodput equation, why `kubectl get nodes` lies during GPU faults, how to use detective tools (`serialconsole.googleapis.com/serial_port_1_output`, Log-Based Alerts, GCE Reservation Health, Emergent Maintenance `upcomingMaintenance`), and the exact remediation playbook for every class of NVIDIA XID error (including the 35-minute in-place repair timeline vs. the 4-hour `windowEndTime` trap).

3. **Gamified 5-Day Read-Only CE Breadcrumb Lab ([`lab-webapp/`](./lab-webapp/) & [`CE_BREADCRUMB_LAB_GUIDE.md`](./CE_BREADCRUMB_LAB_GUIDE.md)):**
   - Aligned **1:1 with Days 1–5 and Column F (Critical User Journeys)** of the 5-Day GPU Reliability Session Plan (`gid=1288298866`).
   - A self-paced, **100% read-only / demo-based** forensic web application (**5 Missions × 4 Breadcrumbs = 20 Breadcrumbs, 20,000 max points**) where CEs investigate pre-staged failures and telemetry without creating or mutating compute resources, unlock deep-dive architectural explanations, and compete on a **Real-Time Live Scoreboard**.

---

## 2. Repository Map

```text
reliability/
├── HANDOFF.md                        # <-- You are here (Master Handoff for Jetski collaborators)
├── BLOG_MTTR_TRAINING_CLUSTERS.md    # Technical blog post on reducing MTTD & MTTR
├── CE_BREADCRUMB_LAB_GUIDE.md        # Complete 5-Day CE lab architecture, RBAC setup & 20-clue answer key
├── Overview.md                       # Literature synthesis (Meta Llama 3, ByteRobust, SuperBench) & baseline
├── PLAN.md                           # Initial experimental plan
├── README.md                         # Cluster inventory & NeMo-RL Gemma 3 27B baseline
├── lab-webapp/                       # Gamified CE Breadcrumb Lab Web App + Live Scoreboard Server
│   ├── HANDOFF.md                    # Web-app specific architecture & extension guide
│   ├── index.html                    # Single-page frontend (Missions 1-5, 20 clues, 10-min timer, scoreboard UI)
│   ├── server.py                     # Zero-dependency Python HTTP + Live Scoreboard REST API server
│   ├── prestage-scenarios.sh         # One-shot script to pre-stage all 5 missions' read-only artifacts
│   └── .gitignore                    # Ignores runtime scoreboard.json state
└── fault-injection-runs/             # Empirical data from Runs 1–11
    ├── HANDOFF.md                    # Detailed run-by-run empirical findings & gotchas
    ├── plan.MD                       # Run 1–11 test matrix
    └── run6/ .. run11/               # Per-run timelines, scripts (sbr-gpu7.sh), and results
```

---

## 3. Running the Interactive CE Lab Web App

### Start the Server Locally
The web app uses [`lab-webapp/server.py`](./lab-webapp/server.py), a zero-dependency Python 3 `ThreadingHTTPServer` that serves static files and the real-time `/api/*` scoreboard endpoints:

```bash
chmod +x reliability/lab-webapp/server.py
PORT=8787 python3 reliability/lab-webapp/server.py
```

- **Local URL:** `http://localhost:8787`
- **Cloudtop Proxy URL (for Google internal access):** `http://<your-cloudtop-hostname>.c.googlers.com:8787`

### Pre-Stage the Cluster Forensics (Optional — Before Live Workshop)
To run the workshop **100% hands-off** (without the instructor manually injecting faults during the session), run [`lab-webapp/prestage-scenarios.sh`](./lab-webapp/prestage-scenarios.sh) once before the workshop starts:

```bash
chmod +x reliability/lab-webapp/prestage-scenarios.sh
./reliability/lab-webapp/prestage-scenarios.sh
```

> **Note:** Every single breadcrumb clue in `index.html` includes a built-in **`📟 View Captured Cluster Telemetry`** drawer containing the exact real outputs from our cluster. Even without live cluster access or if 30+ CEs hit Cloud Logging `429 RESOURCE_EXHAUSTED` quota limits, the lab works 100% standalone.

---

## 4. Gamification, Timer & Instructor Controls

### Password-Protected Actions (Password: `ilovejensen`)
All instructor controls use the password **`ilovejensen`**:
1. **`🔒 Instructor Preview: OFF / ON` (Top Bar):**
   - Prompts for `ilovejensen`. When enabled, unlocks all 5 missions in the left sidebar, unlocks all 20 breadcrumb clues, displays an amber **`🔑 Instructor Answer Key`** banner on every question, and auto-expands all CLI hints, captured telemetry drawers, and educational explanations.
2. **`🔓 Reveal Answer` (On Every Active Question Card):**
   - Prompts for `ilovejensen`. Helps a stuck CE get past a question without turning on full Instructor Preview: fills in the answer, reveals the educational explanation, awards **`0 pts` (`Answer Revealed`)**, and immediately unlocks the next breadcrumb.
3. **`🔒 Reset All Scores` (Inside `🏆 Live Scoreboard` Modal):**
   - Prompts for `ilovejensen`. Wipes `scoreboard.json` clean between workshop cohorts.

### Scoring & Per-Question 10-Minute Countdown Timer
1. **Nickname Onboarding:**
   - On first load, CEs are prompted to choose a nickname (`#nickname-modal`).
   - **Important:** The 10-minute timer for Question 1.1 does **not** start until the CE submits their nickname.
2. **10:00 Countdown per Question (`1,000 pts` → `0 pts`, `20,000 pts` Max Total):**
   - Each of the 20 breadcrumb questions has a `600s` (10-minute) countdown timer (`QUESTION_DURATION_SEC = 600`, `MAX_POINTS_PER_QUESTION = 1000`).
   - Available points scale linearly with remaining time:
     $$\text{Points} = \left\lfloor 1000 \times \frac{t_{\text{remaining}}}{600} \right\rfloor$$
   - Once the timer reaches `00:00`, the player receives **`0 pts`** for that question (they can still submit the correct answer to unlock the next clue).
3. **Real-Time Live Scoreboard:**
   - Displayed both in the **Left Sidebar** (`● Live CE Scoreboard`) and in an expandable **Full Leaderboard Modal (`🏆 Live Scoreboard`)** with per-day (`Day 1 (M1)` .. `Day 5 (M5)`) score breakdowns.

---

## 5. Summary of the 5 Missions (20 Breadcrumbs — Aligned with Sheet Column F)

| Mission | Sheet Alignment (Days 1–5) | Breadcrumbs & Read-Only Learning Objectives |
| :--- | :--- | :--- |
| **Mission 1 · Day 1**<br>*Job Execution Telemetry, In-Band vs. Out-of-Band & Software Resiliency* | **Day 1.1, 1.2 & Day 1 Lab**<br>Goodput primer, brownout/blackout, checkpointing, In-Band vs. Out-of-Band / GCE vs. GKE / VM vs. Host vs. NVLink metrics | **1.1** Inspect baseline step time (`116.39s`) & warm worker init overhead (`t_rm = 721.5s`).<br>**1.2** Audit default GKE DCGM (missing `DCGM_FI_DEV_XID_ERRORS` field `319` & ECC) vs. GCE Out-of-Band / CLAM metrics.<br>**1.3** Identify the silent checkpoint defect (`overlay` FS + `tmp_step_10` rename + `49.14s` blocking save vs. async GCS FUSE/Lustre).<br>**1.4** Diagnose `dapo-gemma3-run1` (`torch.OutOfMemoryError: CUDA out of memory`, `t_re = 0`). |
| **Mission 2 · Day 2**<br>*Job Crashes, NCCL Hangs, HostError RCA & R&R API* | **Day 2.1, 2.2, 2.3 & Day 2 Lab**<br>Job crashes, NCCL hangs, `hostError` (Veritas + CLAM), bad node isolation & `report-faulty-host` (R&R API) | **2.1** Pinpoint gIB RDMA NIC `eth3` (`Rank 9` on `-6df3`) in an NCCL `ALLREDUCE` watchdog timeout.<br>**2.2** Identify unplanned physical host failure in Cloud Audit Logs (`compute.instances.hostError`) & Veritas/CLAM RCA.<br>**2.3** Break the "bad magnet" scheduling loop (`kubectl cordon` + `drain` *before* pod deletion or R&R).<br>**2.4** Invoke `report-faulty-host` (`instances.reportHostAsFaulty` with `faultReasons`) when `upcomingMaintenance` is `null`. |
| **Mission 3 · Day 3**<br>*HotGPU Thermal Throttling, XID 48/63/79 & Emergent Maintenance* | **Day 3.1, 3.2 & Day 3 Lab**<br>HotGPU node vs. rack thermal throttling, decoding XIDs, 7-day `UNSCHEDULED` Emergent Maintenance & `perform-maintenance` | **3.1** Diagnose a `1.44×` step slowdown (`167.2s/step`) from `GPU 3` thermal throttling (`89°C`, `540 MHz`) & distinguish single-GPU vs. rack cooling.<br>**3.2** Query `serialconsole.googleapis.com/serial_port_1_output` for `"NVRM: Xid"` (`XID 48` vs. 3× `XID 63` rules).<br>**3.3** Trace `XID 79` + `XID 154` to Reservation `DEGRADED` & 7-day `UNSCHEDULED` `FAILURE_GPU_XID` Emergent Maintenance.<br>**3.4** Trigger immediate repair via `cloud.google.com/perform-maintenance=true` (~35m in-place repair). |
| **Mission 4 · Day 4**<br>*Planned Maintenance (30–60d), Readiness Traps & Health Scanner* | **Day 4.1 & Day 4 Lab**<br>30–60d maintenance visibility, `GROUPED` vs. staggered upgrades, Maintenance Exclusions & Cluster Health Scanner / NCCL validation | **4.1** Audit 30–60d `upcomingGroupMaintenance` (`PLANNED_UPDATE`) and `schedulingType: GROUPED` vs. `INDEPENDENT`.<br>**4.2** Avoid the `-5.5m` Reservation `HEALTHY` trap and `+4h` `windowEndTime` `ONGOING` trap (`lastStartTimestamp` + `Ready|8 GPUs`).<br>**4.3** Validate post-repair fabric health via Cluster Health Scanner & 16-GPU `all_reduce_perf` (`365.4 GB/s` busbw `PASS`).<br>**4.4** Select Upgrade & Maintenance Exclusion strategies for Synchronous Training vs. Multi-Replica Inference. |
| **Mission 5 · Day 5**<br>*Best Practices: SLA/SLO, Raw vs. Managed, K8s vs. Slurm & Capstone* | **Day 5.1 & Day 5 Lab**<br>GPU SLA/SLOs, Raw vs. Managed mode (`A3U/A4` vs. `A4X NVL72`), GKE vs. Slurm & End-to-End Capstone | **5.1** Connect Runtime Goodput / ETTR (`>90%` SLO) to cutting outage time from `16h 04m` (Run 7) to `1h 18m 18s` / `78m` (Run 11).<br>**5.2** Compare HA (Managed) Mode vs. All Capacity (Raw) Mode across 8-GPU `A3U/A4` hosts and 72-GPU `A4X NVL72` racks.<br>**5.3** Map Slurm primitives (`sbatch`, `Prolog`/`Epilog`, `scontrol drain`) to GKE (`Kueue` + `JobSet`, `CHS` + `NPD`, `kubectl cordon`).<br>**5.4** Solve Capstone Incident `INC-500` (3× `XID 63` + `XID 64` bank exhaustion end-to-end recovery). |
