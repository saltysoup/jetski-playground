# Agent Substrate × `llm-d` Live Cluster Optimization Plan (`plan.md`)

> **Handoff Document for Next Agent / Session**  
> Read this file alongside [`implementation.md`](./implementation.md) and [`README.md`](./README.md) to understand the project goals, architectural findings, current state of both the simulation (`:8765`) and live GKE cluster (`:8090`), and the exact remaining steps.

---

## 1. Objectives & Target Metrics

We are closing the remaining behavioral and performance gaps between the **rehearsal simulation dashboard (`:8765`)** and the **live 1,000-sandbox GKE cluster dashboard (`:8090`)**:

| Requirement | Target Behavior | Simulation (`:8765`) Status | Live Cluster (`:8090`) Status |
|---|---|---|---|
| **1. `Wake Agents` (`0 → 1,000` sandboxes)** | Wake all 1,000 gVisor agent sandboxes in **`<= 3.0s`** (`300–400+ agents/s` throughput), holding at `1,000 / 1,000` running (`0%` Fleet Idle Rate) until `Simulate Traffic` is clicked. | **Done** (`~2.58s` / `~388 agents/s`) | **In Progress**: Improved from `12.2s` $\to$ `4.95s` $\to$ **`3.66s`** (sync `runsc_fast`) and **`1.83s`** (async `runsc_fast`). Needs final `rootfs` / `--overlay2=none` reconciliation (see Section 4). |
| **2. `Simulate Traffic` (~90% Fleet Idle Rate & Random Rotation)** | Transition fleet from `1,000` running to **~90% Fleet Idle Rate** (`~100` active / `~900` suspended). **Different random agents** across the `50×20` grid must continuously wake (`waking` $\to$ `running`), send 1 LLM request (`requesting`), and checkpoint back to rest (`suspending` $\to$ `suspended`). | **Done** (90% idle rate, continuous random turnover across all 1,000 slots) | **Code Complete & Deployed in `keynote_driver`**: Fixed bug where initial 100 agents stayed permanently `stRunning` and `trafficLoop` toggled the same 100 agents. Ready for E2E verification once `atelet` actors are reconciled. |
| **3. `Suspend All` (`1,000 → 0` Scale to Zero)** | Cleanly checkpoint all active agents (`PauseActor`) back to `0 / 1,000` running (`100%` Fleet Idle Rate) in `~3s` with `0` failed checkpoints. | **Done** | **Working with Sync `runsc_fast` (`3.38s`)**; failed under experimental async `runsc_fast` due to missing `SetupBundleRootfs` + `.gvisor.filestore._pause` mount collision (root cause & fix below). |

---

## 2. Environment & Cluster Topology

* **GCP Project**: `tpu-launchpad-playground` (Zone: `asia-northeast1-b`)
* **Cluster 1 — Agent Substrate (`ikwak-substrate-ane1`)**:
  * Kube context: `gke_tpu-launchpad-playground_asia-northeast1-b_ikwak-substrate-ane1`
  * Control plane pool: `4 × n2-standard-8` (`keynote-driver-pool`) running `8 × ate-api-server`, `4 × atenet-router`, `4 × atenet-egress`, `postgres-0`, and `keynote-driver` (in namespace `keynote-demo`).
  * Sandbox worker pool: `25 × c3-standard-4` (`substrate-node-pool`) running `25 × atelet` DaemonSet pods and `1,600 × sandbox-workerpool` pods (`64` per node, hosting `1,000` actors `agent-0001` .. `agent-1000` in namespace `ate-demo-sandbox`).
* **Cluster 2 — `llm-d` on Cloud TPU v6e (`ikwak-tpu-v6e-ane1`)**:
  * Kube context: `gke_tpu-launchpad-playground_asia-northeast1-b_ikwak-tpu-v6e-ane1`
  * Serving `2 × google/gemma-4-12B-it` (`vllm-torchtpu` on `2 × ct6e-standard-4t` TPU v6e-4) behind `gaie-pd-epp` (`v0.10.0`) and `llmd-envoy-gateway` (`10.146.0.45:8080`).
* **Dashboards**:
  * **Live Cluster Dashboard (`:8090`)**: `http://injaekwak.c.googlers.com:8090/` (port-forwarded from `keynote-demo/keynote-driver`).
  * **Simulation Dashboard (`:8765`)**: `http://injaekwak.c.googlers.com:8765/` (`python3 substrate/dashboard/mock_server.py 8765`).
* **Code Repositories on Workstation**:
  * Git repository (pushed to GitHub): `/usr/local/google/home/ikwak/jetski-playground/substrate/`
  * Agent Substrate Go checkout (used to compile `ateapi`, `atelet`, and `keynote_driver` binaries): `/tmp/substrate-src/`

