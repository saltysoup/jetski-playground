# Run 3 — synthetic XID 79 injection: measured results

Testbed: GKE cluster `ikwak-reliability`, europe-west4, 2 x a4-highgpu-8g (16 x B200).
Target node: `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3` (instance ID
`8713135250882265303`). Reservation `nvidia-b200-6bsoymep8ylww`, single block.

**T0 = 2026-08-19T00:45:12Z**, marker `CLAUDE-SIM-RUN3`, injected via privileged debug pod:

```bash
echo "NVRM: Xid (PCI:0000:cc:00): 79, pid=0, name=CLAUDE-SIM-RUN3, GPU has fallen off the bus." \
  | tee /dev/kmsg
```

The GPU was healthy throughout. This measures the **detection and response pipeline**, not a
hardware fault.

---

## Headline: the four latencies

| # | Surface | Latency from T0 | Quality of the number |
| --- | --- | --- | --- |
| M1 | Serial console -> Cloud Logging | **176 ms** | Measured. Log entry timestamp 00:45:12.176226Z |
| M2 | Log-based alert fires | **not measured** | No log-based metric or alert policy exists in the project |
| M3 | Reservation block `healthStatus` -> DEGRADED | **<= 17.5 s** | Upper bound only — see caveat below |
| M4 | Maintenance event scheduled, reason visible | **3 h 46 m 10 s** | Measured indirectly — see below |

Run 1 (2026-08-18T04:04Z) reference for the same injection: M1 <= 4.4 s (upper bound, T0 was
stamped outside the pod), M3 <= 16 min, M4 = 4 h 17 m 48 s, M2 never.

### M3 is an upper bound, not a measurement

`poll.py` checks the Compute surfaces every 30 s and the poller only reached its first cloud
poll at T+17.5 s. The block was *already* `DEGRADED` at that first observation, so the true
latency is somewhere in `(0, 17.5] s`. It is consistent with the claim that block health is
driven directly off the Cloud Logging entry, but it does not prove it. **Run 4 should poll the
block at 5 s cadence starting before T0** to tighten this.

### M4 was recovered after the fact

The poller died when the session terminated at ~04:03Z, before the event was scheduled, so M4
was not observed live. It was reconstructed from the Kubernetes node object:

```
kubectl get node ...-34t3 -o json --show-managed-fields
  -> managedFields: manager "gpu-maintenance-handler", Update, 2026-08-19T04:31:22Z
     (the entry owning annotation node.gke.io/upcoming-maintenance)
```

T0 00:45:12Z -> 04:31:22Z = **3 h 46 m 10 s** to the annotation landing on the node. This is the
GKE-side surface; run 1's 4 h 17 m 48 s was measured at the Compute instance layer, so the two
are not strictly comparable. Both are "roughly four hours".

---

## The three-tier finding

The fast signal is the detailed one; the slow signal is the actionable one.

| Tier | Latency | What it tells you |
| --- | --- | --- |
| Serial console -> Cloud Logging | 176 ms | Exactly what happened: full XID text, PCI address |
| Reservation block health | <= 17.5 s | *That* something is wrong. One bit, nothing more |
| Instance / node maintenance reason | ~3 h 46 m | *Why*: `FAILURE_GPU_XID`, and a rescheduleable event |

If you want to act within seconds, the log is your only option. That is the argument for a
log-based alert, and it is why M2 is the most important gap in this data set.

### The reservation block really does carry only one bit

Full health payload observed on the block during run 3:

```json
"healthInfo": { "healthStatus": "DEGRADED" }
```

No reason code, no XID text, no error detail. `reservationSubBlocks` returned empty. The
`healthyCount` / `degradedCount` fields seen in run 1 were absent this time. Throughout the
observation window the instance's `resourceStatus` had **no `upcomingMaintenance` object at
all** — that object, and with it `maintenanceReasons`, only materialises once Compute Engine
has scheduled a maintenance event.

The instance does expose `physicalHostTopology` (`cluster` / `block` / `subblock` / `host`),
which is how you would correlate repeat failures to a specific physical host.

---

## The scheduled event (state as of 2026-08-19T14:05Z)

Annotation `node.gke.io/upcoming-maintenance` on `-34t3`:

```json
{
  "maintenance_reason":  ["FAILURE_GPU_XID", "FAILURE_GPU"],
  "maintenance_status":  "PENDING",
  "type":                "UNSCHEDULED",
  "can_reschedule":      "true",
  "window_start_time":   "2026-08-26T08:00:00+00:00",
  "window_end_time":     "2026-08-26T12:00:00+00:00"
}
```

`can_reschedule: "true"` is the prerequisite for the
`cloud.google.com/perform-maintenance=true` label workflow. The natural window is a week out;
the label is what pulls it forward.

The clean node `-6df3` carries no such annotation — only
`node.gke.io/machine-termination-datetime: 2026-12-16T01:00:00Z`, which is unrelated.

---

## Did the training job notice? No.

NeMo-RL DAPO/GRPO on Gemma 3 27B IT, 2 nodes / 16 GPUs, ran **50 steps straight through** the
injection (Ray job `05000000`, 2026-08-18T23:45:53Z -> 2026-08-19T01:33:08Z, 107 min). The XID
was injected at step ~26. No step-time perturbation, no NCCL error, no restart.

