# Unitree R1 Humanoid Robot: Fully Offline Edge Deployment Guide

End-to-end deployment guide for running **real-time multimodal AI (Nemotron Speech ASR + Magpie TTS v2602 + Gemma-4 2B Multimodal VLM via Native CUDA Engine)** fully on-device on the **Unitree R1 / G1** (NVIDIA Jetson Orin NX).

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
| **Section 1** | Laptop asset downloads (models, wheels, debs) & transfer to robot | ~10–15 min |
| **Section 2** | Robot system libraries (gRPC debs, Python wheels, MiniLM ONNX runtime) installation | ~2 min |
| **Section 3.1**| Unitree DDS Audio Player compilation (`unitree_play_wav`) | ~30 sec |
| **Section 3.2**| `NeMo-Speech.cpp` CUDA compilation (`sm_87` native) in MAXN mode | ~20–25 min |
| **Section 3.3**| `llama.cpp` Native CUDA Engine compilation (`llama-server` `sm_87`) | ~5–7 min |
| **Section 3.4**| Transition power mode to 15W battery saver | ~10 sec |
| **Section 4** | Verification Tests (Audio Loopback + Full Vision-Voice Assistant) | ~2–3 min |

---

## Quick Resume Guide

Follow these quick steps when you power on the robot:

```bash
# 1. SSH into the robot
ssh unitree@192.168.123.164

# 2. Set Jetson to Maximum Performance (MAXN) during builds
sudo nvpmodel -m 0
sudo jetson_clocks

# 3. Resume the riva_server / llama-server compilation (picks up incrementally from cache)
export PATH=/home/unitree/.local/bin:/usr/local/cuda/bin:$PATH
cd /home/unitree/NeMo-Speech.cpp
ninja -C build-cuda riva_server
ninja -C llama.cpp/build-cuda llama-server

# 4. Once builds finish linking, switch power mode down to 15W battery saver
sudo nvpmodel -m 2

# 5. Proceed to Section 4 to run the Multimodal Assistant!
```

---

## 1. Laptop Staging Preparation (Run on Laptop with Internet)
*Estimated Time: ~10–15 minutes*

Because the Unitree robot has **no internet connection**, stage all weights, wheels, and deb packages in `~/robot_assets` on your developer laptop.

### 1.1 Create Staging Directories
```bash
mkdir -p ~/robot_assets/wheels \
         ~/robot_assets/debs \
         ~/robot_assets/models/magpie-tts/extracted \
         ~/robot_assets/models/onnx
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

#### E. Gemma-4 2B Multimodal VLM (Base Language Model + Vision Projector)
```bash
# Language Model GGUF (Q8_0)
# ~/robot_assets/models/gemma-4-E2B-it-q8_0.gguf

# Vision Projector GGUF (F16)
# ~/robot_assets/models/mmproj-gemma-4-E2B-f16.gguf
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

### 1.5 Transfer Staged Assets & Test Scripts to Robot

Connect your laptop to the robot network (`192.168.123.x`) and SCP everything over:

```bash
# Transfer assets (models, wheels, debs)
scp -r ~/robot_assets unitree@192.168.123.164:~/

# Transfer standalone test scripts
scp robotics/unitree-r1/test_*.py unitree@192.168.123.164:~/
```

---

## 2. Jetson Orin Environment Setup (Offline on Robot)
*Estimated Time: ~2 minutes*

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

### 2.2 Install gRPC & Python Packages
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

### 3.3 Build `llama-server` with CUDA (Native Multimodal VLM Engine)
*Estimated Time: ~5–7 minutes in MAXN mode*
```bash
cd /home/unitree/NeMo-Speech.cpp/llama.cpp

cmake -B build-cuda -G Ninja \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=87 \
  -DCMAKE_BUILD_TYPE=Release

ninja -C build-cuda llama-server -j$(nproc)
```

---

### 3.4 Switch Power Mode to 15W Balanced Mode (Post-Build Battery Saver)
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

### Test 2: Full Multimodal Vision & Voice Assistant (100% Onboard Jetson)

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

#### Terminal 2: Launch Native CUDA Gemma-4 Multimodal VLM Server on Jetson (OpenAI Compatible)
```bash
/home/unitree/NeMo-Speech.cpp/llama.cpp/build-cuda/bin/llama-server \
  -m /home/unitree/robot_assets/models/gemma-4-E2B-it-q8_0.gguf \
  --mmproj /home/unitree/robot_assets/models/mmproj-gemma-4-E2B-f16.gguf \
  --host 127.0.0.1 \
  --port 8000 \
  -c 2048 \
  -ngl 99
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
  5. Gemma-4 generates a concise 1-sentence multimodal answer locally on the Jetson Orin NX.
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
  * `llama-server` (Gemma-4 2B Q8_0 + Vision Projector F16): ~3.2 GB VRAM.
  * Unitree RL Motion / Locomotion Policy: ~1.0–2.0 GB VRAM.
  * System Overhead: ~1.2 GB RAM.
  * Remaining headroom: ~8–9 GB.
