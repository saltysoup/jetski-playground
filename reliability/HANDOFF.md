TL;DR: This directory (`reliability/`) contains the complete GPU Cluster Reliability & Goodput training suite, Day 1-3 slide alignment map, B200 fault-injection benchmarks, and the 3-Day (10-Breadcrumb) Read-Only Live Cloud Console Detective Lab Web App. Run `python3 lab-webapp/server.py` to launch the lab server on port `8787`.

# Handoff Guide: Operation Goodput - 3-Day Live Cloud Console GPU Reliability Detective Lab

This directory (`reliability/`) contains the complete **GPU Cluster Reliability & Goodput** training suite, empirical B200 fault-injection benchmarks, technical blog post, and the gamified **3-Day (10-Breadcrumb) Live Cloud Console CE Detective Lab Web App** aligned 1-to-1 with the Day 1, Day 2, and Day 3 workshop presentations.

---

## 1. Repository Map (`reliability/`)

| Path | Purpose |
| :--- | :--- |
| [`lab-webapp/index.html`](./lab-webapp/index.html) | Single-page interactive **3-Day (10-Breadcrumb) Detective Lab Web App** (`Day 1 Lab`, `Day 2 Lab`, `Day 3 Lab`) with 15-minute per-clue countdown timers (`10,000` max points), live scoreboard, password-protected `Instructor Preview` & `Reveal Answer` (`ilovejensen`), and **timestamp-pinned (`timestamp>="..." AND timestamp<="..."`) Cloud Logging queries** + Veritas links so **no pre-staging is required**. |
| [`lab-webapp/server.py`](./lab-webapp/server.py) | Zero-dependency Python HTTP + JSON API server (`ThreadingHTTPServer` on port `8787`) backing the real-time multi-player CE scoreboard (`scoreboard.json`). |
| [`lab-webapp/prestage-scenarios.sh`](./lab-webapp/prestage-scenarios.sh) | Archive script that originally generated the Day 1, Day 2, and Day 3 pods and serial console `XID 48` logs on `2026-10-19`, `2026-10-20`, and `2026-10-22`. **Not needed before running a lab session** since all queries in `index.html` reference those exact UTC timestamps directly. |
| [`CE_BREADCRUMB_LAB_GUIDE.md`](./CE_BREADCRUMB_LAB_GUIDE.md) | Complete Instructor Guide & Answer Key for all 3 Days (10 Breadcrumbs), including exact timestamp-scoped Cloud Logging (Logs Explorer) queries, slide module alignment, Veritas link (`MACHINE:yugrqvu-tl3`), and accepted keywords. |
| [`BLOG_MTTR_TRAINING_CLUSTERS.md`](./BLOG_MTTR_TRAINING_CLUSTERS.md) | Technical deep-dive blog on maximizing GPU cluster Goodput by reducing MTTD and MTTR on `a4-highgpu-8g` (NVIDIA B200) clusters. |
| [`fault-injection-runs/`](./fault-injection-runs/) | Raw empirical logs, scripts, and `HANDOFF.md` from Runs 1 to 11 on the 2-node `a4-highgpu-8g` B200 GKE cluster (`ikwak-reliability`). |

---

## 2. Aligned Workshop Presentations (Day 1 to Day 3)

- **Day 1 Deck (`Reliability Primer & Job Execution Observability`):** `https://docs.google.com/presentation/d/1Zds3HF4fAX0xvIqYtR_Vk8gyHqMG8RtXlB4ziAg8dGE/edit#slide=id.slide_1792565293443516737_439`
- **Day 2 Deck (`Infrastructure Crashes, Faulty Node R&R & Failure RCA`):** `https://docs.google.com/presentation/d/1LsBIAEnhsVCTdnNoTWlpHtWJix7Ehqi-uwWg00_6g_M/edit#slide=id.slide_1792566224576935619_362`
- **Day 3 Deck (`Hardware Failures [HotGPU & XIDs] & Emergent Maintenance`):** `https://docs.google.com/presentation/d/1TUL-rBs0IcRi511u3HSQMiKJ5btZHOp0TTE1mdN3OhU/edit#slide=id.slide_1792566230641109874_378`

---

## 3. Live Cluster & Observability Details

- **GCP Project:** `gpu-launchpad-playground`
- **GKE Cluster:** `ikwak-reliability` (Region: `europe-west4`, Zone: `europe-west4-b`)
- **Compute Reservation:** `nvidia-b200-6bsoymep8ylww` (`2 x a4-highgpu-8g` = 16 x NVIDIA B200 GPUs)
- **Nodes & Instance IDs:**
  - **Node 1 (`-34t3`):** `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3` · `instance_id="174490015860863227"` · Physical Host in Veritas: `MACHINE:yugrqvu-tl3`
  - **Node 2 (`-6df3`):** `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3` · `instance_id="921428950843228374"`
- **Veritas Machine View (`yugrqvu-tl3`):** `https://data.corp.google.com/sites/vm_stability_gpu_cluster_datasite/gce_veritas_machine_view/?p=MACHINE:yugrqvu-tl3&p=DATE_FROM:2026-08-05&f=package_name_odbr7g:in:cloud-cluster-node-bundle-version,cloud-cluster-node-bundle-version-disruptive,cloud-gpu-drivervm,diorite-hma,firmware-hgx-endurance-msvfud,kernel_selector,platforms-bundle-version,platforms-bundle-version-disruptive,usps_promethium&av=bzgtll:85omjnu3&av=u81fwb:le8y8rev`

---

## 4. Quick Start: Running the Lab Server (Zero Pre-Staging Required)

All Day 1 (`2026-10-22T05:45:00Z` to `05:46:00Z`), Day 2 (`2026-10-19T17:14Z` & `2026-10-20T00:54Z`), and Day 3 (`2026-09-24T20:31:38Z` to `21:49:55Z` + `2026-10-20T00:54:22Z`) logs are permanently indexed in Cloud Logging (`gpu-launchpad-playground`), and every query in the web app includes explicit `timestamp>="..." AND timestamp<="..."` bounds. Run the web server:

```bash
python3 /usr/local/google/home/ikwak/jetski-playground/reliability/lab-webapp/server.py
```
