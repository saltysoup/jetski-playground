# Status, decisions and open items

Last updated 2026-10-05 (redesigned dashboard, measured live; recovery after a GKE auto-upgrade. On 2026-10-04: user guide. On 2026-10-03: three llm-d stages, fleet idle rate 80%, the same load in every stage). The [README](./README.md) has the measured results and the stage runbook; the [user guide](./USER_GUIDE.md) builds, redeploys, updates and tears down the stack. The engineering details are in [implementation.md](./implementation.md).

## Status

The demo works end to end on the live clusters. The build steps were re-verified one by one on 2026-09-26 ([README §10](./README.md#10-how-this-guide-was-verified)) and re-checked on 2026-10-04, and the recovery recipes ran for real on 2026-10-05 ([user guide §7](./USER_GUIDE.md#7-how-this-guide-was-verified)).

| What | Result (ground truth: ate-api states and sandbox processes on the nodes) |
|---|---|
| Wake 1,000 agents | Median 2,988 ms over 10 wakes (2,902–3,286 ms). 4 of the 10 took longer than 3.0 s. 0 wake failures. |
| Simulate Traffic | Fleet idle rate about 91%. 998–1,000 distinct agents complete a wake → LLM call → pause cycle per minute. 0 failed LLM requests. |
| Suspend all | 0.60–0.74 s from Balanced; 2.4–2.6 s with all 1,000 up; 1.3–3.8 s from Priority. Always ends at 1,000 PAUSED and 0 sandboxes. |
| llm-d | Balanced (now labelled Default 50:50) about 53/47. Steer 80/20 reached 80/20 within seconds (button removed from the dashboard on 2026-10-02; the driver API still accepts it). About 90% of prompt tokens come from the prefix cache. |
| Hermes Agent variant (2026-09-28, [hermes/](./hermes/README.md)) | 1,000 Hermes agents on 18 × c4d-standard-16:<br>• wake in 2,444–2,854 ms;<br>• Simulate Traffic at 90.4–91.0% idle, 14,681 turns with 0 failed;<br>• 14,679 of 14,681 codename recalls correct after up to 28 suspends;<br>• Suspend all in 1,441 ms. |
| Move to C4 (2026-09-28) | Light workers: 25 × c3-standard-4 → 25 × c4-standard-4. TPU cluster CPU node: e2-standard-4 → c4-standard-4. Control-plane pool stays on N2 (C4 can't attach the pd-balanced Postgres volume).<br>• Light wake 1,907–1,953 ms (was median 2,988 ms on C3), 0 failures;<br>• 40 s of traffic: 6,993 requests, 0 failed;<br>• Hermes re-check: wakes 2,500–2,923 ms, 1,702 replies with 0 failed, all recalls correct. |
| Three llm-d stages at the same load (2026-10-03, [README §2](./README.md#2-results)) | About 200 agents active (`-fleet-idle-pct` 80; was 90) and the same 200 req/s offered in every stage (Stage 3 added 200 req/s until the evening; `-overload-rate` now adds load to every stage alike). Served about 65 / 103–109 / 110–113 req/s in Stages 1–3; each request waited 2.4–2.6 s / 1.3–1.4 s / 1.25–1.3 s from agent to reply. The pool (32 in flight per pod behind llm-d) is the limit. Stage 3 queue wait: Paid Members 40–84 ms, Free Users 1.6–2.3 s. Three runs, 31,905 requests, 0 failed. (With 400 req/s in Stage 3 that afternoon: 114–115 served, Free Users 3.7–4.5 s.) |
| New dashboard, measured live (2026-10-05, [README §2](./README.md#2-results)) | Each stage side by side with the one before it, same 200 req/s. Without → with llm-d: 1,619 → 3,174 output tok/s (2.0×), agent E2E 2,557 → 1,057 ms (2.4× lower), KV-cache hit 57 → 97%. TTFT at the gateway 475 → 547 ms (1.2× higher; the wait moves from vLLM into llm-d's queue). With flow control: Paid Members (Pro) queue wait 481 → 44 ms, Regular / Free tier 1,340 ms. 8,673 requests, all answered. |
| GKE auto-upgrade (2026-10-04) | Recreated every node of both clusters: driver pod gone, `postgres-0` unschedulable, every agent's snapshot lost. Recovered on 2026-10-05 with [user guide §4.8](./USER_GUIDE.md#48-gke-upgraded-the-cluster-every-node-recreated); all 1,000 agents of both fleets re-created. |

**Hermes open item:** the first suspend and the first wake after a teach are slow (13–29 s and 6.3–6.7 s). The pre-show steps include one warm-up cycle to absorb this ([hermes §5](./hermes/README.md#5-before-the-show)). The root cause was not found: it is not dirty-page writeback, and one slow-suspend node showed 40% IO stall.

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

1. **Node auto-upgrade is on for every node pool in both clusters, with no maintenance exclusion.** Recreating a Substrate node destroys the node-local snapshots of the agents paused on it, and awake agents on it end up `CRASHED`. It already happened once: the 2026-10-04 upgrade recreated every node (Status above). Recommended before the show: `--no-enable-autoupgrade` on `substrate-c4-pool`, `hermes-c4d-pool` and `keynote-driver-pool`, plus a maintenance exclusion on both clusters that covers the show.
2. **Done 2026-10-03: Priority mode now queues on the live pool.** The flow-control saturation detector allows 32 in-flight requests per pod instead of 256, so Stage 3 saturates the pool and the tiers separate (README §2). At 80% fleet idle that same limit caps served traffic at about 110–115 req/s, and Stage 2 queues too. Admitting more per pod (for example 48–64) would raise throughput somewhat, at higher per-request latency; not tested on the pool.
3. **Wake-time margin.** The tail is set by uneven placement: 32–47 agents per node, and nodes with 40 or fewer finish by about 2.45 s. Rebalancing to about 40 per node would likely bring the wake to about 2.4–2.5 s. That estimate comes from the less-loaded nodes; it was not tested.
4. **Safety net for an unrestorable snapshot.** It happened once in about 13,000 pause/restore cycles, and it makes "Wake 1,000" stop at 999. The driver could re-create an agent whose restore fails twice, which would cost about 4 s instead of a stuck wake. Today the fix is the manual repair in README §7.
5. **Postgres data exposure.** `http_srv` in `postgres-0` serves the whole Postgres data directory on the pod network. Serve a separate directory that holds only the binaries, or bake the binaries into images.
6. **Spot TPU nodes can be preempted.** A vLLM pod then takes minutes to come back, and the driver needs the new pod IPs (`deploy-driver.sh`, [user guide §4.1](./USER_GUIDE.md#41-vllm-or-epp-pods-restarted)). Consider on-demand or reserved TPU capacity for the show day.
7. **After the show:**
   - delete `ate-node-tuner` and recreate the Substrate nodes, because its mount and sysctl changes persist;
   - restore Postgres `fsync` and `full_page_writes`;
   - or delete the clusters ([user guide §6](./USER_GUIDE.md#6-tear-it-down)).
8. **The driver pod is a bare Pod with an `emptyDir`.** If its node is drained, upgraded or repaired, nothing re-creates it, and `/work` is lost: the driver binaries, their flags, the dashboard page, the run records and the Hermes driver's files. `deploy-driver.sh` brings the light driver back in one run; the Hermes driver needs its key, binary and start again, and its agents must be taught again ([user guide §4.2](./USER_GUIDE.md#42-driver-pod-deleted)). A Deployment with a persistent volume would avoid this.
