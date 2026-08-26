# Complete Deployment Guide: Multimodal Voice & Vision AI on Unitree R1 (Jetson Orin)

This guide provides the complete, self-contained end-to-end instructions for deploying the multimodal AI stack onto the **NVIDIA Jetson Orin** inside a **Unitree R1** robot.

Because the Unitree R1 operates in an **offline environment (no internet access)**, all models, dependencies (Python 3.8 ARM64 wheels), and source repositories are pre-staged on your host computer (MacBook/PC) and transferred to the robot via a direct Ethernet connection.

---

## System Architecture

```
                                  UNITREE R1 ROBOT (JETSON ORIN)
 ┌─────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                                                                             │
 │   ┌───────────────────────┐         gRPC (Port 50051)         ┌─────────────────────────┐   │
 │   │  Onboard Microphone   │ ────────────────────────────────► │     NeMo-Speech.cpp     │   │
 │   │       (ALSA)          │                                   │       riva_server       │   │
 │   └───────────────────────┘                                   │  (Nemotron ASR + CUDA)  │   │
 │                                                               └────────────┬────────────┘   │
 │                                                                            │                │
 │   ┌───────────────────────┐                                                │ (Transcript)   │
 │   │  Onboard Head Camera  │ ────────────────────────────────┐              ▼                │
 │   │     (/dev/video0)     │                                 │   ┌───────────────────────┐   │
 │   └───────────────────────┘                                 └──►│    Multimodal App     │   │
 │                                                                 │  (Orchestrator Loop)  │   │
 │   ┌───────────────────────┐         gRPC (Port 50051)       ┌───│                       │   │
 │   │   Onboard Speakers    │ ◄───────────────────────────────┤   └───────────┬───────────┘   │
 │   │       (ALSA)          │      (Magpie TTS + Codec)       │               │ (Vision + Tx) │
 │   └───────────────────────┘                                 │               ▼               │
 │                                                             │   ┌───────────────────────┐   │
 │                                                             └───┤      vLLM Server      │   │
 │                                                                 │  (Gemma-4 VLM + MTP)  │   │
 │                                                                 │      (Port 8000)      │   │
 │                                                                 └───────────────────────┘   │
 └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Table of Contents
1. [Network Setup & Direct Ethernet SSH](#step-1-network-setup--direct-ethernet-ssh)
2. [Offline Asset Preparation on Host Laptop](#step-2-offline-asset-preparation-on-host-laptop)
3. [Transferring Assets to Unitree R1](#step-3-transferring-assets-to-unitree-r1)
4. [Test 1: Unitree Audio Loopback Test (Mic & Speakers)](#step-4-test-1-unitree-audio-loopback-test)
5. [Building & Running NeMo-Speech.cpp (CUDA + Riva gRPC)](#step-5-building--running-nemo-speechcpp-on-jetson)
6. [Serving Gemma-4 VLM via vLLM](#step-6-serving-gemma-4-vlm-via-vllm)
7. [Test 2: Full Multimodal Vision & Voice Assistant](#step-7-test-2-full-multimodal-vision--voice-assistant)
8. [Low-Latency Optimization & Cheatsheet](#step-8-low-latency-optimization--cheatsheet)

---

## Step 1: Network Setup & Direct Ethernet SSH

The Unitree internal network uses static IP subnets over its physical Ethernet port.

```
┌───────────────────────────┐      Direct Ethernet Cable      ┌───────────────────────────┐
│     Host Laptop (Mac)     │ ◄─────────────────────────────► │    Unitree R1 (Jetson)    │
│  IP: 192.168.123.50       │                                 │  IP: 192.168.123.164      │
└───────────────────────────┘                                 └───────────────────────────┘
```

### 1.1 Configure Host Laptop Ethernet Interface
1. Connect an Ethernet cable directly between your laptop and the Jetson Orin Ethernet port on the Unitree R1.
2. In macOS **System Settings $\rightarrow$ Network $\rightarrow$ Ethernet**:
   - **Configure IPv4**: Manually
   - **IP Address**: `192.168.123.50`
   - **Subnet Mask**: `255.255.255.0` (`/24`)
   - **Router**: `192.168.123.1` (or leave empty)

### 1.2 Verify Connection & SSH into Robot
```bash
# Ping the Jetson
ping -c 3 192.168.123.164

# SSH into Jetson Orin (Default Unitree password: 123)
ssh unitree@192.168.123.164
```

---

## Step 2: Offline Asset Preparation on Host Laptop

Run all commands in this section on your **MacBook / Host Computer** (which has internet access).

### 2.1 Create Staging Directories
```bash
mkdir -p ~/robot_assets/models/asr \
         ~/robot_assets/models/magpie-tts \
         ~/robot_assets/models/nano-codec \
         ~/robot_assets/wheels
