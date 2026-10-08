import glob
import json
import os
import signal
import subprocess
import time
import urllib.request

DYNAMO_W1 = "http://dynamo-sglang-w1.ubench-llmd.svc.cluster.local:8888"
DYNAMO_W2 = "http://dynamo-sglang-w2.ubench-llmd.svc.cluster.local:8888"
DYNAMO_ROUTER = "http://127.0.0.1:8089"

LLMD_W1 = "http://llmd-vllm-w1.ubench-llmd.svc.cluster.local:8000"
LLMD_W2 = "http://llmd-vllm-w2.ubench-llmd.svc.cluster.local:8000"
LLMD_ROUTER = "http://127.0.0.1:8088"

MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"
TOKENIZER = "deepseek-ai/DeepSeek-V4.1-Flash"
NUM_GPUS = 8.0


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
            if (
                "/aiperf-env/venv/bin/aiperf" in cmd
                or "aiperf system_controller" in cmd
                or "aiperf worker" in cmd
                or "aiperf dataset_manager" in cmd
                or "aiperf records_manager" in cmd
                or "aiperf timing_manager" in cmd
                or "aiperf server_metrics" in cmd
            ):
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def wait_for_all_backends():
    print(
        "Waiting for BOTH Dynamo SGLang (w1, w2) and llm-d vLLM (w1, w2) to become READY so compilation does not contend with benchmarking...",
        flush=True,
    )
    while True:
        statuses = {}
        for name, u in [
            ("dynamo-w1", DYNAMO_W1),
            ("dynamo-w2", DYNAMO_W2),
            ("llmd-w1", LLMD_W1),
            ("llmd-w2", LLMD_W2),
        ]:
            try:
                s, _ = http_get(f"{u}/health", timeout=3)
                statuses[name] = s == 200
            except Exception:
                statuses[name] = False
        if all(statuses.values()):
            print("All 4 backends (dynamo-w1/w2 and llmd-w1/w2) are READY (200 OK)!", flush=True)
            break
        print(f"Backend status: {statuses} - sleeping 10s...", flush=True)
        time.sleep(10)


def summarize_result(tag, out_dir, hit_key):
    json_path = os.path.join(out_dir, "profile_export_aiperf.json")
    if os.path.exists(json_path):
        with open(json_path, "r") as f:
            d = json.load(f)
        out_tput = d.get("output_token_throughput", {}).get("avg", 0.0)
        tot_tput = (d.get("total_token_throughput") or {}).get("avg", 0.0)
        ttft_p90 = d.get("time_to_first_token", {}).get("p90", 0.0)
        itl_p90 = d.get("inter_token_latency", {}).get("p90", 0.0)
        osl_avg = d.get("output_sequence_length", {}).get("avg", 1.0)
        e2e_lat_ms_tok = (ttft_p90 + itl_p90 * max(1.0, osl_avg - 1.0)) / max(
            1.0, osl_avg
        )
        e2e_int = 1000.0 / e2e_lat_ms_tok if e2e_lat_ms_tok > 0 else 0.0
        kv_hit = d.get("router_stats", {}).get(hit_key)
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


def run_aiperf(tag, router, hit_key, stage, conc, duration=28, grace=8.0):
    print(f"\n=======================================================", flush=True)
    print(
        f"Starting {tag} (router={router}, stage={stage}, conc={conc}, duration={duration}s, grace={grace}s)",
        flush=True,
    )
    print(f"=======================================================", flush=True)
    cleanup_stray_aiperf()
    _, reset_msg = http_post(f"{router}/reset?stage={stage}&conc={conc}")
    print(f"Router reset: {reset_msg}", flush=True)

    out_dir = f"/aiperf-env/results/{tag}"
    os.makedirs(out_dir, exist_ok=True)
    log_path = f"/aiperf-env/results/{tag}.log"

    cmd = [
        "/aiperf-env/venv/bin/aiperf",
        "profile",
        "--scenario",
        "inferencex-agentx-mvp",
        "--unsafe-override",
        "--model",
        MODEL,
        "--tokenizer",
        TOKENIZER,
        "--tokenizer-trust-remote-code",
        "--url",
        router,
        "--endpoint-type",
        "chat",
        "--streaming",
        "--use-server-token-count",
        "--public-dataset",
        "semianalysis_cc_traces_weka_062126",
        "--num-dataset-entries",
        "393",
        "--random-seed",
        "42",
        "--trace-idle-gap-cap-seconds",
        "8.0",
        "--system-idle-gap-cap-seconds",
        "0.5",
        "--cache-bust",
        "first_turn_prefix",
        "--concurrency",
        str(conc),
        "--benchmark-duration",
        str(duration),
        "--benchmark-grace-period",
        str(grace),
        "--warmup-requests-per-lane",
        "1",
        "--agentic-warmup-grace-period",
        "600.0",
        "--failed-request-threshold",
        "0.25",
        "--no-gpu-telemetry",
        "--no-auto-plot",
        "--output-artifact-dir",
        out_dir,
    ]
    with open(log_path, "w") as lf:
        try:
            subprocess.run(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=duration + 140,
            )
        except subprocess.TimeoutExpired:
            print(f"Timeout expired for {tag}, cleaning up...", flush=True)
            cleanup_stray_aiperf()

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
    summarize_result(tag, out_dir, hit_key)


def main():
    cleanup_stray_aiperf()
    wait_for_all_backends()

    # 1. NVIDIA Dynamo + SGLang (DeepSeek-V4.1-Flash, 8x B200) — RadixCache already hydrated!
    for c in [16, 32, 64, 128]:
        run_aiperf(
            f"dynamo_stage4_hicache_dram_c{c}",
            DYNAMO_ROUTER,
            "sglang_kv_hit_pct",
            stage=4,
            conc=c,
            duration=28,
        )
    for c in [16, 32, 64, 128]:
        run_aiperf(
            f"dynamo_stage5_mooncake_lustre_c{c}",
            DYNAMO_ROUTER,
            "sglang_kv_hit_pct",
            stage=5,
            conc=c,
            duration=28,
        )

    # 2. llm-d + vLLM (DeepSeek-V4.1-Flash, 8x B200)
    run_aiperf(
        "llmd_dsv4_hydrate_c64",
        LLMD_ROUTER,
        "vllm_kv_hit_pct",
        stage=4,
        conc=64,
        duration=12,
    )
    run_aiperf(
        "llmd_dsv4_hydrate_c128",
        LLMD_ROUTER,
        "vllm_kv_hit_pct",
        stage=4,
        conc=128,
        duration=15,
    )
    run_aiperf(
        "llmd_dsv4_hydrate_c256",
        LLMD_ROUTER,
        "vllm_kv_hit_pct",
        stage=4,
        conc=256,
        duration=15,
    )
    for c in [16, 32, 64, 128, 256]:
        run_aiperf(
            f"llmd_scaffold_stage4_native_dram_c{c}",
            LLMD_ROUTER,
            "vllm_kv_hit_pct",
            stage=4,
            conc=c,
            duration=28,
        )
    for c in [16, 32, 64, 128, 256]:
        run_aiperf(
            f"llmd_scaffold_stage5_mooncake_lustre_c{c}",
            LLMD_ROUTER,
            "vllm_kv_hit_pct",
            stage=5,
            conc=c,
            duration=28,
        )

    print(
        "\nALL DEEPSEEK-V4.1-FLASH DYNAMO + LLMD STAGES 4 AND 5 COMPLETED SUCCESSFULLY!",
        flush=True,
    )


if __name__ == "__main__":
    main()
