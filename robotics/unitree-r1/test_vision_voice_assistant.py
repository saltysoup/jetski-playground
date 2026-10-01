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
import atexit
import os
import signal
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

# Startup phrase spoken once the assistant is up (override with the GREETING env var)
GREETING = os.getenv("GREETING", "Hasta la vista, baby.")
# Hands-free / push-to-talk (quiet room)
DEFAULT_SYSTEM_PROMPT = ("Your name is Jason. Don't use acronyms. You are a robot. For time or numbers spell them "
                         "out in letters. Speak in smooth, complete sentences. Response must be under 35 words. "
                         "Answer only what was asked: never end by offering more help or asking a question back.")
# Tap-to-talk is the noisy conference booth mode: shorter answers for faster turn-taking, and a prompt that
# expects speech recognition mistakes. Applied by apply_tap_mode_profile().
BOOTH_SYSTEM_PROMPT = ("Your name is Jason. You are a friendly robot at a conference booth, talking with visitors. "
                       "Don't use acronyms. For time or numbers spell them out in letters. Speak in smooth, complete "
                       "sentences. Response must be under 25 words. The visitor's words come from speech recognition "
                       "in a noisy room and may contain mistakes: if a request is unclear, ask one short question. "
                       "When describing what you see, focus on the person or object closest to you. Never guess who "
                       "a person is. Politely decline inappropriate requests. Answer only what was asked: never end by "
                       "offering more help or asking if they have another question.")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

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

# --- Head LED (set through the audio daemon's /tmp/unitree_led.sock) ---
# Purple while the robot is listening, green while it is talking. Colours are "R,G,B" (0-255).
def _rgb(name, default):
    try:
        r, g, b = [max(0, min(255, int(v))) for v in os.getenv(name, default).split(",")]
        return (r, g, b)
    except ValueError:
        return tuple(int(v) for v in default.split(","))

LED_ENABLED = os.getenv("LED", "1") != "0"
LED_SOCKET = "/tmp/unitree_led.sock"
LED_LISTEN = _rgb("LED_LISTEN", "160,0,255")  # purple
LED_TALK = _rgb("LED_TALK", "0,255,0")        # green
LED_THINK = _rgb("LED_THINK", "0,0,0")        # between end of speech and the first audio (off)
LED_IDLE = _rgb("LED_IDLE", "0,0,0")          # tap mode, waiting for the button (off)
SPEAKER_LATENCY_SEC = 0.3                     # audio handed to the daemon is heard ~0.3 s later

# --- Hands (BrainCo Revo2, via unitree_hand_bridge -> brainco_hand_server) ---
# Voice commands ("flex your hands", "thumbs up", "count to five", ...) are handled without the LLM, and the
# fingers move gently while the robot talks. Does nothing if the hand services aren't running.
HANDS_ENABLED = os.getenv("HANDS", "1") != "0"
HAND_SOCKET = "/tmp/unitree_hand.sock"
HAND_SPEED = float(os.getenv("HAND_SPEED", "0.8"))       # finger motor speed limit (0-1)
VOICE_GESTURES = os.getenv("VOICE_GESTURES", "1") != "0"
TALK_GESTURES = os.getenv("TALK_GESTURES", "1") != "0"
TALK_INTENSITY = max(0.0, min(1.0, float(os.getenv("TALK_INTENSITY", "1.0"))))  # how far fingers close while talking

# --- Microphone source ---
# "robot": the R1's mic array (multicast UDP, has echo cancellation).
# "usb": any ALSA capture device via arecord, e.g. a wireless handheld mic receiver (MIC_DEVICE, see `arecord -L`).
MIC_SOURCE = os.getenv("MIC_SOURCE", "robot")
MIC_DEVICE = os.getenv("MIC_DEVICE", "default")

# --- Hands-free (VAD) mode ---
VAD_MODEL_PATH = os.getenv("VAD_MODEL_PATH", "/home/unitree/robot_assets/models/vad/silero_vad.onnx")
VAD_GAIN = float(os.getenv("VAD_GAIN", "2.0"))            # mic is quiet; boost before Silero
VAD_START_THRESHOLD = float(os.getenv("VAD_START_THRESHOLD", "0.5"))
VAD_END_THRESHOLD = float(os.getenv("VAD_END_THRESHOLD", "0.35"))
VAD_END_SILENCE_SEC = float(os.getenv("VAD_END_SILENCE_SEC", "0.6"))  # silence that ends an utterance
VAD_MAX_SPEECH_SEC = float(os.getenv("VAD_MAX_SPEECH_SEC", "15"))
VAD_DEBUG = os.getenv("VAD_DEBUG", "0") == "1"
# Proximity gate: background chatter IS speech to Silero, so in a crowd the end of the visitor's sentence is
# never "silence". A frame only counts as the visitor's speech if it is also GATE_DB louder than the
# running background level (median of the last ~10 s outside utterances). 0 disables the gate.
GATE_DB = float(os.getenv("GATE_DB", "6"))
# Tap mode knows someone is about to speak (and false triggers can't happen before a tap), so a lower margin
# keeps more of a soft-spoken visitor. Measured in cafeteria / restaurant noise, see tests/e2e_noise.py.
TAP_GATE_DB = float(os.getenv("TAP_GATE_DB", "3"))
# Wake word: only needed to START a conversation. After each reply, further requests need no wake word
# until FOLLOW_UP_SEC of quiet; then the next request needs the name again.
# ASR spells the name several ways. WAKE_WORDS="" = never needed (answer all speech).
WAKE_WORDS = [w.strip().lower() for w in os.getenv("WAKE_WORDS", "jason,jayson,jaysen,jaison").split(",")
              if w.strip()]
FOLLOW_UP_SEC = float(os.getenv("FOLLOW_UP_SEC", "30"))  # conversation stays open this long after a reply
ECHO_GUARD_SEC = 0.5  # ignore the mic this long after playback ends (speaker latency is ~0.32 s)

