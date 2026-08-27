#!/usr/bin/env python3
"""
==============================================================================
Unitree R1: Real-Time Multimodal Voice & Vision AI Assistant (Gemma-4 + Nemotron)
Pipelined Architecture: Multicast Mic -> Riva ASR -> MiniLM -> Camera -> Gemma-4 -> Magpie TTS -> Audio Daemon
==============================================================================
"""

import sys
import os
import time
import socket
import struct
import base64
import requests
import json
import threading
import queue
import re
import numpy as np
import soundfile as sf
import riva.client

# --- Server & Network Configurations ---
RIVA_URI = "127.0.0.1:50051"
VLLM_URL = os.getenv("VLLM_URL", "http://127.0.0.1:8000/v1/chat/completions")
MODEL_NAME = "gemma"

MCAST_GRP = "239.168.123.161"
MCAST_PORT = 5555
NET_INTERFACE_IP = "192.168.123.164"
NET_INTERFACE_NAME = "eth10"
CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "head")  # "head" (DDS eyes), "wrist0" (/dev/video0), "wrist2" (/dev/video2)
HEAD_CAMERA_SOCK = "/tmp/unitree_head_camera.sock"
HEAD_CAMERA_SNAP = "/tmp/unitree_head_camera.jpg"
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
                opts.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
                self.session = onnxruntime.InferenceSession(model_path, opts, providers=['CPUExecutionProvider'])
                with open(vocab_path, 'r', encoding='utf-8') as f:
                    for idx, line in enumerate(f):
                        self.vocab[line.strip()] = idx
                print("[ROUTER] 🧠 MiniLM-L6-v2 Semantic Router loaded successfully.")
            except Exception as e:
                print("[WARN] MiniLM ONNX init failed (%s), using fast keyword matcher." % e)
                self.session = None

    def tokenize(self, text, max_len=64):
        tokens = ["[CLS]"] + re.findall(r'\w+|[^\w\s]', text.lower())[:max_len-2] + ["[SEP]"]
        input_ids = [self.vocab.get(t, self.vocab.get("[UNK]", 100)) for t in tokens]
        attention_mask = [1] * len(input_ids)
        token_type_ids = [0] * len(input_ids)
        
        padding_len = max_len - len(input_ids)
        input_ids += [0] * padding_len
        attention_mask += [0] * padding_len
        token_type_ids += [0] * padding_len
        
        return {
            'input_ids': np.array([input_ids], dtype=np.int64),
            'attention_mask': np.array([attention_mask], dtype=np.int64),
            'token_type_ids': np.array([token_type_ids], dtype=np.int64)
        }

    def encode(self, text):
        if not self.session:
            return None
        try:
            inputs = self.tokenize(text)
            outputs = self.session.run(None, inputs)
            token_embeddings = outputs[0]
            input_mask_expanded = np.expand_dims(inputs['attention_mask'], -1).astype(float)
            sum_embeddings = np.sum(token_embeddings * input_mask_expanded, axis=1)
            sum_mask = np.clip(input_mask_expanded.sum(axis=1), 1e-9, None)
            mean_pooled = sum_embeddings / sum_mask
            norm = np.linalg.norm(mean_pooled, axis=1, keepdims=True)
            norm = np.clip(norm, 1e-9, None)
            return (mean_pooled / norm)[0]
        except Exception:
            return None

router_encoder = MiniLMEncoder(ROUTER_MODEL_PATH, ROUTER_VOCAB_PATH)

VISUAL_ANCHORS = [
    "what do you see in front of you",
    "describe what is in front of you",
    "tell me what you see right now",
    "look around and describe your environment",
    "can you see what i am holding in my hand",
    "read the text on this paper in front of you",
    "what color is the shirt i am wearing",
    "what is on the table in front of you",
    "inspect this object and tell me what it is",
    "who is standing in front of you"
]

ANCHOR_EMBEDDINGS = []
if router_encoder.session:
    for anchor in VISUAL_ANCHORS:
        emb = router_encoder.encode(anchor)
        if emb is not None:
            ANCHOR_EMBEDDINGS.append(emb)

VISION_KEYWORDS = {
    "see": 1.0, "look": 1.0, "camera": 1.2, "view": 0.8, "watch": 0.8,
    "front": 0.6, "holding": 0.9, "wearing": 0.9, "color": 0.8, "table": 0.5,
    "desk": 0.5, "object": 0.7, "person": 0.6, "image": 0.9, "picture": 0.9,
    "visual": 1.0, "read": 0.8, "text": 0.7, "appearance": 0.9
}

