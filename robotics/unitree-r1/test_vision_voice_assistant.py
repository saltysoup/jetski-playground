#!/usr/bin/env python3
"""
Unitree R1 Test 2: Full Multimodal Vision & Voice Assistant with Semantic Routing
- Microphone: Unitree UDP Multicast (239.168.123.161:5555)
- ASR: Nemotron Streaming ASR (Riva gRPC 50051)
- Semantic Router: 100% Offline MiniLM-L6-v2 ONNX Dense Router
- Camera: OpenCV (/dev/video0) - Triggered only on Visual Intent
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
import onnxruntime

# --- Server & Network Configurations ---
RIVA_URI = "127.0.0.1:50051"
VLLM_URL = os.getenv("VLLM_URL", "http://localhost:8000/v1/chat/completions")
MODEL_NAME = "google/gemma-4-E2B-it"

MCAST_GRP = "239.168.123.161"
MCAST_PORT = 5555
NET_INTERFACE_IP = "192.168.123.164"
NET_INTERFACE_NAME = "eth10"
PLAYER_BIN = "/home/unitree/unitree_sdk2/build/bin/unitree_play_wav"
TEMP_TTS_WAV = "/tmp/robot_tts.wav"

# --- 1. MiniLM ONNX Dense Semantic Router ---
class MiniLMEncoder:
    """100% Offline Fast Sentence-Transformers MiniLM-L6-v2 ONNX Embedding Engine."""
    def __init__(self, model_path, vocab_path):
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = 2
        self.session = onnxruntime.InferenceSession(model_path, sess_options=opts, providers=['CPUExecutionProvider'])
        self.vocab = {}
        with open(vocab_path, 'r', encoding='utf-8') as f:
            for idx, line in enumerate(f):
                self.vocab[line.strip()] = idx
        self.unk_id = self.vocab.get('[UNK]', 100)
        self.cls_id = self.vocab.get('[CLS]', 101)
        self.sep_id = self.vocab.get('[SEP]', 102)

    def tokenize(self, text):
        tokens = [self.cls_id]
        for word in text.lower().split():
            clean = ''.join(c for c in word if c.isalnum())
            tokens.append(self.vocab.get(clean, self.unk_id))
        tokens.append(self.sep_id)
        return tokens

    def encode(self, texts):
        token_lists = [self.tokenize(t) for t in texts]
        max_len = max(len(t) for t in token_lists)
        input_ids = np.zeros((len(texts), max_len), dtype=np.int64)
        attention_mask = np.zeros((len(texts), max_len), dtype=np.int64)
        token_type_ids = np.zeros((len(texts), max_len), dtype=np.int64)

        for i, t in enumerate(token_lists):
            input_ids[i, :len(t)] = t
            attention_mask[i, :len(t)] = 1

        outputs = self.session.run(None, {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'token_type_ids': token_type_ids
        })
        token_embeddings = outputs[0]
        input_mask_expanded = np.expand_dims(attention_mask, -1).astype(float)
        sum_embeddings = np.sum(token_embeddings * input_mask_expanded, axis=1)
        sum_mask = np.clip(input_mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        embeddings = sum_embeddings / sum_mask
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        return embeddings / norms

VISION_UTTERANCES = [
    "what can you see",
    "how many objects are there",
    "what is this",
    "what am I holding",
    "describe what you see",
    "look at this",
    "which of these is heavier",
    "what color is this",
    "can you identify this object",
    "is this heavy",
    "what is on the desk",
    "describe this product",
    "what are you looking at",
    "what is in front of you"
]

ROUTER_MODEL_PATH = "/home/unitree/robot_assets/models/onnx/model_qint8_arm64.onnx"
ROUTER_VOCAB_PATH = "/home/unitree/robot_assets/models/vocab.txt"

print(f"🧠 Initializing MiniLM Semantic Router from {ROUTER_MODEL_PATH}...")
encoder = MiniLMEncoder(ROUTER_MODEL_PATH, ROUTER_VOCAB_PATH)
vision_embeddings = encoder.encode(VISION_UTTERANCES)
print("✅ Semantic Router active!")

def calculate_vision_similarity(user_text):
    query_vector = encoder.encode([user_text])[0]
    scores = [np.dot(query_vector, ut_vector) for ut_vector in vision_embeddings]
    return max(scores)

# --- 2. Initialize Riva gRPC Client ---
print(f"🎙️ Connecting to Riva Speech Server at {RIVA_URI}...")
riva_auth = riva.client.Auth(uri=RIVA_URI)
riva_asr = riva.client.ASRService(riva_auth)
riva_tts = riva.client.SpeechSynthesisService(riva_auth)
print("✅ Riva ASR & TTS connected!")

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
    """Queries vLLM Gemma-4 with multimodal image or text prompt."""
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
                "content": "You are the Unitree R1 humanoid robot assistant. Answer directly in one concise spoken sentence under 25 words."
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
    return "I am ready to assist you."

def main():
    print("=" * 60)
    print("🤖 Unitree R1 Semantic Assistant (MiniLM Router + Nemotron + Magpie)")
    print("=" * 60)
    
    ROUTER_THRESHOLD = 0.35
    
    # 1. Robot says initial prompt
    speak_via_riva("Ask me what I am seeing, or ask any general question")
    
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
        
    # 6. Pass prompt and optional image to Gemma-4
    response_text = query_gemma4_vlm(transcript, image_b64)
    
    # 7. Speak answer through robot speakers
    speak_via_riva(response_text)

if __name__ == "__main__":
    main()
