#!/usr/bin/env python3
"""
End-to-end test of the hands-free (--vad) loop with injected speech.

The robot's mic array cancels the robot's own speaker output, so speech can't be played to it through the
air. Instead, the multicast mic socket is replaced by a fake that streams synthesized utterances in real
time (20 ms packets, like the robot). Everything else is real:
    fake mic -> Silero VAD -> endpointer -> wake word -> Riva ASR -> router -> Gemma-4 -> Magpie -> speaker

Script (FOLLOW_UP_SEC is set to 4 s for the test):
  1. "Jason, what is the capital of France?"   -> answered (wake word)
  2. "And what about Germany?"                 -> answered (inside follow-up window, no wake word)
  3. <wait for follow-up window to expire>
  4. "The meeting is at three o'clock."        -> ignored (not addressed)
  5. "Hey Jason, what do you see?"             -> answered via vision route

Requires services:  bash /home/unitree/app.sh --services-only
Usage: python3 tests/e2e_vad_injected.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")
os.environ["FOLLOW_UP_SEC"] = "4"
import numpy as np  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "test_vision_voice_assistant.py")
spec = importlib.util.spec_from_file_location("assistant", SCRIPT)
assistant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assistant)

MIC_LEVEL = float(os.getenv("MIC_LEVEL", "0.35"))  # TTS is much louder than a person at the robot's mic
SR = 16000
PACKET = 320  # samples per 20 ms packet


def synth(text):
    a = np.frombuffer(assistant.riva_tts.synthesize(text=text, voice_name="jason", language_code="en-US",
                                                    sample_rate_hz=SR).audio, dtype=np.int16)
    return (a.astype(np.float32) * MIC_LEVEL).astype(np.int16)


def silence(sec):
    rng = np.random.RandomState(0)
    return (rng.randn(int(sec * SR)) * 30).astype(np.int16)  # faint room noise


SEGMENTS = [
    ("Jason, what is the capital of France?", 2.0),
    ("And what about Germany?", 1.5),
    (None, 5.0),  # let the 4 s follow-up window expire
    ("The meeting is at three o'clock.", 1.5),
    ("Hey Jason, what do you see?", 2.5),
]
print("[TEST] Synthesizing injected utterances...")
CLIPS = []
for text, lead in SEGMENTS:
    CLIPS.append(np.concatenate([silence(lead), synth(text) if text else silence(0.0), silence(1.5)]))


class Done(Exception):
    pass


class FakeMicSocket(object):
    """Plays CLIPS one after another in real time. A clip starts on the first recv() after the previous
    clip ended, so time spent answering (when nobody reads the mic) doesn't eat the next utterance."""
    def __init__(self):
        self.clip = -1
        self.pos = 0
        self.t_start = None
        self.blocking = True
        self.timeout = 0.1

    def _advance(self):
        if self.clip < 0 or self.pos >= len(CLIPS[self.clip]):
            self.clip += 1
            if self.clip >= len(CLIPS):
                raise Done()
            self.pos = 0
            self.t_start = time.time()
            print("\n[TEST] >>> mic segment %d: %r" % (self.clip + 1, SEGMENTS[self.clip][0]))

    def recv(self, n):
        self._advance()
        due = self.t_start + float(self.pos) / SR
        wait = due - time.time()
        if wait > 0:
            if not self.blocking:
                raise BlockingIOError()
            time.sleep(wait)
        pkt = CLIPS[self.clip][self.pos:self.pos + PACKET]
        self.pos += PACKET
        return pkt.tobytes()

    def setblocking(self, flag):
        self.blocking = bool(flag)

    def settimeout(self, t):
        self.blocking = True
        self.timeout = t

    def gettimeout(self):
        return self.timeout

    def close(self):
        pass


assistant.open_mic_socket = lambda: FakeMicSocket()
buf = io.StringIO()


class Tee(object):
    def write(self, s):
        sys.__stdout__.write(s)
        buf.write(s)

    def flush(self):
        sys.__stdout__.flush()


sys.argv = [SCRIPT, "--vad"]
with contextlib.redirect_stdout(Tee()):
    try:
        assistant.main()
    except Done:
        assistant.wait_for_all_tts_to_finish()
        print("\n[TEST] all segments played")

log = buf.getvalue()
segs = log.split("[TEST] >>> mic segment")[1:]
results = []


def chk(name, ok):
    results.append(ok)
    print("[%s] %s" % ("PASS" if ok else "FAIL", name))


def low(i):
    return segs[i].lower() if i < len(segs) else ""


print("\n================ VAD E2E CHECKS ================")
chk("All 5 segments played", len(segs) == 5)
chk("1: wake word + answer (Paris)", "speech started" in low(0) and "paris" in low(0).split("jason:")[-1])
chk("2: follow-up answered without wake word (Berlin)", "berlin" in low(1).split("jason:")[-1])
chk("3: silence produced no turn", "speech started" not in low(2))
chk("4: not addressed -> ignored", "not addressed" in low(3) and "jason:" not in low(3))
chk("5: vision request answered", "vision route triggered" in low(4) and "sending multimodal query" in low(4))
chk("Vision used the speculative prefill", "using frame captured when you started talking" in low(4))
chk("No errors", "[error]" not in log.lower() and "traceback" not in log.lower())
print("\n".join(l for l in log.splitlines() if l.startswith("[TIMING]")))
print("VAD E2E SUMMARY: %d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
