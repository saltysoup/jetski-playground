# Unitree R1 Humanoid Robot: Fully Offline Edge Deployment Guide

End-to-end deployment guide for running **real-time multimodal AI (Nemotron Speech ASR + Magpie TTS v2602 + Gemma-4 2B VLM via vLLM)** fully on-device on the **Unitree R1 / G1** (NVIDIA Jetson Orin NX).

---

## Hardware & System Specification

| Specification | Value | Notes |
| :--- | :--- | :--- |
| **Target Host** | Unitree R1 / G1 Humanoid Robot | Integrated NVIDIA Jetson Orin NX (16GB RAM/VRAM) |
| **Host OS** | Ubuntu 20.04 LTS (JetPack 5.1.1 / L4T R35.3.1) | Python **3.8.10 (`cp38`)** |
| **Robot Static IP** | `192.168.123.164` | `eth10` interface |
| **Robot SSH** | `unitree@192.168.123.164` (password: `123`) | Internal robot subnet |
| **Microphone** | Unitree UDP Multicast Stream | `239.168.123.161:5555` (16kHz 16-bit Mono PCM) |
| **Speakers** | Unitree DDS AudioClient | `/home/unitree/unitree_sdk2/build/bin/unitree_play_wav` |
| **Head Camera** | Standard USB V4L2 device | `/dev/video0` (OpenCV) |
| **Build Power Profile** | **MAXN Mode (`nvpmodel -m 0`) + `jetson_clocks`** | Maximum clock frequencies (~2.0 GHz) to accelerate builds |
| **Runtime Power Profile**| **15W Balanced Mode (`nvpmodel -m 2`)** | Power-saving mode for robot battery runtime |
| **Internet Access** | **None on robot** | Fully offline deployment via laptop staging |

---

## Estimated Completion Times

| Stage | Action | Est. Duration |
| :--- | :--- | :---: |
| **Section 1** | Laptop asset downloads (models, wheels, debs, vLLM container) & transfer to robot | ~10–15 min |
| **Section 2** | Robot system libraries (gRPC debs, Python wheels, non-root Docker setup) installation | ~3 min |
| **Section 3.1**| Unitree DDS Audio Player compilation (`unitree_play_wav`) | ~30 sec |
| **Section 3.2**| `NeMo-Speech.cpp` CUDA compilation (`sm_87` native) in MAXN mode | ~20–25 min |
| **Section 3.3**| Transition power mode to 15W battery saver | ~10 sec |
| **Section 4** | Verification Tests (Audio Loopback + Full Vision-Voice Assistant) | ~2–3 min |

---

## Quick Resume Guide (To Continue Tomorrow)

Follow these quick steps when you power on the robot:

```bash
# 1. SSH into the robot
ssh unitree@192.168.123.164

# 2. Set Jetson to Maximum Performance (MAXN) for fast builds
sudo nvpmodel -m 0
sudo jetson_clocks

# 3. Resume the riva_server compilation (picks up incrementally from cache)
export PATH=/home/unitree/.local/bin:/usr/local/cuda/bin:$PATH
cd /home/unitree/NeMo-Speech.cpp
ninja -C build-cuda riva_server

# 4. Once riva_server finishes linking, switch power mode down to 15W battery saver
sudo nvpmodel -m 2

# 5. Proceed to Section 4 to run the Multimodal Assistant!
```

---

## 1. Laptop Staging Preparation (Run on Laptop with Internet)
*Estimated Time: ~10–15 minutes*

Because the Unitree robot has **no internet connection**, stage all weights, wheels, deb packages, and the vLLM ARM64 container archive in `~/robot_assets` on your developer laptop.

### 1.1 Create Staging Directories
```bash
mkdir -p ~/robot_assets/wheels \
         ~/robot_assets/debs \
         ~/robot_assets/models/magpie-tts/extracted \
         ~/robot_assets/models/gemma-4-E2B-it \
         ~/robot_assets/models/gemma-4-E2B-it-assistant
```

### 1.2 Download All Models

#### A. Nemotron Streaming ASR (Speech-to-Text)
```bash
huggingface-cli download \
  nvidia/nemotron-speech-streaming-en-0.6b-gguf \
  nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --local-dir ~/robot_assets/models
```