---

## 3. Design Approach

### 3.1 How We Fixed the Live Cluster `Simulate Traffic` Pinning Bug
On the live cluster, the user observed that clicking **`Simulate Traffic`** showed ~90% idle rate, but the **same ~100 agents kept running and toggling between `running` and `requesting`** instead of random agents across the grid waking and suspending:
1. **Root Cause 1 (`startDutyCycleLocked` in `keynote_driver/main.go`)**:
   * When transitioning from `1,000` awake agents (`hold=true`) into the 90% idle duty cycle, `startDutyCycleLocked` paused 900 agents (`rank >= 100`) and had the remaining 100 agents (`rank < 100`) fire `runFirstJoke` — **but never paused those 100 agents afterward**. Those 100 agents remained in `stRunning` indefinitely.
2. **Root Cause 2 (`trafficLoop` vs `dutyLoop` conflict in `keynote_driver/main.go`)**:
   * `trafficLoop` (which runs whenever `trafficRate > 0`) continuously picked random agents currently in `stRunning`, flipped them to `stRequesting` during `askJoke`, and flipped them back to `stRunning`. Because `dutyPauseOne` only paused agents in `stRunning`, `trafficLoop` kept hijacking the active agents and visually pulsing the same ~100 squares.
3. **Fix Implemented & Deployed**:
   * In `startDutyCycleLocked`: the initial `rank < 100` agents now call `d.dutyPauseOne(ctx, b, idx)` immediately after `d.runFirstJoke(ctx, b, idx)` completes (staggered over `0–600ms`), so they also cycle into `stSuspended`.
   * In `dutyPauseOne`: accepts both `stRunning` and `stRequesting`.
   * In `dutyWakeAndRequest` & `dutyLoop`: 96 worker goroutines continuously pick random `stSuspended` agents across all 1,000 slots, wake them (`stWaking` $\to$ `stRunning`), hold `stRunning` for `70–140ms` so the green running state is visible on the grid, send one LLM joke request (`stRequesting`), and immediately pause (`stSuspending` $\to$ `stSuspended`).
   * In `trafficLoop`: when `d.b.dutyCycle` is active, normal `100 req/s` traffic is driven **exclusively by `dutyLoop`**. `trafficLoop` only fires supplementary background requests when `rate > 100` (e.g., `300 req/s` in `Priority` mode) without mutating `d.states`.

---

### 3.2 How We Accelerated Live Cluster `0 → 1,000` Wake (`12.2s → 3.66s → <3.0s`)
Waking 1,000 actors simultaneously (`40` actors per `c3-standard-4` 4-vCPU node) originally took `12.2s` (or `4.95s` after DB/API scaling). We systematically eliminated overhead at each layer:

1. **Orchestrator Layer (`keynote_driver/main.go`) — Saved `~780ms`**:
   * Skipped the `2 × ListActors` preflight RPCs (`~500ms`) in `runBurst` when all 1,000 local agent states are already `stSuspended`, and reduced the pre-burst gate sleep from `300ms` to `20ms`.
2. **Control Plane Layer (`cmd/ateapi/`) — Saved `~1,200ms`**:
   * Added in-memory `actorCache` and `templateCache` in `controlapi` plus `workercache.TryClaimPrewarmed` so `ResumeActor` claims a pre-warmed worker pod in memory and skips 4 synchronous Postgres round-trips.
   * Increased Postgres outbox batch size (`32 → 256`), reduced poll interval (`50ms → 5ms`), and pre-warmed a 4-connection gRPC pool per `atelet` node.
3. **Node Daemon Layer (`cmd/atelet/`) + `runsc_fast` Wrapper — Saved `~1,800ms+`**:
   * Added in-memory `imagecache.memHit` so `EnsureImage` doesn't re-parse OCI JSON manifests from disk on every wake.
   * Added `actorDirsAlreadyReset` fast-path on `ResumeActor` so `PrepareTask` does not wipe and recreate bundle directories that already exist from `PauseActor`.
   * Added `runsc_fast` C wrapper passing `--shared-root=/tmp/runsc-shared-root`, `--gofer-network-namespace=host`, `--host-settings=ignore`, `--restore-spec-validation=ignore`, `-log=/dev/null`, and `GOMAXPROCS=2`.
   * **Result with Synchronous `runsc_fast_sync.c` + `restoreSem = 8`**:
     * `0 → 1,000` wake completed in **`3,664 ms` (`p50 = 1,954 ms`, `min = 278 ms`, `0` failed)**.
     * `1,000 → 0` `PauseActor` suspend completed in **`3,376 ms` (`0` failed)**.

