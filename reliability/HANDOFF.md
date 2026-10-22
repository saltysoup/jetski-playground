# Handoff Guide: Operation Goodput — 3-Day Live Cloud Console GPU Reliability Detective Lab

This directory (`reliability/`) contains the complete **GPU Cluster Reliability & Goodput** training suite, empirical B200 fault-injection benchmarks, technical blog post, and the gamified **3-Day (10-Breadcrumb) Live Cloud Console CE Detective Lab Web App**.

---

## 1. Repository Map (`reliability/`)

| Path | Purpose |
| :--- | :--- |
| [`lab-webapp/index.html`](./lab-webapp/index.html) | Single-page interactive **3-Day (10-Breadcrumb) Detective Lab Web App** (`Day 1 Lab`, `Day 2 Lab`, `Day 3 Lab`) with 10-minute per-clue countdown timers (`10,000` max points), live scoreboard, password-protected `Instructor Preview` & `Reveal Answer` (`ilovejensen`), and **timestamp-pinned (`timestamp>="..." AND timestamp<="..."`) Cloud Logging queries** + Veritas links so **no pre-staging is required**. |
| [`lab-webapp/server.py`](./lab-webapp/server.py) | Zero-dependency Python HTTP + JSON API server (`ThreadingHTTPServer` on port `8787`) backing the real-time multi-player CE scoreboard (`scoreboard.json`). |
| [`lab-webapp/prestage-scenarios.sh`](./lab-webapp/prestage-scenarios.sh) | Archive script that originally generated the Day 1, Day 2, and Day 3 pods and serial console `XID 48` logs on `2026-10-19` and `2026-10-20`. **Not needed before running a lab session** since all queries in `index.html` reference those exact UTC timestamps directly. |
| [`CE_BREADCRUMB_LAB_GUIDE.md`](./CE_BREADCRUMB_LAB_GUIDE.md) | Complete Instructor Guide & Answer Key for all 3 Days (10 Breadcrumbs), including exact timestamp-scoped Cloud Logging (Logs Explorer) queries, Veritas link (`MACHINE:yugrqvu-tl3`), and accepted fuzzy keywords. |
| [`BLOG_MTTR_TRAINING_CLUSTERS.md`](./BLOG_MTTR_TRAINING_CLUSTERS.md) | Technical deep-dive blog on maximizing GPU cluster Goodput by reducing MTTD and MTTR on `a4-highgpu-8g` (NVIDIA B200) clusters. |
| [`fault-injection-runs/`](./fault-injection-runs/) | Raw empirical logs, scripts, and `HANDOFF.md` from Runs 1–11 on the 2-node `a4-highgpu-8g` B200 GKE cluster (`ikwak-reliability`). |

---

## 2. Live Cluster & Observability Details

- **GCP Project:** `gpu-launchpad-playground`
- **GKE Cluster:** `ikwak-reliability` (Region: `europe-west4`, Zone: `europe-west4-b`)
- **Compute Reservation:** `nvidia-b200-6bsoymep8ylww` (`2 × a4-highgpu-8g` = 16 × NVIDIA B200 GPUs)
- **Nodes & Instance IDs:**
  - **Node 1 (`-34t3`):** `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3` · `instance_id="174490015860863227"` · Physical Host in Veritas: `MACHINE:yugrqvu-tl3`
  - **Node 2 (`-6df3`):** `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3` · `instance_id="921428950843228374"`
- **Veritas Machine View (`yugrqvu-tl3`):** `https://data.corp.google.com/sites/vm_stability_gpu_cluster_datasite/gce_veritas_machine_view/?p=MACHINE:yugrqvu-tl3&p=DATE_FROM:2026-08-05&f=package_name_odbr7g:in:cloud-cluster-node-bundle-version,cloud-cluster-node-bundle-version-disruptive,cloud-gpu-drivervm,diorite-hma,firmware-hgx-endurance-msvfud,kernel_selector,platforms-bundle-version,platforms-bundle-version-disruptive,usps_promethium&av=bzgtll:85omjnu3&av=u81fwb:le8y8rev`

---

## 3. Quick Start: Running the Lab Server (Zero Pre-Staging Required)

All Day 1 (`2026-10-20T00:53:54Z`–`00:53:56Z`), Day 2 (`2026-10-19T17:14Z` & `2026-10-20T00:54Z`), and Day 3 (`2026-09-24T20:31:38Z`–`21:49:55Z` + `2026-10-20T00:54:22Z`) logs are permanently indexed in Cloud Logging (`gpu-launchpad-playground`), and every query in the web app includes explicit `timestamp>="..." AND timestamp<="..."` bounds. Simply run the web server:

```bash
python3 /usr/local/google/home/ikwak/jetski-playground/reliability/lab-webapp/server.py
```
