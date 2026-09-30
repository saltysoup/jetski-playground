#!/usr/bin/env python3
"""
End-to-end test of the real assistant main() loop with injected speech.

The robot's mic array cancels the robot's own speaker output (echo cancellation), so the robot cannot
ask itself questions through the air. Instead, this test synthesizes each question with Riva TTS and
returns it from record_push_to_talk(), then runs the unmodified main() loop:
    injected audio -> Riva ASR -> MiniLM router -> head camera -> Gemma-4 -> Magpie TTS -> robot speaker

Requires services:  bash /home/unitree/app.sh --services-only
Usage: python3 tests/e2e_injected.py [path/to/test_vision_voice_assistant.py]
"""
import contextlib
import importlib.util
import io
import os
import sys
import warnings

warnings.filterwarnings("ignore")
import numpy as np  # noqa: E402

SCRIPT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "test_vision_voice_assistant.py")

QUESTIONS = [
    ("Can you tell me what I am holding?", "vision"),
    ("What is the capital of France?", "text"),
]

spec = importlib.util.spec_from_file_location("assistant", SCRIPT)
assistant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assistant)

pending = [q for q, _ in QUESTIONS]


def injected_record():
    text = pending.pop(0)
    resp = assistant.riva_tts.synthesize(text=text, voice_name="jason", language_code="en-US", sample_rate_hz=16000)
    print("\n[INJECT] Question audio: %r" % text)
    return np.frombuffer(resp.audio, dtype=np.int16).tobytes()


assistant.record_push_to_talk = injected_record

buf = io.StringIO()


class Tee(object):
    def write(self, s):
        sys.__stdout__.write(s)
        buf.write(s)

    def flush(self):
        sys.__stdout__.flush()


sys.stdin = io.StringIO("\n" * len(QUESTIONS) + "q\n")
with contextlib.redirect_stdout(Tee()):
    assistant.main()

log = buf.getvalue()
turns = log.split("[INJECT]")[1:]
results = []


def chk(name, ok):
    results.append(ok)
    print("[%s] %s" % ("PASS" if ok else "FAIL", name))


print("\n================ E2E CHECKS ================")
chk("MiniLM router active (not keyword fallback)", "MiniLM ONNX init failed" not in log and assistant.router_encoder.session is not None)
v, t = turns[0].lower(), turns[1].lower()
chk("Vision question transcribed", "you said:" in v and "holding" in v)
chk("Vision route triggered", "vision route triggered" in v)
chk("Head camera frame captured", "captured head eye dds frame" in v)
chk("Gemma multimodal answer", "sending multimodal query" in v and "jason:" in v)
chk("Text question transcribed", "capital" in t and "france" in t)
chk("Text-only route taken", "text-only route" in t)
chk("Gemma answered Paris", "paris" in t.split("jason:")[-1] if "jason:" in t else False)
chk("No errors", "[error]" not in log.lower() and "traceback" not in log.lower())
chk("Clean exit", "[EXIT]" in log)
print("E2E SUMMARY: %d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
