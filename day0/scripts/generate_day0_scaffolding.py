#!/usr/bin/env python3
"""Day-0 Automated Scaffolding Generator for vLLM (llm-d) & SGLang (NVIDIA Dynamo).

Ingests official upstream Day-0 recipes directly from:
  1. vLLM Recipes (`https://recipes.vllm.ai/<org>/<model>` / `vllm-project/recipes`)
  2. SGLang Cookbook (`https://docs.sglang.io/cookbook/...`)

Applies Day-0 benchmarking transformations:
  - Dynamic topology resolution (`resolve_topology`) from upstream recipe & node GPU count.
  - Strips non-benchmark parsers (`--tool-call-parser`, `--enable-auto-tool-choice`,
    `--reasoning-parser`) for pure performance benchmarking.
  - Enables multi-node RDMA (`/dev/infiniband`, `mlx5_0..mlx5_7`, `IPC_LOCK`).
  - Composes upstream building blocks (`Mooncake`, `NixlConnector`,
    `OffloadingConnector`/`TieringOffloadingSpec`, `MultiConnector`, `HiCache`,
    `sglang-router`/`Dynamo`, `llm-d`) backed by same-zone 1,000 MBps/TiB
    Google Cloud Managed Lustre (`/mnt/lustre_1000mbps`).
"""

import argparse
import json
import os
import re
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
import yaml


HARDWARE_VRAM_GB = {
    "h100": 80,
    "h200": 141,
    "b200": 180,
    "gb200": 186,
    "b300": 288,
    "gb300": 288,
    "mi300x": 192,
    "mi325x": 256,
    "mi350x": 288,
    "mi355x": 288,
}

PARSER_FLAGS_TO_STRIP = {
    "--tool-call-parser",
    "--enable-auto-tool-choice",
    "--reasoning-parser",
}


def strip_non_benchmark_flags(flags: List[str]) -> List[str]:
    """Strip eval, tool-calling, and reasoning parser flags for pure perf benchmarking."""
    cleaned: List[str] = []
    skip_next = False
    for tok in flags:
        if skip_next:
            skip_next = False
            continue
        parts = tok.strip().split(None, 1)
        head = parts[0].split("=")[0] if parts else ""
        if head in PARSER_FLAGS_TO_STRIP:
            # If the flag was passed as a single token "--tool-call-parser" without argument
            # inside the same string, skip the next token unless it's a boolean flag.
            if len(parts) == 1 and "=" not in tok and head != "--enable-auto-tool-choice":
                skip_next = True
            continue
        cleaned.append(tok)
    return cleaned


def parse_sglang_cookbook_cell(
    js_path: str,
    hw: str = "b200",
    strategy: str = "high-throughput",
    quant: Optional[str] = None,
    enable_spec_decoding: bool = True,
) -> Dict[str, Any]:
    """Extract the verified hardware/strategy cell from an SGLang Cookbook JS bundle."""
    with open(js_path, "r", encoding="utf-8") as f:
        text = f.read()

    # Extract dockerImage for hardware
    img_match = re.search(rf"{hw}:`([^`]+)`", text)
    docker_image = img_match.group(1) if img_match else "lmsysorg/sglang:nightly-dev-cu13-20260922-582389ce"

    # Find matching cell block
    cells_idx = text.find("cells:[")
    if cells_idx == -1:
        raise ValueError(f"Could not find cells:[...] in {js_path}")
    cells_sub = text[cells_idx:]

    # Match cell with hw and strategy
    pattern = re.compile(
        r"\{match:\{([^}]+)\},([^{}]*?flags:\[[^\]]*\])",
        re.DOTALL,
    )
    selected_flags: List[str] = []
    selected_env: List[str] = []
    low_lat_flags: List[str] = []
    nnodes = 1

    for m in pattern.finditer(cells_sub):
        match_str = m.group(1)
        body_str = m.group(2)
        if f"hw:`{hw}`" not in match_str:
            continue
        if quant and f"quant:`{quant}`" not in match_str and "quant:" in match_str:
            continue

        if "strategy:`low-latency`" in match_str:
            ll_m = re.search(r"flags:\[([^\]]+)\]", body_str)
            if ll_m:
                low_lat_flags = re.findall(r"`([^`]+)`", ll_m.group(1))

        if f"strategy:`{strategy}`" not in match_str:
            continue

        nn_m = re.search(r"nnodes:(\d+)", body_str)
        if nn_m:
            nnodes = int(nn_m.group(1))

        env_m = re.search(r"env:\[([^\]]+)\]", body_str)
        if env_m:
            selected_env = re.findall(r"`([^`]+)`", env_m.group(1))

        flags_m = re.search(r"flags:\[([^\]]+)\]", body_str)
        if flags_m:
            selected_flags = re.findall(r"`([^`]+)`", flags_m.group(1))
        break

    if not selected_flags:
        raise ValueError(f"No SGLang Cookbook cell matched hw={hw}, strategy={strategy} in {js_path}")

    # Merge opt-in speculative decoding from playgroundFeatures / low-latency companion cell if not already present
    if enable_spec_decoding and not any(f.startswith("--speculative-") for f in selected_flags):
        cg_max_bs = None
        for f in low_lat_flags:
            if f.startswith("--cuda-graph-max-bs"):
                cg_max_bs = f.split()[-1]
            if (
                f.startswith("--speculative-")
                or f.startswith("--mem-fraction-static")
                or f.startswith("--cuda-graph-max-bs")
            ):
                if not any(existing.split()[0] == f.split()[0] for existing in selected_flags):
                    selected_flags.append(f)
        if cg_max_bs is not None:
            selected_flags = [
                f"--max-running-requests {cg_max_bs}" if f.startswith("--max-running-requests") else f
                for f in selected_flags
            ]

    # Merge engramHostTable env if present in playgroundFeatures
    if "SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1" in text and "SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1" not in selected_env:
        selected_env.append("SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1")
        selected_env.append("SGLANG_DSV41_ENGRAM_HOST_TABLE_LAYOUT=per_rank")

    cleaned_flags = strip_non_benchmark_flags(selected_flags)
    normalized_flags: List[str] = []
    if not any(f.startswith("--trust-remote-code") for f in cleaned_flags):
        normalized_flags.append("--trust-remote-code")
    for f in cleaned_flags:
        if f.startswith("--attn-dp-size"):
            dp_n = f.split()[-1]
            if not any(x.startswith("--ep-size") or x.startswith("--ep ") for x in cleaned_flags):
                normalized_flags.append(f"--ep-size {dp_n}")
            normalized_flags.append(f"--dp-size {dp_n}")
            normalized_flags.append("--enable-dp-attention")
        else:
            normalized_flags.append(f)

    return {
        "docker_image": docker_image,
        "nnodes": nnodes,
        "env": selected_env,
        "flags": normalized_flags,
    }


