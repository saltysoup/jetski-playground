# Unitree R1 Humanoid Robot: Fully Offline Edge Deployment Guide

End-to-end deployment guide for running **real-time multimodal AI (Nemotron Speech ASR + Magpie TTS v2602 + Gemma-4 2B VLM)** fully on-device on the **Unitree R1 / G1** (Jetson Orin NX).

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
| **Internet Access** | **None on robot** | Fully offline deployment via staging |

---

## 1. Laptop Staging Preparation (With Internet)

All dependencies and weights are staged on the developer laptop and copied via SCP.

### 1.1 Download Compatible Python 3.8 (`cp38`) Wheels & Debs

```bash
mkdir -p ~/robot_assets/wheels ~/robot_assets/debs ~/robot_assets/models/asr ~/robot_assets/models/magpie-tts ~/robot_assets/models/nano-codec ~/robot_assets/models/gemma-4-E2B-it

# Download Python 3.8 cp38 aarch64 binary wheels
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

# Download Ubuntu 20.04 Focal arm64 gRPC debs
cd ~/robot_assets/debs
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/protobuf-compiler-grpc_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/libgrpc++-dev_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/libgrpc++1_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/libgrpc-dev_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/universe/g/grpc/libgrpc6_1.16.1-1ubuntu5_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/main/c/c-ares/libc-ares2_1.15.0-1build1_arm64.deb
curl -fLO http://ports.ubuntu.com/ubuntu-ports/pool/main/c/c-ares/libc-ares-dev_1.15.0-1build1_arm64.deb
```

### 1.2 Transfer Assets to the Robot

```bash
scp -r ~/robot_assets unitree@192.168.123.164:~/
scp robotics/unitree-r1/test_*.py unitree@192.168.123.164:~/
```

---

## 2. Jetson Orin Environment Setup (Offline on Robot)

SSH into the robot (`ssh unitree@192.168.123.164`):

```bash
# 1. Install gRPC & C++ libraries
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

# Apply CUDA GGML patches & build SentencePiece
bash scripts/apply-ggml-patches.sh

cmake -B build-cuda -G Ninja \
  -DGGML_CUDA=ON \
  -DNEMO_SPEECH_BUILD_GRPC=ON \
  -DNEMO_SPEECH_BUILD_ASR=ON \
  -DNEMO_SPEECH_BUILD_TTS=ON \
  -DSENTENCEPIECE_STATIC_LIB=/home/unitree/NeMo-Speech.cpp/.deps/sentencepiece/lib/libsentencepiece.a \
  -DSENTENCEPIECE_INCLUDE_DIR=/home/unitree/NeMo-Speech.cpp/.deps/sentencepiece/include \
  -DCMAKE_BUILD_TYPE=Release

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
  --asr.model.path /home/unitree/robot_assets/models/nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --tts.magpie-model /home/unitree/robot_assets/models/magpie_tts_multilingual_357m.v2602.f16.gguf \
  --tts.codec-model /home/unitree/robot_assets/models/nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --tts.tokenizer-model-dir /home/unitree/robot_assets/models/magpie-tts/extracted \
  --bind 127.0.0.1:50051
```

#### Terminal 2: Launch vLLM Gemma-4 Server
```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model /home/unitree/robot_assets/models/gemma-4-E2B-it \
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
