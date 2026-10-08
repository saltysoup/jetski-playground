import glob
import json
import os
import signal
import subprocess
import time
import urllib.request

DYNAMO_ROUTER = "http://127.0.0.1:8089"
LLMD_ROUTER = "http://127.0.0.1:8088"


def http_get(url, timeout=5):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="ignore")


def http_post(url, timeout=10):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="ignore")


def cleanup_aiperf():
    my_pid = os.getpid()
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            pid = int(p.split("/")[2])
            if pid in (1, my_pid):
                continue
            cmd = open(p, "rb").read().decode("utf-8", errors="ignore").replace("\x00", " ")
            if "/aiperf-env/venv/bin/aiperf" in cmd or "aiperf system_controller" in cmd or "aiperf worker_" in cmd:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


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
        rstats = d.get("router_stats", {})
        kv_hit = rstats.get("sglang_kv_hit_pct") or rstats.get("vllm_kv_hit_pct")
        print(
            f"RESULT {tag}: out_pool={out_tput:.1f} ({out_tput/8.0:.2f}/chip), "
            f"tot_pool={tot_tput:.1f} ({tot_tput/8.0:.1f}/chip), "
            f"TTFT_P90={ttft_p90:.1f}ms, ITL_P90={itl_p90:.2f}ms, "
            f"E2E_Lat={e2e_lat_ms_tok:.2f}ms/tok, E2E_Int={e2e_int:.2f} tok/s/u, kv_hit={kv_hit}%",
            flush=True,
        )
    else:
        print(f"WARNING: {json_path} not found!", flush=True)


def run_aiperf(tag, router, stage, conc, duration=28, grace=8.0):
    print(f"\n=======================================================", flush=True)
    print(f"Starting {tag} (router={router}, stage={stage}, conc={conc}, duration={duration}s, grace={grace}s)", flush=True)
    print(f"=======================================================", flush=True)
    cleanup_aiperf()
    time.sleep(1)
    _, reset_msg = http_post(f"{router}/reset?stage={stage}&conc={conc}")
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
        "--url", router,
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
            subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, check=False, timeout=duration + 330)
        except subprocess.TimeoutExpired:
            print(f"Timeout expired for {tag}, cleaning up...", flush=True)
            cleanup_aiperf()

    _, stats_txt = http_get(f"{router}/stats")
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
    # 1. Re-run llmd_scaffold_stage4_native_dram_c256 with wave-entry-only DRAM eviction delta
    run_aiperf("llmd_scaffold_stage4_native_dram_c256", LLMD_ROUTER, stage=4, conc=256, duration=28, grace=8.0)

    # 2. Complete remaining NVIDIA Dynamo + SGLang Stage 4 & Stage 5 runs
    for c in [64, 128]:
        run_aiperf(f"dynamo_stage4_hicache_dram_c{c}", DYNAMO_ROUTER, stage=4, conc=c, duration=28, grace=8.0)
    for c in [16, 64, 128]:
        run_aiperf(f"dynamo_stage5_mooncake_lustre_c{c}", DYNAMO_ROUTER, stage=5, conc=c, duration=28, grace=8.0)

    print("\nALL REMAINING DSV4 STAGE 4 & STAGE 5 RUNS COMPLETED!", flush=True)


if __name__ == "__main__":
    main()
