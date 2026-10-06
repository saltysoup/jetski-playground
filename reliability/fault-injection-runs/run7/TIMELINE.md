# Run 7 — full timeline

**T0  = 2026-08-24T22:05:13.765Z** (15:05:13 PDT) — Xid 79 injected on `-34t3`, GPU-7 PIDs killed
**T0′ = 2026-08-24T22:31:46.230Z** (15:31:46 PDT) — `cloud.google.com/perform-maintenance=true` applied

Target node `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3`, instance `8713135250882265303`.
Control node `-6df3`. Workload `dapo-run6-recovered`. PDT = UTC−7.

Sources: `run7/timeline.log` (poller, 32.2 s cadence) through 02:48:08Z; GCE API
`lastStartTimestamp` and Kubernetes node/pod `lastTransitionTime` for B12–B13 (the poller
exhausted its tick budget at 02:48:08Z, 12 min before B12).

---

## Every stage

| Stage | Event | UTC | PDT | From T0 | From T0′ |
| --- | --- | --- | --- | --- | --- |
| — | Baseline: reservation + block `HEALTHY`, `type: null`, no maintenance | 22:03:55.780 | 15:03:55 | −1 m 18 s | |
| **B0** | **T0** — Xid 79 written to `/dev/kmsg`, GPU-7 PIDs killed | **22:05:13.765** | **15:05:13** | **0** | |
| B1 | **M1** Xid emitted to Cloud Logging (`timestamp`) | 22:05:13.826 | 15:05:13 | +0.061 s | |
| B1′ | **M1′** Xid queryable (`receiveTimestamp`) | 22:05:15.838 | 15:05:15 | +2.073 s | |
| B2 | Ray job `FAILED` (`ActorUnavailableError` → raylet `SYSTEM_ERROR`) | 22:05:21.870 | 15:05:21 | +8.105 s | |
| B4 | **M3** reservation + block → `DEGRADED` | 22:05:49.376 | 15:05:49 | +35.6 s | |
| B3a | **M2** alert delivered to Pub/Sub (`publishTime`) | 22:06:49.656 | 15:06:49 | +1 m 35.9 s | |
| **B5** | **M4** `upcomingMaintenance` appears — `type: UNSCHEDULED`, `PENDING`, `canReschedule: true`, window `2026-09-01T00:00:00Z → 04:00:00Z`, reasons `["FAILURE_GPU_XID","FAILURE_GPU"]` | **22:24:17.035** | **15:24:17** | **+19 m 03 s** | |
| B6 | Operator decision (approved) | — | — | | |
| **B7** | **T0′** — `perform-maintenance=true` labelled on `-34t3` | **22:31:46.230** | **15:31:46** | +26 m 32 s | **0** |
| — | Label observed; still `UNSCHEDULED` / `PENDING` | 22:32:10.124 | 15:32:10 | +26 m 56 s | +23.9 s |
| B8 | GCE reschedules (`latestWindowStartTime` set) | 22:32:32 | 15:32:32 | +27 m 18 s | +45.8 s |
| **B8** | **`type` → `SCHEDULED`, `PENDING` → `ONGOING`, `canReschedule` → `false`, label consumed, `active-node-maintenance=passthrough` added** | **22:32:38.889** | **15:32:38** | +27 m 25 s | **+52.7 s** |
| — | New window opens (`windowStartTime`) | 22:32:42 | 15:32:42 | +27 m 28 s | +55.8 s |
| B9 | Taint `maintenance-window-started=PreferNoSchedule` | 22:33:09.153 | 15:33:09 | +27 m 55 s | +1 m 22.9 s |
| — | Reservation `maintenancePending 1→0`, `maintenanceOngoing 0→1` | 22:36:09.646 | 15:36:09 | +30 m 56 s | +4 m 23.4 s |
| — | k8s `PerformMaintenanceSuccess` — "Awaiting node termination" | ~22:36–22:48 | ~15:36–15:48 | | |
| **B10** | **`Shutdown` + `NodeNotReady`** — drain took **16 m 54 s** from the taint | **22:50:03** | **15:50:03** | **+44 m 49 s** | +18 m 17 s |
| — | Poller sees `ready=False`, `gpu=0` | 22:50:17.214 | 15:50:17 | +45 m 03 s | +18 m 31 s |
| — | Worker `-66twk` deleted → `-6wbqz` created, **`Pending`, unschedulable** | 22:50:26.122 | 15:50:26 | +45 m 12 s | +18 m 40 s |
| — | Cluster allocatable GPUs **16 → 8** | 22:50:34.832 | 15:50:34 | +45 m 21 s | +18 m 49 s |
| — | Taints `not-ready` → `unreachable` | 22:51:19.308 | 15:51:19 | +46 m 06 s | +19 m 33 s |
| **B11** | **VM `RUNNING` → `REPAIRING`** | **22:52:07.902** | **15:52:07** | **+46 m 54 s** | +20 m 22 s |
| — | *(host repair — 4 h 09 m 49 s of nothing observable)* | | | | |
| ⚠ | **Booked window closes (`windowEndTime`) — repair still `ONGOING`** | **02:32:31** | **19:32:31** | +4 h 27 m 17 s | +4 h 00 m 45 s |
| **B12** | **VM `REPAIRING` → `RUNNING`** (`lastStartTimestamp`) | **02:59:52.285** | **19:59:52** | **+4 h 54 m 39 s** | +4 h 28 m 06 s |
| **B13** | **Node `Ready=True`, 8 GPUs allocatable, 16 cluster-wide; `-6wbqz` scheduled** | **03:01:41** | **20:01:41** | **+4 h 56 m 27 s** | +4 h 29 m 55 s |
| — | Worker `-6wbqz` container running, Ray 2/2 — **cluster whole** | 03:05:40 | 20:05:40 | +5 h 00 m 26 s | +4 h 33 m 54 s |
| **B14** | `prep-pods.sh` — `-6wbqz` reported `PATCHED`, other two `ALREADY PATCHED` | 13:57:42 | 06:57:42 | +15 h 52 m 28 s | |
| **B14** | `dapo-run7-recovered` submitted | 13:58:02 | 06:58:02 | +15 h 52 m 48 s | |
| **B14** | First training step begins (setup = 10 m 58 s) | ~14:09 | ~07:09 | +16 h 04 m | |
| **B14** | 8 steps recorded, mean 116.57 s vs 116 s baseline — **cluster back to full throughput** | 14:21 | 07:21 | +16 h 16 m | |

