# Run 8 — what actually triggers emergent maintenance

Target node `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3`, instance
`174490015860863227`. Control node `-6df3`. Zone `europe-west4-b`, project
`gpu-launchpad-playground`, reservation `nvidia-b200-6bsoymep8ylww`.

Run 6 and run 7 measured what happens *after* a fault is recognised. Run 8 asks the
prior question: **what does the platform have to see before it books a repair at all?**
Six sub-runs, one variable changed at a time.

All injection timestamps below are **serial-console `timestamp`** values — the platform's
own clock, not ours. Reservation and maintenance timestamps come from `watch8d.py` at a
15 s poll cadence, so every one carries a 15 s upper bound on observation error. Where
that bound matters to a conclusion it is stated.

Sources: `timeline-8d.log`, `timeline-8f.log`, `run8d.log`, `run8f.log`,
`iterations.txt` (8a), `iterations-8b.txt`, `reset-8e-i1.txt`, `reset-8f-i{1..5}.txt`.

---

## The matrix

| Sub-run | Date | Stimulus | Between injections | Result |
| --- | --- | --- | --- | --- |
| **8a** | 08-27 | Xid 48 × 5, ~22 min apart | `perform-reboot=true` | **no trigger** (rounds 1–4); round 5's reboot left the local SSD read-only → GKE auto-repair deleted and recreated the node |
| **8b** | 08-27 | Xid 48 × 5, ~17 min apart | nothing | **no trigger** — `res_health` HEALTHY throughout, `upcomingMaintenance` null on all five |
| 8c | — | *(folded into 8d)* | | |
| **8d** | 08-31 | **Xid 63 × 5, ~85 s apart** | nothing | **TRIGGERED** — degraded on #3, reason in 9 m 41 s |
| **8e** | 09-01 | Xid 63 + GPU reset × 5 | `nvidia-smi --gpu-reset -i 7` | **blocked** — reset refused, rc=255, run aborted at round 1 |
| **8f** | 09-03 | **Xid 63 + forced GPU reset × 5** | reset forced through | **TRIGGERED** — degraded on #2, reason in 11 m 03 s |

Two clean negatives (Xid 48), two clean positives (Xid 63). The Xid class is the variable
that matters, and it matters absolutely: five Xid 48s over 85 minutes with a full VM
reboot between each produced nothing at all, while three Xid 63s in three minutes booked
a host repair.

---

## 8f — the headline result

**Does performing the prescribed remediation between faults suppress escalation?**

A real row-remap event tells the operator to fix it: *"reset gpu to activate."* If the
reset clears whatever counter the platform keeps, then an operator following NVIDIA's own
guidance silences the very signal that gets the host repaired, and a genuinely failing GPU
limps on indefinitely. That is the failure mode 8f was built to detect.

**It does not happen. The reset does not suppress escalation.**

### Execution — 5/5 rounds, 5/5 resets successful

| # | Xid 63 injected | Row address | Reset | GPUs after |
| --- | --- | --- | --- | --- |
| 1 | 21:00:08.456 | `0x0000001026b9ac40` | rc=0 | 8 · ECC 0 · remap 0 · pending No · failure No |
| 2 | 21:01:30.428 | `0x0000001026b9bc40` | rc=0 | 8 · ECC 0 · remap 0 · pending No · failure No |
| 3 | 21:03:37.889 | `0x0000001026b9cc40` | rc=0 | 8 · ECC 0 · remap 0 · pending No · failure No |
| 4 | 21:06:05.415 | `0x0000001026b9dc40` | rc=0 | 8 · ECC 0 · remap 0 · pending No · failure No |
| 5 | 21:10:04.218 | `0x0000001026b9ec40` | rc=0 | 8 · ECC 0 · remap 0 · pending No · failure No |

All five rows confirmed present in Cloud Logging (`logscan63.py`, `XID63_LOG_HITS 11`
including 8d's five and 8e's one) — **nothing was filtered**. Both Ray workers stayed
`3/3 Running` with 0 restarts across all five reset cycles.

### Escalation

