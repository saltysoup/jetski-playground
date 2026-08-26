#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unitree R1: Low-Latency Multimodal Vision & Voice Assistant
- Microphone: Unitree UDP Multicast (239.168.123.161:5555)
- ASR: Nemotron ASR (Riva gRPC 50051)
- Semantic Router: 100% Offline Fast Semantic Intent Router
- Camera: OpenCV (/dev/video0) - Triggered dynamically on Visual Intent
- VLM: Gemma-4 Multimodal (llama-server 8000)
- TTS: Magpie TTS v2602 (Riva gRPC 50051) - Sentence-Pipelined Low-Latency Streaming
- Speakers: Unitree AudioClient DDS Player (unitree_play_wav)
"""

import os
import sys
import time
import json
import queue
import base64
import socket
import struct
import warnings
import threading
import subprocess

warnings.filterwarnings("ignore")

import requests
import soundfile as sf
import numpy as np
import riva.client

# --- Server & Network Configurations ---
RIVA_URI = "127.0.0.1:50051"
VLLM_URL = os.getenv("VLLM_URL", "http://127.0.0.1:8000/v1/chat/completions")
MODEL_NAME = "gemma"

MCAST_GRP = "239.168.123.161"
MCAST_PORT = 5555
NET_INTERFACE_NAME = "eth10"
PLAYER_BIN = "/home/unitree/unitree_sdk2/build/bin/unitree_play_wav"

# --- Audio Playback Background Worker ---
tts_audio_queue = queue.Queue()
temp_wav_counter = 0

def audio_playback_worker():
    """Background worker that pulls synthesized TTS audio buffers and plays them through robot speakers."""
    global temp_wav_counter
    while True:
        audio_np = tts_audio_queue.get()
        if audio_np is None:
            break
        try:
            temp_wav = f"/tmp/tts_chunk_{temp_wav_counter % 10}.wav"
            temp_wav_counter += 1
            sf.write(temp_wav, audio_np, 16000, format='WAV', subtype='PCM_16')
            if os.path.exists(PLAYER_BIN):
                subprocess.run([PLAYER_BIN, temp_wav, NET_INTERFACE_NAME], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"❌ Playback error: {e}")
        finally:
            tts_audio_queue.task_done()

playback_thread = threading.Thread(target=audio_playback_worker, daemon=True)
playback_thread.start()

# --- 1. Pure-Python Fast N-Gram Semantic Intent Router ---
VISION_KEYWORDS = {
    "see": 1.5, "look": 1.5, "holding": 1.8, "color": 1.5, "view": 1.2,
    "front": 1.3, "desk": 1.2, "table": 1.2, "object": 1.4, "objects": 1.4,
    "image": 1.5, "picture": 1.5, "camera": 1.5, "identify": 1.4, "describe": 1.3,
    "reading": 1.3, "text": 1.0, "wearing": 1.5, "shirt": 1.5, "room": 1.0
}

VISION_PHRASES = [
    "what is this", "what do you see", "what can you see", "what am i holding",
    "describe what you see", "look at this", "what color is", "what is on the",
    "in front of you", "identify this", "how many objects", "tell me what you see"
]

def calculate_vision_similarity(text):
    """Calculates semantic visual intent score between 0.0 and 1.0."""
    clean_text = text.lower().strip()
    
    # 1. Exact phrase matching
    for phrase in VISION_PHRASES:
        if phrase in clean_text:
            return 0.85
            
    # 2. Weighted keyword matching
    words = [w.strip("?,.!") for w in clean_text.split()]
    score = 0.0
    for w in words:
        if w in VISION_KEYWORDS:
            score += VISION_KEYWORDS[w]
            
    normalized_score = min(1.0, score / 2.0)
    return normalized_score

# --- 2. Initialize Riva gRPC Client ---
print(f"🎙️ Connecting to Riva Speech Server at {RIVA_URI}...")
riva_auth = riva.client.Auth(uri=RIVA_URI)
riva_asr = riva.client.ASRService(riva_auth)
riva_tts = riva.client.SpeechSynthesisService(riva_auth)
print("✅ Riva ASR & TTS connected!")

def synthesize_and_queue_sentence(sentence_text):
    """Synthesizes a single sentence chunk via NeMo TTS and queues for immediate playback."""
    clean_text = sentence_text.strip()
    if not clean_text or len(clean_text) < 2:
        return
    try:
        resp = riva_tts.synthesize(
            text=clean_text,
            voice_name="jason",
            language_code="en-US",
            sample_rate_hz=16000
        )
        if resp.audio:
            audio_np = np.frombuffer(resp.audio, dtype=np.int16)
            tts_audio_queue.put(audio_np)
    except Exception as e:
        print(f"\n❌ TTS Error for '{clean_text}': {e}")

def speak_direct_via_riva(text):
    """Fallback synchronous speech."""
    synthesize_and_queue_sentence(text)
    tts_audio_queue.join()

def record_multicast_audio(duration_sec=4, sample_rate=16000):
    """Captures raw 16kHz 16-bit Mono PCM audio directly from Unitree multicast socket."""
    print(f"🎙️ Listening to Unitree microphone ({MCAST_GRP}:{MCAST_PORT}) for {duration_sec}s...")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass

    sock.bind(('', MCAST_PORT))
    mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(1.0)
    
    audio_bytes = bytearray()
    total_bytes_needed = int(duration_sec * sample_rate * 2)
    start_time = time.time()
    
    while len(audio_bytes) < total_bytes_needed and (time.time() - start_time) < (duration_sec + 1.0):
        try:
            data, _ = sock.recvfrom(2048)
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
    import cv2
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

def query_gemma4_and_stream_tts(user_text, image_b64=None):
    """Streams tokens from Gemma-4, chunks into sentences, and immediately dispatches each sentence to Magpie TTS."""
    print("🧠 Querying Gemma-4 VLM (Sentence-Pipelined Streaming)...")
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
                "content": "You are the Unitree R1 humanoid robot assistant. Answer directly in one concise spoken sentence under 25 words."
            },
            {"role": "user", "content": content}
        ],
        "max_tokens": 80,
        "stream": True
    }
    
    try:
        resp = requests.post(VLLM_URL, json=payload, stream=True, timeout=15)
        if resp.status_code != 200:
            print(f"❌ Server Error: {resp.text}")
            speak_direct_via_riva("I am ready to assist you.")
            return
            
        current_sentence = ""
        print("🤖 Gemma-4: ", end="", flush=True)
        
        for line in resp.iter_lines():
            if line:
                line_str = line.decode("utf-8").strip()
                if line_str.startswith("data: "):
                    data_str = line_str[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk_json = json.loads(data_str)
                        delta = chunk_json["choices"][0].get("delta", {})
                        text_chunk = delta.get("content", "")
                        if text_chunk:
                            print(text_chunk, end="", flush=True)
                            current_sentence += text_chunk
                            
                            # As soon as sentence punctuation arrives, start TTS on a background thread!
                            if any(p in text_chunk for p in [".", "?", "!", "\n"]):
                                sentence_to_speak = current_sentence.strip()
                                if len(sentence_to_speak) > 8:
                                    threading.Thread(
                                        target=synthesize_and_queue_sentence,
                                        args=(sentence_to_speak,),
                                        daemon=True
                                    ).start()
                                    current_sentence = ""
                    except Exception:
                        continue
                        
        print()
        # Flush any remaining text in buffer
        if current_sentence.strip():
            threading.Thread(
                target=synthesize_and_queue_sentence,
                args=(current_sentence.strip(),),
                daemon=True
            ).start()
            
        # Wait for all sentence audio playback chunks to complete
        tts_audio_queue.join()
        
    except Exception as e:
        print(f"❌ Connection Error: {e}")
        speak_direct_via_riva("I encountered an error connecting to my intelligence engine.")

def main():
    print("=" * 60)
    print("🤖 Unitree R1 Streaming Assistant (Semantic Router + Nemotron + Magpie)")
    print("=" * 60)
    
    ROUTER_THRESHOLD = 0.35
    
    # 1. Robot says initial prompt
    speak_direct_via_riva("Ask me what I am seeing, or ask any general question")
    
    # 2. Wait 1 second
    print("⏳ Waiting 1 second...")
    time.sleep(1.0)
    
    # 3. Record voice for 4 seconds from Unitree mic
    audio_bytes = record_multicast_audio(duration_sec=4, sample_rate=16000)
    
    # 4. Transcribe voice
    transcript = transcribe_audio_bytes(audio_bytes)
    if not transcript:
        transcript = "What do you see?"
        print(f"⚠️ Defaulting prompt to: \"{transcript}\"")
        
    # 5. Semantic Routing: Determine if visual intent is present
    similarity_score = calculate_vision_similarity(transcript)
    has_visual_intent = (similarity_score >= ROUTER_THRESHOLD)
    
    image_b64 = None
    if has_visual_intent:
        print(f"🎯 Match: Vision Route triggered (Score: {similarity_score:.2f} >= {ROUTER_THRESHOLD})!")
        image_b64 = capture_camera_frame()
    else:
        print(f"💬 Text-only Route (Score: {similarity_score:.2f} < {ROUTER_THRESHOLD}) - Skipping camera capture.")
        image_b64 = None
        
    # 6. Stream tokens from Gemma-4 & pipelined streaming to Magpie TTS
    query_gemma4_and_stream_tts(transcript, image_b64)
    
    # Clean exit
    os._exit(0)

if __name__ == "__main__":
    main()
