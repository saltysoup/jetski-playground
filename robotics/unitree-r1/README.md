# Complete Deployment Guide: Multimodal AI on Unitree R1 (Jetson Orin)

This guide walks you through deploying the complete offline multimodal voice and vision pipeline on the **NVIDIA Jetson Orin** inside a **Unitree R1** robot.

Because the Unitree R1 is in an **offline environment (no internet access)**, all models, dependencies, and code are pre-downloaded on your host computer (MacBook/PC) and transferred to the Jetson over a direct Ethernet SSH connection.

---

## Table of Contents
1. [Network Setup & Direct Ethernet SSH](#step-1-network-setup--direct-ethernet-ssh)
2. [Offline Package & Asset Preparation](#step-2-offline-package--asset-preparation)
3. [Test 1: Unitree Audio Loopback Test (Mic & Speakers)](#step-3-test-1-unitree-audio-loopback-test)
4. [Installing & Building NeMo-Speech.cpp (CUDA + Riva gRPC)](#step-4-installing--building-nemo-speechcpp-on-jetson)
5. [Deploying vLLM & Gemma-4 (with MTP Speculative Decoding)](#step-5-deploying-vllm--gemma-4-on-jetson)
6. [Test 2: Full Multimodal Vision & Voice Assistant](#step-6-test-2-full-multimodal-vision--voice-assistant)
7. [Operational & Low-Latency Optimization Cheatsheet](#step-7-operational--low-latency-cheatsheet)

---

## Step 1: Network Setup & Direct Ethernet SSH

The Unitree R1 internal network uses static IP subnets over its physical Ethernet port.

```
┌───────────────────────────┐      Direct Ethernet Cable      ┌───────────────────────────┐
│     Host Laptop (Mac)     │ ◄─────────────────────────────► │    Unitree R1 (Jetson)    │
│  IP: 192.168.123.50       │                                 │  IP: 192.168.123.161      │
└───────────────────────────┘                                 └───────────────────────────┘
```

### 1.1 Configure Host Laptop Ethernet Interface
1. Connect an Ethernet cable directly from your MacBook/PC to the Jetson Orin Ethernet port on the Unitree R1.
2. Configure your laptop's Ethernet adapter with a static IP:
   - **IP Address**: `192.168.123.50`
   - **Subnet Mask**: `255.255.255.0` (`/24`)
   - **Router / Gateway**: `192.168.123.1` (or leave blank)

### 1.2 SSH into the Unitree R1 Jetson Orin
Open a terminal on your host machine:

```bash
# Test connectivity
ping -c 3 192.168.123.161

# SSH into Jetson Orin (Default Unitree credentials: unitree / 123)
ssh unitree@192.168.123.161
```

*(Note: If your unit is configured with a different IP, replace `192.168.123.161` with your robot's Jetson IP).*

---

## Step 2: Offline Package & Asset Preparation

Because the Jetson has no internet, download all assets on your host laptop first, then transfer them via `scp`.

### 2.1 Download Speech Models on Host Laptop
Create a staging directory on your laptop and download the models:

```bash
mkdir -p ~/robot_assets/models/magpie-tts ~/robot_assets/wheels

# 1. Nemotron Streaming ASR (0.6B Q8 GGUF)
hf download nvidia/nemotron-speech-streaming-en-0.6b \
  nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --local-dir ~/robot_assets/models

# 2. Magpie TTS Multilingual (magpie_tts_multilingual_357m.v2602.f16.gguf + Tokenizer Archive)
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
  --local-dir ~/robot_assets/models
```

### 2.2 Download Gemma-4 VLM & MTP Draft Models on Host Laptop
```bash
# Main Model: Gemma-4-E2B-it
hf download google/gemma-4-E2B-it \
  --local-dir ~/robot_assets/models/gemma-4-E2B-it

# Speculative MTP Assistant / Draft Model
hf download google/gemma-4-E2B-it-assistant \
  --local-dir ~/robot_assets/models/gemma-4-E2B-it-assistant

# Semantic Router Model
hf download sentence-transformers/all-MiniLM-L6-v2 \
  --local-dir ~/robot_assets/models/all-MiniLM-L6-v2
```

### 2.3 Download Offline Python Wheels (ARM64) on Host Laptop
On a machine with internet access (or using `pip download --platform manylinux2014_aarch64`):
```bash
pip download \
  --only-binary=:all: \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 310 \
  --dest ~/robot_assets/wheels \
  nvidia-riva-client sounddevice soundfile opencv-python requests \
  sentence-transformers semantic-router
```

### 2.4 Transfer All Assets to Jetson Orin
```bash
# Transfer models and wheels over Ethernet SSH
scp -r ~/robot_assets unitree@192.168.123.161:~/robot_assets
scp -r /Users/ikwak/Code/NeMo-Speech.cpp unitree@192.168.123.161:~/NeMo-Speech.cpp
```

---

## Step 3: Test 1: Unitree Audio Loopback Test

This test verifies that the Unitree onboard microphone and onboard speakers are working properly using `unitree_sdk2_python` and local ALSA audio drivers.

### Sequence:
1. Speaks: *"Tell me something"*
2. Pauses for 1 second.
3. Records from onboard microphone for 5 seconds.
4. Plays the recorded audio back through onboard speakers.

### 3.1 Install Offline Wheels on Jetson
SSH into the Jetson and install the transferred wheels:
```bash
cd ~/robot_assets/wheels
pip3 install --no-index --find-links=. *.whl
```

### 3.2 Create `test_audio_loopback.py`
Create `~/test_audio_loopback.py` on the Jetson:

```python
#!/usr/bin/env python3
"""
Unitree R1 Test 1: Audio Loopback (Mic & Speaker)
"""

import time
import os
import numpy as np
import sounddevice as sd
import soundfile as sf

def speak_tts_prompt(text="Tell me something"):
    print(f"🔊 Robot saying: \"{text}\"")
    os.system(f'espeak "{text}" 2>/dev/null || aplay /usr/share/sounds/alsa/Front_Center.wav 2>/dev/null')

def record_audio(duration_sec=5, sample_rate=16000):
    print(f"\n🎙️ RECORDING for {duration_sec} seconds... Speak into the robot mic now!")
    audio = sd.rec(int(duration_sec * sample_rate), samplerate=sample_rate, channels=1, dtype='int16')
    sd.wait()
    print("🛑 Recording finished!")
    return audio

def playback_audio(audio_data, sample_rate=16000):
    print("🔊 Playing back recorded audio through onboard speakers...")
    sd.play(audio_data, samplerate=sample_rate)
    sd.wait()
    print("✅ Playback complete!")

def main():
    print("=" * 60)
    print("🤖 Unitree R1 Audio Test: Microphone & Speaker Loopback")
    print("=" * 60)
    
    # 1. Robot prompts user
    speak_tts_prompt("Tell me something")
    
    # 2. Wait 1 second
    print("⏳ Waiting 1 second...")
    time.sleep(1.0)
    
    # 3. Record for 5 seconds
    audio_data = record_audio(duration_sec=5, sample_rate=16000)
    
    # 4. Playback
    playback_audio(audio_data, sample_rate=16000)

if __name__ == "__main__":
    main()
```

### 3.3 Run Test 1
```bash
python3 ~/test_audio_loopback.py
```

---

## Step 4: Installing & Building NeMo-Speech.cpp on Jetson

Build `NeMo-Speech.cpp` natively with CUDA acceleration on the Jetson Orin.

### 4.1 Build `riva_server` with CUDA
On the Jetson:
```bash
cd ~/NeMo-Speech.cpp

# Configure with CUDA & Riva gRPC support
cmake -B build-cuda -G Ninja \
  -DGGML_CUDA=ON \
  -DNEMO_SPEECH_BUILD_GRPC=ON \
  -DNEMO_SPEECH_BUILD_ASR=ON \
  -DNEMO_SPEECH_BUILD_TTS=ON \
  -DCMAKE_BUILD_TYPE=Release

# Compile the Riva gRPC server
cmake --build build-cuda --target riva_server -j$(nproc)
```

### 4.2 Start `riva_server` (Terminal 1)
```bash
cd ~/NeMo-Speech.cpp

./build-cuda/bin/riva_server \
  --asr.model.path ~/robot_assets/models/nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  --tts.magpie-model ~/robot_assets/models/magpie-tts/magpie_tts_multilingual_357m.v2602.f16.gguf \
  --tts.codec-model ~/robot_assets/models/nemo_nano_codec_22khz_1.89kbps_21.5fps.decoder.f16.gguf \
  --tts.tokenizer-model-dir ~/robot_assets/models/magpie-tts/extracted \
  --bind 127.0.0.1:50051 \
  --tts.cfg-scale 2.5
```

---

## Step 5: Deploying vLLM & Gemma-4 on Jetson

### 5.1 Launch vLLM with MTP Speculative Decoding (Terminal 2)
```bash
vllm serve ~/robot_assets/models/gemma-4-E2B-it \
  --trust-remote-code \
  --max-model-len 1024 \
  --max-num-seqs 1 \
  --max-num-batched-tokens 512 \
  --gpu-memory-utilization 0.55 \
  --speculative-config '{"method":"mtp","model":"/home/unitree/robot_assets/models/gemma-4-E2B-it-assistant","num_speculative_tokens":1}' \
  --override-generation-config '{"temperature": 0.0}' \
  --no-async-scheduling \
  --port 8000
```

---

## Step 6: Test 2: Full Multimodal Vision & Voice Assistant

This end-to-end application integrates:
- Unitree Onboard Microphone
- Unitree Onboard Head Camera
- Unitree Onboard Speakers
- NeMo-Speech Riva ASR & TTS (gRPC)
- Gemma-4 Multimodal VLM (vLLM Endpoint)

### Sequence:
1. Robot speaks: *"Ask me what I am seeing"* via NeMo TTS.
2. Pauses 1 second.
3. Records from the microphone for 5 seconds.
4. Transcribes the speech using Nemotron streaming ASR.
5. Captures an image with the head camera.
6. Passes the image + question to the Gemma-4 VLM.
7. Streams the answer to NeMo TTS and plays the response through the robot speakers.

### 6.1 Create `test_vision_voice_assistant.py`
Create `~/test_vision_voice_assistant.py` on the Jetson:

```python
#!/usr/bin/env python3
"""
Unitree R1 Test 2: Full Multimodal Vision & Voice Assistant
"""

import os
import sys
import cv2
import time
import json
import base64
import requests
import numpy as np
import sounddevice as sd
import riva.client

# Server Configurations
RIVA_URI = "127.0.0.1:50051"
VLLM_URL = "http://localhost:8000/v1/chat/completions"
MODEL_NAME = "google/gemma-4-E2B-it"

# 1. Initialize Riva gRPC Client
riva_auth = riva.client.Auth(uri=RIVA_URI)
riva_asr = riva.client.ASRService(riva_auth)
riva_tts = riva.client.SpeechSynthesisService(riva_auth)

def speak_via_riva(text):
    """Synthesizes text via NeMo TTS and plays through Unitree speakers."""
    print(f"🤖 Speaking: \"{text}\"")
    try:
        resp = riva_tts.synthesize(
            text=text,
            voice_name="jason",
            language_code="en-US",
            sample_rate_hz=22050
        )
        if resp.audio:
            audio_np = np.frombuffer(resp.audio, dtype=np.int16)
            sd.play(audio_np, samplerate=22050)
            sd.wait()
    except Exception as e:
        print(f"❌ TTS Error: {e}")

def record_audio_buffer(duration_sec=5, sample_rate=16000):
    """Records mic audio directly into memory."""
    print(f"\n🎙️ RECORDING for {duration_sec} seconds... Speak now!")
    audio = sd.rec(int(duration_sec * sample_rate), samplerate=sample_rate, channels=1, dtype='int16')
    sd.wait()
    print("🛑 Recording finished!")
    return audio.tobytes()

def transcribe_audio_bytes(audio_bytes):
    """Transcribes audio using NeMo Streaming ASR via Riva gRPC."""
    print("🧠 Transcribing voice with NeMo ASR...")
    config = riva.client.RecognitionConfig(
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hertz=16000,
        language_code="en-US",
        max_alternatives=1,
        enable_automatic_punctuation=True,
    )
    response = riva_asr.offline_recognize(audio_bytes, config)
    if response.results and response.results[0].alternatives:
        transcript = response.results[0].alternatives[0].transcript.strip()
        print(f"🗣️ You said: \"{transcript}\"")
        return transcript
    return ""

def capture_camera_frame():
    """Captures a snapshot from the Unitree head camera."""
    print("📸 Capturing frame from onboard camera...")
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("❌ Could not open camera /dev/video0")
        return None
    
    # Allow camera auto-exposure to stabilize
    for _ in range(5):
        ret, frame = cap.read()
    cap.release()
    
    if ret:
        small = cv2.resize(frame, (384, 384), interpolation=cv2.INTER_AREA)
        _, buffer = cv2.imencode('.jpg', small, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return base64.b64encode(buffer).decode('utf-8')
    return None

def query_gemma4_vlm(user_text, image_b64):
    """Queries vLLM Gemma-4 with multimodal image and text prompt."""
    print("🧠 Querying Gemma-4 VLM...")
    content = []
    if image_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}
        })
    content.append({"type": "text", "text": user_text})
    
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "system",
                "content": "You are the Unitree R1 robot assistant. Answer directly in one concise spoken sentence under 25 words."
            },
            {"role": "user", "content": content}
        ],
        "temperature": 0.0,
        "max_tokens": 80
    }
    
    resp = requests.post(VLLM_URL, json=payload)
    if resp.status_code == 200:
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"💡 Gemma-4 Answer: \"{answer}\"")
        return answer
    else:
        print(f"❌ vLLM Error: {resp.text}")
        return "I encountered an error analyzing what I see."

def main():
    print("=" * 60)
    print("🤖 Unitree R1 Multimodal Test 2 (Camera + Mic + Gemma4 + TTS)")
    print("=" * 60)
    
    # 1. Robot says initial prompt
    speak_via_riva("Ask me what I am seeing")
    
    # 2. Wait 1 second
    print("⏳ Waiting 1 second...")
    time.sleep(1.0)
    
    # 3. Record voice for 5 seconds
    audio_bytes = record_audio_buffer(duration_sec=5, sample_rate=16000)
    
    # 4. Transcribe voice
    transcript = transcribe_audio_bytes(audio_bytes)
    if not transcript:
        transcript = "What do you see?"
        print(f"⚠️ Defaulting prompt to: \"{transcript}\"")
        
    # 5. Capture camera frame
    image_b64 = capture_camera_frame()
    
    # 6. Pass image and prompt to Gemma-4
    response_text = query_gemma4_vlm(transcript, image_b64)
    
    # 7. Speak answer through robot speakers
    speak_via_riva(response_text)

if __name__ == "__main__":
    main()
```

### 6.2 Run Test 2 (Terminal 3)
```bash
python3 ~/test_vision_voice_assistant.py
```

---

## Step 7: Operational & Low-Latency Cheatsheet

### Memory Footprint Checklist
Run `jtop` to ensure total unified memory stays below robot capacity:
- **NeMo-Speech (`riva_server`)**: ~1.2 GB
- **Gemma-4 VLM (`vllm`)**: ~3.5–4.0 GB
- **OS & CycloneDDS**: ~0.8 GB
- **Total**: ~5.5–6.0 GB (Comfortably fits inside Orin 8GB/16GB/32GB)

### Summary of Running Services
| Service | Binary / Command | Port | Function |
| :--- | :--- | :--- | :--- |
| **Speech Engine** | `riva_server` | `50051` | Real-time Streaming ASR & TTS |
| **Vision LLM** | `vllm serve` | `8000` | Gemma-4 MTP Multimodal Inference |
| **Orchestrator** | `test_vision_voice_assistant.py` | — | Unitree I/O & Decision Loop |
