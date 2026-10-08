# Day-0 Model Optimization — Reusable Jetski Agent Prompt Template (5-Stage Scaffolding & GB300 Ready)

Paste the prompt below into **Jetski** whenever a new Day-0 model lands on Hugging Face, **vLLM (`llm-d`)**, or **SGLang (`NVIDIA Dynamo`)**—or when benchmarking on a new accelerator such as **NVIDIA GB300 (`a4x` / `GB300 NVL72`)** or **NVIDIA B200 (`a4-highgpu-8g`)**—to automatically generate an apples-to-apples **Stage 1 $\to$ Stage 5** optimization progression, interactive Pareto charts, Prism results, and executive Slide Ops slides.

---

## Copy-Paste Prompt for Jetski

```markdown
We have a new Day-0 model `<MODEL_ID>` (e.g., `moonshotai/Kimi-K3`, `zai-org/GLM-5.3`, or `deepseek-ai/DeepSeek-V4.1-Flash`) that we want to benchmark and optimize on `<GPU_SKU>` GPUs (e.g., `gb300` / `b200`) in GKE cluster `<CLUSTER_NAME>` (`<ZONE>`, project `<PROJECT_ID>`).

Follow the 5-Stage Day-0 Optimization Playbook in `day0/SCAFFOLDING_GUIDE.md` and `day0/README.md` (`https://github.com/saltysoup/jetski-playground/tree/main/day0`) for `<STACK>` (`llm-d + vLLM`, `NVIDIA Dynamo + SGLang`, or both):

### 1. Hardware, RDMA & Zonal Co-Location Pre-Flight (Mandatory Guardrails)
1. Verify the exact zone and accelerator of our GPU nodes (`kubectl get nodes -L topology.kubernetes.io/zone,cloud.google.com/gke-accelerator`).
2. Verify or provision a Google Cloud Managed Lustre instance in the **exact same zone** as the GPU nodes with `perUnitStorageThroughput: 1000` (`1,000 MBps/TiB` tier) and mount it via CSI (`lustre.csi.storage.gke.io`) at `/mnt/lustre_1000mbps`. **Never** mount a cross-region or cross-zone Lustre instance.
3. Run `day0/scripts/generate_day0_scaffolding.py --model-id <MODEL_ID> --hw <gb300|b200>` against the official upstream recipes (`recipes.vllm.ai` YAML and `docs.sglang.io/cookbook` JS).
4. Stage model weights and speculative decoding (`DSpark` / `EAGLE` / `MTP`) checkpoints onto `/mnt/lustre_1000mbps/models/`.
5. For multi-node models (or RDMA P/D disaggregation), attach all GKE RDMA NICs (`rdma-0..rdma-7`), mount `/usr/local/gib`, `/usr/local/nvidia`, and `/dev/infiniband`, source `/usr/local/gib/scripts/set_nccl_env.sh` with `NCCL_NET=IB`, and pass `--disable-custom-all-reduce` and `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`. Keep engine flags, quantization (`NVFP4`/`MXFP4`/`FP8`), speculative decoding, and batch caps **100% identical across all 5 stages**.

### 2. Run the 5-Stage Progression (`semianalysis_cc_traces_weka_062126`, 393 Agentic Trajectories)
Run `aiperf profile` (`--scenario inferencex-agentx-mvp`, `--streaming`, `--use-server-token-count`) across the 5-point concurrency sweep (`c = [16, 32, 64, 128, 256]` for 2-instance pools, or `c = [8, 16, 32, 64, 128]` for 2-node single-instance pools):
- **Stage 1 (Baseline — Naive L7 Round-Robin)**: Stateless L7 round-robin across workers with cold HBM KV cache between waves.
- **Stage 2 (KV-Cache-Aware Prefix Routing)**: Enable `llm-d EPP` or `NVIDIA Dynamo` prefix-hash session routing (`[rid:...]` / 64 KB prefix hash) so all turns of a trajectory land on the worker holding their HBM KV prefix.
- **Stage 3 (KV Routing + Disaggregated Prefill/Decode, HBM Only)**: Enable real-time prefill-byte + decode-slot scoring (`in_flight_prefill_bytes + in_flight_decode_reqs`) and decoupled P/D admission pacing (`NixlConnector` / `Mooncake RDMA`) so prefill bursts do not stall active decode batches.
- **Stage 4 (Host DRAM KV Cache Tiering + Disagg P/D + KV Routing)**: Enable **vLLM Native `OffloadingConnector`** (`CPUOffloadingSpec` / `SimpleCPUOffloadConnector`) or **SGLang `HiCache`** (`--enable-hierarchical-cache --hicache-ratio 2.0 --hicache-mem-layout page_first --hicache-io-backend kernel`) to tier evicted HBM KV blocks into local Host DRAM (PCIe Gen5 or **GB300 NVLink-C2C Grace LPDDR5X**).
- **Stage 5 (Mooncake + Same-Zone 1,000 MBps/TiB Managed Lustre KV Cache Tier)**: Enable **Mooncake Distributed Store** (`MooncakeStoreConnector` / `--hicache-storage-backend mooncake`) backed by `/mnt/lustre_1000mbps/mooncake_kv_cache` for cluster-wide persistent KV cache sharing across all waves.

### 3. Deliverables
1. Save all 5-stage JSON and CSV summaries to `day0/results/<model>_<stack>_stage1_to_5_summary.{json,csv}`.
2. Update `day0/scripts/generate_pareto_html.py` and generate interactive 5-Stage Pareto HTML dashboards.
3. Add a consolidated side-by-side dual-chart slide (`llm-d + vLLM` on left, `NVIDIA Dynamo + SGLang` on right) to Slide Ops (`pres-ff2d026887ff`) and verify with `$CLI lint` and `$CLI shot`.
4. Sync all results to Prism (`gs://ubench-logs/prism-results-store/`) and commit + push all manifests, scripts, results, and docs to `day0/` in `https://github.com/saltysoup/jetski-playground/tree/main`.
```