```

### 2.2 Download Speech Models (GGUF & Tokenizer)
```bash
# 1. Nemotron Streaming ASR (0.6B Q8 GGUF)
hf download nvidia/nemotron-speech-streaming-en-0.6b \
  nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --local-dir ~/robot_assets/models/asr

# 2. Magpie TTS Multilingual (357M F16 GGUF + Tokenizer archive)
hf download nvidia/magpie_tts_multilingual_357m \
  --include magpie_tts_multilingual_357m.v2602.f16.gguf \
  --include magpie_tts_multilingual_357m.nemo \
  --local-dir ~/robot_assets/models/magpie-tts

# Extract the Magpie tokenizer assets loaded by the runtime
mkdir -p ~/robot_assets/models/magpie-tts/extracted
tar -xf ~/robot_assets/models/magpie-tts/magpie_tts_multilingual_357m.nemo \
  -C ~/robot_assets/models/magpie-tts/extracted

# 3. NanoCodec Neural Audio Decoder (22kHz F16 GGUF)
hf download nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps \
  nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --local-dir ~/robot_assets/models/nano-codec
```

### 2.3 Download Gemma-4 Vision-Language & Speculative MTP Models
```bash
# Main Model: Gemma-4-E2B-it
hf download google/gemma-4-E2B-it \
  --local-dir ~/robot_assets/models/gemma-4-E2B-it

# Speculative MTP Assistant / Draft Model
hf download google/gemma-4-E2B-it-assistant \
  --local-dir ~/robot_assets/models/gemma-4-E2B-it-assistant
```

### 2.4 Download Python 3.8 ARM64 Binary Wheels
The Unitree JetPack 5 environment runs **Python 3.8.10 (`cp38`)**. Download the exact pre-compiled binary wheels:
```bash
cd ~/robot_assets/wheels
rm -rf *.whl

pip download \
  --only-binary=:all: \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 38 \
  --abi cp38 \
  --dest ~/robot_assets/wheels \
  sounddevice soundfile requests nvidia-riva-client opencv-python-headless numpy
```

---

## Step 3: Transferring Assets to Unitree R1

From your **Host Laptop**, transfer the assets and source repositories over Ethernet SSH:

```bash
# 1. Transfer model weights and python wheels (~7 GB total)
scp -r ~/robot_assets unitree@192.168.123.164:~/robot_assets

# 2. Transfer NeMo-Speech.cpp source code
scp -r /Users/ikwak/Code/NeMo-Speech.cpp unitree@192.168.123.164:~/NeMo-Speech.cpp

# 3. Transfer test scripts from jetski-playground
scp /Users/ikwak/Code/jetski-playground/robotics/unitree-r1/test_audio_loopback.py unitree@192.168.123.164:~/test_audio_loopback.py
scp /Users/ikwak/Code/jetski-playground/robotics/unitree-r1/test_vision_voice_assistant.py unitree@192.168.123.164:~/test_vision_voice_assistant.py
```

---

## Step 4: Test 1: Unitree Audio Loopback Test

This test verifies that the Unitree onboard microphone and speakers operate properly through ALSA before starting AI services.

### Test Sequence:
1. Speaks: *"Tell me something"*
2. Waits 1 second.
3. Records 5 seconds of audio from the onboard microphone.
4. Plays the recorded audio back through the robot speakers.

### 4.1 Install Python Wheels on Robot
SSH into the Jetson Orin and install the dependencies:
```bash
ssh unitree@192.168.123.164

cd ~/robot_assets/wheels
pip3 install --no-index --find-links=. \
  sounddevice soundfile requests nvidia-riva-client opencv-python-headless numpy
```

### 4.2 Run Test 1
```bash
python3 ~/test_audio_loopback.py
```

*Expected Output:*
```text
============================================================
🤖 Unitree R1 Audio Test: Microphone & Speaker Loopback
============================================================
🔊 Robot saying: "Tell me something"
⏳ Waiting 1 second...

🎙️ RECORDING for 5 seconds... Speak into the robot mic now!
🛑 Recording finished!
🔊 Playing back recorded audio through onboard speakers...
✅ Playback complete!
```

---

## Step 5: Building & Running NeMo-Speech.cpp on Jetson

Compile `NeMo-Speech.cpp` with native **CUDA** acceleration on the Jetson Orin GPU.

### 5.1 Build `riva_server` with CUDA Support
On the Jetson Orin:
```bash
cd ~/NeMo-Speech.cpp