def parse_vllm_recipe(
    yaml_path: str,
    hw: str = "b200",
    strategy: str = "single_node_tp",
    variant: str = "default",
    enable_spec_decoding: bool = True,
    enable_text_only: bool = True,
) -> Dict[str, Any]:
    """Synthesize base vLLM flags & topology from an official vLLM recipe YAML."""
    with open(yaml_path, "r", encoding="utf-8") as f:
        recipe = yaml.safe_load(f)

    model_cfg = recipe.get("model", {})
    model_id = model_cfg.get("model_id", "")
    base_args = list(model_cfg.get("base_args", []))
    base_env = dict(model_cfg.get("base_env", {}))

    variants = recipe.get("variants", {})
    var_cfg = variants.get(variant) or next(iter(variants.values()), {})
    vram_min_gb = var_cfg.get("vram_minimum_gb", 160)

    # Determine family (blackwell / hopper / amd)
    hw_family = "blackwell" if hw in ("b200", "gb200", "b300", "gb300") else (
        "hopper" if hw in ("h100", "h200") else "amd"
    )

    strat_overrides = recipe.get("strategy_overrides", {}).get(strategy, {})
    tp_map = strat_overrides.get("tp", {})
    import math
    gpu_vram = HARDWARE_VRAM_GB.get(hw, 180)
    min_gpus_by_vram = max(1, 1 << (max(1, math.ceil(vram_min_gb / gpu_vram)) - 1).bit_length())
    recipe_tp = tp_map.get(hw, tp_map.get("default", min_gpus_by_vram))
    # For high-interactivity & throughput parity across 2x 4-GPU or 2x 8-GPU nodes:
    tp_size = max(recipe_tp, 4 if vram_min_gb >= 500 and hw == "b200" else recipe_tp)

    extra_args: List[str] = []
    extra_env: Dict[str, str] = {}

    # Apply family + exact hardware overrides from strategy_overrides
    hw_ov = strat_overrides.get("hardware_overrides", {})
    for key in (hw_family, hw):
        if key in hw_ov:
            extra_args.extend(hw_ov[key].get("extra_args", []))
            extra_env.update(hw_ov[key].get("extra_env", {}))

    # Apply opt-in features (text_only, spec_decoding)
    features = recipe.get("features", {})
    if enable_text_only and "text_only" in features:
        extra_args.extend(features["text_only"].get("args", []))
    if enable_spec_decoding and "spec_decoding" in features:
        spec_feat = features["spec_decoding"]
        if "modes" in spec_feat:
            def_mode = spec_feat.get("default_mode") or next(iter(spec_feat["modes"].keys()))
            mode_cfg = spec_feat["modes"].get(def_mode, {})
            spec_args = mode_cfg.get("args", [])
            if hw in mode_cfg.get("hardware_overrides", {}):
                spec_args = mode_cfg["hardware_overrides"][hw].get("args", spec_args)
        else:
            spec_args = spec_feat.get("args", [])
            if hw in spec_feat.get("hardware_overrides", {}):
                spec_args = spec_feat["hardware_overrides"][hw].get("args", spec_args)
        extra_args.extend(spec_args)

    # For Blackwell (B200/B300) DeepSeek-V4.1-Flash, merge verified high-throughput
    # CUDA graph capture, THP Engram offload, and synthetic 3.51 DSpark benchmark config
    # from the upstream recipe's Blackwell & InferenceX benchmark specification.
    if hw_family == "blackwell" and "DeepSeek-V4.1-Flash" in model_id:
        extra_args = [
            "--language-model-only",
            "--enable-expert-parallel",
            "--engram-config",
            '{"cpu_offload":true,"use_thp":true}',
            "--kernel-config",
            '{"enable_flashinfer_autotune":true}',
            "--attention-config",
            '{"backend":"FLASHINFER_MLA_SPARSE_DSV41","indexer_kv_dtype":"mxfp4","indexer_sparse_logits":true}',
            "--kv-cache-dtype",
            "fp8",
            "--speculative-config",
            '{"method":"dspark","num_speculative_tokens":5,"draft_sample_method":"probabilistic","rejection_sample_method":"synthetic","synthetic_acceptance_length":3.51,"enable_adaptive_verification":false}',
            "--max-model-len",
            "1048576",
            "--compilation-config",
            '{"mode":"VLLM_COMPILE","cudagraph_mode":"FULL_AND_PIECEWISE","cudagraph_capture_sizes":[6,12,18,24,30,36,48,60,72,96,120,144,192,240,288,384,480,576,768,1020,1536,2046,3072,4092,6144,8190],"decoder_replay_cudagraph_capture_sizes":[6,12,18,24,30,36,48,60,72,96,120,144,192,240,288,384,480,576,768,1020,1536,2048,2304,2560,2816,3072,3328,3584,3840,4096]}',
            "--max-cudagraph-capture-size",
            "8190",
            "--max-num-batched-tokens",
            "8192",
            "--max-num-seqs",
            "256",
            "--gpu-memory-utilization",
            "0.97",
            "--enable-prompt-tokens-details",
            "--disable-uvicorn-access-log",
            "--trust-remote-code",
        ]
    elif hw_family == "blackwell" and "GLM-5.3" in model_id:
        extra_args.extend([
            "--max-model-len",
            "131072",
            "--max-num-batched-tokens",
            "8192",
            "--max-num-seqs",
            "256",
            "--gpu-memory-utilization",
            "0.90",
            "--enable-prompt-tokens-details",
            "--disable-uvicorn-access-log",
            "--trust-remote-code",
        ])

    combined_args = strip_non_benchmark_flags(base_args + extra_args)
    combined_env = {**base_env, **extra_env}

    return {
        "model_id": model_id,
        "vram_minimum_gb": vram_min_gb,
        "tp_size": tp_size,
        "base_args": combined_args,
        "base_env": combined_env,
        "kv_offload_support": recipe.get("kv_offload_support", {}),
        "pd_cluster": recipe.get("strategy_overrides", {}).get("pd_cluster", {}),
    }


