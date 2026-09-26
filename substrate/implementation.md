# Agent Substrate × `llm-d` Implementation & Reproduction Guide (`implementation.md`)

> **Handoff Implementation Reference**  
> This document details every code change made across the repository and live cluster, how each component is built and deployed, and the exact commands to complete and verify the remaining `atelet` rootfs + fast-wake rollout.

---

## 1. Summary of Files & Changes Implemented

### 1.1 Repository Layout (`/usr/local/google/home/ikwak/jetski-playground/substrate/`)

| File Path | Status | Description |
|---|---|---|
| `dashboard/_parts/a.html`, `_parts/b.js`, `_parts/c.js`, `index.html` | **Complete** | Stage UI with **Wake Agents** (`skip_jokes: true`), **Simulate Traffic** (`POST /api/simulate_traffic`), **FLEET IDLE RATE %** hero metric (`90%` during traffic, `100%` at rest, `0%` when all 1,000 awake), Click-to-Magnify joke spotlight, and Paid vs Free Priority flow control. |
| `dashboard/mock_server.py` | **Complete** | Rehearsal simulation server (`:8765`) implementing `350–400 agents/s` wake (`~2.58s` for 1,000 agents) and continuous random agent duty-cycle rotation (`waking` $\to$ `running` $\to$ `requesting` $\to$ `suspending` $\to$ `suspended`) at `~90%` Fleet Idle Rate. |
| `substrate-bench/keynote_driver/main.go` | **Complete & Deployed to `:8090`** | Live cluster Go orchestrator (`:8090`). Includes preflight skip when all 1,000 actors are at rest (`~780ms` saved before `T+0`) and fixed `Simulate Traffic` random agent duty-cycle rotation (`startDutyCycleLocked`, `dutyPauseOne`, `dutyWakeAndRequest`, `dutyLoop`, `trafficLoop`). |
| `patches/ateapi-atelet-fast-wake.patch` | **Saved in Repo** | Git diff against `/tmp/substrate-src` containing all `cmd/ateapi` and `cmd/atelet` optimizations. |
| `patches/runsc_fast_sync.c` | **Saved in Repo** | Synchronous C wrapper around `runsc` (`--shared-root=/tmp/runsc-shared-root --gofer-network-namespace=host --host-settings=ignore --restore-spec-validation=ignore -log=/dev/null`, `GOMAXPROCS=2`). Tested at **`3,664 ms`** for `0 → 1,000` wakes and **`3,376 ms`** for `1,000 → 0` checkpoints (`0` failures). |
| `patches/runsc_fast_async.c` | **Saved in Repo** | Experimental asynchronous queueing C wrapper around `runsc` (with `--overlay2=none`). Tested at **`1,832 ms`** for `0 → 1,000` wakes. |

---

## 2. Detailed Walkthrough of Code Changes

### 2.1 Live Cluster Driver (`substrate-bench/keynote_driver/main.go`)

1. **Fast Preflight Skip on `Wake Agents` (`runBurst`, lines 911–955)**:
   * Checks if all 1,000 entries in `d.states` are already `stSuspended`.
   * If `alreadyAtRest == 1000`, skips `d.preflight(ctx)` (which otherwise makes `2 × ListActors` gRPC calls taking `~500ms`) and immediately marks `b.pausedAtT0 = 1000`.
   * Reduces the pre-burst gate sleep from `300ms` to `20ms`.

2. **Random Agent Duty Cycle on `Simulate Traffic` (`lines 1102–1334` & `1471–1546`)**:
   * **`startDutyCycleLocked` (lines 1102–1163)**:
     * Shuffles all 1,000 agent indices.
     * `900` agents (`rank >= 100`) are immediately paused (`dutyPauseOne`) so the fleet drops from `1,000` running to `~100` active (`~90%` idle rate).
     * The initial `100` agents (`rank < 100`) fire `d.runFirstJoke(ctx, b, idx)` staggered over `0–600ms` and **immediately call `d.dutyPauseOne(ctx, b, idx)` afterward** (fixing the bug where those 100 agents remained in `stRunning` forever).
   * **`dutyPauseOne` (lines 1165–1189)**:
     * Accepts agents in either `stRunning` or `stRequesting`, transitions them to `stSuspending`, executes `PauseActor` (or `SuspendActor`), and marks them `stSuspended`.
   * **`dutyWakeAndRequest` & `dutyLoop` (lines 1191–1334)**:
     * Runs `96` concurrent worker goroutines (`targetActive = 100`).
     * Each iteration picks a random `stSuspended` agent across all 1,000 slots, transitions it `stWaking` $\to$ `ResumeActor` $\to$ `stRunning` (holds `70–140ms` so green is visible on the grid) $\to$ `stRequesting` (`askJoke` to `llm-d`) $\to$ `dutyPauseOne` (`stSuspending` $\to$ `stSuspended`).
   * **`trafficLoop` (lines 1471–1546)**:
     * When `d.b != nil && d.b.dutyCycle` is true, `trafficLoop` skips dispatching for `rate <= 100` because `dutyLoop` already drives the `~100 req/s` wake-request-suspend cycle. When `rate > 100` (`Priority` mode at `300 req/s`), `trafficLoop` dispatches the extra `200 req/s` without mutating `d.states`.

