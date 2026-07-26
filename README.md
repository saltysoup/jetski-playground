# Jetski Playground

A private repository containing AI/ML infrastructure, reliability engineering, reinforcement learning, and inference workloads on Google Kubernetes Engine (GKE) and NVIDIA Hypercomputer clusters.

---

## Repository Structure

```text
jetski-playground/
├── README.md                 # Root overview (this file)
├── reliability/              # Multi-node Reinforcement Learning (NeMo-RL / Kuberay) & Reliability Engineering
│   ├── README.md             # Comprehensive AIME 2024 empirical report & RL training instructions
│   └── gemma3-27b-it/        # Helm charts, DAPO/GRPO recipes, & workstation orchestrators for Gemma 3 27B IT
└── inference/                # LLM Inference Workloads & Serving Benchmarks
    └── README.md             # Overview of inference workloads and recipes
```

---

## Sections

* **[`reliability/`](./reliability/README.md):** Production multi-node reinforcement learning recipes, OOM bottleneck solutions (`/dev/shm`), Hopper/Blackwell kernel compatibility, and empirical AIME 2024 Olympiad benchmark reports for **Gemma 3 27B IT** (`google/gemma-3-27b-it`).
* **[`inference/`](./inference/README.md):** Inference workloads, high-throughput serving recipes, and latency benchmarks.
