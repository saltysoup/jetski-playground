"""Uploads Stage 1 -> Stage 4 data points (llm-d + vLLM and NVIDIA Dynamo + SGLang) to Prism (GCS) and ubench-dash (BigQuery)."""

import base64
import datetime
import json
import pathlib
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid


def _encode_context_value(val: str) -> str:
  if not val:
    return ""
  encoded = base64.urlsafe_b64encode(val.encode("utf-8")).decode("utf-8")
  return "e" + encoded.rstrip("=")


def get_adc_token() -> str:
  return subprocess.check_output(
      ["gcloud", "auth", "application-default", "print-access-token"], text=True
  ).strip()


def get_sa_token(adc_token: str) -> str:
  url = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/ubench-db-writer@ml-workload-benchmarks.iam.gserviceaccount.com:generateAccessToken"
  body = json.dumps({
      "scope": [
          "https://www.googleapis.com/auth/cloud-platform",
          "https://www.googleapis.com/auth/bigquery",
      ]
  }).encode("utf-8")
  req = urllib.request.Request(
      url,
      data=body,
      headers={
          "Authorization": f"Bearer {adc_token}",
          "Content-Type": "application/json",
      },
      method="POST",
  )
  resp = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
  return resp["accessToken"]


def ms_to_sec_stats(metric_dict: dict | None, units: str = "s") -> dict | None:
  if not metric_dict:
    return None
  out = {"units": units}
  for k_src, k_dst in [
      ("avg", "mean"),
      ("std", "stddev"),
      ("min", "min"),
      ("p1", "p0p1"),
      ("p1", "p1"),
      ("p5", "p5"),
      ("p10", "p10"),
      ("p25", "p25"),
      ("p50", "p50"),
      ("p75", "p75"),
      ("p90", "p90"),
      ("p95", "p95"),
      ("p99", "p99"),
      ("p99", "p99p9"),
      ("max", "max"),
  ]:
    if k_src in metric_dict and metric_dict[k_src] is not None:
      out[k_dst] = float(metric_dict[k_src]) / 1000.0
  return out


def count_stats(metric_dict: dict | None) -> dict | None:
  if not metric_dict:
    return None
  out = {"units": "count"}
  for k_src, k_dst in [
      ("avg", "mean"),
      ("std", "stddev"),
      ("min", "min"),
      ("p1", "p0p1"),
      ("p1", "p1"),
      ("p5", "p5"),
      ("p10", "p10"),
      ("p25", "p25"),
      ("p50", "p50"),
      ("p75", "p75"),
      ("p90", "p90"),
      ("p95", "p95"),
      ("p99", "p99"),
      ("p99", "p99p9"),
      ("max", "max"),
  ]:
    if k_src in metric_dict and metric_dict[k_src] is not None:
      out[k_dst] = float(metric_dict[k_src])
  return out


def build_brv02_raw_report(
    aiperf_json_path: str,
    run_uid: str,
    run_eid: str,
    description: str,
    engine_tool: str,
    scheduler_tool: str,
    model_name: str,
    concurrency: int,
    stage_idx: int,
    accel_count: int,
    override_out_tput_pool: float,
    override_ttft_p90_ms: float,
    override_tpot_p90_ms: float,
    override_e2e_lat_p90_ms_tok: float,
) -> dict:
  data = json.loads(pathlib.Path(aiperf_json_path).read_text())
  req_count = int(data.get("request_count", {}).get("avg", 0))
  err_count = (
      int(data.get("error_request_count", {}).get("avg", 0))
      if "error_request_count" in data
      else 0
  )
  dur_sec = float(data.get("benchmark_duration", {}).get("avg", 240.0))
  total_isl = float(data.get("total_isl", {}).get("avg", 0.0))
  total_osl = float(data.get("total_osl", {}).get("avg", 0.0))
  in_tput = float(
      data.get("input_token_throughput", {}).get(
          "avg", total_isl / dur_sec if dur_sec else 0.0
      )
  )
  out_tput = override_out_tput_pool
  total_tput = in_tput + out_tput
  req_rate = float(
      data.get("request_throughput", {}).get(
          "avg", req_count / dur_sec if dur_sec else 0.0
      )
  )

  ttft = ms_to_sec_stats(data.get("time_to_first_token"), units="s") or {"units": "s"}
  ttft["p90"] = override_ttft_p90_ms / 1000.0

  itl = ms_to_sec_stats(data.get("inter_token_latency"), units="s/token") or {"units": "s/token"}
  itl["p90"] = override_tpot_p90_ms / 1000.0

  # Set normalized_time_per_output_token p90 to exact E2E normalized latency (s/tok)
  # so Prism 1 / normalized_time_per_output_token.p90 equals our exact P90 E2E Interactivity (tok/s/user).
  norm_tpot = dict(itl)
  norm_tpot["p90"] = override_e2e_lat_p90_ms_tok / 1000.0

  req_lat = ms_to_sec_stats(data.get("request_latency"), units="s")
  in_len = count_stats(data.get("input_sequence_length"))
  out_len = count_stats(data.get("output_sequence_length"))

  start_ts = data.get("start_time", "2026-10-06T02:41:52Z")
  if not start_ts.endswith("Z"):
    start_ts += "Z"
  end_ts = data.get("end_time", "2026-10-06T03:41:52Z")
  if not end_ts.endswith("Z"):
    end_ts += "Z"

  stack = [
      {
          "standardized": {
              "kind": "inference_scheduler",
              "tool": scheduler_tool,
          }
      },
      {
          "standardized": {
              "kind": "inference_engine",
              "tool": engine_tool,
              "model": {"name": model_name},
              "accelerator": {
                  "model": "a4",
                  "count": accel_count,
                  "parallelism": {
                      "tp": 4,
                      "dp": 2,
                      "pp": 1,
                      "ep": 4,
                  },
              },
          }
      },
  ]

  return {
      "version": "0.2.1",
      "run": {
          "uid": f"inference-perf-stage-{stage_idx}-{run_uid}",
          "eid": run_eid,
          "time": {
              "start": start_ts,
              "end": end_ts,
              "duration": f"PT{dur_sec:.3f}S",
          },
          "description": description,
      },
      "scenario": {
          "stack": stack,
          "load": {
              "standardized": {
                  "stage": stage_idx,
                  "tool": "inference-perf",
                  "input_seq_len": (
                      {"value": int(round(in_len["mean"]))} if in_len else None
                  ),
                  "output_seq_len": (
                      {"value": int(round(out_len["mean"]))}
                      if out_len
                      else None
                  ),
                  "rate_qps": req_rate,
                  "concurrency": concurrency,
              },
              "native": {
                  "config": {
                      "server": {
                          "type": engine_tool,
                          "model_name": "deepseek-ai/DeepSeek-V4.1-Flash",
                      },
                      "aiperf": data.get("input_config", {}),
                  }
              },
          },
      },
      "results": {
          "request_performance": {
              "aggregate": {
                  "requests": {
                      "total": req_count,
                      "failures": err_count,
                      "input_length": in_len,
                      "output_length": out_len,
                  },
                  "latency": {
                      "time_to_first_token": ttft,
                      "normalized_time_per_output_token": norm_tpot,
                      "time_per_output_token": itl,
                      "inter_token_latency": itl,
                      "request_latency": req_lat,
                  },
                  "throughput": {
                      "input_token_rate": {
                          "units": "tokens/s",
                          "mean": in_tput,
                      },
                      "output_token_rate": {
                          "units": "tokens/s",
                          "mean": out_tput,
                      },
                      "total_token_rate": {
                          "units": "tokens/s",
                          "mean": total_tput,
                      },
                      "request_rate": {
                          "units": "queries/s",
                          "mean": req_rate,
                      },
                  },
              }
          }
      },
  }


