# Jetski Playground

A repository containing AI/ML infrastructure, robotics, reliability engineering, reinforcement learning, and inference workloads on Google Kubernetes Engine (GKE), NVIDIA Hypercomputer clusters, and NVIDIA Jetson robotics edge compute.

---

## Repository Structure

```text
jetski-playground/
├── README.md                 # Root overview (this file)
├── robotics/                 # Embodied AI & Edge Robotics Deployments
│   └── unitree-r1/           # Offline multimodal voice/vision pipeline on Unitree R1 (Jetson Orin)
├── reliability/              # Multi-node Reinforcement Learning (NeMo-RL / Kuberay) & Reliability Engineering
│   ├── README.md             # Comprehensive AIME 2024 empirical report & RL training instructions
│   └── gemma3-27b-it/        # Helm charts, DAPO/GRPO recipes, & workstation orchestrators for Gemma 3 27B IT
├── inference/                # LLM Inference Workloads & Serving Benchmarks
│   └── README.md             # Overview of inference workloads and recipes
└── substrate/                # Agent Substrate on GKE × llm-d on Cloud TPU v6e (1,000-Agent Keynote Demo)
    ├── README.md             # Results, architecture, runbook & disclosures
    ├── USER_GUIDE.md         # Build, redeploy, recover, update & tear down the stack
    ├── plan.md               # Status, decisions & open items
    ├── implementation.md     # Engineering notes: patches, driver, llm-d config, incidents
    ├── dashboard/            # Stage dashboard UI + zero-dependency rehearsal mock server
    ├── manifests/            # Agent Substrate, vllm-torchtpu, llm-d EPP & Envoy gateway manifests and scripts
    ├── patches/              # ate-api/atelet fast-wake patch & runsc wrapper
    └── substrate-bench/      # keynote_driver orchestrator & earlier Go benchmarks
```

---

## Sections

* **[`substrate/`](./substrate/README.md):** **Agent Substrate on GKE × `llm-d` on Cloud TPU v6e (Trillium)**:
  * 1,000 gVisor agent sandboxes wake from zero in about 2 s on 25 × c4-standard-4 (1,907–1,953 ms over 4 warm wakes; median 2,988 ms on the original C3 nodes). About 80% of the fleet sits idle at zero CPU during traffic.
  * A [Hermes Agent variant](./substrate/hermes/README.md) runs 1,000 Hermes agents on C4D whose conversation memory survives suspend and resume.
  * The agents call 2 × `google/gemma-4-12B-it` (`vllm-torchtpu`) pods in three stages with the same load: round robin without `llm-d`; `llm-d` KV-cache-aware routing (prefix-cache hit 45–72% → 80–98%, about 65 → 105 requests/s served, agent wait about 2.5 s → 1.3 s); and `llm-d` flow control, which serves paid user tiers first (`InferenceObjective` priorities).
  * Includes the interactive stage dashboard, which measures each stage live, side by side with the one before it. On 2026-10-05 `llm-d` gave 2.0× the output tokens/s and 2.4× lower agent E2E latency, while TTFT was 1.2× higher at this saturated load. Flow control cut the Pro tier's queue wait from 481 to 44 ms.
  * A verified [user guide](./substrate/USER_GUIDE.md) builds, redeploys, recovers, updates and tears down the stack.
* **[`robotics/unitree-r1/`](./robotics/unitree-r1/README.md):** Complete offline deployment guide for Unitree R1 (Jetson Orin) running native CUDA `NeMo-Speech.cpp` (Nemotron ASR + Magpie TTS) with `google/gemma-4-E2B-it` VLM, hardware I/O audio testing, and multimodal vision streaming.
* **[`reliability/`](./reliability/README.md):** Production multi-node reinforcement learning recipes, OOM bottleneck solutions (`/dev/shm`), Hopper/Blackwell kernel compatibility, and empirical AIME 2024 Olympiad benchmark reports for **Gemma 3 27B IT** (`google/gemma-3-27b-it`).
* **[`inference/`](./inference/README.md):** Inference workloads, high-throughput serving recipes, and latency benchmarks.
