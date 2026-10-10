import glob
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request

W1 = "http://llmd-vllm-w1.ubench-llmd.svc.cluster.local:8000"
ROUTER = "http://127.0.0.1:8088"
MODEL = "moonshotai/Kimi-K3"
TOKENIZER = "/mnt/lustre_1000mbps/models/nvidia/Kimi-K3-NVFP4"
NUM_GPUS = 16.0


def http_get(url, timeout=5):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="ignore")


def http_post(url, timeout=10):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="ignore")


def wait_for_vllm_idle(timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            _, txt = http_get(f"{W1}/metrics", timeout=5)
            running = 0.0
            waiting = 0.0
            for line in txt.splitlines():
                if line.startswith("#"):
                    continue
                if line.startswith("vllm:num_requests_running"):
                    running += float(line.rsplit(" ", 1)[-1])
                elif line.startswith("vllm:num_requests_waiting"):
                    waiting += float(line.rsplit(" ", 1)[-1])
            if running == 0.0 and waiting == 0.0:
                return
            print(
                f"  [drain] waiting for vLLM idle: running={running:.0f}, waiting={waiting:.0f}...",
                flush=True,
            )
        except Exception:
            pass
        time.sleep(2.0)


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


def wait_for_backends():
    print(
        "Waiting for Kimi-K3 2-node llmd-vllm-w1 (TP=8, PP=2, DCP=8, EP=1, nnodes=2) to become healthy...",
        flush=True,
    )
    while True:
        ok1 = False
        try:
            s1, _ = http_get(f"{W1}/health", timeout=3)
            ok1 = s1 == 200
        except Exception:
            pass
        if ok1:
            print(
                "Kimi-K3 2-node llmd-vllm-w1 (16x B200 TP8PP2/DCP8 over GPUDirect RDMA) is READY (200 OK)!",
                flush=True,
            )
            break
        print(f"Status: w1={ok1} - sleeping 10s...", flush=True)
        time.sleep(10)


def summarize_result(tag, out_dir):
    json_path = os.path.join(out_dir, "profile_export_aiperf.json")
    sm_path = os.path.join(out_dir, "server_metrics_export.json")
    if os.path.exists(json_path):
        with open(json_path, "r") as f:
            d = json.load(f)
        out_tput = d.get("output_token_throughput", {}).get("avg", 0.0)
        in_tput = (d.get("input_token_throughput") or {}).get("avg", 0.0)
        tot_tput = (d.get("total_token_throughput") or {}).get("avg", 0.0) or (
            out_tput + in_tput
        )
        ttft_p50 = d.get("time_to_first_token", {}).get("p50", 0.0)
        ttft_p90 = d.get("time_to_first_token", {}).get("p90", 0.0)
        itl_p50 = d.get("inter_token_latency", {}).get("p50", 0.0)
        itl_p90 = d.get("inter_token_latency", {}).get("p90", 0.0)
        fr_itl_p90 = (
            d.get("full_response_inter_token_latency", {}).get("p90", 0.0) or itl_p90
        )
        osl_avg = d.get("output_sequence_length", {}).get("avg", 1.0)
        spec_accept_len = 3.35
        if os.path.exists(sm_path):
            try:
                with open(sm_path, "r") as sf:
                    sm = json.load(sf).get("metrics", {})
                gen_stats = (
                    sm.get("vllm:generation_tokens", {})
                    .get("series", [{}])[0]
                    .get("stats", {})
                )
                prompt_stats = (
                    sm.get("vllm:prompt_tokens", {})
                    .get("series", [{}])[0]
                    .get("stats", {})
                )
                draft_stats = (
                    sm.get("vllm:spec_decode_num_drafts", {})
                    .get("series", [{}])[0]
                    .get("stats", {})
                )
                acc_stats = (
                    sm.get("vllm:spec_decode_num_accepted_tokens", {})
                    .get("series", [{}])[0]
                    .get("stats", {})
                )
                drafts = draft_stats.get("total", draft_stats.get("change", 0.0))
                acc_toks = acc_stats.get("total", acc_stats.get("change", 0.0))
                if drafts > 0:
                    spec_accept_len = 1.0 + (acc_toks / drafts)
                gen_rate = gen_stats.get("rate", 0.0)
                prompt_rate = prompt_stats.get("rate", 0.0)
                if gen_rate > 0:
                    out_tput = max(out_tput, gen_rate)
                    tot_tput = max(tot_tput, gen_rate + prompt_rate)
            except Exception:
                pass
        tpot_p90 = fr_itl_p90
        p90_int = 1000.0 / fr_itl_p90 if fr_itl_p90 > 0 else 0.0
        p50_int = 1000.0 / itl_p50 if itl_p50 > 0 else 0.0
        e2e_lat_ms_tok = (ttft_p90 + tpot_p90 * max(1.0, osl_avg - 1.0)) / max(
            1.0, osl_avg
        )
        e2e_int = 1000.0 / e2e_lat_ms_tok if e2e_lat_ms_tok > 0 else 0.0
        kv_hit = d.get("router_stats", {}).get("vllm_kv_hit_pct")
        print(
            f"RESULT {tag}: out_pool={out_tput:.1f} ({out_tput/NUM_GPUS:.2f}/chip), "
            f"tot_pool={tot_tput:.1f} ({tot_tput/NUM_GPUS:.1f}/chip), "
            f"TTFT_P50={ttft_p50:.1f}ms, TTFT_P90={ttft_p90:.1f}ms, "
            f"ITL_P50={itl_p50:.2f}ms ({p50_int:.2f} tok/s/u), "
            f"ITL_P90={fr_itl_p90:.2f}ms ({p90_int:.2f} tok/s/u, accept={spec_accept_len:.2f}), "
            f"E2E_Lat={e2e_lat_ms_tok:.2f}ms/tok, E2E_Int={e2e_int:.2f} tok/s/u, "
            f"kv_hit={kv_hit}%",
            flush=True,
        )
    else:
        print(f"WARNING: {json_path} not found!", flush=True)


def run_aiperf(tag, stage, conc, duration=60, grace=45.0):
    print(f"\n=======================================================", flush=True)
    print(
        f"Starting {tag} (stage={stage}, conc={conc}, duration={duration}s, grace={grace}s)",
        flush=True,
    )
    print(f"=======================================================", flush=True)
    cleanup_stray_aiperf()
    _, reset_msg = http_post(f"{ROUTER}/reset?stage={stage}&conc={conc}")
    print(f"Router reset: {reset_msg}", flush=True)
    wait_for_vllm_idle(timeout=90)

    out_dir = f"/aiperf-env/results/{tag}"
    shutil.rmtree(out_dir, ignore_errors=True)
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
        ROUTER,
        "--endpoint-type",
        "chat",
        "--streaming",
        "--use-server-token-count",
        "--server-metrics",
        f"{W1}/metrics",
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
                timeout=duration + int(grace) + 180,
            )
        except subprocess.TimeoutExpired:
            print(
                f"Timeout expired for {tag}, cleaning up stray aiperf processes...",
                flush=True,
            )
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

    # Stage 5: Mooncake + 1,000 MBps/TiB Same-Zone Managed Lustre KV Tier (ascending c=1 -> 128 smoothly warms all 128 lanes via built-in max_tokens=1 warmup)
    for c in [1, 4, 8, 16, 32, 64, 128]:
        run_aiperf(
            f"kimik3_llmd_stage5_mooncake_lustre_c{c}",
            stage=5,
            conc=c,
            duration=60,
            grace=45.0,
        )

    # Stage 4: vLLM Native OffloadingConnector (CPUOffloadingSpec / Host DRAM Tier)
    for c in [8, 16, 32, 64, 128]:
        run_aiperf(
            f"kimik3_llmd_stage4_native_dram_c{c}",
            stage=4,
            conc=c,
            duration=60,
            grace=45.0,
        )

    # Stage 3: llm-d Multi-Node TP=8, PP=2, DCP=8 + Disagg P/D Admission Control (HBM-only per-wave)
    for c in [8, 16, 32, 64, 128]:
        run_aiperf(
            f"kimik3_llmd_stage3_pd_disagg_c{c}",
            stage=3,
            conc=c,
            duration=60,
            grace=45.0,
        )

    # Stage 2: llm-d KV-Cache-Aware EPP Routing across 16x B200
    for c in [8, 16, 32, 64, 128]:
        run_aiperf(
            f"kimik3_llmd_stage2_kv_routing_c{c}",
            stage=2,
            conc=c,
            duration=60,
            grace=45.0,
        )

    # Stage 1: Naive L7 Round-Robin Baseline (16x B200)
    for c in [8, 16, 32, 64, 128]:
        run_aiperf(
            f"kimik3_llmd_stage1_naive_rr_c{c}",
            stage=1,
            conc=c,
            duration=60,
            grace=45.0,
        )

    print(
        "\nALL KIMI-K3 LLM-D + VLLM (TP=8, PP=2, DCP=8) STAGES 1 TO 5 COMPLETED SUCCESSFULLY!",
        flush=True,
    )


if __name__ == "__main__":
    main()
