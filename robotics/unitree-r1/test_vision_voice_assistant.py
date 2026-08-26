#!/usr/bin/env python3
"""
Unitree R1 Test 2: Full Multimodal Vision & Voice Assistant
- Microphone: Unitree UDP Multicast (239.168.123.161:5555)
- ASR: Nemotron Streaming ASR (Riva gRPC 50051)
- Camera: OpenCV (/dev/video0)
- VLM: Gemma-4 Multimodal (vLLM 8000)
- TTS: Magpie TTS (Riva gRPC 50051)
- Speakers: Unitree AudioClient DDS Player (unitree_play_wav)
"""

import os
import sys
import cv2
import time
import json
import base64
import socket
import struct
import subprocess
import requests
import soundfile as sf
import numpy as np
import riva.client

# Server & Network Configurations
RIVA_URI = "127.0.0.1:50051"
VLLM_URL = "http://localhost:8000/v1/chat/completions"
MODEL_NAME = "google/gemma-4-E2B-it"

MCAST_GRP = "239.168.123.161"
MCAST_PORT = 5555
NET_INTERFACE_IP = "192.168.123.164"
NET_INTERFACE_NAME = "eth10"
PLAYER_BIN = "/home/unitree/unitree_sdk2/build/bin/unitree_play_wav"
TEMP_TTS_WAV = "/tmp/robot_tts.wav"

# 1. Initialize Riva gRPC Client
riva_auth = riva.client.Auth(uri=RIVA_URI)
riva_asr = riva.client.ASRService(riva_auth)
riva_tts = riva.client.SpeechSynthesisService(riva_auth)

def speak_via_riva(text):
    """Synthesizes text via NeMo TTS and plays through Unitree robot speakers."""
    print(f"🤖 Speaking: \"{text}\"")
    try:
        resp = riva_tts.synthesize(
            text=text,
            voice_name="jason",
            language_code="en-US",
            sample_rate_hz=16000
        )
        if resp.audio:
            audio_np = np.frombuffer(resp.audio, dtype=np.int16)
            sf.write(TEMP_TTS_WAV, audio_np, 16000, format='WAV', subtype='PCM_16')
            if os.path.exists(PLAYER_BIN):
                subprocess.run([PLAYER_BIN, TEMP_TTS_WAV, NET_INTERFACE_NAME], check=True)
    except Exception as e:
        print(f"❌ TTS / Playback Error: {e}")

def record_multicast_audio(duration_sec=5, sample_rate=16000):
    """Captures live audio from Unitree onboard mic via UDP Multicast."""
    print(f"\n🎙️ RECORDING for {duration_sec} seconds... Speak now!")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", MCAST_PORT))
    
    mreq = socket.inet_aton(MCAST_GRP) + socket.inet_aton(NET_INTERFACE_IP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(2.0)
    
    total_bytes_needed = sample_rate * 2 * duration_sec
    audio_bytes = bytearray()
    time.sleep(0.1)
    
    while len(audio_bytes) < total_bytes_needed:
        try:
            data, _ = sock.recvfrom(4096)
            if data:
                audio_bytes.extend(data)
        except socket.timeout:
            break
            
    sock.close()
    print("🛑 Recording finished!")
    return bytes(audio_bytes[:total_bytes_needed])

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
    try:
        response = riva_asr.offline_recognize(audio_bytes, config)
        if response.results and response.results[0].alternatives:
            transcript = response.results[0].alternatives[0].transcript.strip()
            print(f"🗣️ You said: \"{transcript}\"")
            return transcript
    except Exception as e:
        print(f"❌ ASR Error: {e}")
    return ""

def capture_camera_frame():
    """Captures a snapshot from the Unitree head camera."""
    print("📸 Capturing frame from onboard camera...")
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("❌ Could not open camera /dev/video0")
        return None
    
    ret, frame = None, None
    for _ in range(5):
        ret, frame = cap.read()
    cap.release()
    
    if ret and frame is not None:
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
    
    try:
        resp = requests.post(VLLM_URL, json=payload, timeout=15)
        if resp.status_code == 200:
            answer = resp.json()["choices"][0]["message"]["content"].strip()
            print(f"💡 Gemma-4 Answer: \"{answer}\"")
            return answer
        else:
            print(f"❌ vLLM Error: {resp.text}")
    except Exception as e:
        print(f"❌ vLLM Connection Error: {e}")
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
    
    # 3. Record voice for 5 seconds from Unitree mic
    audio_bytes = record_multicast_audio(duration_sec=5, sample_rate=16000)
    
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
