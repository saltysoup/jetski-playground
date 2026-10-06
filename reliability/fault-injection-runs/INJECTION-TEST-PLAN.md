# XID injection test — measurement plan (draft for review)

Run 2 of the failure-injection testbed. Run 1 (18 Aug 04:04Z) established that the signal
exists; this run measures **how fast each surface reacts** and captures **what a training job
looks like before / during / after**.

Status: **NOT STARTED — awaiting review.** Preconditions verified green at 2026-08-18T22:13Z.

---

## 0. Preconditions

| Check | Required state |
| --- | --- |
| Reservation `nvidia-b200-6bsoymep8ylww` | `maintenancePendingCount: 0`, `maintenanceOngoingCount: 0` |
| Block `...-block-0001` | `healthStatus: HEALTHY` (ideally — see risk R1) |
| Both nodes | `Ready`, no `node.gke.io/upcoming-maintenance` annotation |
| Ray cluster | 3 pods `2/2 Running`, 16 GPUs allocatable |
| Training job | Running and past worker init, in steady-state step loop |

If the block is still `DEGRADED` after maintenance, note it as the starting baseline rather
than blocking the run — the *delta* is what we are measuring.

**Verified 2026-08-18T22:13Z — all preconditions met.** Reservation and block `HEALTHY`, no
pending/ongoing counters, both VMs `RUNNING` with `maintenanceStatus` clear, both nodes `Ready`
with 8 allocatable GPUs (16 total), 3 Ray pods `2/2 Running`, `nvidia-smi` on the repaired node
reports 8 B200s with 0 uncorrected ECC errors, dmesg contains 0 XID lines. Risk R1 did not
materialise: the block returned to `HEALTHY`, so M3 has a clean transition to measure.

### Reference data — the clearing cycle itself (run 1 aftermath)

Triggering `performMaintenance` on the block at 17:46:12.7Z produced a rolling, not simultaneous,
disruption, and the two nodes diverged sharply by maintenance reason:

| Node | Reason | Kubelet stopped | VM back | Node downtime |
| --- | --- | --- | --- | --- |
| `-6df3` | `PLANNED_UPDATE` | 18:19:28Z | 20:59:55Z | **2h 40m 27s** |
| `-34t3` | `FAILURE_GPU_XID`, `FAILURE_GPU` | 18:00:32Z | 22:07:27Z | **4h 06m 55s** |

Gap between the two nodes entering maintenance: 18m 56s. Total block-clear wall clock:
17:46:12Z -> 22:13:04Z = **4h 26m 52s**. The `-34t3` maintenance also overran its own advertised
window (`latest` 21:46:16Z) by 21 minutes, and GKE `autoRepair` — enabled on the pool — did not
intervene during the 4 h `NotReady` period, presumably suppressed by the
`cloud.google.com/maintenance-window-started` taint.

Recovery after the VM booted was fast and fully automatic:

| Offset from VM boot | Event |
| --- | --- |
| +0s | VM `RUNNING` (22:07:27Z) |
| +1m57s | Node `Ready`, 8 GPUs allocatable |
| +2m13s | `maintenance-window-started` taint removed |
| +5m37s | Ray worker `2/2 Running`, cluster back to 16 GPUs |

So of the 4h 27m, **99% is host-side repair and 1% is Kubernetes reconvergence.** That ratio is
worth stating in the write-up — cluster-level tuning cannot buy back time that is being spent
in the hypervisor.

---

## 1. What we are measuring

Three latencies from a single injection event at `T0`:

| # | Metric | Surface | Expected order |
| --- | --- | --- | --- |
| M1 | `T0` -> XID line visible in Cloud Logging | `serialconsole.googleapis.com/serial_port_1_output` | seconds |
| M2 | `T0` -> alert fires | log-based alert (must be created first — see §2) | seconds + alert delay |
| M3 | `T0` -> reservation block reflects the failure | `reservationBlocks.healthStatus` / `maintenanceReasons` | minutes |
| M4 | `T0` -> maintenance event scheduled | `maintenance.googleapis.com/maintenance_events` | hours (run 1: 4h18m) |

Run 1 reference values: M1 <= 4.4 s, M3 <= 16 min, M4 = 4h 17m 48s, M2 never (no alert existed).

Plus the training-job impact question: **does a synthetic XID affect the running job at all?**
Run 1 suggests no — it is a log line, not a real GPU fault, so step times should be unchanged.
That is itself a finding worth stating explicitly rather than implying the demo shows a real stall.

---

## 2. Build the alert first (M2 needs it)

