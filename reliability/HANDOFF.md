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

3. **Gamified Read-Only CE Breadcrumb Lab ([`lab-webapp/`](./lab-webapp/) & [`CE_BREADCRUMB_LAB_GUIDE.md`](./CE_BREADCRUMB_LAB_GUIDE.md)):**
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
   - Prompts for `ilovejensen`. When enabled, unlocks all 3 missions in the left sidebar, unlocks all 10 breadcrumb clues, displays an amber **`🔑 Instructor Answer Key`** banner on every question, and auto-expands all CLI hints, captured telemetry drawers, and educational explanations.
2. **`🔓 Reveal Answer` (On Every Active Question Card):**
   - Prompts for `ilovejensen`. Helps a stuck CE get past a question without turning on full Instructor Preview: fills in the answer, reveals the educational explanation, awards **`0 pts` (`Answer Revealed`)**, and immediately unlocks the next breadcrumb.
3. **`🔒 Reset All Scores` (Inside `🏆 Live Scoreboard` Modal):**
   - Prompts for `ilovejensen`. Wipes `scoreboard.json` clean between workshop cohorts.

### Scoring & Per-Question 10-Minute Countdown Timer
1. **Nickname Onboarding:**
   - On first load, CEs are prompted to choose a nickname (`#nickname-modal`).
   - **Important:** The 10-minute timer for Question 1 does **not** start until the CE submits their nickname.
2. **10:00 Countdown per Question (`1,000 pts` → `0 pts`):**
   - Each of the 10 breadcrumb questions has a `600s` (10-minute) countdown timer (`QUESTION_DURATION_SEC = 600`, `MAX_POINTS_PER_QUESTION = 1000`).
   - Available points scale linearly with remaining time:
     $$\text{Points} = \left\lfloor 1000 \times \frac{t_{\text{remaining}}}{600} \right\rfloor$$
   - Once the timer reaches `00:00`, the player receives **`0 pts`** for that question (they can still submit the correct answer to unlock the next clue).
3. **Real-Time Live Scoreboard:**
   - Displayed both in the **Left Sidebar** (`● Live CE Scoreboard`) and in an expandable **Full Leaderboard Modal (`🏆 Live Scoreboard`)**.
   - Synced across all connected browsers every 2 seconds via `GET /api/scoreboard` and updated immediately on `POST /api/register`, `POST /api/score`, and `POST /api/reset-player`.

---

## 5. Summary of the 3 Missions (10 Breadcrumbs)

| Mission | Scenario | Breadcrumbs & Core Learning Objectives |
| :--- | :--- | :--- |
| **Mission 1**<br>*The Midnight Pager* | `dapo-gemma3-run1` died at Step 1; ML engineer blames GPU hardware. | **1.1** Verify `kubectl get nodes` (`16` healthy B200 GPUs) & find `dapo-gemma3-run1-driver` in `Error`.<br>**1.2** Inspect pod logs to find job-level `torch.OutOfMemoryError: CUDA out of memory` (not hardware!).<br>**1.3** Choose optimal fix: reduce batch/sequence config & resubmit immediately ($t_{re} = 0$, no node reboot/repair). |
| **Mission 2**<br>*The Second Crash* | `dapo-gemma3-run2` died at Step 18 with `NCCL error: unhandled cuda error`. | **2.1** Query Cloud Logging using `log_id("serialconsole.googleapis.com/serial_port_1_output")` and `"NVRM: Xid"`.<br>**2.2** Identify `XID 48` (uncorrectable Double-Bit ECC error) on node `-6df3`.<br>**2.3** Consult [GCP XID Handling Docs](https://docs.cloud.google.com/compute/docs/troubleshooting/troubleshooting-gpus#xid-handling): do *not* report host faulty; stop workloads & reset GPU (`nvidia-smi --gpu-reset`) or reboot node (`kubectl label nodes <NODE> cloud.google.com/perform-reboot=true`). |
| **Mission 3**<br>*The Catastrophic Fault* | Alert fires on `dapo-gemma3-run3` (`Socket closed`). | **3.1** Find `XID 79` (`GPU has fallen off the bus`) + cascading `XID 154` across all 8 GPUs on node `-34t3`.<br>**3.2** Confirm platform detection in GCE Reservation (`DEGRADED`) & Instance `upcomingMaintenance` (`FAILURE_GPU_XID`, `PENDING`, `canReschedule: true`).<br>**3.3** Request immediate emergency maintenance via `kubectl label node <NODE> cloud.google.com/perform-maintenance=true`.<br>**3.4** Verify in-place host repair (~35m): same Node Name/Instance ID, advanced `lastStartTimestamp`, and `Ready=True` with `8` GPUs (avoiding the 4-hour `windowEndTime` `ONGOING` trap). |

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