---

## Durations that matter

| Interval | Duration |
| --- | --- |
| T0 → job dead (badput starts) | 8.1 s |
| T0 → **M4** maintenance reason available (B5) | **19 m 03 s** |
| B5 → B7 (operator decision) | 7 m 29 s |
| **B7 → `type` flip (B8)** | **52.7 s** |
| B8 taint → node drained (B10) | 16 m 54 s |
| B10 → VM `REPAIRING` (B11) | 2 m 05 s |
| **Host repair (`REPAIRING` → `RUNNING`)** | **4 h 09 m 49 s** |
| VM `RUNNING` → node `Ready` | 1 m 49 s |
| Node `Ready` → Ray 2/2 | 3 m 59 s |
| **T0 → cluster whole** | **5 h 00 m 26 s** |
| Booked window (`22:32:42Z → 02:32:31Z`) | 3 h 59 m 49 s |
| **Repair overrun past window close** | **+27 m 21 s** |

---

## Three findings for maintenance engineering

### 1. M4 is non-deterministic across a 17.6× range — not "5 hours"

Identical stimulus, identical node, four runs:

| Run | T0 → maintenance reason populated |
| --- | --- |
| **7** | **19 m 03 s** |
| 3 | 3 h 46 m 10 s |
| 1 | 4 h 17 m 48 s |
| 6 | 5 h 35 m 40 s |

The question is not "why 5 hours". It is **why the same failure yields 19 minutes on one day and 5 h 36 m on another**, with no exposed signal that lets a customer tell which they are in. Bounded to (22:23:44.835, 22:24:17.035] by the 32.2 s poll cadence.

Also: the **Kubernetes annotation `node.gke.io/upcoming-maintenance` surfaced B5 before the GCE API poll did** (22:24:17 vs 22:26:30). Partly a slow-poll artefact, but the annotation is the faster surface to watch.

### 2. The label *does* flip the type — but it does not change the duration

Run 6 discarded the `type` field, so this could not be answered before. Run 7 captured it verbatim. Pre-label 22:32:10 vs post-label 22:32:38:

| Field | Before | After |
| --- | --- | --- |
| `type` | `UNSCHEDULED` | **`SCHEDULED`** |
| `maintenanceStatus` | `PENDING` | `ONGOING` |
| `canReschedule` | `true` | `false` |
| `windowStartTime` | `2026-09-01T00:00:00Z` | `2026-08-24T22:32:42Z` |
| `windowEndTime` | `2026-09-01T04:00:00Z` | `2026-08-25T02:32:31Z` |
| `maintenanceReasons` | `["FAILURE_GPU_XID","FAILURE_GPU"]` | **unchanged** |
| node label | — | `active-node-maintenance=passthrough` |
| `perform-maintenance` | `true` | consumed by GKE |

**The type flip is real.** The duration claim is not:

- `UNSCHEDULED` window: `00:00:00Z → 04:00:00Z` = **4 h 00 m 00 s**
- `SCHEDULED` window: `22:32:42Z → 02:32:31Z` = **3 h 59 m 49 s**

Identical. The 4-hour window was baked into the `UNSCHEDULED` event seven days out, before any label existed. The label changed **when** and **the type string** — not **how long**. No "15 minutes" appears anywhere in any run's data.

**Hypothesis to put to the team:** `maintenanceReasons` stays `FAILURE_GPU_XID` throughout, so `type` appears to track *scheduling state* ("a window is pinned and running") rather than cause or urgency. If that is the intent, `SCHEDULED` is a misleading name and the planned-vs-unplanned framing everyone reasons from is a category error.

