"""Stages our multi-stage Run 0 reports to gs://ubench-logs and gs://ubench-logs/prism-results-store."""

import base64
import json
import pathlib
import subprocess
import urllib.parse
import urllib.request
import uuid


def _encode_context_value(val: str) -> str:
  if not val:
    return ""
  encoded = base64.urlsafe_b64encode(val.encode("utf-8")).decode("utf-8")
  return "e" + encoded.rstrip("=")


def get_token() -> str:
  return subprocess.check_output(
      ["gcloud", "auth", "application-default", "print-access-token"], text=True
  ).strip()


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
  out_tput = float(
      data.get("output_token_throughput", {}).get(
          "avg", total_osl / dur_sec if dur_sec else 0.0
      )
  )
  total_tput = float(
      data.get("total_token_throughput", {}).get(
          "avg", (total_isl + total_osl) / dur_sec if dur_sec else 0.0
      )
  )
  req_rate = float(
      data.get("request_throughput", {}).get(
          "avg", req_count / dur_sec if dur_sec else 0.0
      )
  )

  ttft = ms_to_sec_stats(data.get("time_to_first_token"), units="s")
  itl = ms_to_sec_stats(data.get("inter_token_latency"), units="s/token")
  tpot = ms_to_sec_stats(
      data.get("time_per_output_token") or data.get("inter_token_latency"),
      units="s/token",
  )
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
                  "count": 4,
                  "parallelism": {
                      "tp": 4,
                      "dp": 1,
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
                      "normalized_time_per_output_token": tpot,
                      "time_per_output_token": tpot,
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


RUN_BUNDLES = [
    {
        "bq_run_id": "llmd_stage0_baseline_1xTP4_4gpus_c32_c64_c128",
        "run_uid": "ikwak-llmd-stage0-baseline-1xTP4",
        "run_label": "Stage 0: Baseline (1x TP=4, c=32/64/128)",
        "engine_tool": "vllm",
        "scheduler_tool": "llm-d",
        "model_name": "a4/deepseek_v4_1_flash_4gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 4,
        "stages": [
            (0, 32, "/tmp/run0_results/stages/stage0_baseline_c32/profile_export_aiperf.json", "stage_0_c32/llmd_benchmark_report.json"),
            (1, 64, "/tmp/run0_results/llmd/run0_llmd_c64/profile_export_aiperf.json", "stage_1_c64/llmd_benchmark_report.json"),
            (2, 128, "/tmp/run0_results/run0_3600s_llmd_c128/profile_export_aiperf.json", "stage_2_c128/llmd_benchmark_report.json"),
        ],
    },
    {
        "bq_run_id": "llmd_stage1_naive_rr_2xTP4_8gpus_c32_c64_c128_per_rep",
        "run_uid": "ikwak-llmd-stage1-naive-rr-2xTP4",
        "run_label": "Stage 1: Naive Round-Robin (2x TP=4, c=32/64/128 per rep)",
        "engine_tool": "vllm",
        "scheduler_tool": "envoy-round-robin",
        "model_name": "a4/deepseek_v4_1_flash_4gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "stages": [
            (0, 64, "/tmp/run0_results/stages/stage1_naive_rr_c64/profile_export_aiperf.json", "stage_0_c64/llmd_benchmark_report.json"),
            (1, 128, "/tmp/run0_results/stages/stage1_naive_rr_c128/profile_export_aiperf.json", "stage_1_c128/llmd_benchmark_report.json"),
            (2, 256, "/tmp/run0_results/stages/stage1_naive_rr_c256/profile_export_aiperf.json", "stage_2_c256/llmd_benchmark_report.json"),
        ],
    },
    {
        "bq_run_id": "llmd_stage2_kv_aware_epp_2xTP4_8gpus_c32_c64_c128_per_rep",
        "run_uid": "ikwak-llmd-stage2-kv-aware-epp-2xTP4",
        "run_label": "Stage 2: llm-d KV-Cache-Aware EPP (2x TP=4, c=32/64/128 per rep)",
        "engine_tool": "vllm",
        "scheduler_tool": "llm-d-kv-epp",
        "model_name": "a4/deepseek_v4_1_flash_4gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "stages": [
            (0, 64, "/tmp/run0_results/stages/stage2_kv_routing_c64/profile_export_aiperf.json", "stage_0_c64/llmd_benchmark_report.json"),
            (1, 128, "/tmp/run0_results/stages/stage2_kv_routing_c128/profile_export_aiperf.json", "stage_1_c128/llmd_benchmark_report.json"),
            (2, 256, "/tmp/run0_results/stages/stage2_kv_routing_c256/profile_export_aiperf.json", "stage_2_c256/llmd_benchmark_report.json"),
        ],
    },
    {
        "bq_run_id": "llmd_stage3_lowlat_pd_pacing_2xTP4_8gpus_c32_c64_per_rep",
        "run_uid": "ikwak-llmd-stage3-lowlat-pd-pacing-2xTP4",
        "run_label": "Stage 3 (Low-Latency): First-Token P/D Pacing (2x TP=4, c=32/64 per rep)",
        "engine_tool": "vllm",
        "scheduler_tool": "llm-d-pd-pacing",
        "model_name": "a4/deepseek_v4_1_flash_4gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "stages": [
            (0, 64, "/tmp/run0_results/stages/stage3_pd_lowlat_c64/profile_export_aiperf.json", "stage_0_c64/llmd_benchmark_report.json"),
            (1, 128, "/tmp/run0_results/stages/stage3_pd_lowlat_c128/profile_export_aiperf.json", "stage_1_c128/llmd_benchmark_report.json"),
        ],
    },
    {
        "bq_run_id": "llmd_stage3_hightput_pd_disagg_2xTP4_8gpus_c8_to_c128_per_rep",
        "run_uid": "ikwak-llmd-stage3-hightput-pd-disagg-2xTP4",
        "run_label": "Stage 3 (High-Throughput): Disagg P/D + Real-Time KV (2x TP=4, c=8..128 per rep)",
        "engine_tool": "vllm",
        "scheduler_tool": "llm-d-pd-disagg",
        "model_name": "a4/deepseek_v4_1_flash_4gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/stage3_pd_disagg_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json"),
            (1, 32, "/tmp/run0_results/stages/stage3_pd_disagg_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json"),
            (2, 64, "/tmp/run0_results/stages/stage3_pd_disagg_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json"),
            (3, 128, "/tmp/run0_results/stages/stage3_pd_disagg_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json"),
            (4, 256, "/tmp/run0_results/stages/stage3_pd_disagg_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json"),
        ],
    },
    {
        "bq_run_id": "llmd_stage4_lustre_kv_tiering_2xTP4_8gpus_c8_to_c128_per_rep",
        "run_uid": "ikwak-llmd-stage4-lustre-kv-tiering-2xTP4",
        "run_label": "Stage 4: Managed Lustre KV Cache Tiering + Disagg P/D + KV-Aware (2x TP=4, c=8..128 per rep)",
        "engine_tool": "vllm",
        "scheduler_tool": "llm-d-lustre-kv-tiering",
        "model_name": "a4/deepseek_v4_1_flash_4gpus_fp4_agentx_llmd_vllm_sweep",
        "hardware_name": "B200",
        "accel_count": 8,
        "stages": [
            (0, 16, "/tmp/run0_results/stages/stage4_lustre_kv_tiering_c16/profile_export_aiperf.json", "stage_0_c16/llmd_benchmark_report.json"),
            (1, 32, "/tmp/run0_results/stages/stage4_lustre_kv_tiering_c32/profile_export_aiperf.json", "stage_1_c32/llmd_benchmark_report.json"),
            (2, 64, "/tmp/run0_results/stages/stage4_lustre_kv_tiering_c64/profile_export_aiperf.json", "stage_2_c64/llmd_benchmark_report.json"),
            (3, 128, "/tmp/run0_results/stages/stage4_lustre_kv_tiering_c128/profile_export_aiperf.json", "stage_3_c128/llmd_benchmark_report.json"),
            (4, 256, "/tmp/run0_results/stages/stage4_lustre_kv_tiering_c256/profile_export_aiperf.json", "stage_4_c256/llmd_benchmark_report.json"),
        ],
    },
]


