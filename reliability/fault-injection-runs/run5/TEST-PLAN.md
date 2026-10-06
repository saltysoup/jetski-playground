# Run 5 — full-workflow test plan

**Objective.** Measure the elapsed time between every stage of the detect → schedule → repair →
recover loop, on a job that checkpoints to GCS and resumes from that checkpoint.

**Stimulus.** Synthetic Xid 79 written to `/dev/kmsg` + kill of the GPU-7 compute PIDs.
Nothing is physically damaged; the node stays healthy and every step is reversible until B7.

**Starting state (verified 2026-08-19T23:0xZ).** Reservation HEALTHY (1 healthy block,
0 degraded), block `...-block-0001` HEALTHY, **neither node carries an
`upcoming-maintenance` annotation**. Every transition below is therefore a real transition,
not a re-read of a latched state — the flaw that made run 3's M3 an upper bound.

---

## Explicitly not tested

- **M2 (alert latency).** Dropped to narrative by decision. The Pub/Sub receiver stays up, so
  if a notification lands we get the timestamp free, but nothing blocks on it.
- **Real hardware faults.** No PCI remove. Consequence: the platform sees the same stimulus as
  runs 1 and 3, so this run **cannot** determine whether real faults are detected faster.
  That question stays open.

Prior runs are recorded below as **observations, not predictions**. Runs 1 and 3 are two
samples from a pipeline we do not control and have not characterised; M4 and the repair may
well come in faster this time. The gates exist to record what actually happens.
- Network / NVLink fault injection. (Also: only `eth0` and `lo` are visible inside the worker
  container despite the CR annotating eth2–eth9, so `netem` recipes would need a host pod.)

---

## Pre-flight

### P1 — checkpoint config

`examples/configs/recipes/llm/dapo-gemma3-27b-it-2n8g-fsdp2-automodel.yaml`:

```yaml
checkpointing:
  enabled: true
  checkpoint_dir: /gcs/ckpt/dapo-gemma3-27b-it-2n8g
  metric_name: null        # cadence-based saves, not best-metric
  higher_is_better: true
  save_period: 5           # checkpoint at step 5, 10, 15, ...
  keep_top_k: 3
```

All four extra keys are mandatory — `checkpoint.py` indexes them directly.

### P2 — GCS access (restarts workers)

Bucket `ikwak-reliability-ckpt` (EUROPE-WEST4, UBLA) is created;
`ikwak-reliability-gke-wl-sa@` holds `roles/storage.objectAdmin`.

Edit `raycluster.ray.io/ray-cluster-kuberay`, **head and worker groups both** (the driver runs
on the head):

```yaml
metadata.annotations:
  gke-gcsfuse/volumes: "true"
spec.serviceAccountName: workload-identity-k8s-sa
spec.volumes:
  - name: gcs-ckpt
    csi:
      driver: gcsfuse.csi.storage.gke.io
      volumeAttributes: { bucketName: ikwak-reliability-ckpt, mountOptions: "implicit-dirs" }
containers[ray-worker|ray-head].volumeMounts:
  - { name: gcs-ckpt, mountPath: /gcs/ckpt }
```

### P3 — instrumentation

- `watch-replacement.py` at **5 s** on k8s surfaces and **5 s** (not 30 s) on Compute surfaces,
  started **before** T0, under `nohup`, 24 h window.
- Surfaces polled: node ready/GPUs/taints/labels/annotations/uid; Ray pod phases; cluster
  allocatable GPUs; reservation + block `healthStatus`; per-instance `status` and
  `upcomingMaintenance.maintenanceReasons`; Ray job status.
- Timestamped `ray job logs -f` to `run5/train.log`.
- Host `kmsg` tail on `-34t3` across T0.

---

## Phase A — validate checkpoint save/restore (non-destructive, ~45 min)