Note also that the enum is **`SCHEDULED`**, not `PLANNED`. `PLANNED_UPDATE` is a value of the
*separate* `maintenanceReasons` field, which never changed here. Saying "the type became
planned" conflates two different fields.

#### 2a. The booking is 7 days out, snapped to midnight UTC

| Run | B5 (reason appears) | `windowStartTime` booked | Gap |
| --- | --- | --- | --- |
| 6 | 2026-08-20T23:55:26.680Z | 2026-08-28T00:00:00Z | 7 d 00 h 04 m |
| 7 | 2026-08-24T22:24:17.035Z | 2026-09-01T00:00:00Z | 7 d 01 h 36 m |

Not exactly 168 h — **00:00:00 UTC on detection-day + 8**. Both runs fit that rule exactly.

#### 2b. The "4 hours" is a start-time window, not a duration

The B5 annotation, verbatim, before any label existed:

```json
"start_time_window": { "earliest": "2026-09-01T00:00:00+00:00",
                       "latest":   "2026-09-01T04:00:00+00:00" },
"window_start_time":  "2026-09-01T00:00:00+00:00",
"window_end_time":    "2026-09-01T04:00:00+00:00",
"type": "UNSCHEDULED"
```

GCE's own naming calls that span `start_time_window: earliest → latest` — the window inside
which maintenance may **begin**. `windowStartTime`/`windowEndTime` are the same two instants
under different names. The label re-anchored the identical 4-hour start-window to now
(`22:32:42 → 02:32:31`, 3 h 59 m 49 s); it did not create it.

This also explains the overrun in finding 3: the repair ran 27 m past `windowEndTime` and
nothing complained, which is exactly right if that field is "latest permitted start" rather
than "done by."

**Where the 4-hour folklore probably comes from.** Repair duration across four runs is
4 h 06 m 55 s / 4 h 05 m 18 s / 4 h 01 m 38 s / 4 h 09 m 49 s — **~4 h 06 m ± 4 min,
regardless of type**. The start-window width is also 4 h. Two unrelated 4-hour numbers,
welded into "SCHEDULED maintenance takes 4 hours."

**Two questions to put to them directly:**
1. Is `windowEndTime` the latest permitted **start**, or an expected **completion**? Our
   overrun says the former; the name says the latter.
2. Does `type` ever track cause, or is it purely scheduling state?

The counterfactual our data genuinely **cannot** answer: what this host would have done at
`2026-09-01T00:00Z` had we never applied the label.

### 3. `windowEndTime` is not a deadline, and nothing says so

The window closed at 02:32:31Z. The repair ran 27 m 21 s past it. Across that overrun the object did **not** mutate — no extension, no new window, no status change:

```
windowEndTime:     2026-08-25T02:32:31Z   <- silently in the past
maintenanceStatus: ONGOING                <- unchanged
type:              SCHEDULED, canReschedule: false
```

Anyone polling `windowEndTime` to know when capacity returns gets no signal at all — the field just goes stale. And `ONGOING` describes the *window*, not the *work*: it was `ONGOING` for ~17 minutes while the VM was still `RUNNING`, the node `Ready`, the pod un-evicted and 16 GPUs allocatable.

---

## Badput

At **$180.44/hour** for the 2-node testbed:

| Window | Duration | Cost |
| --- | --- | --- |
| T0 → cluster whole (unavoidable, the thing being measured) | 5 h 00 m 26 s | **$903** |
| Cluster whole → B14 gate (idle, waiting on a human) | 10 h 45 m + | **$1,940+** |

KubeRay replaced the dead worker within 23 s of the shutdown, but `-6wbqz` sat `Pending` with no node for the entire repair — the scheduler had nowhere to put it. With no spare capacity, fast pod-level recovery buys nothing; the floor is the host repair.

---

## Assertions

| # | Assertion | Status |
| --- | --- | --- |
| 1 | Block observed `HEALTHY` before T0 | **PASS** — 22:03:55.780Z, 1 m 18 s pre-T0 |
| 2 | B5 reason is `FAILURE_GPU_XID`, `canReschedule` true | **PASS** — both, at 22:24:17.035Z |
| 3 | Label at B7 stays scoped to `-34t3` | **PASS** — `-6df3` never taken, never tainted, `Ready` throughout |
| 4 | Node UID and instance id unchanged across repair | **PASS** — uid `11db4e2c-c330-409c-b0b6-b0e0f66ab76d`, instance `8713135250882265303`, both unchanged post-repair |
| 5 | Post-recovery step time within noise of **1 m 56 s** baseline | **PASS** — 8 steps, mean **116.57 s (1 m 56.6 s)**, **+0.5%** vs baseline. First 5: 123.35 / 129.47 / 102.49 / 117.82 / 121.78 s (mean 118.98 s). Steps 6–8: 105.57 / 112.37 / 119.73 s. Spread 102.5–129.5 s is ordinary DAPO variance. Setup 657.7 s. |

Post-repair state verified 13:51Z 25 Aug: both nodes `Ready` with 8 GPUs each, Ray 2/2,
`upcomingMaintenance: {}`, reservation `HEALTHY` / `degradedBlockCount: 0`, all maintenance
labels and taints consumed.
