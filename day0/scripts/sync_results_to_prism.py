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
    # --- llm-d + vLLM (2x TP=4 = 8x B200, FP4) ---
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
            (0, 64, "/tmp/run0_results/stages/stage1_naive_rr_c64/profile_export_aiperf.json", "stage_0_c64/llmd_benchmark_report.json", 1036.80, 1230.2, 7.51, 10.97, 91.19, 28.4),
            (1, 128, "/tmp/run0_results/stages/stage1_naive_rr_c128/profile_export_aiperf.json", "stage_1_c128/llmd_benchmark_report.json", 2300.64, 4205.5, 13.79, 25.78, 38.79, 28.4),
            (2, 256, "/tmp/run0_results/stages/stage1_naive_rr_c256/profile_export_aiperf.json", "stage_2_c256/llmd_benchmark_report.json", 3770.88, 4488.3, 23.80, 36.00, 27.78, 28.4),
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
            (0, 64, "/tmp/run0_results/stages/stage2_kv_routing_c64/profile_export_aiperf.json", "stage_0_c64/llmd_benchmark_report.json", 1114.32, 232.9, 3.92, 4.55, 219.54, 94.8),
            (1, 128, "/tmp/run0_results/stages/stage2_kv_routing_c128/profile_export_aiperf.json", "stage_1_c128/llmd_benchmark_report.json", 2742.72, 798.5, 4.24, 6.40, 156.24, 94.8),
            (2, 256, "/tmp/run0_results/stages/stage2_kv_routing_c256/profile_export_aiperf.json", "stage_2_c256/llmd_benchmark_report.json", 4941.68, 2033.3, 5.39, 10.74, 93.15, 94.8),
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
            (0, 16, "/tmp/run0_results/stages/stage3_pd_disagg_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 1373.32, 456.9, 4.15, 7.50, 133.33, 96.2),
            (1, 32, "/tmp/run0_results/stages/stage3_pd_disagg_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 2864.59, 390.8, 4.90, 6.28, 159.19, 96.2),
            (2, 64, "/tmp/run0_results/stages/stage3_pd_disagg_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 4751.20, 956.9, 8.14, 11.18, 89.41, 96.2),
            (3, 128, "/tmp/run0_results/stages/stage3_pd_disagg_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json", 7632.32, 1602.6, 15.11, 19.50, 51.29, 96.2),
            (4, 256, "/tmp/run0_results/stages/stage3_pd_disagg_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json", 9021.60, 8715.4, 29.36, 51.95, 19.25, 96.2),
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
            (0, 16, "/tmp/run0_results/stages/stage4_1000mbps_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json", 1694.33, 381.8, 3.77, 6.57, 152.25, 98.6),
            (1, 32, "/tmp/run0_results/stages/stage4_1000mbps_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json", 3611.98, 267.7, 4.56, 5.74, 174.09, 98.6),
            (2, 64, "/tmp/run0_results/stages/stage4_1000mbps_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json", 5310.30, 539.8, 7.48, 8.99, 111.18, 98.6),
            (3, 128, "/tmp/run0_results/stages/stage4_1000mbps_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json", 8486.54, 1350.4, 12.60, 16.18, 61.81, 98.6),
            (4, 256, "/tmp/run0_results/stages/stage4_1000mbps_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json", 12035.93, 1402.7, 19.06, 24.42, 40.95, 98.6),
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
  run_id = (
      f"{b['inference_software_id']}-deepseek_v4_flash-{prec_lower}-8gpus-"
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
      "run_group": "ikwak-day0-deepseek-v4-1-flash-stage1-to-4",
      "run_source": "ubench",
      "is_run_externally_visible": True,
      "is_run_prism_visible": True,
      "run_type": "user",
      "run_name": run_name,
      "project_identifier": "gpu-launchpad-playground",
      "model_id": "deepseek_v4_flash",
      "inference_software_id": b["inference_software_id"],
      "hardware_id": "a4",
      "recipe_type": "perf",
      "workload_dataset_name_or_path": "semianalysis_cc_traces_weka_062126",
      "workload_num_prompts": req_count,
      "workload_max_input_length": int(round(in_len.get("avg", 130000))),
      "workload_max_output_length": int(round(out_len.get("avg", 450))),
      "workload_quantization_enabled": True,
      "workload_precision_config": b["precision"],
      "workload_tensor_parallel_size": 4,
      "workload_pipeline_parallel_size": 1,
      "workload_expert_parallel_size": 4,
      "workload_is_disaggregated_compute": b["is_disagg"],
      "workload_client_type": "AIPERF",
      "workload_client_manifest": "AIPERF",
      "workload_server_manifest": b["workload_server_manifest"],
      "attention_backend": "flashinfer" if stack_tag == "llmd" else "trtllm_mla",
      "enable_prefix_caching": True,
      "data_parallel_size": 2,
      "hardware_num_nodes": 2,
      "hardware_num_chips_per_node_used": 4,
      "hardware_total_chips_used": 8,
      "hardware_serving_type": "DISAGGREGATED" if b["is_disagg"] else "AGGREGATED",
      "result_success": True,
      "result_duration_seconds": dur_sec,
      "metrics_achieved_request_rate_rps": req_rate,
      "metrics_output_tokens_per_sec": out_tput_pool,
      "metrics_input_tokens_per_sec": in_tput,
      "metrics_total_tokens_per_sec": tot_tput,
      "metrics_output_tokens_per_sec_per_chip": out_tput_pool / 8.0,
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
          "model": "deepseek-ai/DeepSeek-V4.1-Flash",
          "stage": f"Stage {b['stage_num']}",
          "scheduler_tool": b["scheduler_tool"],
          "tp_size": 4,
          "dp_size": 2,
          "total_gpus": 8,
          "precision": b["precision"],
      }),
      "configs_client_flags_json": json.dumps({
          "concurrency": conc_pool,
          "public_dataset": "semianalysis_cc_traces_weka_062126",
          "scenario": "inferencex-agentx-mvp",
      }),
      "configs_container_image_uri": (
          "vllm/vllm-openai:v0.19.0"
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
