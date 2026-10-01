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
mkdir -p ~/robot_assets/models/magpie-tts/extracted          ~/robot_assets/models/onnx          ~/robot_assets/wheels
```

### 2.3 Download All Neural Models
Run the following commands on your laptop:

```bash
# 1. Nemotron Streaming ASR (0.6B Q8 GGUF)
hf download nvidia/nemotron-speech-streaming-en-0.6b   nemotron-speech-streaming-en-0.6b.q8_0.gguf   --local-dir ~/robot_assets/models

# 2. Magpie Multilingual TTS (357M F16 GGUF + Tokenizer)
hf download nvidia/magpie_tts_multilingual_357m   magpie_tts_multilingual_357m.v2602.f16.gguf   --local-dir ~/robot_assets/models

hf download nvidia/magpie_tts_multilingual_357m   magpie_tts_multilingual_357m.nemo   --local-dir ~/robot_assets/models/magpie-tts

tar -xf ~/robot_assets/models/magpie-tts/magpie_tts_multilingual_357m.nemo   -C ~/robot_assets/models/magpie-tts/extracted

# 3. NanoCodec Neural Audio Decoder (22kHz F16 GGUF)
hf download nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps   nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf   --local-dir ~/robot_assets/models

# 4. Gemma-4 Multimodal VLM (Gemma-4 E2B Q8_0 GGUF + mmproj F16)
hf download ggml-org/gemma-4-E2B-it-GGUF   gemma-4-E2B-it-q8_0.gguf   mmproj-gemma-4-E2B-f16.gguf   --local-dir ~/robot_assets/models

# 5. MiniLM-L6-v2 Semantic Router (INT8 ONNX Quantized + Vocab)
hf download sentence-transformers/all-MiniLM-L6-v2   --include "onnx/model_qint8_arm64.onnx"   --include "vocab.txt"   --local-dir ~/robot_assets/models
```

### 2.4 Download Pre-compiled Python 3.8 ARM64 Binary Wheels
The Jetson Orin runs JetPack 5 with **Python 3.8.10 (`cp38`)**. Download the exact Linux ARM64 binary wheels:

```bash
pip download   --only-binary=:all:   --platform manylinux2014_aarch64   --implementation cp   --python-version 38   --abi cp38   --dest ~/robot_assets/wheels   sounddevice soundfile requests nvidia-riva-client opencv-python-headless numpy onnxruntime
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

# 2. Transfer NeMo-Speech.cpp and this repo folder (assistant, launcher, daemon sources, tests)
scp -r /path/to/NeMo-Speech.cpp unitree@192.168.123.164:/home/unitree/NeMo-Speech.cpp
scp -r /path/to/unitree-r1 unitree@192.168.123.164:/home/unitree/unitree-r1
ssh unitree@192.168.123.164 'cp /home/unitree/unitree-r1/app.sh /home/unitree/unitree-r1/test_vision_voice_assistant.py /home/unitree/'
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
pip3 install --no-index --find-links=.   sounddevice soundfile requests nvidia-riva-client opencv-python-headless numpy onnxruntime
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

# Copy C++ daemon sources (from this repo's scripts/ folder, transferred in step 3.2) into SDK examples
cp /home/unitree/unitree-r1/scripts/unitree_head_camera_daemon.cpp example/go2/
cp /home/unitree/unitree-r1/scripts/unitree_audio_daemon.cpp example/g1/audio/
cp /home/unitree/unitree-r1/scripts/unitree_play_wav.cpp example/g1/audio/

# Register the build targets (copying a .cpp alone does not create a make target).
# Skip any line that is already present.
grep -q unitree_play_wav example/g1/CMakeLists.txt || cat >> example/g1/CMakeLists.txt <<'EOF'
add_executable(unitree_play_wav audio/unitree_play_wav.cpp)
target_link_libraries(unitree_play_wav unitree_sdk2)
EOF
grep -q unitree_audio_daemon example/g1/CMakeLists.txt || cat >> example/g1/CMakeLists.txt <<'EOF'
add_executable(unitree_audio_daemon audio/unitree_audio_daemon.cpp)
target_link_libraries(unitree_audio_daemon unitree_sdk2)
EOF
grep -q unitree_head_camera_daemon example/go2/CMakeLists.txt || cat >> example/go2/CMakeLists.txt <<'EOF'
add_executable(unitree_head_camera_daemon unitree_head_camera_daemon.cpp)
target_link_libraries(unitree_head_camera_daemon unitree_sdk2)
EOF

# Build daemons
mkdir -p build && cd build
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
6. Polls until Riva accepts connections and `llama-server` `/health` returns 200 (model fully loaded), then launches the interactive assistant.

Services keep running after the assistant exits, so you can re-run `python3 /home/unitree/test_vision_voice_assistant.py` directly. Other modes:
```bash
bash /home/unitree/app.sh --services-only   # start services only
bash /home/unitree/app.sh --stop            # stop all services (frees GPU memory)
```

