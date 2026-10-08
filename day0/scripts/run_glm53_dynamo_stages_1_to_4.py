import glob
import json
import os
import signal
import subprocess
import time
import urllib.request

W1 = "http://dynamo-sglang-w1.ubench-llmd.svc.cluster.local:8888"
W2 = "http://dynamo-sglang-w2.ubench-llmd.svc.cluster.local:8888"
ROUTER = "http://127.0.0.1:8089"
MODEL = "zai-org/GLM-5.3"
TOKENIZER = "/aiperf-env/models/zai-org/GLM-5.3"
NUM_GPUS = 16.0


def http_get(url, timeout=5):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="ignore")


def http_post(url, timeout=10):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="ignore")


def cleanup_stray_aiperf():
    my_pid = os.getpid()
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            pid = int(p.split("/")[2])
            if pid in (1, my_pid):
                continue
            cmd = open(p, "rb").read().decode("utf-8", "ignore")
            if ("/aiperf-env/venv/bin/aiperf" in cmd or "aiperf system_controller" in cmd or "aiperf worker" in cmd or "aiperf dataset_manager" in cmd or "aiperf records_manager" in cmd or "aiperf timing_manager" in cmd or "aiperf server_metrics" in cmd):
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def wait_for_backends():
    print("Waiting for GLM-5.3 dynamo-sglang-w1 (TP=8) and dynamo-sglang-w2 (TP=8) to become healthy...", flush=True)
    while True:
        ok1, ok2 = False, False
        try:
            s1, _ = http_get(f"{W1}/health", timeout=3)
            ok1 = (s1 == 200)
        except Exception:
            pass
        try:
            s2, _ = http_get(f"{W2}/health", timeout=3)
            ok2 = (s2 == 200)
        except Exception:
            pass
        if ok1 and ok2:
            print("Both GLM-5.3 dynamo-sglang-w1 and dynamo-sglang-w2 are READY (200 OK)!", flush=True)
            break
        print(f"Status: w1={ok1}, w2={ok2} - sleeping 15s...", flush=True)
        time.sleep(15)


def summarize_result(tag, out_dir):
    json_path = os.path.join(out_dir, "profile_export_aiperf.json")
    if os.path.exists(json_path):
        with open(json_path, "r") as f:
            d = json.load(f)
        out_tput = d.get("output_token_throughput", {}).get("avg", 0.0)
        tot_tput = (d.get("total_token_throughput") or {}).get("avg", 0.0)
        ttft_p90 = d.get("time_to_first_token", {}).get("p90", 0.0)
        itl_p90 = d.get("inter_token_latency", {}).get("p90", 0.0)
        osl_avg = d.get("output_sequence_length", {}).get("avg", 1.0)
        e2e_lat_ms_tok = (ttft_p90 + itl_p90 * max(1.0, osl_avg - 1.0)) / max(1.0, osl_avg)
        e2e_int = 1000.0 / e2e_lat_ms_tok if e2e_lat_ms_tok > 0 else 0.0
        kv_hit = d.get("router_stats", {}).get("sglang_kv_hit_pct")
        print(
            f"RESULT {tag}: out_pool={out_tput:.1f} ({out_tput/NUM_GPUS:.2f}/chip), "
            f"tot_pool={tot_tput:.1f} ({tot_tput/NUM_GPUS:.1f}/chip), "
            f"TTFT_P90={ttft_p90:.1f}ms, ITL_P90={itl_p90:.2f}ms, "
            f"E2E_Lat={e2e_lat_ms_tok:.2f}ms/tok, E2E_Int={e2e_int:.2f} tok/s/u, "
            f"kv_hit={kv_hit}%",
            flush=True,
        )
    else:
        print(f"WARNING: {json_path} not found!", flush=True)