---

### 2.2 Control Plane Optimizations (`cmd/ateapi/` in `/tmp/substrate-src`)

Currently deployed to all 8 `ate-api-server` pods via ConfigMap `bin-ateapi-override`:
1. **`cmd/ateapi/internal/workercache/workercache.go`**:
   * Added `TryClaimPrewarmed(namespace, templateName) (Worker, bool)` to claim an idle pre-warmed worker pod from the in-memory cache without querying Postgres.
2. **`cmd/ateapi/internal/controlapi/workflow_resume.go` & `actor.go` & `actor_template.go`**:
   * Added in-memory `actorCache` (`sync.Map`) and `templateCache` (`sync.Map`) so `ResumeActor` resolves the `Actor` and `ActorTemplate` in memory when cached, claims a pre-warmed worker via `TryClaimPrewarmed`, calls `atelet.ResumeTask` directly over the pre-warmed gRPC connection pool, and writes the final `ACTOR_STATE_READY` update asynchronously.
3. **`cmd/ateapi/internal/controlapi/dialer.go` & `cmd/ateapi/internal/store/atepg/outbox.go`**:
   * Pre-establishes 4 gRPC HTTP/2 connections per `atelet` IP with 64MB window sizes.
   * Increased Postgres outbox `batchSize` to `256` and reduced `pollInterval` to `5ms`.

---

## 3. Completing the `atelet` Fast-Wake Rollout (Step-by-Step)

### 3.1 Why the Previous Experimental `atelet` Build Failed on `Suspend All`
1. The deployed `sandbox-workerpool` (`1,600` pods) runs `ateom-gvisor:v0.1.0-gke.1`, which does **not** mount `overlay.json` itself — it expects `atelet` to populate `bundles/_pause/rootfs` and `bundles/sandbox/rootfs` during `CreateActor` (`PrepareTask`).
2. However, on `ResumeActor` (`PauseActor` $\to$ `ResumeActor`), the actor's `bundles/_pause/rootfs` and `bundles/sandbox/rootfs` **already exist on the node's `/var/lib/ateom-gvisor/actors/<actorUID>` directory**!
3. Therefore, in `/tmp/substrate-src/cmd/atelet/oci.go`, `prepareOCIDirectory` should:
   * **Skip wiping and skip `SetupBundleRootfs` if `path.Join(bundlePath, "config.json")` already exists** (the fast path for `ResumeActor`, taking `0 ms`!).
   * **Run `imagecache.SetupBundleRootfs(ctx, bundlePath, img.LayerDirs)` when `config.json` does NOT exist yet** (the initial `CreateActor` path, so `ateom-gvisor` has a complete rootfs).

### 3.2 Exact Code Update for `/tmp/substrate-src/cmd/atelet/oci.go`

Replace lines `86–139` of `/tmp/substrate-src/cmd/atelet/oci.go` with:

```go
	bundlePath := ateompath.OCIBundlePath(actorUID, containerName)

	img, err := imageCache.EnsureImage(ctx, ref)
	if err != nil {
		return fmt.Errorf("in imageCache.EnsureImage: %w", err)
	}

	// Fast path for ResumeActor: if config.json and rootfs already exist on disk
	// from CreateActor / PauseActor, reuse the populated bundle rootfs in-place
	// (0 ms disk I/O) instead of wiping and re-extracting layers.
	if _, err := os.Stat(path.Join(bundlePath, "config.json")); err == nil {
		return nil
	}

	// Initial CreateActor path: populate bundle rootfs for ateom-gvisor.
	if _, err := os.Stat(bundlePath); err == nil {
		if err := imagecache.RemoveAllWritable(bundlePath); err != nil {
			return fmt.Errorf("while clearing bundle %q: %w", bundlePath, err)
		}
	}
	if err := imagecache.SetupBundleRootfs(ctx, bundlePath, img.LayerDirs); err != nil {
		return fmt.Errorf("in imagecache.SetupBundleRootfs: %w", err)
	}
	for _, vm := range volumeMounts {
		target := path.Join(bundlePath, "rootfs", vm.GetMountPath())
		if err := os.MkdirAll(target, 0o755); err != nil {
			return fmt.Errorf("in os.MkdirAll(%q): %w", target, err)
		}
	}

	// Argv and env need only the image config; resolve them before writing
	// any spec so an invalid container config fails fast.
	resolvedArgs, err := resolveProcessArgs(&img.Config, command, args)
	if err != nil {
		return fmt.Errorf("while resolving process args for container %q: %w", containerName, err)
	}
	resolvedEnv := resolveActorEnv(&img.Config, env)
```

