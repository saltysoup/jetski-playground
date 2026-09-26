# Status, decisions and open items

Last updated 2026-09-26. The [README](./README.md) has the guide and the measured results. The engineering details are in [implementation.md](./implementation.md).

## Status

The demo works end to end on the live clusters, and the guide was re-verified step by step on 2026-09-26 ([README §10](./README.md#10-how-this-guide-was-verified)).

| What | Result (ground truth: ate-api states and sandbox processes on the nodes) |
|---|---|
| Wake 1,000 agents | Median 2,988 ms over 10 wakes (2,902–3,286 ms). 4 of the 10 took longer than 3.0 s. 0 wake failures. |
| Simulate Traffic | Fleet idle rate about 91%. 998–1,000 distinct agents complete a wake → LLM call → pause cycle per minute. 0 failed LLM requests. |
| Suspend all | 0.60–0.74 s from Balanced; 2.4–2.6 s with all 1,000 up; 1.3–3.8 s from Priority. Always ends at 1,000 PAUSED and 0 sandboxes. |
| llm-d | Balanced about 53/47. Steer 80/20 reaches 80/20 within seconds. About 90% of prompt tokens come from the prefix cache. |

## Decisions

| Decision | Why |
|---|---|
| Keep atelet `restoreSem` at 12 | 16 was tested (3 runs each, 0 failures) and was not faster. |
| Agents rest with `PauseActor` (node-local snapshot), not `SuspendActor` | Restoring from local disk is what makes a 1,000-agent wake about 3 s. The cost: an agent is pinned to its node, and loses its snapshot if the node is recreated. |
| Synchronous `runsc_fast` wrapper; the asynchronous one is abandoned | The async wrapper returned before the sandbox existed. Its 1.83 s wake was not real, and it orphaned 999 sandboxes (implementation.md §6). |
| No "skip rootfs setup" change in atelet's `oci.go` | An earlier plan proposed it. Its premise was wrong (ateom composes the rootfs itself), and the proposed code did not compile. |
| Exactly one ate-api replica | The patched ate-api caches templates and worker capacity in memory, per process. |
| An in-cluster Envoy instead of a GKE Gateway | The Gateway's load balancer needs health checks from Google ranges, which an automated firewall policy in the project strips; it returned intermittent 503s. |
| llm-d `active-request-scorer` instead of `queue-scorer` | `queue-scorer` reads a lagging vLLM gauge. In a 1,000-request burst it sent about 750 requests in a row to one pod (86.5/13.5). |
| Pre-show health check as a manual procedure ([README §7](./README.md#7-pre-show-health-check)) | Chosen over an automated script. |

## Open items

None of these are implemented. Each needs an owner's decision.

1. **Node auto-upgrade is on for every node pool in both clusters, with no maintenance exclusion.** Recreating a Substrate node destroys the node-local snapshots of the agents paused on it, and awake agents on it end up `CRASHED`. Recommended before the show: `--no-enable-autoupgrade` on `substrate-node-pool` and `keynote-driver-pool`, plus a maintenance exclusion that covers the show.
2. **Priority mode shows no queueing on the live pool.** The flow-control saturation detector allows 256 in-flight requests per pod, and 300 req/s never reaches that, so every band waits the same few milliseconds. An earlier llm-d-only test with a limit of 16 per pod showed premium 0.40 s against best-effort 1.26 s mean latency. Options:
   - lower the per-pod limit for the demo (an llm-d setting; vLLM is unchanged);
   - or present Priority as "bands configured; queues form only under saturation".
3. **Wake-time margin.** The tail is set by uneven placement: 32–47 agents per node, and nodes with 40 or fewer finish by about 2.45 s. Rebalancing to about 40 per node would likely bring the wake to about 2.4–2.5 s. That estimate comes from the less-loaded nodes; it was not tested.
4. **Safety net for an unrestorable snapshot.** It happened once in about 13,000 pause/restore cycles, and it makes "Wake 1,000" stop at 999. The driver could re-create an agent whose restore fails twice, which would cost about 4 s instead of a stuck wake. Today the fix is the manual repair in README §7.
5. **Postgres data exposure.** `http_srv` in `postgres-0` serves the whole Postgres data directory on the pod network. Serve a separate directory that holds only the binaries, or bake the binaries into images.
6. **Spot TPU nodes can be preempted.** A vLLM pod then takes minutes to come back, and the driver needs the new pod IPs (README §6.10). Consider on-demand or reserved TPU capacity for the show day.
7. **After the show:**
   - delete `ate-node-tuner` and recreate the Substrate nodes, because its mount and sysctl changes persist;
   - restore Postgres `fsync` and `full_page_writes`;
   - or delete the clusters (README §11).
