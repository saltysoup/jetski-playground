# GOAL

What do we want to show

Reliability features to showcase superior goodput for training workloads on Google Cloud.
End to end demo showing top common errors and how to prepare your system to quickly recover
and minimize job disruption.

- Issue 1: Hard failure (GPU fell off the bus, ECC, Network) — training STOPS
- Issue 2: Soft failure (slow down due to high GPU temp, straggler) — training SLOWS

Framing: hardware failure at scale is routine physics, not vendor-specific. We prove that with
operational data from five independent organizations, then show what Google Cloud gives you to
detect and recover faster.

---

# Source Papers

Primary purpose of the papers: **identify the top common failures** empirically, from multiple
companies, so the taxonomy is industry-evidenced rather than asserted.

| Paper | arXiv | Org | Scale / period | What it contributes |
| --- | --- | --- | --- | --- |
| Robust LLM Training Infrastructure (ByteRobust) | 2509.16293 | ByteDance | 9,600 GPUs; 778,135 jobs / 3 months | Largest incident table (55,235 incidents); explicit vs implicit failure split; ETTR 97% |
| From Detection to Recovery | 2605.09370 | (504-GPU study) | 504 GPUs / 73 days | MTTD/MTTR spine; multi-signal detection; auto-retry vs manual data |
| The Llama 3 Herd of Models | 2407.21783 | Meta | 16K H100 / 54 days | Component-level root-cause table for one dedicated pretraining run |
| Revisiting Reliability in Large-Scale ML Research Clusters | 2410.21680 | Meta | 150M A100 GPU-hours, ~4M jobs / 11 months | ETTR metric + fitted MTBF model projecting failure rate by GPU scale |
| Characterization of LLM Development in the Datacenter (Acme) | 2403.07648 | Shanghai AI Lab | 4,704 A100 (Seren 2,288 + Kalos 2,416) / 6 months | Failure cost weighted by GPU-hours lost, not incident count |

Note on the two Meta papers: they do NOT conflict, they measure different systems.
2407.21783 = one dedicated H100 pretraining run. 2410.21680 = multi-tenant A100 research
clusters. Label them distinctly in the text so readers don't conflate the numbers.

## Cross-paper failure synthesis (draft table for the post)

| Failure class | ByteRobust (incidents) | Acme (GPU-hours lost) | Llama 3 (interruptions) | Hard/Soft |
| --- | --- | --- | --- | --- |
| CUDA error | 36.1% | 15.77% | — | Hard |
| GPU hardware fault | — | node failures 14.30% | Faulty GPU 30.1% | Hard |
| GPU memory / ECC | — | ECC 11.00% | HBM3 17.2% + SRAM 4.5% | Hard |
| Interconnect (NVLink/IB/net) | Infiniband 2.9% | **NVLink 30.25%** + net 4.53% | Switch/cable 8.4% | Hard |
| Job hang (silent) | 9.9% | — | — | Hard, high MTTD |
| Perf degradation / straggler | MFU decline 0.8% | — | — | **Soft** |
| Storage / filesystem | disk 5.0% + FS 2.1% + HDFS 2.0% | — | — | Both |
| Software / framework | — | framework 13% | Software bug 12.9% | Hard |
| CPU overload / OOM | 11.0% + 10.1% | OOM 3.28% | — | Hard |

**Key narrative insight — occurrence != cost.** The ranking changes depending on how you count.
ByteRobust ranks CUDA errors first *by incident count*; Acme ranks NVLink first *by GPU-hours
lost* (30.25%). Rare-but-expensive beats common-but-cheap. This is exactly why the post measures
in goodput/time-lost rather than ticket counts — it motivates section 3.

**What "good" looks like (benchmark for our demo):** Llama 3 reported >90% effective training
time; ByteRobust reported 97% ETTR on a 3-month 9,600-GPU job. Those are the bars.

---

# Proposed Flow

## 1. Hook / Context Setting

Open with the concrete number, not a literature review:
Llama 3 405B — 466 interruptions in 54 days on 16K H100s, 419 unexpected, ~78% traced to
hardware. Only three required manual intervention (automation did the rest — foreshadows the
whole post).

Then widen to the industry-wide point using the cross-paper table above: five organizations,
five fleets, same failure classes. This is physics at scale, not a vendor problem.

Divide into soft failures (training slows) and hard failures (training stops).

## 2. Cost of Downtime

Quantify with 512 x B200 (64 x a4-highgpu-8g nodes).

- Use **DWS calendar mode list price for a4-highgpu-8g**, from
  https://cloud.google.com/products/dws/pricing#calendar-mode-gpu-vm-pricing