| Step | Action | Pass condition |
| --- | --- | --- |
| A1 | Submit job detached (`ray job submit --no-wait`) | reaches step 1 |
| A2 | Reach step 5 | **object exists under `gs://ikwak-reliability-ckpt/`** |
| A3 | Record checkpoint **write duration** and size | recorded (a 27B FSDP2 checkpoint over FUSE may be slow — this is itself a result) |
| A4 | Kill GPU-7 compute PIDs | job crashes |
| A5 | Resubmit | **resumes at step 5, not step 0** |

If A2 or A5 fails, stop. Everything downstream is meaningless without them, and this is the
cheap place to find out.

---

## Phase B — the measured run

`T0` stamped **inside** the injecting pod, immediately before the write.

| # | Event | Prior observation | Source |
| --- | --- | --- | --- |
| B1 | Job running, past a checkpoint | — | — |
| B2 | **T0**: inject Xid 79 → `/dev/kmsg`, then kill GPU-7 PIDs (same second) | — | — |
| B3 | Entry in Cloud Logging (`serial_port_1_output`) | M1 ≈ 176 ms | run 3 |
| B4 | Job crashes (NCCL abort → Ray FAILED) | seconds | — |
| B5 | Block `healthStatus` → DEGRADED | M3 ≤ 17.5 s — tighten to a real measurement | run 3 |
| B6 | Instance `upcomingMaintenance.maintenanceReasons` = `FAILURE_GPU_XID`, `can_reschedule=true` | M4: 3 h 46 m / 4 h 18 m — **gates at T+1 m / T+15 m / T+60 m** | runs 3, 1 |
| B7 | Apply `cloud.google.com/perform-maintenance=true` | **T0′** | — |
| B8 | `active-node-maintenance` label appears | +38 s | phase 2 |
| B9 | `maintenance-window-started` taint | +56 s | phase 2 |
| B10 | SIGTERM to Ray actors | +12 m 18 s | phase 2 |
| B11 | Node NotReady, 8→0 GPUs | +12 m 31 s | phase 2 |
| B12 | VM → REPAIRING | +15 m 16 s | phase 2 |
| B13 | Block + reservation → HEALTHY | +4 h 17 m 49 s | phase 2 |
| B14 | VM RUNNING → node Ready → taint cleared → Ray 2/2 | +4 h 28 m 33 s | phase 2 |
| B15 | **Resubmit → resumes from checkpoint** | minutes | the only resubmission |

**The cluster stays down from B4 to B15, and that is deliberate.** A genuinely faulty GPU VM
would fail any job scheduled onto it, so resubmitting mid-window would be a fiction. The dead
window *is* the badput measurement: B4 → B15 is the full cost of one GPU fault, and it is the
headline number this run exists to produce.

**Gates at B6 are measurements, not waits.** Record which gate it lands in.

---

## Assertions (a run that "completes" without these proves nothing)

1. Checkpoint object visible in GCS before B2.
2. A5 and B15 both restore at the expected step, not 0.
3. Block observed HEALTHY immediately before T0 (real transition at B5).
4. Reason at B6 is `FAILURE_GPU_XID` and `can_reschedule` is `true`.
5. Label at B7 stays scoped to `-34t3`; `-6df3` unaffected despite GROUPED scheduling.
6. Node UID unchanged across repair (confirms repair-in-place, as in phase 2).

---

## Open decision — graceful termination

Currently off (`gke-disruption-handling` deleted after run 3). The job is already dead by B10,
killed at B4, so graceful termination has nothing to save in this run — its value is on a
*planned* maintenance path where the job is still running when SIGTERM lands. Leave it off for
run 5; test it separately. **Unresolved, does not block.**

## Risks

| Risk | Mitigation |
| --- | --- |
| Checkpoint config KeyError | Phase A |
| 27B checkpoint slow/large over FUSE | measured at A3; raise `save_period` if it dominates step time |
| M4 duration unknown | gates at T+1 m / T+15 m / T+60 m record it either way |
| 2 nodes, no spare — full host repair on the critical path every time | inherent to the testbed; state it in the post |

**Duration** unknown, bounded by B6 and B13. If both come in at the 15 min we were quoted,
~1 h total; if they match runs 1 and 3, ~8 h 30 m.
