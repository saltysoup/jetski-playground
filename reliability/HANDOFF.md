# Operation Goodput — GPU Cluster Reliability Blog & Interactive CE Lab Handoff

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
   - *"Operation Goodput: How to Slash MTTD and MTTR on Large-Scale GPU Training Clusters"*
   - Covers the Goodput equation, why `kubectl get nodes` lies during GPU faults, how to use detective tools (`serialconsole.googleapis.com/serial_port_1_output`, Log-Based Alerts, GCE Reservation Health, Emergent Maintenance `upcomingMaintenance`), and the exact remediation playbook for every class of NVIDIA XID error (including the 35-minute in-place repair timeline vs. the 4-hour `windowEndTime` trap).

3. **Gamified 5-Day CE Breadcrumb Lab ([`lab-webapp/`](./lab-webapp/) & [`CE_BREADCRUMB_LAB_GUIDE.md`](./CE_BREADCRUMB_LAB_GUIDE.md)):**
   - Aligned 1:1 with the **GPU Reliability and Observability Training - Agenda 09/2026** (**5 Lab Sections: `Day 1 Lab` through `Day 5 Lab`, 16 Breadcrumb Clues, 16,000 Max Points**).
   - A self-paced, individual forensic web application where CEs investigate pre-staged failures on a read-only GKE cluster, enter answers into a fuzzy-matched text box (or multiple-choice selector) without seeing other people's answers, unlock deep-dive educational explanations, and compete on a **Real-Time Live Scoreboard**.

---

## 2. Repository Map

```text
reliability/
├── HANDOFF.md                        # <-- You are here (Master Handoff for Jetski collaborators)
├── BLOG_MTTR_TRAINING_CLUSTERS.md    # Technical blog post on reducing MTTD & MTTR
├── CE_BREADCRUMB_LAB_GUIDE.md        # Complete CE lab architecture, RBAC setup & answer key
├── Overview.md                       # Literature synthesis (Meta Llama 3, ByteRobust, SuperBench)
├── PLAN.md                           # Initial experimental plan
├── README.md                         # Cluster inventory & NeMo-RL Gemma 3 27B baseline
├── lab-webapp/                       # Gamified CE Breadcrumb Lab Web App + Live Scoreboard Server
│   ├── HANDOFF.md                    # Web-app specific architecture & extension guide
│   ├── index.html                    # Single-page frontend (Missions 1-3, 10-min timer, scoreboard UI)
│   ├── server.py                     # Zero-dependency Python HTTP + Live Scoreboard REST API server
│   ├── prestage-scenarios.sh         # One-shot script to pre-stage all 3 scenarios before workshop
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

> **Note:** Every breadcrumb clue in `index.html` also includes a built-in **`📟 View Captured Cluster Telemetry`** drawer containing the exact real outputs from our cluster. Even without live cluster access or if 20 CEs hit Cloud Logging `429 RESOURCE_EXHAUSTED` quota limits, the lab works 100% standalone.

---

## 4. Gamification, Timer & Instructor Controls

### Password-Protected Actions (Password: `ilovejensen`)
All instructor controls use the password **`ilovejensen`**:
1. **`🔒 Instructor Preview: OFF / ON` (Top Bar):**
   - Prompts for `ilovejensen`. When enabled, unlocks all 5 daily lab sections in the left sidebar, unlocks all 16 breadcrumb clues, displays an amber **`🔑 Instructor Answer Key`** banner on every question, and auto-expands all CLI hints, captured telemetry drawers, and educational explanations.
2. **`🔓 Reveal Answer` (On Every Active Question Card):**
   - Prompts for `ilovejensen`. Helps a stuck CE get past a question without turning on full Instructor Preview: fills in the answer, reveals the educational explanation, awards **`0 pts` (`Answer Revealed`)**, and immediately unlocks the next breadcrumb.
3. **`🔒 Reset All Scores` (Inside `🏆 Live Scoreboard` Modal):**
   - Prompts for `ilovejensen`. Wipes `scoreboard.json` clean between workshop cohorts.

### Scoring & Per-Question 10-Minute Countdown Timer
1. **Nickname Onboarding:**
   - On first load, CEs are prompted to choose a nickname (`#nickname-modal`).
   - **Important:** The 10-minute timer for Question 1 does **not** start until the CE submits their nickname.
2. **10:00 Countdown per Question (`1,000 pts` → `0 pts`):**
   - Each of the 16 breadcrumb questions has a `600s` (10-minute) countdown timer (`QUESTION_DURATION_SEC = 600`, `MAX_POINTS_PER_QUESTION = 1000`, `16,000` max total points).
   - Available points scale linearly with remaining time:
     $$\text{Points} = \left\lfloor 1000 \times \frac{t_{\text{remaining}}}{600} \right\rfloor$$
   - Once the timer reaches `00:00`, the player receives **`0 pts`** for that question (they can still submit the correct answer to unlock the next clue).
3. **Real-Time Live Scoreboard:**
   - Displayed both in the **Left Sidebar** (`● Live CE Scoreboard`) and in an expandable **Full Leaderboard Modal (`🏆 Live Scoreboard`)** with per-day score columns (`Day 1` – `Day 5`).
   - Synced across all connected browsers every 2 seconds via `GET /api/scoreboard` and updated immediately on `POST /api/register`, `POST /api/score`, and `POST /api/reset-player`.

---

## 5. Summary of the 5 Daily Lab Sections (16 Breadcrumbs)