*(Note: verify the signature of `imagecache.SetupBundleRootfs` in `/tmp/substrate-src/internal/imagecache/` or `git log -p -n 1 internal/imagecache/` in `/tmp/substrate-src` before compiling).*

### 3.3 Compile Synchronous `runsc_fast` (with `--overlay2=none`) & Build `atelet`

```bash
# 1. Compile synchronous runsc_fast with --overlay2=none into cmd/atelet/runsc_fast
gcc -O3 -static -s -o /tmp/substrate-src/cmd/atelet/runsc_fast \
  /usr/local/google/home/ikwak/jetski-playground/substrate/patches/runsc_fast_sync.c

# 2. Build atelet binary and compress
cd /tmp/substrate-src
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o /tmp/bin_atelet ./cmd/atelet
gzip -f -9 -c /tmp/bin_atelet > /tmp/bin_atelet.gz

# 3. Upload ConfigMap and restart atelet DaemonSet
export CTX_SUB="gke_tpu-launchpad-playground_asia-northeast1-b_ikwak-substrate-ane1"
kubectl --context="${CTX_SUB}" -n ate-system create configmap bin-atelet-override \
  --from-file=atelet.gz=/tmp/bin_atelet.gz --dry-run=client -o yaml \
  | kubectl --context="${CTX_SUB}" -n ate-system apply -f -

kubectl --context="${CTX_SUB}" -n ate-system rollout restart ds/atelet
kubectl --context="${CTX_SUB}" -n ate-system rollout status ds/atelet --timeout=180s
```

---

## 4. How to Build & Deploy `keynote_driver` (`:8090`)

Whenever you modify `/usr/local/google/home/ikwak/jetski-playground/substrate/substrate-bench/keynote_driver/main.go` or `dashboard/index.html`:

```bash
export CTX_SUB="gke_tpu-launchpad-playground_asia-northeast1-b_ikwak-substrate-ane1"

# 1. Copy updated source to /tmp/substrate-src and build static binary
cp /usr/local/google/home/ikwak/jetski-playground/substrate/substrate-bench/keynote_driver/main.go \
  /tmp/substrate-src/cmd/keynote_driver/main.go
(cd /tmp/substrate-src && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o /tmp/keynote_driver ./cmd/keynote_driver)

# 2. Upload binary (and optionally dashboard/index.html) to keynote-driver pod and restart process
kubectl --context="${CTX_SUB}" -n keynote-demo cp /tmp/keynote_driver keynote-driver:/work/keynote_driver.new
kubectl --context="${CTX_SUB}" -n keynote-demo cp /usr/local/google/home/ikwak/jetski-playground/substrate/dashboard/index.html keynote-driver:/work/static/index.html
kubectl --context="${CTX_SUB}" -n keynote-demo exec keynote-driver -- sh -c '
  chmod +x /work/keynote_driver.new &&
  mv /work/keynote_driver.new /work/keynote_driver &&
  kill $(pidof keynote_driver) 2>/dev/null || true
'
```

---

## 5. Verification & Reconcile Runbook on `:8090`

```bash
# 1. Ensure port-forward is running on :8090
kubectl --context="gke_tpu-launchpad-playground_asia-northeast1-b_ikwak-substrate-ane1" \
  -n keynote-demo port-forward --address 0.0.0.0 pod/keynote-driver 8090:8090 &

# 2. Reconcile all 1,000 actors so every actor has a clean rootfs and PAUSED checkpoint
curl -s -X POST http://localhost:8090/api/reconcile
# Poll until phase == "idle" and suspended == 1000:
watch -n 2 'curl -s http://localhost:8090/api/state | jq "{phase, running, suspended, failed, idle_rate_pct, note}"'

# 3. Test Wake Agents (0 -> 1,000)
curl -s -X POST http://localhost:8090/api/burst -H 'Content-Type: application/json' \
  -d '{"hold":true,"skip_jokes":true}'
# Check wake latency summary:
curl -s http://localhost:8090/api/state | jq '.summary'

# 4. Test Simulate Traffic (~90% Fleet Idle Rate, random agents waking -> requesting -> suspending)
curl -s -X POST http://localhost:8090/api/simulate_traffic

# 5. Test Suspend All (1,000 -> 0)
curl -s -X POST http://localhost:8090/api/suspend
```