- **State explicitly**: this is list price under DWS calendar mode, captured on <DATE>, used to
  give an *illustrative* cost of downtime. Actual cost varies by consumption model (on-demand /
  DWS Flex-start / CUD), region, and negotiated rate.
- Frame GPU rental as the defensible **floor** on downtime cost. Schedule slip and opportunity
  cost sit on top and are not modelled here.

Arithmetic to show (DWS calendar list price, captured 17 Aug 2026):
```
  a4-highgpu-8g node-hour                 $90.22      ($11.2775 per GPU-hour)
  x 64 nodes  = 512 x B200 cluster-hour   $5,774.08
  / 60        = per cluster-minute        $96.23
  / 3600      = per cluster-second        $1.6039
```

Illustrative downtime cost at 512 GPUs:

| Scenario | Duration | Cost |
| --- | --- | --- |
| Undetected straggler | 45 min | $4,330.56 |
| Hard failure, manual recovery | 2 h | $11,548.16 |
| Hard failure, automated recovery | 10 min | $962.35 |

**The number that should anchor the whole post:** a 30-day run on this cluster costs
$4,157,337.60. **Each single percentage point of goodput is worth $41,573.** Moving from Llama 3's
>90% effective training time to ByteRobust's 97% ETTR is worth **$291,013** on one 30-day run.

That reframes the post: this is not about avoiding outages, it is about buying back percentage
points of goodput — which is exactly what section 3 gives readers a formula for.

Demo-scale reference: 2 x a4-highgpu-8g = $180.44/hour; one 100-step run (~3h27m) ~= $622.52.

## 3. The Goodput Model  <-- NEW, this is the spine

Use Google Cloud's own definition rather than inventing one.

**ML Productivity Goodput** (Google Cloud) has three components:
1. **Scheduling Goodput** — fraction of time all required resources are available
2. **Runtime Goodput** — fraction of available-resource time spent making forward progress
3. **Program Goodput** — fraction of peak hardware performance extracted (MFU)

Explicit formula given by Google Cloud for Runtime Goodput:
```
  Runtime Goodput = (Checkpointing Interval - t_ch) / (Checkpointing Interval + t_re + t_rm)

    t_ch = time since last checkpoint when failure occurs   (lost work)
    t_re = time to reschedule the slice                     (~MTTR infra)
    t_rm = time to resume training                          (~MTTR job)
```
Source: cloud.google.com/blog/products/ai-machine-learning/goodput-metric-as-measure-of-ml-productivity
(verify the composition rule of the three components before publishing)

**Badput taxonomy** — the AI-Hypercomputer/ml-goodput-measurement library defines the categories
we should instrument against. Most relevant to this post:
- WASTED_PROGRESS_FROM_DISRUPTION (maps to t_ch)
- INFRASTRUCTURE_RECOVERY_FROM_DISRUPTION (maps to t_re)
- UNPRODUCTIVE_CHECKPOINT_SAVE_TIME / RESTORE_TIME
- CUSTOM_BADPUT_EVENTS (evals, SDC checks)
Note: category names are TPU-flavoured but the concepts map directly to GPU.

Cross-reference: Meta 2410.21680 and ByteRobust both use **ETTR (Effective Training Time
Ratio)** — the industry has converged on the same metric under two names. Worth one sentence.

**Every primitive in section 5 must be mapped to the term it reduces.** That converts a feature
list into an engineering argument, and lets readers compute whether their checkpoint interval is
costing more than it saves.

## 4. Failure Taxonomy and How We Simulate Them

Present the cross-paper table, then narrow to the **top 5 we will actually demo**. Selection
rule: it must appear high in the cross-paper evidence AND be simulable on a live cluster.
If we cannot simulate it, it does not earn a demo slot.

### Top 5 failures to demo

| # | Failure | Cross-paper evidence | Hard/Soft |
| --- | --- | --- | --- |
| 1 | GPU fell off the bus / node fault | Llama 3 faulty GPU 30.1%; Acme node failures 14.30% | Hard |
| 2 | GPU memory / ECC (HBM, SRAM) | Llama 3 HBM3 17.2% + SRAM 4.5%; Acme ECC 11.00% | Hard |
| 3 | Interconnect: NVLink / RDMA / network | **Acme NVLink 30.25%** (top by GPU-hours) + net 4.53%; Llama 3 switch/cable 8.4% | Hard |
| 4 | Job hang / silent stall | ByteRobust 9.9% — highest MTTD class | Hard |
| 5 | Straggler / thermal throttle | ByteRobust MFU decline 0.8% — this is Issue 2 | **Soft** |