#### B. Magpie Multilingual TTS v2602 (Text-to-Speech)
```bash
huggingface-cli download \
  nvidia/magpie-tts-357m-multilingual-gguf \
  magpie_tts_multilingual_357m.v2602.f16.gguf \
  --local-dir ~/robot_assets/models
```

#### C. Nemo NanoCodec 22kHz Decoder
```bash
huggingface-cli download \
  nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps-gguf \
  nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --local-dir ~/robot_assets/models
```

#### D. Magpie TTS Tokenizer Extraction
```bash
# Download the .nemo archive to extract tokenizers
huggingface-cli download \
  nvidia/magpie_tts_multilingual_357m \
  magpie_tts_multilingual_357m.nemo \
  --local-dir ~/robot_assets/models/magpie-tts

# Extract tokenizer files into ~/robot_assets/models/magpie-tts/extracted/
tar -xf ~/robot_assets/models/magpie-tts/magpie_tts_multilingual_357m.nemo \
  -C ~/robot_assets/models/magpie-tts/extracted/
```

#### E. Gemma-4 2B Multimodal VLM & Draft Assistant
```bash
# Download base Gemma-4 2B Multimodal model
huggingface-cli download \
  google/gemma-4-E2B-it \
  --local-dir ~/robot_assets/models/gemma-4-E2B-it

# Download MTP draft assistant for accelerated speculative decoding
huggingface-cli download \
  google/gemma-4-E2B-it-assistant \
  --local-dir ~/robot_assets/models/gemma-4-E2B-it-assistant
```

---

### 1.3 Download Offline Wheels (Python 3.8 / `cp38` `aarch64`)

The Jetson Orin runs Ubuntu 20.04 with **Python 3.8.10 (`cp38`)**. Download precompiled binary wheels for ARM64:

```bash
pip download \
  --only-binary=:all: \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 38 \
  --abi cp38 \
  --dest ~/robot_assets/wheels \
  "sounddevice==0.5.6" \
  "soundfile==0.13.1" \
  "requests==2.32.4" \
  "cffi==1.17.1" \
  "pycparser==2.23" \
  "protobuf==3.20.3" \
  "grpcio==1.38.0" \
  "grpcio_tools==1.38.0" \
  "nvidia-riva-client==2.16.0" \
  "cmake==4.4.2" \
  "ninja==1.13.0" \
  "onnxruntime==1.16.3" \
  "coloredlogs==15.0.1" \
  "humanfriendly==10.0" \
  "flatbuffers==25.12.19" \
  "packaging==26.2" \
  "sympy==1.13.3" \
  "mpmath==1.3.0"
```

---

### 1.4 Download Ubuntu 20.04 (Focal) gRPC DEB Packages

```bash
cd ~/robot_assets/debs
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/protobuf-compiler-grpc_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/libgrpc++-dev_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/libgrpc++1_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/libgrpc-dev_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/libgrpc6_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/main/c/c-ares/libc-ares2_1.15.0-1build1_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/main/c/c-ares/libc-ares-dev_1.15.0-1build1_arm64.deb
```

---

### 1.5 Download vLLM ARM64 Container Archive (For Local Jetson Execution)

On macOS with Colima/Docker, pull the official ARM64 vLLM OpenAI image and export it to an offline tarball:

```bash
# 1. Switch context to colima (if using Colima on Mac)
docker context use colima

# 2. Pull official ARM64 vLLM image
docker pull --platform linux/arm64 vllm/vllm-openai:latest

# 3. Save container image to an offline tarball
docker save vllm/vllm-openai:latest -o ~/robot_assets/vllm_arm64.tar
```

---

### 1.6 Transfer Staged Assets & Test Scripts to Robot

Connect your laptop to the robot network (`192.168.123.x`) and SCP everything over:

```bash
# Transfer assets (models, wheels, debs, vLLM container)
scp -r ~/robot_assets unitree@192.168.123.164:~/

# Transfer standalone test scripts
scp robotics/unitree-r1/test_*.py unitree@192.168.123.164:~/
```

---

## 2. Jetson Orin Environment Setup (Offline on Robot)
*Estimated Time: ~3 minutes*

SSH into the robot (`ssh unitree@192.168.123.164`):

### 2.1 Set Power Mode to MAXN (Maximum Performance for Compilation)
Set the Jetson to **MAXN mode** and enable max clock frequencies to accelerate compilation:

