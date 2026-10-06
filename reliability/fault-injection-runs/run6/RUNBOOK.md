# Run 6 runbook — exact commands, in order

Everything below is staged and pre-flighted. Nothing has been fired.

`gcloud`'s user credential is dead (Context Aware Access), but **application-default
credentials still work**, and every Cloud surface here is reached over REST with an ADC
token. The run is not blocked on `gcloud auth login`. `kubectl` is unaffected.

---

## Already done

| | |
| --- | --- |
| `run6/dapo-...yaml` | recipe with `checkpointing.enabled: false` |
| `prep-pods.sh` | repointed to run6; **run and verified on all 3 pods** |
| `run6/preflight.py` | all surfaces green (below) |
| `run6/watch.py` | merged poller — M1, M2, M3, M4 and the repair timeline in one clock |
| `run6/inject-pod.yaml` | privileged pod on `-34t3`, **created and Ready**, sleeping |
| `run6/inject.sh` | the stimulus; refuses to fire without `--go` |
| `submit_gemma3-27b-it.sh` | submission-id file repointed to run6 |
| `run6/alerting.json` | policy + both notification channel ids |

**Alerting now has two channels on one policy** (`14253142552209113916`), not two policies —
a duplicate policy on the same condition would double-fire on every Xid:

- **pubsub** → `xid-alerts` — the measurable one. `publishTime` is a server clock, so M2 is
  correct even when the poll is late.
- **email** → `ikwak@google.com` — the one a human reacts to. Channel
  `9977523644883873718`, created 20 Aug 16:52 UTC.

`notificationRateLimit` is 300 s, which delays a *repeat* alert, not the first — so it does
not affect M2. `autoClose` is 1800 s.

`prep-pods.sh` now asserts `checkpointing.enabled == false` rather than grepping the
checkpoint dir. The run5 and run6 configs differ *only* on that key, so the old check would
have passed on a stale copy and silently turned checkpointing back on.

### Pre-flight result

```
reservation healthStatus                     HEALTHY
block ...-block-0001 healthStatus            HEALTHY      <- assertion 1 satisfied
instance 34t3 / 6df3                         RUNNING, no upcomingMaintenance
alert policy 14253142552209113916            enabled, condition "NVRM: Xid", -> pubsub
notification channel 1117176896799437752     enabled
topic xid-alerts / subscription xid-alerts-sub  exist, drained (0 stale messages)
```

### Verified against the cluster, not assumed

- GPU 7 on `-34t3` is PCI `0000:CC:00.0`, so the injected message carries a real address.
- `-34t3` instance id is still `8713135250882265303`, unchanged since run 3 — the run 4
  repair preserved the instance, and the M1 log filter is still keyed correctly.
- `lastStartTimestamp` on `-34t3` is 2026-08-19T20:17Z, consistent with the run 4 recovery.
- The injection pod can write `/dev/kmsg` and can see host PIDs (2680 visible).
- Host `nvidia-smi` is reachable at `/home/kubernetes/bin/nvidia/bin/nvidia-smi` via
  `nsenter -t 1 -m -p`, so GPU-7 PIDs can be resolved and killed.

### Two things the pre-flight changed about the measurement

**The right log filter is run 3's, and it is not the obvious one.** Xid lines land under
`log_id("serialconsole.googleapis.com/serial_port_1_output")`, *not* under
`compute.googleapis.com/serial_port_1_output`. `watch.py` uses run 3's proven filter,
which is also the one the alert policy matches on.

**Cloud Logging reads in this project are being 429'd** — quota exceeded on
`Read requests per minute`, from something other than us. That would have made M1 a poll
artefact. It doesn't, because `watch.py` reports M1 from the log entry's **own**
`timestamp` and `receiveTimestamp`, and M2 from the Pub/Sub message's **publishTime**.
Those are server clocks: a poll that succeeds late still yields the correct latency.
Reporting both `timestamp` and `receiveTimestamp` also fixes a soft spot in run 3's 176 ms,
which was the serial console's clock alone.

---

## Sequence

### 1. Get the job running (~10 min)

