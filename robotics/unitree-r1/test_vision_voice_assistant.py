#!/usr/bin/env python3
"""
==============================================================================
Unitree R1: Real-Time Multimodal Voice & Vision AI Assistant (Gemma-4 + Nemotron)
Pipelined Architecture: Multicast Mic -> Riva ASR -> MiniLM -> Camera -> Gemma-4 -> Magpie TTS -> Audio Daemon
==============================================================================
Modes:
  python3 test_vision_voice_assistant.py          push-to-talk (press ENTER to start / stop talking)
  python3 test_vision_voice_assistant.py --vad    hands-free: Silero VAD end-of-speech detection + wake word
                                                  (used by the r1-assistant systemd service)
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
import random
import re
import subprocess
import unicodedata
import collections
import numpy as np
import soundfile as sf
# onnxruntime MUST be imported before riva.client: if riva's grpc/protobuf load first, ORT rejects the
# MiniLM model with "INVALID_GRAPH ... Unsqueeze" and the router silently falls back to keyword matching.
try:
    import onnxruntime  # noqa: F401
except ImportError:
    onnxruntime = None
import riva.client

# --- Server & Network Configurations ---
RIVA_URI = "127.0.0.1:50051"
# llama-server exposes an OpenAI-compatible endpoint (VLLM_URL kept for backwards compatibility)
LLM_URL = os.getenv("LLM_URL", os.getenv("VLLM_URL", "http://127.0.0.1:8000/v1/chat/completions"))
MODEL_NAME = "gemma"

MCAST_GRP = "239.168.123.161"
MCAST_PORT = 5555
# Each mic datagram is 5120 bytes (160 ms). A smaller recv() buffer silently truncates the datagram:
# the old recv(4096) dropped 20% of all audio (32 ms out of every 160 ms) before it reached ASR.
MIC_RECV_BYTES = 65536
NET_INTERFACE_IP = "192.168.123.164"
NET_INTERFACE_NAME = "eth10"
CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "head")  # "head" (DDS eyes), "wrist0" (/dev/video0), "wrist2" (/dev/video2)
HEAD_CAMERA_SOCK = "/tmp/unitree_head_camera.sock"
# Snapshot lives in RAM (/dev/shm) - /tmp is on the NVMe root fs and would be rewritten 25x/sec
HEAD_CAMERA_SNAP = "/dev/shm/unitree_head_camera.jpg"
MAX_FRAME_AGE_SEC = 1.0  # Reject camera frames older than this (stale DDS stream)
# Gemma-4 turns every image into a fixed number of tokens (~280), so a larger, undistorted frame costs
# the same prefill time as the old squashed 256x256 one (measured 1.63-1.67 s for all sizes) but the
# model sees much more detail. The aspect ratio is preserved.
IMAGE_MAX_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "768"))
AUDIO_SOCKET = "/tmp/unitree_audio.sock"
PLAYER_BIN = "/home/unitree/unitree_sdk2/build/bin/unitree_play_wav"
TTS_SAMPLE_RATE = 16000
TTS_VOICE = "jason"

ROUTER_MODEL_PATH = "/home/unitree/robot_assets/models/onnx/model_qint8_arm64.onnx"
ROUTER_VOCAB_PATH = "/home/unitree/robot_assets/models/vocab.txt"
# Calibrated on-robot with tests/calibrate_router.py: 0.35 -> 24/28, 0.40 -> 25/28, 0.50 -> 22/28.
# Kept at 0.35 because a missed vision request is worse than an unneeded camera frame.
ROUTER_THRESHOLD = float(os.getenv("ROUTER_THRESHOLD", "0.35"))

MAX_RECORD_SEC = 30.0      # Safety cap for push-to-talk recording
AGC_NOISE_FLOOR = 500      # Don't amplify recordings whose peak is below this (silence / noise)
LLM_MAX_TOKENS = 80        # ~35 words + headroom so replies are not cut mid-sentence

# "Mister Roboto" (not "Mr. robot. oh."): Magpie reads it as one phrase without the pause
GREETING = os.getenv("GREETING", "My name is Jason. Domo arigato, Mister Roboto.")
SYSTEM_PROMPT = ("Your name is Jason. Don't use acronyms. You are a robot. For time or numbers spell them out "
                 "in letters. Speak in smooth, complete sentences. Response must be under 35 words.")

# Streaming TTS: Magpie starts returning audio after ~0.25 s instead of synthesizing the whole sentence
# first. The GPU is shared with the LLM, which slows offline synthesis ~3x while Gemma is decoding:
# measured first answer audio 4.0-4.2 s (offline, with gaps) vs 1.2-1.7 s (streaming, gapless).
TTS_STREAMING = os.getenv("TTS_STREAMING", "1") != "0"
TTS_PREBUFFER_SEC = float(os.getenv("TTS_PREBUFFER_SEC", "0.25"))  # audio collected before the first send
# Streamed audio can't be peak-normalized per sentence, so use a fixed gain with a soft limiter.
# Raw Magpie peaks are 13k-22k, so 2.0 roughly matches the old per-sentence normalization.
TTS_GAIN = float(os.getenv("TTS_GAIN", "2.0"))
TTS_LIMITER_KNEE = 20000.0
TTS_SILENCE_LEVEL = 400

# Fillers are synthesized once at startup and played from memory the moment the request is understood
TEXT_FILLERS = ["Hmm.", "Let me think.", "Okay.", "Sure.", "Hmm, let's see."]
VISION_FILLERS = ["Let's see.", "Let me take a look.", "Looking at that."]
WAKE_ACK = "Yes?"

# Speculative image prefill: grab a frame when the user starts talking and let llama-server encode it
# while they speak. If the request turns out to be visual, the image is already in the prompt cache
# (vision time-to-first-token 1.6 s -> 0.19 s); otherwise it is simply not used.
SPECULATIVE_PREFILL = os.getenv("SPECULATIVE_PREFILL", "1") != "0"

# --- Hands-free (VAD) mode ---
VAD_MODEL_PATH = os.getenv("VAD_MODEL_PATH", "/home/unitree/robot_assets/models/vad/silero_vad.onnx")
VAD_GAIN = float(os.getenv("VAD_GAIN", "2.0"))            # mic is quiet; boost before Silero
VAD_START_THRESHOLD = float(os.getenv("VAD_START_THRESHOLD", "0.5"))
VAD_END_THRESHOLD = float(os.getenv("VAD_END_THRESHOLD", "0.35"))
VAD_END_SILENCE_SEC = float(os.getenv("VAD_END_SILENCE_SEC", "0.6"))  # silence that ends an utterance
VAD_DEBUG = os.getenv("VAD_DEBUG", "0") == "1"
# Wake words: utterances must start with one of these unless inside the follow-up window.
# ASR spells the name several ways. Set WAKE_WORDS="" to respond to everything.
WAKE_WORDS = [w.strip().lower() for w in os.getenv("WAKE_WORDS", "jason,jayson,jaysen,jaison").split(",")
              if w.strip()]
FOLLOW_UP_SEC = float(os.getenv("FOLLOW_UP_SEC", "3"))  # no wake word needed this long after a reply
ECHO_GUARD_SEC = 0.5  # ignore the mic this long after playback ends (speaker latency is ~0.32 s)


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

    @staticmethod
    def _is_punctuation(ch):
        cp = ord(ch)
        if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
            return True
        return unicodedata.category(ch).startswith("P")

    def _basic_tokenize(self, text):
        """BERT BasicTokenizer: lowercase, strip accents, split on whitespace and punctuation."""
        text = unicodedata.normalize("NFD", text.lower())
        text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
        words = []
        for word in text.split():
            current = ""
            for ch in word:
                if self._is_punctuation(ch):
                    if current:
                        words.append(current)
                        current = ""
                    words.append(ch)
                else:
                    current += ch
            if current:
                words.append(current)
        return words

    def _wordpiece(self, word, max_chars=100):
        """Greedy longest-match-first WordPiece (matches HF BertTokenizer)."""
        if len(word) > max_chars:
            return ["[UNK]"]
        pieces = []
        start = 0
        while start < len(word):
            end = len(word)
            match = None
            while start < end:
                piece = word[start:end] if start == 0 else "##" + word[start:end]
                if piece in self.vocab:
                    match = piece
                    break
                end -= 1
            if match is None:
                return ["[UNK]"]
            pieces.append(match)
            start = end
        return pieces

    def tokenize(self, text, max_len=64):
        wordpieces = []
        for word in self._basic_tokenize(text):
            wordpieces.extend(self._wordpiece(word))
        tokens = ["[CLS]"] + wordpieces[:max_len-2] + ["[SEP]"]
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
last_riva_error = None
_warmup_audio = b""  # "ready" utterance, reused to warm up ASR
for attempt in range(1, 31):
    try:
        riva_auth = riva.client.Auth(uri=RIVA_URI)
        riva_asr = riva.client.ASRService(riva_auth)
        riva_tts = riva.client.SpeechSynthesisService(riva_auth)
        # Test basic synthesis call to verify server is listening and ready
        _warmup_audio = riva_tts.synthesize(text="ready", voice_name=TTS_VOICE, language_code="en-US",
                                            sample_rate_hz=TTS_SAMPLE_RATE).audio or b""
        print("[OK] Riva ASR & TTS connected and warmed up!")
        break
    except Exception as e:
        last_riva_error = e
        if attempt % 3 == 0:
            print("[RIVA] Waiting for Riva server... (%d/30)" % attempt)
        time.sleep(1)
else:
    # Without Riva there is no ASR or TTS - fail loudly instead of running a mute assistant
    print("[FATAL] Could not reach Riva Speech Server at %s after 30s: %s" % (RIVA_URI, last_riva_error))
    print("[FATAL] Check /home/unitree/riva_server.log or start the services with: bash /home/unitree/app.sh")
    sys.exit(1)

# --- 3. Per-turn latency log ---
class TurnTimer(object):
    """Records when each pipeline stage first happened, relative to the end of the user's speech."""
    def __init__(self, t0=None):
        self.t0 = t0 if t0 is not None else time.time()
        self.marks = collections.OrderedDict()
        self._lock = threading.Lock()

    def mark(self, name):
        with self._lock:
            if name not in self.marks:
                self.marks[name] = time.time() - self.t0

    def summary(self):
        with self._lock:
            return " ".join("%s=%dms" % (k, int(v * 1000)) for k, v in self.marks.items())

