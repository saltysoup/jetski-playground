#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unitree R1: Real-Time Multimodal Vision & Voice Assistant
- Microphone: Unitree UDP Multicast (239.168.123.161:5555) with AGC & Push-to-Talk
- ASR: Nemotron ASR (Riva gRPC 50051) - Ultra-Fast CUDA Offline Recognize (~45ms)
- Speculative Perception: Concurrent Background Camera Capture (0ms Perceived Latency)
- Semantic Router: 100% Offline MiniLM-L6-v2 Dense Intent Classifier
- Camera: Forward Head Camera (/dev/video2) exclusively
- VLM: Gemma-4 Multimodal (llama-server 8000 --flash-attn on -t 6 -ub 128 --cache-ram 0)
- TTS: Magpie TTS v2602 (Riva gRPC 50051) - Ultra Low Latency Streaming
- Audio Daemon: Persistent UNIX Socket (/tmp/unitree_audio.sock) - Gapless Playback
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
import select
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
NET_INTERFACE_IP = "192.168.123.164"
NET_INTERFACE_NAME = "eth10"
AUDIO_SOCKET = "/tmp/unitree_audio.sock"
PLAYER_BIN = "/home/unitree/unitree_sdk2/build/bin/unitree_play_wav"

ROUTER_MODEL_PATH = "/home/unitree/robot_assets/models/onnx/model_qint8_arm64.onnx"
ROUTER_VOCAB_PATH = "/home/unitree/robot_assets/models/vocab.txt"

# --- 1. MiniLM Dense Semantic Intent Router ---
class MiniLMEncoder:
    """100% Offline Fast MiniLM-L6-v2 ONNX Embedding Engine."""
    def __init__(self, model_path, vocab_path):
        self.session = None
        self.vocab = {}
        if os.path.exists(model_path) and os.path.exists(vocab_path):
            try:
                import onnxruntime
                opts = onnxruntime.SessionOptions()
                opts.intra_op_num_threads = 2
                self.session = onnxruntime.InferenceSession(model_path, sess_options=opts, providers=['CPUExecutionProvider'])
                with open(vocab_path, 'r', encoding='utf-8') as f:
                    for idx, line in enumerate(f):
                        self.vocab[line.strip()] = idx
                self.unk_id = self.vocab.get('[UNK]', 100)
                self.cls_id = self.vocab.get('[CLS]', 101)
                self.sep_id = self.vocab.get('[SEP]', 102)
                print("[ROUTER] MiniLM Dense Embedding Engine initialized.")
            except Exception as e:
                self.session = None

    def tokenize(self, text):
        tokens = [self.cls_id]
        for word in text.lower().split():
            clean = ''.join(c for c in word if c.isalnum())
            tokens.append(self.vocab.get(clean, self.unk_id))
        tokens.append(self.sep_id)
        return tokens

    def encode(self, texts):
        if not self.session:
            return None
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
    "what color is this",
    "can you identify this object",
    "is this heavy",
    "what is on the desk",
    "describe what is in front of you",
    "what are you looking at",
    "who is in front of you",
    "what am I wearing"
]

VISION_KEYWORDS = {
    "see": 1.5, "look": 1.5, "holding": 1.8, "color": 1.5, "view": 1.2,
    "front": 1.3, "desk": 1.2, "table": 1.2, "object": 1.4, "objects": 1.4,
    "image": 1.5, "picture": 1.5, "camera": 1.5, "identify": 1.4, "describe": 1.3,
    "reading": 1.3, "text": 1.0, "wearing": 1.5, "shirt": 1.5, "room": 1.0,
    "floor": 1.2, "feet": 1.2, "hand": 1.4, "person": 1.5, "who": 1.2
}

VISION_PHRASES = [
    "what is this", "what do you see", "what can you see", "what am i holding",
    "describe what you see", "look at this", "what color is", "what is on the",
    "in front of you", "identify this", "how many objects", "tell me what you see",
    "who am i", "what am i wearing"
]

encoder = MiniLMEncoder(ROUTER_MODEL_PATH, ROUTER_VOCAB_PATH)
dense_vision_embeddings = encoder.encode(VISION_UTTERANCES) if encoder.session else None

