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
| **KubeRay operator** | **Required — not installed by default. See below.** |
| Reservation | `nvidia-b200-6bsoymep8ylww` (zone `europe-west4-b`), `SPECIFIC_RESERVATION`, count 2 |
| Workload | NeMo-RL DAPO/GRPO, Gemma 3 27B IT (see `gemma3-27b-it/`) |

Tooling: `gcloud`, `kubectl`, `helm`, `gh`, and cluster admin on the target cluster.

### KubeRay operator

`values.yaml` renders a `RayCluster` custom resource, so the KubeRay operator and its CRDs
must exist first. A stock GKE cluster does not have them:

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update kuberay
helm install kuberay-operator kuberay/kuberay-operator \
  --namespace kuberay-system --create-namespace --version 1.4.2

kubectl get crd | grep ray.io    # expect rayclusters, rayjobs, rayservices
```

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

### 4a. Free the GPUs

The job needs all 16 GPUs. Anything already holding them must be scaled down first — back up
its spec so you can restore it:

```bash
kubectl get lws <name> -n default -o yaml > backup-lws.yaml
kubectl scale lws <name> -n default --replicas=0
```

Note for LeaderWorkerSet: scaling to 0 may leave the leader pod in `Succeeded` and the per-group
worker StatefulSet alive, because that StatefulSet is owned by the leader **pod**, not the LWS.
GPUs stay pinned until the leader pod is gone:

```bash
kubectl delete pod <lws-name>-0 -n default
```

Confirm 0 GPUs are allocated before continuing.

### 4b. Deploy the Ray cluster

**The release name must be `ray-cluster`.** `values.yaml` hardcodes the ConfigMap name
`ray-cluster-kuberay-fluentbit-config`, while the template generates
`{{ include "ray-cluster.fullname" . }}-fluentbit-config`. With `nameOverride: kuberay`, only a
release named `ray-cluster` produces a matching name. Installing as `kuberay` (as the root
`README.md` shows) yields `kuberay-fluentbit-config`, and every pod hangs in
`ContainerCreating` with:

```
MountVolume.SetUp failed for volume "fluentbit-config-volume" :
  configmap "ray-cluster-kuberay-fluentbit-config" not found
```

```bash
cd gemma3-27b-it
helm install ray-cluster . -n default -f values.yaml
```

`values.yaml` also references `imagePullSecrets: gar-secret`. That secret does not need to exist
for the default `nvcr.io/nvidia/nemo-rl:v0.6.0` image — kubelet warns and pulls anonymously. It
is only needed if you switch to the custom Artifact Registry image.

Verify:

```bash
kubectl get pods -n default -l ray.io/cluster
kubectl exec -it $(kubectl get pods -l ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}') \
  -c ray-head -- ray status
```

Expect 16 GPUs and 2 worker nodes. First start is slow — the NeMo-RL image is tens of GB.

### 4c. Submit

```bash
export HF_TOKEN=$(kubectl get secret hf-secret -n default -o jsonpath='{.data.HF_TOKEN}' | base64 -d)
bash gemma3-27b-it/submit_gemma3-27b-it.sh
```

Note the recipe heredoc inside `submit_gemma3-27b-it.sh` sets `max_num_steps: 10` and
`tensor_parallel_size: 2`, which differ from the 100-step / TP=4 figures in the root
`README.md`. The in-script values win. At ~2m04s per step, 10 steps is roughly 20–25 minutes
plus model download and the AIME evals — a convenient window for injecting failures mid-run.

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
- https://docs.cloud.google.com/ai-hypercomputer/docs/manage/host-events-reservations#emergency-notifications

### Emergent maintenance

Emergent maintenance is set on the **reservation**, not the cluster or the node pool. There is
no GKE-side flag — `gcloud container clusters/node-pools update` has nothing for it in GA, beta
or alpha, and the GKE v1beta1 API only exposes `HostMaintenancePolicy.maintenanceInterval` and
`opportunisticMaintenanceStrategy`.

```bash
gcloud compute reservations update RESERVATION_NAME \
  --enable-emergent-maintenance \
  --zone=ZONE
```

What it buys you: when Compute detects a host error or a host is reported faulty, the advance
notice for the resulting **unplanned** maintenance goes from a few hours to **at least 7 days**.
It does not avoid the maintenance — it buys enough time to drain and checkpoint deliberately
instead of losing work to an abrupt termination. This is the single highest-leverage proactive
setting for `t_ch` in the goodput formula.

Verify (the CLI may be blocked by Context Aware Access; the REST API via ADC is not):

```bash
gcloud compute reservations describe RESERVATION_NAME --zone=ZONE \
  --format='value(enableEmergentMaintenance)'