This is the correct result and it must be stated plainly in the post: **a synthetic XID
exercises the platform's detection path while the workload sails through.** It proves your
alerting and node-replacement plumbing works. It does not demonstrate recovery, because nothing
broke. Demonstrating recovery requires a real fault (planned: PCI-remove a GPU).

---

## Cluster configuration findings

### Graceful termination was off by default

Server 1.35.6-gke.1641000 / nodes 1.35.6-gke.1250000. No `gke-disruption-handling` ConfigMap
existed. Applying one was picked up **live by both node handlers with no restart**:

```
34t3  03:41:03  config loaded: &{GracefulTermination:true ...}   Old config: <nil>
6df3  03:41:31  config loaded: &{GracefulTermination:true ...}   Old config: <nil>
```

`Old config: <nil>` confirms graceful termination was genuinely off, not merely undocumented.
The ConfigMap was **deleted again at ~03:46Z** so that the first replacement measures the
ungraceful baseline. Both handlers confirmed back to `New config: <nil>`.

### The maintenance-handler DaemonSet

Present in `kube-system` on both GPU nodes. Relevant args:

```
--taint=cloud.google.com/impending-node-termination::NoSchedule
--regular-vm-timeout=1h0m0s
--immediate-shutdown=true
```

So the graceful drain window for a GPU VM is **one hour** — ample for a checkpoint flush, once
checkpointing writes somewhere that survives the node.

### Checkpointing is currently disabled

In `examples/configs/recipes/llm/dapo-gemma3-27b-it-2n8g-fsdp2-automodel.yaml`:

```yaml
checkpointing:
  enabled: false
  checkpoint_dir: results/dapo-gemma3-27b-it-2n8g    # node-local
```

Any recovery measured today restarts from step 0. Enabling this against GCS FUSE is a
prerequisite for the real-fault test and for any defensible goodput arithmetic.

### Jobs must be submitted detached

`submit_gemma3-27b-it.sh` runs `python3 run_grpo.py` in the foreground of a `kubectl exec`.
When the driving session ended at ~04:03Z the driver was killed; Ray marked job `06000000`
SUCCEEDED at 04:14:18Z after liveness lapsed, 25 min into what was configured as a 500-step
run. Ray retains no logs for driver-type jobs, so the ending is unrecoverable. Use
`ray job submit --no-wait` for anything that must outlive a 4-hour replacement.

---

## Reference: run 1 aftermath (the clearing cycle)

`performMaintenance` on the block at 2026-08-18T17:46:12.7Z produced a rolling, not
simultaneous, disruption. The two nodes diverged sharply by maintenance reason:

| Node | Reason | Kubelet stopped | VM back | Node downtime |
| --- | --- | --- | --- | --- |
| `-6df3` | `PLANNED_UPDATE` | 18:19:28Z | 20:59:55Z | **2 h 40 m 27 s** |
| `-34t3` | `FAILURE_GPU_XID`, `FAILURE_GPU` | 18:00:32Z | 22:07:27Z | **4 h 06 m 55 s** |

Gap between nodes entering maintenance: 18 m 56 s. Total block-clear wall clock 4 h 26 m 52 s.
The `-34t3` maintenance overran its advertised `latest` window (21:46:16Z) by 21 minutes. GKE
`autoRepair`, enabled on the pool, did not intervene during the 4 h `NotReady` period —
presumably suppressed by the `cloud.google.com/maintenance-window-started` taint.

Recovery once the VM booted was fast and fully automatic:

| Offset from VM boot | Event |
| --- | --- |
| +0 s | VM `RUNNING` (22:07:27Z) |
| +1 m 57 s | Node `Ready`, 8 GPUs allocatable |
| +2 m 13 s | `maintenance-window-started` taint removed |
| +5 m 37 s | Ray worker `2/2 Running`, cluster back to 16 GPUs |

**99% of the 4 h 27 m is host-side repair; 1% is Kubernetes reconvergence.** Cluster-level
tuning cannot buy back time being spent in the hypervisor. Note also that with only two GPU
nodes there is no spare capacity to fail over to, so every MTTR measured on this testbed
includes the full host repair.

---

## Open items carried into run 4

1. **M2 has never been measured.** Create a log-based metric + alert policy on
   `log_id("serialconsole.googleapis.com/serial_port_1_output") "NVRM: Xid"` before injecting.
2. **Tighten M3** with 5 s cadence starting before T0, and confirm the block reads `HEALTHY`
   pre-injection so there is a real transition to time.
3. **Stamp T0 inside the debug pod** immediately before the write (run 1's 4.4 s was inflated
   by pod startup; run 3 fixed this and got 176 ms).
4. **Run the poller under `nohup` with a 24 h window** so a dead session cannot blind it again.
   This is what cost run 3 its live M4.
5. **Host-side GPU metrics remain empty.** The project exposes 40
   `compute.googleapis.com/instance/gpu/*` metrics (ECC counts, row remapping, NVLink errors,
   `infra_health`, `failure_prediction_status`, `nccl_hang`) but none carry data for these two
   A4 nodes. "These metrics exist but are empty on A4" is a materially different claim from
   "these metrics do not exist" — worth resolving before publishing.