No log-based metrics exist in the project today, and the one alert policy
(`AI Hypercomputer Group Maintenance Alert`) watches reservation audit logs only — it did not
fire for run 1.

Create a log-based alert policy on:

```
log_id("serialconsole.googleapis.com/serial_port_1_output")
resource.type="gce_instance"
"NVRM: Xid"
```

Reuse the existing notification channel `11354446803132792422`, or a new one if you want the
page to land somewhere separate. Record the incident open time to compute M2.

---

## 3. Observability capture — three snapshots

Same set of artefacts captured at each of three moments, so they can be shown side by side.

### Snapshot A — BEFORE injection (steady state, >= 5 steps completed)

| Artefact | How |
| --- | --- |
| Step times | `train.log` via `tail-train.sh`; last 5 step durations |
| Ray dashboard | port-forward 8265; screenshot cluster + actor view |
| GPU utilisation | Cloud Monitoring `DCGM_FI_PROF_GR_ENGINE_ACTIVE`, `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_POWER_USAGE`, `DCGM_FI_DEV_GPU_TEMP` |
| Node state | `kubectl get nodes`, conditions, taints, annotations |
| Reservation | block `healthStatus`, `maintenancePendingCount`, `maintenanceReasons` |
| Logs | serial console query showing zero XID hits |

### Snapshot B — IMMEDIATELY AFTER injection (T0 -> T0+15 min)

Same six artefacts, plus:
- exact timestamp of first Cloud Logging XID entry (M1)
- alert incident open time (M2)
- first reservation poll where health/reason changes (M3)
- step-time series across the injection boundary — did the job even notice?

### Snapshot C — AFTER recovery (back to normal)

Same six artefacts. "Recovery" here needs defining — see open question Q2.

---

## 4. Execution sequence

1. Confirm preconditions (§0).
2. Create the log-based alert policy (§2). Verify it is enabled.
3. Relaunch the training job. Worker init is ~721 s; wait for steady state.
   Suggested short run: `max_num_steps: 20` so the job outlives the whole measurement window.
4. Capture Snapshot A.
5. Start two pollers before injecting:
   - serial-console log poller, 1 s cadence, records first XID hit
   - reservation block poller, 30 s cadence, records first health/reason change
6. `T0` — inject XID 79 into `/dev/kmsg` on one node, timestamped to sub-second precision.
   Use a distinct marker (`CLAUDE-SIM-RUN2`) so run 1 and run 2 are separable in logs.
7. Capture Snapshot B over the following 15 min; leave the reservation poller running for 6 h
   to catch M4.
8. Decide and execute recovery (Q2), then capture Snapshot C.
9. Write results into `Overview.md` §11 and push.

---

## 5. Risks and open questions

**R1 — the block may not return to HEALTHY.** If `FAILURE_GPU` reflects genuine hardware, the
post-maintenance state may still be degraded, so M3 would have no clean transition to measure.
Mitigation: record whatever the post-maintenance baseline is and measure the delta.

**R2 — the injection books real maintenance again.** Run 1 caused a real, disruptive event to be
scheduled. Run 2 will likely do the same, and clearing it means another perform-maintenance
cycle. Budget for that.

**R3 — timestamp precision.** Run 1's 4.4 s is an upper bound because `T0` was stamped before
the debug pod started. Fix: stamp `T0` *inside* the debug pod immediately before the write.

**Q1 — one node or both?** One node keeps a clean control for comparison. Recommend one
(`-34t3` again, for continuity with run 1).

**Q2 — what counts as "recovery"?** Options, cheapest first:
  (a) Do nothing — the synthetic XID has no live effect; "recovery" is just the log ageing out.
      Honest but weak as a demo.
  (b) Cordon + drain the node, let the job restart on the remaining node, uncordon. Shows the
      operational response to the signal. No checkpoint dependency.
  (c) Full checkpoint-restore recovery. Blocked — checkpoints are unrecoverable until
      `checkpoint_dir` points at shared storage (GCS FUSE / Lustre CSI are installed).
Recommend (b) for this run, and treat (c) as a separate exercise once shared storage is mounted.

**Q3 — do we also want the host-side GPU health metrics?** The project exposes 40
`compute.googleapis.com/instance/gpu/*` metrics (ECC counts, row remapping, NVLink errors,
`infra_health`, `failure_prediction_status`, `nccl_hang`) but **none of them carry data for
these two A4 nodes** — only one unrelated instance in the project reports them. Worth finding
out why before the write-up, since "these metrics exist but are empty on A4" is a materially
different claim from "these metrics do not exist".