# --- Tap-to-talk (--tap) mode: a wireless presenter clicker / USB button starts listening ---
# BUTTON_DEVICE: "auto" (first keyboard-like input device, hot-plug aware) or a /dev/input/eventN path.
BUTTON_DEVICE = os.getenv("BUTTON_DEVICE", "auto")
# Keys that act as the talk button. Defaults cover presenter clickers (PageUp/PageDown, arrows, B, F5)
# and keyboards (Enter, Space). Codes from linux/input-event-codes.h.
BUTTON_KEYS = [int(k) for k in os.getenv("BUTTON_KEYS", "28,57,104,109,105,106,48,63,96").split(",") if k.strip()]
RESET_KEYS = [int(k) for k in os.getenv("RESET_KEYS", "1,19").split(",") if k.strip()]  # Esc, R: new visitor
BUTTON_HOLD = os.getenv("BUTTON_HOLD", "0") == "1"   # 1 = hold to talk (release ends), 0 = tap to start
TAP_MAX_SEC = float(os.getenv("TAP_MAX_SEC", "8"))               # longest question after a tap
TAP_NO_SPEECH_SEC = float(os.getenv("TAP_NO_SPEECH_SEC", "5"))   # give up if nobody speaks after a tap


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
_playback_run_start = 0.0  # when the current continuous stretch of audio was handed off

def _register_playback(num_samples):
    global _playback_until, _playback_run_start
    with _playback_lock:
        now = time.time()
        if now >= _playback_until:
            _playback_run_start = now
        start = max(now, _playback_until)
        _playback_until = start + float(num_samples) / TTS_SAMPLE_RATE

def playback_end_time():
    with _playback_lock:
        return _playback_until

def robot_is_talking(now=None):
    """True while the speaker is producing our audio (handoff + ~0.3 s speaker latency)."""
    now = time.time() if now is None else now
    with _playback_lock:
        return _playback_run_start + SPEAKER_LATENCY_SEC <= now < _playback_until + SPEAKER_LATENCY_SEC

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

class LedIndicator(object):
    """Head LED. The main loop sets the state ("listening" / "thinking" / "idle"); whenever the robot is
    talking (greeting, fillers, answers) the LED is green regardless of the state.
    Colours go to the audio daemon as "R G B" datagrams; only changes are sent (plus a refresh every 3 s)."""
    STATES = {"listening": LED_LISTEN, "thinking": LED_THINK, "idle": LED_IDLE}

    def __init__(self, enabled=LED_ENABLED, path=LED_SOCKET):
        self.enabled = enabled
        self.path = path
        self.base = LED_IDLE
        self._sent = None
        self._last_send = 0.0
        self._lock = threading.Lock()
        self._warned = False
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) if enabled else None
        if enabled:
            threading.Thread(target=self._loop, daemon=True).start()

    def color(self, now=None):
        return LED_TALK if robot_is_talking(now) else self.base

    def set(self, state):
        self.base = self.STATES[state]
        self.update()

    def update(self, force=False):
        if not self.enabled:
            return
        with self._lock:
            color = self.color()
            now = time.time()
            if not force and color == self._sent and now - self._last_send < 3.0:
                return
            try:
                self.sock.sendto(("%d %d %d" % color).encode(), self.path)
                self._sent, self._last_send = color, now
            except (socket.error, OSError) as e:
                self._sent, self._last_send = color, now  # don't retry in a tight loop
                if not self._warned:
                    self._warned = True
                    print("[LED] Can't reach %s (%s) - restart the audio daemon with the LED-enabled build "
                          "(bash app.sh --services-only). Continuing without LED." % (self.path, e))

    def off(self):
        self.base = (0, 0, 0)
        self.enabled and self.update(force=True)

    def _loop(self):
        while True:
            self.update()
            time.sleep(0.05)

led = LedIndicator(enabled=False)  # replaced in main(); a no-op for tests that import this module

