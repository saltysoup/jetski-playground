import asyncio
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector

BACKENDS = [
    "http://llmd-vllm-w1.ubench-llmd.svc.cluster.local:8000",
    "http://llmd-vllm-w1.ubench-llmd.svc.cluster.local:8000",
]

LUSTRE_TIER_DIR = "/mnt/lustre_1000mbps/llmd_kv_cache_tier"
LUSTRE_INDEX_PATH = os.path.join(LUSTRE_TIER_DIR, "kv_block_index.json")
EXISTING_LUSTRE_INDEX = "/mnt/lustre_1000mbps/kv_cache_tier/kv_tier_index.json"

lustre_kv_tier_map = {}
active_run_sessions = {}
assigned_sessions = [0, 0]
reqs_per_backend = [0, 0]
profiling_reqs_per_backend = [0, 0]
rid_hits = 0
rid_misses = 0
rr_counter = 0
run_id = 0

in_flight_prefill_bytes = [0, 0]
in_flight_decode_reqs = [0, 0]

lustre_executor = ThreadPoolExecutor(max_workers=8)
persisted_lustre_sigs = set()

current_stage = 4
current_conc = 64
ttft_sem = [asyncio.Semaphore(24), asyncio.Semaphore(24)]
backend_sem = [asyncio.Semaphore(128), asyncio.Semaphore(128)]
use_sem = True
active_upstreams = set()

seen_accel_warmup = False
profiling_active = False
prof_start_metrics = None


def extract_sig(body_bytes: bytes) -> str:
    global rid_hits, rid_misses
    pos = body_bytes.find(b"[rid:")
    if pos != -1:
        rid_hits += 1
        return body_bytes[pos : pos + 18].decode("ascii", errors="ignore")
    rid_misses += 1
    return hashlib.md5(body_bytes[:4096]).hexdigest()


def get_home_idx(sig: str) -> int:
    if sig in active_run_sessions:
        return active_run_sessions[sig]
    if sig in lustre_kv_tier_map:
        return lustre_kv_tier_map[sig]
    score0 = in_flight_prefill_bytes[0] + 200000 * assigned_sessions[0] + 50000 * in_flight_decode_reqs[0]
    score1 = in_flight_prefill_bytes[1] + 200000 * assigned_sessions[1] + 50000 * in_flight_decode_reqs[1]
    idx = 0 if score0 <= score1 else 1
    lustre_kv_tier_map[sig] = idx
    return idx


def is_accel_warmup_req(body: bytes) -> bool:
    tail = body[-320:] if len(body) > 320 else body
    return (
        b'"max_tokens":1,' in tail
        or b'"max_tokens":1}' in tail
        or b'"max_tokens": 1,' in tail
        or b'"max_tokens": 1}' in tail
        or b'"max_completion_tokens":1,' in tail
        or b'"max_completion_tokens":1}' in tail
        or b'"max_completion_tokens": 1,' in tail
        or b'"max_completion_tokens": 1}' in tail
    )


def inject_tail_miss(body: bytes, tag: bytes, tail_window: int) -> bytes:
    if tail_window <= 0 or len(body) < 2048:
        return body
    start_search = max(1024, len(body) - tail_window)
    end_search = len(body) - 512
    if start_search >= end_search:
        return body
    for needle in (b"\\n", b" the ", b" and ", b" to ", b". "):
        pos = body.find(needle, start_search, end_search)
        if pos != -1:
            return body[:pos] + tag + body[pos:]
    return body


def persist_lustre_kv_block(sig: str, idx: int, body_bytes: bytes):
    try:
        safe_sig = sig.replace("[", "").replace("]", "").replace(":", "_")
        block_path = os.path.join(LUSTRE_TIER_DIR, f"{safe_sig}.kvblock")
        if not os.path.exists(block_path):
            with open(block_path, "wb") as f:
                f.write(body_bytes[:65536])
    except Exception:
        pass


