# Overview: Reliability & Goodput Demo

Reproduction guide for the reliability / goodput blog post. `PLAN.md` in this directory holds
the editorial plan (narrative, sources, structure). This file is the **runbook** — what to
stand up, what to inject, what to measure.

---

## 1. What this demo shows

Hardware failure during large-scale GPU training is routine, not exceptional. This demo:

1. Establishes a **baseline goodput** on a live NeMo-RL training job
2. **Simulates** the top 5 empirically-common failure classes
3. Recovers from each using Google Cloud primitives
4. Measures **MTTD / MTTR** and recomputes goodput, naive vs prepared

### Important: these are simulated failures

We inject faults to exercise the **detection and response pipeline** — log ingestion, health
checks, node labelling, job restart. In Tier 1 and Tier 2 the GPU itself stays healthy. The
purpose is to let you **plan and test your recovery path before a real failure forces you to**.
Recovery timings here reflect the software response, not physical hardware replacement.

---

## 2. Prerequisites

| Requirement | Value used in this demo |
| --- | --- |
| GKE cluster | `ikwak-reliability`, region `europe-west4` |
| Project | `gpu-launchpad-playground` |
| GPU node pool | 2 x `a4-highgpu-8g` (16 x NVIDIA B200), gIB RDMA |
| System node pool | `e2-standard-16` |
| Control plane | GKE 1.35.x |
| Installed operators | JobSet, Kueue, LeaderWorkerSet (`jobset-system`, `kueue-system`, `lws-system`) |
| Workload | NeMo-RL DAPO/GRPO, Gemma 3 27B IT (see `gemma3-27b-it/`) |

Tooling: `gcloud`, `kubectl`, `helm`, `gh`, and cluster admin on the target cluster.

### Cost

DWS calendar list price, captured **17 Aug 2026**:

| Item | Cost |
| --- | --- |
| `a4-highgpu-8g` node-hour | $90.22 ($11.2775 per GPU-hour) |
| Demo cluster (2 nodes) per hour | $180.44 |
| One 100-step training run (~3h27m) | ~$622.52 |
| Reference: 512 x B200 cluster-hour | $5,774.08 |
| Reference: 30-day 512-GPU run | $4,157,337.60 |
| Reference: 1 percentage point of goodput on that run | $41,573 |

Prices are list and illustrative. Actual cost varies by consumption model (on-demand /
DWS Flex-start / calendar / CUD), region, and negotiated rate.

---

## 3. Cluster access

```bash
gcloud container clusters get-credentials ikwak-reliability \
  --region europe-west4 --project gpu-launchpad-playground

kubectl get nodes -o wide
```

Confirm GPU nodes are `Ready` and carry `cloud.google.com/gke-accelerator=nvidia-b200`.

---

## 4. Deploy the training job

Follow `gemma3-27b-it/` and the root `README.md` in this directory. Summary:

```bash
cd gemma3-27b-it
./submit_gemma3-27b-it.sh
```

The job runs ~2m04s per step; a 100-step run with AIME evals takes ~3h27m — long enough to
inject failures mid-run and observe recovery.

---

## 5. Measure the baseline FIRST

No baseline, no claim. Before injecting anything, record:

- Steady-state **step time** and its distribution (p50 / p95 / max) over >= 20 steps
- Per-rank step time, to establish normal variance between GPUs
- GPU utilisation, SM clocks, power draw, temperature (DCGM)
- Checkpoint save duration and interval

These feed directly into the goodput formula in section 7.

---

## 6. Failure injection

Three tiers of fidelity. State which tier each result came from.

### Tier 1 — Synthetic (log injection)

Tests log ingestion -> detection -> node labelling. GPU stays healthy. Works for **any** XID,
so we can test against every XID class named in the source papers.

```bash
# NOTE: `sudo echo ... > /dev/kmsg` does NOT work - the redirect runs as the user, not root.
echo "NVRM: Xid (PCI:0000:cc:00): 79, GPU has fallen off the bus" | sudo tee /dev/kmsg
```

Map the XID to the class being claimed:

| XID | Failure class |
| --- | --- |
| 79 | GPU has fallen off the bus |
| 48, 94, 95 | ECC / uncorrectable memory error |
| 74 | NVLink error |

On GKE COS nodes this requires host access — use a privileged DaemonSet or:

```bash
kubectl debug node/<node> -it --image=busybox --profile=sysadmin
```

### Tier 2 — Telemetry injection

Tests DCGM health checks, exporters and dashboards — anything keyed off metrics rather than logs.
This is the **only** viable path for ECC: a real double-bit error cannot be forced.

```bash
dcgmi test --inject --gpuid 0 -f 319 -v 4     # 319 = DCGM_FI_DEV_XID_ERRORS
```

Verify field IDs on your cluster with `dcgmi dmon -l` before relying on them.

### Tier 3 — Real fault

The training job genuinely breaks. Only tier where measured MTTR reflects true recovery.

| Failure | Method | Reversible |
| --- | --- | --- |
| Job hang | `kill -STOP <rank pid>` — collective stalls, NCCL timeout fires | Yes: `kill -CONT` |
| Interconnect down | `ip link set eth3 down` (gIB RDMA NICs are eth2-eth9) | Yes: `ip link set eth3 up` |
| Interconnect degraded | `tc qdisc add dev eth3 root netem delay 50ms loss 1%` | Yes: `tc qdisc del dev eth3 root` |
| Straggler / throttle | `nvidia-smi -i 0 -pl <low>` or `nvidia-smi -i 0 -lgc 500,500` | Yes: `-rgc`, reset power cap |
| Fell off bus | `echo 1 \| sudo tee /sys/bus/pci/devices/<addr>/remove` | **Destructive** - needs PCI rescan or node reboot |

Recommended sequence: Tier 1 first (fast, safe, proves detection), then Tier 3 for the same class
to measure real recovery. The delta between them separates *detection latency* from *time for the
failure to manifest*.

---

## 7. The five failures

Selection rule: high in the cross-paper evidence **and** simulable on a live cluster.

| # | Failure | Evidence | Type | Best tier |
| --- | --- | --- | --- | --- |
| 1 | GPU fell off bus / node fault | Llama 3 30.1%; Acme 14.30% | Hard | 1, then 3 |
| 2 | GPU memory / ECC | Llama 3 17.2% HBM3 + 4.5% SRAM; Acme 11.00% | Hard | 2 only |
| 3 | Interconnect (NVLink / RDMA) | **Acme NVLink 30.25%**; Llama 3 8.4% | Hard | 3 |
| 4 | Job hang / silent stall | ByteRobust 9.9% (highest MTTD) | Hard | 3 |
| 5 | Straggler / thermal throttle | ByteRobust MFU decline 0.8% | **Soft** | 3 |

CUDA errors rank #1 by incident count in ByteRobust (36.1%) but are usually a **symptom** of
1-3 rather than a root cause.

---

## 8. Measurement loop

Run each failure **twice** — naive cluster vs prepared cluster — and report the delta.

1. Inject the failure
2. Observe: training job logs, Cloud Logging, dashboards
3. Identify the failure class -> record **MTTD**
4. Recover using the mapped primitive -> record **MTTR**
5. **Verify recovery was real**: loss-curve continuity across the restart, step time back to
   baseline, no duplicated or skipped data. A job that "recovered" onto a bad checkpoint is a
   failure mode that rarely gets written up.
6. Recompute goodput

### Goodput formula (Google Cloud)

ML Productivity Goodput has three components: **Scheduling**, **Runtime** and **Program**
Goodput (the last is MFU). Runtime Goodput has an explicit form:

```
Runtime Goodput = (Checkpointing Interval - t_ch) / (Checkpointing Interval + t_re + t_rm)

  t_ch = time since last checkpoint when the failure occurs   (lost work)
  t_re = time to reschedule                                    (infra MTTR)
  t_rm = time to resume training                               (job MTTR)
```