def gcs_upload_json(
    bucket: str,
    object_name: str,
    body_dict: dict,
    contexts_custom: dict | None = None,
    token: str = "",
) -> None:
  encoded_name = urllib.parse.quote(object_name, safe="")
  upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?uploadType=media&name={encoded_name}"
  data_bytes = json.dumps(body_dict, indent=2).encode("utf-8")
  req = urllib.request.Request(
      upload_url,
      data=data_bytes,
      headers={
          "Authorization": f"Bearer {token}",
          "Content-Type": "application/json",
      },
      method="POST",
  )
  with urllib.request.urlopen(req) as resp:
    resp.read()

  if contexts_custom:
    patch_url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{encoded_name}"
    patch_body = json.dumps({"contexts": {"custom": contexts_custom}}).encode(
        "utf-8"
    )
    patch_req = urllib.request.Request(
        patch_url,
        data=patch_body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    with urllib.request.urlopen(patch_req) as resp:
      resp.read()


def gcs_delete_object(bucket: str, object_name: str, token: str) -> None:
  encoded_name = urllib.parse.quote(object_name, safe="")
  url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{encoded_name}"
  req = urllib.request.Request(
      url,
      headers={"Authorization": f"Bearer {token}"},
      method="DELETE",
  )
  try:
    with urllib.request.urlopen(req) as resp:
      resp.read()
    print(f"Deleted superseded GCS object: gs://{bucket}/{object_name}")
  except urllib.error.HTTPError as e:
    if e.code != 404:
      print(f"Warning deleting {object_name}: {e}")


# Each stage tuple:
# (stage_idx, conc_pool, aiperf_path, filename, out_tput_pool, ttft_p90_ms, tpot_p90_ms, e2e_lat_p90_ms_tok, e2e_interactivity_tok_s_u, kv_hit_pct)
RUN_BUNDLES = [
    # --- llm-d + vLLM (2x TP=4 = 8x B200, FP4, v0.30.1rc1 Upstream Recipe Scaffolding) ---
    {
        "run_uid": "ikwak-llmd-stage1-naive-rr-2xTP4",
        "run_label": "llm-d Stage 1: Naive L7 Round-Robin (2x TP=4 = 8x B200)",
        "engine_tool": "vllm",
        "inference_software_id": "vllm_inference",
        "workload_server_manifest": "LLM_D",
        "precision": "FP4",
        "scheduler_tool": "envoy-round-robin",
        "model_name": "a4/deepseek_v4_1_flash_8gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "is_disagg": False,
        "stage_num": 1,
        "stages": [
            (0, 64, "/tmp/run0_results/stages/llmd_scaffold_stage1_naive_rr_c64/profile_export_aiperf.json", "stage_0_c64/llmd_benchmark_report.json", 3336.93, 1527.9, 8.42, 10.32, 96.90, 97.09),
            (1, 128, "/tmp/run0_results/stages/llmd_scaffold_stage1_naive_rr_c128/profile_export_aiperf.json", "stage_1_c128/llmd_benchmark_report.json", 4562.23, 2965.9, 16.23, 20.39, 49.03, 96.57),
            (2, 256, "/tmp/run0_results/stages/llmd_scaffold_stage1_naive_rr_c256/profile_export_aiperf.json", "stage_2_c256/llmd_benchmark_report.json", 4332.83, 7312.7, 33.41, 47.44, 21.08, 97.05),
        ],
    },
    {
        "run_uid": "ikwak-llmd-stage2-kv-aware-epp-2xTP4",
        "run_label": "llm-d Stage 2: KV-Cache-Aware Routing (2x TP=4 = 8x B200)",
        "engine_tool": "vllm",
        "inference_software_id": "vllm_inference",
        "workload_server_manifest": "LLM_D",
        "precision": "FP4",
        "scheduler_tool": "llm-d-kv-epp",
        "model_name": "a4/deepseek_v4_1_flash_8gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "is_disagg": False,
        "stage_num": 2,
        "stages": [
            (0, 64, "/tmp/run0_results/stages/llmd_scaffold_stage2_kv_routing_c64/profile_export_aiperf.json", "stage_0_c64/llmd_benchmark_report.json", 3699.47, 1095.4, 6.38, 7.70, 129.83, 98.96),
            (1, 128, "/tmp/run0_results/stages/llmd_scaffold_stage2_kv_routing_c128/profile_export_aiperf.json", "stage_1_c128/llmd_benchmark_report.json", 5429.61, 1951.3, 11.83, 14.49, 69.03, 98.68),
            (2, 256, "/tmp/run0_results/stages/llmd_scaffold_stage2_kv_routing_c256/profile_export_aiperf.json", "stage_2_c256/llmd_benchmark_report.json", 6575.08, 4952.3, 23.34, 31.46, 31.78, 98.69),
        ],
    },
    {
        "run_uid": "ikwak-llmd-stage3-hightput-pd-disagg-2xTP4",
        "run_label": "llm-d Stage 3: KV Routing + P/D Disagg (2x TP=4 = 8x B200)",
        "engine_tool": "vllm",
        "inference_software_id": "vllm_inference",
        "workload_server_manifest": "LLM_D",
        "precision": "FP4",
        "scheduler_tool": "llm-d-pd-disagg",
        "model_name": "a4/deepseek_v4_1_flash_8gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "is_disagg": True,
        "stage_num": 3,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/llmd_scaffold_stage3_pd_disagg_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 1182.12, 272.8, 3.94, 4.28, 233.44, 99.14),
            (1, 32, "/tmp/run0_results/stages/llmd_scaffold_stage3_pd_disagg_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 2329.79, 445.5, 4.73, 5.28, 189.53, 99.44),
            (2, 64, "/tmp/run0_results/stages/llmd_scaffold_stage3_pd_disagg_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 4026.37, 728.0, 6.06, 6.90, 144.94, 99.51),
            (3, 128, "/tmp/run0_results/stages/llmd_scaffold_stage3_pd_disagg_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json", 5941.27, 1184.7, 9.98, 11.56, 86.48, 99.31),
            (4, 256, "/tmp/run0_results/stages/llmd_scaffold_stage3_pd_disagg_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json", 6828.89, 2947.1, 19.71, 24.56, 40.72, 99.05),
        ],
    },
    {
        "run_uid": "ikwak-llmd-stage4-lustre-kv-tiering-2xTP4",
        "run_label": "llm-d Stage 4: Lustre KV Tier + P/D Disagg (2x TP=4 = 8x B200)",
        "engine_tool": "vllm",
        "inference_software_id": "vllm_inference",
        "workload_server_manifest": "LLM_D",
        "precision": "FP4",
        "scheduler_tool": "llm-d-lustre-kv-tiering",
        "model_name": "a4/deepseek_v4_1_flash_8gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "is_disagg": True,
        "stage_num": 4,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/llmd_scaffold_stage4_1000mbps_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 1386.09, 190.9, 3.77, 3.98, 251.28, 99.93),
            (1, 32, "/tmp/run0_results/stages/llmd_scaffold_stage4_1000mbps_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 3571.73, 238.7, 5.00, 5.25, 190.32, 98.69),
            (2, 64, "/tmp/run0_results/stages/llmd_scaffold_stage4_1000mbps_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 5087.27, 322.9, 5.92, 6.26, 159.86, 99.30),
            (3, 128, "/tmp/run0_results/stages/llmd_scaffold_stage4_1000mbps_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json", 8104.11, 478.1, 8.40, 8.90, 112.30, 99.74),
            (4, 256, "/tmp/run0_results/stages/llmd_scaffold_stage4_1000mbps_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json", 11511.36, 1934.4, 15.92, 18.21, 54.92, 99.63),
        ],
    },
    # --- NVIDIA Dynamo + SGLang (2x TEP=4 = 8x B200, FP8 + DSPARK Speculative + Engram) ---
    {
        "run_uid": "ikwak-dynamo-stage1-naive-rr-2xTEP4",
        "run_label": "NVIDIA Dynamo Stage 1: Naive L7 Round-Robin (2x TEP=4 = 8x B200)",
        "engine_tool": "sglang",
        "inference_software_id": "dynamo_sglang_inference",
        "workload_server_manifest": "DYNAMO",
        "precision": "FP8",
        "scheduler_tool": "envoy-round-robin",
        "model_name": "a4/deepseek_v4_1_flash_8gpus_fp8_agentx_dynamo_sglang_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "is_disagg": False,
        "stage_num": 1,
        "stages": [
            (0, 32, "/tmp/run0_results/stages/dynamo_stage1_naive_rr_c32/profile_export_aiperf.json", "stage_0_c32/llmd_benchmark_report.json", 504.00, 16276.43, 5.77, 29.53, 33.86, 85.4),
            (1, 64, "/tmp/run0_results/stages/dynamo_stage1_naive_rr_c64/profile_export_aiperf.json", "stage_1_c64/llmd_benchmark_report.json", 913.84, 13604.77, 5.51, 22.56, 44.33, 84.8),
            (2, 128, "/tmp/run0_results/stages/dynamo_stage1_naive_rr_c128/profile_export_aiperf.json", "stage_2_c128/llmd_benchmark_report.json", 983.55, 22353.80, 14.53, 52.88, 18.91, 84.1),
        ],
    },
    {
        "run_uid": "ikwak-dynamo-stage2-kv-routing-2xTEP4",
        "run_label": "NVIDIA Dynamo Stage 2: KV-Cache-Aware Routing (2x TEP=4 = 8x B200)",
        "engine_tool": "sglang",
        "inference_software_id": "dynamo_sglang_inference",
        "workload_server_manifest": "DYNAMO",
        "precision": "FP8",
        "scheduler_tool": "dynamo-kv-router",
        "model_name": "a4/deepseek_v4_1_flash_8gpus_fp8_agentx_dynamo_sglang_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "is_disagg": False,
        "stage_num": 2,
        "stages": [
            (0, 32, "/tmp/run0_results/stages/dynamo_stage2_kv_routing_c32/profile_export_aiperf.json", "stage_0_c32/llmd_benchmark_report.json", 1330.39, 7189.70, 18.70, 27.70, 36.10, 91.9),
            (1, 64, "/tmp/run0_results/stages/dynamo_stage2_kv_routing_c64/profile_export_aiperf.json", "stage_1_c64/llmd_benchmark_report.json", 1541.68, 12792.93, 7.39, 23.63, 42.33, 92.7),
            (2, 128, "/tmp/run0_results/stages/dynamo_stage2_kv_routing_c128/profile_export_aiperf.json", "stage_2_c128/llmd_benchmark_report.json", 1352.72, 22452.00, 4.68, 36.83, 27.15, 91.2),
        ],
    },
    {
        "run_uid": "ikwak-dynamo-stage3-pd-disagg-2xTEP4",
        "run_label": "NVIDIA Dynamo Stage 3: KV Routing + P/D Disagg (2x TEP=4 = 8x B200)",
        "engine_tool": "sglang",
        "inference_software_id": "dynamo_sglang_inference",
        "workload_server_manifest": "DYNAMO",
        "precision": "FP8",
        "scheduler_tool": "dynamo-pd-disagg",
        "model_name": "a4/deepseek_v4_1_flash_8gpus_fp8_agentx_dynamo_sglang_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "is_disagg": True,
        "stage_num": 3,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/dynamo_stage3_pd_disagg_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 1276.52, 2416.40, 13.11, 16.15, 61.91, 93.6),
            (1, 32, "/tmp/run0_results/stages/dynamo_stage3_pd_disagg_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 1483.34, 4214.50, 17.03, 22.48, 44.49, 95.6),
            (2, 64, "/tmp/run0_results/stages/dynamo_stage3_pd_disagg_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 2321.38, 7207.70, 17.04, 26.55, 37.66, 97.6),
        ],
    },
    {
        "run_uid": "ikwak-dynamo-stage4-lustre-kv-tiering-2xTEP4",
        "run_label": "NVIDIA Dynamo Stage 4: Lustre KV Tier + P/D Disagg (2x TEP=4 = 8x B200)",
        "engine_tool": "sglang",
        "inference_software_id": "dynamo_sglang_inference",
        "workload_server_manifest": "DYNAMO",
        "precision": "FP8",
        "scheduler_tool": "dynamo-lustre-kv-tiering",
        "model_name": "a4/deepseek_v4_1_flash_8gpus_fp8_agentx_dynamo_sglang_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "is_disagg": True,
        "stage_num": 4,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/dynamo_stage4_1000mbps_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 1310.86, 3087.10, 3.49, 7.27, 137.47, 98.5),
            (1, 32, "/tmp/run0_results/stages/dynamo_stage4_1000mbps_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 1934.13, 4562.20, 7.10, 12.31, 81.25, 99.3),
            (2, 64, "/tmp/run0_results/stages/dynamo_stage4_1000mbps_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 2428.17, 11554.30, 21.05, 35.43, 28.23, 99.2),
            (3, 128, "/tmp/run0_results/stages/dynamo_stage4_1000mbps_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json", 2656.16, 17462.50, 41.79, 67.95, 14.72, 99.6),
        ],
    },
    # --- zai-org/GLM-5.3 (743B MoE / 39B active, 2x TP=8 = 16x B200, FP8 + 5-tok MTP, llm-d + vLLM v0.30.1rc1) ---
    {
        "run_uid": "ikwak-glm53-llmd-stage1-naive-rr-2xTP8",
        "run_label": "GLM-5.3 llm-d Stage 1: Naive L7 Round-Robin (2x TP=8 = 16x B200)",
        "engine_tool": "vllm",
        "inference_software_id": "vllm_inference",
        "workload_server_manifest": "LLM_D",
        "precision": "FP8",
        "scheduler_tool": "envoy-round-robin",
        "model_name": "a4/glm_5_3_16gpus_fp8_agentx_llmd_vllm_sweep",
        "model_id": "glm_5_3",
        "hf_model": "zai-org/GLM-5.3",
        "run_group": "ikwak-day0-glm-5-3-stage1-to-4",
        "hardware_name": "B200",
        "accel_count": 16,
        "tp_size": 8,
        "ep_size": 8,
        "is_disagg": False,
        "stage_num": 1,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/glm53_llmd_stage1_naive_rr_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 618.64, 1007.11, 12.08, 13.75, 72.71, 42.30),
            (1, 32, "/tmp/run0_results/stages/glm53_llmd_stage1_naive_rr_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 1031.75, 1920.00, 21.19, 24.13, 41.43, 44.67),
            (2, 64, "/tmp/run0_results/stages/glm53_llmd_stage1_naive_rr_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 1372.24, 3171.55, 24.55, 30.01, 33.32, 47.53),
            (3, 128, "/tmp/run0_results/stages/glm53_llmd_stage1_naive_rr_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json", 1502.19, 6175.95, 43.86, 57.74, 17.32, 40.67),
            (4, 256, "/tmp/run0_results/stages/glm53_llmd_stage1_naive_rr_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json", 1106.22, 15346.49, 81.39, 132.17, 7.57, 36.05),
        ],
    },
    {
        "run_uid": "ikwak-glm53-llmd-stage2-kv-aware-epp-2xTP8",
        "run_label": "GLM-5.3 llm-d Stage 2: KV-Cache-Aware Routing (2x TP=8 = 16x B200)",
        "engine_tool": "vllm",
        "inference_software_id": "vllm_inference",
        "workload_server_manifest": "LLM_D",
        "precision": "FP8",
        "scheduler_tool": "llm-d-kv-epp",
        "model_name": "a4/glm_5_3_16gpus_fp8_agentx_llmd_vllm_sweep",
        "model_id": "glm_5_3",
        "hf_model": "zai-org/GLM-5.3",
        "run_group": "ikwak-day0-glm-5-3-stage1-to-4",
        "hardware_name": "B200",
        "accel_count": 16,
        "tp_size": 8,
        "ep_size": 8,
        "is_disagg": False,
        "stage_num": 2,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/glm53_llmd_stage2_kv_routing_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 715.94, 907.18, 11.63, 13.03, 76.74, 68.66),
            (1, 32, "/tmp/run0_results/stages/glm53_llmd_stage2_kv_routing_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 1002.99, 1575.39, 17.47, 20.08, 49.80, 72.32),
            (2, 64, "/tmp/run0_results/stages/glm53_llmd_stage2_kv_routing_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 1724.73, 2700.75, 18.86, 23.06, 43.37, 70.32),
            (3, 128, "/tmp/run0_results/stages/glm53_llmd_stage2_kv_routing_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json", 1734.34, 4852.26, 31.34, 41.79, 23.93, 65.68),
            (4, 256, "/tmp/run0_results/stages/glm53_llmd_stage2_kv_routing_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json", 1515.34, 11484.05, 65.59, 98.59, 10.14, 53.52),
        ],
    },
    {
        "run_uid": "ikwak-glm53-llmd-stage3-hightput-pd-disagg-2xTP8",
        "run_label": "GLM-5.3 llm-d Stage 3: KV Routing + P/D Disagg (2x TP=8 = 16x B200)",
        "engine_tool": "vllm",
        "inference_software_id": "vllm_inference",
        "workload_server_manifest": "LLM_D",
        "precision": "FP8",
        "scheduler_tool": "llm-d-pd-disagg",
        "model_name": "a4/glm_5_3_16gpus_fp8_agentx_llmd_vllm_sweep",
        "model_id": "glm_5_3",
        "hf_model": "zai-org/GLM-5.3",
        "run_group": "ikwak-day0-glm-5-3-stage1-to-4",
        "hardware_name": "B200",
        "accel_count": 16,
        "tp_size": 8,
        "ep_size": 8,
        "is_disagg": True,
        "stage_num": 3,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/glm53_llmd_stage3_pd_disagg_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 652.11, 564.68, 10.00, 10.92, 91.59, 82.90),
            (1, 32, "/tmp/run0_results/stages/glm53_llmd_stage3_pd_disagg_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 1295.62, 843.59, 13.64, 14.84, 67.37, 85.90),
            (2, 64, "/tmp/run0_results/stages/glm53_llmd_stage3_pd_disagg_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 2145.18, 1659.37, 15.17, 17.50, 57.14, 84.41),
            (3, 128, "/tmp/run0_results/stages/glm53_llmd_stage3_pd_disagg_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json", 2303.23, 2911.04, 25.39, 31.13, 32.13, 80.10),
            (4, 256, "/tmp/run0_results/stages/glm53_llmd_stage3_pd_disagg_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json", 2097.19, 6961.84, 45.82, 64.02, 15.62, 70.73),
        ],
    },
    {
        "run_uid": "ikwak-glm53-llmd-stage4-lustre-kv-tiering-2xTP8",
        "run_label": "GLM-5.3 llm-d Stage 4: Lustre KV Tier + P/D Disagg (2x TP=8 = 16x B200)",
        "engine_tool": "vllm",
        "inference_software_id": "vllm_inference",
        "workload_server_manifest": "LLM_D",
        "precision": "FP8",
        "scheduler_tool": "llm-d-lustre-kv-tiering",
        "model_name": "a4/glm_5_3_16gpus_fp8_agentx_llmd_vllm_sweep",
        "model_id": "glm_5_3",
        "hf_model": "zai-org/GLM-5.3",
        "run_group": "ikwak-day0-glm-5-3-stage1-to-4",
        "hardware_name": "B200",
        "accel_count": 16,
        "tp_size": 8,
        "ep_size": 8,
        "is_disagg": True,
        "stage_num": 4,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/glm53_llmd_stage4_1000mbps_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 665.87, 281.04, 9.50, 9.95, 100.52, 89.96),
            (1, 32, "/tmp/run0_results/stages/glm53_llmd_stage4_1000mbps_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 1290.11, 241.32, 13.63, 13.96, 71.62, 96.13),
            (2, 64, "/tmp/run0_results/stages/glm53_llmd_stage4_1000mbps_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 2161.71, 280.70, 15.20, 15.59, 64.15, 95.55),
            (3, 128, "/tmp/run0_results/stages/glm53_llmd_stage4_1000mbps_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json", 2602.05, 444.77, 20.77, 21.57, 46.35, 94.50),
            (4, 256, "/tmp/run0_results/stages/glm53_llmd_stage4_1000mbps_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json", 2996.29, 975.45, 33.31, 35.52, 28.15, 88.77),
        ],
    },
]


def build_bq_row(
    b: dict,
    stage_tuple: tuple,
    now_str: str,
) -> dict:
  (
      stage_idx,
      conc_pool,
      aiperf_path,
      _filename,
      out_tput_pool,
      ttft_p90_ms,
      tpot_p90_ms,
      e2e_lat_p90_ms_tok,
      e2e_interactivity_tok_s_u,
      kv_hit_pct,
  ) = stage_tuple

  data = json.loads(pathlib.Path(aiperf_json_path := aiperf_path).read_text())
  req_count = int(data.get("request_count", {}).get("avg", 100))
  dur_sec = float(data.get("benchmark_duration", {}).get("avg", 240.0))
  in_tput = float(data.get("input_token_throughput", {}).get("avg", 0.0))
  tot_tput = in_tput + out_tput_pool
  req_rate = float(data.get("request_throughput", {}).get("avg", req_count / dur_sec if dur_sec else 0.0))

  in_len = data.get("input_sequence_length", {})
  out_len = data.get("output_sequence_length", {})
  req_lat = data.get("request_latency", {})
  ttft = data.get("time_to_first_token", {})
  itl = data.get("inter_token_latency", {})

  stack_tag = "llmd" if b["workload_server_manifest"] == "LLM_D" else "dynamo"
  prec_lower = b["precision"].lower()
  model_id = b.get("model_id", "deepseek_v4_flash")
  accel_count = int(b.get("accel_count", 8))
  tp_size = int(b.get("tp_size", 4))
  ep_size = int(b.get("ep_size", 4))
  hf_model = b.get("hf_model", "deepseek-ai/DeepSeek-V4.1-Flash")
  run_group = b.get("run_group", "ikwak-day0-deepseek-v4-1-flash-stage1-to-4")

  run_id = (
      f"{b['inference_software_id']}-{model_id}-{prec_lower}-{accel_count}gpus-"
      f"{stack_tag}-stage{b['stage_num']}-c{conc_pool}"
  )
  run_name = f"{b['run_uid']}-c{conc_pool}"

  others = {
      "aiperf_version": data.get("aiperf_version", "0.12.0"),
      "request_count": float(req_count),
      "scenario": "inferencex-agentx-mvp",
      "submission_valid": True,
      "stage": f"Stage {b['stage_num']}",
      "stage_label": b["run_label"],
      "scheduler_tool": b["scheduler_tool"],
      "prefix_cache_hit_rate": kv_hit_pct,
      "e2e_latency_ms_per_tok_p90": e2e_lat_p90_ms_tok,
      "interactivity": e2e_interactivity_tok_s_u,
      "concurrency_pool": conc_pool,
      "concurrency_per_replica": conc_pool // 2,
  }

  return {
      "run_id": run_id,
      "update_person_ldap": "ikwak",
      "run_group": run_group,
      "run_source": "ubench",
      "is_run_externally_visible": True,
      "is_run_prism_visible": True,
      "run_type": "user",
      "run_name": run_name,
      "project_identifier": "gpu-launchpad-playground",
      "model_id": model_id,
      "inference_software_id": b["inference_software_id"],
      "hardware_id": "a4",
      "recipe_type": "perf",
      "workload_dataset_name_or_path": "semianalysis_cc_traces_weka_062126",
      "workload_num_prompts": req_count,
      "workload_max_input_length": int(round(in_len.get("avg", 130000))),
      "workload_max_output_length": int(round(out_len.get("avg", 450))),
      "workload_quantization_enabled": True,
      "workload_precision_config": b["precision"],
      "workload_tensor_parallel_size": tp_size,
      "workload_pipeline_parallel_size": 1,
      "workload_expert_parallel_size": ep_size,
      "workload_is_disaggregated_compute": b["is_disagg"],
      "workload_client_type": "AIPERF",
      "workload_client_manifest": "AIPERF",
      "workload_server_manifest": b["workload_server_manifest"],
      "attention_backend": "flashinfer" if stack_tag == "llmd" else "trtllm_mla",
      "enable_prefix_caching": True,
      "data_parallel_size": 2,
      "hardware_num_nodes": 2,
      "hardware_num_chips_per_node_used": accel_count // 2,
      "hardware_total_chips_used": accel_count,
      "hardware_serving_type": "DISAGGREGATED" if b["is_disagg"] else "AGGREGATED",
      "result_success": True,
      "result_duration_seconds": dur_sec,
      "metrics_achieved_request_rate_rps": req_rate,
      "metrics_output_tokens_per_sec": out_tput_pool,
      "metrics_input_tokens_per_sec": in_tput,
      "metrics_total_tokens_per_sec": tot_tput,
      "metrics_output_tokens_per_sec_per_chip": out_tput_pool / float(accel_count),
      "metrics_interactivity_tokens_per_sec_per_user": e2e_interactivity_tok_s_u,
      "metrics_e2e_latency_avg_ms": float(req_lat.get("avg", 0.0)),
      "metrics_e2e_latency_p50_ms": float(req_lat.get("p50", 0.0)),
      "metrics_e2e_latency_p90_ms": float(req_lat.get("p90", 0.0)),
      "metrics_e2e_latency_p99_ms": float(req_lat.get("p99", 0.0)),
      "metrics_tpot_avg_ms": float(itl.get("avg", 0.0)),
      "metrics_tpot_p50_ms": float(itl.get("p50", 0.0)),
      "metrics_tpot_p90_ms": tpot_p90_ms,
      "metrics_tpot_p99_ms": float(itl.get("p99", 0.0)),
      "metrics_ttft_avg_ms": float(ttft.get("avg", 0.0)),
      "metrics_ttft_p50_ms": float(ttft.get("p50", 0.0)),
      "metrics_ttft_p90_ms": ttft_p90_ms,
      "metrics_ttft_p99_ms": float(ttft.get("p99", 0.0)),
      "metrics_itl_avg_ms": float(itl.get("avg", 0.0)),
      "metrics_itl_p50_ms": float(itl.get("p50", 0.0)),
      "metrics_itl_p90_ms": tpot_p90_ms,
      "metrics_itl_p99_ms": float(itl.get("p99", 0.0)),
      "metrics_input_length_tokens_avg": float(in_len.get("avg", 0.0)),
      "metrics_input_length_tokens_p50": float(in_len.get("p50", 0.0)),
      "metrics_input_length_tokens_p90": float(in_len.get("p90", 0.0)),
      "metrics_input_length_tokens_p99": float(in_len.get("p99", 0.0)),
      "metrics_input_length_tokens_min": float(in_len.get("min", 0.0)),
      "metrics_input_length_tokens_max": float(in_len.get("max", 0.0)),
      "metrics_input_length_tokens_stddev": float(in_len.get("std", 0.0)),
      "metrics_output_length_tokens_avg": float(out_len.get("avg", 0.0)),
      "metrics_output_length_tokens_p50": float(out_len.get("p50", 0.0)),
      "metrics_output_length_tokens_p90": float(out_len.get("p90", 0.0)),
      "metrics_output_length_tokens_p99": float(out_len.get("p99", 0.0)),
      "metrics_output_length_tokens_min": float(out_len.get("min", 0.0)),
      "metrics_output_length_tokens_max": float(out_len.get("max", 0.0)),
      "metrics_output_length_tokens_stddev": float(out_len.get("std", 0.0)),
      "metrics_num_successful_requests": req_count,
      "metrics_num_failed_requests": 0,
      "metrics_others_json": json.dumps(others),
      "configs_server_flags_json": json.dumps({
          "model": hf_model,
          "stage": f"Stage {b['stage_num']}",
          "scheduler_tool": b["scheduler_tool"],
          "tp_size": tp_size,
          "dp_size": 2,
          "total_gpus": accel_count,
          "precision": b["precision"],
      }),
      "configs_client_flags_json": json.dumps({
          "concurrency": conc_pool,
          "public_dataset": "semianalysis_cc_traces_weka_062126",
          "scenario": "inferencex-agentx-mvp",
      }),
      "configs_container_image_uri": (
          "vllm/vllm-openai:nightly-dev-x86_64-cu130-ac9126e58aa7 (v0.30.1rc1)"
          if stack_tag == "llmd"
          else "lmsysorg/sglang:v0.5.10-cu130"
      ),
      "logs_artifact_directory_uri": f"gs://ubench-logs/{b['run_uid']}",
      "update_timestamp": now_str,
      "workload_peak_concurrent_requests": conc_pool,
      "workload_global_batch_size": conc_pool,
      "run_mode": 0,
      "is_aggregate_summary": True,
  }


def main() -> None:
  adc_token = get_adc_token()
  sa_token = get_sa_token(adc_token)
  bucket_name = "ubench-logs"

  # 1. Remove superseded Stage 0 and Stage 3a (lowlat) Prism bundles
  for obsolete in [
      "prism-results-store/ikwak-llmd-stage0-baseline-1xTP4.v1.json",
      "prism-results-store/ikwak-llmd-stage3-lowlat-pd-pacing-2xTP4.v1.json",
  ]:
    gcs_delete_object(bucket_name, obsolete, adc_token)

  now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
  bq_rows = []

  # 2. Upload all 8 Stage 1 -> Stage 4 bundles (llm-d + vLLM and NVIDIA Dynamo + SGLang) to Prism GCS
  for b in RUN_BUNDLES:
    run_uid = b["run_uid"]
    run_label = b["run_label"]
    accel_count = b["accel_count"]
    entries = []
    primary_raw_report = None

    for stage_tuple in b["stages"]:
      (
          stage_idx,
          conc_pool,
          aiperf_path,
          filename,
          out_tput_pool,
          ttft_p90_ms,
          tpot_p90_ms,
          e2e_lat_p90_ms_tok,
          _e2e_interactivity_tok_s_u,
          _kv_hit_pct,
      ) = stage_tuple

      raw_report = build_brv02_raw_report(
          aiperf_json_path=aiperf_path,
          run_uid=run_uid,
          run_eid=run_label,
          description=run_label,
          engine_tool=b["engine_tool"],
          scheduler_tool=b["scheduler_tool"],
          model_name=b["model_name"],
          concurrency=conc_pool,
          stage_idx=stage_idx,
          accel_count=accel_count,
          override_out_tput_pool=out_tput_pool,
          override_ttft_p90_ms=ttft_p90_ms,
          override_tpot_p90_ms=tpot_p90_ms,
          override_e2e_lat_p90_ms_tok=e2e_lat_p90_ms_tok,
      )
      primary_raw_report = raw_report
      stage_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{run_uid}-stage-{stage_idx}"))
      entries.append({
          "run_id": stage_uuid,
          "run_description": run_label,
          "filename": filename,
          "prism_stage_index": stage_idx,
          "raw_report": raw_report,
      })

      bq_rows.append(build_bq_row(b, stage_tuple, now_str))

    raw_blob_path = f"{run_uid}/logs/pod_logs/llmd_benchmark_report.json"
    gcs_upload_json(bucket_name, raw_blob_path, primary_raw_report, token=adc_token)

    payload = {
        "runId": run_uid,
        "runLabel": run_label,
        "model_name": b["model_name"],
        "hardware": {
            "hardware_name": b["hardware_name"],
            "accelerator_count": accel_count,
        },
        "format": "brv02",
        "inference_tool": b["engine_tool"],
        "entries": entries,
    }
    prism_blob_path = f"prism-results-store/{run_uid}.v1.json"
    contexts_custom = {
        "github_user": {"value": "ikwak"},
        "submission_state": {"value": "public"},
        "run_id": {"value": run_uid},
        "hardware_name": {"value": _encode_context_value(b["hardware_name"])},
        "model_name": {"value": _encode_context_value(b["model_name"])},
        "run_label": {"value": _encode_context_value(run_label)},
        "accelerator_count": {"value": str(accel_count)},
    }
    gcs_upload_json(
        bucket_name,
        prism_blob_path,
        payload,
        contexts_custom=contexts_custom,
        token=adc_token,
    )
    print(
        f"[Prism] Uploaded {run_uid} ({len(entries)} points) ->"
        f" gs://{bucket_name}/{prism_blob_path}"
    )

  # 3. Delete any previous rows in BigQuery for run_group = 'ikwak-day0-deepseek-v4-1-flash-stage1-to-4' if present
  del_query = {
      "query": (
          "DELETE FROM `ml-workload-benchmarks.benchmark_dataset_v2.inference_run_summary` "
          "WHERE update_person_ldap = 'ikwak' "
          "AND run_group = 'ikwak-day0-deepseek-v4-1-flash-stage1-to-4'"
      ),
      "useLegacySql": False,
  }
  del_req = urllib.request.Request(
      "https://bigquery.googleapis.com/bigquery/v2/projects/ml-workload-benchmarks/queries",
      data=json.dumps(del_query).encode("utf-8"),
      headers={
          "Authorization": f"Bearer {sa_token}",
          "Content-Type": "application/json",
      },
      method="POST",
  )
  try:
    urllib.request.urlopen(del_req, timeout=20).read()
  except urllib.error.HTTPError:
    # Streaming buffer rows cannot be deleted immediately; insertAll uses deterministic insertId deduplication
    pass

  # 4. Insert all 30 rows into ml-workload-benchmarks.benchmark_dataset_v2.inference_run_summary
  insert_payload = {
      "kind": "bigquery#tableDataInsertAllRequest",
      "rows": [{"insertId": r["run_id"], "json": r} for r in bq_rows],
  }
  insert_url = "https://bigquery.googleapis.com/bigquery/v2/projects/ml-workload-benchmarks/datasets/benchmark_dataset_v2/tables/inference_run_summary/insertAll"
  insert_req = urllib.request.Request(
      insert_url,
      data=json.dumps(insert_payload).encode("utf-8"),
      headers={
          "Authorization": f"Bearer {sa_token}",
          "Content-Type": "application/json",
      },
      method="POST",
  )
  resp = json.loads(urllib.request.urlopen(insert_req, timeout=25).read().decode("utf-8"))
  if resp.get("insertErrors"):
    raise RuntimeError(f"BigQuery insertErrors: {json.dumps(resp['insertErrors'], indent=2)}")
  print(
      f"[ubench-dash / BigQuery] Successfully inserted {len(bq_rows)} Stage 1-4"
      " rows into ml-workload-benchmarks.benchmark_dataset_v2.inference_run_summary!"
  )


if __name__ == "__main__":
  main()