```bash
# 1. Set power profile to MAXN (Mode 0)
sudo nvpmodel -m 0

# 2. Lock CPU and GPU clocks to maximum frequency for fastest build
sudo jetson_clocks

# 3. Verify current mode
sudo nvpmodel -q
```

---

### 2.2 Configure Non-Root Docker Access
Enable running Docker commands directly without requiring `sudo`:

```bash
# 1. Add unitree user to the docker group
sudo usermod -aG docker unitree

# 2. Activate group changes in the current shell session (or exit and reconnect SSH)
newgrp docker

# 3. Verify non-root access
docker ps
```

---

### 2.3 Install gRPC & Python Packages
```bash
# 1. Install gRPC & C++ system libraries
sudo dpkg -i ~/robot_assets/debs/*.deb

# 2. Link CUDA stub to real Tegra driver binary
sudo cp -L /usr/lib/aarch64-linux-gnu/tegra/libcuda.so.1.1 /usr/local/cuda/targets/aarch64-linux/lib/stubs/libcuda.so

# 3. Install Python 3.8 packages, MiniLM ONNX runtime, and modern CMake/Ninja
pip3 install --no-index --find-links=/home/unitree/robot_assets/wheels \
  cmake ninja protobuf sounddevice soundfile requests nvidia-riva-client numpy \
  onnxruntime coloredlogs flatbuffers sympy packaging humanfriendly mpmath

export PATH=/home/unitree/.local/bin:/usr/local/cuda/bin:$PATH
```

---

### 2.4 Load Offline vLLM Container Image
Load the vLLM image into the robot's local Docker daemon (runs without `sudo`):

```bash
# Load offline container image
docker load -i /home/unitree/robot_assets/vllm_arm64.tar

# Verify image is loaded
docker images | grep vllm
```

---

## 3. Build Audio Player & Speech Engine on Robot

### 3.1 Build Unitree DDS Audio Player
*Estimated Time: ~30 seconds*
```bash
cd /home/unitree/unitree_sdk2/build
cmake ..
make unitree_play_wav
```

---

### 3.2 Build `NeMo-Speech.cpp` with CUDA (Optimized for Jetson Orin `sm_87`)
*Estimated Time: ~20–25 minutes in MAXN mode*
```bash
cd /home/unitree/NeMo-Speech.cpp

# Apply CUDA GGML patches
bash scripts/apply-ggml-patches.sh

# Configure CMake targeting ONLY Orin sm_87
cmake -B build-cuda -G Ninja \
  -DCMAKE_CUDA_ARCHITECTURES=87 \
  -DGGML_CUDA=ON \
  -DNEMO_SPEECH_BUILD_GRPC=ON \
  -DNEMO_SPEECH_BUILD_ASR=ON \
  -DNEMO_SPEECH_BUILD_TTS=ON \
  -DSENTENCEPIECE_STATIC_LIB=/home/unitree/NeMo-Speech.cpp/.deps/sentencepiece/lib/libsentencepiece.a \
  -DSENTENCEPIECE_INCLUDE_DIR=/home/unitree/NeMo-Speech.cpp/.deps/sentencepiece/include \
  -DCMAKE_BUILD_TYPE=Release

# Build riva_server
ninja -C build-cuda riva_server -j$(nproc)
```

---

### 3.3 Switch Power Mode to 15W Balanced Mode (Post-Build Battery Saver)
*Estimated Time: ~10 seconds*

Once all compilation steps are finished, switch the Jetson to **15W Balanced Mode** to preserve robot battery during active inference and autonomous operation:

```bash
# Set power profile to 15W (Mode 2)
sudo nvpmodel -m 2

# Verify current mode
sudo nvpmodel -q
```

---

## 4. Verification & Testing
*Estimated Time: ~2–3 minutes*

### Test 1: Audio Loopback Verification (Mic & Speakers)
Tests the UDP multicast microphone capture (`239.168.123.161:5555`) and DDS audio playback:
```bash
python3 ~/test_audio_loopback.py
```
* **Expected behavior**: The robot prompts you to speak, records 5 seconds of audio from the onboard microphone, and plays it back through the onboard speakers.

---

### Test 2: Full Multimodal Vision & Voice Assistant (with Semantic Router)