def resolve_topology(
    engine: str,
    parsed_recipe: Dict[str, Any],
    hw: str = "b200",
    gpus_per_node: int = 8,
    num_nodes: int = 2,
) -> Dict[str, Any]:
    """Dynamically resolve replica GPU count, TP, EP, DP, and placement across nodes."""
    if engine == "sglang":
        flags = parsed_recipe["flags"]
        tp, ep, dp = 1, 1, 1
        for f in flags:
            parts = f.split()
            if len(parts) == 2:
                if parts[0] in ("--tp", "--tp-size", "--tensor-parallel-size"):
                    tp = int(parts[1])
                elif parts[0] in ("--ep", "--ep-size", "--expert-parallel-size"):
                    ep = int(parts[1])
                elif parts[0] in ("--dp", "--dp-size", "--data-parallel-size", "--attn-dp-size"):
                    dp = int(parts[1])
        gpus_per_replica = max(tp, ep, dp) * parsed_recipe.get("nnodes", 1)
    else:
        tp = parsed_recipe["tp_size"]
        ep = tp
        dp = 1
        gpus_per_replica = tp

    total_cluster_gpus = gpus_per_node * num_nodes
    # Use 2 replicas for Stage 1 -> Stage 4 multi-replica scale-out & P/D disaggregation
    num_replicas = 2
    total_benchmark_gpus = gpus_per_replica * num_replicas

    return {
        "hw": hw,
        "tp_size": tp,
        "ep_size": ep,
        "dp_size": dp,
        "gpus_per_replica": gpus_per_replica,
        "num_replicas": num_replicas,
        "total_gpus": total_benchmark_gpus,
        "total_cluster_gpus": total_cluster_gpus,
        "rdma_devices": "mlx5_0,mlx5_1,mlx5_2,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_7",
    }


def build_sglang_stage_commands(
    model_path: str,
    served_model_name: str,
    sglang_cell: Dict[str, Any],
    topo: Dict[str, Any],
    lustre_mount: str = "/mnt/lustre_1000mbps",
) -> Dict[str, Any]:
    """Synthesize SGLang commands for Stages 1-4 using official SGLang Cookbook building blocks."""
    # Replace placeholders in official cookbook flags
    base_flags: List[str] = []
    for f in sglang_cell["flags"]:
        if f.startswith("--model-path"):
            base_flags.append(f"--model-path {model_path}")
            base_flags.append(f"--served-model-name {served_model_name}")
        elif f.startswith("--host"):
            base_flags.append("--host 0.0.0.0")
        elif f.startswith("--port"):
            base_flags.append("--port 8888")
        elif f.startswith("--swa-prefix-tails") and "DeepSeek-V4.1-Flash" in served_model_name:
            base_flags.append("--swa-prefix-tails 4096")
        elif f.startswith("--max-running-requests") and "DeepSeek-V4.1-Flash" in served_model_name:
            base_flags.append("--max-running-requests 128")
        elif f.startswith("--chunked-prefill-size") and "DeepSeek-V4.1-Flash" in served_model_name:
            base_flags.append("--chunked-prefill-size 4096")
        else:
            base_flags.append(f)

    if "DeepSeek-V4.1-Flash" in served_model_name:
        for req_flag in (
            "--chunked-prefill-size 4096",
            "--swa-prefix-tails 4096",
            "--prefill-decode-interval 16",
        ):
            if not any(f.startswith(req_flag.split()[0]) for f in base_flags):
                base_flags.append(req_flag)

    # Add observability flags (non-intrusive)
    for obs_flag in ("--enable-metrics", "--enable-cache-report", "--watchdog-timeout 3600"):
        if not any(f.startswith(obs_flag.split()[0]) for f in base_flags):
            base_flags.append(obs_flag)

    # Stage 4 (Host DRAM KV Cache Tiering): Native SGLang HiCache to pinned Host DRAM (no external file/storage backend)
    hicache_dram_flags = [
        "--enable-hierarchical-cache",
        "--hicache-ratio 2.0",
        "--hicache-size 0",
        "--hicache-mem-layout page_first",
        "--hicache-io-backend kernel",
        "--hicache-write-policy write_through",
    ]

    # Stage 5 (Same-Zone Managed Lustre KV Offloading via Mooncake): SGLang HiCache + Mooncake storage backend backed by Lustre
    hicache_mooncake_lustre_flags = [
        "--enable-hierarchical-cache",
        "--hicache-ratio 1.2",
        "--hicache-size 0",
        "--hicache-mem-layout page_first_direct",
        "--hicache-io-backend direct",
        "--hicache-write-policy write_through",
        "--hicache-storage-backend mooncake",
        "--hicache-storage-prefetch-policy wait_complete",
    ]

    # Native SGLang Cookbook PD Disaggregation building block (AXIS_HANDLERS.pdDisagg)
    pd_prefill_flags = [
        f for f in base_flags if not f.startswith("--speculative-")
    ] + [
        "--disaggregation-mode prefill",
        "--disaggregation-transfer-backend mooncake",
        f"--disaggregation-ib-device {topo['rdma_devices']}",
    ]
    pd_decode_flags = [
        f for f in base_flags if not f.startswith("--speculative-")
    ] + [
        "--disaggregation-mode decode",
        "--disaggregation-transfer-backend mooncake",
        f"--disaggregation-ib-device {topo['rdma_devices']}",
    ]

    mooncake_master_cmd = (
        "mooncake_master "
        "-rpc_port=50051 "
        "-rpc_thread_num=8 "
        "-default_kv_lease_ttl=30000 "
        "-eviction_high_watermark_ratio=0.95 "
        "-eviction_ratio=0.1 "
        "-enable_offload=true "
        "-enable_disk_eviction=true "
        f"-root_fs_dir={lustre_mount}/mooncake_kv_cache "
        "-logtostderr"
    )
    mooncake_client_cmd = (
        f"MOONCAKE_OFFLOAD_FILE_STORAGE_PATH={lustre_mount}/mooncake_kv_cache "
        "mooncake_client "
        "-host=127.0.0.1 "
        "-port=50052 "
        "-global_segment_size='64GB' "
        "-local_buffer_size='4GB' "
        "-metadata_server='P2PHANDSHAKE' "
        "-master_server_address='dynamo-sglang-w1.ubench-llmd.svc.cluster.local:50051' "
        "-protocol='rdma' "
        f"-device_names='{topo['rdma_devices']}' "
        "-enable_offload=true "
        "-start_offload_rpc_server=true "
        "-logtostderr"
    )

    return {
        "base_flags": base_flags,
        "stage4_hicache_dram_flags": base_flags + hicache_dram_flags,
        "stage5_hicache_mooncake_lustre_flags": base_flags + hicache_mooncake_lustre_flags,
        "hicache_lustre_flags": base_flags + hicache_mooncake_lustre_flags,
        "hicache_mooncake_flags": base_flags + hicache_mooncake_lustre_flags,
        "pd_prefill_flags": pd_prefill_flags,
        "pd_decode_flags": pd_decode_flags,
        "mooncake_master_cmd": mooncake_master_cmd,
        "mooncake_client_cmd": mooncake_client_cmd,
        "routers": {
            "stage1_naive_rr": (
                "sglang-router launch "
                "--worker-urls http://dynamo-sglang-w1.ubench-llmd.svc.cluster.local:8888 "
                "http://dynamo-sglang-w2.ubench-llmd.svc.cluster.local:8888 "
                "--policy round_robin --host 0.0.0.0 --port 8089"
            ),
            "stage2_kv_aware": (
                "sglang-router launch "
                "--worker-urls http://dynamo-sglang-w1.ubench-llmd.svc.cluster.local:8888 "
                "http://dynamo-sglang-w2.ubench-llmd.svc.cluster.local:8888 "
                "--policy cache_aware --host 0.0.0.0 --port 8089"
            ),
            "stage3_pd_disagg": (
                "sglang-router launch "
                "--pd-disaggregation "
                "--prefill http://dynamo-sglang-w1.ubench-llmd.svc.cluster.local:8888 8998 "
                "--decode http://dynamo-sglang-w2.ubench-llmd.svc.cluster.local:8888 "
                "--policy cache_aware --host 0.0.0.0 --port 8089"
            ),
        },
        "env": {
            "MOONCAKE_PROTOCOL": "rdma",
            "MOONCAKE_DEVICE": topo["rdma_devices"],
            "MOONCAKE_MASTER": "dynamo-sglang-w1.ubench-llmd.svc.cluster.local:50051",
            "MOONCAKE_GLOBAL_SEGMENT_SIZE": "68719476736",
            "MOONCAKE_LOCAL_BUFFER_SIZE": "4294967296",
            "MOONCAKE_METADATA_SERVER": "P2PHANDSHAKE",
            "MOONCAKE_ENABLE_SSD_OFFLOAD": "1",
            "MOONCAKE_OFFLOAD_FILE_STORAGE_PATH": f"{lustre_mount}/mooncake_kv_cache",
        },
    }


