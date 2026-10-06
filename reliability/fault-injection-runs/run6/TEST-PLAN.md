# Run 6 — detect → identify → repair → recover, measured end to end

**Objective.** Measure the elapsed time between every stage of the loop a cluster operator
actually walks when a GPU fault appears: the platform surfaces a signal, you identify the
failure class from it, you use a Google Cloud primitive to act on it, the host is repaired, and
the cluster comes back to full capacity and baseline throughput.

**Stimulus.** Synthetic Xid 79 written to `/dev/kmsg` on `-34t3`, plus a kill of the GPU-7
compute PIDs so the training job actually dies. Nothing is physically damaged; the node stays
healthy and every step is reversible until B7.

**Output of this run** is the two tables below, filled in: observed behaviour, UTC start, UTC
end and duration for every stage.

> **STATUS: COMPLETE.** B0–B14 all executed and measured. Phase B1 ran 20 Aug, phase B2 21 Aug,
> B14 24 Aug. All five assertions resolved — see the bottom of this document. The remaining gap
> in the whole programme is Tier 3 (a real hardware fault); every number here still comes from a
> synthetic Xid plus a process kill.

---

## Explicitly out of scope

- **Checkpoint save / restore.** Deliberately removed. Showing how to checkpoint is not the
  goal of this post, and it is a large enough topic to deserve its own treatment — GKE offers
  multi-tier checkpointing for exactly this. `checkpointing.enabled: false` for this run, so the
  resubmitted job restarts from step 0 and the recovery we measure is *cluster* recovery, not
  job-state recovery.
- **Throughput-recovery graph.** Step times are recorded either side of the event so the graph
  can be built later, but the graph itself is presented separately.
- **A dedicated baseline phase.** Skipped by decision. ~~See the caveat under B14.~~ **Resolved:**
  the pre-injection job supplied a like-for-like baseline after the fact — see assertion 5.
- **Real hardware faults.** No PCI remove. The platform sees the same stimulus as runs 1, 3 and
  4, so this run cannot determine whether real faults are detected faster. That stays open.
- **Network / NVLink fault injection.** Only `eth0` and `lo` are visible inside the worker
  container despite the CR annotating eth2–eth9, so `netem` recipes need a host pod.

Prior runs are quoted below as **observations, not predictions**. Runs 1, 3 and 4 are samples
from a pipeline we do not control and have not characterised. The gates exist to record what
actually happens.

---

## Pre-flight

### P1 — workload

`run6/dapo-gemma3-27b-it-2n8g-fsdp2-automodel.yaml`, checkpointing off. NeMo-RL DAPO/GRPO on
Gemma 3 27B IT, 2 nodes / 16 GPUs, ~2 m 05 s per step measured 20 Aug.

Submit detached (`ray job submit --no-wait`). A foreground driver dies with the calling shell —
that cost run 3 its 500-step run — and this run has to survive a ~4 h repair window.

**The job must be running and past step 1 before T0.** Not a baseline phase — a precondition. If
no process is bound to GPU 7 when the injector fires, the kill is a no-op: M1 through M4 stay
valid because they are driven by the log line, but the job-death clock at B2 and the whole
badput window measure nothing. `inject.sh` prints `NO_GPU7_PIDS` if this happens.

### P2 — pod preparation

`prep-pods.sh` before **every** submission. The recipe config lives only in the container
filesystem, so any pod recreation destroys it — including the worker pod that comes back on the
repaired node at B13. This is a real trap: the resubmission at B14 is exactly when it bites.

### P3 — instrumentation, started before T0, under `nohup`

`run6/watch.py` — one process, one clock, covering what run 3's poller and run 4's poller did
separately.

- k8s surfaces every 5 s: node ready/GPUs/taints/labels/annotations/uid, Ray pod phases,
  cluster allocatable GPUs, Ray job status.
