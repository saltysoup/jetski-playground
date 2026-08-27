# Unitree R1 / G1 Robot: Offline Multimodal Voice & Vision AI Stack

Complete, self-contained end-to-end guide for deploying the **Offline Real-Time Multimodal Voice & Vision Assistant** on the **NVIDIA Jetson Orin** inside a Unitree robot.

---

## 1. System Architecture & Hardware Topology

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
                                      │ Transcript (~45ms CUDA)
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

### Hardware Connections & Networking:
- **Jetson Orin IP:** `192.168.123.164/24` (Interface: `eth10`)
- **Unitree Motion/Vision Controller:** `192.168.123.161`
- **Microphone Stream:** Multicast UDP stream on `239.168.123.161:5555`
- **Speaker Stream:** Unitree SDK2 Audio Client via `/tmp/unitree_audio.sock`
- **Camera Topology:**
  - **Head Eye Camera 👁️:** DDS on `eth10` (`videohub` API 1001 via `unitree_head_camera_daemon`)
  - **Left Wrist Camera ✋:** USB V4L2 node `/dev/video0` (BrainCo 5-Finger Hand)
  - **Right Wrist Camera ✋:** USB V4L2 node `/dev/video2` (BrainCo 5-Finger Hand)

---

## 2. Phase 1: Offline Asset Preparation on Host Laptop

Because the robot operates in an **isolated offline network** (no internet access), all model weights and pre-compiled Python ARM64 wheels must be downloaded on your laptop first.

### 2.1 Install Hugging Face Hub CLI
On your host laptop (MacBook or Linux PC):
```bash
pip install -U huggingface_hub
```

### 2.2 Create Asset Staging Directories
```bash
mkdir -p ~/robot_assets/models/magpie-tts/extracted \
         ~/robot_assets/models/onnx \
         ~/robot_assets/wheels
```

### 2.3 Download All Neural Models
Run the following commands on your laptop:

```bash
# 1. Nemotron Streaming ASR (0.6B Q8 GGUF)
hf download nvidia/nemotron-speech-streaming-en-0.6b \
  nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --local-dir ~/robot_assets/models

# 2. Magpie Multilingual TTS (357M F16 GGUF + Tokenizer)
hf download nvidia/magpie_tts_multilingual_357m \
  magpie_tts_multilingual_357m.v2602.f16.gguf \
  --local-dir ~/robot_assets/models

hf download nvidia/magpie_tts_multilingual_357m \
  magpie_tts_multilingual_357m.nemo \
  --local-dir ~/robot_assets/models/magpie-tts

tar -xf ~/robot_assets/models/magpie-tts/magpie_tts_multilingual_357m.nemo \
  -C ~/robot_assets/models/magpie-tts/extracted

# 3. NanoCodec Neural Audio Decoder (22kHz F16 GGUF)
hf download nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps \
  nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --local-dir ~/robot_assets/models

# 4. Gemma-4 Multimodal VLM (Gemma-4 E2B Q8_0 GGUF + mmproj F16)
hf download ggml-org/gemma-4-E2B-it-GGUF \
  gemma-4-E2B-it-q8_0.gguf \
  mmproj-gemma-4-E2B-f16.gguf \
  --local-dir ~/robot_assets/models
```

### 2.4 Download Pre-compiled Python 3.8 ARM64 Binary Wheels
The Jetson Orin runs JetPack 5 with **Python 3.8.10 (`cp38`)**. Download the exact Linux ARM64 binary wheels:

```bash
pip download \
  --only-binary=:all: \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 38 \
  --abi cp38 \
  --dest ~/robot_assets/wheels \
  sounddevice soundfile requests nvidia-riva-client opencv-python-headless numpy onnxruntime
```

---

## 3. Phase 2: Connecting to Robot & Transferring Assets

### 3.1 Network Setup on Host Laptop
1. Connect an Ethernet cable directly from your laptop to the Jetson Orin Ethernet port.
2. In your host network settings, configure Ethernet IPv4 manually:
   - **IP Address:** `192.168.123.50`
   - **Subnet Mask:** `255.255.255.0`