def calculate_vision_similarity(text):
    """Calculates semantic visual intent score using MiniLM Dense Embeddings or Fast Semantic Matcher."""
    clean_text = text.lower().strip()
    
    # 1. MiniLM Dense Vector Embedding Similarity (if ONNX engine active)
    if encoder.session and dense_vision_embeddings is not None:
        try:
            query_vector = encoder.encode([clean_text])[0]
            scores = [np.dot(query_vector, ut_vector) for ut_vector in dense_vision_embeddings]
            return float(max(scores))
        except Exception:
            pass
            
    # 2. Fast Semantic Fallback
    for phrase in VISION_PHRASES:
        if phrase in clean_text:
            return 0.85
            
    words = [w.strip("?,.!") for w in clean_text.split()]
    score = 0.0
    for w in words:
        if w in VISION_KEYWORDS:
            score += VISION_KEYWORDS[w]
            
    return min(1.0, score / 2.0)

# --- 2. Initialize Riva gRPC Client ---
print("[RIVA] Connecting to Riva Speech Server at %s..." % RIVA_URI)
riva_auth = riva.client.Auth(uri=RIVA_URI)
riva_asr = riva.client.ASRService(riva_auth)
riva_tts = riva.client.SpeechSynthesisService(riva_auth)
print("[OK] Riva ASR & TTS connected!")

# --- 3. Zero-Delay Multi-Threaded Pipelined TTS Architecture ---
synthesis_queue = queue.Queue()
playback_queue = queue.Queue()

def tts_synthesizer_worker():
    """Background worker that continuously synthesizes queued text chunks into audio buffers."""
    while True:
        text_chunk = synthesis_queue.get()
        if text_chunk is None:
            playback_queue.put(None)
            synthesis_queue.task_done()
            break
        try:
            clean_text = text_chunk.strip()
            if clean_text and len(clean_text) >= 2:
                resp = riva_tts.synthesize(
                    text=clean_text,
                    voice_name="jason",
                    language_code="en-US",
                    sample_rate_hz=16000
                )
                if resp.audio:
                    audio_np = np.frombuffer(resp.audio, dtype=np.int16)
                    # Software Peak Normalization / Gain Boost
                    max_val = np.max(np.abs(audio_np))
                    if max_val > 0:
                        gain = min(3.5, 30000.0 / float(max_val))
                        audio_np = np.clip(audio_np * gain, -32767, 32767).astype(np.int16)
                    playback_queue.put(audio_np)
        except Exception as e:
            print("\n[ERROR] Synthesis worker error for '%s': %s" % (text_chunk, e))
        finally:
            synthesis_queue.task_done()