def build_vllm_stage_commands(
    model_path: str,
    served_model_name: str,
    vllm_recipe: Dict[str, Any],
    topo: Dict[str, Any],
    lustre_mount: str = "/mnt/lustre_1000mbps",
) -> Dict[str, Any]:
    """Synthesize vLLM commands for Stages 1-5 using official vLLM Recipes building blocks."""
    base_args = [
        f"vllm serve {model_path}",
        f"--served-model-name {served_model_name}",
        "--host 0.0.0.0",
        "--port 8000",
        f"--tensor-parallel-size {topo['tp_size']}",
    ]
    # Append recipe args
    i = 0
    raw = vllm_recipe["base_args"]
    while i < len(raw):
        tok = raw[i]
        if i + 1 < len(raw) and not raw[i + 1].startswith("--"):
            val = raw[i + 1]
            if val.startswith("{") or val.startswith("["):
                base_args.append(f"{tok} '{val}'")
            else:
                base_args.append(f"{tok} {val}")
            i += 2
        else:
            base_args.append(tok)
            i += 1

    # Stage 3: Native NixlConnector from strategies/pd_cluster.yaml
    nixl_prefill_cfg = json.dumps({
        "kv_connector": "NixlConnector",
        "kv_role": "kv_producer",
        "kv_load_failure_policy": "fail",
    })
    nixl_decode_cfg = json.dumps({
        "kv_connector": "NixlConnector",
        "kv_role": "kv_consumer",
        "kv_load_failure_policy": "fail",
    })

    # Stage 4 (Host DRAM KV Cache Tiering):
    # Follow recipes.vllm.ai per-model kv_offload recipe:
    # - SimpleCPUOffloadConnector (?kv_offload=simple) for standard FullAttention/MLA models
    # - OffloadingConnector with CPUOffloadingSpec (?kv_offload=offloading_cpu) for hybrid/DSA models (e.g. DeepSeek-V4.1-Flash, GLM-5.3)
    kv_offload_support = vllm_recipe.get("kv_offload_support", {})
    if "simple" in kv_offload_support and "offloading_cpu" not in kv_offload_support:
        stage4_dram_cfg = json.dumps({
            "kv_connector": "SimpleCPUOffloadConnector",
            "kv_role": "kv_both",
            "kv_connector_extra_config": {
                "cpu_bytes_to_use_per_rank": 68719476736,
                "lazy_offload": False,
            },
        })
    else:
        stage4_dram_cfg = json.dumps({
            "kv_connector": "OffloadingConnector",
            "kv_role": "kv_both",
            "kv_connector_extra_config": {
                "spec_name": "CPUOffloadingSpec",
                "cpu_bytes_to_use": 137438953472,
                "blocks_per_chunk": 4,
            },
        })

    simple_cpu_offload_cfg = json.dumps({
        "kv_connector": "SimpleCPUOffloadConnector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {
            "cpu_bytes_to_use_per_rank": 68719476736,
            "lazy_offload": False,
        },
    })

    # Stage 5 (Same-Zone Managed Lustre KV Offloading via Mooncake):
    # MooncakeStoreConnector backed by mooncake_master (-root_fs_dir=/mnt/lustre_1000mbps/mooncake_kv_cache) + mooncake_client
    mooncake_store_cfg = json.dumps({
        "kv_connector": "MooncakeStoreConnector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {
            "load_async": True,
            "lookup_async": True,
        },
    })

    # Stage 5 PD + Mooncake MultiConnector from kv_store_centralized_mooncake.yaml
    multi_prefill_cfg = json.dumps({
        "kv_connector": "MultiConnector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {
            "connectors": [
                {"kv_connector": "NixlConnector", "kv_role": "kv_producer", "kv_load_failure_policy": "fail"},
                {
                    "kv_connector": "MooncakeStoreConnector",
                    "kv_role": "kv_both",
                    "kv_connector_extra_config": {"load_async": True, "lookup_async": True},
                },
            ]
        },
    })

    mooncake_vllm_config = {
        "mode": "standalone-store",
        "metadata_server": "P2PHANDSHAKE",
        "master_server_address": "llmd-vllm-w1.ubench-llmd.svc.cluster.local:50051",
        "global_segment_size": "0GB",
        "local_buffer_size": "4GB",
        "protocol": "rdma",
        "device_name": topo["rdma_devices"],
        "enable_offload": True,
    }

    return {
        "base_args": base_args,
        "stage3_pd_prefill_args": base_args + [f"--kv-transfer-config '{nixl_prefill_cfg}'"],
        "stage3_pd_decode_args": base_args + [f"--kv-transfer-config '{nixl_decode_cfg}'"],
        "stage4_dram_offload_args": base_args + [f"--kv-transfer-config '{stage4_dram_cfg}'"],
        "stage4_simple_cpu_offload_args": base_args + [f"--kv-transfer-config '{simple_cpu_offload_cfg}'"],
        "stage5_mooncake_lustre_args": base_args + [f"--kv-transfer-config '{mooncake_store_cfg}'"],
        "stage5_multi_connector_prefill_args": base_args + [f"--kv-transfer-config '{multi_prefill_cfg}'"],
        "mooncake_vllm_config": mooncake_vllm_config,
        "env": {
            **vllm_recipe["base_env"],
            "PYTHONHASHSEED": "0",
            "UCX_NET_DEVICES": "all",
            "NCCL_CUMEM_ENABLE": "1",
            "MOONCAKE_CONFIG_PATH": "/tmp/mooncake_vllm_config.json",
            "MOONCAKE_OFFLOAD_FILE_STORAGE_PATH": f"{lustre_mount}/mooncake_kv_cache",
        },
    }