- Compute surfaces every **5 s for the first 20 min**, then 30 s: reservation and block
  `healthStatus`, per-instance `status`, `upcomingMaintenance.maintenanceReasons`,
  `canReschedule`, `windowStartTime`.
- Cloud Logging polled for the Xid entry; **M1 is read off the entry's own `timestamp` and
  `receiveTimestamp`**, not off when the poll succeeded. Logging reads in this project are
  being 429'd by another consumer, so a poll-time measurement would be an artefact.
- Pub/Sub `xid-alerts-sub` drained each tick; **M2 is read off the message `publishTime`**, same
  reasoning. Note the email's received time by hand alongside it — that is the latency an
  operator actually experiences, and it includes mail delivery on top of the Pub/Sub number.
- Separately: timestamped `ray job logs -f` to `run6/train.log`.

### P4 — alerting, end to end, verified before T0 rather than after

One policy, `14253142552209113916`, condition matching
`log_id("serialconsole.googleapis.com/serial_port_1_output") resource.type="gce_instance"
"NVRM: Xid"` — every Xid class, not just 79. Two channels rather than two policies; a duplicate
policy on the same condition would double-fire.

| Channel | Target | Role | State |
| --- | --- | --- | --- |
| pubsub `1117176896799437752` | topic `xid-alerts` | the measurable one — `publishTime` is a server clock | subscription drained, 0 stale |
| email `9977523644883873718` | `ikwak@google.com` | the one a human reacts to | **delivery confirmed 20 Aug 16:52 UTC** |

`notificationRateLimit` is 300 s, which throttles a *repeat* alert but not the first, so it does
not distort M2. `autoClose` is 1800 s.

M2 has gone unmeasured for three runs for a different reason each time: no policy in run 1 and
run 3, and in run 4 a policy that existed but was never stimulated. This time the whole path —
policy enabled, both channels attached, email actually arriving — was verified *before* the
injection instead of being discovered afterwards.

The filter was also confirmed against a real injection, not just read: run 3's line is still
retrievable through it, which is the same match the alert evaluates.

```
2026-08-19T00:45:12.176226544Z  inst=8713135250882265303
[ 9456.903376] NVRM: Xid (PCI:0000:cc:00): 79, pid=0, name=CLAUDE-SIM-RUN3, GPU has fallen off the bus.
```

That entry also explains the run 4 gap: the policy was created 19 Aug 15:38 UTC, ~15 h after the
only injection available to trigger it.

Every timestamp below comes from `run6/timeline.log`. T0 is stamped inside the injecting pod on
the node's own clock, immediately before the kmsg write: **T0 = 2026-08-20T18:19:46.611Z**.

One bookkeeping note. The poller has to be started *before* the injector, so it was started with
an intended T0 of `18:16:38Z` and the real T0 landed 188.611 s later. Rather than restart the
poller mid-event — which risks a polling gap and a contended Pub/Sub pull — it was left running
and the offset recorded in `run6/t0-offset.txt`. Every line in `timeline.log` carries an absolute
wall clock alongside its `T+` column, so the correction is exact: **true T+ = logged T+ − 188.611 s**.
All durations in the tables below are computed from the absolute timestamps against the real T0.

---

## Phase B1 — detection: T0 → the platform tells you what broke

All four latencies are measured **from T0**, not from each other — they run concurrently off the
same stimulus, so "duration" and "from T0" are the same number here.

