# Run 6 — measured results

**T0 = 2026-08-20T18:19:46.611Z** (stamped on `-34t3`'s own clock, immediately before the
kmsg write). Target node `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3`,
instance `8713135250882265303`. Control node `-6df3`. Workload `dapo-run6c`, submitted
17:56:27.563Z, past step 6 at T0.

Source of record: `run6/timeline.log`, `run6/t0.txt`, `run6/t0-offset.txt`, `run6/train.log`.

---

## Phase B1 — detection

| Metric | Latency from T0 | Absolute UTC | Source clock |
| --- | --- | --- | --- |
| **M1** Xid emitted to Cloud Logging | **0.062 s** | 18:19:46.673 | log entry `timestamp` |
| **M1′** Xid queryable in Cloud Logging | **2.075 s** | 18:19:48.686 | log entry `receiveTimestamp` |
| Training job dead | **8.56 s** | 18:19:55.166 | Ray job `end_time` |
| **M3** reservation block `DEGRADED` | **24.5 s** (bounded 19.5–24.5) | 18:20:11.106 | poller, 5 s cadence |
| **M2** alert delivered to Pub/Sub | **135.3 s** (2 m 15.3 s) | 18:22:01.950 | message `publishTime` |
| **M2′** alert email in operator's inbox | **2 m 13 s – 3 m 13 s** | 18:22 (11:22 PDT) | recipient's mail client, minute resolution |
| **M4** `maintenanceReasons` populated | *pending* | | |

### Three tiers, confirmed

Run 3's finding holds and is now measured on one clock with a verified pre-T0 baseline:

1. **Milliseconds — the detailed signal.** 62 ms to emit the full Xid line with its PCI
   address and class number. This is the only signal that says *what* broke.
2. **Seconds — the coarse signal.** 24.5 s for the reservation block to go `DEGRADED`.
   One bit. `degradedHostCount`, `maintenancePendingCount` and `maintenanceOngoingCount`
   were all `null` — it tells you *that* something is wrong and nothing else.
3. **Minutes — the notification.** 2 m 15 s for the log-based alert to reach Pub/Sub.

The ordering is the argument for log-based alerting: the signal that arrives first is the
one that tells you the most, and it costs a log sink and an alert policy to get at it.

### Findings worth carrying into the post

**The reservation follows the block immediately.** `res_health` and `block_health` both
flipped to `DEGRADED` in the same 5 s poll. Prior runs only tracked the block; watching the
reservation is equivalent and is one API call shallower.

**`receiveTimestamp` is the honest M1 number.** Run 3 reported 176 ms from the serial
console's clock alone. Emitting is 62 ms; being *queryable* — which is what any alert,
sink or dashboard actually depends on — is 2.075 s, 33× longer. Quote 2 s, not 62 ms.

**The job died on actor RPC, not NCCL timeout.** `ActorUnavailableError: ... RpcError:
Socket closed rpc_code: 14`, then raylet `Worker exit type: SYSTEM_ERROR ... connection
error code 2`. 8.56 s from GPU-process kill to Ray marking the job FAILED. No NCCL
collective timeout was reached — the process death propagated faster than any timeout
would have fired. Worth stating, because NCCL timeouts are what people expect to see and
tune, and here they were irrelevant.

**M2 measured at last.** Three prior runs missed it for three different reasons (no policy,
no policy, policy created 15 h after the only injection). 2 m 15 s is a real number for a
log-based alert policy on a `"NVRM: Xid"` text match.

**Email costs nothing extra over Pub/Sub.** The email landed in the same minute as the
Pub/Sub publish. Both channels fan out in parallel from the single policy, so the
operator-experienced latency is essentially the policy's own evaluation latency — mail
delivery adds ~0–60 s, not a separate multi-minute leg. This matters for the post: you do
not have to choose a machine channel over a human one to get a fast alert, and the
~2 min figure is dominated by log-based alert evaluation, not by the delivery mechanism.

---

## Phase B2 — repair

*Pending B5.*

---

## Assertions

| # | Assertion | Status |
| --- | --- | --- |
| 1 | Block observed `HEALTHY` before T0 | **PASS** — 18:17:03.845Z, 2 m 43 s pre-T0, `res_health` and `block_health` both HEALTHY, `34t3_maint: CLEAR` |
| 2 | B5 reason is `FAILURE_GPU_XID`, `canReschedule` true | pending |
| 3 | Label at B7 stays scoped to `-34t3` | pending |
| 4 | Node UID and instance id unchanged across repair | pending — pre-repair `uid=11db4e2c`, `instance_id=8713135250882265303` |
| 5 | Post-recovery step time within noise of ~2 m 05 s | pending |
