# Unitree R1 Humanoid Robot: Fully Offline Edge Deployment Guide

End-to-end deployment guide for running **real-time multimodal AI (Nemotron Speech ASR + Magpie TTS v2602 + Gemma-4 2B VLM)** fully on-device on the **Unitree R1 / G1** (NVIDIA Jetson Orin NX).

---

## Hardware & System Specification

| Specification | Value | Notes |
| :--- | :--- | :--- |
| **Target Host** | Unitree R1 / G1 Humanoid Robot | Integrated NVIDIA Jetson Orin NX |
| **Host OS** | Ubuntu 20.04 LTS (JetPack 5.x) | Python **3.8.10 (`cp38`)** |
| **Robot Static IP** | `192.168.123.164` | `eth10` interface |
| **Robot SSH** | `unitree@192.168.123.164` (password: `123`) | Internal network connection |
| **Microphone** | Unitree UDP Multicast Stream | `239.168.123.161:5555` (16kHz 16-bit Mono PCM) |
| **Speakers** | Unitree DDS AudioClient | `/home/unitree/unitree_sdk2/build/bin/unitree_play_wav` |
| **Head Camera** | Standard USB V4L2 device | `/dev/video0` (OpenCV) |
| **Power Profile** | **15W Balanced Mode (`nvpmodel -m 2`)** | Optimized for onboard battery operation |
| **Internet Access** | **None on robot** | Fully offline deployment via staging |

---

## 1. Laptop Staging Preparation (Run on Laptop with Internet)

Because the Unitree robot has **no internet connection**, stage all weights, wheels, and deb packages in `~/robot_assets` on your developer laptop.

### 1.1 Create Staging Directories
```bash
mkdir -p ~/robot_assets/wheels \
         ~/robot_assets/debs \
         ~/robot_assets/models/asr \
         ~/robot_assets/models/magpie-tts/extracted \
         ~/robot_assets/models/nano-codec \
         ~/robot_assets/models/gemma-4-E2B-it \
         ~/robot_assets/models/gemma-4-E2B-it-assistant
```

### 1.2 Download All Models

#### A. Nemotron Streaming ASR (Speech-to-Text)
```bash
huggingface-cli download \
  nvidia/nemotron-speech-streaming-en-0.6b-gguf \
  nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --local-dir ~/robot_assets/models/asr
```

#### B. Magpie Multilingual TTS v2602 (Text-to-Speech)
```bash
huggingface-cli download \
  nvidia/magpie-tts-357m-multilingual-gguf \
  magpie_tts_multilingual_357m.v2602.f16.gguf \
  --local-dir ~/robot_assets/models/magpie-tts
```

#### C. Nemo NanoCodec 22kHz Decoder
```bash
huggingface-cli download \
  nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps-gguf \
  nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --local-dir ~/robot_assets/models/nano-codec
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
  "ninja==1.13.0"
```

---

### 1.4 Download Ubuntu 20.04 (Focal) gRPC DEB Packages

These allow compiling the native C++ `riva_server` without an active internet connection on the robot:

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

SSH into the robot (`ssh unitree@192.168.123.164`):

### 2.1 Set Power Mode to 15W (Battery Saver)
By default, the Jetson Orin runs in `MAXN` (uncapped power), which rapidly drains the robot's onboard battery. Switch to **15W Balanced Mode**:

```bash
# Set power profile to 15W
sudo nvpmodel -m 2

# Verify current mode
sudo nvpmodel -q
```
*(Note: A system reboot applies all hardware frequency limits cleanly)*

---

### 2.2 Install gRPC & System Packages
```bash
# 1. Install gRPC & C++ system libraries
sudo dpkg -i ~/robot_assets/debs/*.deb

# 2. Install Python 3.8 packages & modern CMake/Ninja
pip3 install --no-index --find-links=/home/unitree/robot_assets/wheels \
  cmake ninja protobuf sounddevice soundfile requests nvidia-riva-client numpy

export PATH=/home/unitree/.local/bin:/usr/local/cuda/bin:$PATH
```

---

## 3. Build Audio Player & Speech Engine on Robot

### 3.1 Build Unitree DDS Audio Player
```bash
cd /home/unitree/unitree_sdk2/build
cmake ..
make unitree_play_wav
```

### 3.2 Build `NeMo-Speech.cpp` with CUDA
```bash
cd /home/unitree/NeMo-Speech.cpp

# Apply CUDA GGML patches
bash scripts/apply-ggml-patches.sh

# Configure CMake with CUDA, gRPC, and static SentencePiece
cmake -B build-cuda -G Ninja \
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

## 4. Verification & Testing

### Test 1: Audio Loopback Verification (Mic & Speakers)
Tests the UDP multicast microphone capture (`239.168.123.161:5555`) and DDS audio playback:
```bash
python3 ~/test_audio_loopback.py
```
* **Expected behavior**: The robot prompts you to speak, records 5 seconds of audio from the onboard microphone, and plays it back through the onboard speakers.

---

### Test 2: Full Multimodal Vision & Voice Assistant

#### Terminal 1: Launch Riva Speech Server
```bash
/home/unitree/NeMo-Speech.cpp/build-cuda/bin/riva_server \
  --asr.model.path /home/unitree/robot_assets/models/asr/nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --tts.magpie-model /home/unitree/robot_assets/models/magpie-tts/magpie_tts_multilingual_357m.v2602.f16.gguf \
  --tts.codec-model /home/unitree/robot_assets/models/nano-codec/nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --tts.tokenizer-model-dir /home/unitree/robot_assets/models/magpie-tts/extracted \
  --bind 127.0.0.1:50051
```

#### Terminal 2: Launch vLLM Gemma-4 Server
```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model /home/unitree/robot_assets/models/gemma-4-E2B-it \
  --speculative-model /home/unitree/robot_assets/models/gemma-4-E2B-it-assistant \
  --num-speculative-tokens 3 \
  --port 8000 \
  --gpu-memory-utilization 0.60 \
  --max-model-len 2048
```

#### Terminal 3: Run Interactive Assistant
```bash
python3 ~/test_vision_voice_assistant.py
```
* **Expected behavior**:
  1. Robot asks: *"Ask me what I am seeing"* via Magpie TTS.
  2. You speak a question (e.g., *"What is in front of you?"*).
  3. Nemotron ASR transcribes your speech.
  4. Unitree head camera captures the live scene.
  5. Gemma-4 generates a concise 1-sentence answer.
  6. Magpie TTS speaks the response through the robot's onboard speakers.