def run_aiperf(tag, stage, conc, duration=30, grace=10.0):
    print(f"\n=======================================================", flush=True)
    print(f"Starting {tag} (stage={stage}, conc={conc}, duration={duration}s, grace={grace}s)", flush=True)
    print(f"=======================================================", flush=True)
    cleanup_stray_aiperf()
    _, reset_msg = http_post(f"{ROUTER}/reset?stage={stage}&conc={conc}")
    print(f"Router reset: {reset_msg}", flush=True)

    out_dir = f"/aiperf-env/results/{tag}"
    os.makedirs(out_dir, exist_ok=True)
    log_path = f"/aiperf-env/results/{tag}.log"

    cmd = [
        "/aiperf-env/venv/bin/aiperf",
        "profile",
        "--scenario", "inferencex-agentx-mvp",
        "--unsafe-override",
        "--model", MODEL,
        "--tokenizer", TOKENIZER,
        "--tokenizer-trust-remote-code",
        "--url", ROUTER,
        "--endpoint-type", "chat",
        "--streaming",
        "--use-server-token-count",
        "--public-dataset", "semianalysis_cc_traces_weka_062126",
        "--num-dataset-entries", "393",
        "--random-seed", "42",
        "--trace-idle-gap-cap-seconds", "8.0",
        "--system-idle-gap-cap-seconds", "0.5",
        "--cache-bust", "first_turn_prefix",
        "--concurrency", str(conc),
        "--benchmark-duration", str(duration),
        "--benchmark-grace-period", str(grace),
        "--warmup-requests-per-lane", "1",
        "--agentic-warmup-grace-period", "600.0",
        "--failed-request-threshold", "0.25",
        "--no-gpu-telemetry",
        "--no-auto-plot",
        "--output-artifact-dir", out_dir,
    ]
    with open(log_path, "w") as lf:
        try:
            subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, check=False, timeout=duration + 150)
        except subprocess.TimeoutExpired:
            print(f"Timeout expired for {tag}, cleaning up stray aiperf processes...", flush=True)
            cleanup_stray_aiperf()

    _, stats_txt = http_get(f"{ROUTER}/stats")
    print(f"Router stats after {tag}: {stats_txt}", flush=True)

    json_path = os.path.join(out_dir, "profile_export_aiperf.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r") as f:
                d = json.load(f)
            d["router_stats"] = json.loads(stats_txt)
            with open(json_path, "w") as f:
                json.dump(d, f, indent=2)
        except Exception:
            pass
    summarize_result(tag, out_dir)


def main():
    wait_for_backends()

    # Incremental hydration so both w1 and w2 have trajectory lanes warm in RadixCache + Lustre HiCache
    run_aiperf("glm53_dynamo_hydrate_c64", stage=4, conc=64, duration=12, grace=8.0)
    run_aiperf("glm53_dynamo_hydrate_c128", stage=4, conc=128, duration=15, grace=8.0)

    # Stage 4: 1,000 MBps/TiB Same-Zone Managed Lustre KV Tier + Disagg P/D + KV-Aware (2x TP=8 = 16x B200)
    for c in [16, 32, 64, 128]:
        run_aiperf(f"glm53_dynamo_stage4_1000mbps_c{c}", stage=4, conc=c, duration=30, grace=10.0)

    # Stage 3: NVIDIA Dynamo Disaggregated P/D + Real-Time Active-Load Balancing (HBM-only per-wave)
    for c in [16, 32, 64, 128]:
        run_aiperf(f"glm53_dynamo_stage3_pd_disagg_c{c}", stage=3, conc=c, duration=30, grace=10.0)

    # Stage 2: NVIDIA Dynamo KV-Cache-Aware Routing across 2x TP=8 SGLang replicas
    for c in [32, 64, 128]:
        run_aiperf(f"glm53_dynamo_stage2_kv_routing_c{c}", stage=2, conc=c, duration=30, grace=10.0)

    # Stage 1: Naive L7 Round-Robin across 2x TP=8 SGLang replicas (16x B200)
    for c in [32, 64, 128]:
        run_aiperf(f"glm53_dynamo_stage1_naive_rr_c{c}", stage=1, conc=c, duration=30, grace=10.0)

    print("\nALL GLM-5.3 DYNAMO + SGLANG STAGES 1 TO 4 COMPLETED SUCCESSFULLY!", flush=True)


if __name__ == "__main__":
    main()