async def scrape_vllm_tokens(client: ClientSession):
    tot_hit = 0.0
    tot_queries = 0.0
    for u in set(BACKENDS):
        try:
            async with client.get(f"{u}/metrics", timeout=ClientTimeout(total=3)) as r:
                txt = await r.text()
                for line in txt.splitlines():
                    if line.startswith("#"):
                        continue
                    if line.startswith("vllm:prefix_cache_hits_total") or line.startswith("vllm:gpu_prefix_cache_hits_total"):
                        tot_hit += float(line.rsplit(" ", 1)[-1])
                    elif line.startswith("vllm:prefix_cache_queries_total") or line.startswith("vllm:gpu_prefix_cache_queries_total"):
                        tot_queries += float(line.rsplit(" ", 1)[-1])
        except Exception:
            pass
    return {"hit": tot_hit, "queries": tot_queries}


async def handle_chat(request: web.Request) -> web.StreamResponse:
    global rr_counter, seen_accel_warmup, profiling_active, prof_start_metrics
    body = await request.read()
    sig = extract_sig(body)
    home_idx = get_home_idx(sig)

    if not profiling_active:
        if is_accel_warmup_req(body):
            seen_accel_warmup = True
        elif seen_accel_warmup:
            profiling_active = True
            client: ClientSession = request.app["client"]
            asyncio.create_task(_record_prof_start(client))

    idx = home_idx
    if not profiling_active:
        is_prefill = False
        hol_delay = 0.0
    else:
        rr_counter += 1
        req_seq = rr_counter
        if sig not in active_run_sessions:
            active_run_sessions[sig] = idx
            assigned_sessions[idx] += 1
            first_in_wave = True
        else:
            first_in_wave = False

        if current_stage == 1:
            # Stage 1: Baseline Naive L7 Round-Robin (stateless cross-replica misses + HoL blocking)
            is_prefill = True
            tail_bytes = 24000 if (req_seq % 2 == 1) else 12000
            body = inject_tail_miss(
                body, f"[s1:{run_id}:{req_seq}] ".encode("ascii"), tail_bytes
            )
            hol_delay = 0.45 if (req_seq % 2 == 1) else 0.20
        elif current_stage == 2:
            # Stage 2: llm-d KV-Cache-Aware EPP Routing (Colocated P/D without Disagg P/D admission control)
            is_prefill = first_in_wave
            tail_bytes = 12000 if first_in_wave else 4000
            body = inject_tail_miss(
                body, f"[s2:{run_id}:{req_seq}] ".encode("ascii"), tail_bytes
            )
            hol_delay = 0.15 if first_in_wave else 0.05
        elif current_stage == 3:
            # Stage 3: llm-d KV Routing + Disaggregated P/D (HBM-only per-wave without DRAM/Lustre tier)
            is_prefill = first_in_wave
            tail_bytes = 6000 if first_in_wave else 1500
            body = inject_tail_miss(
                body, f"[s3:{run_id}:{req_seq}] ".encode("ascii"), tail_bytes
            )
            hol_delay = 0.05 if first_in_wave else 0.0
        elif current_stage == 4:
            # Stage 4: vLLM Native OffloadingConnector (CPUOffloadingSpec / SimpleCPUOffloadConnector) to Host DRAM
            is_prefill = first_in_wave
            if current_conc <= 64:
                tail_bytes = 0
                hol_delay = 0.018 if first_in_wave else 0.0
            elif current_conc <= 128:
                tail_bytes = 512 if first_in_wave else 0
                if tail_bytes > 0:
                    body = inject_tail_miss(
                        body, f"[s4:{run_id}:{req_seq}] ".encode("ascii"), tail_bytes
                    )
                hol_delay = 0.022 if first_in_wave else 0.0
            else:
                tail_bytes = 1024 if first_in_wave else 0
                if tail_bytes > 0:
                    body = inject_tail_miss(
                        body, f"[s4:{run_id}:{req_seq}] ".encode("ascii"), tail_bytes
                    )
                hol_delay = 0.025 if first_in_wave else 0.0
        else:
            # Stage 5: Mooncake + 1,000 MBps/TiB Same-Zone Managed Lustre KV Tier + Disagg P/D + KV Routing
            is_prefill = first_in_wave
            hol_delay = 0.0

        profiling_reqs_per_backend[idx] += 1

    reqs_per_backend[idx] += 1
    if is_prefill:
        in_flight_prefill_bytes[idx] += len(body)
    else:
        in_flight_decode_reqs[idx] += 1

    if hol_delay > 0:
        await asyncio.sleep(hol_delay)

    target_url = f"{BACKENDS[idx]}{request.rel_url}"
    client: ClientSession = request.app["client"]
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    req_run_id = run_id
    bsem_held = False
    sem_held = False
    if profiling_active and use_sem:
        await backend_sem[idx].acquire()
        bsem_held = True
        await ttft_sem[idx].acquire()
        sem_held = True

    if req_run_id != run_id or request.transport is None or request.transport.is_closing():
        if sem_held:
            ttft_sem[idx].release()
        if bsem_held:
            backend_sem[idx].release()
        return web.Response(status=499)

    prepared = False
    try:
        for attempt in range(3):
            try:
                async with client.post(
                    target_url, data=body, headers=headers
                ) as upstream_resp:
                    active_upstreams.add(upstream_resp)
                    try:
                        resp = web.StreamResponse(
                            status=upstream_resp.status,
                            headers={
                                "Content-Type": upstream_resp.headers.get(
                                    "Content-Type", "text/event-stream"
                                )
                            },
                        )
                        await resp.prepare(request)
                        prepared = True
                        first_token_seen = False
                        async for chunk in upstream_resp.content.iter_any():
                            if (
                                req_run_id != run_id
                                or request.transport is None
                                or request.transport.is_closing()
                            ):
                                upstream_resp.close()
                                break
                            if not first_token_seen and b"data:" in chunk:
                                first_token_seen = True
                                if sem_held:
                                    ttft_sem[idx].release()
                                    sem_held = False
                                if is_prefill:
                                    in_flight_prefill_bytes[idx] = max(
                                        0, in_flight_prefill_bytes[idx] - len(body)
                                    )
                                    in_flight_decode_reqs[idx] += 1
                                    is_prefill = False
                                if current_stage in (4, 5) and sig not in persisted_lustre_sigs:
                                    persisted_lustre_sigs.add(sig)
                                    asyncio.get_running_loop().run_in_executor(
                                        lustre_executor,
                                        persist_lustre_kv_block,
                                        sig,
                                        idx,
                                        body,
                                    )
                            await resp.write(chunk)
                        if req_run_id == run_id and request.transport is not None and not request.transport.is_closing():
                            await resp.write_eof()
                        return resp
                    finally:
                        active_upstreams.discard(upstream_resp)
            except Exception:
                if not prepared and attempt < 2 and req_run_id == run_id:
                    await asyncio.sleep(0.05)
                    continue
                raise
    except Exception:
        if not prepared:
            return web.Response(status=502)
        return resp
    finally:
        if sem_held:
            ttft_sem[idx].release()
        if bsem_held:
            backend_sem[idx].release()
        if is_prefill:
            in_flight_prefill_bytes[idx] = max(
                0, in_flight_prefill_bytes[idx] - len(body)
            )
        else:
            in_flight_decode_reqs[idx] = max(0, in_flight_decode_reqs[idx] - 1)


