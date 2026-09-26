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
    ├── README.md             # Results, architecture, step-by-step reproduction guide, runbook & disclosures
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
  * 1,000 gVisor agent sandboxes wake from zero in about 3 s (median 2,988 ms over 10 measured wakes). About 90% of the fleet sits idle at zero CPU during traffic, and suspend-all takes about 0.6 s.
  * The agents call 2 × `google/gemma-4-12B-it` (`vllm-torchtpu`) pods behind `llm-d`: about 90% prefix-cache hit rate, live 80/20 header steering, and `InferenceObjective` priority bands.
  * Includes the interactive stage dashboard and a verified end-to-end reproduction guide.
* **[`robotics/unitree-r1/`](./robotics/unitree-r1/README.md):** Complete offline deployment guide for Unitree R1 (Jetson Orin) running native CUDA `NeMo-Speech.cpp` (Nemotron ASR + Magpie TTS) with `google/gemma-4-E2B-it` VLM, hardware I/O audio testing, and multimodal vision streaming.
* **[`reliability/`](./reliability/README.md):** Production multi-node reinforcement learning recipes, OOM bottleneck solutions (`/dev/shm`), Hopper/Blackwell kernel compatibility, and empirical AIME 2024 Olympiad benchmark reports for **Gemma 3 27B IT** (`google/gemma-3-27b-it`).
* **[`inference/`](./inference/README.md):** Inference workloads, high-throughput serving recipes, and latency benchmarks.
