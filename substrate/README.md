# Agent Substrate × llm-d: 1,000 agents on GKE calling Gemma 4 12B on Cloud TPU v6e

A keynote demo:
- **Agent Substrate** wakes 1,000 sandboxed agents from zero in about 2 seconds on GKE (C4 nodes; about 3 s on the original C3 nodes).
- The agents call **Gemma 4 12B**, served by vLLM on **Cloud TPU v6e** behind the **llm-d** router.
- The stage dashboard shows both sides live and walks the llm-d side through three stages, each measured side by side against the one before it: **without llm-d** (plain round robin at the gateway), **with llm-d** (KV-cache-aware routing) and **with flow control** (paid tiers first when the pool is saturated).

This folder has the dashboard, the orchestrator ("keynote driver"), the patches, the manifests, and a step-by-step [user guide](./USER_GUIDE.md). Its steps were re-verified against the running clusters on 2026-09-26 and re-checked on 2026-10-04, and its recovery recipes were run for real on 2026-10-05, after a GKE auto-upgrade had recreated every node (see [How this guide was verified](#10-how-this-guide-was-verified)). The three stages were measured live on 2026-10-03, and again on 2026-10-05 with the redesigned dashboard (§2).

**To build, redeploy, update or tear down the stack, start with [USER_GUIDE.md](./USER_GUIDE.md).** This README has the results, the architecture, the pre-show health check, the stage runbook and the disclosures.

> [!TIP]
> **Hermes Agent variant:** [`hermes/`](./hermes/README.md) runs [Hermes Agent](https://github.com/NousResearch/hermes-agent) inside each of the 1,000 sandboxes. It runs a ~90% idle duty cycle (the light fleet runs 80% since 2026-10-03) and wakes all 1,000 in ~2.8 s on 18 × c4d-standard-16. On stage, each agent's memory visibly survives suspend (measured on 2026-09-28).

> [!WARNING]
> This is a demo setup, and several settings trade safety for speed:
> - Postgres runs with `fsync=off`;
> - a privileged node tuner is used;
> - the gVisor flags weaken isolation;
> - an unauthenticated file server exposes the Postgres data directory.
>
> Read [Known issues and disclosures](#9-known-issues-and-disclosures) before reusing any of it.

**Contents:** [1. What the demo shows](#1-what-the-demo-shows) · [2. Results](#2-results) · [3. Architecture](#3-architecture) · [4. Changes from stock](#4-changes-from-stock) · [5. Repository layout](#5-repository-layout) · [6. Reproduce it](#6-reproduce-it) · [7. Pre-show health check](#7-pre-show-health-check) · [8. Stage runbook](#8-stage-runbook) · [9. Known issues and disclosures](#9-known-issues-and-disclosures) · [10. How this guide was verified](#10-how-this-guide-was-verified) · [11. Cleanup and revert](#11-cleanup-and-revert)

---

## 1. What the demo shows

The dashboard is one page laid out for a 1920×1080 stage screen (redesigned on 2026-10-05). It has three sections, opened one at a time like an accordion: **1 · Agents fleet**, **2 · llm-d routing** (Efficiency) and **3 · Flow control**. The two closed sections fold into 72 px rails at the edges, and each rail keeps one live number on it: agents active, the throughput gain, Pro's queue-wait gain. Click a rail to open its section, or use the keys below. In a window taller than 16:9 (a 16:10 laptop screen, up to 4:3) the page gets taller instead of leaving empty bands above and below; the charts, the flow-control rows and the Agents ticker take the extra height. A wider window still gets bands left and right.

**1 · Agents fleet:** 1,000 Agent Substrate actors (`agent-0001` … `agent-1000`).
- Each agent is a gVisor sandbox. When idle it is paused to a snapshot on its node's local disk.
- A grid shows every agent's state, with one tile per GKE node.
- Also shown:
  - the number of agents active;
  - the fleet idle rate;
  - a millisecond wake and suspend clock;
  - a ramp chart;
  - a ticker with the agents' LLM replies (click one to magnify it).
- The section's header has the **Wake Agents**, **Simulate Traffic** and **Suspend all** buttons.

**2 · llm-d routing and 3 · Flow control:** two vLLM replicas (`pod-1`, `pod-2`) of `google/gemma-4-12B-it` on TPU v6e.
- The llm-d header switches between the two views (**1 Efficiency** | **2 Flow control**) and holds the **LIVE** pill.
- Each view asks one question. Two buttons under it select the stage without and with the feature. Below them, a comparison card measured live in this run shows:
  - the stage without the feature on the left;
  - the stage with it on the right;
  - the gain in the middle.
- **Efficiency:** "Same accelerators. Same model. How does llm-d's routing improve cache hit rate, throughput and latency?"
  - Buttons: **Without llm-d** (Stage 1, round robin) and **With llm-d** (Stage 2, KV-cache-aware router).
  - Each side shows throughput, E2E latency and the KV-cache hit rate.
  - The middle shows the throughput gain, how much lower E2E latency is (in yellow, as "higher", when it got worse), and the hit-rate gain in points.
  - Below: 60 s charts of throughput and KV-cache hit, red without llm-d and green with it, marked at every stage switch.
- **Flow control:** "Traffic spikes across Paid, Premium & Regular tiers. How does Flow Control protect 💎 Paid Members (Pro)?"
  - Buttons: **Without flow control** (Stage 2, one shared queue) and **With flow control** (Stage 3, priority payment tiers).
  - Each side shows the queue latency of Paid Members (Pro) and of the Regular tier. The middle shows how much lower Pro's is and how many times longer the Regular tier waits.
  - Below, one row per tier: 💎 Paid Members (Pro) = premium, Paid Standard = standard, 🆓 Regular / Free Tier = best-effort. The dashboard called the last tier "Free Users" until 2026-10-05.
  - Each row has a bar for its live queue latency, then queued requests, mean wait and dispatch rate. The footer shows the pool saturation and compares Pro with the Regular / Free tier.
  - Without flow control the requests carry no tier, so the three rows share one "shared queue" cell.
- **How the comparison is measured:** each side is the mean of its last 10 s with traffic on, counted from 5 s after the switch. During those first 5 s its numbers are dimmed.
  - A badge on each side says RUNNING NOW, MEASURED (earlier in this run), NO TRAFFIC (selected, traffic off) or NOT RUN YET.
  - A new Wake starts the comparisons over; reloading the page keeps them.
  - **Throughput:** output tokens/s of both pods together.
  - **E2E latency:** from the driver's first call to the agent until the reply, retries included, so every queue counts. A reply counts for the stage that was active when it was sent.
  - **TTFT** is not on the page since 2026-10-06. At this load it doesn't improve with llm-d: the wait before the first token moves from vLLM's queue into llm-d's (§2).
  - **KV-cache hit:** the share of prompt tokens served from the prefix cache.
  - **Queue latency:** the mean wait in llm-d's flow-control queue, per tier.
- **Operator menu (click LIVE):**
  - traffic counters;
  - the driver's last error note;
  - the keyboard keys;
  - the admin actions: reconcile agents, and reset memory in the Hermes variant.

  A yellow dot on LIVE means the driver has a note.

| Button | What happens |
|---|---|
| **Wake Agents** | All 1,000 paused agents are resumed at once (`ResumeActor`). The clock stops when all 1,000 are RUNNING. No LLM calls are made yet. |
| **Simulate Traffic** | Starts the duty cycle at 200 requests/s. Random agents wake, run a script inside their sandbox that asks the LLM for a short PyTorch joke, and pause again. About 80% of the fleet is paused at any moment ("fleet idle rate"), so about 200 agents are active. The driver flag `-fleet-idle-pct` sets it (default 80; it was 90%, about 100 agents and 100 req/s, until 2026-10-03). |
| **Stage buttons** | **Without llm-d** and **With llm-d** in the Efficiency view, **Without flow control** and **With flow control** in the Flow control view. With llm-d and Without flow control are the same Stage 2. They change only the headers on the agents' next requests (driver modes `roundrobin`, `kvaware`, `flow`). Neither the gateway nor the llm-d config is touched when switching.<br>• **Stage 1, without llm-d** (round robin), `roundrobin`: `x-route-mode: round-robin`. The gateway skips the llm-d endpoint picker and spreads requests round robin over the two pods. 200 req/s.<br>• **Stage 2, with llm-d** (KV-cache-aware router), `kvaware`: no routing header. The endpoint picker sends each request to the pod that already has its prefix in KV cache, weighed against load. 200 req/s.<br>• **Stage 3, with flow control** (priority payment tiers), `flow`: 200 req/s, the same load as Stages 1 and 2, with each request tagged with its user tier in `x-llm-d-inference-objective`: 30% Paid Members (Pro) (premium), 40% Paid Standard (standard), 30% Regular / Free Tier (best-effort). When the pool is saturated, flow control dispatches the paid tiers first. (Until the evening of 2026-10-03 Stage 3 added 200 req/s of overload traffic; the driver flag `-overload-rate` still adds overload traffic, to every stage alike, default 0.)<br>The old mode names still work (`balanced` = `kvaware`, `priority` = `flow`). The driver API also accepts `steer8020` (`x-target-pod` header steering, measured in §2.2), which has no button. |
| **Suspend all** | Pauses every running agent back to zero compute. |

Keyboard:
- `W` wake, `T` traffic, `S` suspend.
- `1` / `2` / `3` select the three stages (also `R`, `D` or `B`, and `P`). From the Flow control view, `1` also switches to Efficiency; from Efficiency, `3` switches to Flow control.
- `A` / `E` / `F` open the Agents, Efficiency and Flow control sections; `←` / `→` step through them.
- `Esc` closes the operator menu or a magnified joke.

The keys work whichever section is open.

Each reply shown in the ticker came from inside an agent's sandbox. The agent's script POSTs to the llm-d gateway with `wget`, saves the reply to `/tmp/agent_memory.json` in the sandbox, and returns it.

In steady traffic each request also carries its team's notes ahead of the question, the way an agent carries its memory. The 1,000 agents work in 240 teams (`-contexts 240`), and every agent of a team sends the same 60 lines of notes (`-agent-context-lines 60`); on the live pods that came to about 1.9k prompt tokens per request. The team count was chosen so that the notes of all 240 teams don't stay in one pod's prefix cache, while those of half the teams do. Round robin then often lands a request on a pod that no longer has its team's notes, while KV-aware routing keeps each team on one pod. At 80% fleet idle on 2026-10-03, 43–75% of prompt tokens came from the prefix cache with round robin, against 78–98% with KV-aware routing (§2).

## 2. Results

> [!IMPORTANT]
> **New dashboard, measured live (2026-10-05).** The dashboard now puts each stage side by side with the one before it (§1). The driver, its flags, the gateway, the EPP values and the vLLM pods are the same as on 2026-10-03 evening, and the same 200 req/s is offered in every stage.
>
> **Before the run:**
> - GKE had auto-upgraded both clusters the day before and recreated every node, so all 1,000 agents had been re-created (§9, [user guide §4.8](./USER_GUIDE.md#48-gke-upgraded-the-cluster-every-node-recreated)).
> - Both vLLM pods were warmed up with 70 s of Stage 1 traffic.
> - An earlier run that afternoon, before the warm-up, is not used. pod-2 was still cold after the upgrade, because the Hermes agents' teach traffic had gone to pod-1. In Stage 1 pod-2 queued 149 requests at a 7.4 s TTFT, and one agent's request timed out.
>
> **The run:** clicking the dashboard's own buttons in headless Chrome, about 27 s per stage: Wake → Simulate Traffic → Stage 1 → 2 → 3 → Suspend all. The table shows the dashboard's own comparison numbers (each side the mean of its last 10 s, §1). 8,673 LLM requests, all answered.
>
> | | Without llm-d (Stage 1, round robin) | With llm-d (Stage 2, KV-cache-aware) | On the page |
> |---|---|---|---|
> | Throughput, output tok/s of both pods | 1,619 | 3,174 | 2.0× |
> | E2E latency, agent request → reply | 2,557 ms | 1,057 ms | 2.4× lower |
> | TTFT at the gateway: llm-d queue + vLLM | 475 ms | 547 ms | 1.2× higher, in yellow (not shown since 2026-10-06) |
> | KV-cache hit | 57.1% | 97.1% | +40 pts |
>
> | | Without flow control (Stage 2) | With flow control (Stage 3) | On the page |
> |---|---|---|---|
> | 💎 Paid Members (Pro): queue latency in llm-d | 481 ms | 44 ms | 11× lower |
> | 🆓 Regular / Free Tier: queue latency in llm-d | 481 ms | 1,340 ms | 2.8× longer ("absorbs") |
>
> - **TTFT does not improve with llm-d at this load.** The page showed that in yellow; since 2026-10-06 it shows E2E latency only. The pool is saturated (saturation 1.02–1.06 in Stage 2). Behind llm-d the concurrency detector admits 32 requests per pod, and the rest wait in llm-d's queue.
>   - **Stage 2:** a request waited 473–480 ms in that queue, then 65–66 ms on vLLM.
>   - **Stage 1:** there is no llm-d queue, and the 475 ms is vLLM's own queue plus prefill.
>   - The wait before the first token moves from vLLM into llm-d; it doesn't shrink. What gets faster is everything after the first token: decode is quicker with cache hits and with 32 requests per pod instead of ~70, so E2E is 2.4× lower.
> - **Stage 1 was even this time.** pod-1 / pod-2: E2E 2.61 / 2.53 s, TTFT 673 / 329 ms, prefix-cache hit 55% / 60%, 68 / 70 requests running and 5 / 2 waiting inside vLLM. In a run where one pod falls behind (§9), Stage 1's TTFT is higher.
> - **Stage 2:** both pods E2E 0.49–0.55 s and prefix-cache hit about 98%. 58–70 requests waited in llm-d's single band.
> - **Stage 3:** throughput stayed at about 3,250 tok/s and E2E at about 1.0 s, with saturation 1.06–1.11.
>   - Live queue wait per tier: Paid Members (Pro) 46–49 ms, Paid Standard 66 ms, Regular / Free Tier 1.24 s, with 53–56 requests queued.
>   - The 1.24 s is about a sixth of the agents' 8 s `wget` timeout.
> - **Wake 1,000:** 1,887 ms. **Suspend all** straight from Stage 3, with about 190 agents up: 1,129 ms.
> - On the mock (§2.3) TTFT (in `api/state`; no longer on the page) comes out about 1.3× *lower* with llm-d. The mock's pool model was fitted to the 2026-10-03 runs, where one pod fell behind in Stage 1, so its numbers are simulated and not this result.

> [!IMPORTANT]
> **The same load in every stage (2026-10-03, evening).** All three stages now offer the same 200 req/s from the same ~200 active agents (80% fleet idle). Stage 3 no longer adds 200 req/s of overload traffic, so the stages differ only in how llm-d routes and queues the requests. The driver's new `-overload-rate` flag adds open-loop overload traffic to every stage alike (default 0; `-overload-rate=200` would offer 400 req/s everywhere, not measured). The banner now shows the offered rate in every stage's title, not only in Stage 3's. Nothing else changed: same gateway, EPP values and vLLM pods.
>
> Measured live in three runs, 31,905 LLM requests with 0 failed and 0 retries: one through the driver API (60 s per stage) and two clicking the dashboard's own buttons in headless Chrome (about 22 s per stage; the screenshots in §2.3 are from a later run with the same settings). Values are ranges of 2 s samples of `api/state`, skipping the first 10 s after each click; every metric is a mean over the last 3 s. Served and Output tok/s give the per-run means, then the range of all samples. **Output tok/s** is the tokens both pods generate per second, the banner's Output Tok/s. **Agent wait** is the mean time from an agent's request to its reply, including any queue in llm-d: requests in flight ÷ requests served (Little's law).
>
> | Stage (offered load) | Served | Output tok/s, both pods | Agent wait | E2E pod-1 / pod-2 | TTFT pod-1 / pod-2 | Prefix-cache hit pod-1 / pod-2 | Flow control |
> |---|---|---|---|---|---|---|---|
> | 1. No llm-d, round robin (200 req/s) | ~65–66 req/s (54–70) | ~1,620–1,680 (1,510–1,720) | 2.4–2.6 s | 2.1–4.9 / 0.53–2.8 s | 0.29–2.9 s / 74–810 ms | 45–60% / 51–72% | bypassed |
> | 2. llm-d Router, KV-aware (200 req/s) | ~103–109 req/s (96–120) | ~2,520–2,760 (2,320–2,980) | 1.3–1.4 s | 0.36–0.55 / 0.74–0.95 s | 47–91 / 89–187 ms | 86–98% / 80–95% | one band (no tiers yet); saturation 0.97–1.16; 54–86 queued, wait 0.53–0.85 s |
> | 3. llm-d + Flow, user tiers (200 req/s) | ~110–113 req/s (99–118) | ~2,780–2,840 (2,550–3,000) | 1.25–1.3 s | 0.31–0.47 / 0.74–0.97 s | 42–66 / 91–153 ms | 90–98% / 83–94% | saturation 0.91–1.30. Queue wait: Paid Members 40–84 ms, Paid Standard 44–134 ms, Free Users 1.56–2.27 s (19–42× Paid Members in the dashboard runs). Free Users queue 34–80 deep; 0 skipped. |
>
> - **Same agents, same load: with llm-d they get about 60–70% more done, and each request waits about half as long.** Served rises from ~65 to ~103–113 req/s (Output Tok/s from ~1.6–1.7k to ~2.5–2.8k) and agent wait falls from 2.4–2.6 s to 1.25–1.4 s. The prefix-cache hit rises from 45–72% to 80–98%.
> - **Why served stays below 200:** the duty cycle is a closed loop. 196 workers each wake an agent, wait for its reply and pause it again, so a worker sends its next request only after the last reply. Faster replies let the same agents send more, which is why Stages 2 and 3 serve more. Behind llm-d, the concurrency detector admits 32 requests per pod; the rest wait in llm-d's queue.
> - **Stage 3 changes who waits, not how much gets done.** Served and agent wait stay about as in Stage 2. But the paid tiers now wait 40–134 ms in llm-d's queue, against 0.53–0.85 s for every request in Stage 2, while Free Users wait 1.6–2.3 s, about a quarter of the agents' 8 s `wget` timeout.
> - **The dashboard's per-pod E2E and TTFT were vLLM's own** (until the 2026-10-05 redesign). In Stage 1 the queue is inside vLLM, so they included it; in Stages 2 and 3 it is in llm-d, so they didn't (the flow card showed it). Agent wait is the like-for-like number: about 2× lower with llm-d, where the banner's E2E suggested about 4×.
> - **Stage 1 differs from run to run.** In the API run pod-1 fell behind (E2E 2.4–4.9 s, up to 85 requests waiting inside vLLM) while pod-2 stayed at 0.53–2.3 s; in both dashboard runs the two pods were about even at 2.1–2.8 s.
> - Wake 1,000: 1,983, 1,918 and 1,869 ms (per-agent p50 1,124–1,126 ms). Suspend all straight from Stage 3, with about 190 agents up: 1,458, 1,482 and 1,208 ms.

> [!NOTE]
> **Fleet idle rate 80%, with Stage 3 at 400 req/s (2026-10-03, afternoon).** Simulate Traffic now keeps about 80% of the 1,000 agents paused instead of 90%, so about 200 are active and the offered load doubles: 200 req/s in Stages 1 and 2, 400 req/s in Stage 3. The driver's new `-fleet-idle-pct` flag sets it (default 80). Stage 3's overload is held back while 220 requests are in flight (`-max-inflight`, now auto = active agents + 20; it was 120). Nothing else changed: same gateway, EPP values and vLLM pods.
>
> Measured live in two runs on the light fleet, 0 failed in both: one through the driver API (17,865 LLM requests, 1 retry) and one clicking the dashboard's own buttons in headless Chrome (7,338 requests; §2.3 now shows the evening run's screenshots instead). Ranges cover both runs: 2 s samples of `api/state`, skipping the first 5–10 s after each click; every metric is a mean over the last 3 s.
>
> | Stage (offered load) | Served | E2E pod-1 / pod-2 | TTFT pod-1 / pod-2 | Prefix-cache hit pod-1 / pod-2 | Flow control |
> |---|---|---|---|---|---|
> | 1. No llm-d, round robin (200 req/s) | ~66–69 req/s (59–74) | 2.5–5.0 / 0.47–2.5 s | 0.46–3.0 s / 55–657 ms | 43–60% / 54–75% | bypassed |
> | 2. llm-d Router, KV-aware (200 req/s) | ~101–111 req/s (88–119) | 0.36–0.67 / 0.74–0.98 s | 46–96 / 96–169 ms | 82–98% / 78–94% | one band (no tiers yet); saturation 0.97–1.16; 42–82 queued, wait 0.51–0.99 s |
> | 3. llm-d + Flow, user tiers (400 req/s) | ~114–115 req/s (98–122) | 0.33–0.46 / 0.70–0.98 s | 45–64 / 88–131 ms | 96–98% / 87–94% | saturation 0.95–1.17. Queue wait: Paid Members 39–72 ms, Paid Standard 49–114 ms, Free Users 3.7–4.5 s (67–96× Paid Members in the dashboard run). Free Users queue 132–147 deep. |
>
> - **Twice the offered load, a little more served: the pool is the limit.** Against 90% idle (next box), served rose 10–22% in Stages 2 and 3 and barely in Stage 1. Behind llm-d the concurrency detector admits 32 requests per pod, and the rest wait in llm-d's queue, now already in Stage 2. In Stage 3 the driver skips about 145–150 of the 200 extra req/s at the 220 in-flight cap.
> - **Stage 1: one pod falls behind.** Round robin keeps sending half the requests to the pod that is slower on prefill. It builds a queue inside vLLM (up to 85 waiting), its hit rate drops, and it falls further behind. That was pod-1 in both runs; at 90% idle the two pods were about equal.
> - **Stage 2 now queues:** 0.5–1.0 s in llm-d's single band. The dashboard's per-pod E2E is vLLM's and doesn't include that wait; the flow card shows it.
> - **Free Users wait about 4 s,** half of the agents' 8 s `wget` timeout: 0 failed and 1 retry in the 25,203 requests of both runs. With `-max-inflight=120` they waited 1.6–2.3 s, but Stage 3 then added almost no load (about 199 of its extra 200 req/s were skipped), so the auto value stays.
> - Wake 1,000: 1,904 and 1,930 ms (per-agent p50 1,100–1,111 ms). Suspend all straight from Stage 3, with about 190 agents up: 2,112 and 1,952 ms.

> [!NOTE]
> **Three-stage update (2026-10-03), measured at 90% fleet idle.** The llm-d side now runs as three stages (§1). Two cluster changes make them real:
> - **Stage 1 baseline:** the Envoy gateway has a second route. Requests with `x-route-mode: round-robin` skip the llm-d endpoint picker and go round robin to the vLLM pods, through a new headless Service `gemma4-12b-vllm-pods` (§4).
> - **Stage 3 queueing:** the endpoint picker's flow-control concurrency detector now allows 32 requests in flight per pod instead of 256. 300 req/s now saturates the pool, so queues form and the user tiers separate. With 256 the pool never saturated (§2.2).
>
> **Measured live on 2026-10-03** on the light fleet, at 90% fleet idle (about 100 agents active; 100 / 100 / 300 req/s), before the 80% change above. One run clicked the dashboard's own buttons in headless Chrome: Wake → Simulate Traffic → Stage 2 → Stage 3 → Suspend all. Values are ranges of 2 s samples of `api/state`, skipping the first 5–8 s after each click; every metric is a mean over the last 3 s.
>
> | Stage (offered load) | Served | E2E pod-1 / pod-2 | TTFT pod-1 / pod-2 | Prefix-cache hit | Flow control |
> |---|---|---|---|---|---|
> | 1. No llm-d, round robin (100 req/s) | ~65 req/s (50–70) | 0.86–1.10 / 0.87–1.08 s | 159–247 / 162–306 ms | 57–73% | bypassed |
> | 2. llm-d Router, KV-aware (100 req/s) | ~91 req/s (80–98) | 0.34–0.51 / 0.55–0.86 s | 41–67 / 75–129 ms | 81–94% / 81–89%, climbing | one band (no tiers yet); saturation 0.56–1.00, wait ≤ 40 ms |
> | 3. llm-d + Flow, user tiers (300 req/s) | ~104 req/s (93–107) | 0.38–0.56 / 0.81–0.91 s | 51–97 / 101–153 ms | 86–97% / 83–91%, drifting down | saturation 0.95–1.14. Queue wait: Paid Members 49–69 ms, Paid Standard 73–118 ms, Free Users 1.26–1.45 s (19–28× Paid Members). Free Users queue 34–49 deep. |
>
> - 6,593 LLM requests in the run, 0 failed. Wake 1,000: 1,914 ms (per-agent p50 1,104 ms). Suspend all straight from Stage 3: 1,450 ms (about 95 agents up).
> - **Served is below offered in Stages 1 and 3, for different reasons.** In Stage 1 the duty cycle is the limit: 96 workers each wake an agent, wait for its reply and pause it again, so slower replies mean fewer requests per second. That is also why Stage 2 serves more with the same agents. In Stage 3 the driver holds back requests above 100 req/s while 120 are in flight (`-max-inflight`) and counts them as skipped (about 140/s); llm-d queues the rest.
> - An earlier Stage 3 run the same night gave the same picture: queue wait 50–76 ms for Paid Members, 70–130 ms for Paid Standard and 1.09–1.53 s for Free Users. Its prefix-cache hit drifted from about 95% to about 85% over a minute.
> - pod-2 is consistently slower than pod-1 in Stages 2 and 3 (§9).

> [!IMPORTANT]
> **Repair update (2026-10-02).** "Suspend all" hung on the light fleet: 255 agents were stuck PAUSING or DELETING.
> - **Cause:**
>   - The old driver retried a failed LLM call up to 35 times, 100 ms apart, with no per-agent cap on overload traffic.
>   - When llm-d shed calls (an EPP in-flight leak, then EPP liveness restarts under load), the retries piled processes into the 256 MiB workers. 255 workers were OOM-killed in three waves (09-29, 10-01, 10-02), and each left its agent stuck.
>   - Two snapshots taken during an OOM wave could never be restored.
> - **Second cause, slow wakes:**
>   - The sandbox's PID 1 never reaps orphaned processes. A `/process` call cut off mid-flight (atenet's 10 s route timeout, or a pause during a call) left its `wget` behind as a zombie inside the snapshot.
>   - The 684 agents created before 10-02 had 3.5–14.2 MiB checkpoints (p50 5.7 MiB), against 1.4–3.0 MiB for fresh agents, and wakes had slowed to 2.7–3.1 s.
> - **Fixes:**
>   - **Driver `4d45c260` ([user guide Step 3](./USER_GUIDE.md#step-3-build-the-patched-binaries-and-the-driver)):**
>     - backoff retries: 4 tries for the first joke, 3 for duty-cycle calls, 1 for overload traffic;
>     - at most 2 LLM calls in flight per agent;
>     - `wget -T 8`, so a call ends before atenet's timeout;
>     - waits up to 10 s for an agent's calls before pausing it;
>     - marks an agent whose restore fails, and `reconcile` re-creates it.
>   - **Cluster:**
>     - replaced the 255 OOM-restarted worker pods;
>     - re-created all 1,000 agents from the golden snapshot (zombie-free);
>     - rebalanced placement to 32–41 agents per node (§7).
> - **Measured after the repair (light fleet, 2026-10-02):**
>   - Wake 1,000: **1,893–1,905 ms** over 3 warm wakes, 0 failed; per-agent p50 1,084–1,135 ms. Suspend all with 1,000 up: 1,528–1,582 ms. ate-api showed 1,000 RUNNING, then 1,000 PAUSED, after every step.
>   - Stage flow (30 s Default 50:50, then 60 s Priority, then **Suspend all straight from Priority**): 10,176 LLM calls, 0 failed. **Suspend all took 956 ms** (57 agents up).
>   - After that run: 0 worker restarts, no new EPP restarts, every checkpoint ≤ 3.0 MiB, flow-control saturation back to 0.00.
> - **Priority now delivers ~130 req/s, not 300.** The per-agent cap turns the excess into skipped requests instead of OOMs, and vLLM latency is about 2× the 09-26 level (§9, "vLLM is about half as fast").

> [!NOTE]
> **C4 update (2026-09-28).** The workers moved from 25 × c3-standard-4 to 25 × c4-standard-4, and the TPU cluster's CPU node from e2-standard-4 to c4-standard-4. The 1,000 light agents were re-created on the new nodes.
> - Wake 1,000 (dashboard's Wake, no LLM calls): **1,907–1,953 ms** over 4 warm wakes, 0 failures; per-agent p50 1,045–1,084 ms. On C3 the median was 2,988 ms (below).
> - Suspend all from idle: 1,623–1,734 ms.
> - Simulate Traffic, 40 s: 6,993 LLM requests, 0 failed.
> - The first wake after re-creating the agents restores from the golden snapshot in GCS and took 33.6 s, as [user guide Step 11](./USER_GUIDE.md#step-11-create-the-1000-agents-and-warm-them-up) warns.
> - The Hermes variant, already on C4D, was re-checked after the move: wakes 2,500–2,923 ms, 60 s of traffic with 1,702 replies and 0 failures, and every recall correct, so agent memory survived the move of the CPU node and gateway.
>
> The tables below are the original C3 measurements.

**How these were measured:** measured on the live clusters on 2026-09-26, with the configuration in this folder:
- patched ate-api/atelet with `restoreSem` 12;
- keynote driver v4;
- rest mode `pause`.

Every run was checked against **ground truth**, not only the dashboard's own counters:
- actor states from ate-api (`kubectl ate get actors`);
- real gVisor sandbox processes counted on the 25 nodes.

### 2.1 Agent Substrate: wake, duty cycle, suspend

| Run (driver v4, restoreSem 12) | Wake 1,000 → all RUNNING | Per-agent wake p50 | Then | Suspend all (from click) | After suspend (ate-api / nodes) |
|---|---|---|---|---|---|
| morning, Priority run | 2,984 ms | – | 60 s Priority traffic | 3,781 ms (waited on one failing agent, see §9) | 1,000 PAUSED / 0 sandboxes |
| morning, warm-up after re-creating one agent | 3,111 ms | – | no traffic | 2,522 ms (all 1,000 up) | 1,000 PAUSED / 0 |
| morning, Balanced run | 2,940 ms | – | 60 s Balanced traffic | 604 ms | 1,000 PAUSED / 0 |
| morning, health check | 2,990 ms | – | no traffic | 2,505 ms (all 1,000 up) | 1,000 PAUSED / 0 |
| afternoon r1 (first wake after an atelet restart) | 3,286 ms | 1,628 ms | 60 s Balanced | 741 ms | 1,000 PAUSED / 0 |
| afternoon r2 | 2,902 ms | 1,571 ms | 60 s Balanced | 602 ms | 1,000 PAUSED / 0 |
| afternoon r3 | 2,985 ms | 1,648 ms | 60 s Balanced | 683 ms | 1,000 PAUSED / 0 |
| guide verification, health check (first wake after an atelet restart) | 3,089 ms | 1,660 ms | no traffic | 2,566 ms (all 1,000 up) | 1,000 PAUSED / 0 |
| guide verification, full cycle | 2,958 ms | 1,578 ms | Balanced → 80/20 → Priority → Balanced, 150 s | 607 ms | 1,000 PAUSED / 0 |
| guide verification, §7 commands run as written | 3,085 ms | 1,619 ms | no traffic | 2,440 ms (all 1,000 up) | 1,000 PAUSED / 0 |

**Wake 1,000:**
- Median 2,988 ms over these 10 wakes, range 2,902–3,286 ms.
- 4 of 10 took longer than 3.0 s: the two first wakes after an atelet restart, the warm-up after re-creating an agent, and one ordinary health check (3,085 ms).
- 0 wake failures in every run.
- In the full-cycle verification run: 100 agents were running at 715 ms, 500 at 1,589 ms, 750 at 2,172 ms and all 1,000 at 2,958 ms. The fastest agent took 475 ms.

**Why the tail is about 3 s:** an agent is pinned to the node that holds its local snapshot, and today's placement is uneven, at 32–47 agents per node.
- Nodes with 40 or fewer agents finish by about 2.45 s.
- The 2–3 nodes with 46–47 agents set the tail.

**Simulate Traffic (60 s, Balanced):**
- Fleet idle rate p50 is 90.9–91%.
- 998–1,000 distinct agents complete a full wake → LLM call → pause cycle.
- About 6,200 LLM requests with 0 failed.
- Mid-traffic ground truth in the verification run: 934 PAUSED, 27 PAUSING and 39 RUNNING actors; 65 sandboxes on the nodes.

**Suspend all:**
- 0.60–0.74 s from the click in Balanced mode, when about 100 agents are up.
- 2.4–2.6 s with all 1,000 up.
- 1.3–3.8 s in Priority mode, because it waits for queued LLM calls.

**`restoreSem` experiment:** atelet's per-node restore concurrency, 16 against the deployed 12. Same 60 s Balanced cycle, 0 failures in all runs. 16 was not faster, so 12 stays deployed.

| restoreSem | Wake 1,000 (3 runs) | Per-agent p50 | Suspend all |
|---|---|---|---|
| 16 | 2,992 (first after restart) / 3,099 / 3,326 ms | 1,793–1,824 ms | 613–670 ms |
| 12 | 3,286 (first after restart) / 2,902 / 2,985 ms | 1,571–1,648 ms | 602–741 ms |

### 2.2 llm-d on TPU v6e (2026-09-26, before the three stages)

**Setup:** medians over each phase, excluding the first 8 s after each switch.
- Traffic comes from the agents' sandboxes, through the gateway, to the EPP and then vLLM.
- Every request uses the same 286-token system prompt, a per-agent user prompt, and `max_tokens` 50. The requests carried no team notes yet (they were added on 2026-10-03, §1).
- "Balanced" (later labelled **Default 50:50**) is today's Stage 2, `kvaware`; "Priority" is today's Stage 3, `flow`, with the earlier tier mix of 20% premium / 60% standard / 20% best-effort. The Steer 80/20 row was measured with a dashboard button that has since been removed (2026-10-02); the driver API still accepts `steer8020`.

| Strategy (offered load) | Split pod-1 / pod-2 | Per-pod req/s | Output tok/s per pod | E2E latency per pod | Prefix-cache hit | Flow control |
|---|---|---|---|---|---|---|
| Balanced (100 req/s) | 53.4% / 46.6% | 56.7 / 49.5 | 1,309 / 1,135 | 172 / 165 ms | 89.5% | idle (saturation 0) |
| Steer 80/20 (100 req/s) | **80.0% / 20.0%** | 83.7 / 21.0 | 1,928 / 483 | 196 / 149 ms | 89.5% | idle |
| Priority (300 req/s) | 50.5% / 49.5% | 124.2 / 119.4 | 2,886 / 2,848 | 345 / 346 ms | 89.5% | saturation median 0.2, max 0.45; queues up to 31; wait ≤ 19 ms in every band |
| Balanced again (100 req/s) | 53.6% / 46.4% | 56.9 / 49.3 | 1,318 / 1,146 | 170 / 165 ms | 89.5% | idle |

**Traffic totals:** 21,681 LLM requests during the traffic phases of this run.
- 0 failed.
- 5 retries after an llm-d HTTP 503.
- 8 requests abandoned because their agent was being paused.

**What this shows:**
- **80/20 steering takes effect within seconds.** The split reached 77–80.7% within the first sampled window after the click.
- **The prefix cache is reused across all 1,000 agents.** About 90% of prompt tokens are served from the prefix cache. But nearly all of each prompt was the shared system prompt, which every pod caches after its first request, so any routing would hit about as often (not measured). That is why the three stages add team notes (§1).
- **With the detector at 256, Priority mode produced no visible queueing.** The saturation detector allowed 256 in-flight requests per pod, so 300 req/s never saturated the pool, and every band waited about the same few milliseconds.
  - An earlier llm-d-only test capped concurrency at 16 per pod to force saturation. There, the mean latency was premium 0.40 s, standard 0.68 s and best-effort 1.26 s, and the EPP queue wait was about 80 ms for premium against about 1.1 s for best-effort.
  - Since 2026-10-03 the deployed values cap it at 32 per pod, and Stage 3 queues on the live pool (top of §2).

### 2.3 Dashboard screenshots

**Live cluster (2026-10-05):** captured during the run in the box at the top of §2, by clicking the dashboard's own buttons in headless Chrome. The page still showed TTFT then; it was removed on 2026-10-06.

| Agents fleet, Stage 1 traffic | Efficiency: Stage 1 running, Stage 2 not run yet |
|---|---|
| ![Live: agents fleet with traffic](./docs/images/live_01_agents_traffic.png) | ![Live: Efficiency, Stage 1](./docs/images/live_02_eff_stage1.png) |

| Efficiency: both stages measured | Flow control: Stage 2, one shared queue |
|---|---|
| ![Live: Efficiency, both stages measured](./docs/images/live_03_eff_both_measured.png) | ![Live: Flow control, shared queue](./docs/images/live_04_flow_stage2_shared_queue.png) |

| Flow control: Stage 3, priority tiers | Agents fleet after Suspend all |
|---|---|
| ![Live: Flow control on](./docs/images/live_05_flow_stage3.png) | ![Live: all suspended](./docs/images/live_06_agents_suspended.png) |

**Mock backend:** the rest were rendered by `dashboard/shoot.mjs` against `dashboard/mock_server.py`, not the live cluster, so their numbers are simulated. The mock runs the same three stages.

| Idle | Operator menu (click LIVE) |
|---|---|
| ![Idle](./docs/images/01_idle.png) | ![Operator menu](./docs/images/04b_operator_menu.png) |

| A window taller than 16:9 (1920×1200) | Stage 3 settling: the new side's numbers dimmed |
|---|---|
| ![Flow control at 1920x1200](./docs/images/08d_flow_1920x1200.png) | ![Stage 3 measuring](./docs/images/06b_flow_stage3_measuring.png) |

More states are in [`docs/images/`](./docs/images/):
- mid-wake and a magnified joke;
- 1440×900 and the folded rails;
- suspend and reconcile;
- a vLLM pod down and other errors;
- reconnect.

---

## 3. Architecture

```mermaid
flowchart LR
  subgraph SUB["Substrate cluster"]
    DRV["keynote-driver: dashboard + API on 8090"]
    API["ate-api-server x1 (patched) + Postgres"]
    NET["atenet-router x4"]
    LET["atelet (patched) on 25 nodes"]
    AG["1,000 agents: gVisor sandboxes"]
  end
  subgraph TPU["TPU cluster"]
    GW["Envoy gateway, hostPort 8080"]
    EPP["llm-d EPP v0.10.0"]
    P1["pod-1: vLLM Gemma 4 12B, TPU v6e-4"]
    P2["pod-2: vLLM Gemma 4 12B, TPU v6e-4"]
  end
  DRV -- "ResumeActor / PauseActor (gRPC, 32 connections)" --> API
  API --> LET
  LET -- "restore / checkpoint" --> AG
  DRV -- "POST /process: run the agent script" --> NET
  NET --> AG
  AG -- "POST /v1/chat/completions + routing headers" --> GW
  GW -- "ext_proc: pick a pod (Stages 2, 3)" --> EPP
  GW -- "chosen pod, or round robin (Stage 1)" --> P1
  GW --> P2
  DRV -. "scrape /metrics" .-> P1
  DRV -. "scrape /metrics" .-> P2
  DRV -. "scrape /metrics" .-> EPP
```

**Wake path:** the driver calls `ResumeActor` for all 1,000 agents at once, over 32 gRPC connections. ate-api binds each actor to a pre-warmed worker pod on the node that holds its snapshot. That node's atelet restores the gVisor sandbox with `runsc restore`, through the `runsc_fast` wrapper. `ResumeActor` returns when the actor is RUNNING.

**LLM path:** the driver POSTs to `atenet-router` `/process`, addressed to the actor's DNS name. The agent's sandbox runs a shell script that calls the gateway with `wget`. In steady traffic the request carries the team's notes ahead of the question (§1). The stage sets the headers:
- Stage 1 (`roundrobin`): `x-route-mode: round-robin`. Envoy matches it on a separate route that has `ext_proc` disabled, so the EPP never sees the request. Envoy spreads these requests `ROUND_ROBIN` over the pods behind the headless Service `gemma4-12b-vllm-pods`.
- Stage 2 (`kvaware`): no routing header.
- Stage 3 (`flow`): `x-llm-d-inference-objective` naming the request's user tier: `premium-traffic` (priority 100), `standard-traffic` (0) or `best-effort-traffic` (−10), from [`inference-objectives.yaml`](./manifests/tpu/inference-objectives.yaml).
- `x-target-pod` only with driver API `steer8020` (no dashboard button).

In Stages 2 and 3, Envoy asks the EPP (`ext_proc`) which pod to use:
- The EPP scores pods by prefix-cache match (from vLLM's KV-cache events over ZMQ), in-flight requests, KV-cache utilization and the `x-target-pod` header.
- Flow control applies priority bands, but only when the pool is saturated. Its concurrency detector counts a pod as full at 32 requests in flight.
- Envoy then forwards the request to the chosen vLLM pod (`ORIGINAL_DST`).

**Why an in-cluster Envoy instead of a GKE Gateway:** the Gateway's internal load balancer depends on health-check probes from Google's ranges (35.191.0.0/16, 130.211.0.0/22). In this project an automated firewall policy strips non-RFC1918 source ranges, and the load balancer returned intermittent 503s. A plain Envoy on the CPU node, with `hostPort` 8080, avoids load balancers entirely. The agents reach it at `<node IP>:8080` across the shared VPC.

| | Substrate cluster (`ikwak-substrate-ane1`) | TPU cluster (`ikwak-tpu-v6e-ane1`) |
|---|---|---|
| Location, version | `asia-northeast1-b`, GKE 1.35.8-gke.1380001 (auto-upgraded on 2026-10-04), REGULAR channel | `asia-northeast1-b`, GKE 1.35.8-gke.1380001 (created at 1.35.7; auto-upgraded, last on 2026-10-04), REGULAR |
| Node pools | `substrate-c4-pool`: 25 × c4-standard-4 (hyperdisk-balanced 100 GB), which runs atelet and 1,600 worker pods (64 per node).<br>`keynote-driver-pool`: 4 × n2-standard-8, label `pool=keynote-driver`, taint `dedicated=keynote-driver:NoSchedule`, which runs Postgres, 1 ate-api, 4 atenet-router, 4 atenet-egress and the keynote driver. It stays on N2: C4 can't attach Postgres's pd-balanced volume.<br>The [Hermes variant](./hermes/README.md) adds `hermes-c4d-pool` (18 × c4d-standard-16). | 1 × c4-standard-4 CPU node (hyperdisk-balanced), which runs the EPP and Envoy. In the demo cluster this pool is `cpu-c4-pool`; [user guide Step 6](./USER_GUIDE.md#step-6-tpu-cluster) creates it as `default-pool`. Both manifests select it by `cloud.google.com/machine-family: c4`.<br>`tpu-v6e-spot` and `tpu-v6e-spot-decode`: 1 × ct6e-standard-4t each (TPU v6e, 2x2 topology, Spot, hyperdisk-balanced 100 GB, taint `google.com/tpu=present:NoSchedule`). |
| Networking | VPC-native. Pods `10.56.0.0/14` and services `34.118.224.0/20`, both auto-assigned. Dataplane V2. | Pods `172.28.0.0/14` and services `172.24.16.0/20`, from the subnet's secondary ranges `pods` and `services`. Gateway API standard channel. |
| Other | Workload Identity; beta APIs `podcertificaterequests` + `clustertrustbundles`; managed OpenTelemetry (all set by `setup-gcp`) | – |

**Software:**
- **Agent Substrate:** [agent-substrate/substrate](https://github.com/agent-substrate/substrate) `v0.1.0` (`fa6d949`) with the GKE release images `v0.1.0-gke.1`. ate-api and atelet are replaced by patched builds (§4).
- **vLLM:** [vllm-project/vllm-torchtpu](https://github.com/vllm-project/vllm-torchtpu) @ `3eb7abb5` with no code changes. The deployed image digest is `sha256:699c7ccf…`. It runs `google/gemma-4-12B-it` with TP=4, `--max-model-len 65536` (Hermes Agent needs ≥64k; replies are still capped at 50 tokens), `--gpu-memory-utilization 0.90` and KV-cache events over ZMQ.
- **llm-d:** EPP `ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.10.0`, installed with the `inferencepool` Helm chart v1.2.0. Envoy is `envoyproxy/envoy:v1.39-latest`.

## 4. Changes from stock

| Where | Change | Why |
|---|---|---|
| ate-api ([patch](./patches/ateapi-atelet-fast-wake.patch)) | Caches: actor templates (2 s TTL), atepg templates, and JWT verification (20 s TTL, so a revoked token keeps working for up to 20 s).<br>`UpdateActorFast` finalizes RUNNING without a re-read.<br>Optimistic worker-cache `MarkFull` and asynchronous worker-binding bookkeeping.<br>Outbox poll interval cut from 50 to 5 ms.<br>Mutex in the atelet dialer.<br>Default log level `warn`. | Removes synchronous Postgres round trips from `ResumeActor`. **Requires exactly one ate-api replica**, because the caches are per process. |
| atelet (same patch) | `restoreSem` = 12 (per-node restore concurrency).<br>Skips `resetActorDirs` when already clean.<br>`writeFileAtomic` replaced by plain `os.WriteFile` (no fsync).<br>Local checkpoints are hard-linked.<br>Image and volume setup runs sequentially.<br>Embeds the `runsc_fast` wrapper. | Faster restores |
| [`runsc_fast_sync.c`](./patches/runsc_fast_sync.c) | Adds `--shared-root=/tmp/runsc-shared-root --gofer-network-namespace=host --host-settings=ignore --restore-spec-validation=ignore -log=/dev/null`.<br>Drops `--alsologtostderr` and `-direct`.<br>Sets `GOMAXPROCS=2`. | Faster `runsc restore`. **Weakens isolation, and gVisor logs are discarded.** |
| Postgres ([`scale-control-plane.sh`](./manifests/substrate/scale-control-plane.sh)) | Settings: `max_connections 1000`, `shared_buffers 4GB`, `work_mem 32MB`, `synchronous_commit off`, **`fsync off`, `full_page_writes off`**, `autovacuum_naptime 5s`, and others.<br>Pinned to one keynote-driver-pool node with 2–16 CPU. | Database latency during 1,000 concurrent binds |
| ate-api, atenet | ate-api has 1 replica, pinned next to Postgres, with a DB pool of 160 max / 64 min connections. atenet-router and atenet-egress have 4 replicas each on keynote-driver-pool. | Keeps the control plane off the busy worker nodes |
| [WorkerPool](./manifests/substrate/sandbox-workerpool.yaml) | 1,600 pre-warmed workers, requests 10m CPU / 64 Mi, limits 1 CPU / 256 Mi | Each node keeps idle workers even with all 1,000 agents awake |
| [ActorTemplate `sandbox-dense`](./manifests/substrate/sandbox-dense-template.yaml.tmpl) | The upstream sandbox demo app, sized to 1 CPU / 256 Mi | Matches the dense workers |
| podcertificate-controller | `WORKERS_PER_SIGNER=16` | Signs the 1,600 worker certificates quickly |
| [ate-node-tuner](./manifests/substrate/ate-node-tuner.yaml) | A privileged DaemonSet:<br>• remounts `/var` with `nobarrier,commit=600`;<br>• sets dirty-page sysctls, the THP setting and the performance CPU governor;<br>• runs a page-cache warmer that reads every local checkpoint every 20 s. | Faster restores. **Risks data loss on a node crash.** |
| keynote driver ([main.go](./substrate-bench/keynote_driver/main.go)) | Rests agents with `PauseActor`, a node-local snapshot (`-rest-mode=pause`); uses 32 gRPC connections; runs a duty cycle of about 200 active agents (`-fleet-idle-pct`, default 80; 90 until 2026-10-03); cross-checks against ate-api after Suspend all.<br>Three stages (`roundrobin`, `kvaware`, `flow`) set per-request headers (§3); a retry is sent with the headers of the stage active at that moment (§9). Steady-traffic requests carry team notes (`-contexts 240`, `-agent-context-lines 60`); Stage 3 tags user tiers 30/40/30 (`-tier-mix`). Every stage offers the same 200 req/s; `-overload-rate` adds open-loop overload traffic to every stage alike (default 0; until the evening of 2026-10-03 Stage 3 alone added 200 req/s), held back while 220 requests are in flight (`-max-inflight`, auto = active agents + 20). These are the flag defaults, so the driver args in [`deploy-driver.sh`](./manifests/substrate/deploy-driver.sh) don't list them. | The demo orchestrator |
| llm-d ([values](./manifests/tpu/gaie-values-flowctl.yaml)) | Scorers: precise prefix-cache (weight 3); `active-request-scorer` instead of `queue-scorer` (2); kv-cache-utilization (2); `header-label-affinity-scorer` on `x-target-pod` (100).<br>Flow control with bands 100 / 0 / −10 and a concurrency detector at **32** per pod (256 until 2026-10-02). | `queue-scorer` reads a lagging vLLM gauge; in a 1,000-request burst it sent about 750 requests in a row to one pod (86.5/13.5).<br>At 256 the pool never saturated, so flow control never queued (§2.2). Measured on one pod with prefix-cached ~1.7k-token prompts, 32 in flight served 66 req/s at 452 ms. |
| Envoy gateway ([manifest](./manifests/tpu/llmd-envoy-gateway.yaml)) | A second route: requests with `x-route-mode: round-robin` have `ext_proc` disabled and go `ROUND_ROBIN` to cluster `vllm_round_robin` (`STRICT_DNS` on the new headless Service `gemma4-12b-vllm-pods`, which selects the InferencePool's pod labels). | Stage 1's "No llm-d" baseline, on the same gateway and pods, with no llm-d in the path |

## 5. Repository layout

```text
substrate/
├── README.md                          results, architecture, health check, runbook and disclosures
├── USER_GUIDE.md                      build, redeploy, recover, update and tear down the stack
├── plan.md                            status, decisions and open items
├── implementation.md                  engineering notes: patches, tuning, measurements, lessons
├── dashboard/
│   ├── index.html                     the stage dashboard (one file, served by the driver)
│   ├── mock_server.py                 offline mock of the driver API for rehearsal (simulated numbers)
│   └── shoot.mjs                      headless-Chrome screenshot script (drives the mock)
├── docs/images/                       dashboard screenshots, rendered with the mock (live_*.png and hermes-live.png are from the live cluster)
├── hermes/                            Hermes Agent variant (see hermes/README.md)
│   ├── README.md                      guide, measured results, pre-show steps, known issues
│   ├── actor/                         actor image: Dockerfile, entrypoint, warm-up, Hermes config, persona
│   └── manifests/                     WorkerPool, ActorTemplate and LLM-proxy Service
├── manifests/
│   ├── substrate/
│   │   ├── scale-control-plane.sh     sizes and tunes the Substrate cluster (idempotent, DRY_RUN=1)
│   │   ├── deploy-patched-binaries.sh runs the patched ate-api/atelet on the stock images (idempotent, DRY_RUN=1)
│   │   ├── http_srv.go                the file server that script uses
│   │   ├── sandbox-workerpool.yaml    1,600-worker WorkerPool
│   │   ├── sandbox-dense-template.yaml.tmpl  ActorTemplate for the agents
│   │   ├── ate-node-tuner.yaml        node tuning DaemonSet (see §9)
│   │   ├── deploy-driver.sh           deploys or re-points the keynote driver and dashboard (idempotent, DRY_RUN=1)
│   │   └── keynote-driver.yaml        driver namespace, RBAC and pod
│   └── tpu/
│       ├── prereqs.yaml               StorageClass, ServiceAccount and PVCs for the vLLM pods
│       ├── gemma4-12b-torchtpu-deployments.yaml  vLLM pod-1 and pod-2
│       ├── gemma4-12b-render-svc.yaml Service the EPP uses for tokenization
│       ├── inference-objectives.yaml  priority bands (premium / standard / best-effort)
│       ├── gaie-values-flowctl.yaml   llm-d EPP Helm values (deployed)
│       ├── gaie-values-kvaware.yaml   earlier values, not deployed
│       └── llmd-envoy-gateway.yaml    in-cluster Envoy gateway
├── ops/                               repair and rehearsal tools (§7)
│   ├── ck_scan.py                     per-agent checkpoint sizes from every node
│   ├── rebalance.py                   evens out agents per node after re-creations
│   ├── recreate_lost.py               re-creates agents whose snapshot node is gone, then reconciles (user guide §4.5)
│   ├── sandbox_scan.py                lists worker pods with leftover sandboxes, stuck agents or restarts (§7)
│   └── demo_soak.py                   replays the stage flow and samples the driver's state
├── patches/
│   ├── ateapi-atelet-fast-wake.patch  against agent-substrate/substrate@fa6d949 (v0.1.0)
│   ├── runsc_fast_sync.c              runsc wrapper embedded in atelet (deployed)
│   └── runsc_fast_async.c             DO NOT USE: failed experiment (see implementation.md)
└── substrate-bench/
    ├── keynote_driver/main.go         the driver: dashboard backend + orchestrator
    ├── keynote_driver/hermes.go       -harness hermes: agent turns, memory grading, LLM proxy
    └── agent_bench/ burst_bench/ poisson_wave/   earlier CLI benchmarks; not used by the demo (they compile against v0.1.0 + the patch)
```

---

## 6. Reproduce it

On 2026-10-04 the build steps moved to the **[user guide](./USER_GUIDE.md)**, which also checks a running stack, recovers from restarts, updates one component at a time and tears everything down. The steps keep their numbers: former §6.N is Step N.

| Was | Now in the user guide |
|---|---|
| 6.0 Prerequisites and variables | [Step 0](./USER_GUIDE.md#step-0-tools-quota-and-variables) |
| 6.1 Shared VPC | [Step 1](./USER_GUIDE.md#step-1-shared-vpc) |
| 6.2 Substrate cluster and Agent Substrate v0.1.0 | [Step 2](./USER_GUIDE.md#step-2-substrate-cluster-and-agent-substrate) |
| 6.3 Build the patched binaries and the driver | [Step 3](./USER_GUIDE.md#step-3-build-the-patched-binaries-and-the-driver) |
| 6.4 Size and tune the Substrate cluster | [Step 4](./USER_GUIDE.md#step-4-size-and-tune-the-substrate-cluster) |
| 6.5 Run the patched ate-api and atelet | [Step 5](./USER_GUIDE.md#step-5-run-the-patched-ate-api-and-atelet) |
| 6.6 TPU cluster | [Step 6](./USER_GUIDE.md#step-6-tpu-cluster) |
| 6.7 vLLM image | [Step 7](./USER_GUIDE.md#step-7-vllm-image) |
| 6.8 vLLM pods | [Step 8](./USER_GUIDE.md#step-8-vllm-pods) |
| 6.9 llm-d: CRDs, priorities, endpoint picker, gateway | [Step 9](./USER_GUIDE.md#step-9-llm-d-crds-priorities-endpoint-picker-gateway). "Changing a running install" is now [§5.4](./USER_GUIDE.md#54-envoy-gateway) and [§5.5](./USER_GUIDE.md#55-epp-values). |
| 6.10 Keynote driver and dashboard | [Step 10](./USER_GUIDE.md#step-10-keynote-driver-and-dashboard). The copy block is now the script [`deploy-driver.sh`](./manifests/substrate/deploy-driver.sh). |
| 6.11 Create the 1,000 agents and warm them up | [Step 11](./USER_GUIDE.md#step-11-create-the-1000-agents-and-warm-them-up) |
| 6.12 Open the dashboard, or rehearse offline | [Step 12](./USER_GUIDE.md#step-12-open-the-dashboard-or-rehearse-offline) |
| 11. Cleanup and revert | [§6 Tear it down](./USER_GUIDE.md#6-tear-it-down) |

New in the guide: a **Check** with its expected output after every step, [read-only checks of the whole stack](./USER_GUIDE.md#3-check-the-whole-stack), [recovery recipes](./USER_GUIDE.md#4-redeploy-and-recover) (a vLLM or EPP restart, a lost driver pod, a `postgres-0` restart, a recreated node, a RECONNECTING dashboard) and [updates of one component](./USER_GUIDE.md#5-update-one-component).

---

## 7. Pre-show health check

Do this once before the show, with the port-forward from [user guide Step 11](./USER_GUIDE.md#step-11-create-the-1000-agents-and-warm-them-up) running, and then don't touch the agents until the show. It makes sure the snapshots used on stage come from a clean, idle pause.

```bash
post() { curl -s -X POST -H 'Content-Type: application/json' "localhost:8090/api/$1" -d "${2:-{\}}"; echo; }
state() { curl -s localhost:8090/api/state | python3 -c 'import json,sys,collections; d=json.load(sys.stdin); b=d["burst"] or {}; print(d["phase"], dict(collections.Counter(d["agents"])), "all_running_ms", b.get("all_running_ms"), "wake_failed", b.get("wake_failed"), "all_suspended_ms", b.get("all_suspended_ms"))'; }
post strategy '{"mode":"roundrobin"}'                            # Stage 1: the driver keeps the last stage, so set it before every show
post burst '{"hold":true,"wake_only":true}'; sleep 8; state     # expect: running {'2': 1000} all_running_ms ~2000 wake_failed 0 ...
kubectl ate --context="${CTX_SUB}" get actors -a ate-demo-sandbox -o json \
  | python3 -c 'import json,sys,collections; d=json.load(sys.stdin); d=d if isinstance(d,list) else d.get("actors",[]); print(collections.Counter(a["status"]["state"] for a in d))'   # expect: Counter({'ACTOR_STATE_RUNNING': 1000})
post suspend; sleep 6; state                                     # expect: idle {'0': 1000} ... all_suspended_ms ~1600
kubectl ate --context="${CTX_SUB}" get actors -a ate-demo-sandbox -o json \
  | python3 -c 'import json,sys,collections; d=json.load(sys.stdin); d=d if isinstance(d,list) else d.get("actors",[]); print(collections.Counter(a["status"]["state"] for a in d))'   # expect: Counter({'ACTOR_STATE_PAUSED': 1000})
```

**If an agent does not reach RUNNING:**
1. The driver's note (yellow dot on **LIVE**) reads `agent-NNNN wake failed: … runsc restore …`. The full error, typically `inconsistent private memory files on restore`, is in the driver log: `kubectl --context="${CTX_SUB}" -n keynote-demo exec keynote-driver -- tail -n 200 /work/driver.log`.
2. Run `post reconcile` and wait until `state` prints `idle`. The driver re-creates every agent whose restore failed, and every agent that is not at rest.
3. Run this health check again. A re-created agent's first wake restores from the golden snapshot in GCS, so it is slower.

**If Suspend all hangs or ends "Suspend incomplete", or Wake 1,000 gets slower than about 2.1 s:**
1. **Stuck agents and leftover sandboxes.** Suspend all can't finish while an agent won't pause. That happens when:
   - its worker was OOM-restarted, which leaves the agent PAUSING or DELETING;
   - its sandbox died or hung, so every pause fails inside gVisor with `runsc checkpoint … exit status 128`. No worker restarts. This is what happened on 2026-10-07 (§9).

   The driver marks an agent broken the first time its checkpoint or restore fails inside `runsc`: its cell turns red, the duty cycle never picks it again, and Suspend all doesn't retry it. Suspend all then ends as **Suspend incomplete · N still up**, and the note reads `suspend-all: N agents still not at rest in ate-api; marked broken: agent-NNNN (run reconcile to re-create them)`. Long runs can also leave gVisor sandboxes that no agent owns, in workers that ate-api counts as free. `ops/sandbox_scan.py` lists all these workers, and any that restarted. Replace them (the WorkerPool recreates them), then reconcile:
   ```bash
   CTX_SUB="${CTX_SUB}" python3 ops/sandbox_scan.py             # read-only: each worker to replace, and why
   CTX_SUB="${CTX_SUB}" NAMES_ONLY=1 python3 ops/sandbox_scan.py \
     | xargs -r kubectl --context="${CTX_SUB}" -n ate-demo-sandbox delete pod --grace-period=10
   post reconcile
   until curl -s localhost:8090/api/state | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin)["phase"]!="idle")'; do sleep 5; done
   curl -s localhost:8090/api/state | python3 -c 'import json,sys; print(json.load(sys.stdin).get("note"))'   # expect: preflight: … re-created N (0 failed) … 0 not at rest
   ```
   The scan runs only while the driver is idle. To be safe, wait about 5 minutes before the next wake: ate-api's worker cache relists every 5 minutes (§9).
2. **Bloated snapshots.** Run `CTX_SUB=… python3 ops/ck_scan.py`. A fresh agent checkpoints at 1.4 MiB and at 2–4 MiB after it has served traffic. Bigger checkpoints restore more slowly. On 2026-10-02 they came from zombie processes (§9). On 2026-10-07, after a 21-hour run, they were 4.5–8.0 MiB, and Wake 1,000 took about 3.1 s. Re-create those agents: delete them with `kubectl ate … delete actor --any-state`, then `post reconcile`. To re-create all 1,000 (deleting 24 at a time took 75 s on 2026-10-07):
   ```bash
   seq -f 'agent-%04g' 1 1000 | xargs -P 24 -I{} kubectl ate --context="${CTX_SUB}" delete actor {} --any-state -a ate-demo-sandbox
   post reconcile   # then wait for idle as above; expect: re-created 1000 (0 failed); at T0: 1000 suspended, 0 paused, 0 not at rest
   ```
   Then run this health check once, so every agent gets a local snapshot (the first wake took 2.5 s on 2026-10-07), and go on to step 3.
3. **Uneven placement.** Every re-created agent lands on a random node, and the node with the most agents sets the wake time. `CTX_SUB=… python3 ops/rebalance.py 40 10` evens it out. On 2026-10-02 it took 10 rounds, about 80 s, and left 32–41 agents per node. On 2026-10-07, after all 1,000 were re-created (up to 53 on one node), it took 10 rounds, about 94 s, and left 34–41. It wakes and suspends the fleet every round, so run it only while the stage is not in use.
4. Run this health check again.

## 8. Stage runbook

1. **Before walking on:** the health check passed, and the Agents section shows 0 agents running, "of 1,000 · all suspended". The driver keeps the last stage, so press `1` before every show; **Without llm-d** (Stage 1) is then selected in the Efficiency view. The next Wake clears the comparisons left over from a rehearsal.
2. **Wake Agents** (`W`): the counter races to 1,000 in about 2 s (1,887 ms on 2026-10-05). It sends no LLM calls.
3. **Simulate Traffic** (`T`, Stage 1, round robin): about 200 agents are active at 80% fleet idle, and jokes scroll; click a joke to magnify it. Every stage offers the same 200 req/s.
4. **Efficiency** (`E`): the **Without llm-d** side says RUNNING NOW. Its numbers are dimmed for the first 5 s, then show the mean of the last 10 s. On 2026-10-05: 1,619 tok/s, E2E 2,557 ms, KV-cache hit 57%. Give each stage 20–30 s; the live run used about 27 s.
5. **With llm-d** (`2`, Stage 2): the With llm-d side fills in, and the middle shows the gains. On 2026-10-05: throughput 2.0×, E2E latency 2.4× lower, KV-cache hit +40 pts.
   - The page doesn't show TTFT. Talk about throughput and E2E latency, and don't claim a faster first token: the wait before the first token moves from vLLM into llm-d's queue (top of §2).
6. **Flow control** (`F`): **Without flow control** is the Stage 2 that is running, so its side is already measured: every tier waits in one shared queue, 481 ms on 2026-10-05. Press `3` (**With flow control**, Stage 3) and the requests carry a payment tier. Paid Members (Pro) then wait about 44 ms (11× lower), and the Regular / Free tier about 1.3 s (2.8× longer). Throughput stays at about 3,250 tok/s.
   - From Efficiency, `3` also opens Flow control, and from Flow control `1` opens Efficiency.
   - These are queue waits inside llm-d, not end-to-end latency; talk about them as "the free tier waits in line" (§9).
   - Going back to Stage 2 while traffic runs costs queued Regular / Free-tier requests one 8 s `wget` timeout; then they are retried at Stage 2's standard priority (§9). Going forward (1 → 2 → 3) avoids it.
7. **Suspend all** (`S`): 1,129 ms straight from Stage 3 on 2026-10-05, with about 190 agents up (1,208–1,482 ms on 2026-10-03 evening).
8. After any reconcile (operator menu: click **LIVE**, then **reconcile agents** twice) or agent re-creation, do one warm-up wake + suspend (the health check) before the next show.

## 9. Known issues and disclosures

**Safety trade-offs (demo only):**
- **Postgres:** `fsync=off` and `full_page_writes=off`. A node crash can corrupt the Substrate database.
- **Node tuner:** privileged and hostPID. It remounts `/var` with `nobarrier,commit=600`, so up to 10 minutes of writes can be lost and the filesystem can be corrupted on a crash. It also changes sysctls, THP and the CPU governor, and runs a page-cache warmer. Its effects persist until the nodes are recreated.
- **`runsc_fast` flags:** `--gofer-network-namespace=host`, `--host-settings=ignore` and `--restore-spec-validation=ignore` weaken gVisor isolation and checks. `-log=/dev/null` discards gVisor logs, which is why a sandbox crash could not be root-caused.
- **`http_srv` in `postgres-0`:** serves the whole Postgres data directory, including raw database files, unauthenticated on the pod network.
- **Patched ate-api:**
  - It must run as a single replica, which is a single point of failure.
  - A revoked token keeps working for up to 20 s because of the JWT cache.
  - Worker binding is asynchronous. RUNNING is still only set after the restore completes; we checked this.
- **Patched atelet:** writes files non-atomically and without fsync.

**Honesty notes for the stage:**
- **Wake Agents** makes no LLM calls; jokes start with Simulate Traffic.
- Each duty-cycle agent is held "running" for 70–140 ms before its request so the grid is visible.
- Numbers on the `:8765` mock and in `docs/images/` are simulated, except `live_*.png` and `hermes-live.png`.
- The page-cache warmer keeps checkpoints in RAM.
- **Stage 1 is a baseline we built:** round robin on the same Envoy and pods, with llm-d taken out of the path. It is not a separate product or a stock load balancer.
- **The workload is shaped so routing matters:** all agents of a team send the same ~1.5k tokens of team notes, and the number of teams (240) was chosen so that one pod's prefix cache keeps about half of them (§1). With short, mostly shared prompts, round robin would hit about as often as KV-aware routing (§2.2).
- **Stage 3's Paid vs Free gap is real, and it is queue wait.** It is the EPP's flow-control queue wait per tier, and it shows up only because the concurrency detector counts a pod as full at 32 in flight, so the pool saturates. vLLM gets no tier information, so once dispatched every tier is served alike.
- **The Regular / Free tier waits 1.2–2.3 s in Stage 3,** a sixth to a quarter of the agents' 8 s `wget` timeout: about 1.3 s on 2026-10-05, and 1.6–2.3 s in the three runs on 2026-10-03 evening (31,905 requests in all, with 0 failures and 0 retries). With 400 req/s in Stage 3 (the 2026-10-03 afternoon runs) it waited 3.7–4.5 s, half the timeout; `-max-inflight` (auto 220) limits how deep that queue gets only when `-overload-rate` adds traffic. A slower pool would push the wait toward the timeout.
- **Served is below offered:** with the same 200 req/s offered in every stage, about 65 req/s were served in Stage 1, 103–109 in Stage 2 and 110–113 in Stage 3 on 2026-10-03 evening (§2). The duty cycle is a closed loop, so slower replies mean fewer requests. The dashboard's throughput (output tokens/s) counts only what was served. With 400 req/s in Stage 3 it served 114–115; at 90% idle about 65 of 100 and 104 of 300.
- **Stage 2 queues too, inside llm-d:** in one band, for 0.47–0.85 s (2026-10-03 and 2026-10-05). The dashboard's E2E includes that wait, because the driver times each request from its first call to the agent until the reply. The TTFT it showed on 2026-10-05 added llm-d's queue wait to vLLM's own TTFT; since 2026-10-06 it shows no TTFT. Until 2026-10-05 the banner and the per-pod cards showed vLLM's own E2E and TTFT, which left that wait out and made Stage 2 look about 4× better; timed by the agents, it is 2–2.4× (§2). **TTFT gets no better with llm-d at this load:** the wait before the first token moves from vLLM's queue into llm-d's (top of §2).
- **In Stage 1 the pods queue inside vLLM at 80% idle:** round robin sends each pod half the requests whatever its queue. In three of the five 80% runs on 2026-10-03 pod-1 fell behind (E2E up to 5 s, up to 85 requests waiting inside vLLM) while pod-2 stayed at 0.5–2.5 s; in the other two, and on 2026-10-05, both pods ran at 2.1–2.8 s. A pod that has just restarted is slow at first, so warm both up before measuring ([user guide §4.8](./USER_GUIDE.md#48-gke-upgraded-the-cluster-every-node-recreated)).
- **KV usage reads low (10–25%)** in `api/state` (`kv_usage_pct`; the dashboard showed it per pod until 2026-10-05): vLLM counts only the cache blocks that running requests use. Cached prefixes that no running request holds are not counted, even though they still produce hits.
- **pod-2 was slower with llm-d routing on 2026-10-03:** its E2E latency was 1.4–3.0× pod-1's in Stages 2 and 3, usually with a lower prefix-cache hit. Under round robin (Stage 1) the two pods were about equal at 90% idle; at 80% pod-1 was the slower one in three runs and about even with pod-2 in two. Both run the same image and flags; pod-2 runs on node pool `tpu-v6e-spot-decode` with its own model PVC. Not investigated. On 2026-10-05, after the GKE upgrade had restarted both pods on new nodes, they were even in Stage 2: E2E 0.49–0.55 s and about 98% prefix-cache hit on both.
- **The driver keeps the last stage** across Suspend all and reconcile. Select Stage 1 before each show.
- **Going back from Stage 3 to Stage 2 costs queued Regular / Free-tier requests one 8 s timeout.** Stage 2 sends everything at standard priority, and while the pool is saturated that band rarely empties, so Regular / Free-tier requests already queued in the lowest band usually sit until the agents' 8 s `wget` timeout. Until late on 2026-10-03 an agent's retries kept the tier of its first try, so those requests timed out three times and failed: 41 of 8,284 in a rehearsal run that started in Stage 3 (the driver had kept the last stage), and 42 of 8,156 in a test that switched from Stage 3 to Stage 2 on purpose (25 s in Stage 3, 40 s in Stage 2). The driver now sends each retry with the stage active at that moment. The same test then had 0 failed of 8,142 (41 requests needed one retry), and a run clicking back to Stage 2 on the dashboard had 0 failed of 9,141 (43 retried once); the lowest band was empty 8–11 s after the switch. Going forward (1 → 2 → 3) avoids the wait; to start over, Suspend all and press `1`.

**Operational risks:**
- **Unrestorable snapshot.** Once in about 13,000 pause/restore cycles, an agent's app died just before a pause. gVisor checkpointed it with no error, and every later restore failed (`inconsistent private memory files on restore`). "Wake 1,000" then stops at 999. Two more appeared on 2026-10-02, taken while their workers were being OOM-killed. The driver now marks such an agent, and `reconcile` re-creates it (§7).
- **Worker memory headroom is small.** Each worker has a 256 MiB limit.
  - The old driver's retries hit it: 255 workers were OOM-killed in three waves. An agent whose worker dies is stuck PAUSING or DELETING, and Suspend all waits for it.
  - With at most 2 calls in flight per agent, worker peaks reached 187 MiB on 2026-10-02.
  - Raising the per-agent cap or the retry budget needs bigger workers. Changing the WorkerPool's memory recreates all 1,600 workers.
- **Zombie processes in snapshots.** The sandbox's PID 1 (the upstream `sandbox` binary, BusyBox userland) does not reap orphaned processes.
  - Two things cut off a `/process` call: atenet's route timeout (10 s by default, not overridden here) and a pause during a call. Either way the shell is killed, its `wget` becomes a zombie, and the zombie is checkpointed with the agent.
  - Agents collected up to about 75 zombies, checkpoints grew to 14 MiB, and Wake 1,000 slowed from about 1.9 s to 2.7–3.1 s.
  - The driver now avoids both triggers (`wget -T 8`, and it waits for an agent's calls before pausing it). `ops/ck_scan.py` shows checkpoint growth.
- **Long Simulate Traffic runs wear the fleet down.** One ran nonstop for 21 hours on 2026-10-06/07 (4.8 M LLM requests). It left:
  - 2 agents that could not be paused. One's sandbox died; the other's `runsc checkpoint` hung for 90 s. After that every pause of either failed within milliseconds (`exit status 128`). No worker restarted and the node logged no OOM kill. gVisor's logs are discarded (above), so the cause is unknown.
  - 1 agent with an unrestorable snapshot, which the duty cycle tried to wake more than 190 times;
  - 20 gVisor sandboxes that no agent owned (about one an hour), each in a worker that ate-api counted as free;
  - checkpoints of 4.5–8.0 MiB. With the stuck agents fixed, Wake 1,000 still took about 3.1 s and Suspend all about 3.5 s.

  Suspend all could not finish: it kept retrying the 2 stuck agents for about 25 s and then left them up, so its clock never stopped. The driver now marks an agent broken at its first failed checkpoint or restore, and skips it from then on (§7). §7 fixed the fleet: the 22 workers replaced, a reconcile, then all 1,000 agents re-created and rebalanced. The health check then woke all 1,000 in 1.76–1.85 s and suspended them in 1.53–1.56 s, with 0 failed. Before a show that follows a long run, do the same.
- **EPP in-flight leak and restarts.** The endpoint picker's in-flight count can leak. Flow-control saturation then stays high with no traffic (0.99 was seen), and llm-d sheds or queues calls. Its liveness probe (1 s timeout) also restarted it under load. If saturation stays above 0 while idle, restart it with `kubectl --context="${CTX_TPU}" rollout restart deployment/gaie-pd-epp`. The pod IP changes, so re-run `deploy-driver.sh` ([user guide §4.3](./USER_GUIDE.md#43-flow-control-saturation-stays-above-0-while-idle)).
- **Failed resumes hold workers.** After a failed resume, the patched ate-api's worker cache can keep that worker marked as taken until its next relist (every 5 minutes). Repeated failed wakes can therefore use up a node's free workers. This is why the driver no longer retries a failed restore.
- **vLLM is about half as fast as on 2026-09-26.** The vLLM pods were restarted on 2026-09-28 with `--max-model-len 65536` for the Hermes variant. Before that they ran 2048; the image digest and every other flag are unchanged.
  - Measured directly against each pod on 2026-10-02, with the driver's exact request: inter-token latency 6.0 ms at 1 request in flight, 15.4 ms at 32 and 30.5 ms at 64. At 64 a pod served 81.5 req/s with 752 ms mean latency. Both pods measured the same.
  - On 2026-09-26, with 2048, each pod served 124 req/s at 345 ms (§2.2).
  - The likely cause is the context length: vllm-torchtpu sizes the attention kernel's per-sequence page table and its tuned block sizes from `--max-model-len`. This has not been confirmed by an A/B test.
  - Every request since 2026-09-28 had under 5,000 prompt tokens, so a smaller `--max-model-len` (for example 8192) would serve both variants. Hermes reads its 64k window from the driver's proxy (`-context-length`), not from vLLM. A request longer than vLLM's limit would be rejected.
- **Stale checkpoints.** Deleting an actor leaves its local checkpoint on the node. After the 2026-10-02 re-creations there were 1,299 such directories (6.7 GiB across the 25 nodes), and 1,329 (6.0 GiB) after the 2026-10-07 re-creation. The node tuner's page-cache warmer still reads them every 20 s.
- **Node recreation destroys node-local snapshots.** This includes auto-upgrade, auto-repair and maintenance. Agents paused on the affected node can no longer be restored and must be re-created. Upstream also warns that actors awake when their worker dies go `CRASHED`.
  - **It happened on 2026-10-04:** GKE auto-upgraded both clusters to 1.35.8-gke.1380001 and recreated every node. The driver pod was gone, `postgres-0` could not be scheduled, the atelets crash-looped on Postgres's old IP, and no agent could wake. It was recovered with [user guide §4.8](./USER_GUIDE.md#48-gke-upgraded-the-cluster-every-node-recreated), which re-creates all 1,000 agents of both fleets.
  - **Auto-upgrade is still on for every pool in both demo clusters, and there is no maintenance exclusion.**
  - Consider `--no-enable-autoupgrade` on the node pools, and a maintenance exclusion, through the show.
- **Spot TPU nodes can be preempted.** The vLLM pod then restarts on a new node, which takes minutes. The pod IPs change, so re-run `deploy-driver.sh` ([user guide §4.1](./USER_GUIDE.md#41-vllm-or-epp-pods-restarted)).
  - On 2026-10-06 pod-1's Spot node (`tpu-v6e-spot`) was replaced at 08:07 UTC. The gateway kept serving both pods, but both drivers still had pod-1's old address and showed it down. So the dashboards' throughput and KV-cache hit counted pod-2 only until `deploy-driver.sh` and the Hermes start block re-pointed them, about 9.5 hours later.
- **`postgres-0` restarts** stop `http_srv`. Re-run `deploy-patched-binaries.sh` before anything restarts ate-api or atelet ([user guide §4.4](./USER_GUIDE.md#44-postgres-0-restarted)).
- **Wake-time margin is thin.** The node with the most agents sets the wake time (§2.1). Every re-created agent lands on a random node, so rebalance after re-creating many (§7).
- **`kubectl port-forward` can hang after the driver restarts.** It keeps running but logs `error creating forwarding stream … Timeout`, and the dashboard shows RECONNECTING. Stop it and start it again; `curl -m 6 localhost:8090/api/state` checks it ([user guide §4.6](./USER_GUIDE.md#46-dashboard-says-reconnecting)).

## 10. How this guide was verified

Done on 2026-09-26 against the live clusters. The rule was: run every build, deploy and demo step for real; check cluster, VPC and TPU creation read-only; recreate nothing. "As written" means the command block was copied out of this README (its §6 then; now the [user guide](./USER_GUIDE.md), where former §6.N is Step N) and run unchanged, with only the Step 0 variables set.

**2026-10-03 (three stages):** not a full re-verification. The changed gateway manifest and EPP values were applied to the running TPU cluster as in user guide [§5.4](./USER_GUIDE.md#54-envoy-gateway) and [§5.5](./USER_GUIDE.md#55-epp-values) (then §6.9, "Changing a running install"), the new driver and dashboard were copied into the running driver pod (checksums compared) and its `-epp` flag was updated in place, and the three stages were measured live by clicking the dashboard's own buttons in headless Chrome while `api/state` was polled every 2 s (top of §2).

**2026-10-03 (80% fleet idle):** the driver built from this folder (with `-fleet-idle-pct`) and the dashboard were copied into the running driver pod with the copy block that was then §6.10 (checksums compared); `/work/args` is unchanged, since 80 and the auto `-max-inflight` are the defaults. `api/state` then reported `fleet_idle_pct` 80, `duty_target` 200 and `max_inflight` 220. The three stages were measured twice, through the driver API and by clicking the dashboard in headless Chrome (top of §2). Nothing on the TPU side changed. The Hermes variant's driver was not touched.

**2026-10-03 evening (the same load in every stage):** the driver built from this folder (with `-overload-rate`) and the dashboard were copied into the running driver pod with the copy block that was then §6.10 (checksums compared), and the dashboard once more after the stage-title change; `/work/args` is unchanged. `api/state` then reported `stage_rates` 200 for every stage and `overload_rate` 0. The three stages were measured three times, once through the driver API and twice by clicking the dashboard in headless Chrome (top of §2). Nothing on the TPU side changed. The Hermes variant's driver was not touched; it serves the same dashboard file. Later that evening only the dashboard was copied again, for the banner's Output Tok/s, and one more dashboard run took the §2.3 screenshots.

**2026-10-03 late evening (retries follow the stage; the Agents panel can be hidden):** the driver built from this folder and the dashboard were copied into the running driver pod with the copy block that was then §6.10 (checksums compared); `/work/args` is unchanged. The §9 test (25 s in Stage 3, then 40 s in Stage 2, through the driver API) ran on the old driver and again on the new one. Then one run used the dashboard in headless Chrome with real mouse and keyboard input: `W`, `T`, `2`, `3`, the divider dragged all the way left, a click on Stage 2, `S` with the Agents panel hidden, and a double-click on the divider. It had 9,141 LLM requests with 0 failed (43 retried once); Wake 1,000 took 1,982 ms and Suspend all 1,323 ms. `shoot.mjs` on the mock gave all its screenshots with no console errors; only the new `04d_llmd_full_width.png` was added to `docs/images/`. Nothing on the TPU side changed. The Hermes variant's driver was not touched; it serves the same dashboard file. Then only the dashboard was copied again, so that windows taller than 16:9 fill their height: at 1920×1080 its layout matched the previous file box for box, on the mock and on the live page, and `shoot.mjs` again gave all its screenshots with no console errors (`07_1440x900.png` updated).

**2026-10-04 (user guide):** the build and deploy steps moved to the [user guide](./USER_GUIDE.md), with a check after every step. Step 3 was rebuilt from a fresh upstream clone (byte-identical), the Step 4 and Step 5 scripts' dry runs reported everything unchanged, `kubectl diff` and the Helm comparison found no spec differences, and the new `deploy-driver.sh` changed nothing on the live driver pod; its change paths were tested on a throwaway pod. Details, and what was not run: [user guide §7](./USER_GUIDE.md#7-how-this-guide-was-verified).

**2026-10-05 (recovery after the GKE upgrade; new dashboard):** GKE had auto-upgraded both clusters on 2026-10-04 and recreated every node (§9). The stack was recovered with [user guide §4.8](./USER_GUIDE.md#48-gke-upgraded-the-cluster-every-node-recreated):
- the control plane re-pinned, then `deploy-patched-binaries.sh`;
- `deploy-driver.sh` with `BIN_DIR`, with the same driver build;
- all 1,000 agents of both fleets re-created, and the light fleet rebalanced;
- the Hermes driver restarted with the same build and flags, and its agents taught again;
- both vLLM pods warmed up.

Details are in [user guide §7](./USER_GUIDE.md#7-how-this-guide-was-verified). After that only the dashboard changed:
- The redesigned `index.html` was copied with `deploy-driver.sh` ([user guide §5.1](./USER_GUIDE.md#51-dashboard)).
- One run clicked its own buttons in headless Chrome (top of §2; the live screenshots in §2.3).
- `shoot.mjs` on the mock gave all its screenshots in the main, edge and offline scenarios. The only console errors were the expected ones: the edge scenario's deliberate busy click and the offline scenario's refused connections.
- The Hermes dashboard serves the same file and was checked while idle.

Nothing on the TPU side was changed by hand.

**2026-10-07 (broken agents after a 21-hour run; light fleet re-created):** see §9 for what the run left behind.
- The 22 workers were found by hand, with the same check `ops/sandbox_scan.py` now makes, and deleted; the reconcile then re-created the 3 broken agents.
- The driver built from this folder (it marks broken agents, §7) was copied with `deploy-driver.sh` and `BIN_DIR`; `/work/args` is unchanged. A checkpoint failure can't be forced on the live cluster, so its new failure paths were tested only against a fake ate-api.
- All 1,000 light agents were deleted with §7's `kubectl ate … delete actor` call, 24 at a time, and re-created by the reconcile. The `seq … | xargs` wrapper in §7 was checked only with `echo`.
- Then the §7 health check, `rebalance.py 40 10`, and the health check twice more (numbers in §7 and §9). `ck_scan.py` then showed every checkpoint at 1.4 MiB.
- `ops/sandbox_scan.py`: run offline on the data saved during the incident, its selection picked the same 22 workers. Live, with all agents at rest, it found 0 sandboxes. With all 1,000 awake it matched each of the 1,000 sandboxes to its agent's worker. Its output has not yet been piped into a real delete.
- The Hermes fleet and driver were not touched. Nothing on the TPU side changed.

The table below is from 2026-09-26. Its step numbers are the user guide's (then README §6.N).

| Step | How it was checked | Result |
|---|---|---|
| Step 1 VPC | `gcloud compute networks describe`, `subnets describe`, `firewall-rules list` | Custom-mode VPC. Subnet 172.24.0.0/20 with private Google access and secondary ranges `pods` 172.28.0.0/14 and `services` 172.24.16.0/20 (GKE added one more for the Substrate cluster's pods). The internal-allow rule has an auto-generated name but the same direction, priority, source ranges and protocols. |
| Step 2 Substrate cluster | `gcloud container clusters describe`; the `setup-gcp` and `ate-setup` sources at `fa6d949` (subcommands and environment variables); the clone, checkout and `kubectl-ate` build lines as written; the template rendered with `envsubst` compared with `kubectl ate get actor-template`; the create line as written | Matches §3. The rendered template equals the live one field for field, and the create line returns `AlreadyExists` without changing it. `setup-gcp` and `ate-setup` were **not** run: they would modify or recreate the live cluster. |
| Step 3 Build | As written, in a new directory with a fresh clone of upstream | The patch applies cleanly. `runsc_fast`, the three binaries, both `.gz` files and `http_srv` are byte-identical to what runs in the cluster. |
| Step 4 `scale-control-plane.sh` + node tuner | As written: dry run, real run, node-tuner apply | Everything `unchanged`; no pod restarted |
| Step 5 `deploy-patched-binaries.sh` | As written, with the Step 3 build | Both binaries `unchanged`, served correctly over HTTP, specs unchanged, nothing restarted. An earlier real run, with a byte-different build of the same code, uploaded the binaries and restarted ate-api and all 25 atelets in 46 s. |
| Step 6 TPU cluster | `gcloud container clusters describe`, including its node pools | Matches §3 |
| Step 7 vLLM image | `gcloud artifacts docker images describe` at the digest in the manifest | Present, and both vLLM pods run that digest. **Not rebuilt** (TPU side is read-only). |
| Step 8 vLLM pods | `kubectl diff` of `prereqs.yaml`, the deployments and the render Service; a positive control confirmed `diff` catches changes | No differences |
| Step 9 llm-d | `kubectl diff` of the objectives and the gateway; `helm template` with the repo values against `helm get manifest`; `kubectl diff` of the v1.0.1 CRDs | No differences. All 7 Helm objects are identical. The only CRD difference is the GKE-managed `InferencePool`. |
| Step 10 driver | The copy block as then written (now `deploy-driver.sh`), including `kubectl apply` of `keynote-driver.yaml` | Same args as before. The driver restarted with the Step 3 build (`fdef79b4`) and saw 1,000 paused agents. The pod itself was not recreated. |
| Step 11 reconcile | The `reconcile`, wait and `note` lines as written | Works: `re-created 0 … 1000 paused, 0 not at rest`. All 1,000 agents already existed, so the create path was not exercised. |
| Step 12 mock | `mock_server.py`, `curl` of the API, `shoot.mjs` as written | All 13 screenshots, no console errors. This check found a stale wait in `shoot.mjs` that timed out at the suspend step; it is fixed. |
| §7 health check | As written | 1,000 RUNNING in 3,085 ms with 0 failed; then 1,000 PAUSED in 2,440 ms, and 0 sandboxes on the 25 nodes |
| Full demo cycle | Driver API plus ground truth (ate-api states, sandbox processes on the nodes) | See §2: Balanced → 80/20 → Priority → Balanced, 21,681 LLM requests with 0 failed, 1,000 PAUSED and 0 sandboxes after suspend |

**Not run:**
- cluster, VPC and node-pool creation;
- the vLLM image build;
- `setup-gcp` and `ate-setup`;
- Step 11's creation of new agents (all 1,000 already existed);
- the teardown (now [user guide §6](./USER_GUIDE.md#6-tear-it-down)).

## 11. Cleanup and revert

Moved to the user guide on 2026-10-04: [§6 Tear it down](./USER_GUIDE.md#6-tear-it-down). It either keeps the clusters and undoes the demo's changes, or deletes everything: both clusters, the snapshot bucket, the vLLM image, the firewall rule, the subnet and the VPC. None of it was run on the demo clusters.
