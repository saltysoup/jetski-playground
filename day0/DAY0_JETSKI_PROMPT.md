# Day-0 Model Optimization — Reusable Jetski Agent Prompt Template

Paste the prompt below into **Jetski** whenever a new Day-0 model lands on Hugging Face, **vLLM (`llm-d`)**, or **SGLang (`NVIDIA Dynamo`)** and you want to automatically generate an apples-to-apples **Stage 1 $\to$ Stage 4** optimization progression, interactive Pareto charts, Prism results, and executive slides.

---

## Copy-Paste Prompt for Jetski

```markdown
We have a new Day-0 model `<MODEL_ID>` (e.g., `deepseek-ai/DeepSeek-V4.1-Flash` or `Qwen/Qwen3.8-Flash-Next`) that we want to benchmark and optimize on `<GPU_SKU>` GPUs in GKE cluster `<CLUSTER_NAME>` (`<ZONE>`, project `<PROJECT_ID>`).

Follow the 4-Stage Day-0 Optimization Playbook in `day0/README.md` (`https://github.com/saltysoup/jetski-playground/tree/main/day0`) for `<STACK>` (`llm-d + vLLM`, `NVIDIA Dynamo + SGLang`, or both):

### 1. Hardware & Zonal Co-Location Pre-Flight (Mandatory Guardrails)
1. Verify the exact zone of our GPU nodes (`kubectl get nodes -L topology.kubernetes.io/zone,cloud.google.com/gke-Accelerator`).
2. Verify or provision a Google Cloud Managed Lustre instance in the **exact same zone** as the GPU nodes with `perUnitStorageThroughput: 1000` (`1,000 MBps/TiB` tier, e.g., 9 TiB = 9 GB/s) and mount it via CSI (`lustre.csi.storage.gke.io`) at `/mnt/lustre_1000mbps`. **Never** mount a cross-region or cross-zone Lustre instance.
3. Stage model weights on same-region GCS Rapid Cache / Managed Lustre and deploy **2 identical multi-GPU replicas** (`2x TP=<TP_SIZE>` or `2x TEP=<TEP_SIZE>`, 1 replica per node, `<TOTAL_GPUS>` GPUs total). Keep engine flags, quantization (`FP4`/`FP8`), speculative decoding (`DSpark`/`MTP`), and `--max-num-seqs` / `--max-running-requests` **100% identical across all 4 stages** so there is zero apples-to-bananas config drift.

### 2. Run the 4-Stage Multi-Replica Progression (`semianalysis_cc_traces_weka_062126`)
Run `aiperf profile` (`--scenario inferencex-agentx-mvp`, `--streaming`, `--use-server-token-count`) across the concurrency sweep (`c=8, 16, 32, 64, 128` per replica / `c=16, 32, 64, 128, 256` pool):
- **Stage 1 (Baseline — Naive L7 Round-Robin)**: Stateless L7 round-robin across the 2 replicas (`--cache-bust first_turn_prefix`, cold KV cache before each run). Measure the multi-replica scale-out tax (KV hit rate collapse & TTFT inflation).
- **Stage 2 (KV-Cache-Aware Prefix Routing)**: Enable `llm-d EPP` prefix affinity or `NVIDIA Dynamo` KV-aware session routing (keyed on `[rid:...]` / prefix hash) so all turns of a trajectory land on the replica holding their HBM KV prefix.
- **Stage 3 (KV Routing + Disaggregated Prefill/Decode)**: Enable real-time prefill-byte + decode-slot scoring (`in_flight_prefill_bytes + assigned_sessions + in_flight_decode_reqs`) and decoupled P/D admission pacing so prefill bursts do not stall active decode batches.
- **Stage 4 (Same-Zone 1,000 MBps/TiB Managed Lustre KV Cache Tier + Disagg P/D + KV-Aware)**: Hydrate and persist KV blocks asynchronously (`ThreadPoolExecutor` / non-blocking DMA offload, never synchronous writes on the SSE streaming loop) to `/mnt/lustre_1000mbps` so returning multi-turn trajectories across waves hit warm Lustre/HBM KV blocks (`97–99%` KV hit rate).

### 3. Deliverables
1. Verify all numbers and confirm monotonic Pareto frontier improvements from Stage 1 -> Stage 2 -> Stage 3 -> Stage 4.
2. Provide a 1-sentence explanation of why each stage improves performance, plus the exact throughput (`tok/s/chip`) and latency (`P90 TTFT` & `P90 E2E Interactivity tok/s/user`) uplift of **Stage 4 vs. Stage 1 (Baseline)** and **Stage 4 vs. Stage 3**.
3. Generate an InferenceX-style Pareto slide (`P90 E2E Normalized Interactivity tok/s/user` on X-axis, `Output Token Throughput tok/s/chip` on Y-axis) with the 4-stage comparison table (`Stages 1, 2, 3, 4`) and 3-line summary below the chart in Slide Ops and export to Google Slides.
4. Sync all JSON results to Prism (`gs://ubench-logs/prism-results-store/`) and commit the results + artifacts to `day0/` in `https://github.com/saltysoup/jetski-playground/tree/main`.
```