---

### 3.3 Root Cause of the Experimental `atelet` Failure & How to Finish `< 3.0s` Cleanly
When we tested two further changes in `atelet` (`cmd/atelet/oci.go` skipping `imagecache.SetupBundleRootfs` in favor of `overlay.json`, and `runsc_fast_async.c` backgrounding `runsc restore`), we hit two issues during `Suspend All` / `Reconcile`:
1. **Why `SetupBundleRootfs` cannot be skipped on `CreateActor` with the deployed `sandbox-workerpool` (`v0.1.0-gke.1`)**:
   * The 1,600 running `sandbox-workerpool` pods run `ateom-gvisor` image `v0.1.0-gke.1`, which predates `overlay.json` mounting in `ateom`. It expects `atelet` to populate `bundles/_pause/rootfs` and `bundles/sandbox/rootfs` on `/var/lib/ateom-gvisor`.
   * However, once `PrepareTask` populates `rootfs/` on initial creation (`CreateActor`), **`ResumeActor` does NOT need to wipe or re-untar `rootfs/`** as long as `prepareOCIDirectory` checks if `rootfs/bin` (or `config.json`) already exists and skips `RemoveAllWritable` + `SetupBundleRootfs` on `ResumeActor`!
2. **Why `runsc restore sandbox` failed with `.gvisor.filestore._pause` when `rootfs` was a plain directory**:
   * `runsc` defaults to `--overlay2=root:self`, which creates `.gvisor.filestore.<container_name>` at the mount source root (`/var/lib/ateom-gvisor`). When both `_pause` and `sandbox` were restored on the same hostPath mount source, `sandbox` failed with:
     `creating gofer filestore files: mount source already has a filestore file ".gvisor.filestore._pause"; repeated submounts are not supported with overlay optimizations`
   * Passing `--overlay2=none` in `runsc_fast` disables `.gvisor.filestore.*` creation on the shared mount source.

---

## 4. Concrete Action Plan for the Next Agent

Follow the exact commands in [`implementation.md`](./implementation.md) to execute these 3 steps:

### Step 1: Restore `SetupBundleRootfs` (Create-Only) in `cmd/atelet/oci.go` + Use `runsc_fast_sync.c` (with `--overlay2=none`)
1. In `/tmp/substrate-src/cmd/atelet/oci.go`, restore `imagecache.SetupBundleRootfs` when `bundlePath/rootfs` is empty (initial `CreateActor`), **but skip `RemoveAllWritable` and `SetupBundleRootfs` when `bundlePath/config.json` already exists** (during `ResumeActor`). That way:
   * Initial `CreateActor` (`Reconcile`) properly populates `bundles/_pause/rootfs` and `bundles/sandbox/rootfs` for `ateom-gvisor` `v0.1.0-gke.1`.
   * Subsequent `ResumeActor` calls take **0 ms** in `prepareOCIDirectory` (zero disk untar/copy on wake!).
2. Compile `substrate/patches/runsc_fast_sync.c` (with `--overlay2=none` added) to `/tmp/substrate-src/cmd/atelet/runsc_fast`, set `restoreSem` in `cmd/atelet/main.go` to `12` (or `16`), rebuild `bin_atelet.gz`, and roll out to the `atelet` DaemonSet.

### Step 2: Reconcile All 1,000 Actors on `:8090`
1. Trigger `curl -s -X POST http://localhost:8090/api/reconcile` (or click `Reconcile` in the UI).
2. Wait ~25s for any broken actors to be deleted, recreated with full `rootfs/`, and paused to `ACTOR_STATE_PAUSED` (`1,000 / 1,000` suspended, `0` failed).

### Step 3: End-to-End Verify on `:8090`
1. **Test `Wake Agents`**:
   * Trigger `curl -s -X POST http://localhost:8090/api/burst -d '{"hold":true,"skip_jokes":true}'`.
   * Verify `0 → 1,000` wake time is `~2.5s–3.0s` (`0` failed).
2. **Test `Simulate Traffic` (~90% Fleet Idle Rate)**:
   * Trigger `curl -s -X POST http://localhost:8090/api/simulate_traffic`.
   * Verify `idle_rate_pct` settles around `~90%` (`~100` active / `~900` suspended), random agents across all 1,000 grid cells transition through `waking` $\to$ `running` $\to$ `requesting` $\to$ `suspending` $\to$ `suspended`, and jokes stream into the Replies ticker.
3. **Test `Suspend All`**:
   * Trigger `curl -s -X POST http://localhost:8090/api/suspend`.
   * Verify all active agents checkpoint cleanly back to `0` running / `1,000` suspended (`100%` idle rate, `0` failed).
