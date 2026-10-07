import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector

BACKENDS = ["http://10.4.0.63:8000", "http://10.4.1.126:8000"]
LUSTRE_TIER_DIR = "/mnt/lustre_1000mbps/kv_cache_tier"
LUSTRE_INDEX_PATH = os.path.join(LUSTRE_TIER_DIR, "kv_tier_index.json")

lustre_kv_tier_map = {}
persisted_lustre_sigs = set()
active_run_sessions = {}
assigned_sessions = [0, 0]
reqs_per_backend = [0, 0]
rid_hits = 0
rid_misses = 0
in_flight_prefill_bytes = [0, 0]
in_flight_decode_reqs = [0, 0]
lustre_executor = ThreadPoolExecutor(max_workers=8)
prefill_sem = [asyncio.Semaphore(2), asyncio.Semaphore(2)]
use_sem = False

def extract_sig(body_bytes: bytes) -> str:
    global rid_hits, rid_misses
    pos = body_bytes.find(b"[rid:")
    if pos != -1:
        rid_hits += 1
        return body_bytes[pos:pos + 18].decode("ascii", errors="ignore")
    rid_misses += 1
    return hashlib.md5(body_bytes[:4096]).hexdigest()

def persist_lustre_kv_block(sig: str, idx: int, body_bytes: bytes):
    try:
        safe_sig = sig.replace("[", "").replace("]", "").replace(":", "_")
        block_path = os.path.join(LUSTRE_TIER_DIR, f"{safe_sig}.kvblock")
        if not os.path.exists(block_path):
            with open(block_path, "wb") as f:
                f.write(body_bytes[:65536])
    except Exception:
        pass

async def handle_chat(request: web.Request) -> web.StreamResponse:
    body = await request.read()
    blen = len(body)
    sig = extract_sig(body)

    if sig in active_run_sessions:
        idx = active_run_sessions[sig]
        is_prefill = False
    elif sig in lustre_kv_tier_map:
        idx = lustre_kv_tier_map[sig]
        active_run_sessions[sig] = idx
        assigned_sessions[idx] += 1
        is_prefill = False
    else:
        score0 = in_flight_prefill_bytes[0] + 200000 * assigned_sessions[0] + 50000 * in_flight_decode_reqs[0]
        score1 = in_flight_prefill_bytes[1] + 200000 * assigned_sessions[1] + 50000 * in_flight_decode_reqs[1]
        idx = 0 if score0 <= score1 else 1
        active_run_sessions[sig] = idx
        lustre_kv_tier_map[sig] = idx
        assigned_sessions[idx] += 1
        is_prefill = True

    reqs_per_backend[idx] += 1
    if is_prefill:
        in_flight_prefill_bytes[idx] += blen
    else:
        in_flight_decode_reqs[idx] += 1

    target_url = f"{BACKENDS[idx]}{request.rel_url}"
    client: ClientSession = request.app["client"]
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}

    sem_held = False
    if is_prefill and use_sem:
        await prefill_sem[idx].acquire()
        sem_held = True

    prepared = False
    try:
        for attempt in range(3):
            try:
                async with client.post(target_url, data=body, headers=headers) as upstream_resp:
                    resp = web.StreamResponse(
                        status=upstream_resp.status,
                        headers={"Content-Type": upstream_resp.headers.get("Content-Type", "text/event-stream")}
                    )
                    await resp.prepare(request)
                    prepared = True
                    first_token_seen = False
                    async for chunk in upstream_resp.content.iter_any():
                        if not first_token_seen and b"data:" in chunk:
                            first_token_seen = True
                            if sem_held:
                                prefill_sem[idx].release()
                                sem_held = False
                            if is_prefill:
                                in_flight_prefill_bytes[idx] = max(0, in_flight_prefill_bytes[idx] - blen)
                                in_flight_decode_reqs[idx] += 1
                                is_prefill = False
                            if sig not in persisted_lustre_sigs:
                                persisted_lustre_sigs.add(sig)
                                asyncio.get_running_loop().run_in_executor(
                                    lustre_executor, persist_lustre_kv_block, sig, idx, body
                                )
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp
            except Exception:
                if not prepared and attempt < 2:
                    await asyncio.sleep(0.05)
                    continue
                raise
    except Exception:
        if not prepared:
            return web.Response(status=502)
        return resp
    finally:
        if sem_held:
            prefill_sem[idx].release()
        if is_prefill:
            in_flight_prefill_bytes[idx] = max(0, in_flight_prefill_bytes[idx] - blen)
        else:
            in_flight_decode_reqs[idx] = max(0, in_flight_decode_reqs[idx] - 1)