def generate_k8s_sglang_manifest(
    sglang_cell: Dict[str, Any],
    sglang_cmds: Dict[str, Any],
    topo: Dict[str, Any],
    nodes: List[str],
    stage_mode: str = "base",
    image_override: Optional[str] = None,
) -> str:
    """Generate K8s Deployment + Service manifest with RDMA (/dev/infiniband), HiCache DRAM (Stage 4), and Mooncake Lustre (Stage 5)."""
    image = image_override or sglang_cell["docker_image"]
    if stage_mode == "stage4_dram":
        flags = sglang_cmds["stage4_hicache_dram_flags"]
    elif stage_mode in ("stage5_mooncake_lustre", "hicache_lustre"):
        flags = sglang_cmds["stage5_hicache_mooncake_lustre_flags"]
    else:
        flags = sglang_cmds["base_flags"]
    flag_lines = " \\\n            ".join(flags)
    gpus = str(topo["gpus_per_replica"])

    docs = []
    for idx, node_name in enumerate(nodes[: topo["num_replicas"]], start=1):
        w_name = f"w{idx}"
        mooncake_boot = ""
        if stage_mode in ("stage5_mooncake_lustre", "hicache_lustre"):
            master_boot = (
                f"nohup {sglang_cmds['mooncake_master_cmd']} > /tmp/mooncake_master.log 2>&1 < /dev/null &\n          sleep 1\n          "
                if idx == 1
                else ""
            )
            mooncake_boot = (
                f"mkdir -p /mnt/lustre_1000mbps/mooncake_kv_cache\n          "
                f"{master_boot}"
                f"nohup {sglang_cmds['mooncake_client_cmd']} > /tmp/mooncake_client.log 2>&1 < /dev/null &\n          "
            )
        doc = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: dynamo-sglang-{w_name}
  namespace: ubench-llmd
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: dynamo-sglang-worker
      replica: {w_name}
  template:
    metadata:
      annotations:
        gke-gcsfuse/cpu-limit: "32"
        gke-gcsfuse/ephemeral-storage-limit: 600Gi
        gke-gcsfuse/memory-limit: 64Gi
        gke-gcsfuse/volumes: "true"
      labels:
        app: dynamo-sglang-worker
        replica: {w_name}
    spec:
      nodeSelector:
        kubernetes.io/hostname: {node_name}
      serviceAccountName: workload-identity-k8s-sa
      tolerations:
      - operator: Exists
      containers:
      - name: sglang
        image: {image}
        imagePullPolicy: IfNotPresent
        securityContext:
          capabilities:
            add: ["IPC_LOCK", "SYS_RESOURCE"]
        command: ["/bin/bash", "-c"]
        env:
        - name: NCCL_NET_PLUGIN
          value: "none"
        - name: NCCL_TUNER_PLUGIN
          value: "none"
        - name: NCCL_PROFILER_PLUGIN
          value: "none"
        - name: SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE
          value: "1"
        - name: SGLANG_DSV41_ENGRAM_HOST_TABLE_LAYOUT
          value: "per_rank"
        - name: SGLANG_SIMULATE_ACC_LEN
          value: "3.51"
        - name: SGLANG_SIMULATE_ACC_METHOD
          value: "match-expected"
        - name: SGLANG_SIMULATE_ACC_TOKEN_MODE
          value: "real-draft-token"
        - name: MOONCAKE_PROTOCOL
          value: "rdma"
        - name: MOONCAKE_DEVICE
          value: "{topo['rdma_devices']}"
        - name: MOONCAKE_MASTER
          value: "dynamo-sglang-w1.ubench-llmd.svc.cluster.local:50051"
        - name: MOONCAKE_GLOBAL_SEGMENT_SIZE
          value: "68719476736"
        - name: MOONCAKE_LOCAL_BUFFER_SIZE
          value: "4294967296"
        - name: MOONCAKE_METADATA_SERVER
          value: "P2PHANDSHAKE"
        - name: MOONCAKE_ENABLE_SSD_OFFLOAD
          value: "1"
        - name: MOONCAKE_OFFLOAD_FILE_STORAGE_PATH
          value: "/mnt/lustre_1000mbps/mooncake_kv_cache"
        - name: UCX_NET_DEVICES
          value: "all"
        args:
        - |
          set -euo pipefail
          ulimit -l unlimited || true
          export PYTHONNOUSERSITE=1
          export PYTHONUNBUFFERED=1
          export SGLANG_TIMEOUT_KEEP_ALIVE=900
          {mooncake_boot}if [ -d /mnt/lustre_1000mbps/sglang_kernel_cache/{w_name}/sglang ]; then
            echo "Restoring pre-compiled SGLang FlashInfer/Triton/CuteDSL kernels from Lustre..."
            mkdir -p /root/.cache
            cp -rn /mnt/lustre_1000mbps/sglang_kernel_cache/{w_name}/sglang /root/.cache/ || true
          fi
          echo "Starting Upgraded Upstream Recipe SGLang Worker {w_name} (TP={topo['tp_size']}, EP={topo['ep_size']}, mode={stage_mode}, RDMA enabled)..."
          exec sglang serve \\
            {flag_lines}
        ports:
        - containerPort: 8888
          name: http
        - containerPort: 50051
          name: mooncake-rpc
        resources:
          limits:
            nvidia.com/gpu: "{gpus}"
          requests:
            cpu: "64"
            memory: 700Gi
            nvidia.com/gpu: "{gpus}"
        volumeMounts:
        - mountPath: /gcs
          name: gcs-model
          readOnly: true
        - mountPath: /mnt/lustre_1000mbps
          name: lustre-tier
        - mountPath: /dev/infiniband
          name: dev-infiniband
        - mountPath: /dev/shm
          name: dshm
      volumes:
      - name: gcs-model
        csi:
          driver: gcsfuse.csi.storage.gke.io
          volumeAttributes:
            bucketName: ikwak-eu-stuff
            mountOptions: implicit-dirs,max-conns-per-host=0,file-cache:max-size-mb:-1,file-cache:cache-file-for-range-read:true,file-cache:enable-parallel-downloads:true,file-cache:max-parallel-downloads:32,file-cache:download-chunk-size-mb:64
      - name: lustre-tier
        persistentVolumeClaim:
          claimName: lustre-1000mbps-pvc
      - name: dev-infiniband
        hostPath:
          path: /dev/infiniband
          type: Directory
      - name: dshm
        emptyDir:
          medium: Memory
          sizeLimit: 256Gi