Note on CUDA errors: ByteRobust ranks them #1 by incident count (36.1%), but a CUDA error is
usually a **symptom** of #1-#3 rather than a root cause. Say so explicitly — it explains why the
most-reported error string is not the most useful thing to alert on.

### Three tiers of simulation fidelity

Be explicit in the post about which tier each demo uses. This is the honest framing and it is
also genuinely useful — each tier tests a different part of the stack.

**Tier 1 — Synthetic (log injection).** Tests the log ingestion -> detection -> labelling path.
The GPU stays healthy.
```bash
# arbitrary XID, so we can test against any XID from the papers
echo "NVRM: Xid (PCI:0000:cc:00): 79, GPU has fallen off the bus" | sudo tee /dev/kmsg
```
Map the XID to the class being claimed: **79** fell off bus, **48/94/95** ECC, **74** NVLink.

**Tier 2 — Telemetry injection.** Tests DCGM health checks, exporters, dashboards and anything
keyed off metrics rather than logs.
```bash
dcgmi test --inject --gpuid 0 -f 319 -v 4     # 319 = DCGM_FI_DEV_XID_ERRORS
```
Verify field IDs on the cluster with `dcgmi dmon -l` before publishing.

**Tier 3 — Real fault.** The training job genuinely breaks. Highest fidelity, and the only tier
where measured MTTR reflects a true recovery.

| Failure | Real-fault method | Reversible? |
| --- | --- | --- |
| 1. Fell off bus | `echo 1 \| sudo tee /sys/bus/pci/devices/<addr>/remove` | Needs PCI rescan / node reboot — **destructive** |
| 2. ECC | Tier 2 only (cannot force real DBE) | n/a |
| 3. Interconnect | `ip link set eth3 down` on a gIB RDMA NIC (eth2-eth9) | Yes, `ip link set eth3 up` |
| 3b. Degraded net | `tc qdisc add dev eth3 root netem delay 50ms loss 1%` | Yes, `tc qdisc del` |
| 4. Job hang | `kill -STOP <rank pid>` — collective stalls, NCCL timeout fires | Yes, `kill -CONT` |
| 5. Straggler | `nvidia-smi -i 0 -pl <low watts>` or `-lgc 500,500` | Yes, `-rgc` / reset power cap |

Sequencing suggestion: run Tier 1 first (fast, safe, proves detection), then Tier 3 for the same
failure class to show real recovery. The delta between them is itself interesting — it shows how
much of your MTTD is detection logic vs how much is the failure taking time to manifest.

### Simulating an arbitrary XID

We need a way to inject *any* XID so we can test against the top XIDs identified in the papers.
Working command (the original `sudo echo ... > /dev/kmsg` fails — the redirect runs as the
user, not root):
```bash
echo "NVRM: Xid (PCI:0000:cc:00): 79, GPU has fallen off the bus" | sudo tee /dev/kmsg
```
On GKE COS nodes this needs a privileged DaemonSet or `kubectl debug node/...` with host access
to /dev/kmsg. Include that manifest in the appendix.

Also show `dcgmi test --inject` — sets real DCGM field values, so it exercises DCGM-based health
checks and exporters rather than just the log parser. Pairing "fake log line" vs "fake telemetry"
covers both detection paths.

### IMPORTANT CAVEAT TO STATE IN THE POST

We are **simulating** failures, not breaking hardware. Injecting an XID into `/dev/kmsg`
exercises the *detection and response pipeline* — log ingestion, health checks, node labelling,
job restart — while the GPU itself stays healthy. The purpose is to let you **plan and test your
recovery path before a real failure forces you to**. Recovery timings measured here are
representative of the software response, not of physical hardware replacement.

## 5. Google Cloud Primitives

Replace the flat list with a decision table. Every row maps to a goodput term from section 3.

| Failure class | Signal | Primitive | Proactive / Reactive | Reduces | Your action |
| --- | --- | --- | --- | --- | --- |
| Impending host fault | maintenance notice | Emergent maintenance (**reservation-level**) | Proactive | t_ch | Drain + checkpoint early |
| Pre-job latent fault | health scan | Cluster Health Scanner | Proactive | MTBF exposure | Gate job admission |
| GPU fell off bus | XID 79 in kmsg | NPD + GKE CTM label | Reactive | MTTD | Cordon, report faulty host |
| Confirmed bad host | — | Report faulty hosts API | Reactive | t_re | Replace node |
| Any | logs/metrics | Cloud Logging + DCGM exporter | Reactive | MTTD | Alert + dashboard |
| Disruption | — | Auto Checkpointing | Proactive | t_ch | Save on signal |

