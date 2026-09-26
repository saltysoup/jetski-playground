# Engineering notes

How the demo reaches a ~3 s wake of 1,000 agents, what was changed in Agent Substrate and why, what the driver does, and what went wrong along the way. The measured results and the step-by-step guide are in the [README](./README.md). The status and open items are in [plan.md](./plan.md).

**Contents:** [1. The wake path](#1-the-wake-path) · [2. The ate-api / atelet patch](#2-the-ate-api--atelet-patch) · [3. The runsc_fast wrapper](#3-the-runsc_fast-wrapper) · [4. The keynote driver](#4-the-keynote-driver) · [5. llm-d configuration](#5-llm-d-configuration) · [6. Incidents and lessons](#6-incidents-and-lessons) · [7. How results were measured](#7-how-results-were-measured)

## 1. The wake path

1. **Driver.** It calls `ResumeActor` for all 1,000 agents at once, spread over 32 gRPC connections to ate-api. The clock starts at T+0, when all 1,000 calls are released together, and stops when the last one returns. A call returns when ate-api reports that agent RUNNING.
2. **ate-api.** It binds the actor to a pre-warmed worker pod on the node that holds the actor's paused snapshot, then asks that node's atelet to restore it.
3. **atelet.** It prepares the actor's directories, then has the worker's ateom run `runsc restore` through the `runsc_fast` wrapper. At most `restoreSem` restores run at once on a node.
4. **ate-api** marks the actor RUNNING only when the restore has returned.

**How the wake time came down.** 25 × c3-standard-4 nodes, about 40 agents per node.

| Configuration | Wake 1,000 | Source |
|---|---|---|
| Stock v0.1.0 | 12.2 s | earlier project notes, not re-run |
| + control-plane sizing (Postgres, ate-api, atenet) | 4.95 s | earlier project notes, not re-run |
| + the patch below, synchronous `runsc_fast`, `restoreSem` 8 | 3.66 s (per-agent p50 1.95 s) | earlier project notes, not re-run |
| + `restoreSem` 12, driver v4 (**deployed**) | median 2.99 s, 2.90–3.29 s over 10 wakes | measured 2026-09-26 (README §2) |

**What sets the ~3 s tail now: placement.**
- An agent that rests with `PauseActor` keeps its snapshot on its node's local disk, so it can only wake on that node.
- Today's placement is uneven, at 32–47 agents per node.
- Nodes with 40 or fewer agents finish by about 2.45 s. The 2–3 nodes with 46–47 agents take 2.8–3.2 s.
- Across nodes, the number of agents correlates with the node's slowest wake at 0.89.
- We found no disk, memory or Postgres pressure that would explain the tail otherwise.

**The first wake after an agent is created is slower.** It restores from the template's golden snapshot in GCS instead of the local disk. The first 1,000-agent wake after all agents were re-created took 3.25 s, and one re-created agent took 2.33 s on its own.

## 2. The ate-api / atelet patch

The patch is [`patches/ateapi-atelet-fast-wake.patch`](./patches/ateapi-atelet-fast-wake.patch), against `agent-substrate/substrate@fa6d949` (v0.1.0). It changes 17 files (+431 / −247 lines). The common thread is to take synchronous Postgres round trips, fsyncs and redundant filesystem work out of `ResumeActor`.

### ate-api

| Where | Change | Risk |
|---|---|---|
| `controlapi/actor_template.go`, `store/atepg/atepg.go` | In-process caches for ActorTemplates. The control API's cache has a 2 s TTL. The store's cache has no TTL; it is updated on this process's own creates and updates, and cleared on deletes. | Needs exactly one ate-api replica: another replica would never see those invalidations. |
| `controlapi/workflow_resume.go` | `loadActorForResume` reuses the actor it already loaded instead of reading it again. | – |
| `controlapi/workflow_resume.go` | `BindActorToWorker` (the row-locked bind) runs in a goroutine while the restore proceeds. `finalizeRunning` waits for it, so RUNNING still requires both the bind and the restore; we checked this. | A failed bind is discovered after the restore work has started. |
| `controlapi/workflow_resume.go` | The contention retry backoff is 2 ms → 20 ms cap (25 steps) instead of 15 ms → 250 ms (12 steps). The lease is closed in the background. | More retries under heavy contention. |
| `controlapi/actor.go`, `store/atepg/atepg.go` | `UpdateActorFast` writes the final RUNNING state from the caller's copy, without a re-read. | Relies on the lease for exclusivity. |
| `scheduling/scheduling.go`, `workercache/workercache.go` | The scheduler parses the constraints once per call, holds a mutex while it picks, and marks the chosen worker full in the cache right away (`MarkFull`). That way concurrent binds don't all pick the same worker. | The cache may briefly under-report free capacity. |
| `store/atepg/outbox.go` | Outbox poll interval 50 → 5 ms. | More idle Postgres queries. |
| `controlapi/dialer.go` | A mutex around the atelet connection-cache lookup and dial, so concurrent resumes don't open duplicate connections to one atelet. | – |
| `main.go` | JWT verification results cached for 20 s. Default log level `warn`. | A revoked token keeps working for up to 20 s. Fewer logs. |

### atelet and shared packages

| Where | Change | Risk |
|---|---|---|
| `cmd/atelet/main.go` | `restoreSem = 12`: at most 12 `RestoreWorkload` calls at once per node. | – |
| `cmd/atelet/main.go` | Skips `resetActorDirs` when the actor directories are already clean. | – |
| `cmd/atelet/main.go` | Local checkpoints are hard-linked instead of copied, with a copy as the fallback. | Both paths share one inode; safe as long as nothing writes those files in place. |
| `cmd/atelet/main.go` | Local restores don't rewrite the sandbox record if it exists. | – |
| `cmd/atelet/main.go` | `writeFileAtomic` became a plain `os.WriteFile`, with no temp file, rename or fsync. | Torn files after a crash. |
| `cmd/atelet/main.go` | A mutex around the ateom connection cache; when two calls dial at once, the extra connection is closed. | – |
| `cmd/atelet/oci.go` | The bundle directory is cleared only if it exists. Image lookup and image-volume resolution run one after the other, and the volume step returns early when there are none. | – |
| `cmd/atelet/sandbox_assets.go` | Embeds `runsc_fast` (built from `runsc_fast_sync.c`), writes it next to the fetched `runsc` as `runsc-fast` once, and uses it for every call. | See §3. |
| `internal/imagecache` | Resolved images are cached in memory by reference and by digest. Concurrent HEAD requests for one reference are merged. | A moved tag is not re-resolved until atelet restarts. The demo pins the image by digest. |
| `internal/imagecache/spec.go`, `internal/ocispec` | Compact JSON. The overlay spec is written without temp file and rename. | Torn file after a crash. |

**How it is deployed.**
- The stock release images (`v0.1.0-gke.1`) are kept.
- [`deploy-patched-binaries.sh`](./manifests/substrate/deploy-patched-binaries.sh) adds `fetch-bin` init containers that download the patched binaries from a small file server in `postgres-0`.
- The file server is a known security problem (plan.md open item 5).

## 3. The runsc_fast wrapper

[`runsc_fast_sync.c`](./patches/runsc_fast_sync.c) is a static C program that atelet installs as `runsc-fast`. For each call it:
- execs the real `runsc` with extra flags: `--shared-root=/tmp/runsc-shared-root --gofer-network-namespace=host --host-settings=ignore --restore-spec-validation=ignore -log=/dev/null`;
- drops `--alsologtostderr` and `-direct`;
- sets `GOMAXPROCS=2`, so that 12 concurrent restores on a 4-vCPU node don't each spin up a full set of Go threads.

It waits for `runsc` to finish, so a restore still means a running sandbox.

**Costs:**
- The host network namespace for the gofer, ignored host settings and skipped restore-spec validation all weaken gVisor's isolation and checks.
- `-log=/dev/null` throws away gVisor's logs. That's why the unrestorable-snapshot incident (§6) could not be root-caused.

[`runsc_fast_async.c`](./patches/runsc_fast_async.c) is a failed experiment; do not use it (§6).

## 4. The keynote driver

[`substrate-bench/keynote_driver/main.go`](./substrate-bench/keynote_driver/main.go) is one Go binary. It serves the dashboard and a JSON API on `:8090`, and it orchestrates the agents.

**API:**
- `GET api/state`, polled by the page every 250 ms;
- `POST api/burst` (`{"hold":true,"wake_only":true}` for Wake Agents);
- `POST api/simulate_traffic`, `api/traffic`, `api/strategy`, `api/suspend` and `api/reconcile`;
- `GET api/summary` and `api/events`.

**Wake Agents.**
- `ResumeActor` for all 1,000 agents at once.
- When every agent is already at rest, the pre-burst `ListActors` preflight is skipped (about 0.5 s saved before T+0).
- No LLM calls are made.

**Simulate Traffic (duty cycle).**
- 96 workers loop: pick a random paused agent → `ResumeActor` → hold it "running" for 70–140 ms so the state is visible on the grid → run one LLM request inside its sandbox → `PauseActor`.
- That keeps about 100 agents active and about 90% idle.
- Priority mode raises the load to 300 req/s. The extra 200 req/s go to agents that are already awake.

**One LLM request.**
- The driver POSTs to `atenet-router` `/process` for the agent. The sandbox runs a shell script that calls the llm-d gateway with `wget`, adding the routing header for the current strategy.
- The script writes the reply to per-request temp files and saves `/tmp/agent_memory.json` atomically.
- Failed attempts are retried up to 35 times, and every retry is counted (`retried`, `failed_attempts`, `retry_reasons`).
- Once the driver has started putting that agent to rest, the request is abandoned instead of retried.

**Suspend all.**
- Claims every agent, waits for in-flight duty-cycle wakes, pauses and requests (5 s cap per request), then pauses everything that is up, 200 at a time.
- Then it **cross-checks with ate-api**, and pauses anything still up.
- The suspend clock runs from the click and includes that fix-up.

**llm-d metrics.**
- Every 0.5 s the driver scrapes vLLM `/metrics` on both pods and the EPP's `/metrics`.
- Rates, means and the pod split are computed over a 3 s window.

**Bugs found and fixed while verifying** (v1 was the driver as found):

| Version | Bug | Fix |
|---|---|---|
| v2 | Suspend all left agents running: 980 PAUSED and 20 RUNNING, while the dashboard showed 0. A duty-cycle wake that landed after the click was recorded as suspended but never paused. | Count in-flight duty wakes and pauses and wait for them; count in-flight requests per agent; claim agents before queuing; add the ate-api cross-check; time from the click. |
| v3 | Retries were invisible. With up to 35 attempts, "0 failed" hid problems. | Retry counters in `api/state` and in the run summary |
| v4 | In Priority mode, concurrent requests to one agent overwrote each other's output files (`/tmp/llmd_resp.json`): 6.9% retries. Retries that outlived the pause then silently **woke paused agents again**, because atenet's ingress calls `ResumeActor` for every request. | Per-request temp files and an atomic memory file; no retries once an agent is being put to rest. Retries fell from 969 to 7, and those 7 are genuine llm-d 503s. |

## 5. llm-d configuration

The Helm values are in [`manifests/tpu/gaie-values-flowctl.yaml`](./manifests/tpu/gaie-values-flowctl.yaml): EPP `v0.10.0`, chart `inferencepool` v1.2.0.

**Scorers (weight):**
- `prefix-cache-scorer` (3), fed by the `precise-prefix-cache-producer`, which indexes vLLM's KV-cache events (ZMQ, port 5557);
- `active-request-scorer` (2);
- `kv-cache-utilization-scorer` (2);
- `header-label-affinity-scorer`, named `target-pod-affinity` (100). It scores 1 for the pod whose `llm-d.ai/replica` label equals the `x-target-pod` header, and the weight outvotes all the others combined. In Steer mode the driver labels 80% of requests `pod-1` and 20% `pod-2`.

**Why not `queue-scorer`:** it reads vLLM's waiting-queue gauge, which lags. During a 1,000-request burst it kept choosing the same pod and sent about 750 requests in a row there (86.5/13.5). `active-request-scorer` counts requests the EPP itself has dispatched, so it reacts immediately.

**Flow control:**
- Priority bands 100 / 0 / −10 (premium / standard / best-effort, from [`inference-objectives.yaml`](./manifests/tpu/inference-objectives.yaml)).
- A concurrency-based saturation detector at 256 in-flight requests per pod (vLLM's default `max_num_seqs` on these pods).
- At 300 req/s the pool stays well below that (saturation median 0.2, max 0.45). Queues stay short, at up to 31 requests, and every band waits 19 ms or less, so the bands don't separate (README §2.2).

**Prefix cache:** every agent sends the same 286-token system prompt, so about 90% of prompt tokens are served from the prefix cache. Each agent's own user prompt and temperature 1.0 keep the replies distinct.

## 6. Incidents and lessons

1. **The asynchronous runsc wrapper faked a 1.83 s wake.**
   - What happened: `runsc_fast_async.c` queued `runsc restore` in the background and returned at once. ate-api marked agents RUNNING before their sandboxes existed.
   - The next Suspend all left every agent stuck in PAUSING, with 999 orphaned gVisor sandboxes and corrupt runsc state.
   - Recovery: kill the orphans, recycle 613 worker pods, then run the driver's reconcile, which recreated the agents with new UIDs.
   - The `.gvisor.filestore` errors seen at the time came from that stale state. `--overlay2=none` was not the fix.
   - *Lesson:* a latency win must be confirmed by what is actually running on the nodes.
2. **An earlier plan misdiagnosed the problem.**
   - It proposed skipping rootfs setup in atelet's `oci.go`. But ateom composes the rootfs overlay itself, in its privileged pod, and the proposed code called `SetupBundleRootfs` with the wrong signature, so it did not compile.
   - The "skip if `config.json` exists" path would never have run either, because checkpoint and restore reset the actor directories.
   - *Lesson:* check the premise against the code at the exact commit before writing the fix.
3. **The dashboard's own counters were not ground truth.**
   - Suspend all showed 0 running while 20 agents were RUNNING, and its timer skipped the drain.
   - Retries were invisible.
   - *Lesson:* every run is now checked against ate-api and the node processes (§7), and the driver cross-checks ate-api itself.
4. **One agent's snapshot became unrestorable.**
   - The agent's app reset its connections 0.1 s before a pause, and gVisor checkpointed it **with no error**.
   - Every later restore failed with `inconsistent private memory files on restore: savedMFOwners = [_pause:/]`: the snapshot held only the pause container.
   - Effect: "Wake 1,000" stopped at 999 and Suspend all waited 3.8 s.
   - Repair: delete the agent, reconcile, and do a warm-up wake. That is the manual procedure in README §7.
   - Frequency: once in about 13,000 pause/restore cycles.
   - The cause of the app's death is unknown: there was no panic output, and gVisor's logs go to `/dev/null` (§3).
5. **`restoreSem` 16 was tried and rolled back.** Three 60 s cycles at 16 against 12, both with 0 failures: wake 2,992–3,326 ms at 16 against 2,902–3,286 ms at 12, and a worse per-agent p50. 12 stays.
6. **A 14 s window with an empty binary.**
   - During one manual upload, the served `bin_atelet.gz` was an empty file for 14 s. The rename happened before the checksum check.
   - Nothing restarted in that window.
   - `deploy-patched-binaries.sh` now uploads to a temporary name, checks the sha256, and only then renames.
7. **`kubectl cp` can report success on a truncated copy.** Always compare checksums before switching binaries. The driver upgrade block in README §6.10 does.

## 7. How results were measured

**Every run was checked in three ways:**
- **ate-api's actor states:** `kubectl ate get actors -a ate-demo-sandbox -o json`, counted by state.
- **Real gVisor sandboxes on the nodes:** processes whose `argv[0]` is `runsc-sandbox`, counted on each of the 25 nodes from a hostPID pod.
- **The driver's own records:** per-agent wake latency, milestones, the ramp per 1,000 ms, and LLM request, retry and abandon counts, from `api/state` and `api/summary`.

**Wake time** runs from T+0 to the last successful `ResumeActor`, which is when ate-api reports that agent RUNNING. T+0 is when the driver releases all 1,000 calls at once, about 20 ms after the API call when every agent is already at rest.

**llm-d numbers** are medians over each phase, excluding the first 8 s after a strategy switch.
