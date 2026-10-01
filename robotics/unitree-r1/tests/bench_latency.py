#!/usr/bin/env python3
"""
Latency benchmark for the R1 voice & vision stack (silent: no speaker output).
Requires services:  bash /home/unitree/app.sh --services-only

Measures: Riva ASR (offline + streaming), Riva TTS (offline + streaming first chunk),
llama-server text/vision time-to-first-token, and multimodal prompt-cache reuse.
Run:  python3 tests/bench_latency.py
"""
import base64
import json
import statistics
import struct
import socket
import time
import warnings

warnings.filterwarnings("ignore")
import numpy as np  # noqa: E402
import onnxruntime  # noqa: E402,F401  (must precede riva import, see assistant)
import requests  # noqa: E402
import riva.client  # noqa: E402

SR = 16000
LLM = "http://127.0.0.1:8000/v1/chat/completions"
SYSTEM = ("Your name is Jason. Don't use acronyms. You are a robot. For time or numbers spell them out in "
          "letters. Speak in smooth, complete sentences. Response must be under 35 words.")

auth = riva.client.Auth(uri="127.0.0.1:50051")
tts = riva.client.SpeechSynthesisService(auth)
asr = riva.client.ASRService(auth)


def ms(xs):
    return "median %4.0f ms (min %4.0f, max %4.0f, n=%d)" % (statistics.median(xs) * 1000, min(xs) * 1000,
                                                            max(xs) * 1000, len(xs))


def synth(text):
    return np.frombuffer(tts.synthesize(text=text, voice_name="jason", language_code="en-US",
                                        sample_rate_hz=SR).audio, dtype=np.int16)


print("=" * 70)
print("TTS (Riva Magpie)")
for text in ["Hmm.", "Let me take a look.", "The capital of France is Paris, a famous and beautiful city."]:
    ts = []
    for _ in range(3):
        t0 = time.time()
        a = synth(text)
        ts.append(time.time() - t0)
    print("  offline  %-62r %s  -> %.2fs audio (RTF %.2f)" % (text, ms(ts), len(a) / float(SR),
                                                                 statistics.median(ts) / (len(a) / float(SR))))
text = "The capital of France is Paris, a famous and beautiful city."
try:
    ts_first, ts_total = [], []
    for _ in range(3):
        t0 = time.time()
        first = None
        n = 0
        for resp in tts.synthesize_online(text=text, voice_name="jason", language_code="en-US", sample_rate_hz=SR):
            if resp.audio and first is None:
                first = time.time() - t0
            n += len(resp.audio)
        ts_first.append(first if first is not None else float("nan"))
        ts_total.append(time.time() - t0)
    print("  STREAMING first chunk  %s | total %s | %.2fs audio" % (ms(ts_first), ms(ts_total), n / 2.0 / SR))
except Exception as e:
    print("  STREAMING synthesize_online NOT SUPPORTED: %s" % str(e).splitlines()[0][:120])

print("=" * 70)
print("ASR (Riva Nemotron)")
question = synth("Can you tell me what I am holding in my hand right now?")
cfg = riva.client.RecognitionConfig(encoding=riva.client.AudioEncoding.LINEAR_PCM, sample_rate_hertz=SR,
                                    language_code="en-US", max_alternatives=1, enable_automatic_punctuation=True)
ts = []
for _ in range(5):
    t0 = time.time()
    r = asr.offline_recognize(question.tobytes(), cfg)
    ts.append(time.time() - t0)
txt = r.results[0].alternatives[0].transcript if r.results else ""
print("  offline  %.2fs clip  %s  %r" % (len(question) / float(SR), ms(ts), txt))

try:
    scfg = riva.client.StreamingRecognitionConfig(config=cfg, interim_results=True)
    chunk = int(0.1 * SR)
    for paced in (False, True):
        lat = []
        for _ in range(2):
            state = {"t_last": None, "final": "", "t_final": None}

            def gen():
                for i in range(0, len(question), chunk):
                    if paced:
                        time.sleep(0.1)
                    yield question[i:i + chunk].tobytes()
                state["t_last"] = time.time()
            for resp in asr.streaming_response_generator(audio_chunks=gen(), streaming_config=scfg):
                for res in resp.results:
                    if res.is_final and res.alternatives:
                        state["final"] += res.alternatives[0].transcript
                        state["t_final"] = time.time()
            if state["t_final"] and state["t_last"]:
                lat.append(max(0.0, state["t_final"] - state["t_last"]))
        label = "real-time paced" if paced else "as fast as possible"
        print("  STREAMING (%s): final transcript %s after last audio chunk  %r" % (
            label, ms(lat) if lat else "n/a", state["final"]))
