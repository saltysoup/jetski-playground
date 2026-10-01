#!/usr/bin/env python3
"""
End-to-end test of tap-to-talk (--tap) in crowd noise, with injected speech and simulated button presses.

The mic socket is replaced by a fake that streams cafeteria noise (DEMAND PCAFETER) in real time with
synthesized questions mixed in at SNR_DB (default 10 dB); the button is a fake ButtonListener whose presses
are timed to the audio. Everything else is real:
    fake mic -> Silero + proximity gate -> TapRecorder -> Riva ASR -> router -> Gemma-4 -> Magpie -> speaker
    (+ head LED: purple while listening, green while talking)

Script:
  1. press, "What is the capital of France?"        -> answered (Paris)
  2. press, "And what about Germany?"                -> answered from conversation memory (Berlin)
  3. reset key                                       -> memory cleared ("new visitor")
  4. press, nobody speaks (crowd only)               -> cancelled after TAP_NO_SPEECH_SEC
  5. press, "What do you see in front of you?"       -> vision route with the speculative prefill

Requires services:  bash /home/unitree/app.sh --services-only     (the robot speaks the answers)
Usage: python3 tests/e2e_tap_injected.py         SNR_DB=5 python3 tests/e2e_tap_injected.py
"""
import contextlib
import glob
import importlib.util
import io
import os
import queue
import sys
import time
import warnings

warnings.filterwarnings("ignore")
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "test_vision_voice_assistant.py")
spec = importlib.util.spec_from_file_location("assistant", SCRIPT)
assistant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assistant)

SR = 16000
PACKET = 320
MIC_LEVEL = float(os.getenv("MIC_LEVEL", "0.35"))
SNR_DB = float(os.getenv("SNR_DB", "10"))
NOISE_FILE = os.getenv("NOISE_FILE") or (sorted(glob.glob("/home/unitree/robot_assets/models/noise/pcafeter*.wav"))
                                         or [None])[0]


def synth(text):
    a = np.frombuffer(assistant.riva_tts.synthesize(text=text, voice_name="jason", language_code="en-US",
                                                    sample_rate_hz=SR).audio, dtype=np.int16)
    return a.astype(np.float32) * MIC_LEVEL