| Event | UTC | From i1 | Note |
| --- | --- | --- | --- |
| i1 injected | 21:00:08.456 | 0 | |
| i2 injected | 21:01:30.428 | +1 m 22.0 s | |
| **block + reservation → `DEGRADED`** | **21:03:24.614** | **+3 m 16.2 s** | **+1 m 54.2 s after i2, and 13.3 s *before* i3** |
| i3 injected | 21:03:37.889 | +3 m 29.4 s | escalation already underway |
| i4 injected | 21:06:05.415 | +5 m 57.0 s | |
| i5 injected | 21:10:04.218 | +10 m 55.8 s | |
| **`upcomingMaintenance` populated** | **21:11:11.028** | **+11 m 02.6 s** | +7 m 46.4 s after `DEGRADED` |

```
maintenanceStatus     PENDING
maintenanceReasons    ["FAILURE_GPU_XID", "FAILURE_GPU"]
type                  UNSCHEDULED
canReschedule         true
windowStartTime       2026-09-11T00:00:00Z
windowEndTime         2026-09-11T04:00:00Z
latestWindowStartTime 2026-09-11T00:00:01Z
```

Reservation and block both `maintenancePendingCount 1`, `maintenanceOngoingCount 0`.

### 8f vs 8d

| | 8d — no reset | 8f — reset each round |
| --- | --- | --- |
| Injections before `DEGRADED` | 3 | **2** |
| Trigger injection → `DEGRADED` | 16.6 s | 1 m 54.2 s |
| `DEGRADED` → maintenance reason | 9 m 24.2 s | 7 m 46.4 s |
| **i1 → maintenance reason** | **12 m 32.1 s** | **11 m 02.6 s** |
| Maintenance window offered | 2026-09-08 00:00–04:00Z | 2026-09-11 00:00–04:00Z |

Escalation was, if anything, marginally *faster* with the reset in the loop, and required
one fewer Xid.

---

## Findings worth carrying into the post

**The Xid class is the gate, not the Xid count.** Ten Xid 48s across 8a and 8b — spread
over hours, with VM reboots in 8a — produced no degradation, no reason, nothing. Two to
three Xid 63s inside three minutes booked a host repair twice. Anyone reasoning about
"how many errors before Google acts" is asking the wrong question; the answer is
determined by which error, and Xid 63 (row remapper) is on the list while Xid 48 (double
bit ECC) is not. This is worth confirming with the GCE team, because it is
counter-intuitive: Xid 48 is the more alarming code to most operators.

**Doing the right thing locally does not cost you the fleet-level repair.** This is the
operator-facing headline. Reset the GPU to activate the row remap — as the driver message
instructs — and the platform still sees the pattern and still books the fix. The two
remediation layers do not fight each other. Before 8f this was an open risk in the
runbook; it is now measured.