No baseline phase — this is a precondition, not a measurement. The job only has to be past
step 1 so that something is bound to GPU 7 when the injector fires.

```bash
cd /home/user/reliability-demo
bash prep-pods.sh                                    # idempotent; re-run is free
bash submit_gemma3-27b-it.sh dapo-run6

HEAD=$(kubectl get pods -l ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}')
nohup kubectl exec $HEAD -c ray-head -- ray job logs -f dapo-run6 \
  > run6/train.log 2>&1 &
```

Confirm before going further — if this is empty, the kill at T0 is a no-op:

```bash
kubectl exec xid-inject-run6 -- nsenter -t 1 -m -p -- \
  /home/kubernetes/bin/nvidia/bin/nvidia-smi \
  --query-compute-apps=pid,gpu_bus_id --format=csv,noheader | grep CC:00
```

### 2. Start the poller BEFORE the injection

The poller needs T0, and T0 comes from the injector — so start it with the *intended*
T0 (now, rounded), then correct the file afterwards. Its first tick records the pre-T0
cloud state, which is what makes B4 a transition rather than a re-read.

```bash
cd /home/user/reliability-demo/run6
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SUBMISSION=dapo-run6 nohup python3 watch.py "$T0" 720 > watch.out 2>&1 &
sleep 20 && tail -5 timeline.log      # confirm START + a CHANGE-CLOUD baseline line
```

### 3. T0 — the stimulus

```bash
bash /home/user/reliability-demo/run6/inject.sh --go
```

Prints and records the resolved GPU-7 PIDs, the host-clock T0, `KMSG_WROTE_OK`, and the
kill. Writes `run6/t0.txt`. **Reconcile that against the T0 the poller was started with**;
if they differ by more than a few seconds, restart the poller with the real value — the
log is a plain append, so nothing is lost.

### 4. Watch

```bash
tail -f /home/user/reliability-demo/run6/timeline.log
```

Expect, in order: `M1-LOG` (~sub-second), job death (~17 s), `M2-ALERT` (**first ever
measurement**), `CHANGE-CLOUD` with `block_health: DEGRADED`, then `34t3_reasons:
["FAILURE_GPU_XID"]` at B5 — anywhere from minutes to ~4 h.

### 5. B7 — pull the repair forward

Only once B5 shows a reason and `canReschedule: true`:

```bash
kubectl label node gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3 \
  cloud.google.com/perform-maintenance=true --overwrite
date -u +%Y-%m-%dT%H:%M:%SZ > /home/user/reliability-demo/run6/t0-prime.txt
```

The poller keeps running through the repair; run 4 took 4 h 28 m from here to a 2/2 Ray
cluster.

### 6. B14 — recover

```bash
cd /home/user/reliability-demo
bash prep-pods.sh                                    # NOT optional
bash submit_gemma3-27b-it.sh dapo-run6-recovered
```

The worker pod on the repaired node comes back from the image, and the image has never
contained this recipe. Skipping `prep-pods.sh` here fails the resubmission — this is the
single most likely way the run gets thrown away at the last step.

Record 5 steps and compare against ~2 m 05 s (assertion 5).

---

## Cost and duration

Two a4-highgpu-8g at $90.22/node-hour = **$180.44/hour**, billed whether or not the GPUs
are doing anything — which is the point being measured.

| | Optimistic (B5 ≈ 15 min) | Prior observations (B5 ≈ 4 h) |
| --- | --- | --- |
| Get the job running | 10 min | 10 min |
| T0 → B5 (maintenance reason appears) | 15 min | ~3 h 46 m |
| B7 → B13 (label → cluster whole) | 4 h 30 m | 4 h 30 m |
| Recovery to baseline | 25 min | 25 min |
| **Total** | **~5 h 20 m — $965** | **~8 h 50 m — $1,595** |

B5 is the variable, and it is the one number nobody has been able to predict across three
runs. The cluster is unusable from job death until B14 either way; that dead window *is*
the headline badput measurement.

## Abort

Before B7, everything is reversible — the Xid is a log line and the killed job can simply
be resubmitted. **After B7 there is no abort**: the label commits a real host repair with
no spare capacity to fall back on.