# --- Hands: poses, gestures and a smooth motion engine ---
# Finger order: thumb, thumb_aux (thumb rotation), index, middle, ring, pinky. 0 = open, 1 = closed.
HAND_POSES = {
    "open": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "relax": (0.15, 0.1, 0.2, 0.25, 0.3, 0.35),   # natural, slightly curled
    "fist": (1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
    "point": (1.0, 1.0, 0.0, 1.0, 1.0, 1.0),
    "thumbs_up": (0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
    "peace": (1.0, 1.0, 0.0, 0.0, 1.0, 1.0),
    "rock": (1.0, 1.0, 0.0, 1.0, 1.0, 0.0),
    "one": (1.0, 1.0, 0.0, 1.0, 1.0, 1.0),
    "two": (1.0, 1.0, 0.0, 0.0, 1.0, 1.0),
    "three": (1.0, 1.0, 0.0, 0.0, 0.0, 1.0),
    "four": (1.0, 1.0, 0.0, 0.0, 0.0, 0.0),
}

def _ripple(close=True):
    """Fingers curl one after another, pinky first (or open in reverse) - the 'flex'."""
    steps = [(0, 0, 0, 0, 0, 1), (0, 0, 0, 0, 1, 1), (0, 0, 0, 1, 1, 1), (0, 0, 1, 1, 1, 1), (1, 1, 1, 1, 1, 1)]
    if not close:
        steps = steps[::-1][1:] + [(0, 0, 0, 0, 0, 0)]
    return [(tuple(float(v) for v in s), 0.13) for s in steps]

def _hold(name, sec):
    return (HAND_POSES[name], sec)

# Gesture = {side: [(pose, seconds to reach it), ...]}; holding = same pose again. Every gesture ends relaxed.
_FLEX = _ripple(True) + [_hold("fist", 0.3)] + _ripple(False) + _ripple(True) + [_hold("fist", 0.3)] \
    + _ripple(False) + [_hold("relax", 0.6)]
HAND_GESTURES = {
    "flex": {"left": _FLEX, "right": _FLEX},
    "fist": {s: [_hold("fist", 0.5), _hold("fist", 1.5), _hold("relax", 0.6)] for s in ("left", "right")},
    "open": {s: [_hold("open", 0.5), _hold("open", 1.5), _hold("relax", 0.6)] for s in ("left", "right")},
    "thumbs_up": {"right": [_hold("thumbs_up", 0.5), _hold("thumbs_up", 2.0), _hold("relax", 0.6)]},
    "point": {"right": [_hold("point", 0.5), _hold("point", 2.0), _hold("relax", 0.6)]},
    "peace": {"right": [_hold("peace", 0.5), _hold("peace", 2.0), _hold("relax", 0.6)]},
    "rock": {"right": [_hold("rock", 0.5), _hold("rock", 2.0), _hold("relax", 0.6)]},
    # ~0.5 s per number, roughly in step with the spoken "One, two, three, four, five."
    "count": {"right": [_hold("fist", 0.4), _hold("one", 0.5), _hold("two", 0.5), _hold("three", 0.5),
                        _hold("four", 0.5), _hold("open", 0.5), _hold("open", 0.8), _hold("relax", 0.6)]},
    "wave": {"right": [_hold("open", 0.4)]
             + [((0, 0, 0.6, 0, 0.6, 0), 0.25), ((0, 0, 0, 0.6, 0, 0.6), 0.25)] * 3
             + [_hold("open", 0.3), _hold("relax", 0.6)]},
}

def smoothstep_pose(start, target, frac):
    """Pose between start and target with ease-in/ease-out (no jerk at either end)."""
    f = max(0.0, min(1.0, frac))
    f = f * f * (3.0 - 2.0 * f)
    return tuple(a + (b - a) * f for a, b in zip(start, target))

class HandMotion(object):
    """Drives both hands at 50 Hz through the hand bridge (/tmp/unitree_hand.sock).
    play(gesture) runs a keyframe gesture; while the robot is talking (and no gesture is playing) the
    fingers drift slowly between relaxed poses, and settle back to "relax" when it stops."""
    RATE_HZ = 50.0

    def __init__(self, enabled=HANDS_ENABLED, path=HAND_SOCKET, talk_gestures=TALK_GESTURES, seed=None):
        self.enabled = enabled
        self.path = path
        self.talk_gestures = talk_gestures
        self.rng = random.Random(seed)
        self._lock = threading.Lock()
        self.pose = {"left": HAND_POSES["open"], "right": HAND_POSES["open"]}  # server starts with open hands
        self._queue = {"left": collections.deque(), "right": collections.deque()}
        self._segment = {"left": None, "right": None}  # (start_pose, target, t0, duration)
        self._sent = {"left": None, "right": None}
        self._settle = {"left": False, "right": False}
        self._warned = False
        self._state_cache = (0.0, None)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) if enabled else None
        if enabled:
            threading.Thread(target=self._loop, daemon=True).start()

    # --- public API ---
    def play(self, name):
        with self._lock:
            for side, frames in HAND_GESTURES[name].items():
                self._queue[side].clear()
                self._queue[side].extend(frames)
                self._segment[side] = None

    def relax(self):
        with self._lock:
            for side in ("left", "right"):
                self._queue[side].clear()
                self._queue[side].append((HAND_POSES["relax"], 0.6))
                self._segment[side] = None

    def relax_now(self):
        """Send the relaxed pose immediately (at exit, when the 50 Hz loop is about to stop)."""
        if self.enabled:
            with self._lock:
                for side in ("left", "right"):
                    self._queue[side].clear()
                    self._segment[side] = None
            for side in ("left", "right"):
                self._send(side, HAND_POSES["relax"], speed=0.4)

    def busy(self, side=None):
        sides = (side,) if side else ("left", "right")
        return any(self._queue[s] or self._segment[s] for s in sides)

    def connected(self, max_age_ms=1500):
        """Sides whose hand state was seen recently (asks the bridge at most every 2 s)."""
        t, cached = self._state_cache
        if time.time() - t < 2.0 and cached is not None:
            return cached
        sides = []
        state = self.query_state()
        for side, (age, _) in (state or {}).items():
            if 0 <= age <= max_age_ms:
                sides.append(side)
        self._state_cache = (time.time(), sides)
        return sides

    def query_state(self, timeout=0.3):
        """{"left": (age_ms, q[6]), "right": ...} from the bridge, or None if it isn't running."""
        if not self.enabled or not os.path.exists(self.path):
            return None
        reply_path = "/tmp/r1_assistant_hand_%d_%d.sock" % (os.getpid(), threading.current_thread().ident % 100000)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            if os.path.exists(reply_path):
                os.unlink(reply_path)
            s.bind(reply_path)
            s.settimeout(timeout)
            s.sendto(b"state", self.path)
            return parse_hand_state(s.recv(512).decode())
        except (socket.error, OSError, ValueError):
            return None
        finally:
            s.close()
            if os.path.exists(reply_path):
                os.unlink(reply_path)

    # --- engine ---
    def step(self, now):
        """Advance both hands to time `now`; returns {side: pose} for sides whose pose changed."""
        out = {}
        talking = robot_is_talking(now)
        with self._lock:
            for side in ("left", "right"):
                if talking and self.talk_gestures:
                    self._settle[side] = True  # relax once the robot stops talking
                seg = self._segment[side]
                if seg is None and not self._queue[side]:
                    if talking and self.talk_gestures:
                        self._queue[side].append(self._talk_frame())
                    elif self._settle[side] and not talking:
                        self._settle[side] = False
                        self._queue[side].append((HAND_POSES["relax"], 0.8))
                if seg is None and self._queue[side]:
                    target, dur = self._queue[side].popleft()
                    seg = self._segment[side] = (self.pose[side], tuple(target), now, max(0.02, dur))
                if seg is None:
                    continue
                start, target, t0, dur = seg
                frac = (now - t0) / dur
                if frac >= 1.0:
                    self.pose[side] = target
                    self._segment[side] = None
                else:
                    self.pose[side] = smoothstep_pose(start, target, frac)
                out[side] = self.pose[side]
        return out

    def _talk_frame(self):
        """Next random 'talking hands' shape: each hand independently picks an expressive shape (with jitter)
        and moves to it in 0.35-0.9 s. TALK_INTENSITY (0-1) scales how far the fingers close."""
        r = self.rng.uniform
        k = TALK_INTENSITY
        style = self.rng.choice(("open", "curl", "curl", "half_point", "loose_fist", "random", "random", "beat"))
        if style == "open":          # open palm, "you see..."
            pose = (r(0.0, 0.15), r(0.0, 0.3), r(0.0, 0.1), r(0.0, 0.1), r(0.0, 0.15), r(0.0, 0.2))
        elif style == "curl":        # relaxed, uneven curl
            pose = (r(0.1, 0.4), r(0.1, 0.4), r(0.1, 0.5), r(0.2, 0.6), r(0.3, 0.7), r(0.3, 0.75))
        elif style == "half_point":  # index out, others half closed
            pose = (r(0.4, 0.7), r(0.3, 0.6), r(0.0, 0.15), r(0.5, 0.8), r(0.55, 0.85), r(0.55, 0.85))
        elif style == "loose_fist":
            pose = (r(0.5, 0.8), r(0.4, 0.7), r(0.6, 0.85), r(0.6, 0.85), r(0.6, 0.85), r(0.6, 0.85))
        elif style == "beat":        # quick emphasis: snap to a firmer close
            pose = (r(0.4, 0.6), r(0.3, 0.5), r(0.5, 0.8), r(0.55, 0.85), r(0.6, 0.85), r(0.6, 0.85))
            return tuple(v * k for v in pose), r(0.25, 0.4)
        else:                        # independent random fingers
            pose = (r(0.0, 0.6), r(0.0, 0.6), r(0.0, 0.85), r(0.0, 0.85), r(0.0, 0.85), r(0.0, 0.85))
        return tuple(v * k for v in pose), r(0.35, 0.9)

    def _send(self, side, pose, speed=None):
        msg = "%s %s %.2f" % (side, " ".join("%.3f" % v for v in pose), HAND_SPEED if speed is None else speed)
        try:
            self.sock.sendto(msg.encode(), self.path)
            self._sent[side] = pose
        except (socket.error, OSError):
            if not self._warned:
                self._warned = True
                print("[HANDS] Hand bridge not running (%s) - gestures disabled. Start it with app.sh." % self.path)

    def _loop(self):
        period = 1.0 / self.RATE_HZ
        while True:
            changed = self.step(time.time())
            live = self.connected() if changed else ()
            for side, pose in changed.items():
                if side not in live:
                    continue
                if self._sent[side] is None or max(abs(a - b) for a, b in zip(pose, self._sent[side])) > 0.003:
                    self._send(side, pose)
            time.sleep(period)

def parse_hand_state(text):
    """'left 12 q0..q5 | right -1 q0..q5' -> {"left": (12, [...]), "right": (-1, [...])}"""
    out = {}
    for part in text.split("|"):
        f = part.split()
        if len(f) == 8 and f[0] in ("left", "right"):
            out[f[0]] = (int(f[1]), [float(v) for v in f[2:]])
    return out

hands = HandMotion(enabled=False)  # replaced in main()

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
class ArecordMic(object):
    """ALSA capture (e.g. a wireless handheld mic's USB receiver) via `arecord`, exposing the subset of the
    socket API the assistant uses (recv / settimeout / gettimeout / setblocking / close).
    Note: unlike the robot's mic array there is no echo cancellation - the robot never listens while it
    talks (echo guard), so this is fine without barge-in."""
    def __init__(self, device):
        self.proc = subprocess.Popen(["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", "16000", "-c", "1",
                                      "-t", "raw", "--buffer-time=200000"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.fd = self.proc.stdout.fileno()
        self.timeout = 0.1
        time.sleep(0.1)
        if self.proc.poll() is not None:
            err = self.proc.stderr.read().decode("utf-8", "replace").strip()
            raise RuntimeError("arecord failed on MIC_DEVICE=%s: %s (list devices with: arecord -L)" % (device, err))

    def recv(self, n):
        import select
        ready, _, _ = select.select([self.fd], [], [], self.timeout)
        if not ready:
            if self.timeout == 0:
                raise BlockingIOError()
            raise socket.timeout()
        data = os.read(self.fd, min(n, 5120))
        if not data:
            raise RuntimeError("arecord stopped (USB mic unplugged?)")
        return data

    def settimeout(self, t):
        self.timeout = 0.1 if t is None else t

    def gettimeout(self):
        return self.timeout

    def setblocking(self, flag):
        self.timeout = 0.1 if flag else 0

    def close(self):
        try:
            self.proc.kill()
            self.proc.wait(1)
        except Exception:
            pass

def open_mic_socket():
    """Opens the configured microphone (MIC_SOURCE): robot multicast stream or a USB/ALSA mic.
    Both deliver 16 kHz mono int16."""
    if MIC_SOURCE == "usb":
        return ArecordMic(MIC_DEVICE)
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
CONVERSATION_MEMORY_SEC = float(os.getenv("CONVERSATION_MEMORY_SEC", "120"))  # 30 in tap (booth) mode
_history = []          # [(user_text, assistant_text), ...]
_history_time = 0.0    # when the last exchange finished

def reset_conversation():
    """New visitor: forget the previous conversation."""
    global _history, _history_time
    _history = []
    _history_time = 0.0

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
        spoken = []  # sentences actually spoken (closing offers removed); this is what gets remembered
        current_sentence = ""
        print("[ROBOT] Jason: ", end="", flush=True)

        def speak(sentence):
            if spoken and is_closing_offer(sentence):
                print(" [dropped closing offer]", end="", flush=True)
                return
            timer_mark("first_sentence")
            spoken.append(sentence)
            queue_text_for_streaming_tts(sentence)
        
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
                                speak(sentence_to_speak)
                    except Exception:
                        continue
                        
        print()
        # Enqueue any remaining full phrase
        if current_sentence.strip():
            speak(current_sentence.strip())
        elif not full_text.strip():
            queue_text_for_streaming_tts("Sorry, I don't have an answer for that.")
            
        # Wait for pipelined audio playback to complete cleanly
        wait_for_all_tts_to_finish()
        return " ".join(spoken)
            
    except Exception as e:
        print("[ERROR] Connection Error: %s" % e)
        speak_direct_via_riva("I encountered an error connecting to my intelligence engine.")
        return ""

# Small models often tack on "Can I help you with anything else today?" even when told not to.
# Such closing offers are dropped before they are spoken - unless they are the whole reply (then it is
# probably a genuine clarifying question, which the booth prompt asks for).
_CLOSING_OFFER = re.compile(
    r"\b(anything|something) else\b|\banother question\b|\b(any )?(other|more|further) questions?\b"
    r"|\bhow (can|may) i (help|assist)\b|\bwhat else can i\b|\bcan i (help|assist) you\b"
    r"|\b(let me know|feel free)\b|\bis there (anything|something)\b|\bi am (here|ready) to (help|assist)\b"
    r"|\bwhat would you like (to know|me to do|assistance with|help with)\b"
    r"|\bplease (tell me|let me know|state) what you\b|\bhow can i be of\b|\bwhat you are looking for\b"
    r"|\bis (that|this) what you\b|\bwhat you were asking\b|\bdoes (that|this) (answer|help)\b"
    r"|\bwould you like to (know|hear) more\b|\banything more\b",
    re.IGNORECASE)

def is_closing_offer(sentence):
    return bool(_CLOSING_OFFER.search(sentence))

# Voice commands for hand gestures. The whole (normalised) request must match, so questions that merely
# contain the words ("what is the point of life", "how do you flex") still go to the LLM.
_GESTURE_LEADIN = re.compile(
    r"^(?:hey|ok|okay|so|now|jason|can you|could you|would you|will you|please|show me|show us|let me see"
    r"|let us see|lets see|do|give me|give us|go ahead and|you)\s+")
_GESTURE_TAIL = r"(?:\s+(?:for me|for us|please|now|again|jason))*"
_GESTURE_COMMANDS = [
    (r"flex(?: (?:your|the|those|these) (?:hands?|fingers?))?"
     r"|(?:wiggle|move|stretch) (?:your|the|those|these) (?:hands?|fingers?)", "flex", "Check out these fingers!"),
    (r"(?:make|do) a fist|fist", "fist", "Like this!"),
    (r"(?:a |the )?thumbs? up", "thumbs_up", "Thumbs up!"),
    (r"point(?: your finger)?", "point", "Over there!"),
    (r"(?:a |the )?(?:peace|victory)(?: sign)?", "peace", "Peace!"),
    (r"rock on|(?:a |the )?(?:rock|rock and roll|devil horns?) sign|devil horns", "rock", "Rock on!"),
    (r"count(?: to (?:five|5)| with your fingers| on your fingers)", "count", "One, two, three, four, five."),
    (r"open your hands?", "open", "Ta-da!"),
    (r"wave(?: (?:at|to) (?:me|us|everyone|everybody))?(?: hello| hi)?|say (?:hi|hello)", "wave", "Hello there!"),
]
_GESTURE_COMMANDS = [(re.compile(r"^(?:%s)%s$" % (p, _GESTURE_TAIL)), g, r) for p, g, r in _GESTURE_COMMANDS]

def match_gesture_command(text):
    """'Jason, can you flex your hands?' -> ("flex", "Check out these fingers!"); (None, None) if no command."""
    t = re.sub(r"[^\w\s']", " ", (text or "").lower()).replace("'", "")
    t = re.sub(r"\s+", " ", t).strip()
    prev = None
    while prev != t:
        prev, t = t, _GESTURE_LEADIN.sub("", t)
    for pattern, gesture, reply in _GESTURE_COMMANDS:
        if pattern.match(t):
            return gesture, reply
    return None, None

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

class WakeSession(object):
    """The wake word only starts a conversation. Every answered request keeps the conversation open for
    another follow_up_sec (counted from the end of the reply); after that much quiet the next request needs
    the wake word again. A request that STARTED while the conversation was open still counts as inside it."""

    def __init__(self, wake_words=None, follow_up_sec=None):
        self.required = bool(WAKE_WORDS if wake_words is None else wake_words)
        self.follow_up_sec = FOLLOW_UP_SEC if follow_up_sec is None else follow_up_sec
        self.until = 0.0
        self._closed_reported = True

    def is_open(self, now):
        return (not self.required) or now < self.until

    def answered(self, now):
        self.until = now + self.follow_up_sec
        self._closed_reported = False

    def just_closed(self, now):
        """True once when an open conversation times out (for a log line)."""
        if self.required and not self._closed_reported and now >= self.until:
            self._closed_reported = True
            return True
        return False

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
                 pre_roll_sec=0.3, min_speech_sec=0.4, max_speech_sec=VAD_MAX_SPEECH_SEC, keep_tail_sec=0.2):
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
        self.last_drop = 0  # trailing silence frames trimmed from the last "end" utterance
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
            self.last_drop = drop
            if voiced < self.min_voiced_frames:
                return "discard", None
            return "end", b"".join(frames)
        return "speech", self._voiced * self.frame_sec

def frame_level_db(frame_int16):
    """RMS level of a frame in dBFS (-96 for digital silence)."""
    x = frame_int16.astype(np.float64)
    rms = np.sqrt(np.mean(x * x)) if len(x) else 0.0
    return 20.0 * np.log10(max(rms, 0.5) / 32768.0)

class ProximityGate(object):
    """Counts a frame as the visitor's speech only if it is gate_db louder than the background.

    In a crowd, Silero (correctly) reports the chatter as speech, so "silence" never arrives and the end of
    the visitor's sentence can't be found. The visitor in front of the robot is louder than the crowd, so we
    track the background level (median of recent frames outside utterances) and require a margin above it.
    """
    def __init__(self, gate_db=GATE_DB, window_sec=10.0, frame_sec=SileroVAD.FRAME / 16000.0,
                 min_floor_db=-70.0):
        self.gate_db = gate_db
        self.min_floor_db = min_floor_db
        self._levels = collections.deque(maxlen=max(1, int(window_sec / frame_sec)))
        self._floor_cache = None

    def observe_background(self, level_db):
        self._levels.append(level_db)
        self._floor_cache = None

    def floor_db(self):
        if self._floor_cache is None:
            if len(self._levels) < 15:  # < 0.5 s of data: assume a quiet room
                self._floor_cache = self.min_floor_db
            else:
                self._floor_cache = max(self.min_floor_db, float(np.median(self._levels)))
        return self._floor_cache

    def passes(self, level_db, margin_db=None):
        margin = self.gate_db if margin_db is None else margin_db
        return self.gate_db <= 0 or level_db >= self.floor_db() + margin

class VoiceFrameScorer(object):
    """Silero speech probability, zeroed when the frame is not clearly louder than the background.

    Hysteresis: starting an utterance needs the full GATE_DB margin; continuing one only needs
    continue_db (default half) on the loudest of the last ~160 ms, so soft syllables and word endings
    aren't cut (measured at 5 dB SNR in cafeteria noise: full margin throughout clipped words, WER 0.42)."""
    def __init__(self, vad, gate, continue_db=None, hold_frames=5):
        self.vad = vad
        self.gate = gate
        self.continue_db = gate.gate_db / 2.0 if continue_db is None else continue_db
        self._recent = collections.deque(maxlen=hold_frames)
        self.last_level = -96.0

    def __call__(self, frame_bytes, in_utterance):
        frame = np.frombuffer(frame_bytes, dtype=np.int16)
        prob = self.vad(frame, VAD_GAIN)
        level = frame_level_db(frame)
        self.last_level = level
        self._recent.append(level)
        if not in_utterance:
            self.gate.observe_background(level)
            return prob if self.gate.passes(level) else 0.0
        return prob if self.gate.passes(max(self._recent), self.continue_db) else 0.0

    def reset(self):
        self.vad.reset()
        self._recent.clear()

# Linux input subsystem: struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
INPUT_EVENT = struct.Struct("llHHi")
EV_KEY = 0x01
_NOT_A_BUTTON = re.compile(r"gpio-keys|HDMI|HDA|Image|Camera|Video|Webcam", re.IGNORECASE)

def parse_input_devices(text):
    """Parses /proc/bus/input/devices into [(name, event_path, key_codes_set)]."""
    devices = []
    for block in text.strip().split("\n\n"):
        name, event, keys = "", None, set()
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("H: Handlers="):
                m = re.search(r"\bevent(\d+)\b", line)
                if m:
                    event = "/dev/input/event%s" % m.group(1)
            elif line.startswith("B: KEY="):
                words = line.split("=", 1)[1].split()
                for i, word in enumerate(reversed(words)):  # last word = bits 0..63
                    value = int(word, 16)
                    for bit in range(64):
                        if value >> bit & 1:
                            keys.add(i * 64 + bit)
        if event:
            devices.append((name, event, keys))
    return devices

def find_button_device(text, wanted_keys):
    """First input device that can send one of the talk keys and isn't a known non-button (camera, HDMI...)."""
    for name, event, keys in parse_input_devices(text):
        if _NOT_A_BUTTON.search(name):
            continue
        if keys & set(wanted_keys):
            return name, event
    return None, None

class ButtonListener(object):
    """Reads a presenter clicker / USB button / keyboard directly from /dev/input (works under systemd with
    no terminal; needs the 'input' group). Re-scans every 2 s, so the receiver can be plugged in any time.
    Also accepts ENTER on stdin when run in a terminal. Events: "press", "release", "reset"."""
    def __init__(self, device=BUTTON_DEVICE, talk_keys=BUTTON_KEYS, reset_keys=RESET_KEYS):
        self.device = device
        self.talk_keys = set(talk_keys)
        self.reset_keys = set(reset_keys)
        self.events = queue.Queue()
        self._warned = set()
        threading.Thread(target=self._input_loop, daemon=True).start()
        if sys.stdin is not None and sys.stdin.isatty():
            threading.Thread(target=self._stdin_loop, daemon=True).start()

    def _stdin_loop(self):
        while True:
            line = sys.stdin.readline()
            if not line:
                return
            cmd = line.strip().lower()
            self.events.put("reset" if cmd in ("r", "reset") else "press")

    def _open(self):
        path, name = self.device, self.device
        if self.device == "auto":
            try:
                with open("/proc/bus/input/devices") as f:
                    name, path = find_button_device(f.read(), self.talk_keys)
            except IOError:
                path = None
        if not path:
            return None
        try:
            fd = os.open(path, os.O_RDONLY)
            print("[BUTTON] 🔘 Using %s (%s)" % (name, path))
            return fd
        except OSError as e:
            if path not in self._warned:
                self._warned.add(path)
                hint = " - add the user to the 'input' group (sudo usermod -aG input unitree, then log in again)" \
                    if e.errno == 13 else ""
                print("[BUTTON] Cannot open %s: %s%s" % (path, e.strerror, hint))
            return None

    def _input_loop(self):
        while True:
            fd = self._open()
            if fd is None:
                time.sleep(2.0)
                continue
            try:
                while True:
                    data = os.read(fd, INPUT_EVENT.size)
                    if len(data) < INPUT_EVENT.size:
                        break
                    _, _, ev_type, code, value = INPUT_EVENT.unpack(data)
                    if ev_type != EV_KEY or value == 2:  # 2 = auto-repeat
                        continue
                    if code in self.reset_keys and value == 1:
                        self.events.put("reset")
                    elif code in self.talk_keys:
                        self.events.put("press" if value == 1 else "release")
            except OSError:
                print("[BUTTON] Button device disconnected - waiting for it to come back")
            finally:
                os.close(fd)

    def get(self, timeout=0.0):
        try:
            return self.events.get(timeout=timeout) if timeout else self.events.get_nowait()
        except queue.Empty:
            return None

    def clear(self):
        while self.get() is not None:
            pass

class TapRecorder(object):
    """One tap-to-talk turn (pure logic, no I/O - unit tested). Recording starts at the press.
    feed(frame, prob) / button(event) return (None, None), ("start", None), ("end", audio_bytes) or
    ("cancel", reason).

    Tap mode (hold=False): ends when the visitor stops talking (endpointer), on a second tap (everything since
    the first tap is used), after max_sec, or is cancelled if nobody speaks within no_speech_sec.
    Hold mode (hold=True): records from press to release (max_sec cap); VAD end-of-speech is ignored.
    """
    def __init__(self, endpointer, frame_sec=SileroVAD.FRAME / 16000.0, max_sec=TAP_MAX_SEC,
                 no_speech_sec=TAP_NO_SPEECH_SEC, hold=BUTTON_HOLD):
        self.endpointer = endpointer
        self.endpointer.reset()
        self.frame_sec = frame_sec
        self.max_frames = int(round(max_sec / frame_sec))
        self.no_speech_frames = int(round(no_speech_sec / frame_sec))
        self.hold = hold
        self.frames = []
        self.heard_speech = False

    def feed(self, frame, prob):
        self.frames.append(frame)
        event, payload = self.endpointer.process(frame, prob)
        result = (None, None)
        if event == "start":
            self.heard_speech = True
            result = ("start", None)
        elif event == "discard":  # cough / click: keep waiting for the real question
            self.heard_speech = False
        elif event == "end" and not self.hold:
            # Everything since the tap (minus trailing silence), not just the endpointer's utterance: a short
            # first word may have been discarded as a cough and must not be cut off the question.
            return "end", b"".join(self.frames[:len(self.frames) - self.endpointer.last_drop])
        if len(self.frames) >= self.max_frames:
            return "end", b"".join(self.frames)
        if not self.hold and not self.heard_speech and not self.endpointer.active \
                and len(self.frames) >= self.no_speech_frames:
            return "cancel", "No speech heard after the tap - cancelled."
        return result

    def button(self, event):
        if event == "reset":
            return "cancel", "Reset - turn cancelled."
        if (event == "press" and not self.hold) or (event == "release" and self.hold):
            if not self.frames:
                return "cancel", "Button released before any audio arrived - cancelled."
            return "end", b"".join(self.frames)
        return None, None

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

        # Hand gesture voice commands ("flex your hands", "thumbs up") skip the LLM entirely
        gesture, gesture_reply = match_gesture_command(transcript) if VOICE_GESTURES else (None, None)
        if gesture and hands.enabled:
            if prefill:
                prefill.cancel()
            if hands.connected():
                print("[HANDS] 🖐  Gesture command: %s" % gesture)
                hands.play(gesture)
            else:
                print("[HANDS] Gesture '%s' requested but the hands are not connected - start brainco_hand_server "
                      "(bash app.sh --services-only)." % gesture)
                gesture_reply = "Sorry, my hands are not connected right now."
            queue_text_for_streaming_tts(gesture_reply, "answer")
            wait_for_all_tts_to_finish()
            remember_exchange(transcript, gesture_reply)
            print("[TIMING] after end of speech: %s (+~0.3s speaker latency)" % _turn_timer.summary())
            return True

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
        led.set("listening")
        audio_bytes = record_push_to_talk()
        led.set("thinking")
        process_turn(audio_bytes, time.time(), require_wake=False, prefill=prefill)
        led.set("idle")

def run_vad_mode():
    if not os.path.exists(VAD_MODEL_PATH):
        print("[FATAL] Silero VAD model not found at %s (see README: Hands-free mode)" % VAD_MODEL_PATH)
        sys.exit(1)
    scorer = VoiceFrameScorer(SileroVAD(VAD_MODEL_PATH), ProximityGate())
    endpointer = UtteranceEndpointer()
    sock = open_mic_socket()
    frame_bytes = SileroVAD.FRAME * 2

    wake_name = WAKE_WORDS[0].capitalize() if WAKE_WORDS else ""
    if WAKE_WORDS:
        print("[VAD] 👂 Hands-free mode. Start with \"%s, ...\" - then no wake word is needed until %.0fs of quiet."
              % (wake_name, FOLLOW_UP_SEC))
    else:
        print("[VAD] 👂 Hands-free mode, wake word disabled - responding to all speech.")
    if GATE_DB > 0:
        print("[VAD] Proximity gate on: speech must be %.0f dB above the background." % GATE_DB)
    led.set("listening")  # always listening in hands-free mode (green while talking)

    session = WakeSession()
    started_in_conversation = False
    ignore_until = 0.0
    pending = b""
    prefill = None
    wake_checked = False
    last_debug = 0.0

    while True:
        try:
            data = sock.recv(MIC_RECV_BYTES)
        except socket.timeout:
            data = b""
        if not endpointer.active and session.just_closed(time.time()):
            print("[VAD] 💤 Conversation closed after %.0fs of quiet - say \"%s, ...\" to start again."
                  % (FOLLOW_UP_SEC, wake_name))
        if not data:
            continue
        # Echo guard: never listen to the robot's own voice
        if time.time() < max(ignore_until, playback_end_time() + ECHO_GUARD_SEC):
            pending = b""
            continue
        pending += data
        while len(pending) >= frame_bytes:
            frame, pending = pending[:frame_bytes], pending[frame_bytes:]
            prob = scorer(frame, endpointer.active)
            if VAD_DEBUG and time.time() - last_debug > 0.25:
                last_debug = time.time()
                print("[VAD] p=%.2f level=%.0fdB floor=%.0fdB %s" % (
                    prob, scorer.last_level, scorer.gate.floor_db(), "#" * int(prob * 40)))
            event, payload = endpointer.process(frame, prob)

            if event == "start":
                print("[VAD] 🗣️ Speech started")
                started_in_conversation = session.is_open(time.time())
                wake_checked = started_in_conversation
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
                led.set("thinking")
                in_conversation = started_in_conversation or session.is_open(t_end)
                answered = process_turn(apply_agc(payload), t_end, require_wake=not in_conversation, prefill=prefill)
                prefill = None
                if answered:
                    session.answered(time.time())
                    if WAKE_WORDS:
                        print("[VAD] 👂 Listening - no wake word needed for the next %.0fs." % FOLLOW_UP_SEC)
                # Everything recorded while we were busy is stale (and may contain the robot's own voice)
                drain_socket(sock)
                pending = b""
                scorer.reset()
                endpointer.reset()
                ignore_until = time.time() + ECHO_GUARD_SEC
                led.set("listening")
                break

def run_tap_mode(buttons=None, sock=None, max_turns=None):
    """Booth mode: a presenter clicker / USB button (or ENTER in a terminal) starts listening; the turn ends
    when the visitor stops talking. No wake word, so crowd chatter can't trigger the robot.
    buttons / sock / max_turns are for the injected end-to-end test."""
    scorer = None
    if os.path.exists(VAD_MODEL_PATH):
        scorer = VoiceFrameScorer(SileroVAD(VAD_MODEL_PATH), ProximityGate(gate_db=TAP_GATE_DB))
    else:
        print("[TAP] Silero VAD model not found at %s - a turn ends on the second tap (or after %.0fs)."
              % (VAD_MODEL_PATH, TAP_MAX_SEC))
    buttons = buttons or ButtonListener()
    sock = sock or open_mic_socket()
    endpointer = UtteranceEndpointer()
    frame_bytes = SileroVAD.FRAME * 2
    ready_msg = "[TAP] 🔘 Ready - %s (Esc / R = new visitor)." % (
        "hold the button while speaking" if BUTTON_HOLD else "tap the button, then speak")
    print(ready_msg)
    if sys.stdin is not None and sys.stdin.isatty():
        print("[TAP] (In this terminal: ENTER = button, r + ENTER = new visitor)")

    turn = None
    prefill = None
    pending = b""
    ignore_until = 0.0
    turns = 0
    led.set("idle")

    def cancel(reason):
        if prefill:
            prefill.cancel()
        print("[TAP] %s" % reason)

    while max_turns is None or turns < max_turns:
        # 1. Button events
        result = (None, None)
        event = buttons.get()
        while event is not None and result[0] is None:
            if turn is not None:
                result = turn.button(event)
            elif event == "reset":
                reset_conversation()
                print("[TAP] 🔄 New visitor - conversation memory cleared.")
            elif event == "press":
                print("[TAP] 🎙️ Listening...")
                led.set("listening")
                prefill = SpeculativePrefill() if SPECULATIVE_PREFILL else None
                turn = TapRecorder(endpointer)
            event = buttons.get() if result[0] is None else None

        # 2. Microphone
        if result[0] is None:
            try:
                data = sock.recv(MIC_RECV_BYTES)
            except socket.timeout:
                data = b""
            if time.time() < max(ignore_until, playback_end_time() + ECHO_GUARD_SEC):
                pending = b""  # echo guard: never listen to the robot's own voice
                data = b""
            pending += data
            while len(pending) >= frame_bytes and result[0] is None:
                frame, pending = pending[:frame_bytes], pending[frame_bytes:]
                # Idle frames teach the gate the booth's background level
                prob = scorer(frame, endpointer.active) if scorer else 1.0
                if turn is not None:
                    result = turn.feed(frame, prob)
                    if result[0] == "start":
                        print("[TAP] 🗣️ Speech started")
                        result = (None, None)

        # 3. End of turn
        if result[0] == "cancel":
            cancel(result[1])
            if "Reset" in result[1]:
                reset_conversation()
                print("[TAP] 🔄 New visitor - conversation memory cleared.")
        elif result[0] == "end":
            led.set("thinking")
            process_turn(apply_agc(result[1]), time.time(), require_wake=False, prefill=prefill)
            turns += 1
        if result[0] is not None:
            turn = None
            prefill = None
            drain_socket(sock)  # stale audio, may contain the robot's own voice
            pending = b""
            if scorer:
                scorer.reset()
            endpointer.reset()
            buttons.clear()  # taps while the robot was talking don't start a new turn
            ignore_until = time.time() + ECHO_GUARD_SEC
            led.set("idle")
            print(ready_msg)

def apply_tap_mode_profile(env=os.environ):
    """Tap-to-talk = noisy booth: booth prompt (short answers, expects recognition mistakes) and the
    conversation is forgotten after 30 s, so the next visitor starts fresh. SYSTEM_PROMPT /
    CONVERSATION_MEMORY_SEC env vars still win."""
    global SYSTEM_PROMPT, CONVERSATION_MEMORY_SEC
    SYSTEM_PROMPT = env.get("SYSTEM_PROMPT", BOOTH_SYSTEM_PROMPT)
    CONVERSATION_MEMORY_SEC = float(env.get("CONVERSATION_MEMORY_SEC", "30"))

def select_mode(argv, env):
    """--tap / --vad flags, else ASSISTANT_MODE (ptt | vad | tap), else push-to-talk."""
    for flag, mode in (("--tap", "tap"), ("--vad", "vad"), ("--ptt", "ptt")):
        if flag in argv:
            return mode
    mode = env.get("ASSISTANT_MODE", "").strip().lower()
    return mode if mode in ("ptt", "vad", "tap") else "ptt"

MODE_NAMES = {"ptt": "Push-to-talk (ENTER to start / stop)",
              "vad": "Hands-free (Silero VAD%s)" % (", wake word" if WAKE_WORDS else ""),
              "tap": "Tap-to-talk for noisy rooms (button starts, VAD ends, booth prompt)"}

def main():
    global led, hands
    mode = select_mode(sys.argv[1:], os.environ)
    if mode == "tap":
        apply_tap_mode_profile()  # before warm-up, which caches the system prompt in llama-server
    led = LedIndicator()
    atexit.register(led.off)  # LED off when the assistant exits (Ctrl+C, or systemd stop via SIGTERM)
    hands = HandMotion()
    atexit.register(hands.relax_now)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("=" * 60)
    print("[SYSTEM] Unitree R1 Multimodal Assistant (Natural Continuous Flow)")
    print("[AUDIO] Non-Blocking Gapless Audio Daemon (/tmp/unitree_audio.sock)")
    print("[MODE] %s, mic: %s" % (MODE_NAMES[mode], MIC_SOURCE))
    if hands.enabled:
        sides = hands.connected()
        if sides:
            print("[HANDS] Connected: %s (voice gestures %s, talking hands %s)" % (
                " + ".join(sides), "on" if VOICE_GESTURES else "off", "on" if TALK_GESTURES else "off"))
            hands.relax()
        else:
            print("[HANDS] Not connected (start brainco_hand_server + unitree_hand_bridge, see app.sh) - no gestures.")
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

    if mode == "vad":
        run_vad_mode()
    elif mode == "tap":
        run_tap_mode()
    else:
        run_push_to_talk()

if __name__ == "__main__":
    main()