---
apiVersion: v1
kind: Service
metadata:
  name: dynamo-sglang-{w_name}
  namespace: ubench-llmd
spec:
  selector:
    app: dynamo-sglang-worker
    replica: {w_name}
  ports:
  - name: http
    port: 8888
    targetPort: 8888
  - name: mooncake-rpc
    port: 50051
    targetPort: 50051"""
        docs.append(doc)
    return "\n---\n".join(docs) + "\n"


def generate_k8s_vllm_manifest(
    vllm_cmds: Dict[str, Any],
    topo: Dict[str, Any],
    nodes: List[str],
    stage_mode: str = "base",
    image_override: str = "vllm/vllm-openai:nightly-dev-x86_64-cu130-ac9126e58aa7",
) -> str:
    """Generate K8s Deployment + Service manifest for vLLM (llm-d) with RDMA, Native OffloadingConnector DRAM (Stage 4), and Mooncake Lustre (Stage 5)."""
    if stage_mode == "stage4_dram":
        args_list = vllm_cmds["stage4_dram_offload_args"]
    elif stage_mode in ("stage5_mooncake_lustre", "offload_fs"):
        args_list = vllm_cmds["stage5_mooncake_lustre_args"]
    else:
        args_list = vllm_cmds["base_args"]
    flag_lines = " \\\n            ".join(args_list)
    gpus = str(topo["gpus_per_replica"])
    mc_cfg_json = json.dumps(vllm_cmds["mooncake_vllm_config"])

    docs = []
    for idx, node_name in enumerate(nodes[: topo["num_replicas"]], start=1):
        w_name = f"w{idx}"
        mooncake_boot = ""
        if stage_mode in ("stage5_mooncake_lustre", "offload_fs"):
            master_boot = (
                "nohup mooncake_master -rpc_port=50051 -rpc_thread_num=8 -default_kv_lease_ttl=30000 "
                "-eviction_high_watermark_ratio=0.95 -eviction_ratio=0.1 -enable_offload=true "
                "-enable_disk_eviction=true -root_fs_dir=/mnt/lustre_1000mbps/mooncake_kv_cache "
                "-logtostderr > /tmp/mooncake_master.log 2>&1 < /dev/null &\n          sleep 1\n          "
                if idx == 1
                else ""
            )
            mooncake_boot = (
                f"mkdir -p /mnt/lustre_1000mbps/mooncake_kv_cache\n          "
                f"cat <<'EOF' > /tmp/mooncake_vllm_config.json\n{mc_cfg_json}\nEOF\n          "
                f"{master_boot}"
                f"MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/mnt/lustre_1000mbps/mooncake_kv_cache "
                f"nohup mooncake_client -host=127.0.0.1 -port=50052 -global_segment_size='64GB' "
                f"-local_buffer_size='4GB' -metadata_server='P2PHANDSHAKE' "
                f"-master_server_address='llmd-vllm-w1.ubench-llmd.svc.cluster.local:50051' "
                f"-protocol='rdma' -device_names='{topo['rdma_devices']}' -enable_offload=true "
                f"-start_offload_rpc_server=true -logtostderr > /tmp/mooncake_client.log 2>&1 < /dev/null &\n          "
            )
        doc = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: llmd-vllm-{w_name}
  namespace: ubench-llmd
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: llmd-vllm-worker
      replica: {w_name}
  template:
    metadata:
      annotations:
        gke-gcsfuse/cpu-limit: "32"
        gke-gcsfuse/ephemeral-storage-limit: 600Gi
        gke-gcsfuse/memory-limit: 64Gi
        gke-gcsfuse/volumes: "true"
      labels:
        app: llmd-vllm-worker
        replica: {w_name}
    spec:
      nodeSelector:
        kubernetes.io/hostname: {node_name}
      serviceAccountName: workload-identity-k8s-sa
      tolerations:
      - operator: Exists
      containers:
      - name: vllm
        image: {image_override}
        imagePullPolicy: IfNotPresent
        securityContext:
          capabilities:
            add: ["IPC_LOCK", "SYS_RESOURCE"]
        command: ["/bin/bash", "-c"]
        env:
        - name: NCCL_NET_PLUGIN
          value: "none"
        - name: NCCL_TUNER_PLUGIN
          value: "none"
        - name: NCCL_PROFILER_PLUGIN
          value: "none"
        - name: PYTHONHASHSEED
          value: "0"
        - name: UCX_NET_DEVICES
          value: "all"
        - name: NCCL_CUMEM_ENABLE
          value: "1"
        - name: VLLM_ENGINE_READY_TIMEOUT_S
          value: "7200"
        - name: VLLM_SERVER_DEV_MODE
          value: "1"
        - name: MOONCAKE_CONFIG_PATH
          value: "/tmp/mooncake_vllm_config.json"
        - name: MOONCAKE_OFFLOAD_FILE_STORAGE_PATH
          value: "/mnt/lustre_1000mbps/mooncake_kv_cache"
        args:
        - |
          set -euo pipefail
          ulimit -l unlimited || true
          export VLLM_USE_V2_MODEL_RUNNER=1
          export VLLM_USE_RUST_FRONTEND=1
          export PYTHONUNBUFFERED=1
          {mooncake_boot}if [ -d /mnt/lustre_1000mbps/vllm_kernel_cache/{w_name}/cache ]; then
            echo "Restoring pre-compiled vLLM FlashInfer/Triton/torch.compile kernels from Lustre..."
            mkdir -p /root/.cache
            cp -rn /mnt/lustre_1000mbps/vllm_kernel_cache/{w_name}/cache/* /root/.cache/ || true
          fi
          echo "Starting Upgraded Upstream Recipe vLLM Worker {w_name} (TP={topo['tp_size']}, mode={stage_mode}, RDMA enabled)..."
          exec {flag_lines}
        ports:
        - containerPort: 8000
          name: http
        - containerPort: 50051
          name: mooncake-rpc
        resources:
          limits:
            nvidia.com/gpu: "{gpus}"
          requests:
            cpu: "64"
            memory: 700Gi
            nvidia.com/gpu: "{gpus}"
        volumeMounts:
        - mountPath: /gcs
          name: gcs-model
          readOnly: true
        - mountPath: /mnt/lustre_1000mbps
          name: lustre-tier
        - mountPath: /dev/infiniband
          name: dev-infiniband
        - mountPath: /dev/shm
          name: dshm
      volumes:
      - name: gcs-model
        csi:
          driver: gcsfuse.csi.storage.gke.io
          volumeAttributes:
            bucketName: ikwak-eu-stuff
            mountOptions: implicit-dirs,max-conns-per-host=0,file-cache:max-size-mb:-1,file-cache:cache-file-for-range-read:true,file-cache:enable-parallel-downloads:true,file-cache:max-parallel-downloads:32,file-cache:download-chunk-size-mb:64
      - name: lustre-tier
        persistentVolumeClaim:
          claimName: lustre-1000mbps-pvc
      - name: dev-infiniband
        hostPath:
          path: /dev/infiniband
          type: Directory
      - name: dshm
        emptyDir:
          medium: Memory
          sizeLimit: 512Gi
---
apiVersion: v1
kind: Service
metadata:
  name: llmd-vllm-{w_name}
  namespace: ubench-llmd
spec:
  selector:
    app: llmd-vllm-worker
    replica: {w_name}
  ports:
  - name: http
    port: 8000
    targetPort: 8000
  - name: mooncake-rpc
    port: 50051
    targetPort: 50051"""
        docs.append(doc)
    return "\n---\n".join(docs) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generate Day-0 Scaffolding from Upstream vLLM & SGLang Recipes")
    parser.add_argument("--model-id", default="deepseek-ai/DeepSeek-V4.1-Flash")
    parser.add_argument("--model-path", default="/gcs/deepseek-ai/DeepSeek-V4.1-Flash")
    parser.add_argument("--hw", default="b200")
    parser.add_argument("--sglang-strategy", default="high-throughput")
    parser.add_argument("--sglang-js", default="/tmp/sglang_dsv41_config.js")
    parser.add_argument("--vllm-yaml", default="/tmp/vllm_dsv41_flash.yaml")
    parser.add_argument(
        "--sglang-image",
        default="lmsysorg/sglang:nightly-dev-cu13-20260922-582389ce@sha256:0e1b14e302619a42ef5581b87db6804651d4f946301cf543d74a3a1eb1c33b40",
    )
    parser.add_argument(
        "--vllm-image",
        default="vllm/vllm-openai:nightly-dev-x86_64-cu130-ac9126e58aa7",
    )
    parser.add_argument("--output-dir", default="/usr/local/google/home/ikwak/jetski-playground/day0/manifests")
    parser.add_argument("--manifest-prefix", default="")
    args = parser.parse_args()

    sglang_cell = parse_sglang_cookbook_cell(args.sglang_js, hw=args.hw, strategy=args.sglang_strategy)
    sglang_topo = resolve_topology("sglang", sglang_cell, hw=args.hw)
    sglang_cmds = build_sglang_stage_commands(
        args.model_path, args.model_id, sglang_cell, sglang_topo
    )

    vllm_recipe = parse_vllm_recipe(args.vllm_yaml, hw=args.hw)
    vllm_topo = resolve_topology("vllm", vllm_recipe, hw=args.hw)
    vllm_cmds = build_vllm_stage_commands(
        args.model_path, args.model_id, vllm_recipe, vllm_topo
    )

    nodes = [
        "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3",
        "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3",
    ]

    os.makedirs(args.output_dir, exist_ok=True)

    # Write synthesized SGLang manifests (Stages 1-3 Base, Stage 4 HiCache DRAM, Stage 5 Mooncake Lustre)
    sglang_base_yaml = generate_k8s_sglang_manifest(
        sglang_cell, sglang_cmds, sglang_topo, nodes, stage_mode="base", image_override=args.sglang_image
    )
    sglang_dram_yaml = generate_k8s_sglang_manifest(
        sglang_cell, sglang_cmds, sglang_topo, nodes, stage_mode="stage4_dram", image_override=args.sglang_image
    )
    sglang_lustre_yaml = generate_k8s_sglang_manifest(
        sglang_cell, sglang_cmds, sglang_topo, nodes, stage_mode="stage5_mooncake_lustre", image_override=args.sglang_image
    )
    vllm_base_yaml = generate_k8s_vllm_manifest(
        vllm_cmds, vllm_topo, nodes, stage_mode="base", image_override=args.vllm_image
    )
    vllm_dram_yaml = generate_k8s_vllm_manifest(
        vllm_cmds, vllm_topo, nodes, stage_mode="stage4_dram", image_override=args.vllm_image
    )
    vllm_lustre_yaml = generate_k8s_vllm_manifest(
        vllm_cmds, vllm_topo, nodes, stage_mode="stage5_mooncake_lustre", image_override=args.vllm_image
    )

    if args.manifest_prefix:
        pfx = f"-{args.manifest_prefix}"
        sg_tag = f"{sglang_topo['num_replicas']}x-tp{sglang_topo['tp_size']}"
        vl_tag = f"{vllm_topo['num_replicas']}x-tp{vllm_topo['tp_size']}"
        base_manifest_path = os.path.join(args.output_dir, f"dynamo-sglang{pfx}-{sg_tag}-{args.hw}-upstream-recipe.yaml")
        dram_manifest_path = os.path.join(args.output_dir, f"dynamo-sglang{pfx}-{sg_tag}-{args.hw}-stage4-hicache-dram.yaml")
        hicache_manifest_path = os.path.join(args.output_dir, f"dynamo-sglang{pfx}-{sg_tag}-{args.hw}-hicache-lustre.yaml")
        vllm_base_path = os.path.join(args.output_dir, f"llmd-vllm{pfx}-{vl_tag}-{args.hw}-upstream-recipe.yaml")
        vllm_dram_path = os.path.join(args.output_dir, f"llmd-vllm{pfx}-{vl_tag}-{args.hw}-stage4-native-dram.yaml")
        vllm_offload_path = os.path.join(args.output_dir, f"llmd-vllm{pfx}-{vl_tag}-{args.hw}-offload-lustre.yaml")
        meta_path = os.path.join(args.output_dir, f"synthesized_day0_recipes_{args.manifest_prefix}.json")
    else:
        base_manifest_path = os.path.join(args.output_dir, "dynamo-sglang-2x-tep4-b200-upstream-recipe.yaml")
        dram_manifest_path = os.path.join(args.output_dir, "dynamo-sglang-2x-tep4-b200-stage4-hicache-dram.yaml")
        hicache_manifest_path = os.path.join(args.output_dir, "dynamo-sglang-2x-tep4-b200-hicache-lustre.yaml")
        vllm_base_path = os.path.join(args.output_dir, "llmd-vllm-2x-tp4-b200-upstream-recipe.yaml")
        vllm_dram_path = os.path.join(args.output_dir, "llmd-vllm-2x-tp4-b200-stage4-native-dram.yaml")
        vllm_offload_path = os.path.join(args.output_dir, "llmd-vllm-2x-tp4-b200-offload-lustre.yaml")
        meta_path = os.path.join(args.output_dir, "synthesized_day0_recipes.json")

    for path, content in (
        (base_manifest_path, sglang_base_yaml),
        (dram_manifest_path, sglang_dram_yaml),
        (hicache_manifest_path, sglang_lustre_yaml),
        (vllm_base_path, vllm_base_yaml),
        (vllm_dram_path, vllm_dram_yaml),
        (vllm_offload_path, vllm_lustre_yaml),
    ):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    # Save machine-readable synthesis metadata for Prism / ubench-dash sync
    metadata = {
        "model_id": args.model_id,
        "hw": args.hw,
        "sglang": {
            "topology": sglang_topo,
            "cookbook_cell": sglang_cell,
            "stage_commands": sglang_cmds,
        },
        "vllm": {
            "topology": vllm_topo,
            "recipe": vllm_recipe,
            "stage_commands": vllm_cmds,
        },
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    print("=== Day-0 Upstream Recipe Synthesis Complete (5-Stage Architecture) ===")
    print(f"Model: {args.model_id} | Hardware: {args.hw}")
    print(f"SGLang Topology: {sglang_topo['num_replicas']}x (TP={sglang_topo['tp_size']}, EP={sglang_topo['ep_size']}) = {sglang_topo['total_gpus']} GPUs, RDMA={sglang_topo['rdma_devices']}")
    print(f"vLLM Topology:   {vllm_topo['num_replicas']}x (TP={vllm_topo['tp_size']}) = {vllm_topo['total_gpus']} GPUs, RDMA={vllm_topo['rdma_devices']}")
    print(f"Wrote:\n  - {base_manifest_path}\n  - {dram_manifest_path}\n  - {hicache_manifest_path}\n  - {vllm_base_path}\n  - {vllm_dram_path}\n  - {vllm_offload_path}\n  - {meta_path}")


if __name__ == "__main__":
    main()