def main() -> None:
  token = get_token()
  bucket_name = "ubench-logs"

  for b in RUN_BUNDLES:
    run_uid = b["run_uid"]
    run_label = b.get("run_label", run_uid)
    accel_count = b.get("accel_count", 8)
    entries = []
    primary_raw_report = None

    for stage_idx, conc, aiperf_path, filename in b["stages"]:
      raw_report = build_brv02_raw_report(
          aiperf_json_path=aiperf_path,
          run_uid=run_uid,
          run_eid=run_label,
          description=run_label,
          engine_tool=b["engine_tool"],
          scheduler_tool=b["scheduler_tool"],
          model_name=b["model_name"],
          concurrency=conc,
          stage_idx=stage_idx,
      )
      raw_report["scenario"]["stack"][1]["standardized"]["accelerator"]["count"] = accel_count
      primary_raw_report = raw_report
      stage_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{run_uid}-stage-{stage_idx}"))
      entries.append({
          "run_id": stage_uuid,
          "run_description": run_label,
          "filename": filename,
          "prism_stage_index": stage_idx,
          "raw_report": raw_report,
      })

    raw_blob_path = f"{run_uid}/logs/pod_logs/llmd_benchmark_report.json"
    gcs_upload_json(bucket_name, raw_blob_path, primary_raw_report, token=token)
    print(f"Uploaded raw report: gs://{bucket_name}/{raw_blob_path}")

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
        token=token,
    )
    print(
        f"Uploaded multi-stage Prism store report ({len(entries)} stages):"
        f" gs://{bucket_name}/{prism_blob_path}"
    )


if __name__ == "__main__":
  main()