def active_rms(x):
    f = x[:len(x) // 512 * 512].reshape(-1, 512)
    rms = np.sqrt(np.mean(f ** 2, axis=1))
    return float(np.sqrt(np.mean(rms[rms > 0.1 * rms.max()] ** 2)))


# (label, idle seconds before the event, event, text spoken after the press, seconds after)
SEGMENTS = [
    ("capital", 3.0, "press", "What is the capital of France?", 2.5),
    ("follow-up", 1.0, "press", "And what about Germany?", 2.5),
    ("reset", 1.0, "reset", None, 1.0),
    ("no speech", 1.0, "press", None, 6.5),
    ("vision", 1.0, "press", "What do you see in front of you?", 2.5),
]
print("[TEST] Synthesizing questions and mixing crowd noise (%s, SNR %.0f dB)..." % (
    os.path.basename(NOISE_FILE or "none"), SNR_DB))
speech = {t: synth(t) for _, _, _, t, _ in SEGMENTS if t}
if NOISE_FILE:
    noise, sr = sf.read(NOISE_FILE, dtype="float32")
    noise = (noise if noise.ndim == 1 else noise[:, 0]) * 32768.0
    noise_gain = np.mean([active_rms(s) for s in speech.values()]) / (
        np.sqrt(np.mean(noise ** 2)) * 10 ** (SNR_DB / 20.0))
else:
    noise, noise_gain = np.random.RandomState(0).randn(SR * 10) * 30, 1.0
noise_pos = [0]


def noise_chunk(n):
    idx = (np.arange(n) + noise_pos[0]) % len(noise)
    noise_pos[0] += n
    return noise[idx] * noise_gain


CLIPS, EVENT_AT = [], []
for label, idle, event, text, after in SEGMENTS:
    body = np.concatenate([np.zeros(int(0.4 * SR)), speech[text]]) if text else np.zeros(0)
    clean = np.concatenate([np.zeros(int(idle * SR)), body, np.zeros(int(after * SR))])
    CLIPS.append(np.clip(clean + noise_chunk(len(clean)), -32767, 32767).astype(np.int16))
    EVENT_AT.append(int(idle * SR))


class Done(Exception):
    pass


class FakeButtons(object):
    def __init__(self):
        self.events = queue.Queue()

    def get(self, timeout=0.0):
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None

    def clear(self):
        while self.get() is not None:
            pass


BUTTONS = FakeButtons()


class FakeMicSocket(object):
    """Plays CLIPS in real time; fires each segment's button event when its audio reaches the event time.
    A clip starts on the first recv() after the previous one ended (answer time doesn't eat audio)."""
    def __init__(self):
        self.clip, self.pos, self.t_start, self.fired = -1, 0, None, False
        self.blocking, self.timeout = True, 0.1

    def _advance(self):
        if self.clip < 0 or self.pos >= len(CLIPS[self.clip]):
            self.clip += 1
            if self.clip >= len(CLIPS):
                raise Done()
            self.pos, self.t_start, self.fired = 0, time.time(), False
            print("\n[TEST] >>> segment %d: %s" % (self.clip + 1, SEGMENTS[self.clip][0]))

    def recv(self, n):
        self._advance()
        due = self.t_start + float(self.pos) / SR
        wait = due - time.time()
        if wait > 0:
            if not self.blocking:
                raise BlockingIOError()
            time.sleep(wait)
        if not self.fired and self.pos >= EVENT_AT[self.clip]:
            self.fired = True
            BUTTONS.events.put(SEGMENTS[self.clip][2])
        pkt = CLIPS[self.clip][self.pos:self.pos + PACKET]
        self.pos += PACKET
        return pkt.tobytes()

    def setblocking(self, flag):
        self.blocking = bool(flag)

    def settimeout(self, t):
        self.blocking, self.timeout = True, t

    def gettimeout(self):
        return self.timeout

    def close(self):
        pass


assistant.open_mic_socket = lambda: FakeMicSocket()
assistant.ButtonListener = lambda *a, **k: BUTTONS
buf = io.StringIO()


class Tee(object):
    def write(self, s):
        sys.__stdout__.write(s)
        buf.write(s)

    def flush(self):
        sys.__stdout__.flush()


sys.argv = [SCRIPT, "--tap"]
with contextlib.redirect_stdout(Tee()):
    try:
        assistant.main()
    except Done:
        assistant.wait_for_all_tts_to_finish()
        print("\n[TEST] all segments played")

log = buf.getvalue()
segs = log.split("[TEST] >>> segment")[1:]
results = []


def chk(name, ok):
    results.append(ok)
    print("[%s] %s" % ("PASS" if ok else "FAIL", name))


def low(i):
    return segs[i].lower() if i < len(segs) else ""


print("\n================ TAP E2E CHECKS (crowd noise, SNR %.0f dB) ================" % SNR_DB)
chk("All 5 segments played", len(segs) == 5)
chk("1: tap + answer (Paris)", "speech started" in low(0) and "paris" in low(0).split("jason:")[-1])
chk("2: follow-up answered from memory (Berlin)", "berlin" in low(1).split("jason:")[-1])
chk("3: reset clears the conversation", "new visitor" in low(2))
chk("4: crowd-only tap cancelled (no answer)", "no speech heard" in low(3) and "jason:" not in low(3))
chk("5: vision request answered", "vision route triggered" in low(4) and "sending multimodal query" in low(4))
chk("Vision used the speculative prefill", "using frame captured when you started talking" in low(4))
chk("LED reachable", "[led] can't reach" not in log.lower())
chk("No errors", "[error]" not in log.lower() and "traceback" not in log.lower())
print("\n".join(l for l in log.splitlines() if l.startswith("[TIMING]")))
print("TAP E2E SUMMARY: %d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