def calculate_vision_similarity(user_text):
    """Calculates cosine similarity to visual intent anchors with keyword fallback."""
    if ANCHOR_EMBEDDINGS:
        q_emb = router_encoder.encode(user_text)
        if q_emb is not None:
            sims = [float(np.dot(q_emb, a_emb)) for a_emb in ANCHOR_EMBEDDINGS]
            return max(sims)
            
    words = re.findall(r'\w+', user_text.lower())
    score = 0.0
    for w in words:
        if w in VISION_KEYWORDS:
            score += VISION_KEYWORDS[w]
            
    return min(1.0, score / 2.0)

# --- 2. Initialize Riva gRPC Client ---
print("[RIVA] Connecting to Riva Speech Server at %s..." % RIVA_URI)
riva_auth = None
riva_asr = None
riva_tts = None
for attempt in range(1, 31):
    try:
        riva_auth = riva.client.Auth(uri=RIVA_URI)
        riva_asr = riva.client.ASRService(riva_auth)
        riva_tts = riva.client.SpeechSynthesisService(riva_auth)
        # Test basic synthesis call to verify server is listening and ready
        _ = riva_tts.synthesize(text="ready", voice_name="jason", language_code="en-US", sample_rate_hz=16000)
        print("[OK] Riva ASR & TTS connected and warmed up!")
        break
    except Exception:
        if attempt % 3 == 0:
            print("[RIVA] Waiting for Riva server... (%d/30)" % attempt)
        time.sleep(1)

# --- 3. Natural Sentence-Level Multi-Threaded Pipelined TTS ---
synthesis_queue = queue.Queue()
playback_queue = queue.Queue()

def trim_silence_padding(audio_np, threshold=400):
    """Trims dead-air silence from the beginning and end of synthesized chunks so sentences connect smoothly."""
    abs_audio = np.abs(audio_np)
    non_silent = np.where(abs_audio > threshold)[0]
    if len(non_silent) > 0:
        # Keep 10ms pad at edges to prevent clipping
        start_idx = max(0, non_silent[0] - 160)
        end_idx = min(len(audio_np), non_silent[-1] + 160)
        return audio_np[start_idx:end_idx]
    return audio_np

def tts_synthesizer_worker():
    """Background worker that synthesizes natural, complete sentence chunks into audio buffers."""
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
                    
                    # Trim leading and trailing dead air for seamless phrase joining
                    audio_np = trim_silence_padding(audio_np)
                    if len(audio_np) > 0:
                        playback_queue.put(audio_np)
        except Exception as e:
            print("\n[ERROR] Synthesis worker error for '%s': %s" % (text_chunk, e))
        finally:
            synthesis_queue.task_done()

def audio_playback_worker():
    """Streams audio buffers through non-blocking UNIX domain socket directly to the audio daemon in <1ms."""
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
    """Enqueues a natural text chunk to immediately begin TTS synthesis in background."""
    synthesis_queue.put(text_chunk)

def wait_for_all_tts_to_finish():
    """Waits until all queued synthesis and audio playbacks are complete."""
    synthesis_queue.join()
    playback_queue.join()

def speak_direct_via_riva(text_to_speak):
    """Synchronous speech for standalone announcements."""
    queue_text_for_streaming_tts(text_to_speak)
    wait_for_all_tts_to_finish()