Docs to cite:
- https://docs.cloud.google.com/ai-hypercomputer/docs/manage/manage-gke-clusters#report-faulty-hosts-how-to
- https://docs.cloud.google.com/compute/docs/instances/host-maintenance-overview
- https://docs.cloud.google.com/ai-hypercomputer/docs/manage/host-events-reservations#emergency-notifications

**Point worth making explicitly in the post**, because it is easy to get wrong: emergent
maintenance is enabled on the **reservation**, not on the cluster or the node pool. There is no
GKE-side flag for it.

```bash
gcloud compute reservations update RESERVATION_NAME \
  --enable-emergent-maintenance --zone=ZONE
```

It extends advance notice for unplanned maintenance — triggered by a host error or a faulty-host
report — from a few hours to at least 7 days. That is the difference between an abrupt
termination that costs you `t_ch` of unsaved work and a planned drain that costs you nothing.

Also worth a callout: a reservation with `schedulingType: GROUPED` is maintained **all at once**,
not rolling. For a 2-node job that means no surviving replica and no failover — checkpoint
interval, not replica count, is what determines lost work. Readers should check their own
reservation's `schedulingType` before assuming a rolling recovery story applies to them.

### Sidebar: what about TPUs?

This post is scoped to GPU (A4 / B200) training clusters. The same goodput levers apply on TPUs,
but the primitives are different enough that they deserve their own treatment. Readers on TPUs
should start here:

- Cluster reliability for trillion parameter models on TPUs
  https://cloud.google.com/blog/products/compute/cluster-reliability-for-trillion-parameter-models-on-tpus
- We terminated a TPU mid-training and it recovered in seconds: introduction to Elastic Training
  with MaxText
  https://developers.googleblog.com/we-terminated-a-tpu-mid-training-and-it-recovered-in-seconds-introduction-to-elastic-training-with-maxtext/

TPU-specific primitives covered by those posts (out of scope here): TPU health predictor,
Elastic Training on Pathways, MaxText elastic recovery.

Note: the MaxText post's "terminated mid-training, recovered in seconds" is exactly the
before/after shape we want for the GPU demos. Worth a one-line nod as "this is the bar."

## 6. Testbed and Baseline  <-- NEW

State the scale gap honestly up front: we demo on 2 nodes / 16 GPUs; the cost model in section 2
is 512 GPUs. We measure MTTD/MTTR at demo scale and extrapolate cost, showing the arithmetic.

- Cluster: `ikwak-reliability`, europe-west4, GKE. 2 x a4-highgpu-8g (16 x B200) + system pool.
  Running jobset-system, kueue-system, lws-system.
- Workload: NeMo-RL DAPO/GRPO on Gemma 3 27B IT
  https://github.com/saltysoup/jetski-playground/tree/main/reliability
  ~2m04s per step; 100-step run ~3h27m — long enough to inject failures mid-run.
- **Measure baseline first**: steady-state goodput and the per-step time distribution, before
  injecting anything. No baseline, no claim.

## 7. Demo: Per-Failure Recovery

For each simulated failure, run it **twice** — naive cluster vs prepared cluster — and report the
delta. This is what makes section 2's cost math pay off.

Loop for each failure:
1. Simulate the failure on the cluster
2. Observe: training job, Cloud Logging, dashboards
3. Identify the failure class quickly -> record **MTTD**
4. Recover using the mapped primitive -> record **MTTR**
5. **Verify recovery actually worked** — loss curve continuity across the restart, step time back
   to baseline, no duplicated or skipped data. A job that "recovered" onto a bad checkpoint is a
   failure mode readers have hit and rarely see written up.
6. Recompute goodput with the section 3 formula; show naive vs prepared side by side

Straggler (Issue 2) needs its own detection method — it is not a smaller hard failure:
- per-rank step-time outlier detection
- NCCL collective timing
- `DCGM_FI_DEV_THERMAL_VIOLATION`, clock throttle reasons via `nvidia-smi -q -d PERFORMANCE`
Supporting evidence: MegaScale (2402.15627) and C4 (2406.04594) both cover straggler detection
if we want a citation here.

## 8. Day-0 Readiness Checklist  <-- NEW, the screenshot-and-share artifact

- Checkpoint interval math — derived from the Runtime Goodput formula, not guessed
- Async / in-memory checkpointing enabled
- NCCL timeouts and heartbeat configured
- JobSet / Kueue restart policy set
- DCGM exporter + dashboard deployed
- Node auto-repair on
- Cluster Health Scanner in the job admission path
- Alerting on XID classes, not just job exit codes
- Baseline goodput recorded so regressions are visible

## 9. Appendix

- XID injection recipes + privileged DaemonSet manifest
- `dcgmi test --inject` examples
- Cluster spec and reproduction steps
- Teardown instructions + approximate cost to reproduce the demo
