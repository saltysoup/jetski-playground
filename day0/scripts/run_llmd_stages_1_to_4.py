import json
import os
import subprocess
import time
import urllib.request

W1 = "http://llmd-vllm-w1.ubench-llmd.svc.cluster.local:8000"
W2 = "http://llmd-vllm-w2.ubench-llmd.svc.cluster.local:8000"
ROUTER = "http://127.0.0.1:8088"


def http_get(url, timeout=5):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="ignore")


def http_post(url, timeout=10):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="ignore")


def wait_for_backends():
    print("Waiting for llmd-vllm-w1 and llmd-vllm-w2 to become healthy...", flush=True)
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
            print("Both llmd-vllm-w1 and llmd-vllm-w2 are READY (200 OK)!", flush=True)
            break
        print(f"Status: w1={ok1}, w2={ok2} - sleeping 15s...", flush=True)
        time.sleep(15)


def save_vllm_kernel_caches():
    print("Saving compiled vLLM /root/.cache to Lustre for fast future restarts...", flush=True)


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
        kv_hit = d.get("router_stats", {}).get("vllm_kv_hit_pct")
        print(
            f"RESULT {tag}: out_pool={out_tput:.1f} ({out_tput/8.0:.2f}/chip), "
            f"tot_pool={tot_tput:.1f} ({tot_tput/8.0:.1f}/chip), "
            f"TTFT_P90={ttft_p90:.1f}ms, ITL_P90={itl_p90:.2f}ms, "
            f"E2E_Lat={e2e_lat_ms_tok:.2f}ms/tok, E2E_Int={e2e_int:.2f} tok/s/u, "
            f"kv_hit={kv_hit}%",
            flush=True,
        )
    else:
        print(f"WARNING: {json_path} not found!", flush=True)


def run_aiperf(tag, stage, conc, duration=30, grace=8.0):
    print(f"\n=======================================================", flush=True)
    print(f"Starting {tag} (stage={stage}, conc={conc}, duration={duration}s, grace={grace}s)", flush=True)
    print(f"=======================================================", flush=True)
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
        "--model", "deepseek-ai/DeepSeek-V4.1-Flash",
        "--tokenizer", "deepseek-ai/DeepSeek-V4.1-Flash",
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
        "--failed-request-threshold", "0.15",
        "--no-gpu-telemetry",
        "--no-auto-plot",
        "--output-artifact-dir", out_dir,
    ]
    with open(log_path, "w") as lf:
        subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, check=False)

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

    # Incremental hydration so both w1 and w2 have trajectory lanes warm in prefix cache
    run_aiperf("llmd_scaffold_hydrate_c64", stage=4, conc=64, duration=12)
    run_aiperf("llmd_scaffold_hydrate_c128", stage=4, conc=128, duration=15)
    run_aiperf("llmd_scaffold_hydrate_c256", stage=4, conc=256, duration=15)

    # Stage 4: 1,000 MBps/TiB Same-Zone Managed Lustre KV Tier + Disagg P/D + KV-Aware
    for c in [16, 32, 64, 128, 256]:
        run_aiperf(f"llmd_scaffold_stage4_1000mbps_c{c}", stage=4, conc=c, duration=30)

    # Stage 3: llm-d Disaggregated P/D + KV Routing (HBM-only per-wave)
    for c in [16, 32, 64, 128, 256]:
        run_aiperf(f"llmd_scaffold_stage3_pd_disagg_c{c}", stage=3, conc=c, duration=30)

    # Stage 2: llm-d KV-Cache-Aware Routing across 2x TP=4 vLLM replicas
    for c in [64, 128, 256]:
        run_aiperf(f"llmd_scaffold_stage2_kv_routing_c{c}", stage=2, conc=c, duration=30)

    # Stage 1: Naive L7 Round-Robin across 2x TP=4 vLLM replicas (8x B200)
    for c in [64, 128, 256]:
        run_aiperf(f"llmd_scaffold_stage1_naive_rr_c{c}", stage=1, conc=c, duration=30)

    print("\nALL UPGRADED LLM-D + VLLM STAGES 1 TO 4 COMPLETED SUCCESSFULLY!", flush=True)


if __name__ == "__main__":
    main()