| # | Stage | UTC start | UTC end | Duration | Observed behaviour |
| --- | --- | --- | --- | --- | --- |
| B0 | **T0** — Xid 79 written to `/dev/kmsg`, GPU-7 PIDs killed | `18:19:46.611` | `18:19:46.611` | — | Two compute PIDs resolved on PCI `0000:CC:00.0` (`1247809`, `1255204`), T0 stamped on the node's clock, `KMSG_WROTE_OK`, then both killed. All 8 GPUs had live processes, so GPU 7 was genuinely busy at T0. |
| B1 | **M1** Xid visible in Cloud Logging | `18:19:46.611` | `18:19:46.673` | **0.062 s** | Entry `timestamp` `18:19:46.672803043Z`. Full line preserved verbatim including the PCI address: `NVRM: Xid (PCI:0000:cc:00): 79, pid=0, name=CLAUDE-SIM-RUN6, GPU has fallen off the bus.` Serial-console kernel offset `[79296.432578]` also carried through. |
| B1′ | M1 ingested (`receiveTimestamp`) | `18:19:46.611` | `18:19:48.686` | **2.075 s** | The number that matters for anything downstream of Logging: 62 ms to emit, a further 2.0 s to be queryable. Run 3's 176 ms was the serial-console clock alone and understated this. |
| B2 | Training job dies | `18:19:46.611` | `18:19:55.166` | **8.56 s** | Ray control plane marks `dapo-run6c` FAILED at `18:19:55.166Z`. Surfaced as `ActorUnavailableError: ... RpcError: Socket closed rpc_code: 14`, then raylet `Worker exit type: SYSTEM_ERROR ... connection error code 2`. No NCCL timeout — the actor RPC broke first. Driver exit code 1; the log follower printed the failure at `18:19:59.178Z` (T+12.57 s). |
| B3a | **M2** alert delivered to Pub/Sub | `18:19:46.611` | `18:22:01.950` | **2 m 15.3 s** | **First ever measurement of this leg.** From the message's own `publishTime`; no attributes set. Policy `14253142552209113916` fired on the `"NVRM: Xid"` condition as designed. |
| B3b | Alert email lands in the inbox | `18:19:46.611` | `18:22` (11:22 PDT, minute resolution) | **2 m 13 s – 3 m 13 s** | Operator-confirmed receipt at `ikwak@google.com`. Lands in the **same minute** as the Pub/Sub publish (18:22:01.950Z), so the two channels fan out in parallel from the one policy and mail delivery adds ~0–60 s on top, not a separate multi-minute leg. Inbox clocks are minute-resolution, hence the range rather than a point. |
| B4 | **M3** reservation block → `DEGRADED` | `18:19:46.611` | `18:20:11.106` | **24.5 s** (bounded 19.5–24.5 s) | Block `...-block-0001` `healthStatus` HEALTHY → DEGRADED, and the **parent reservation followed in the same poll** — `res_health` also DEGRADED. `degradedHostCount`, `maintenancePendingCount` and `maintenanceOngoingCount` all stayed `null`: one bit, no reason, no count. 5 s poll cadence, so the true transition is in `(18:20:06, 18:20:11]`. |
| B5 | **M4** `maintenanceReasons` populated on the instance | `18:19:46.611` | `23:55:26.680` | **5 h 35 m 40 s** | `maintenanceStatus: PENDING`, `maintenanceReasons: ["FAILURE_GPU_XID", "FAILURE_GPU"]`, `canReschedule: true`, `windowStartTime: 2026-08-28T00:00:00Z` — a repair booked 7 days out. Node object picked it up 7.7 s later (`maint_status: PENDING` at `23:55:34.409`). 30 s cloud poll cadence at this point, so bounded to ±30 s. |

**Prior observations for comparison, not expectation:**

| | run 1 | run 3 | run 4/5 |
| --- | --- | --- | --- |
| M1 | ≤ 4.4 s (T0 stamped outside the pod) | 176 ms | — |
| M2 | never (no policy) | never (no policy) | never (policy built 15 h too late to catch anything) |
| M3 | ≤ 16 min | ≤ 17.5 s (30 s poll cadence) | — |
| M4 | 4 h 17 m 48 s | 3 h 46 m 10 s (recovered from `managedFields`) | — |
| Job death | job was undisturbed | job was undisturbed | 17 s after kill |

**B5 is the wildcard.** It has varied by half an hour between two runs with no known driver, and
it gates everything after it. Record which of T+1 m / T+15 m / T+60 m / longer it lands in — a
gate here is a measurement, not a wait.