### 5.1.1 Tests
```bash
cd /home/unitree/unitree-r1
python3 tests/test_assistant_units.py   # offline unit tests (no services needed)
python3 tests/calibrate_router.py       # MiniLM router accuracy vs threshold
python3 tests/integration_test.py       # silent: services, ASR/TTS, camera, Gemma, router load
python3 tests/bench_latency.py          # silent: ASR/TTS/LLM/vision latency numbers
python3 tests/e2e_injected.py /home/unitree/test_vision_voice_assistant.py   # push-to-talk loop, robot speaks
python3 tests/e2e_vad_injected.py       # hands-free loop (VAD + wake word + follow-up), robot speaks
```
> [!NOTE]
> The robot's mic array cancels the robot's own speaker output, so the E2E tests inject synthesized
> questions in place of mic capture rather than having the robot ask itself out loud.

### 5.1.2 Hands-free mode (VAD + wake word)
Instead of pressing ENTER, the assistant can listen continuously: [Silero VAD](https://github.com/snakers4/silero-vad)
detects the end of each utterance and the request is sent automatically.

1. On the laptop, download the model (2.3 MB) and copy it to the robot:
   ```bash
   mkdir -p ~/robot_assets/models/vad
   curl -L -o ~/robot_assets/models/vad/silero_vad.onnx \
     https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx
   scp ~/robot_assets/models/vad/silero_vad.onnx unitree@192.168.123.164:/home/unitree/robot_assets/models/vad/
   ```
2. Try it in a terminal: `python3 /home/unitree/test_vision_voice_assistant.py --vad`
3. Say **"Jason, what do you see?"**. For 10 s after each reply, follow-up questions need no wake word.
   Speech that doesn't start with the wake word is ignored. The mic is muted while the robot talks.

### 5.1.3 Run in the background at boot (systemd)
```bash
cd /home/unitree/unitree-r1
sudo bash service/install_service.sh            # install, enable at boot, start now
journalctl -u r1-assistant -f                   # live assistant log
sudo systemctl stop r1-assistant                # stop (e.g. to use push-to-talk in a terminal)
sudo systemctl restart r1-assistant             # restart after editing the script / env
sudo bash service/install_service.sh --remove   # uninstall
```
`r1-services` starts the four backend services (via `app.sh --services-only`) and `r1-assistant` runs the
assistant with `--vad`, restarting it if it crashes. Settings go in `/home/unitree/r1-assistant.env`
(one `VAR=value` per line), for example:

| Variable | Default | Meaning |
|---|---|---|
| `WAKE_WORDS` | `jason,jayson,jaysen,jaison` | Accepted spellings of the name; empty = respond to all speech |
| `FOLLOW_UP_SEC` | `10` | Seconds after a reply during which no wake word is needed |
| `MEMORY_TURNS` | `3` | Previous exchanges sent to Gemma, so follow-ups like "and Germany?" work (0 = off) |
| `CONVERSATION_MEMORY_SEC` | `120` | Forget the conversation after this much silence |
| `VAD_GAIN` | `2.0` | Mic boost before VAD (raise if quiet speech is missed) |
| `VAD_END_SILENCE_SEC` | `0.6` | Pause length that ends an utterance |
| `GREETING` | `My name is Jason. Domo arigato, Mister Roboto.` | Startup phrase |
| `TTS_GAIN` | `2.0` | Speech volume (soft-limited, never clips) |
| `TTS_STREAMING` | `1` | `0` = synthesize whole sentences (slower first audio) |
| `SPECULATIVE_PREFILL` | `1` | Pre-encode the camera frame while the user talks |
| `IMAGE_MAX_SIDE` | `768` | Longest side of the frame sent to Gemma |
| `ROUTER_THRESHOLD` | `0.35` | Vision-intent threshold |

### 5.1.4 Latency design
Each turn prints a `[TIMING]` line (milliseconds after the end of your speech). What makes it fast:
- **Instant fillers:** "Hmm." / "Let me take a look." are synthesized at startup and played from memory.
- **Streaming TTS:** Magpie and Gemma share the GPU, and offline synthesis slows ~3x while Gemma is
  decoding. Streaming starts playback after ~0.25 s of audio (first answer audio 4.0-4.2 s -> 1.2-1.7 s).
- **Speculative image prefill:** when you start talking, a frame is captured and llama-server encodes it
  (max_tokens=1). If the request is visual, the final query hits the prompt cache (TTFT 1.6 s -> ~0.2 s).
  Note the frame is from the *start* of your sentence; set `SPECULATIVE_PREFILL=0` to capture it at the end.
- **Warm-up:** ASR, text and vision paths are exercised once at startup (first calls are 1.4-3 s slower).

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