# Configure CMake with CUDA and Riva gRPC server targets
cmake -B build-cuda -G Ninja \
  -DGGML_CUDA=ON \
  -DNEMO_SPEECH_BUILD_GRPC=ON \
  -DNEMO_SPEECH_BUILD_ASR=ON \
  -DNEMO_SPEECH_BUILD_TTS=ON \
  -DCMAKE_BUILD_TYPE=Release

# Build binary
cmake --build build-cuda --target riva_server -j$(nproc)
```

### 5.2 Launch `riva_server` (Terminal 1)
```bash
cd ~/NeMo-Speech.cpp

./build-cuda/bin/riva_server \
  --asr.model.path ~/robot_assets/models/asr/nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --tts.magpie-model ~/robot_assets/models/magpie-tts/magpie_tts_multilingual_357m.v2602.f16.gguf \
  --tts.codec-model ~/robot_assets/models/nano-codec/nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --tts.tokenizer-model-dir ~/robot_assets/models/magpie-tts/extracted \
  --bind 127.0.0.1:50051 \
  --tts.cfg-scale 2.5
```
*Leave this running in Terminal 1.*

---

## Step 6: Serving Gemma-4 VLM via vLLM

In a new terminal window (**Terminal 2**), start `vLLM` with **MTP Speculative Decoding**:

```bash
ssh unitree@192.168.123.164

vllm serve ~/robot_assets/models/gemma-4-E2B-it \
  --trust-remote-code \
  --max-model-len 1024 \
  --max-num-seqs 1 \
  --max-num-batched-tokens 512 \
  --gpu-memory-utilization 0.55 \
  --speculative-config '{"method":"mtp","model":"/home/unitree/robot_assets/models/gemma-4-E2B-it-assistant","num_speculative_tokens":1}' \
  --override-generation-config '{"temperature": 0.0}' \
  --enable-prefix-caching \
  --port 8000
```
*Leave this running in Terminal 2.*

---

## Step 7: Test 2: Full Multimodal Vision & Voice Assistant

In **Terminal 3**, run the full integrated assistant:

### Test Sequence:
1. Speaks: *"Ask me what I am seeing"* via Magpie TTS.
2. Waits 1 second.
3. Records speech for 5 seconds from the onboard mic.
4. Transcribes speech via Nemotron Streaming ASR over Riva gRPC.
5. Captures a live frame from the onboard camera (`/dev/video0`).
6. Passes the image and prompt to the Gemma-4 VLM.
7. Synthesizes the generated answer via Magpie TTS and speaks it through the robot speakers.

```bash
ssh unitree@192.168.123.164

python3 ~/test_vision_voice_assistant.py
```

*Expected Output:*
```text
============================================================
🤖 Unitree R1 Multimodal Test 2 (Camera + Mic + Gemma4 + TTS)
============================================================
🤖 Speaking: "Ask me what I am seeing"
⏳ Waiting 1 second...

🎙️ RECORDING for 5 seconds... Speak now!
🛑 Recording finished!
🧠 Transcribing voice with NeMo ASR...
🗣️ You said: "What do you see in front of you?"
📸 Capturing frame from onboard camera...
🧠 Querying Gemma-4 VLM...
💡 Gemma-4 Answer: "I see a person sitting at a desk with a computer."
🤖 Speaking: "I see a person sitting at a desk with a computer."
```

---

## Step 8: Low-Latency Optimization & Cheatsheet

### Memory Allocation Breakdown (Jetson Unified Memory)
| Component | Engine | VRAM / RAM |
| :--- | :--- | :--- |
| **ASR + TTS** | `NeMo-Speech.cpp` (`riva_server`) | ~1.2 GB |
| **VLM + MTP** | `vLLM` (`gemma-4-E2B-it` + MTP Assistant) | ~3.8 GB |
| **Robot OS + CycloneDDS** | Unitree SDK2 | ~0.8 GB |
| **Total Memory** | — | **~5.8 GB** *(Fits comfortably inside 8GB, 16GB, and 32GB Orin)* |

### Service Management Ports
| Service | Protocol | Default Port | Endpoint / Target |
| :--- | :--- | :--- | :--- |
| **NeMo-Speech** | gRPC | `50051` | `riva.speech.ASR` / `riva.speech.TTS` |
| **vLLM Inference** | HTTP REST | `8000` | `http://127.0.0.1:8000/v1/chat/completions` |
| **Camera Feed** | V4L2 | `/dev/video0` | `cv2.VideoCapture(0)` (384x384 JPEG) |