3. Verify connection:
   ```bash
   ping -c 3 192.168.123.164
   ```

### 3.2 Transfer Assets to Robot via SCP
From your laptop terminal:
```bash
# 1. Transfer model weights and wheels (~10 GB)
scp -r ~/robot_assets unitree@192.168.123.164:/home/unitree/robot_assets

# 2. Transfer NeMo-Speech.cpp and assistant code
scp -r /path/to/NeMo-Speech.cpp unitree@192.168.123.164:/home/unitree/NeMo-Speech.cpp
scp app.sh test_vision_voice_assistant.py unitree@192.168.123.164:/home/unitree/
```

---

## 4. Phase 3: Jetson Setup & Building Native CUDA Binaries

SSH into the Jetson Orin:
```bash
ssh unitree@192.168.123.164
# Default Password: 123
```

### 4.1 Install Offline Python Wheels
```bash
cd /home/unitree/robot_assets/wheels
pip3 install --no-index --find-links=. \
  sounddevice soundfile requests nvidia-riva-client opencv-python-headless numpy onnxruntime
```

### 4.2 Build Riva Speech Server (CUDA ASR + Magpie TTS)
```bash
cd /home/unitree/NeMo-Speech.cpp
mkdir -p build-cuda && cd build-cuda
cmake .. -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
```

### 4.3 Build Gemma-4 Multimodal VLM (`llama-server`)
```bash
cd /home/unitree/NeMo-Speech.cpp/llama.cpp
mkdir -p build-cuda && cd build-cuda
cmake .. -DGGML_CUDA=ON -DGGML_CUDA_FA_ALL_QUANTS=ON -DCMAKE_BUILD_TYPE=Release
make llama-server -j$(nproc)
```

### 4.4 Build Unitree SDK2 Daemons
```bash
cd /home/unitree/unitree_sdk2

# Copy C++ daemon sources into SDK examples
cp /home/unitree/NeMo-Speech.cpp/scripts/unitree_head_camera_daemon.cpp example/go2/
cp /home/unitree/NeMo-Speech.cpp/scripts/unitree_audio_daemon.cpp example/g1/audio/

# Build daemons
cd build
cmake ..
make unitree_head_camera_daemon unitree_audio_daemon unitree_play_wav -j$(nproc)
```

---

## 5. Phase 4: Launching and Testing the Assistant

### 5.1 One-Command Full Stack Startup
Make the launcher executable and run:
```bash
chmod +x /home/unitree/app.sh
bash /home/unitree/app.sh
```

`app.sh` automatically:
1. Cleans up any stale background processes.
2. Launches `riva_server` with Nemotron ASR and Magpie TTS on port `50051`.
3. Launches `llama-server` with Gemma-4 VLM and mmproj on port `8000`.
4. Launches `unitree_audio_daemon` (handling gapless speaker output).
5. Launches `unitree_head_camera_daemon` (streaming head eye frames over DDS).
6. Polls until all neural engines complete CUDA graph warmup, then launches the interactive assistant.

---

### 5.2 Selecting Vision Source
To switch between the head eyes and wrist cameras, export `CAMERA_SOURCE` before running `app.sh`:

```bash
# 1. Head Eye Wide-Angle Camera (Default):
export CAMERA_SOURCE="head"
bash /home/unitree/app.sh

# 2. Left BrainCo Hand Wrist Camera:
export CAMERA_SOURCE="wrist0"
bash /home/unitree/app.sh

# 3. Right BrainCo Hand Wrist Camera:
export CAMERA_SOURCE="wrist2"
bash /home/unitree/app.sh
```

---

### 5.3 Power Management (15W Recommended Mode)
To reduce battery drain and prevent thermal throttling:
```bash
# Set Jetson Orin to 15W mode (Mode ID 2)
sudo nvpmodel -m 2

# Verify
sudo nvpmodel -q
```
