# Run 9 — result: two Xid 63s are not enough

**Closed early 2026-09-24 06:19Z, before B7. No maintenance event raised, no label applied,
no repair booked.** Superseded by runs 10/11 (`run10/TEST-PLAN.md`).

Target `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3`, instance `174490015860863227`,
GPU 7 (`0000:cc:00.0`). Workload `dapo-run9` running throughout.

## What was done

Two synthetic Xid 63 row-remapper lines, **no GPU reset between them** — the single design
change that separates run 9 from 8f.

| | UTC | Row address |
| --- | --- | --- |
| Baseline (poller) | 05:31:03.146 | res HEALTHY, block HEALTHY, node Ready, 8 GPUs, cluster 16, no taints, no label |
| Pre-injection GPU health | 05:28:39.816 | all 8 GPUs: driver 580.159.04, persistence Enabled, ECC uncorrected 0, remapped_rows.uncorrectable 0, pending No, failure No |
| i1 | 06:01:53.613 | `0x0000001026baac40` |
| i2 | 06:03:31.289 | `0x0000001026babc40` (Δ 97.7 s) |

Poller at 5 s cadence, supervised, plus an independent 60 s instance probe.

## The result

**Zero CHANGE lines for the full 15-minute observation window** (i2 → 06:18:31Z), and none by
06:19:37Z when the run was closed. Heartbeats at 06:07:07, 06:12:15 and 06:17:22 all read:

```
res=HEALTHY block=HEALTHY 34t3=CLEAR vm=RUNNING ready=True gpus=8
```

No reservation degradation, no block degradation, no `upcomingMaintenance`, no node taint, no
change in allocatable GPUs, and the training job kept stepping.

## What it means

8f degraded **1 m 54 s after its second injection**. Run 9, same node, same GPU, same message
format, same injection spacing, no reset — **nothing at 15 minutes**, at least 8× longer than
8f took. The one removed variable was the GPU reset.

**Therefore: 8f's degradation on the second Xid 63 was reset-assisted.** The forced
`nvidia-smi --gpu-reset` — its driver unload/reload — feeds the same detector that counts
Xids. Two Xid 63s on their own are not sufficient.

Corollary: the GCE engineer's **"three consecutive Xid 63s" rule holds as stated.** 8d
degraded on the third (+16.6 s) and that is now the clean, unassisted measurement of the
threshold. 8f's apparent two-Xid trigger was an artefact of our own instrumentation.

## Also checked — a claim from the ClusterMAX article

That article reports that after XID injection GKE marks the node `GPUUnhealthy=True` and sets
`cloud.google.com/health-check-status=warning`. Checked live on `-34t3` at 06:11:04Z:

- **No `GPUUnhealthy` node condition exists** — not `False`, absent. The node carries the
  standard Node Problem Detector set (`CperHardwareErrorFatal`, `KernelDeadlock`,
  `XfsShutdown`, `ReadOnlyRootFileSystem`, …), all `False`, `Ready=True`.
- **No `cloud.google.com/health-check-status` label.** The only GPU labels are
  `cloud.google.com/gke-gpu=true` and `cloud.google.com/gke-gpu-driver-version=default`.
- No relevant annotations. Only taint is the standard `nvidia.com/gpu=present:NoSchedule`.

So the node-level GKE health-check layer they describe is **not present on this cluster**,
and every measurement in runs 3–9 is of a different system: the GCE reservation/block health
and instance `upcomingMaintenance` API. Whether that layer is a nodepool feature we have not
enabled, or is gated on Xid class, is an open question for the engineer.

## Why run 9 was closed before B7

Xid 63 needs three injections and ~12 minutes to raise an event. Xid 79 needed **one** and
35.6 s to DEGRADED in run 7, and it is on the list Google proactively repairs. With the
threshold question now answered, there was no reason to spend the remaining two injections
and a repair cycle on the slower stimulus. Runs 10/11 move to Xid 79 — synthetic first as a
controlled replication of run 7, then a genuine driver-emitted one via PCIe secondary bus
reset.

## Artifacts

`timeline-9.log` (poller, 5 s, supervised), `instance-9.jsonl` (60 s independent probe),
`inject63-i{1,2}.txt`, `t63-i{1,2}.txt`, `pre-gpu-health.txt`, `train.log`, `TEST-PLAN.md`.