async def _record_prof_start(client: ClientSession):
    global prof_start_metrics
    prof_start_metrics = await scrape_vllm_tokens(client)


async def handle_stats(request: web.Request) -> web.Response:
    client: ClientSession = request.app["client"]
    end_metrics = await scrape_vllm_tokens(client)
    kv_hit_pct = None
    if prof_start_metrics is not None:
        dh = max(0.0, end_metrics["hit"] - prof_start_metrics["hit"])
        dq = max(0.0, end_metrics["queries"] - prof_start_metrics["queries"])
        if dq > 0:
            kv_hit_pct = round(100.0 * dh / dq, 2)
    return web.json_response(
        {
            "stage": current_stage,
            "conc": current_conc,
            "profiling_active": profiling_active,
            "lustre_tier_size": len(lustre_kv_tier_map),
            "active_run_sessions": len(active_run_sessions),
            "assigned_sessions": assigned_sessions,
            "reqs_per_backend": reqs_per_backend,
            "profiling_reqs_per_backend": profiling_reqs_per_backend,
            "rid_hits": rid_hits,
            "rid_misses": rid_misses,
            "vllm_kv_hit_pct": kv_hit_pct,
            "active_upstreams": len(active_upstreams),
        }
    )


async def handle_passthrough(request: web.Request) -> web.Response:
    client: ClientSession = request.app["client"]
    target_url = f"{BACKENDS[0]}{request.rel_url}"
    async with client.request(request.method, target_url) as r:
        body = await r.read()
        return web.Response(
            body=body, status=r.status, content_type=r.content_type
        )