_turn_timer = None

def timer_mark(name):
    t = _turn_timer
    if t is not None:
        t.mark(name)

# --- 4. Natural Sentence-Level Multi-Threaded Pipelined TTS ---
# synthesis_queue items: (text or pre-rendered int16 array, tag). playback_queue items: (int16 array, tag).
# Tags ("greeting", "filler", "answer", ...) are only used for the per-turn timing log.
synthesis_queue = queue.Queue()
playback_queue = queue.Queue()

# Estimated wall-clock time at which the robot speaker finishes the audio we have handed off.
# The daemon accepts audio instantly, so queue.join() alone returns while the robot is still talking.
_playback_lock = threading.Lock()
_playback_until = 0.0

def _register_playback(num_samples):
    global _playback_until
    with _playback_lock:
        start = max(time.time(), _playback_until)
        _playback_until = start + float(num_samples) / TTS_SAMPLE_RATE

def playback_end_time():
    with _playback_lock:
        return _playback_until

def apply_tts_gain(audio_np, gain=None):
    """Fixed gain with a soft (tanh) limiter above TTS_LIMITER_KNEE, so loud peaks don't hard-clip."""
    g = TTS_GAIN if gain is None else gain
    x = audio_np.astype(np.float32) * g
    mag = np.abs(x)
    over = mag > TTS_LIMITER_KNEE
    if np.any(over):
        head = 32767.0 - TTS_LIMITER_KNEE
        mag[over] = TTS_LIMITER_KNEE + head * np.tanh((mag[over] - TTS_LIMITER_KNEE) / head)
        x = np.sign(x) * mag
    return np.clip(x, -32767, 32767).astype(np.int16)

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