#### Terminal 1: Launch Riva Speech Server (ASR + TTS)
```bash
export LD_LIBRARY_PATH=/home/unitree/NeMo-Speech.cpp/build-cuda/bin:$LD_LIBRARY_PATH
/home/unitree/NeMo-Speech.cpp/build-cuda/bin/riva_server \
  --asr.model.path /home/unitree/robot_assets/models/nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --tts.magpie-model /home/unitree/robot_assets/models/magpie_tts_multilingual_357m.v2602.f16.gguf \
  --tts.codec-model /home/unitree/robot_assets/models/nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --tts.tokenizer-model-dir /home/unitree/robot_assets/models/magpie-tts/extracted \
  --bind 127.0.0.1:50051
```

#### Terminal 2: Launch vLLM Gemma-4 Server Directly on Jetson (CUDA Accelerated, Non-Root)
```bash
docker run --runtime nvidia --network host -it --rm \
  -v /home/unitree/robot_assets/models:/models \
  vllm/vllm-openai:latest \
  vllm serve /models/gemma-4-E2B-it \
    --trust-remote-code \
    --max-model-len 1024 \
    --max-num-seqs 1 \
    --max-num-batched-tokens 512 \
    --gpu-memory-utilization 0.5 \
    --speculative-config '{"method":"mtp","model":"/models/gemma-4-E2B-it-assistant","num_speculative_tokens":1}' \
    --override-generation-config '{"temperature": 1.0, "top_p": 0.95, "top_k": 64}' \
    --no-async-scheduling \
    --port 8000
```

#### Terminal 3: Run Interactive Semantic Assistant
```bash
python3 ~/test_vision_voice_assistant.py
```
* **Expected behavior**:
  1. Robot asks: *"Ask me what I am seeing, or ask any general question"* via Magpie TTS.
  2. You speak a question (e.g. *"What is in front of you?"* or *"Tell me a joke"*).
  3. Nemotron ASR transcribes your speech.
  4. **MiniLM Semantic Router** evaluates intent:
     * If visual: captures `/dev/video0` head camera and attaches image.
     * If conversational: skips camera to minimize latency.
  5. Gemma-4 generates a concise 1-sentence answer locally on the Jetson Orin.
  6. Magpie TTS speaks the response through the robot's onboard speakers.

---

## TODO / Optimization & Tuning Backlog

### 1. Memory Profiling & Real-Time Monitoring
Instructions for tracking memory consumption across co-located models on the Jetson Orin NX (16GB Shared Unified Memory):
* **Real-time Jetson Memory Monitor (`tegrastats`)**:
  ```bash
  # Check active RAM, GPU VRAM usage, and power draw (in mW) every 1000ms:
  sudo tegrastats --interval 1000
  ```
* **Footprint Breakdown Baseline**:
  * `NeMo-Speech.cpp` (`riva_server`): ~1.44 GB VRAM (Nemotron ASR: 649 MB, Magpie TTS: 721 MB, NanoCodec: 67 MB).
  * Unitree RL Motion / Locomotion Policy: ~1.0–2.0 GB VRAM.
  * System & Display Overhead: ~1.2 GB RAM.
  * Remaining headroom for VLM (Gemma-4 KV Cache): ~10–11 GB.

---

### 2. GPU Memory Utilization Rate Tuning (`--gpu-memory-utilization`)
* **Goal**: Determine the maximum stable ceiling for vLLM memory allocation when co-located with `riva_server` and the Unitree RL locomotion policy.
* **Test Plan**:
  * Benchmark `--gpu-memory-utilization 0.50`, `0.55`, `0.60`, and `0.65`.
  * Verify that memory pressure does not trigger the OOM killer during simultaneous speech synthesis, camera frame processing, and locomotion commands.

---

### 3. Latency Optimization: Reduce Max Batched Tokens (`--max-num-batched-tokens`)
* **Goal**: Minimize Time-to-First-Token (TTFT) for low-latency conversational response.
* **Test Plan**:
  * Default test value: `512` tokens.
  * Test reduced batch sizes: `--max-num-batched-tokens 256` and `--max-num-batched-tokens 128`.
  * Evaluate TTFT and end-to-end voice loop latency (target: <1.2s from speech finish to audio playback).

---

### 4. Context Window Expansion (`--max-model-len`)
* **Goal**: Increase conversation and visual history capacity for multi-turn reasoning.
* **Test Plan**:
  * Baseline: `--max-model-len 1024`.
  * Benchmark expanded context lengths: `--max-model-len 2048` and `--max-model-len 4096`.
  * Measure the memory overhead of the expanded KV cache and assess any impact on frame token throughput.
