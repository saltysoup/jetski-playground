# Run 9 — final confirmation test plan

**Status: awaiting approval. Nothing injected, nothing labelled.**

Target `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3` (instance `174490015860863227`),
control `-6df3`. Zone `europe-west4-b`, project `gpu-launchpad-playground`, reservation
`nvidia-b200-6bsoymep8ylww`.

---

## 1. Why run 9 exists

Two things to confirm, one new and one left open.

**New — repair duration.** We have been told repairs should now be much faster than the
~4 hours seen previously. Our actual measurements are worse than "4 hours":

| Repair | Label applied | Label → node usable | Source |
| --- | --- | --- | --- |
| 8d | 2026-08-31T22:38:03.073Z | **4 h 36 m 03 s** | `timeline-8d.log`, full poller coverage |
| 8f | 2026-09-04T01:23:32.702Z | **≈4 h 47 m** (4 h 45 m 01 s label → VM start) | instance `lastStartTimestamp`; poller died at 02:32Z |

So the baseline to beat is **~4.6–4.8 h**, from two repairs on the same instance three days
apart, and the second was ~9 min *slower* than the first. If the new claim holds, run 9
should come in dramatically under this. This is the headline measurement.

**Open — the detection threshold.** 8d degraded on the 3rd Xid 63 (16.6 s after it). 8f
degraded on the **2nd**, 13.3 s *before* the 3rd landed — but 8f had a forced GPU reset
between every injection, so we cannot tell whether two Xids are simply enough, or whether
the reset's driver unload/reload contributed. Run 9 resolves this by injecting **without
any reset** and stopping at two.

These compose neatly: the minimal-stimulus test is also what raises the maintenance event
that the repair test consumes. One event, both questions.

---

## 2. Baseline verified 2026-09-24

```
34t3   Ready, 8 allocatable GPUs, VM RUNNING, upcomingMaintenance null
6df3   Ready, VM RUNNING
res_health HEALTHY, block HEALTHY, pending/ongoing null
no perform-maintenance or perform-reboot labels present
```

Clean. No leftover state from 8f.

---

## 3. Design decisions

**No GPU reset anywhere in run 9.** 8f already answered "does the reset suppress
escalation" — it does not. Removing the reset makes run 9 a clean replication of 8d's
conditions with only the injection count varied, which is exactly what the threshold
question needs. `force-reset-gpu7.sh` is not used.

**Same node, `-34t3`.** It has had two repairs and all prior data is on it, so timings are
directly comparable. The alternative — testing on `-6df3` for host independence — buys
independence at the cost of comparability and loses the control node. Comparability wins,
but note the caveat in the write-up: if `-34t3`'s host is treated differently after two
repairs, run 9's repair time is not necessarily a fleet-wide number.

**Fresh row addresses.** `ROW_BASE=0x1026ba9c40` → rows `0x…baac40` … `0x…baec40`, distinct
from 8d (`b7ac40`–`b7ec40`) and 8f (`b9ac40`–`b9ec40`) so `logscan63.py` can attribute
every line unambiguously.

**Faster polling than 8f.** 8f's 15 s cadence is what prevented us from claiming the
trigger→reason interval was a fixed pipeline delay — the bound was wider than the effect.
Run 9 polls **5 s during detection** and **10 s across the whole repair**. If the repair is
now ~30 min rather than ~4.7 h, 15 s resolution would also be too coarse to characterise
the phases.

**The poller must survive the session.** The single biggest instrumentation failure so far:
8f's repair completion was lost because the watcher died at ~02:32Z and we only recovered
the end time retroactively from `lastStartTimestamp`. Run 9 adds (a) a supervisor that
restarts the watcher if it exits, (b) append-mode logging so a restart does not truncate,
and (c) an independent 60 s probe writing raw instance JSON to disk, so even total watcher
loss leaves ground truth.

---

## 4. Phases

### Phase 0 — arm (≈10 min, no stimulus)

1. Re-verify the five guard conditions on `-34t3`: Ready, 8 allocatable GPUs, VM RUNNING,
   `upcomingMaintenance` null, `res_health` HEALTHY. Abort on any failure.
2. Record pre-state: driver version, and for all 8 GPUs `ecc.errors.uncorrected.volatile.total`,
   `remapped_rows.uncorrectable`, `remapped_rows.pending`, `remapped_rows.failure` → `pre-gpu-health.txt`.