class StreamChunker(object):
    """Turns streamed TTS chunks into playback sends.

    - Leading silence of the sentence is dropped.
    - Nothing is sent until `prebuffer` samples are collected (absorbs chunk-arrival jitter).
    - After that every chunk is sent as soon as it arrives.
    - Fully silent chunks are held back and only sent if more speech follows, so the
      trailing silence of a sentence never delays the next one.
    """
    def __init__(self, prebuffer, silence_level=TTS_SILENCE_LEVEL):
        self.prebuffer = prebuffer
        self.silence_level = silence_level
        self.pending = []
        self.held = []
        self.started = False
        self.leading = True

    def push(self, chunk):
        """Returns a list of arrays that are ready to play (possibly empty)."""
        if len(chunk) == 0:
            return []
        loud = np.where(np.abs(chunk.astype(np.int32)) > self.silence_level)[0]
        if self.leading:
            if len(loud) == 0:
                return []
            chunk = chunk[max(0, loud[0] - 160):]
            self.leading = False
        elif len(loud) == 0:
            self.held.append(chunk)
            return []
        if self.held:
            self.pending.extend(self.held)
            self.held = []
        self.pending.append(chunk)
        if not self.started and sum(len(p) for p in self.pending) < self.prebuffer:
            return []
        self.started = True
        out = np.concatenate(self.pending)
        self.pending = []
        return [out]

    def flush(self):
        """End of sentence: returns what is left (trailing silence trimmed); held silence is dropped."""
        if not self.pending:
            return []
        tail = np.concatenate(self.pending)
        self.pending = []
        loud = np.where(np.abs(tail.astype(np.int32)) > self.silence_level)[0]
        if len(loud) == 0:
            return []
        return [tail[:min(len(tail), loud[-1] + 160)]]

def synthesize_offline(text):
    """Whole-sentence synthesis (used for the filler cache and as a fallback)."""
    resp = riva_tts.synthesize(text=text, voice_name=TTS_VOICE, language_code="en-US",
                               sample_rate_hz=TTS_SAMPLE_RATE)
    if not resp.audio:
        return None
    audio_np = trim_silence_padding(apply_tts_gain(np.frombuffer(resp.audio, dtype=np.int16)))
    return audio_np if len(audio_np) > 0 else None

def synthesize_streaming(text, tag):
    """Streams Magpie audio to the playback queue as it is generated. Returns True if anything was sent."""
    chunker = StreamChunker(int(TTS_PREBUFFER_SEC * TTS_SAMPLE_RATE))
    sent = False
    for resp in riva_tts.synthesize_online(text=text, voice_name=TTS_VOICE, language_code="en-US",
                                           sample_rate_hz=TTS_SAMPLE_RATE):
        if not resp.audio:
            continue
        for piece in chunker.push(apply_tts_gain(np.frombuffer(resp.audio, dtype=np.int16))):
            playback_queue.put((piece, tag))
            sent = True
    for piece in chunker.flush():
        playback_queue.put((piece, tag))
        sent = True
    return sent

def tts_synthesizer_worker():
    """Background worker that synthesizes natural, complete sentence chunks into audio buffers."""
    while True:
        item = synthesis_queue.get()
        if item is None:
            playback_queue.put(None)
            synthesis_queue.task_done()
            break
        payload, tag = item
        sent = False
        try:
            if isinstance(payload, np.ndarray):
                # Pre-rendered audio (cached filler): play immediately
                if len(payload) > 0:
                    playback_queue.put((payload, tag))
            else:
                clean_text = payload.strip()
                if clean_text and len(clean_text) >= 2:
                    if TTS_STREAMING:
                        sent = synthesize_streaming(clean_text, tag)
                    else:
                        audio_np = synthesize_offline(clean_text)
                        if audio_np is not None:
                            playback_queue.put((audio_np, tag))
        except Exception as e:
            print("\n[ERROR] Synthesis worker error for '%s': %s" % (payload if not isinstance(payload, np.ndarray)
                                                                    else "<audio>", e))
            # Streaming failed before producing audio: retry once with whole-sentence synthesis
            if TTS_STREAMING and not sent and not isinstance(payload, np.ndarray):
                try:
                    audio_np = synthesize_offline(payload.strip())
                    if audio_np is not None:
                        playback_queue.put((audio_np, tag))
                except Exception as e2:
                    print("[ERROR] Offline TTS fallback failed: %s" % e2)
        finally:
            synthesis_queue.task_done()

def _send_to_audio_daemon(raw_pcm):
    """Returns True if the PCM was handed to the audio daemon. A stale socket file (daemon killed) returns False."""
    if not os.path.exists(AUDIO_SOCKET):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2.0)
        sock.connect(AUDIO_SOCKET)
        sock.sendall(raw_pcm)
        return True
    except (socket.error, OSError):
        return False
    finally:
        sock.close()