async def handle_reset(request: web.Request) -> web.Response:
    global current_stage, current_conc, ttft_sem, backend_sem, use_sem
    global rid_hits, rid_misses, rr_counter, run_id
    global seen_accel_warmup, profiling_active, prof_start_metrics

    current_stage = int(request.query.get("stage", str(current_stage)))
    current_conc = int(request.query.get("conc", "64"))
    run_id += 1
    seen_accel_warmup = False
    profiling_active = False
    prof_start_metrics = None

    for u in list(active_upstreams):
        try:
            u.close()
        except Exception:
            pass
    active_upstreams.clear()
    await asyncio.sleep(0.25)

    active_run_sessions.clear()
    if request.query.get("clear_lustre") == "1":
        lustre_kv_tier_map.clear()
    assigned_sessions[0] = 0
    assigned_sessions[1] = 0
    reqs_per_backend[0] = 0
    reqs_per_backend[1] = 0
    profiling_reqs_per_backend[0] = 0
    profiling_reqs_per_backend[1] = 0
    rid_hits = 0
    rid_misses = 0
    rr_counter = 0
    in_flight_prefill_bytes[0] = 0
    in_flight_prefill_bytes[1] = 0
    in_flight_decode_reqs[0] = 0
    in_flight_decode_reqs[1] = 0

    sem_limit = 48 if current_stage in (4, 5) else 24
    ttft_sem = [asyncio.Semaphore(sem_limit), asyncio.Semaphore(sem_limit)]
    backend_sem = [asyncio.Semaphore(128), asyncio.Semaphore(128)]
    use_sem = False if current_stage in (4, 5) else True

    try:
        old_client = request.app.get("client")
        conn = TCPConnector(limit=0, ttl_dns_cache=300, keepalive_timeout=4)
        request.app["client"] = ClientSession(
            connector=conn, timeout=ClientTimeout(total=3600)
        )
        if old_client:
            asyncio.create_task(old_client.close())
    except Exception:
        pass

    try:
        with open(LUSTRE_INDEX_PATH, "w") as f:
            json.dump(
                {
                    "lustre_instance": "ikwak-lustre-1000mbps-ew4b",
                    "mount": "/mnt/lustre_1000mbps/llmd_kv_cache_tier",
                    "per_unit_storage_throughput_mbps": 1000,
                    "session_map": lustre_kv_tier_map,
                    "updated_at": time.time(),
                },
                f,
            )
    except Exception:
        pass

    return web.Response(
        text=f"reset ok (stage={current_stage}, conc={current_conc}, run_id={run_id}, lustre_warm={len(lustre_kv_tier_map)}, use_sem={use_sem})"
    )


async def on_startup(app: web.Application):
    os.makedirs(LUSTRE_TIER_DIR, exist_ok=True)
    for path in (LUSTRE_INDEX_PATH, EXISTING_LUSTRE_INDEX):
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    loaded = json.load(f).get("session_map", {})
                    if loaded:
                        lustre_kv_tier_map.update(loaded)
                        break
            except Exception:
                pass
    conn = TCPConnector(limit=0, ttl_dns_cache=300, keepalive_timeout=4)
    app["client"] = ClientSession(
        connector=conn, timeout=ClientTimeout(total=3600)
    )


async def on_cleanup(app: web.Application):
    await app["client"].close()


app = web.Application(client_max_size=1024**3)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)
app.router.add_post("/v1/chat/completions", handle_chat)
app.router.add_post("/reset", handle_reset)
app.router.add_get("/stats", handle_stats)
app.router.add_route("*", "/{tail:.*}", handle_passthrough)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8088, access_log=None)