**"Three consecutive Xid 63" is approximately right, not exactly right.** The rule as
given to us predicts degradation on the third. 8d matched it exactly (16.6 s after #3).
8f degraded after **two**, 13.3 s before the third even landed. So the threshold is not a
hard count of three, or the window/ordering semantics differ from the plain reading, or
the reset cycle contributes something of its own. Worth pinning down with the engineer —
specifically what "consecutive" is scoped to and whether a driver unload/reload counts.

**The reset generates no Xids of its own.** Attribution here was the obvious objection, so
the full serial console for 20:50–21:16 was pulled and read. The only Xid lines in the
entire window are our five injected 63s. A forced reset emits exactly:

```
[drm] [nvidia-drm] [GPU ID 0x0000cc00] Removing device
[drm] [nvidia-drm] [GPU ID 0x0000cc00] Unloading driver
NVRM: Attempting to remove device 0000:cc:00.0 with non-zero usage count!
NVRM: Continuing with GPU removal for device 0000:cc:00.0
[drm] [nvidia-drm] [GPU ID 0x0000cc00] Loading driver
[drm] Initialized nvidia-drm 0.0.0 for 0000:cc:00.0 on minor 7
```

plus `kbifCacheVFInfo_GB100` and `_gpuFabricProbeRbmSleepLinks` noise that also appears
with no reset in progress. Zero Xids. The reason string is `FAILURE_GPU_XID`. The residual
caveat — which 8f cannot separate — is whether the driver unload/reload feeds the same
detector and is why two Xids sufficed instead of three.

**Possible fixed pipeline delay from trigger to reason — flagged as a hypothesis, not a
result.** The interval from the injection that completes the trigger pattern to the
maintenance reason appearing was 9 m 40.75 s in 8d (i3) and 9 m 40.60 s in 8f (i2) — 0.15 s
apart. That is a suspiciously tight match and would suggest the reason is computed on a
fixed schedule from the triggering event rather than from the `DEGRADED` flip, which
varied by 98 s between the two runs. **But it cannot be claimed on this evidence:** n=2,
the 15 s poller means the true intervals are only bounded to [565.6, 580.8] s, and an
alternative alignment one injection earlier (8d i2 ↔ 8f i1, ~664.7 s) fits equally well
because the two runs had similar injection spacing. Needs a run with deliberately uneven
spacing and a faster poller to resolve.

**Nothing here resembles the ~5-hour reason delay seen in earlier runs.** Two consecutive
runs landed at 11–12 minutes from first injection to `maintenanceReasons`. Run 7 was
19 m 03 s from a single Xid 79. Whatever produced the multi-hour delay is not the normal
path and remains an open question for the maintenance engineering meeting.

**Detection has two stages and only the second is actionable.** `DEGRADED` arrives in
3 min and carries one bit — something is wrong on this block. The maintenance reason
arrives at 11–12 min and is the first signal that names the fault and offers a window. Any
runbook keyed on reservation health alone is reacting to strictly less information than
one keyed on the Xid in Cloud Logging, which is available in ~2 s (run 6, M1′).

---

## 8e — why the first attempt at this failed, and the GKE dependency it exposed

8e is 8f's experiment run two days earlier. It aborted at round 1:

```
--- processes holding GPU 7 ---
(none)
--- attempting reset of GPU 7 ---
The following GPUs could not be reset:
  GPU 00000000:CC:00.0: In use by another client
RESET_RC=255
```

The diagnosis at the time — that Fabric Manager held the device and this was a structural
NVSwitch limitation on A4/B200 — **was wrong**. `nv-fabricmanager` is not running on this
node and there are zero `/dev/nvidia-nvswitch*` devices; Fabric Manager appears only in
NVIDIA's generic error boilerplate. Two things had gone wrong:

1. **`nvidia-smi --query-compute-apps` reports only CUDA compute contexts.** It printed
   `(none)` while four processes held `/dev/nvidia7` open. Scanning `/proc/*/fd` is the
   only reliable way to enumerate device holders.
2. **The real holders are the GKE GPU management stack** — `nvidia-persistenced`,
   `nv-hostengine`, `nvidia-gpu-device-plugin`, `dcgm-exporter`. Software. Stoppable.

### The dependency, documented

This is the reusable operational finding, implemented in `force-reset-gpu7.sh`. **On GKE,
a single-GPU `nvidia-smi --gpu-reset` requires:**

- `systemctl stop nvidia-persistenced.path nvidia-persistenced.service` — the `.path` unit
  matters; stopping only the service lets the path trigger restart it immediately
- `nvidia-smi -pm 0`
- killing `nv-hostengine`, `dcgm-exporter` and `nvidia-gpu-device-plugin`

with one non-obvious complication. The last three are DaemonSet containers and **kubelet
restarts them within ~1 s**. The first attempt (`reset-8f-preflight.txt`) failed with
`DRAIN_WAITED=0s REMAINING=2` and fresh PIDs: the `/proc` scan sitting between the kill and
the reset was itself slow enough that the holders were back before `nvidia-smi` opened the
device. The fix is to **kill repeatedly to drive kubelet into restart backoff**, then kill
and reset back to back with no scan in between:

```
for k in 1 2 3; do pkill -TERM …; sleep 2; done   # induce backoff
pkill -TERM …                                      # final kill
nvidia-smi --gpu-reset -i 7                        # immediately, no scan
```

`reset-8f-preflight2.txt` then returned `GPU 00000000:CC:00.0 was successfully reset.`
and this held for all five rounds of the real run.

Ordering note for anyone reusing `run8f.sh`: each round injects **first**, with the whole
management stack up, and only then takes the holders down for the reset window. Stopping
the daemons once and running all five rounds inside that window would be simpler, but if
managed DCGM or the device plugin sits in the platform's detection path, a null result
would be indistinguishable from "we broke the detector". Keeping them up at injection time
is what makes a null interpretable.

---

## The 8d repair — B7 to B14 measured end to end

8d's maintenance event was converted into a real host repair by labelling the node
`cloud.google.com/perform-maintenance=true` at **2026-08-31T22:38:03.073Z** (`t-label-8d.txt`).
This is the only complete repair cycle measured in run 8. All offsets are from the label.

| Event | UTC | From label |
| --- | --- | --- |
| `PENDING`→`ONGOING`, `UNSCHEDULED`→`SCHEDULED`, `canReschedule` `true`→`false`, window rewritten `2026-09-08T00:00Z` → `2026-08-31T22:38:27Z` | 22:38:23.996 | **+20.9 s** |
| Reservation + block `maintenancePendingCount 1→0`, `maintenanceOngoingCount 0→1` | 22:38:56.339 | +53.3 s |
| VM `RUNNING` → `REPAIRING` | 23:03:55.590 | +25 m 52.5 s |
| Block `maintenanceOngoingCount 1 → null` | 09-01 02:39:27.177 | +4 h 01 m 24.1 s |
| Maintenance `ONGOING` → `CLEAR`, reasons → null, reservation healthy | 03:06:03.392 | **+4 h 28 m 00.3 s** |
| VM `REPAIRING` → `RUNNING` | 03:11:56.626 | +4 h 33 m 53.6 s |
| Node `Ready` with 8 allocatable GPUs | ~03:14:06 | **+4 h 36 m 03 s** |

Time spent in `REPAIRING`: **4 h 08 m 01 s**.

**The type flip answers the standing question.** Applying the label did not merely approve
the existing event — 20.9 s later GCE rewrote it: `UNSCHEDULED` became `SCHEDULED`,
`canReschedule` went `true`→`false`, and the window jumped from eight days out to 24 s in
the future. That is why the maintenance type appears to change from unplanned to planned:
**the label converts the event**, it does not just consent to it. After that flip there is
no abort path.

### Three findings on the repair signals

**`windowEndTime` is advisory and does not bound the repair.** The window closed at
`2026-09-01T02:38:17Z` with `maintenanceStatus` still `ONGOING`. It was never extended.
The event cleared **27 m 46 s after its own stated end**, and the node was not usable until
**35 m 49 s** after it. Do not build a runbook, an SLO, or a capacity plan on this field.

**`reservationBlocks[].reservationMaintenance.maintenanceOngoingCount` is not a reliable
"repair in flight" signal.** It went `1 → null` at 02:39:27, **26 m 36 s before** the
maintenance actually cleared. Anything polling that counter to decide the host is done will
decide it early.

**Reservation health clears before the VM does, and Kubernetes lags both.** Maintenance
cleared and the reservation went healthy at 03:06:03; the VM did not reach `RUNNING` until
03:11:56, **5 m 53 s later**; the node annotation and allocatable GPU count trailed further
still. The k8s node annotation lags the GCE API in *both* directions. A readiness gate must
poll the instance and the node, not the reservation.

---

## Open questions for maintenance engineering

1. What does PSIS stand for, and is emergent maintenance immediate or reschedulable?
2. What is "consecutive" scoped to — a count, a time window, or a rate? Does a driver
   unload/reload reset or contribute to it? (8f degraded on two, not three.)
3. Is Xid 48 genuinely not a trigger? 8a and 8b say no across ten injections.
4. When did the filtering that strips Xids with fake PCIe addresses deploy, and does it
   validate anything beyond the address? Our lines pass, so we are testing the real path.
5. Why did `maintenanceReasons` once take ~5 hours to populate when it now reliably takes
   11–12 minutes?
6. What does `windowEndTime` actually guarantee? (See 8d: it does not bound the repair.)