**Result: 5 h 35 m 40 s — the longest of the three, and the spread is now wider, not narrower.**
Three samples: 3 h 46 m 10 s (run 3), 4 h 17 m 48 s (run 1), 5 h 35 m 40 s (run 6). Range 1 h 50 m
on the same stimulus, same node type, same project. No driver identified. **This is the single
least predictable number in the programme and it sits on the critical path** — you cannot label
for repair until it lands, so it is a hard floor under MTTR that no amount of alerting buys back.
For the post: quote it as "hours, unpredictably" rather than a point estimate, and note that the
booked window was 7 days out — the repair Google schedules on its own is not the repair you want.

**Assertion for this phase:** the block must be observed `HEALTHY` on the poller's first tick,
*before* T0, or B4 is a re-read of a latched state rather than a transition. This is the flaw
that made run 3's M3 an upper bound.

**Satisfied.** The poller's first cloud tick, `18:17:03.845Z` — 2 m 43 s *before* T0 — recorded
`res_health: HEALTHY`, `block_health: HEALTHY`, both instances `RUNNING`, `34t3_maint: CLEAR`,
no `maintenanceReasons`. B4 is therefore a measured HEALTHY → DEGRADED transition, not a re-read,
and run 3's caveat does not apply to this number.

---

## Phase B2 — repair: T0′ → the cluster is whole again

`T0′` is the moment the label is applied. Stages here are **sequential** — each one's start is
the previous one's end — so duration is the time spent in that stage, and the running total from
T0′ is what run 4 reported.

**T0′ = 2026-08-21T16:45:14.596Z.** Source of record: `run6/watch-b2.out`, `run6/t0-prime.txt`.
Note the poller for this phase was started with an intended T0′ of `16:44:47.450Z`; all durations
below are computed from absolute timestamps against the real T0′, not off the poller's `T+` column.