3. Confirm no stale `perform-maintenance` / `perform-reboot` / `scheduled-maintenance-time` labels.
4. Start `watch9.py` at 5 s under the supervisor → `timeline-9.log`.
5. Start `instance-probe.sh` at 60 s → `instance-9.jsonl`.
6. Confirm the `xid-inject-run8` pod is Running on `-34t3` (the injection path).
7. Capture baseline for ≥2 min before any injection.

### Phase 1 — minimal-stimulus ladder (30–75 min)

No resets. ~85 s spacing (8d's proven cadence). **Stop at the first sign of escalation.**

| Step | Action | Then |
| --- | --- | --- |
| 1 | Inject Xid 63 #1, #2 at ~85 s apart | observe **15 min** |
| 2 | If no `DEGRADED` and no reason → inject #3 | observe **15 min** |
| 3 | If still nothing → inject #4 | observe **15 min** |
| 4 | If still nothing → inject #5 | observe **30 min** |
| 5 | If still nothing after 5 → **abort before B7** | run `logscan63.py` to separate "filtered" from "platform declined" |

Outcomes and what each means:

- **Fires on 2** → two Xid 63s are sufficient on their own; 8f's early degradation was not
  reset-assisted, and the "three consecutive" rule as stated is wrong.
- **Fires on 3** → the rule is right, and 8f's degradation on two *was* reset-assisted —
  i.e. a driver unload/reload feeds the same detector. That is a finding in its own right.
- **Fires on 4–5, or not at all** → something changed in the last three weeks; the filtering
  question reopens and `logscan63.py` is the discriminator.

### Phase 2 — detection measurement (no action, just capture)

Record to ms, using serial-console `timestamp` as authoritative for injections:

| Metric | Definition | 8d | 8f |
| --- | --- | --- | --- |
| M1′ | Xid queryable in Cloud Logging | ~2 s (run 6) | — |
| M3 | block + reservation → `DEGRADED` | i3 + 16.6 s | i2 + 1 m 54.2 s |
| **M4** | **`upcomingMaintenance` populated** | **i1 + 12 m 32.1 s** | **i1 + 11 m 03 s** |
| — | `DEGRADED` → reason | 9 m 24.2 s | 7 m 46.4 s |
| — | trigger injection → reason | 9 m 40.75 s | 9 m 40.60 s |

Capture the event verbatim: `type`, `maintenanceStatus`, `maintenanceReasons`,
`canReschedule`, `windowStartTime`, `windowEndTime`, `latestWindowStartTime`. With 5 s
polling the trigger→reason interval gets a ±5 s bound instead of ±15 s, which is what the
"fixed pipeline delay" hypothesis needs to be confirmed or killed.

### Phase 3 — **APPROVAL GATE (B7)**

Full stop. I present the raised event and the detection numbers, and wait for an explicit
go/no-go. **After the label there is no abort path** — `canReschedule` goes `false` within
~1 min and the repair proceeds to completion.

### Phase 4 — repair, B7 → B14 (the headline measurement)

Apply `cloud.google.com/perform-maintenance=true`, record `LABEL_SENT`/`LABEL_ACK` to ms
→ `t-label-9.txt`. Poll at 10 s and capture every stage:

| Stage | Event | 8d reference |
| --- | --- | --- |
| B8 | `PENDING`→`ONGOING`, `UNSCHEDULED`→`SCHEDULED`, `canReschedule`→`false`, **window rewritten** | +20.9 s (8f: +56.3 s) |
| — | reservation + block `pending 1→0`, `ongoing 0→1` | +53.3 s |
| B9 | taint `maintenance-window-started=PreferNoSchedule` | — |
| B10 | node `Shutdown` + `NodeNotReady`; drain duration; pod evictions; cluster allocatable 16→8 | run 7: 16 m 54 s from taint |
| — | VM `RUNNING` → `REPAIRING` | +25 m 52 s (8f: +39 m 06 s) |
| — | VM `REPAIRING` → `RUNNING` (cross-check `lastStartTimestamp`) | +4 h 33 m 54 s |
| — | maintenance `ONGOING` → `CLEAR`, reasons → null, reservation `HEALTHY` | +4 h 28 m 00 s |
| — | block `maintenanceOngoingCount 1 → null` | +4 h 01 m 24 s |
| B12/B13 | node `Ready`; cluster allocatable 8→16 | — |
| **B14** | `prep-pods.sh` then resubmit workload (**pre-approved**, no gate) | — |

**Headline metric: label → node `Ready` with 8 allocatable GPUs.** Compare against
4 h 36 m 03 s (8d) and ≈4 h 47 m (8f).

### Phase 5 — re-test the three secondary findings against a fast repair

These were derived from a 4.6 h repair and may not survive a 30 min one:

1. **Is `windowEndTime` still advisory?** 8d overran its stated end by 27 m 46 s. If GCE now
   stamps a *shorter* window at B8 (rather than the usual 4 h), that is itself strong
   evidence the repair procedure changed — record the rewritten window verbatim.
2. **Does `maintenanceOngoingCount` still clear early?** It went `1 → null` 26 m 36 s before
   the maintenance actually cleared in 8d. On a short repair that error may dominate.
3. **Does reservation health still lead the VM?** 8d: 5 m 53 s. Worth knowing whether the
   gap is a fixed lag or a fraction of repair time.

### Phase 6 — write-up

`run9/RESULTS.md` and `run9/TIMELINE.md` in run 6/7 house style; update `run8/RESULTS.md`
with the recovered 8f repair figure (4 h 45 m 01 s) and cross-link; correct `plan.MD` §9
(~2 m 05 s → 1 m 56 s) while in there.

---

## 5. Success criteria

Run 9 succeeds if it produces **all four**:

1. A number for label → usable node, directly comparable to 4 h 36 m / 4 h 47 m.
2. A definitive answer on 2 vs 3 Xid 63s with no reset confound.
3. A reproduced M4 (Xid → maintenance reason) to ±5 s, third data point after 12 m 32 s and 11 m 03 s.
4. The rewritten maintenance window recorded at B8, to see whether GCE's own advertised
   window shrank along with the repair.

A *slow* repair is still a valid result — it means the change has not reached this
reservation, which is exactly what maintenance engineering needs to hear.

---

## 6. Abort conditions

| Condition | Action |
| --- | --- |
| Guard fails in Phase 0 | stop, do not inject |
| No escalation after 5 injections | stop **before B7**, run `logscan63.py`, report as a filtering/threshold change |
| Injected lines absent from Cloud Logging | stop before B7 — we would be testing the filter, not the platform |
| Node auto-repaired or recreated mid-run (cf. 8a round 5) | stop, record, re-baseline |
| Anything unexpected between B7 and B10 | **no abort exists** — record and continue |

---

## 7. Cost and duration

At **$180.44/h** for the two-node testbed:

| Scenario | Duration | Cost |
| --- | --- | --- |
| Claim holds (repair ~30 min) | ~1.5 h | ~$270 |
| Claim partly holds (~2 h) | ~3 h | ~$540 |
| Unchanged (~4.7 h) | ~5.75 h | ~$1,040 |

Phase 1 worst case (ladder to 5 injections) adds ~45 min / ~$135.

---

## 8. Artifacts

Reused unchanged: `inject63.sh`, `logscan63.py`, `probe.py`, `prep-pods.sh`.
New: `watch9.py` (adaptive cadence, append mode), `watch-supervisor.sh`,
`instance-probe.sh`, `run9-detect.sh` (the ladder).
Explicitly **not** used: `force-reset-gpu7.sh`, `run8f.sh`.

Outputs: `timeline-9.log`, `instance-9.jsonl`, `pre-gpu-health.txt`, `post-gpu-health.txt`,
`inject63-i{1..5}.txt`, `t-label-9.txt`, `run9.log`, `RESULTS.md`, `TIMELINE.md`.

---

## 9. Decisions — LOCKED 2026-09-24

1. **Scope: full run through B14.** Both questions answered in one event. B7 approval gate
   stands at Phase 3.
2. **Workload: yes, real DAPO training job running during the repair.** B10 drain/eviction
   is measured for real. Run 7 saw 16 m 54 s from taint to shutdown with a live job; if the
   repair is now ~30 min, drain may be the dominant term in the operator's total outage,
   which would be the most useful finding in the run. This adds a Phase 0 prerequisite:
   the job must be submitted and past its first checkpoint before Phase 1 begins.
3. **Node: `-34t3`**, for comparability with 8d and 8f. Caveat to carry into the write-up:
   this host has been repaired twice, so run 9's repair time is not automatically a
   fleet-wide number.

### Consequence of decision 2 — added to Phase 0

- Submit the DAPO job and confirm it is training (past step ~6, as in run 6) before injecting.
- Record the Ray submission ID and T0 offset so job-death latency can be measured against
  the injection, as in run 6 (`ActorUnavailableError` → raylet `SYSTEM_ERROR`, 8.56 s).
- **Total-outage metric added:** job death → job running again after B14. This is the number
  an operator actually feels, and no previous run has measured it end to end.