except Exception as e:
    print("  STREAMING recognize NOT SUPPORTED: %s" % str(e).splitlines()[0][:120])

print("=" * 70)
print("LLM (llama-server Gemma-4)")


def chat(content, max_tokens=40, stream=True):
    payload = {"model": "gemma", "max_tokens": max_tokens, "temperature": 0.1, "stream": stream,
               "cache_prompt": True,
               "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]}
    t0 = time.time()
    if not stream:
        j = requests.post(LLM, json=payload, timeout=60).json()
        return time.time() - t0, j.get("timings", {}), j["choices"][0]["message"]["content"]
    ttft, text, timings = None, "", {}
    with requests.post(LLM, json=payload, stream=True, timeout=60) as r:
        for line in r.iter_lines():
            if not line.startswith(b"data: ") or line[6:] == b"[DONE]":
                continue
            j = json.loads(line[6:])
            timings = j.get("timings", timings)
            c = j["choices"][0].get("delta", {}).get("content") if j.get("choices") else None
            if c:
                if ttft is None:
                    ttft = time.time() - t0
                text += c
    return ttft, timings, text


def frame_b64(size=(256, 256)):
    """size: (w, h) tuple, or None for the full-resolution frame."""
    import cv2
    for _ in range(20):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect("/tmp/unitree_head_camera.sock")
        n = struct.unpack("I", s.recv(4))[0]
        d = b""
        while len(d) < n:
            chunk = s.recv(n - len(d))
            if not chunk:
                break
            d += chunk
        s.close()
        if n and len(d) == n:
            break
        time.sleep(0.1)  # daemon returns size 0 while its frame is stale
    else:
        raise RuntimeError("head camera returned no fresh frame")
    img = cv2.imdecode(np.frombuffer(d, np.uint8), cv2.IMREAD_COLOR)
    if size:
        img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    return base64.b64encode(cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 75])[1]).decode()


ts = [chat([{"type": "text", "text": q}])[0] for q in ["What is the capital of France?", "Tell me a joke.",
                                                         "How are you today?"]]
print("  text TTFT                              %s" % ms(ts))

img = frame_b64()
im = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}}
t1, tm1, _ = chat([im, {"type": "text", "text": "Describe what you see in one sentence."}])
print("  vision TTFT, new image                 %4.0f ms  prompt_n=%s prompt_ms=%s" % (
    t1 * 1000, tm1.get("prompt_n"), tm1.get("prompt_ms")))
t2, tm2, _ = chat([im, {"type": "text", "text": "What am I holding?"}])
print("  vision TTFT, same image, new question  %4.0f ms  prompt_n=%s prompt_ms=%s  <- image cache reuse?" % (
    t2 * 1000, tm2.get("prompt_n"), tm2.get("prompt_ms")))

# Speculative prefill: image only, then the real question
img2 = frame_b64()
im2 = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img2}}
t0 = time.time()
chat([im2], max_tokens=1, stream=False)
prefill = time.time() - t0
t3, tm3, _ = chat([im2, {"type": "text", "text": "What color is the floor?"}])
print("  speculative prefill (image only)       %4.0f ms" % (prefill * 1000))
print("  vision TTFT after prefill              %4.0f ms  prompt_n=%s prompt_ms=%s" % (
    t3 * 1000, tm3.get("prompt_n"), tm3.get("prompt_ms")))
t4, tm4, _ = chat([{"type": "text", "text": "What is two plus two?"}])
print("  text TTFT right after vision           %4.0f ms  prompt_n=%s" % (t4 * 1000, tm4.get("prompt_n")))

for size in ((256, 256), (448, 252), (512, 288), (768, 432), None):
    b = frame_b64(size)
    t5, tm5, ans = chat([{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}},
                         {"type": "text", "text": "Describe what you see in one sentence."}])
    print("  vision TTFT, %-8s image             %4.0f ms  prompt_n=%s  %r" % (
        "%dx%d" % size if size else "full", t5 * 1000, tm5.get("prompt_n"), ans[:70]))