| # | Stage | UTC start | UTC end | Duration | Cumulative from T0′ | Observed behaviour |
| --- | --- | --- | --- | --- | --- | --- |
| B6 | Decide to act — `canReschedule` true, reason known | `08-20 23:55:26` | `08-21 16:45:14` | **16 h 49 m 48 s** | — | Not a platform number — the operator was asleep. Recorded so it can be excluded from the badput figure rather than quietly absorbed into it. `canReschedule: true` and the reason code were both available from B5 onward; nothing was waiting on the platform. |
| B7 | **T0′** label `perform-maintenance=true` applied | `16:45:14.596` | `16:45:18.508` | **3.9 s** | **+3.9 s** | Applied to `-34t3` only. **The label was never once observed on a node poll** — consumed inside the 5 s cadence. Run 4 saw it at +21 s; do not rely on reading it back as confirmation. |
| B8 | → `active-node-maintenance` label appears, event PENDING → ONGOING | `16:45:18.508` | `16:45:25.698` | **7.2 s** | **+11.1 s** | `active-node-maintenance: passthrough` on the node; `maintenanceStatus` PENDING → ONGOING. Run 4: +38 s. |
| B8′ | → window pulled forward, `canReschedule` → false | `16:45:25.698` | `16:45:35.526` | **9.8 s** | **+20.9 s** | `windowStartTime` rewritten `2026-08-28T00:00:00Z` → `2026-08-21T16:45:34Z`. **This is the primitive working: a repair booked 7 days out becomes a repair starting now, in 21 seconds.** `canReschedule` flips false — the commit point. |
| B9 | → `maintenance-window-started` taint applied | `16:45:35.526` | `16:45:47.453` | **11.9 s** | **+32.9 s** | Node still `Ready` with 8 allocatable GPUs; the taint only stops new scheduling. Run 4: +56 s. |
| B10 | → node `NotReady`, 8 → 0 allocatable GPUs | `16:45:47.453` | `17:26:40.032` | **40 m 52.6 s** | **+41 m 25.4 s** | **The drain window — 3× run 4's 12 m 31 s for an identical operation.** Cluster GPUs 16 → 8 at +41 m 33.6 s, `-34t3` allocatable 8 → 0 at +41 m 46.0 s, node `Unknown`/`unreachable` at +42 m 50.5 s. Nothing was running on the node to drain, so this is not workload eviction — it is platform latency, and it is not a constant. |
| B11 | → VM enters `REPAIRING` | `17:26:40.032` | `17:28:52.280` | **2 m 12.2 s** | **+43 m 37.7 s** | Run 4: +15 m 16 s. |
| B12 | → block + reservation back to `HEALTHY` | `17:28:52.280` | `21:28:18.195` | **3 h 59 m 25.9 s** | **+4 h 43 m 03.6 s** | **The host repair itself — 81% of the phase.** VM still `REPAIRING` when health flipped; `RUNNING` followed at +4 h 48 m 58.9 s. Run 4: +4 h 17 m 49 s. |
| B13 | → node `Ready`, taint cleared, 16 GPUs, Ray 2/2 | `21:28:18.195` | `21:39:57.720` | **11 m 39.5 s** | **+4 h 54 m 43.1 s** | 16 GPUs allocatable +4 h 50 m 34.7 s, node `Ready` +4 h 50 m 45.0 s, taint and maintenance labels cleared +4 h 51 m 05.8 s, worker pod 1/2 +4 h 54 m 25.2 s, **Ray 2/2 +4 h 54 m 43.1 s**. Run 4: +4 h 28 m 34 s. |
| B14 | `prep-pods.sh`, resubmit, step time back to baseline | `08-24 16:45:02` | `08-24 17:07:23` | **22 m 21 s** | — | Submitted `dapo-run6-recovered` 16:45:02.053; **step 1 begins 16:57:25.161 (+12 m 23 s startup)**; 5 steps complete 17:07:23.135. Run separately from B13 by 67 h of idle cluster — see the badput table. |

**Where the wall clock actually goes.** Of the 4 h 54 m 43 s from T0′ to a usable cluster:

| Segment | Duration | Share |
| --- | --- | --- |
| Label → taint (the primitive doing its work) | 32.9 s | 0.2% |
| Drain window (B9 → B10) | 40 m 52.6 s | 13.9% |
| Host-side repair (B10 → B12) | 4 h 01 m 38 s | 82.0% |
| Kubernetes + Ray reconvergence (B12 → B13) | 11 m 39.5 s | 4.0% |

The headline holds — **~82% of the recovery is the hypervisor and nothing you control** — but the
"~1% Kubernetes reconvergence" figure from runs 1 and 4 does **not**. It was 4.0% here, and the
drain window was 14%. Quote the shape (repair dominates, cluster tuning cannot buy it back), not
the precise 99/1 split, which does not replicate.

### The badput window

The number this run exists to produce, spanning both phases:

Three numbers, because one would be dishonest. The run took 94 hours end to end, but most of that
was nobody looking at it.

| | UTC start | UTC end | Duration | Cost @ $180.44/h |
| --- | --- | --- | --- | --- |
| **Attentive operator** — death → B5 → label → repair → forward progress, with the two human gaps removed | — | — | **10 h 42 m 38 s** | **$1,933** |
| Platform-attributable as actually run — job death (B2) → cluster usable (B13) | `08-20 18:19:55` | `08-21 21:39:58` | 27 h 20 m 03 s | $4,932 |
| Total wall clock — job death (B2) → forward progress (B14 step 1) | `08-20 18:19:55` | `08-24 16:57:25` | 94 h 37 m 30 s | $17,074 |

**Publish the first row.** It is what the platform can deliver to someone who is watching, and it
decomposes cleanly into three parts an engineer can act on differently:

| Component | Duration | Share | Can you reduce it? |
| --- | --- | --- | --- |
| Job death → B5 reason code available | 5 h 35 m 32 s | 52% | **No.** Waiting on Google to classify the fault. Alerting does not help; you know at 2 s and can do nothing until 5 h 36 m. |
| B5 → cluster usable (the labelled repair) | 4 h 54 m 43 s | 46% | Barely — 82% of it is host-side. |
| Restart → forward progress | 12 m 23 s | 2% | Yes, and it is already small. |

**The finding that reframes the post: detection is not the bottleneck, and neither is restart.**
You learn what broke in 2 seconds and you are back to training 12 minutes after you choose to
restart, but 98% of the badput sits in two waits you cannot compress. Buying goodput back at this
scale means *not being on the critical path at all* — spare capacity to drain onto — rather than
faster detection. That is the honest version of §5, and it is a stronger argument than a feature
list.

The two excluded gaps, recorded so nobody has to rediscover them: **16 h 49 m 48 s** between B5
populating and the label being applied (operator asleep), and **67 h 05 m 04 s** between the
cluster being whole and B14 being submitted (session gap). Together $15,141 of billed idle — a
demonstration in its own right that the cluster bills whether or not the GPUs are busy.

The cluster stays down across the whole window on purpose. A genuinely faulty GPU VM would fail
any job scheduled onto it, so resubmitting mid-window would be a fiction. Two nodes and no spare
means the job cannot run at all — state that as the testbed caveat every time this number is
quoted, because at 512 GPUs you would drain onto spares and it would look completely different.

---

## What each stage demonstrates for the post

The spine of §5 — every row is a signal, an identification, and a primitive that acts on it.

| Stage | Signal you see | What it tells you | Primitive you reach for |
| --- | --- | --- | --- |
| B1 | Xid 79 in Cloud Logging, full text + PCI address | *exactly* what failed, in ms | serial console → Cloud Logging |
| B3a/b | alert in Pub/Sub and in your inbox | the operator is told without watching a dashboard | log-based alert policy, multi-channel |
| B4 | block `healthStatus: DEGRADED` | *that* something is wrong — one bit, no reason | reservation health |
| B5 | `FAILURE_GPU_XID` + rescheduleable event | *why*, and that Google has booked a repair | upcoming maintenance |
| B7 | — | pull the repair forward instead of waiting for the window | `perform-maintenance` label |
| B10 | drain window before the node goes down | time to flush state, if you have any | `gke-disruption-handling` |
| B13 | node Ready, 16 GPUs allocatable | capacity is back | GKE node lifecycle |

If the three-tier finding from run 3 holds — the fast signal is the detailed one, the slow signal
is the actionable one — then B1, B4 and B5 land seconds/seconds/hours apart, and that ordering
*is* the argument for log-based alerting.

---

## Assertions — a run that "completes" without these proves nothing

| # | Assertion | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 | Block observed `HEALTHY` before T0, so B4 is a real transition | **PASS** | First cloud tick `18:17:03.845Z`, 2 m 43 s pre-T0: `res_health` and `block_health` both HEALTHY, both instances `RUNNING`, `34t3_maint: CLEAR`, no reasons. Run 3's upper-bound caveat does not apply to this M3. |
| 2 | Reason at B5 is `FAILURE_GPU_XID` and `canReschedule` is `true` | **PASS** | `["FAILURE_GPU_XID", "FAILURE_GPU"]`, `canReschedule: true`, at `23:55:26.680Z`. Two reasons, not one — the specific Xid class *and* the generic GPU fault. |
| 3 | Label at B7 stays scoped to `-34t3`; `-6df3` unaffected | **PASS** | `-6df3` held `ready: True`, 8 allocatable GPUs, `maint_status: null`, taints unchanged across the entire 4 h 55 m, despite GROUPED maintenance scheduling on the reservation. Blast radius is one node. |
| 4 | Node UID and Compute instance id unchanged across the repair | **PASS (partial)** | Node UID `11db4e2c-c330-409c-b0b6-b0e0f66ab76d` unchanged through the repair and still current on 24 Aug; `providerID` unchanged. **Compute instance id not re-verified** — `gcloud` was unauthenticated in the 24 Aug session. Pre-repair value `8713135250882265303` is recorded in `run6/inject-result.txt`; one `gcloud compute instances describe` closes this. |
| 5 | Post-recovery step time within noise of baseline | **PASS** | Post-repair mean 111.7 s over steps 2–5 (113, 101, 116, 117 s) vs pre-fault mean 116.3 s over steps 2–8 (131, 119, 103, 119, 122, 107, 113 s). Post-repair is *inside* the pre-fault distribution and marginally faster. Step 1 (151 s post, warmup) excluded from both. |