def audio_playback_worker():
    """Streams audio buffers through non-blocking UNIX domain socket directly to the audio daemon in <1ms."""
    fallback_warned = False
    while True:
        item = playback_queue.get()
        if item is None:
            playback_queue.task_done()
            break
        audio_np, tag = item
        try:
            raw_pcm = audio_np.tobytes()
            # 1. Fast Path: Persistent UNIX Domain Socket to Audio Daemon (<1ms)
            if _send_to_audio_daemon(raw_pcm):
                timer_mark("%s_audio" % tag)
                _register_playback(len(audio_np))
            elif os.path.exists(PLAYER_BIN):
                # 2. Fallback Path: Subprocess CLI player (blocking, returns after playback)
                if not fallback_warned:
                    print("\n[WARN] Audio daemon unavailable, falling back to %s" % PLAYER_BIN)
                    fallback_warned = True
                timer_mark("%s_audio" % tag)
                temp_wav = "/tmp/tts_chunk_%d.wav" % int(time.time() * 1000 % 100)
                sf.write(temp_wav, audio_np, TTS_SAMPLE_RATE, format='WAV', subtype='PCM_16')
                subprocess.run([PLAYER_BIN, temp_wav, NET_INTERFACE_NAME], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                print("\n[ERROR] No audio output available (daemon down and %s missing)" % PLAYER_BIN)
        except Exception as e:
            print("\n[ERROR] Playback worker error: %s" % e)
        finally:
            playback_queue.task_done()

threading.Thread(target=tts_synthesizer_worker, daemon=True).start()
threading.Thread(target=audio_playback_worker, daemon=True).start()

def queue_text_for_streaming_tts(text_chunk, tag="answer"):
    """Enqueues a natural text chunk to immediately begin TTS synthesis in background."""
    synthesis_queue.put((text_chunk, tag))

def wait_for_all_tts_to_finish():
    """Waits until all queued synthesis and audio playbacks are complete."""
    synthesis_queue.join()
    playback_queue.join()
    # Wait for the robot speaker to actually finish, so the mic does not record the robot's own voice
    remaining = playback_end_time() - time.time()
    if remaining > 0:
        time.sleep(remaining)

def speak_direct_via_riva(text_to_speak, tag="system"):
    """Synchronous speech for standalone announcements."""
    queue_text_for_streaming_tts(text_to_speak, tag)
    wait_for_all_tts_to_finish()

_filler_cache = {}
FILLER_CACHE_DIR = os.path.expanduser("~/.cache/r1_assistant/fillers")

def _filler_cache_path(text):
    import hashlib
    key = "%s|%s|%s|%.3f|%d" % (text, TTS_VOICE, TTS_SAMPLE_RATE, TTS_GAIN, TTS_SILENCE_LEVEL)
    return os.path.join(FILLER_CACHE_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".npy")

def prepare_filler_cache():
    """Pre-renders filler phrases so they can be played with zero synthesis latency.
    Rendered audio is kept on disk (keyed by text/voice/gain), so only the first start pays for synthesis."""
    for text in TEXT_FILLERS + VISION_FILLERS + [WAKE_ACK]:
        if text in _filler_cache:
            continue
        path = _filler_cache_path(text)
        try:
            if os.path.exists(path):
                _filler_cache[text] = np.load(path)
                continue
        except Exception:
            pass
        try:
            audio_np = synthesize_offline(text)
            if audio_np is not None:
                _filler_cache[text] = audio_np
                try:
                    if not os.path.isdir(FILLER_CACHE_DIR):
                        os.makedirs(FILLER_CACHE_DIR)
                    np.save(path, audio_np)
                except Exception:
                    pass
        except Exception as e:
            print("[WARN] Could not pre-render filler %r: %s" % (text, e))

def speak_filler(options, tag="filler"):
    """Plays a random filler from the cache (instant); falls back to synthesizing it if not cached yet."""
    cached = [t for t in options if t in _filler_cache]
    if cached:
        text = random.choice(cached)
        synthesis_queue.put((_filler_cache[text], tag))
    else:
        text = random.choice(options)
        synthesis_queue.put((text, tag))
    return text

# --- 5. Live Camera Subsystem (Head Eyes DDS + Wrist UVC) ---
def encode_frame_b64(frame):
    """Downscales (aspect ratio preserved) and JPEG-encodes a BGR frame for Gemma."""
    import cv2
    h, w = frame.shape[:2]
    scale = float(IMAGE_MAX_SIDE) / max(h, w)
    if scale < 1.0:
        frame = cv2.resize(frame, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA)
    _, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return base64.b64encode(buffer).decode('utf-8')

def capture_head_camera_frame(verbose=True):
    """Captures ultra-fast live frame from Head Eyes Camera daemon (<1ms via UNIX socket)."""
    import cv2
    raw_jpeg = None
    
    # 1. Fast Path: Read via UNIX socket from daemon (daemon returns size 0 if its latest frame is stale)
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

    # 2. Secondary Fast Path: Read from tmpfs (/dev/shm), only if the snapshot is fresh
    if raw_jpeg is None and os.path.exists(HEAD_CAMERA_SNAP):
        try:
            age = time.time() - os.path.getmtime(HEAD_CAMERA_SNAP)
            if age <= MAX_FRAME_AGE_SEC:
                with open(HEAD_CAMERA_SNAP, "rb") as f:
                    raw_jpeg = f.read()
            else:
                print("[WARN] Head camera snapshot is stale (%.1fs old) - is unitree_head_camera_daemon running?" % age)
        except Exception:
            pass

    if raw_jpeg:
        np_arr = np.frombuffer(raw_jpeg, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is not None:
            cv2.imwrite("/home/unitree/last_camera_snap.jpg", frame)
            if verbose:
                mean_brightness = float(np.mean(frame))
                print("[CAMERA] 👁️ Captured Head Eye DDS frame (Size: %dx%d, Brightness: %.1f)" % (frame.shape[1], frame.shape[0], mean_brightness))
            return encode_frame_b64(frame)
            
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

    def capture_frame_b64(self, verbose=True):
        import cv2
        frame = None
        with self.lock:
            if self.last_frame is not None:
                frame = self.last_frame.copy()
                
        if frame is None and self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            
        if frame is not None:
            cv2.imwrite("/home/unitree/last_camera_snap.jpg", frame)
            if verbose:
                mean_brightness = float(np.mean(frame))
                print("[CAMERA] ✋ Captured Wrist /dev/video%d frame (Size: %dx%d, Brightness: %.1f)" % (self.device_idx, frame.shape[1], frame.shape[0], mean_brightness))
            return encode_frame_b64(frame)
        print("[ERROR] Failed to capture frame from wrist camera /dev/video%d" % self.device_idx)
        return None

wrist_stream = None
if CAMERA_SOURCE in ["wrist0", "wrist2", "0", "2"]:
    dev_idx = 2 if CAMERA_SOURCE in ["wrist2", "2"] else 0
    wrist_stream = LiveCameraStream(dev_idx)

def capture_camera_frame(verbose=True):
    if CAMERA_SOURCE == "head":
        return capture_head_camera_frame(verbose)
    elif wrist_stream:
        return wrist_stream.capture_frame_b64(verbose)
    else:
        return capture_head_camera_frame(verbose)

# --- 6. Robust Audio Capture & Instant ASR ---
def open_mic_socket():
    """Joins the robot's multicast microphone stream (16 kHz mono int16)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((MCAST_GRP, MCAST_PORT))
    mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(NET_INTERFACE_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(0.1)
    return sock

def drain_socket(sock, max_sec=0.5):
    """Discards buffered mic packets (e.g. the robot's own voice recorded while it was talking)."""
    end = time.time() + max_sec
    old_timeout = sock.gettimeout()
    sock.setblocking(False)
    try:
        while time.time() < end:
            try:
                sock.recv(MIC_RECV_BYTES)
            except (BlockingIOError, socket.error):
                break
    finally:
        sock.settimeout(old_timeout)

def apply_agc(raw_audio):
    """Software Automatic Gain Control (AGC) - skip near-silent input to avoid amplifying noise into ASR hallucinations."""
    if not raw_audio or len(raw_audio) < 2:
        return None
    raw_audio = raw_audio[:len(raw_audio) - (len(raw_audio) % 2)]  # int16 alignment
    audio_np = np.frombuffer(raw_audio, dtype=np.int16)
    peak = int(np.max(np.abs(audio_np.astype(np.int32))))
    if peak >= AGC_NOISE_FLOOR:
        target_peak = 24000.0
        gain = min(5.0, target_peak / float(peak))
        audio_np = np.clip(audio_np * gain, -32767, 32767).astype(np.int16)
    return audio_np.tobytes()

def record_push_to_talk():
    """Captures live audio from Unitree multicast socket with push-to-talk and AGC."""
    sock = open_mic_socket()
    
    print("\n[TALK] 🎙️ LISTENING... Speak now! (Press [ENTER] when finished speaking)")
    
    audio_frames = []
    stop_event = threading.Event()
    
    def listen_for_enter():
        sys.stdin.readline()
        stop_event.set()
        
    input_thread = threading.Thread(target=listen_for_enter, daemon=True)
    input_thread.start()
    
    # Drain old buffered packets for 50ms
    drain_end = time.time() + 0.05
    while time.time() < drain_end:
        try:
            sock.recv(MIC_RECV_BYTES)
        except socket.timeout:
            break
            
    record_deadline = time.time() + MAX_RECORD_SEC
    while not stop_event.is_set():
        if time.time() > record_deadline:
            print("[TALK] Max recording length (%ds) reached - press [ENTER] to continue." % MAX_RECORD_SEC)
            stop_event.wait()  # Still consume the pending ENTER so it doesn't leak into the next prompt
            break
        try:
            data = sock.recv(MIC_RECV_BYTES)
            if data:
                audio_frames.append(data)
        except socket.timeout:
            continue
            
    sock.close()
    return apply_agc(b"".join(audio_frames))

def _asr_config():
    return riva.client.RecognitionConfig(
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        sample_rate_hertz=16000,
        language_code="en-US",
        max_alternatives=1,
        enable_automatic_punctuation=True
    )

def transcribe_audio_bytes(audio_bytes, verbose=True):
    """Sends 16kHz audio directly to Riva ASR engine (~70ms warm; the first call after start takes ~1.4s)."""
    if not audio_bytes or len(audio_bytes) < 3200:
        return ""
    try:
        response = riva_asr.offline_recognize(audio_bytes, _asr_config())
        if response.results and len(response.results) > 0:
            transcript = response.results[0].alternatives[0].transcript.strip()
            if verbose:
                print("[ASR] ⚡ You said: \"%s\"" % transcript)
            return transcript
    except Exception as e:
        print("[ERROR] ASR Error: %s" % e)
    return ""

# --- 7. Natural Continuous Flow Token Streaming ---
# We split speech ONLY at natural clause and sentence boundaries for human-like prosody
SENTENCE_ENDINGS = set([".", "!", "?"])
CLAUSE_SEPARATORS = set([",", ";", ":", "\u2014"])
MIN_CLAUSE_WORDS = 7

def split_speakable_text(buffer, min_clause_words=MIN_CLAUSE_WORDS):
    """Splits streamed LLM text at sentence/clause boundaries.

    A boundary only counts once the following character has arrived and is whitespace, so decimals
    like "3.5" are not split. Clause breaks require min_clause_words words. Returns (ready_list, remainder).
    """
    ready = []
    start = 0
    for i, ch in enumerate(buffer):
        is_boundary = False
        if ch == "\n":
            is_boundary = True
        elif i + 1 < len(buffer) and buffer[i + 1].isspace():
            if ch in SENTENCE_ENDINGS:
                is_boundary = True
            elif ch in CLAUSE_SEPARATORS and len(buffer[start:i + 1].split()) >= min_clause_words:
                is_boundary = True
        if is_boundary:
            piece = buffer[start:i + 1].strip()
            if piece:
                ready.append(piece)
            start = i + 1
    return ready, buffer[start:]

# Short-term memory so follow-ups ("And what about Germany?") work. Text only (no old images), last
# MEMORY_TURNS exchanges, forgotten after CONVERSATION_MEMORY_SEC of silence.
MEMORY_TURNS = int(os.getenv("MEMORY_TURNS", "3"))
CONVERSATION_MEMORY_SEC = float(os.getenv("CONVERSATION_MEMORY_SEC", "120"))
_history = []          # [(user_text, assistant_text), ...]
_history_time = 0.0    # when the last exchange finished

def remember_exchange(user_text, assistant_text):
    global _history, _history_time
    if MEMORY_TURNS <= 0 or not assistant_text.strip():
        return
    _history = (_history + [(user_text, assistant_text.strip())])[-MEMORY_TURNS:]
    _history_time = time.time()

def history_messages():
    if not _history or time.time() - _history_time > CONVERSATION_MEMORY_SEC:
        return []
    msgs = []
    for user_text, assistant_text in _history:
        msgs.append({"role": "user", "content": user_text})
        msgs.append({"role": "assistant", "content": assistant_text})
    return msgs

def build_llm_messages(user_text, image_b64=None, history=None):
    """Prefill and final request must produce byte-identical prompts up to the end of the image,
    otherwise llama-server can't reuse the cached image: same system prompt, same history, image before text."""
    content = []
    if image_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,%s" % image_b64}
        })
    if user_text:
        content.append({"type": "text", "text": user_text})
    return ([{"role": "system", "content": SYSTEM_PROMPT}]
            + (history_messages() if history is None else history)
            + [{"role": "user", "content": content}])

class SpeculativePrefill(object):
    """Captures a camera frame and has llama-server encode it (max_tokens=1) while the user is still talking.

    If the request is routed to vision, query with the same image (`image_b64`) after `wait()`: the image is
    then served from the prompt cache. On a text-only route call `cancel()` - the frame is just discarded.
    """
    def __init__(self):
        self.image_b64 = None
        self.elapsed = None
        self.history = history_messages()  # frozen, so the final query has the identical prefix
        self._cancelled = threading.Event()
        self._done = threading.Event()
        self._t0 = time.time()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        resp = None
        try:
            self.image_b64 = capture_camera_frame(verbose=False)
            if not self.image_b64 or self._cancelled.is_set():
                return
            payload = {
                "model": MODEL_NAME,
                "messages": build_llm_messages(None, self.image_b64, self.history),
                "max_tokens": 1,
                "temperature": 0.1,
                "stream": True,
                "cache_prompt": True,
            }
            resp = requests.post(LLM_URL, json=payload, stream=True, timeout=20)
            for _ in resp.iter_lines():
                if self._cancelled.is_set():
                    break
            self.elapsed = time.time() - self._t0
        except Exception as e:
            print("[WARN] Speculative image prefill failed: %s" % e)
        finally:
            if resp is not None:
                resp.close()
            self._done.set()

    def cancel(self):
        self._cancelled.set()

    def wait(self, timeout=6.0):
        return self._done.wait(timeout)

def query_gemma4_and_stream_tts(user_text, image_b64=None, history=None):
    """Streams tokens from Gemma-4 and dispatches complete, natural sentence chunks to parallel TTS pipeline.
    Returns the full reply text ("" on error)."""
    if image_b64:
        print("[GEMMA] Sending Multimodal Query (Image + Text)...")
    else:
        print("[GEMMA] Sending Fast Text-Only Query...")
    
    payload = {
        "model": MODEL_NAME,
        "messages": build_llm_messages(user_text, image_b64, history),
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": 0.1,
        "stream": True,
        "cache_prompt": True
    }
    
    try:
        resp = requests.post(LLM_URL, json=payload, stream=True, timeout=20)
        if resp.status_code != 200:
            print("[ERROR] Server Error (HTTP %d): %s" % (resp.status_code, resp.text))
            speak_direct_via_riva("Sorry, my language model is not ready yet. Please try again in a moment.")
            return
            
        full_text = ""
        current_sentence = ""
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
                            timer_mark("llm_first_token")
                            print(text_chunk, end="", flush=True)
                            current_sentence += text_chunk
                            full_text += text_chunk
                            
                            # Split at the exact boundary position so text after the punctuation
                            # (e.g. ". The") stays with the next sentence
                            ready, current_sentence = split_speakable_text(current_sentence)
                            for sentence_to_speak in ready:
                                timer_mark("first_sentence")
                                queue_text_for_streaming_tts(sentence_to_speak)
                    except Exception:
                        continue
                        
        print()
        # Enqueue any remaining full phrase
        if current_sentence.strip():
            timer_mark("first_sentence")
            queue_text_for_streaming_tts(current_sentence.strip())
        elif not full_text.strip():
            queue_text_for_streaming_tts("Sorry, I don't have an answer for that.")
            
        # Wait for pipelined audio playback to complete cleanly
        wait_for_all_tts_to_finish()
        return full_text
            
    except Exception as e:
        print("[ERROR] Connection Error: %s" % e)
        speak_direct_via_riva("I encountered an error connecting to my intelligence engine.")
        return ""

# --- 8. Wake word, Voice Activity Detection & end-of-speech detection ---
def match_wake_word(transcript, wake_words=None, max_leading_words=3):
    """Checks whether an utterance is addressed to the robot ("Jason, ...", "Hey Jason ...", "OK so Jason ...").

    Returns (matched, request_text_without_wake_word). An empty wake word list matches everything.
    """
    words = WAKE_WORDS if wake_words is None else wake_words
    if not words:
        return True, transcript.strip()
    pattern = r"^\W*(?:[\w']+\W+){0,%d}?(?:%s)\b[\s,.!?;:-]*" % (
        max_leading_words, "|".join(re.escape(w) for w in words))
    m = re.match(pattern, transcript, re.IGNORECASE)
    if not m:
        return False, ""
    return True, transcript[m.end():].strip()

class SileroVAD(object):
    """Silero VAD v5 (ONNX, CPU, <1 ms per 32 ms frame). Input: 512 int16 samples at 16 kHz."""
    FRAME = 512
    CONTEXT = 64

    def __init__(self, model_path):
        import onnxruntime
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3  # the v5 graph logs hundreds of harmless "unused initializer" warnings
        self.session = onnxruntime.InferenceSession(model_path, opts, providers=["CPUExecutionProvider"])
        names = [i.name for i in self.session.get_inputs()]
        if "state" not in names:
            raise RuntimeError("Expected Silero VAD v5 (inputs input/state/sr), got inputs %s" % names)
        self._sr = np.array(16000, dtype=np.int64)
        self.reset()

    def reset(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT, dtype=np.float32)

    def __call__(self, frame_int16, gain=1.0):
        x = np.clip(frame_int16.astype(np.float32) * (gain / 32768.0), -1.0, 1.0)
        inp = np.concatenate([self._context, x])[None, :]
        out, self._state = self.session.run(None, {"input": inp, "state": self._state, "sr": self._sr})
        self._context = x[-self.CONTEXT:]
        return float(out[0][0])

class UtteranceEndpointer(object):
    """Turns per-frame speech probabilities into utterances (pure logic, no I/O - unit tested).

    process(frame_bytes, prob) returns one of:
      (None, None)          nothing happened
      ("start", None)       speech started (frames from the pre-roll are included in the utterance)
      ("speech", seconds)   still in speech; payload = voiced duration so far
      ("end", audio_bytes)  utterance finished (trailing silence trimmed to keep_tail_sec)
      ("discard", None)     too short to be a request (cough, click, ...)
    """
    def __init__(self, frame_sec=SileroVAD.FRAME / 16000.0, start_threshold=VAD_START_THRESHOLD,
                 end_threshold=VAD_END_THRESHOLD, start_sec=0.15, end_silence_sec=VAD_END_SILENCE_SEC,
                 pre_roll_sec=0.3, min_speech_sec=0.4, max_speech_sec=15.0, keep_tail_sec=0.2):
        self.frame_sec = frame_sec
        self.start_threshold = start_threshold
        self.end_threshold = end_threshold
        self.start_frames = max(1, int(round(start_sec / frame_sec)))
        self.end_frames = max(1, int(round(end_silence_sec / frame_sec)))
        self.pre_roll_frames = int(round(pre_roll_sec / frame_sec))
        self.min_voiced_frames = int(round(min_speech_sec / frame_sec))
        self.max_frames = int(round(max_speech_sec / frame_sec))
        self.keep_tail_frames = int(round(keep_tail_sec / frame_sec))
        self.reset()

    def reset(self):
        self.active = False
        self._pre = collections.deque(maxlen=self.pre_roll_frames + self.start_frames)
        self._run = 0
        self._frames = []
        self._voiced = 0
        self._silence = 0

    def audio_so_far(self):
        return b"".join(self._frames)

    def process(self, frame, prob):
        if not self.active:
            self._pre.append(frame)
            self._run = self._run + 1 if prob >= self.start_threshold else 0
            if self._run >= self.start_frames:
                self.active = True
                self._frames = list(self._pre)
                self._voiced = self._run
                self._silence = 0
                self._pre.clear()
                return "start", None
            return None, None

        self._frames.append(frame)
        if prob >= self.end_threshold:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1
        if self._silence >= self.end_frames or len(self._frames) >= self.max_frames:
            drop = max(0, self._silence - self.keep_tail_frames)
            frames = self._frames[:len(self._frames) - drop] if drop else self._frames
            voiced = self._voiced
            self.reset()
            if voiced < self.min_voiced_frames:
                return "discard", None
            return "end", b"".join(frames)
        return "speech", self._voiced * self.frame_sec

# --- 9. One conversational turn (shared by push-to-talk and VAD modes) ---
def process_turn(audio_bytes, t_end_of_speech, require_wake=False, prefill=None):
    """ASR -> wake word check -> routing -> filler -> Gemma -> TTS. Returns True if the robot answered."""
    global _turn_timer
    _turn_timer = TurnTimer(t_end_of_speech)
    try:
        transcript = transcribe_audio_bytes(audio_bytes)
        timer_mark("asr")
        if not transcript:
            if prefill:
                prefill.cancel()
            print("[WARN] No speech detected, try speaking closer to the mic.")
            return False

        if require_wake:
            addressed, request = match_wake_word(transcript)
            if not addressed:
                if prefill:
                    prefill.cancel()
                print("[VAD] (not addressed to %s - ignoring)" % (WAKE_WORDS[0].capitalize() if WAKE_WORDS else "me"))
                return False
            if not request:
                # Just the name: acknowledge and open the follow-up window
                if prefill:
                    prefill.cancel()
                speak_filler([WAKE_ACK], tag="ack")
                wait_for_all_tts_to_finish()
                return True
            transcript = request

        # MiniLM Dense Semantic Routing: Determine visual intent
        similarity_score = calculate_vision_similarity(transcript)
        has_visual_intent = (similarity_score >= ROUTER_THRESHOLD)
        timer_mark("route")

        image_b64 = None
        history = None  # None = current conversation memory
        if has_visual_intent:
            print("[ROUTE] 🎯 Vision Route Triggered (MiniLM Score: %.2f >= %.2f)!" % (similarity_score, ROUTER_THRESHOLD))
            # Pre-rendered visual filler: plays instantly while the image is processed
            speak_filler(VISION_FILLERS)
            if prefill is not None:
                # Wait for the speculative prefill so the final query hits the cached image
                # (a second request now would land on another slot and re-encode the image)
                prefill.wait()
                timer_mark("prefill_ready")
                image_b64 = prefill.image_b64
                if image_b64:
                    history = prefill.history
                    print("[CAMERA] Using frame captured when you started talking (pre-encoded%s)." % (
                        " in %.1fs" % prefill.elapsed if prefill.elapsed else ""))
            if not image_b64:
                print("[CAMERA] Capturing fresh frame from %s..." % CAMERA_SOURCE)
                image_b64 = capture_camera_frame()
                timer_mark("frame")
        else:
            print("[ROUTE] 💬 Text-only Route (MiniLM Score: %.2f < %.2f) - Discarding camera frame." % (similarity_score, ROUTER_THRESHOLD))
            if prefill is not None:
                prefill.cancel()
            speak_filler(TEXT_FILLERS)

        # Stream tokens from Gemma-4 & speak via Magpie TTS
        reply = query_gemma4_and_stream_tts(transcript, image_b64, history)
        if reply:
            remember_exchange(transcript, reply)
        print("[TIMING] after end of speech: %s (+~0.3s speaker latency)" % _turn_timer.summary())
        return True
    finally:
        _turn_timer = None

def warm_up_models():
    """First calls are slow (ASR ~1.4 s, first image ~3.2 s). Run them once at startup instead of on the
    user's first question, and pre-render the fillers."""
    t0 = time.time()
    prepare_filler_cache()
    try:
        if _warmup_audio:
            transcribe_audio_bytes(_warmup_audio, verbose=False)
    except Exception:
        pass
    try:
        # Also caches the system prompt prefix in llama-server
        requests.post(LLM_URL, json={"model": MODEL_NAME, "messages": build_llm_messages("Hello"),
                                     "max_tokens": 1, "cache_prompt": True}, timeout=30)
    except Exception as e:
        print("[WARN] LLM warm-up failed: %s" % e)
    SpeculativePrefill().wait(20)  # loads the vision encoder
    print("[WARMUP] Fillers, ASR and LLM (text + vision) warmed up in %.1fs (%d fillers cached)." % (
        time.time() - t0, len(_filler_cache)))

# --- 10. Main loops ---
def run_push_to_talk():
    while True:
        print("\n" + "-" * 50)
        print("[PROMPT] Press [ENTER] to start speaking (or type 'q' to quit):")
        sys.stdout.flush()
        
        line = sys.stdin.readline()
        if not line:  # EOF on stdin (e.g. piped input finished)
            print("[EXIT] stdin closed, exiting assistant.")
            break
        choice = line.strip().lower()
        if choice == 'q' or choice == 'exit':
            print("[EXIT] Exiting assistant.")
            break

        # Start encoding the current camera view while the user talks
        prefill = SpeculativePrefill() if SPECULATIVE_PREFILL else None
            
        # Push to talk audio capture with AGC
        audio_bytes = record_push_to_talk()
        process_turn(audio_bytes, time.time(), require_wake=False, prefill=prefill)

def run_vad_mode():
    if not os.path.exists(VAD_MODEL_PATH):
        print("[FATAL] Silero VAD model not found at %s (see README: Hands-free mode)" % VAD_MODEL_PATH)
        sys.exit(1)
    vad = SileroVAD(VAD_MODEL_PATH)
    endpointer = UtteranceEndpointer()
    sock = open_mic_socket()
    frame_bytes = SileroVAD.FRAME * 2

    if WAKE_WORDS:
        print("[VAD] 👂 Hands-free mode. Say \"%s, ...\" (no wake word needed for %.0fs after each reply)."
              % (WAKE_WORDS[0].capitalize(), FOLLOW_UP_SEC))
    else:
        print("[VAD] 👂 Hands-free mode, wake word disabled - responding to all speech.")

    follow_up_until = 0.0
    ignore_until = 0.0
    pending = b""
    prefill = None
    wake_checked = False
    last_debug = 0.0

    def no_wake_needed():
        return (not WAKE_WORDS) or time.time() < follow_up_until

    while True:
        try:
            data = sock.recv(MIC_RECV_BYTES)
        except socket.timeout:
            continue
        # Echo guard: never listen to the robot's own voice
        if time.time() < max(ignore_until, playback_end_time() + ECHO_GUARD_SEC):
            pending = b""
            continue
        pending += data
        while len(pending) >= frame_bytes:
            frame, pending = pending[:frame_bytes], pending[frame_bytes:]
            prob = vad(np.frombuffer(frame, dtype=np.int16), VAD_GAIN)
            if VAD_DEBUG and time.time() - last_debug > 0.25:
                last_debug = time.time()
                print("[VAD] p=%.2f %s" % (prob, "#" * int(prob * 40)))
            event, payload = endpointer.process(frame, prob)

            if event == "start":
                print("[VAD] 🗣️ Speech started")
                wake_checked = no_wake_needed()
                prefill = SpeculativePrefill() if (SPECULATIVE_PREFILL and wake_checked) else None
            elif event == "speech" and not wake_checked and payload >= 0.9:
                # Peek at the first ~second: only spend GPU on an image prefill if it starts with the wake word
                wake_checked = True
                peek = transcribe_audio_bytes(apply_agc(endpointer.audio_so_far()), verbose=False)
                if match_wake_word(peek)[0] and SPECULATIVE_PREFILL:
                    prefill = SpeculativePrefill()
            elif event == "discard":
                if prefill:
                    prefill.cancel()
                prefill = None
            elif event == "end":
                t_end = time.time()
                answered = process_turn(apply_agc(payload), t_end, require_wake=not no_wake_needed(), prefill=prefill)
                prefill = None
                if answered:
                    follow_up_until = time.time() + FOLLOW_UP_SEC
                    print("[VAD] 👂 Listening (follow-up open for %.0fs)..." % FOLLOW_UP_SEC)
                # Everything recorded while we were busy is stale (and may contain the robot's own voice)
                drain_socket(sock)
                pending = b""
                vad.reset()
                endpointer.reset()
                ignore_until = time.time() + ECHO_GUARD_SEC
                break

def main():
    vad_mode = "--vad" in sys.argv[1:] or os.getenv("ASSISTANT_MODE", "").lower() == "vad"
    print("=" * 60)
    print("[SYSTEM] Unitree R1 Multimodal Assistant (Natural Continuous Flow)")
    print("[AUDIO] Non-Blocking Gapless Audio Daemon (/tmp/unitree_audio.sock)")
    print("[MODE] %s" % ("Hands-free (Silero VAD + wake word)" if vad_mode else "Push-to-talk"))
    print("=" * 60)
    
    # 1. Robot greeting (warm-up starts once the greeting is synthesized, so they don't compete for the GPU)
    queue_text_for_streaming_tts(GREETING, "greeting")
    synthesis_queue.join()
    warmup = threading.Thread(target=warm_up_models, daemon=True)
    warmup.start()
    wait_for_all_tts_to_finish()
    # Listen only once warm: otherwise the first question competes with the warm-up for the GPU
    # (~5 s with cached fillers, mostly hidden behind the greeting)
    warmup.join(60)

    if vad_mode:
        run_vad_mode()
    else:
        run_push_to_talk()

if __name__ == "__main__":
    main()