# or, if CAA blocks gcloud:
TOKEN=$(gcloud auth application-default print-access-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://compute.googleapis.com/compute/v1/projects/PROJECT/zones/ZONE/reservations/RESERVATION_NAME"
```

Fields worth reading on the reservation:

| Field | Meaning |
| --- | --- |
| `enableEmergentMaintenance` | Whether the extended notice window is on |
| `schedulingType` | `GROUPED` = whole reservation maintained together; `INDEPENDENT` = per-VM |
| `resourceStatus.reservationMaintenance.upcomingGroupMaintenance` | `type`, `maintenanceStatus`, `canReschedule`, window times |
| `resourceStatus.healthInfo` | `healthyBlockCount` / `degradedBlockCount` |
| `specificReservation.{count,inUseCount,assuredCount}` | Whether any spare capacity exists |

**`GROUPED` changes the recovery story.** Maintenance lands on every VM in the reservation at
once rather than rolling one node at a time, so a 2-node job has no surviving replica to fail
over to. Checkpoint-restore is the only recovery path, which makes checkpoint interval — not
replica count — the variable that determines lost work.

To trigger the pending event early, on purpose, as a real test:

```bash
gcloud compute reservations perform-maintenance RESERVATION_NAME \
  --scope=running --zone=ZONE      # scopes: all | running | unused
```

This performs genuine host maintenance, not a simulation. `--scope=unused` is a no-op when
`inUseCount == count`. Do not run this without spare capacity unless you accept that the nodes
go down together and come back only as the reservation returns them.

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

## 11. Measured results — run of 18 Aug 2026

Cluster `ikwak-reliability`, 2 x a4-highgpu-8g (16 x B200), NeMo-RL v0.6.0,
Gemma 3 27B IT, DAPO, 10 steps.

### Baseline

| Metric | Value |
| --- | --- |
| Step time, mean (steps 1–9) | **116.39 s** |
| Step time, p50 / min / max | 118.08 s / 102.39 s / 129.10 s |
| Step time stdev | 8.02 s (6.9% of mean) |
| Final step (includes end validation) | 213.94 s |
| Validation (256 AIME problems) | 30.7–35.4 s |
| Checkpoint save | **49.14 s** (23.0% of that step) |
| E2E throughput | 2.39 samples/s, 3,509 tokens/s |
| AIME 2024 step 0 | 24.61% (63/256) |
| AIME 2024 step 10 | 23.44% (60/256) |

10 steps is far too short to improve AIME — the step-10 dip is noise, not
over-specialisation. Do not read a trend into it.

### Recovery-cost constants

These are the numbers that actually drive the goodput formula:

| Constant | Value | Notes |
| --- | --- | --- |
| Container image pull (cold) | **450 s** | `nemo-rl:v0.6.0` is 20,106,828,316 B (20.1 GB) |
| vLLM init | 428.9 s | |
| Policy init | 93.7 s | |
| Other setup | 195.9 s | |
| **Total worker init (`t_rm`)** | **721.5 s (12m 01s)** | Paid on *every* restart |
| **`t_rm` + cold image pull** | **~1,171 s (19m 31s)** | Paid when the node is new |

`t_rm` of 721.5 s is 6.2x the 116 s step time. Worked example with a 10-step
checkpoint interval (1,164 s) and a failure landing mid-interval
(`t_ch` = 582 s), same node so `t_re` ~ 0:

```
Runtime Goodput = (1164 - 582) / (1164 + 0 + 721.5) = 582 / 1885.5 = 30.9%
```

Nearly 70% badput from a single failure. Worker init, not lost steps, is the
dominant term — which is the argument for warm standby workers and for
checkpoint intervals set against `t_rm`, not against step count.

### Detection results — both default paths failed

**Tier 1: XID 79 injected into `/dev/kmsg`** on a GPU node (tagged `CLAUDE-SIM`).
Confirmed present in `dmesg`. Over the following 90+ seconds:

| Observed | Result |
| --- | --- |
| Node condition change | none |
| Taint / cordon / `unschedulable` | none |
| Kubernetes event | none |
| Reached Cloud Logging | **no** |
| **MTTD** | **never detected** |

Nothing on a default GKE cluster consumes `/dev/kmsg` for XID errors. Beware
false confidence when grepping Cloud Logging for `Xid` — it substring-matches
the unrelated field `containerBoxID`.

**Tier 2: GKE managed DCGM exporter does not export XID or ECC at all.**
`gke-dcgm-exporter:4.4.1-4.6.0-gke.17` exposes 168 series across 21 families:

```
DCGM_FI_DEV_SM_CLOCK              DCGM_FI_PROF_GR_ENGINE_ACTIVE
DCGM_FI_DEV_MEMORY_TEMP           DCGM_FI_PROF_SM_ACTIVE
DCGM_FI_DEV_GPU_TEMP              DCGM_FI_PROF_PIPE_TENSOR_ACTIVE
DCGM_FI_DEV_POWER_USAGE           DCGM_FI_PROF_DRAM_ACTIVE
DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION  DCGM_FI_PROF_PIPE_FP64_ACTIVE
DCGM_FI_DEV_GPU_UTIL              DCGM_FI_PROF_PIPE_FP32_ACTIVE
DCGM_FI_DEV_MEM_COPY_UTIL         DCGM_FI_PROF_PIPE_FP16_ACTIVE
DCGM_FI_DEV_FB_TOTAL              DCGM_FI_PROF_PCIE_TX_BYTES
DCGM_FI_DEV_FB_FREE               DCGM_FI_PROF_PCIE_RX_BYTES
DCGM_FI_DEV_FB_USED               DCGM_FI_PROF_NVLINK_TX_BYTES
                                  DCGM_FI_PROF_NVLINK_RX_BYTES
```

Absent: `DCGM_FI_DEV_XID_ERRORS` (319), all ECC counters (SBE/DBE, volatile and
aggregate), NVLink *error* counters (only byte counters are present), PCIe replay,
throttle reasons, retired/remapped pages, and any health field.

So injecting field 319 with `dcgmi test --inject` has nothing downstream to observe
it. **The utilisation metrics are all there; the failure metrics are all missing.**

**Prescriptive takeaway — this is the headline.** On a default GKE cluster the top
two failure classes from the source papers, GPU-fell-off-bus and ECC, are
*undetectable*. Not slow to detect: invisible. Before any of this matters you must
deploy either:
- your own DCGM exporter with `DCGM_FI_DEV_XID_ERRORS` and the ECC fields enabled
  (the managed one cannot be reconfigured), or
- node-problem-detector with a custom kmsg rule matching `NVRM: Xid`.

Measure your own MTTD before trusting it. A dashboard full of `GPU_UTIL` looks
reassuring and tells you nothing about whether a GPU is dying.

### Checkpoint integrity — critical defect found

The 10-step run reported a successful checkpoint. It is not usable:

| Location | Contents |
| --- | --- |
| Head pod, `step_10/` | 28 KB: `config.yaml`, `training_info.json`, `train_dataloader.pt` |
| Worker A, `tmp_step_10/` | 152 GB, `policy/weights/model/shard-*.safetensors` |
| Worker B, `tmp_step_10/` | 152 GB, `policy/optimizer/optim/*.distcp` |

Three compounding problems:

1. **`/opt/nemo-rl/results` is the container overlay filesystem** — not a PV, not
   even an emptyDir. The only volumes on the worker are `/tmp/ray`, `/dev/shm`,
   the NVIDIA driver hostPath, and the gIB plugin. A pod restart destroys the
   checkpoint. Node loss destroys it. There is no copy anywhere else.
2. **The checkpoint is split across two pods** — weights on one, optimizer state on
   the other. Neither is complete on its own.
3. **The `tmp_step_10` -> `step_10` rename never completed on the workers.** The head
   renamed its metadata and recorded `current_step: 10`, so the run *looks* resumable
   while the weights sit under a `tmp_` path a resume will not search.

Net effect: **MTTR for any node-loss failure is infinite, not slow.** There is
nothing to recover from. This invalidates a recovery demo until checkpoints are
written to shared, durable storage — the cluster already has the GCS FUSE and Lustre
CSI drivers installed, so mounting one and pointing `checkpointing.checkpoint_dir` at
it is the fix.

This is precisely the failure mode called out in section 8 step 5. A job that reports
a clean checkpoint and cannot actually restore from it is worse than one that
obviously fails, because you only discover it during an incident.

Also observed: `Async mode is only supported for torch >= 2.9.0, disabling async
mode`, so the 49.14 s checkpoint save is fully blocking — pure badput on every save.

### Repo issues found while reproducing

| Issue | Detail |
| --- | --- |
| Wrong helm release name in root `README.md` | Must be `ray-cluster`; see section 4b |
| KubeRay operator not installed | No `ray.io` CRDs existed; chart cannot apply |
| `submit_gemma3-27b-it.sh` uses `set -x` | Echoes `HF_TOKEN` in cleartext into the job log, which fluent-bit ships to Cloud Logging. Use `set +x` around the export |
| LWS scale-to-0 leaves GPUs pinned | Leader pod stays `Succeeded` and owns the worker StatefulSet |
| Config drift | Script heredoc says 10 steps / TP=2; `README.md` says 100 steps / TP=4 |

---

## 12. Teardown

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

## 13. Source papers

| Paper | arXiv | Org | Contribution |
| --- | --- | --- | --- |
| Robust LLM Training Infrastructure (ByteRobust) | 2509.16293 | ByteDance | 55,235 incidents / 3 months; explicit vs implicit split; 97% ETTR |
| From Detection to Recovery | 2605.09370 | 504-GPU study | MTTD/MTTR methodology; multi-signal detection |
| The Llama 3 Herd of Models | 2407.21783 | Meta | Component-level root-cause table, 16K H100 / 54 days |
| Revisiting Reliability in Large-Scale ML Research Clusters | 2410.21680 | Meta | ETTR + MTBF model by GPU scale |
| Characterization of LLM Development in the Datacenter (Acme) | 2403.07648 | Shanghai AI Lab | Failure cost weighted by GPU-hours lost |

The two Meta papers measure **different systems** — 2407.21783 is one dedicated H100 pretraining
run; 2410.21680 is multi-tenant A100 research clusters. Do not conflate their numbers.