**The assertion-5 caveat is retired, and it was solved better than planned.** The plan expected a
weak comparison against a remembered ~2 m 05 s figure. Instead the pre-injection job `dapo-run6c`
in `run6/train.log` supplied a genuine baseline: same job, same config, same nodes, ~10 minutes
before the fault. It carries no per-line wall clock, but the embedded vLLM worker log lines
(`08-20 HH:MM:SS`) bracket each `Step N/500` marker and reconstruct the boundaries to ±1 s. So the
comparison is like-for-like, not cross-day. Note the real baseline is **~1 m 56 s, not 2 m 05 s** —
correct that figure wherever it is quoted.

**A finding that belongs in §8, not here.** `prep-pods.sh` reported `PATCHED` for `worker-66twk`
— the pod that came back on the repaired node — and `ALREADY PATCHED` for the two untouched pods.
The repaired node's worker genuinely returned from the container image with neither the recipe
config nor the tokenizer patch. The trap the runbook predicted is real and it fires exactly once,
at the last step of the run, where it would have destroyed the result.

## Risks

| Risk | Mitigation | Outcome |
| --- | --- | --- |
| B5 takes ~4 h, as in runs 1 and 3 | Nothing to do but record it; the poller runs unattended for 12 h | **Materialised, worse than feared** — 5 h 36 m. Poller held. |
| Job not running at T0, so the kill is a no-op | Precondition check in P1; `inject.sh` reports `NO_GPU7_PIDS` | **Avoided** — all 8 GPUs had live processes; 2 PIDs killed on `0000:CC:00.0`, job dead in 8.56 s. |
| Config lost when the worker pod is recreated at B13 | `prep-pods.sh` at B14, before resubmitting | **Materialised, mitigation worked** — `worker-66twk` came back unpatched; `prep-pods.sh` caught it. Would have failed the run at the last step. |
| Logging read 429s hide M1 | M1 and M2 come from server-side timestamps, not poll times | **Avoided** — M1 read off `timestamp`/`receiveTimestamp`, M2 off `publishTime`. |
| 2 nodes, no spare — full host repair on the critical path | Inherent to the testbed; state it wherever the number is quoted | **Stands.** Also the dominant term in the badput analysis — see the "not the bottleneck" finding. |
| No abort after B7 | Confirm B5's reason and `canReschedule` before labelling | **Held** — both confirmed before labelling; no abort needed. |
| M2 missed for a fourth run | Retired — policy, both channels and live email delivery all verified pre-T0 (P4) | **Retired** — 2 m 15.3 s, first ever measurement. |
| *(unanticipated)* Operator gaps dwarf every measured stage | none — not foreseen | **Materialised** — 16 h 50 m + 67 h of billed idle, $15,141. Any future run needs a wall-clock budget and a teardown trigger, not just a poller. |

## Duration and cost

Two a4-highgpu-8g at $90.22/node-hour = **$180.44/hour**, billed whether or not the GPUs are
doing anything — which is exactly what is being measured.

