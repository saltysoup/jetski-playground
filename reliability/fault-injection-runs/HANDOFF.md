# GKE B200 training-reliability demo — handoff

Last updated 2026-10-06. Read this first when resuming. Per-run detail lives in
`runN/RESULTS.md` / `runN/TIMELINE.md`.

Lives in `saltysoup/jetski-playground` at `reliability/fault-injection-runs/` (pushed from the
workstation copy at `/home/user/reliability-demo`). The parent `reliability/` folder holds the
training recipe (`gemma3-27b-it/`), the runbook (`Overview.md`) and the editorial plan
(`PLAN.md`). `plan.MD` here is the workstation blog draft, which has diverged from
`../PLAN.md` — see TODO.

## 1. What this project is

We measure, end to end, what happens on Google Cloud when a GPU in a GKE training cluster
fails. The pipeline: fault → Cloud Logging → reservation health → `upcomingMaintenance`
reason → operator applies the repair label → host repair → node back → job running again.
The output is data for a blog post and for meetings with GCE maintenance engineering.

| | |
| --- | --- |
| Cluster | `ikwak-reliability`, project `gpu-launchpad-playground`, zone `europe-west4-b` |
| GPU nodes | 2 × a4-highgpu-8g (16 × B200), reservation `nvidia-b200-6bsoymep8ylww`, one block |
| Target node | `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3`, instance ID `174490015860863227` (was `8713135250882265303` before run 8a's node recreation), GPU 7 = `0000:cc:00.0` |
| Control node | `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3` |
| Workload | NeMo-RL DAPO, Gemma 3 27B-it, 2 nodes × 8 GPUs, FSDP2, on Ray (KubeRay). Step ≈ 1 m 56 s–2 m 00 s |
| Cost | $90.22 / node-hour, **$180.44 / h for both GPU nodes, idle or not** |

### Stage ladder used in every timeline

| Stage | Meaning |
| --- | --- |
| B0 / T0 | Fault injected |
| B1 (M1) | Xid visible in Cloud Logging (serial console) |
| B2 | Training job fails |
| B4 (M3) | Reservation + block `DEGRADED` |
| B5 (M4) | `upcomingMaintenance` on instance with `maintenanceReasons` |
| B7 | `cloud.google.com/perform-maintenance=true` applied to the node (**irreversible**) |
| B8 | Maintenance `PENDING → ONGOING`, `canReschedule → false` |
| B9 / B10 | Taint / node drained `NotReady` |
| B11 / B12 | VM `REPAIRING` / back to `RUNNING` |
| B13 | Node `Ready` with 8 allocatable GPUs (B13b: Ray 2/2 workers) |
| B14 | `prep-pods.sh` + resubmit, training stepping again |

## 2. Runs so far

| Run | Date | Stimulus | Outcome | Key number |
| --- | --- | --- | --- | --- |
| 1 | 08-18 | synthetic Xid 79 | reason after 4 h 17 m; node replaced | baseline |
| 2 | 08-18 | training baseline | 30-step job, checkpoint failures | step ≈ 1 m 56 s |
| 3 | 08-19 | synthetic Xid 79 | M1 176 ms, M3 ≤ 17.5 s, **reason 3 h 46 m** | |
| 4 | 08-19 | label pulled repair forward | replacement timeline + alerting | |
| 5 | 08-20 | checkpoint/resume validation | measured, then cut from the demo | |
| 6 | 08-20 | synthetic Xid 79, full ladder | `run6/RESULTS.md`, `RUNBOOK.md` | |
| 7 | 08-24 | synthetic Xid 79 | DEGRADED 35.6 s, reason 19 m 03 s, label→Ready 4 h 29 m 55 s, type flipped to SCHEDULED; B14 approval gate idled cluster ~10 h 45 m | fault→stepping 16 h 04 m |
| 8a/8b | 08-27 | Xid 48 × 5 | **no trigger** (8a's reboot broke local SSD → GKE recreated node, new instance ID) | Xid 48 is not a trigger |
| 8d | 08-31 | Xid 63 × 5, 85 s apart | DEGRADED on #3, reason 9 m 41 s (12 m 32 s from T0); label→Ready **4 h 36 m 03 s**; type flipped to SCHEDULED | |
| 8e | 09-01 | Xid 63 + `nvidia-smi --gpu-reset` | reset refused rc=255, aborted | |
| 8f | 09-03 | Xid 63 + forced reset × 5 | DEGRADED on #2, reason 11 m 03 s; label→Ready 4 h 45 m 01 s | reset does not suppress escalation |
| 9 | 09-24 | Xid 63 × 2, no reset | **no trigger** in 15 min | 8f was reset-assisted; "3 consecutive Xid 63" rule holds |
| 10 | 09-24 | synthetic Xid 79 (run-7 format incl. `CLAUDE-SIM` marker) | DEGRADED 12.5 s, **reason ~4 h** (B5 between 10:28 and 10:55Z); label→Ready **34 m 44 s** | `run10/TIMELINE.md` |
| **11** | 09-24 | **genuine Xid 79 via PCIe SBR** on GPU 7's upstream port | reason **26 m 12 s**, label→Ready **35 m 25 s**, **fault→training stepping 1 h 18 m 18 s** | `run11/TIMELINE.md` |

### Run 11 headline (the latest end-to-end measurement)

Target was < 60 min end to end. Measured **1 h 18 m 18 s**.

| Phase | Duration | Share |
| --- | --- | --- |
| Fault → maintenance reason | 26 m 12 s | 33 % |
| Reason → label (automated by `orchestrate.sh`) | 10 s | 0 % |
| Label → drain complete | 14 m 23 s | 18 % |
| Drain → node Ready (REPAIRING ≈ 16 m 20 s) | 21 m 01 s | 27 % |
| Node Ready → Ray whole | 4 m 18 s | 5 % |
| prep + submit + job setup → Step 1 | 12 m 14 s | 16 % |

Node back to the platform at +1 h 01 m 47 s. With the advertised 10–15 min reason latency
the total would have been ~67–72 min; the remaining gap is our own ~12 min job startup.

## 3. Findings

### Settled

1. **Repair is now fast, and consistent.** Label → node Ready: 34 m 44 s (run 10), 35 m 25 s
   (run 11), vs 4 h 29 m–4 h 45 m in August (runs 7, 8d, 8f). Time in REPAIRING ~15–16 min
   vs ~4 h 08 m. Holds for a synthetic log line and a genuine whole-node fault alike.
2. **Cloud Logging is near-instant**: 0.06–2 s from fault to queryable entry.
3. **No filtering of synthetic Xids.** Run 10's line, fake `[0.000000]` timestamp and
   `CLAUDE-SIM-RUN10` marker included, went DEGRADED in 12.5 s. The run 8 nulls were
   Xid-class policy, not a filter.
4. **Trigger policy by Xid class:** Xid 79 → one is enough. Xid 63 → three consecutive
   (run 9: two without a reset do nothing; 8f's two-trigger was reset-assisted).
   Xid 48 → not a trigger (10 injections).
5. **The maintenance record closes at `windowEndTime` (4 h), not at repair completion.**
   Run 10 cleared 5.2 s after windowEnd, run 11 6.1 s after — ~3.5 h after the node was
   already usable. In 8d, where the repair overran, it cleared 27 m 46 s *after* windowEnd.
   So it closes at roughly max(windowEnd, repair done). **Never gate automation on it** —
   doing so would idle the node ~3.5 h (~$315).
6. **Reservation HEALTHY leads VM RUNNING by a fixed ~5.5 min** (5 m 20 s, 5 m 29 s,
   5 m 53 s). Readiness gates must poll the instance and the node, not the reservation.
7. **The type no longer flips to SCHEDULED.** In runs 7/8d/8f the label converted
   `UNSCHEDULED → SCHEDULED`; in runs 10/11 it stayed `UNSCHEDULED`. Either way, after B8
   `canReschedule=false` and there is no abort.
8. **A genuine Xid 79 faults the whole node.** One GPU dropping off the bus (SBR) produced
   Xid 154 "Node Reboot Required" on all 8 GPUs plus Xid 145/45 NVLink errors on GPUs 1/5/6.
9. **Kubernetes is blind to it.** The node stayed `Ready` with 8 allocatable GPUs for ~40 min
   after the fault until the repair drain. No `GPUUnhealthy` condition or
   `health-check-status` label exists on this cluster (the ClusterMAX article's GKE layer is
   not present here).
10. **Operator latency dominates unless automated.** Run 7 lost ~10 h 45 m (~$1,940) to an
    approval gate; run 10 lost ~4 h to the label and ~1.5 h to resubmit. `run11/orchestrate.sh`
    removed both (label 10 s after reason, submit 52 s after Ray whole).

### Still open — the main problem

**Maintenance-reason latency is bimodal and unexplained.** Same stimulus class, same node:

| Run | Fault → reason |
| --- | --- |
| 1 | 4 h 17 m 48 s |
| 3 | 3 h 46 m 10 s |
| 7 | 19 m 03 s |
| 8d | 12 m 32 s |
| 8f | 11 m 03 s (after degrade) |
| 10 | ~3 h 57 m – 4 h 24 m |
| 11 | 26 m 12 s |

This is the user's standing question #1 ("why did the reason take ~5 h?") and it is now the
single biggest term in end-to-end recovery. ~4 h looks suspiciously like the 4 h maintenance
window length — worth raising.

## 4. Questions for GCE maintenance engineering

1. Why is fault → `maintenanceReasons` sometimes ~11–26 min and sometimes ~4 h for the same
   Xid 79? Is there a batch/sweep interval or a queue? (table above)
2. Why does the maintenance record stay `ONGOING` until `windowEndTime` when the repair
   finished 3.5 h earlier? What signal should automation use for "repair done"?
3. Why did the label stop converting `UNSCHEDULED → SCHEDULED` between 09-03 and 09-24?
   (standing question #2)
4. Is the GKE node health-check layer (`GPUUnhealthy`, `cloud.google.com/health-check-status`)
   available for this nodepool, and how is it enabled?
5. What does PSIS stand for; is emergent maintenance immediate or reschedulable?
6. What is "consecutive" scoped to for Xid 63 (count / window / rate)?
7. Confirm Xid 48 is intentionally not a trigger.

## 5. Current state (as of 2026-10-06)

- **Both GPU nodes are up, Ready, and idle — burning ~$180/h.** The RayCluster no longer
  exists (deleted sometime after 09-25 02:37Z); `dapo-run11-recovered` was at Step 149/500
  when last observed. System nodepool still hosts unrelated llm-d / inference-perf pods.
- Node `-34t3`: maintenance CLEAR, reservation and block HEALTHY, 8 GPUs.
- All monitors from run 11 have stopped (they died when the session ended ~02:37Z 09-25).
- The B7 approvals given for runs 10 and 11 are **consumed**. Any further label needs fresh
  explicit approval from the user.

## 6. TODO

Housekeeping (no cluster needed):
- [ ] Write `run10/RESULTS.md` and `run11/RESULTS.md` (narrative; timelines already exist).
- [ ] Update `run8/RESULTS.md` with the recovered 8f repair figure (label → Ready 4 h 45 m 01 s).
- [ ] Correct `plan.MD` §9 step-time baseline (~2 m 05 s → 1 m 56 s) and add runs 7–11
      (§9 currently stops at "still open going into run 6").
- [ ] Reconcile `plan.MD` (workstation draft: §9 measured results, checkpoint-out-of-scope
      note) with `../PLAN.md` (repo: reservation-level emergent-maintenance callout, GROUPED
      scheduling note, "verify recovery" wording). Neither is a superset.
- [ ] Add runs 3–11 results to `../Overview.md` §11 (it only covers the 18 Aug run).
- [ ] Send the §4 questions to the GCE engineer.
- [ ] Rotate the Hugging Face token: it was in `train-run1-20260818.log` in plain text
      (redacted before commit, but it sat on disk since 08-18).

Decisions for the user:
- [ ] **Cluster teardown or scale GPU nodepool to 0** while not testing ($180.44/h).
- [ ] Whether to run more repetitions to characterise the reason-latency distribution.

## 7. Suggested next steps

1. Take the §4 questions — especially #1 and #2 — to maintenance engineering; run 11's
   timeline is the exhibit.
2. If more data is wanted: repeat run 11 (genuine SBR + `orchestrate.sh`) 2–3 times to
   size the reason-latency distribution. Each costs one repair cycle; needs B7 approval.
3. Cut our own ~12 min job startup (model load / Ray setup) — the only part of the tail we
   control. Pre-warmed image or cached weights would bring the run-11 total to ~66 min.
4. Consider a log-based alert on Xid 79/154 to drain and cordon at +seconds, instead of
   waiting for Kubernetes (which never noticed) or the maintenance reason.

## 8. How to run a test (run 11 recipe)

All scripts use ADC (`gcloud auth application-default print-access-token`); the gcloud user
credential is dead. Secrets stay in the Kubernetes Secret at `/etc/nemo-secrets/`, sourced
with tracing off. Launch every long-running monitor with `setsid nohup … < /dev/null &` or
it dies with the session.

1. Baseline: node Ready, 8 GPUs, maintenance null, reservation HEALTHY; training stepping.
   Capture `pre-gpu-health` (ECC, remap, NVLink, `BRIDGE_CONTROL` = `0002` on `0000:ca:01.0`).
2. Start monitors: `watch-supervisor.sh` (5 s poller → `timeline-N.log`) and
   `instance-probe.sh` (60 s → `instance-N.jsonl`).
3. Start `orchestrate.sh` (auto label on reason — **only with B7 approval** — then waits for
   VM restart, node `True|8`, Ray 2/2, runs `prep-pods.sh` + submit, waits for Step 2).
4. Fault: `run11/sbr-gpu7.sh --go` (genuine Xid 79) or `run10/inject79.sh 1` (synthetic).
5. Find the first Xid with an ascending paginated Cloud Logging query on
   `log_id("serialconsole.googleapis.com/serial_port_1_output") resource.labels.instance_id="174490015860863227"`
   (`logscan79.py` orders desc, page 100 — misses the start of big bursts).
6. Build `TIMELINE.md` from `timeline-N.log` CHANGE lines, `orchestrate.log`, k8s
   `lastTransitionTime`, instance `lastStartTimestamp`, and `train.log`.

`prep-pods.sh` is **not optional** before any resubmit: the repaired node's pod comes back
without the recipe config and the rank-0 tokenizer patch.

## 9. Layout

| Path | What |
| --- | --- |
| `HANDOFF.md` | this file |
| `plan.MD` | blog post draft from the workstation (diverged from `../PLAN.md`) |
| `INJECTION-TEST-PLAN.md` | original measurement plan |
| `prep-pods.sh`, `submit_gemma3-27b-it.sh` | B14 recovery |
| `watch-*.sh`, `tail-train.sh` | early monitors |
| `run3/` … `run11/` | per-run scripts, raw logs, `TEST-PLAN.md`, `RESULTS.md`, `TIMELINE.md` |
| `run10/TEST-PLAN.md` | plan for runs 10–11 incl. SBR method rationale and topology |
