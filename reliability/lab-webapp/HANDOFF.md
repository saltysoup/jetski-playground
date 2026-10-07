# Operation Goodput — CE Breadcrumb Lab Web App Handoff (`lab-webapp/`)

> **Master Handoff:** See [`../HANDOFF.md`](../HANDOFF.md) for the full project overview covering the MTTR Blog Post ([`../BLOG_MTTR_TRAINING_CLUSTERS.md`](../BLOG_MTTR_TRAINING_CLUSTERS.md)), CE Lab Guide ([`../CE_BREADCRUMB_LAB_GUIDE.md`](../CE_BREADCRUMB_LAB_GUIDE.md)), and Fault Injection Runs ([`../fault-injection-runs/HANDOFF.md`](../fault-injection-runs/HANDOFF.md)).

---

## 1. Quick Start

```bash
# Run the web app + live scoreboard backend server on port 8787:
PORT=8787 python3 server.py
```

Open `http://localhost:8787` (or `http://<your-cloudtop-hostname>.c.googlers.com:8787`).

---

## 2. Files in `lab-webapp/`

| File | Purpose |
| :--- | :--- |
| [`index.html`](./index.html) | Self-contained single-page application (HTML/CSS/JS) with 3 Missions (10 Breadcrumb Clues), Nickname Onboarding Modal, Per-Question 10:00 Countdown Timer (`1,000 pts` → `0 pts`), Password-Protected `🔓 Reveal Answer` & `🔒 Instructor Preview` (`ilovejensen`), and Real-Time Live Scoreboard UI. |
| [`server.py`](./server.py) | Zero-dependency Python `ThreadingHTTPServer` serving static files and the REST API for the real-time scoreboard (`GET /api/scoreboard`, `POST /api/register`, `POST /api/score`, `POST /api/reset-player`, `POST /api/reset-scoreboard`), persisted atomically to `scoreboard.json`. |
| [`prestage-scenarios.sh`](./prestage-scenarios.sh) | One-shot script to pre-stage all 3 mission scenarios on the GKE cluster (`ikwak-reliability`) prior to the workshop so the instructor does not need to inject live faults during the session. |
| [`.gitignore`](./.gitignore) | Excludes runtime `scoreboard.json` state from git commits. |

---

## 3. Instructor Password & Controls

- **Password:** `ilovejensen`
- **Where it is used:**
  1. **`🔒 Instructor Preview: OFF/ON`** (top bar): Unlocks all 3 missions, all 10 clues, answer keys, CLI hints, captured telemetry, and explanations.
  2. **`🔓 Reveal Answer`** (on each active clue card): Reveals the answer and explanation for that single question, awards `0 pts`, and unlocks the next clue so a stuck CE can continue.
  3. **`🔒 Reset All Scores`** (inside `🏆 Live Scoreboard` modal): Clears all players from `scoreboard.json`.