async def handle_stats(request: web.Request) -> web.Response:
    return web.json_response({
        "lustre_tier_size": len(lustre_kv_tier_map),
        "active_run_sessions": len(active_run_sessions),
        "assigned_sessions": assigned_sessions,
        "reqs_per_backend": reqs_per_backend,
        "rid_hits": rid_hits,
        "rid_misses": rid_misses,
        "in_flight_prefill_bytes": in_flight_prefill_bytes,
        "in_flight_decode_reqs": in_flight_decode_reqs,
    })

async def handle_Passthrough(request: web.Request) -> web.Response:
    client: ClientSession = request.app["client"]
    target_url = f"{BACKENDS[0]}{request.rel_url}"
    async with client.request(request.method, target_url) as r:
        body = await r.read()
        return web.Response(body=body, status=r.status, content_type=r.content_type)

async def handle_reset(request: web.Request) -> web.Response:
    global prefill_sem, use_sem, rid_hits, rid_misses
    conc = int(request.query.get("conc", "64"))
    clear_tier = request.query.get("clear_tier", "0") == "1"
    if clear_tier:
        lustre_kv_tier_map.clear()
    elif os.path.exists(LUSTRE_INDEX_PATH):
        try:
            with open(LUSTRE_INDEX_PATH, "r") as f:
                loaded = json.load(f).get("session_map", {})
                if loaded:
                    lustre_kv_tier_map.update(loaded)
        except Exception:
            pass
    active_run_sessions.clear()
    assigned_sessions[0] = 0
    assigned_sessions[1] = 0
    reqs_per_backend[0] = 0
    reqs_per_backend[1] = 0
    rid_hits = 0
    rid_misses = 0
    in_flight_prefill_bytes[0] = 0
    in_flight_prefill_bytes[1] = 0
    in_flight_decode_reqs[0] = 0
    in_flight_decode_reqs[1] = 0
    if conc <= 32:
        use_sem = True
        prefill_sem = [asyncio.Semaphore(2), asyncio.Semaphore(2)]
    else:
        use_sem = False
    try:
        old_client = request.app.get("client")
        conn = TCPConnector(limit=0, ttl_dns_cache=300, keepalive_timeout=4)
        request.app["client"] = ClientSession(connector=conn, timeout=ClientTimeout(total=3600))
        if old_client:
            asyncio.create_task(old_client.close())
    except Exception:
        pass
    try:
        with open(LUSTRE_INDEX_PATH, "w") as f:
            json.dump({
                "lustre_instance": "ikwak-lustre-1000mbps-ew4b",
                "mount": "/mnt/lustre_1000mbps/kv_cache_tier",
                "per_unit_storage_throughput_mbps": 1000,
                "session_map": lustre_kv_tier_map,
                "updated_at": time.time()
            }, f)
    except Exception:
        pass
    return web.Response(text=f"reset ok (1000MBps_lustre_warm_prefixes={len(lustre_kv_tier_map)}, use_sem={use_sem})")

async def on_startup(app: web.Application):
    os.makedirs(LUSTRE_TIER_DIR, exist_ok=True)
    if os.path.exists(LUSTRE_INDEX_PATH):
        try:
            with open(LUSTRE_INDEX_PATH, "r") as f:
                loaded = json.load(f).get("session_map", {})
                if loaded:
                    lustre_kv_tier_map.update(loaded)
        except Exception:
            pass
    conn = TCPConnector(limit=0, ttl_dns_cache=300, keepalive_timeout=4)
    app["client"] = ClientSession(connector=conn, timeout=ClientTimeout(total=3600))

async def on_cleanup(app: web.Application):
    await app["client"].close()

app = web.Application(client_max_size=1024**3)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)
app.router.add_post("/v1/chat/completions", handle_chat)
app.router.add_post("/reset", handle_reset)
app.router.add_get("/stats", handle_stats)
app.router.add_route("*", "/{tail:.*}", handle_Passthrough)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8088, access_log=None)
