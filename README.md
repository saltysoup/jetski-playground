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
└── inference/                # LLM Inference Workloads & Serving Benchmarks
    └── README.md             # Overview of inference workloads and recipes
```

---

## Sections

* **[`robotics/unitree-r1/`](./robotics/unitree-r1/README.md):** Complete offline deployment guide for Unitree R1 (Jetson Orin) running native CUDA `NeMo-Speech.cpp` (Nemotron ASR + Magpie TTS) with `google/gemma-4-E2B-it` VLM, hardware I/O audio testing, and multimodal vision streaming.
* **[`reliability/`](./reliability/README.md):** Production multi-node reinforcement learning recipes, OOM bottleneck solutions (`/dev/shm`), Hopper/Blackwell kernel compatibility, and empirical AIME 2024 Olympiad benchmark reports for **Gemma 3 27B IT** (`google/gemma-3-27b-it`).
* **[`inference/`](./inference/README.md):** Inference workloads, high-throughput serving recipes, and latency benchmarks.