| Lab Section | Training Agenda Modules | Breadcrumbs & Core Learning Objectives |
| :--- | :--- | :--- |
| **Day 1 Lab**<br>*Primer, Job Metrics & Brownouts* | `[Day 1.1]`, `[Day 1.2]`, `[Day 1 Lab]` | **1.1** Audit in-band GKE DCGM (`DCGM_FI_DEV_*`, missing Field `319` `DCGM_FI_DEV_XID_ERRORS`) vs. out-of-band GCE telemetry across `16` B200 GPUs.<br>**1.2** Diagnose Run 6's silent PCIe Gen5 → Gen1 brownout (`116.4s/step` → `167.2s/step`, `30.4%` Goodput loss, `0` XIDs).<br>**1.3** Optimize software resiliency & async/hierarchical checkpointing to minimize $t_{cp}$ and $t_{rw}$. |
| **Day 2 Lab**<br>*Job Crashes, RCA & R&R API* | `[Day 2.1]`, `[Day 2.2]`, `[Day 2.3]` | **2.1** Separate application crashes (`torch.OutOfMemoryError: CUDA out of memory`) from infrastructure faults.<br>**2.2** Trace unplanned VM terminations via Cloud Audit Logs (`cloudaudit.googleapis.com/system_event` → `compute.instances.hostError`) + Veritas/CLAM.<br>**2.3** Isolate a faulty node (`kubectl cordon`/`drain`) and call the Report & Replace (R&R) API (`gcloud compute instances report-faulty-host`). |
| **Day 3 Lab**<br>*Hot GPUs, XIDs & Emergent Maintenance* | `[Day 3.1]`, `[Day 3.2]`, `[Day 3 Lab]` | **3.1** Query `serialconsole.googleapis.com/serial_port_1_output` (`"NVRM: Xid"`) to catch `XID 48` (Double-Bit ECC) on `-6df3`.<br>**3.2** Apply the official remediation for isolated `XID 48` (`nvidia-smi --gpu-reset` or `cloud.google.com/perform-reboot=true`) & understand `HotGPU` thermal throttling.<br>**3.3** Trace `XID 79` (`GPU has fallen off the bus`) + `XID 154` into a 7-day `UNSCHEDULED` / `PENDING` Emergent Maintenance window (`FAILURE_GPU_XID`, `canReschedule: true`).<br>**3.4** Pull the repair forward immediately via `kubectl label node <NODE> cloud.google.com/perform-maintenance=true`. |
| **Day 4 Lab**<br>*Planned Maintenance & Health Scanner* | `[Day 4.1]`, `[Day 4 Lab]` | **4.1** Audit 30–60 day upcoming maintenance & choose group/synchronized maintenance at checkpoints for training vs. rolling maintenance for inference.<br>**4.2** Verify ~35m in-place host repair via `lastStartTimestamp` + K8s `Ready=True` (`8` GPUs) without waiting 4 hours for `windowEndTime` (`ONGOING`).<br>**4.3** Qualify the repaired node with **Cluster Health Scanner (CHS)** / multi-node NCCL `all_reduce_perf` (`368.4 GB/s` `PASS`). |
| **Day 5 Lab**<br>*Best Practices & Capstone* | `[Day 5.1]`, `[Day 5 Lab]` | **5.1** Compare HA (Managed) Mode vs. All-Capacity (Raw) Mode + `N + K` hot-spares across `a4-highgpu-8g` & `a4x` NVL72.<br>**5.2** Compare GKE (Kubernetes) vs. Slurm fault isolation & recovery primitives.<br>**5.3** Master 4-Incident Capstone Dispatch across `XID 31`, `1× XID 48`, `3× XID 63`, and `XID 79`. |

---

## 6. How to Modify or Extend the Lab

### Adding or Editing Questions
Open [`lab-webapp/index.html`](./lab-webapp/index.html) and locate `const MISSIONS = [...]`. Each mission contains a `clues` array with objects following this schema:

```javascript
{
  id: "m4-c1",                           // Unique ID (prefix with m1-, m2-, m3-, etc.)
  stepLabel: "Breadcrumb 4.1 · ...",     // Eyebrow tag on card
  title: "Question Title",
  prompt: `HTML prompt shown to the CE...`,
  inputType: "text",                     // "text" or "choice"
  placeholder: "e.g., ...",              // For "text" inputs
  acceptedKeywords: ["keyword1", "kw2"], // Case-insensitive substring match unlocks clue
  warmKeywords: ["almost1", "almost2"],  // Triggers amber "Almost there!" nudge
  warmMessage: "Amber nudge message...",
  // Or for inputType: "choice":
  // choices: [{ id: "a", text: "..." }, { id: "b", text: "..." }],
  // correctChoice: "b",
  hint: `HTML / CLI command hint...`,
  telemetrySample: `Raw terminal/log output for the 429 fallback drawer...`,
  explanationTitle: "Correct! Summary headline...",
  explanationHtml: `<p>Deep-dive educational explanation...</p>`
}
```

### Ideas for Next Collaborators
- **Cloud Run Deployment:** Add a `Dockerfile` in `lab-webapp/` so the app and live scoreboard can be deployed to Cloud Run (`--max-instances=1` for in-memory/file state, or backed by Firestore) for global CE workshops.
- **Mission 4 (Bonus — Silent Straggler / Goodput Degradation):** Add an optional 4th mission based on Run 6 (`fault-injection-runs/run6/RESULTS.md`) where PCIe Gen5 → Gen1 throttling causes a `1.44×` step-time slowdown (`116.4s` → `167.2s/step`) with zero XIDs.
- **CSV Export:** Add an instructor button in the scoreboard modal to download the final cohort leaderboard and per-clue solve times as a CSV.
