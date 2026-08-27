# Unitree R1 / G1 Jetson Deployment Guide

This guide documents the complete end-to-end setup, compilation, model placement, and hardware configuration for the **Offline Multimodal Voice & Vision Assistant** running on the NVIDIA Jetson Orin compute board.

---

## 1. System Architecture Overview

```
                          ┌────────────────────────┐
                          │   Human User (Voice)   │
                          └───────────┬────────────┘
                                      │ Multicast UDP (239.168.123.161:5555)
                                      ▼
                        ┌───────────────────────────┐
                        │  Nemotron Streaming ASR   │
                        │    (Riva Server :50051)   │
                        └─────────────┬─────────────┘
                                      │ Transcript (~45ms)
                                      ▼
                        ┌───────────────────────────┐
                        │  MiniLM Semantic Router   │
                        │     (Offline ONNX)        │
                        └──────┬─────────────┬──────┘
                               │             │
              [Visual Intent]  │             │  [Text-Only Intent]
                               ▼             ▼
       ┌──────────────────────────────┐      │
       │  Unitree Head Camera Daemon  │      │
       │     (DDS eth10 /videohub)    │      │
       └──────────────┬───────────────┘      │
                      │ JPEG Frame (<1ms)    │
                      ▼                      │
         ┌─────────────────────────┐         │
         │ Gemma-4 Multimodal VLM  │◄────────┘
         │   (llama-server :8000)  │
         └────────────┬────────────┘
                      │ Pipelined Token Stream
                      ▼
         ┌─────────────────────────┐
         │     Magpie CUDA TTS     │
         │   (Riva Server :50051)  │
         └────────────┬────────────┘
                      │ 22kHz PCM Chunks
                      ▼
         ┌─────────────────────────┐
         │  Unitree Audio Daemon   │
         │   (/tmp/unitree_audio)  │
         └────────────┬────────────┘
                      │ Speaker Playback
                      ▼
           [Robot Spoken Output]
```

---

## 2. Hardware Topology & Network Configuration

### Network Interface (`eth10`)
- **Robot Jetson IP:** `192.168.123.164/24`
- **Motion/Vision Board IP:** `192.168.123.161`
- **Subnet:** `192.168.123.0/24`

### Camera Connections
| Camera | Physical Source | Protocol / Node | Perspective |
| :--- | :--- | :--- | :--- |
| **Head Eyes Camera** 👁️ | Internal Head/Vision Board | DDS on `eth10` (`videohub` API 1001) | Front wide-angle room view |
| **Left Wrist Camera** ✋ | BrainCo Hand Breakout Port 1 | USB V4L2 (`/dev/video0`) | Downward hand / desk workspace view |
| **Right Wrist Camera** ✋ | BrainCo Hand Breakout Port 2 | USB V4L2 (`/dev/video2`) | Horizontal arm / hand view |

---

## 3. Directory Layout on the Robot (`/home/unitree/`)

```text
/home/unitree/
├── app.sh                              # Master service launcher
├── test_vision_voice_assistant.py      # Main assistant controller
├── NeMo-Speech.cpp/                    # Speech & TTS engines
│   └── build-cuda/bin/riva_server      # Riva ASR + Magpie TTS server
├── llama.cpp/                          # Multimodal VLM engine
│   └── build-cuda/bin/llama-server     # Gemma-4 VLM server
├── unitree_sdk2/                       # Unitree Robotics SDK
│   └── build/bin/
│       ├── unitree_head_camera_daemon  # Head Eye DDS frame provider
│       ├── unitree_audio_daemon        # Persistent low-latency audio player
│       └── unitree_play_wav            # Standalone audio test binary
└── robot_assets/models/                # Model weights directory
    ├── nemotron-speech-streaming-en-0.6b.q8_0.gguf
    ├── magpie_tts_multilingual_357m.v2602.f16.gguf
    ├── nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf
    ├── gemma-4-E2B-it-q8_0.gguf
    ├── mmproj-gemma-4-E2B-f16.gguf
    ├── magpie-tts/extracted/           # Tokenizer files
    └── onnx/model_qint8_arm64.onnx     # MiniLM semantic router
```

---

## 4. Build Instructions

### A. Build NeMo-Speech.cpp (ASR + Magpie TTS)
```bash
cd /home/unitree/NeMo-Speech.cpp
mkdir -p build-cuda && cd build-cuda
cmake .. -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
```

### B. Build llama.cpp (Gemma-4 Multimodal VLM)
```bash
cd /home/unitree/NeMo-Speech.cpp/llama.cpp
mkdir -p build-cuda && cd build-cuda
cmake .. -DGGML_CUDA=ON -DGGML_CUDA_FA_ALL_QUANTS=ON -DCMAKE_BUILD_TYPE=Release
make llama-server -j$(nproc)
```

### C. Build Unitree SDK2 Daemons
```bash
cd /home/unitree/unitree_sdk2/build
cmake ..
make unitree_audio_daemon unitree_head_camera_daemon unitree_play_wav -j$(nproc)
```

---

## 5. Running the Assistant

### One-Command Full Stack Startup:
```bash
bash /home/unitree/app.sh
```

### Selecting Camera Source:
```bash
# Use Head Eye Wide-Angle Camera (Default):
export CAMERA_SOURCE="head"
bash /home/unitree/app.sh

# Use Left BrainCo Wrist Camera:
export CAMERA_SOURCE="wrist0"
bash /home/unitree/app.sh

# Use Right BrainCo Wrist Camera:
export CAMERA_SOURCE="wrist2"
bash /home/unitree/app.sh
```

---

## 6. Power Management (Jetson Orin)

To set the robot to the recommended **15W power mode** (prolongs battery life and prevents thermal throttling):

```bash
# Check current power mode
sudo nvpmodel -q

# Set to 15W mode (Mode 2)
sudo nvpmodel -m 2
```