def audio_playback_worker():
    """Streams audio buffers through persistent UNIX domain socket directly to the audio daemon in <1ms."""
    while True:
        audio_np = playback_queue.get()
        if audio_np is None:
            playback_queue.task_done()
            break
        try:
            raw_pcm = audio_np.tobytes()
            # 1. Fast Path: Persistent UNIX Domain Socket to Audio Daemon (<1ms)
            if os.path.exists(AUDIO_SOCKET):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(AUDIO_SOCKET)
                sock.sendall(raw_pcm)
                sock.close()
            elif os.path.exists(PLAYER_BIN):
                # 2. Fallback Path: Subprocess CLI player
                temp_wav = "/tmp/tts_chunk_%d.wav" % int(time.time() * 1000 % 100)
                sf.write(temp_wav, audio_np, 16000, format='WAV', subtype='PCM_16')
                subprocess.run([PLAYER_BIN, temp_wav, NET_INTERFACE_NAME], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("\n[ERROR] Playback worker error: %s" % e)
        finally:
            playback_queue.task_done()

threading.Thread(target=tts_synthesizer_worker, daemon=True).start()
threading.Thread(target=audio_playback_worker, daemon=True).start()

def queue_text_for_streaming_tts(text_chunk):
    """Enqueues a text chunk to immediately begin TTS synthesis in background."""
    synthesis_queue.put(text_chunk)

def wait_for_all_tts_to_finish():
    """Waits until all queued synthesis and audio playbacks are complete."""
    synthesis_queue.join()
    playback_queue.join()

def speak_direct_via_riva(text_to_speak):
    """Synchronous speech for standalone announcements."""
    queue_text_for_streaming_tts(text_to_speak)
    wait_for_all_tts_to_finish()

# --- 4. Speculative Zero-Latency Camera Capture System ---
speculative_image_b64 = None
speculative_cam_thread = None

def capture_camera_frame():
    """Captures a fresh live snapshot exclusively from the forward-facing head camera (/dev/video2)."""
    import cv2
    cap = cv2.VideoCapture(2, cv2.CAP_V4L2)
    if not cap.isOpened():
        print("[ERROR] Could not open forward head camera /dev/video2")
        return None
    
    # Flush 15 frames to discard any stale cached buffer
    ret, frame = None, None
    for _ in range(15):
        ret, frame = cap.read()
    cap.release()
    
    if ret and frame is not None:
        cv2.imwrite("/home/unitree/last_camera_snap.jpg", frame)
        small = cv2.resize(frame, (384, 384), interpolation=cv2.INTER_AREA)
        _, buffer = cv2.imencode('.jpg', small, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return base64.b64encode(buffer).decode('utf-8')
    return None

def trigger_speculative_camera_capture():
    """Starts capturing the forward camera in a concurrent background thread while the user speaks."""
    global speculative_image_b64, speculative_cam_thread
    speculative_image_b64 = None
    
    def worker():
        global speculative_image_b64
        speculative_image_b64 = capture_camera_frame()
        
    speculative_cam_thread = threading.Thread(target=worker)
    speculative_cam_thread.daemon = True
    speculative_cam_thread.start()

def get_speculative_camera_frame():
    """Returns the pre-captured camera frame immediately (0ms perceived latency)."""
    global speculative_image_b64, speculative_cam_thread
    if speculative_cam_thread and speculative_cam_thread.is_alive():
        speculative_cam_thread.join(timeout=1.0)
    return speculative_image_b64

# --- 5. Robust Audio Capture & Instant ASR ---
def record_push_to_talk():
    """Captures live audio from Unitree multicast socket with push-to-talk, AGC, and speculative camera snapping."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", MCAST_PORT))
    
    # Bind to eth10 multicast
    mreq = socket.inet_aton(MCAST_GRP) + socket.inet_aton(NET_INTERFACE_IP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    
    # Flush stale packets from buffer
    try:
        while True:
            sock.recv(4096)
    except Exception:
        pass
        
    # Trigger speculative camera capture concurrently in background
    trigger_speculative_camera_capture()
    
    print("\n[MIC] 🎙️ RECORDING... Speak into the robot microphone now.")
    print("[TIP] Press [ENTER] when you are done speaking.")
    sys.stdout.flush()
    
    audio_chunks = []
    
    # Non-blocking stdin monitoring while capturing UDP packets
    while True:
        # Check if ENTER was pressed
        r_stdin, _, _ = select.select([sys.stdin], [], [], 0.01)
        if sys.stdin in r_stdin:
            sys.stdin.readline()
            break
            
        # Read available UDP multicast packets
        r_sock, _, _ = select.select([sock], [], [], 0.03)
        if sock in r_sock:
            try:
                data = sock.recv(4096)
                if data:
                    audio_chunks.append(data)
            except Exception:
                pass
                
    sock.close()
    print("[MIC] Recording finished! Transcribing...")
    
    if not audio_chunks:
        return None
        
    raw_bytes = b"".join(audio_chunks)
    audio_np = np.frombuffer(raw_bytes, dtype=np.int16)
    
    # Apply Automatic Gain Control (AGC) on microphone capture
    peak = np.max(np.abs(audio_np))
    if peak > 100:
        boost = min(8.0, 24000.0 / float(peak))
        audio_np = np.clip(audio_np * boost, -32767, 32767).astype(np.int16)
        
    return audio_np.tobytes()

def transcribe_audio_bytes(audio_bytes):
    """Transcribes audio using NeMo Streaming ASR via Riva gRPC (~45ms latency)."""
    if not audio_bytes:
        return ""
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
            print("[ASR] ⚡ You said: \"%s\"" % transcript)
            return transcript
    except Exception as e:
        print("[ERROR] ASR Error: %s" % e)
    return ""

def query_gemma4_and_stream_tts(user_text, image_b64=None):
    """Streams tokens from Gemma-4 and dispatches early sentence chunks to parallel TTS pipeline."""
    if image_b64:
        print("[GEMMA] Sending Multimodal Query (Image + Text)...")
    else:
        print("[GEMMA] Sending Fast Text-Only Query...")
        
    content = []
    if image_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,%s" % image_b64}
        })
    content.append({"type": "text", "text": user_text})
    
    system_prompt = "Your name is Jason. Don't use acronyms. For time or numbers spell them out in letters. Response needs to be under 42 words."
    
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content}
        ],
        "max_tokens": 80,
        "stream": True
    }
    
    try:
        resp = requests.post(VLLM_URL, json=payload, stream=True, timeout=15)
        if resp.status_code != 200:
            print("[ERROR] Server Error: %s" % resp.text)
            speak_direct_via_riva("I am ready to assist you.")
            return
            
        full_text = ""
        current_sentence = ""
        first_chunk_sent = False
        print("[ROBOT] Jason: ", end="", flush=True)
        
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
                            full_text += text_chunk
                            
                            words = current_sentence.strip().split()
                            # 1. First-Chunk Trigger: 5+ words or natural punctuation gives ~1.5s speech buffer for seamless playback!
                            if not first_chunk_sent and (len(words) >= 5 or any(p in text_chunk for p in [",", ";", ".", "!", "?"])):
                                queue_text_for_streaming_tts(current_sentence.strip())
                                current_sentence = ""
                                first_chunk_sent = True
                            # 2. Subsequent Sentence Trigger
                            elif first_chunk_sent and any(p in text_chunk for p in [".", "?", "!", "\n", ","]) and len(words) >= 4:
                                queue_text_for_streaming_tts(current_sentence.strip())
                                current_sentence = ""
                    except Exception:
                        continue
                        
        print()
        # Enqueue any remaining words
        if current_sentence.strip():
            queue_text_for_streaming_tts(current_sentence.strip())
        elif not full_text.strip():
            queue_text_for_streaming_tts("I see what is in front of me.")
            
        # Wait for pipelined audio playback to complete cleanly
        wait_for_all_tts_to_finish()
            
    except Exception as e:
        print("[ERROR] Connection Error: %s" % e)
        speak_direct_via_riva("I encountered an error connecting to my intelligence engine.")

def main():
    print("=" * 60)
    print("[SYSTEM] Unitree R1 Multimodal Assistant")
    print("[AUDIO] Persistent Zero-Delay Audio Daemon (/tmp/unitree_audio.sock)")
    print("=" * 60)
    
    ROUTER_THRESHOLD = 0.35
    
    # 1. Robot greeting
    startup_greeting = "My name is Jason. I like long walks on the beach and listening to domo arigato mr roboto."
    speak_direct_via_riva(startup_greeting)
    
    while True:
        print("\n" + "-" * 50)
        print("[PROMPT] Press [ENTER] to start speaking (or type 'q' to quit):")
        sys.stdout.flush()
        
        choice = sys.stdin.readline().strip().lower()
        if choice == 'q' or choice == 'exit':
            print("[EXIT] Exiting assistant.")
            break
            
        # 2. Push to talk audio capture with AGC + Concurrent Background Camera Snapping
        audio_bytes = record_push_to_talk()
        
        # 3. Transcribe voice (~45ms CUDA ASR)
        transcript = transcribe_audio_bytes(audio_bytes)
        if not transcript:
            print("[WARN] No speech detected, try speaking closer to the mic.")
            continue
            
        # 4. MiniLM Dense Semantic Routing: Determine visual intent
        similarity_score = calculate_vision_similarity(transcript)
        has_visual_intent = (similarity_score >= ROUTER_THRESHOLD)
        
        image_b64 = None
        if has_visual_intent:
            print("[ROUTE] 🎯 Vision Route Triggered (MiniLM Score: %.2f >= %.2f)!" % (similarity_score, ROUTER_THRESHOLD))
            # Retrieve pre-captured speculative image in 0ms!
            image_b64 = get_speculative_camera_frame()
            if image_b64:
                print("[ROUTE] ⚡ Using pre-captured speculative frame from /dev/video2 (0ms latency).")
            else:
                print("[WARN] Speculative frame not ready, capturing on demand...")
                image_b64 = capture_camera_frame()
        else:
            print("[ROUTE] 💬 Text-only Route (MiniLM Score: %.2f < %.2f) - Discarding camera frame." % (similarity_score, ROUTER_THRESHOLD))
            image_b64 = None
            
        # 5. Stream tokens from Gemma-4 & speak via Magpie TTS
        query_gemma4_and_stream_tts(transcript, image_b64)

if __name__ == "__main__":
    main()