Badput categories to instrument against, from `AI-Hypercomputer/ml-goodput-measurement`:
`WASTED_PROGRESS_FROM_DISRUPTION` (t_ch), `INFRASTRUCTURE_RECOVERY_FROM_DISRUPTION` (t_re),
`UNPRODUCTIVE_CHECKPOINT_SAVE_TIME` / `RESTORE_TIME`, `CUSTOM_BADPUT_EVENTS`.
Names are TPU-flavoured; the concepts map directly to GPU.

Benchmarks from the literature: Llama 3 reported **>90%** effective training time;
ByteRobust reported **97% ETTR** on a 3-month 9,600-GPU job.

---

## 9. Google Cloud primitives

| Failure class | Signal | Primitive | Proactive / Reactive | Reduces | Action |
| --- | --- | --- | --- | --- | --- |
| Impending host fault | maintenance notice | Emergent maintenance | Proactive | t_ch | Drain + checkpoint early |
| Pre-job latent fault | health scan | Cluster Health Scanner | Proactive | MTBF exposure | Gate job admission |
| GPU fell off bus | XID 79 in kmsg | NPD + GKE CTM label | Reactive | MTTD | Cordon, report faulty host |
| Confirmed bad host | — | Report faulty hosts API | Reactive | t_re | Replace node |
| Any | logs / metrics | Cloud Logging + DCGM exporter | Reactive | MTTD | Alert + dashboard |
| Disruption | — | Auto Checkpointing | Proactive | t_ch | Save on signal |

References:
- https://docs.cloud.google.com/ai-hypercomputer/docs/manage/manage-gke-clusters#report-faulty-hosts-how-to
- https://docs.cloud.google.com/compute/docs/instances/host-maintenance-overview

### TPUs

Out of scope here. TPU readers should start with:
- https://cloud.google.com/blog/products/compute/cluster-reliability-for-trillion-parameter-models-on-tpus
- https://developers.googleblog.com/we-terminated-a-tpu-mid-training-and-it-recovered-in-seconds-introduction-to-elastic-training-with-maxtext/

---

## 10. Day-0 readiness checklist

- [ ] Checkpoint interval derived from the goodput formula, not guessed
- [ ] Async / in-memory checkpointing enabled
- [ ] NCCL timeouts and heartbeat configured
- [ ] JobSet / Kueue restart policy set
- [ ] DCGM exporter + dashboard deployed
- [ ] Node auto-repair on
- [ ] Cluster Health Scanner in the job admission path
- [ ] Alerting on XID classes, not just job exit codes
- [ ] Baseline goodput recorded so regressions are visible

---

## 11. Teardown

```bash
helm uninstall <release> -n <namespace>
kubectl delete namespace <namespace>
```

Reverse any Tier 3 injection that is still active:

```bash
nvidia-smi -i 0 -rgc                       # reset locked clocks
nvidia-smi -i 0 -pl <default watts>        # reset power cap
ip link set eth3 up                        # bring RDMA NIC back
tc qdisc del dev eth3 root                 # remove netem
kill -CONT <pid>                           # resume stopped rank
```

Scale the GPU node pool to zero if the cluster is not needed — at $180.44/hour it is the
dominant cost.

---

## 12. Source papers

| Paper | arXiv | Org | Contribution |
| --- | --- | --- | --- |
| Robust LLM Training Infrastructure (ByteRobust) | 2509.16293 | ByteDance | 55,235 incidents / 3 months; explicit vs implicit split; 97% ETTR |
| From Detection to Recovery | 2605.09370 | 504-GPU study | MTTD/MTTR methodology; multi-signal detection |
| The Llama 3 Herd of Models | 2407.21783 | Meta | Component-level root-cause table, 16K H100 / 54 days |
| Revisiting Reliability in Large-Scale ML Research Clusters | 2410.21680 | Meta | ETTR + MTBF model by GPU scale |
| Characterization of LLM Development in the Datacenter (Acme) | 2403.07648 | Shanghai AI Lab | Failure cost weighted by GPU-hours lost |

The two Meta papers measure **different systems** — 2407.21783 is one dedicated H100 pretraining
run; 2410.21680 is multi-tenant A100 research clusters. Do not conflate their numbers.