# --- 4. Live Camera Subsystem (Head Eyes DDS + Wrist UVC) ---
def capture_head_camera_frame():
    """Captures ultra-fast live frame from Head Eyes Camera daemon (<1ms via UNIX socket)."""
    import cv2
    raw_jpeg = None
    
    # 1. Fast Path: Read via UNIX socket from daemon
    if os.path.exists(HEAD_CAMERA_SOCK):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.5)
            sock.connect(HEAD_CAMERA_SOCK)
            size_data = sock.recv(4)
            if len(size_data) == 4:
                size = struct.unpack("I", size_data)[0]
                if size > 0:
                    chunks = []
                    bytes_recv = 0
                    while bytes_recv < size:
                        chunk = sock.recv(min(size - bytes_recv, 65536))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        bytes_recv += len(chunk)
                    if bytes_recv == size:
                        raw_jpeg = b"".join(chunks)
            sock.close()
        except Exception:
            pass

    # 2. Secondary Fast Path: Read from tmpfs
    if raw_jpeg is None and os.path.exists(HEAD_CAMERA_SNAP):
        try:
            with open(HEAD_CAMERA_SNAP, "rb") as f:
                raw_jpeg = f.read()
        except Exception:
            pass

    if raw_jpeg:
        np_arr = np.frombuffer(raw_jpeg, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is not None:
            cv2.imwrite("/home/unitree/last_camera_snap.jpg", frame)
            mean_brightness = float(np.mean(frame))
            print("[CAMERA] 👁️ Captured Head Eye DDS frame (Size: %dx%d, Brightness: %.1f)" % (frame.shape[1], frame.shape[0], mean_brightness))
            small = cv2.resize(frame, (256, 256), interpolation=cv2.INTER_AREA)
            _, buffer = cv2.imencode('.jpg', small, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            return base64.b64encode(buffer).decode('utf-8')
            
    print("[ERROR] Could not capture frame from Head Eye Camera")
    return None

class LiveCameraStream:
    """Maintains a persistent V4L2 background reader thread for wrist cameras."""
    def __init__(self, device_idx=0):
        self.device_idx = device_idx
        self.cap = None
        self.last_frame = None
        self.lock = threading.Lock()
        self.running = False
        self._init_camera()

    def _init_camera(self):
        try:
            import cv2
            self.cap = cv2.VideoCapture(self.device_idx, cv2.CAP_V4L2)
            if self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.running = True
                self.worker_thread = threading.Thread(target=self._reader_loop, daemon=True)
                self.worker_thread.start()
                print("[CAMERA] 📷 Live wrist camera stream started on /dev/video%d" % self.device_idx)
            else:
                print("[ERROR] Could not open wrist camera /dev/video%d" % self.device_idx)
        except Exception as e:
            print("[ERROR] Wrist camera init error: %s" % e)

    def _reader_loop(self):
        while self.running:
            if self.cap and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret and frame is not None:
                    with self.lock:
                        self.last_frame = frame
            time.sleep(0.01)

    def capture_frame_b64(self):
        import cv2
        frame = None
        with self.lock:
            if self.last_frame is not None:
                frame = self.last_frame.copy()
                
        if frame is None and self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            
        if frame is not None:
            cv2.imwrite("/home/unitree/last_camera_snap.jpg", frame)
            mean_brightness = float(np.mean(frame))
            print("[CAMERA] ✋ Captured Wrist /dev/video%d frame (Size: %dx%d, Brightness: %.1f)" % (self.device_idx, frame.shape[1], frame.shape[0], mean_brightness))
            small = cv2.resize(frame, (256, 256), interpolation=cv2.INTER_AREA)
            _, buffer = cv2.imencode('.jpg', small, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            return base64.b64encode(buffer).decode('utf-8')
        print("[ERROR] Failed to capture frame from wrist camera /dev/video%d" % self.device_idx)
        return None

wrist_stream = None
if CAMERA_SOURCE in ["wrist0", "wrist2", "0", "2"]:
    dev_idx = 2 if CAMERA_SOURCE in ["wrist2", "2"] else 0
    wrist_stream = LiveCameraStream(dev_idx)

def capture_camera_frame():
    if CAMERA_SOURCE == "head":
        return capture_head_camera_frame()
    elif wrist_stream:
        return wrist_stream.capture_frame_b64()
    else:
        return capture_head_camera_frame()

# --- 5. Robust Audio Capture & Instant ASR ---
def record_push_to_talk():
    """Captures live audio from Unitree multicast socket with push-to-talk and AGC."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((MCAST_GRP, MCAST_PORT))
    
    mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(NET_INTERFACE_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(0.1)
    
    print("\n[TALK] 🎙️ LISTENING... Speak now! (Press [ENTER] when finished speaking)")
    
    audio_frames = []
    stop_recording = False
    
    def listen_for_enter():
        nonlocal stop_recording
        sys.stdin.readline()
        stop_recording = True
        
    input_thread = threading.Thread(target=listen_for_enter, daemon=True)
    input_thread.start()
    
    # Drain old buffered packets for 50ms
    drain_end = time.time() + 0.05
    while time.time() < drain_end:
        try:
            sock.recv(4096)
        except socket.timeout:
            break
            
    while not stop_recording:
        try:
            data = sock.recv(4096)
            if data:
                audio_frames.append(data)
        except socket.timeout:
            continue
            
    sock.close()
    
    raw_audio = b"".join(audio_frames)
    if not raw_audio:
        return None
        
    audio_np = np.frombuffer(raw_audio, dtype=np.int16)
    
    # Software Automatic Gain Control (AGC)
    peak = np.max(np.abs(audio_np))
    if peak > 0:
        target_peak = 24000.0
        gain = min(5.0, target_peak / float(peak))
        audio_np = np.clip(audio_np * gain, -32767, 32767).astype(np.int16)
        
    return audio_np.tobytes()

def transcribe_audio_bytes(audio_bytes):
    """Sends 16kHz audio directly to Riva ASR engine (~45ms CUDA latency)."""
    if not audio_bytes or len(audio_bytes) < 3200:
        return ""
        
    config = riva.client.RecognitionConfig(
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hertz=16000,
        language_code="en-US",
        max_alternatives=1,
        enable_automatic_punctuation=True
    )
    
    try:
        response = riva_asr.offline_recognize(audio_bytes, config)
        if response.results and len(response.results) > 0:
            transcript = response.results[0].alternatives[0].transcript.strip()
            print("[ASR] ⚡ You said: \"%s\"" % transcript)
            return transcript
    except Exception as e:
        print("[ERROR] ASR Error: %s" % e)
    return ""

# --- 6. Natural Continuous Flow Token Streaming ---
def query_gemma4_and_stream_tts(user_text, image_b64=None):
    """Streams tokens from Gemma-4 and dispatches complete, natural sentence chunks to parallel TTS pipeline."""
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
    
    system_prompt = "Your name is Jason. Don't use acronyms. You are a robot. For time or numbers spell them out in letters. Speak in smooth, complete sentences. Response must be under 35 words."
    
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content}
        ],
        "max_tokens": 50,
        "temperature": 0.1,
        "stream": True
    }
    
    try:
        resp = requests.post(VLLM_URL, json=payload, stream=True, timeout=20)
        if resp.status_code != 200:
            print("[ERROR] Server Error: %s" % resp.text)
            speak_direct_via_riva("I am ready to assist you.")
            return
            
        full_text = ""
        current_sentence = ""
        print("[ROBOT] Jason: ", end="", flush=True)
        
        # We split speech ONLY at natural clause and sentence boundaries for human-like prosody
        SENTENCE_ENDINGS = set([".", "!", "?", "\n"])
        CLAUSE_SEPARATORS = set([",", ";", ":", "—"])
        
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
                            # 1. Major sentence boundary: emit immediately
                            has_sentence_end = any(p in text_chunk for p in SENTENCE_ENDINGS)
                            # 2. Major clause comma boundary: emit only if at least 7 words accumulated
                            has_clause_break = any(p in text_chunk for p in CLAUSE_SEPARATORS) and len(words) >= 7
                            
                            if has_sentence_end or has_clause_break:
                                sentence_to_speak = current_sentence.strip()
                                if sentence_to_speak:
                                    queue_text_for_streaming_tts(sentence_to_speak)
                                    current_sentence = ""
                    except Exception:
                        continue
                        
        print()
        # Enqueue any remaining full phrase
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
    print("[SYSTEM] Unitree R1 Multimodal Assistant (Natural Continuous Flow)")
    print("[AUDIO] Non-Blocking Gapless Audio Daemon (/tmp/unitree_audio.sock)")
    print("=" * 60)
    
    ROUTER_THRESHOLD = 0.35
    
    # 1. Robot greeting
    startup_greeting = "My name is Jason. Domo Arigato Mr robot-oh."
    speak_direct_via_riva(startup_greeting)
    
    while True:
        print("\n" + "-" * 50)
        print("[PROMPT] Press [ENTER] to start speaking (or type 'q' to quit):")
        sys.stdout.flush()
        
        choice = sys.stdin.readline().strip().lower()
        if choice == 'q' or choice == 'exit':
            print("[EXIT] Exiting assistant.")
            break
            
        # 2. Push to talk audio capture with AGC
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
        import random
        if has_visual_intent:
            print("[ROUTE] 🎯 Vision Route Triggered (MiniLM Score: %.2f >= %.2f)!" % (similarity_score, ROUTER_THRESHOLD))
            # Speculative visual conversational filler (<30ms instant spoken response)
            vision_fillers = ["Let's see.", "Let me take a look.", "Looking at that."]
            queue_text_for_streaming_tts(random.choice(vision_fillers))
            
            # Capture live fresh camera frame right after query completion
            print("[CAMERA] Capturing fresh frame from %s..." % CAMERA_SOURCE)
            image_b64 = capture_camera_frame()
        else:
            print("[ROUTE] 💬 Text-only Route (MiniLM Score: %.2f < %.2f) - Discarding camera frame." % (similarity_score, ROUTER_THRESHOLD))
            # Speculative conversational filler for text-only queries (<30ms instant spoken response)
            text_fillers = ["Let me think.", "Hmm, let's see.", "Sure,", "Got it.", "Well,"]
            queue_text_for_streaming_tts(random.choice(text_fillers))
            image_b64 = None
            
        # 5. Stream tokens from Gemma-4 & speak via Magpie TTS
        query_gemma4_and_stream_tts(transcript, image_b64)

if __name__ == "__main__":
    main()