| | If B5 lands early (~15 min) | If B5 matches runs 1 and 3 (~4 h) | **ACTUAL** |
| --- | --- | --- | --- |
| T0 → B5 | 15 min | ~3 h 46 m | **5 h 36 m** |
| B7 → B13 | 4 h 30 m | 4 h 30 m | **4 h 55 m** |
| Recovery to baseline | 25 min | 25 min | **22 min** |
| **Total** | **~5 h 10 m — $935** | **~8 h 40 m — $1,565** | **10 h 43 m — $1,933** |

B5 is the variable, and it is the one number nobody has predicted correctly across three runs.
**Four runs now.** It overshot the pessimistic estimate by 1 h 50 m. Both estimates were low
overall; the only stage that came in under was the restart. Budget the pessimistic case and then
add half again.

## Abort

Before B7, everything is reversible — the Xid is a log line and the killed job can simply be
resubmitted. **After B7 there is no abort**: the label commits a real host repair, and there is
no spare capacity to fall back on.

*Not exercised — the run went to completion.*

---

## Still open after run 6

| Gap | Status | Why it matters |
| --- | --- | --- |
| M1 detection latency | **CLOSED** | 62 ms emit / 2.075 s queryable. Quote the second number. |
| M2 alert latency | **CLOSED** | 2 m 15.3 s to Pub/Sub, email in the same minute. Open across runs 1–5. |
| M3 an upper bound, never a measurement | **CLOSED** | 24.5 s, bounded 19.5–24.5, against a verified pre-T0 HEALTHY baseline. |
| M4 varies with no known driver | **WORSE** | Now 3 samples spanning 1 h 50 m (3 h 46 m / 4 h 18 m / 5 h 36 m). Sits on the critical path and accounts for 52% of the attentive-operator badput. Needs either an explanation or an explicit "hours, unpredictably" framing in the post. |
| Recovery to baseline throughput | **CLOSED** | Assertion 5, like-for-like against the pre-injection job. Real baseline is 1 m 56 s. |
| Host-side `instance/gpu/*` metrics empty on A4 | **OPEN** | All 40 `compute.googleapis.com/instance/gpu/*` metrics carry no data. Untouched by this run. "Exists but empty" ≠ "does not exist" — resolve before publishing. |
| **No Tier 3 real fault has ever been run** | **OPEN — the big one** | Every number in runs 1–6 comes from a synthetic Xid plus a process kill. This run cannot say whether a real fault is detected faster or slower, or whether B5 behaves differently when the hardware is actually broken. Everything above is the *response* pipeline measured honestly; it is not a hardware-failure measurement. |
| Straggler / soft failure (Issue 2) | **OPEN — never attempted** | The plan's §4 failure #5 and half the stated goal of the post. No run has touched it. Needs its own detection path: per-rank step-time outliers, NCCL collective timing, `DCGM_FI_DEV_THERMAL_VIOLATION`. |
| Network / NVLink injection | **OPEN — blocked** | Only `eth0` and `lo` visible inside the worker container despite the CR annotating eth2–eth9. Needs a host-network pod for `netem`. |
| Naive vs prepared comparison (§7 of `plan.MD`) | **OPEN** | Every run so far measures the prepared path only. There is no naive-cluster arm, so the delta the cost model is meant to monetise has never been measured. |
| Compute instance id across repair | **OPEN — trivial** | One `gcloud compute instances describe` closes assertion 4 fully. |

### Recommended next run

**Run 7 should be Tier 3, and it should be the straggler.** Two reasons: it is the only one of the
top-5 failure classes that is genuinely reversible (`nvidia-smi -lgc 500,500`, then `-rgc`), so it
does not spend a 5-hour repair cycle or $900 to produce one data point; and it is the entire Issue
2 half of the post, which currently has zero measurements behind it. The hard-failure path is now
well characterised — six runs, four detection metrics closed, a full repair timeline and a verified
throughput recovery. Spending another day on it has diminishing returns while half the stated
scope has never been run.
