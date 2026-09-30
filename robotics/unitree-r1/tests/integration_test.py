#!/usr/bin/env python3
"""
On-robot integration test for the Unitree R1 voice & vision stack.
Requires services to be running:  bash /home/unitree/app.sh --services-only

Run:  python3 tests/integration_test.py   (silent: no speaker output)
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np
import requests

MCAST_GRP = "239.168.123.161"
MCAST_PORT = 5555
NET_INTERFACE_IP = "192.168.123.164"
AUDIO_SOCKET = "/tmp/unitree_audio.sock"
HEAD_CAMERA_SOCK = "/tmp/unitree_head_camera.sock"
HEAD_CAMERA_SNAP = "/dev/shm/unitree_head_camera.jpg"
RIVA_URI = "127.0.0.1:50051"
LLM_BASE = "http://127.0.0.1:8000"
SR = 16000

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" - " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- helpers
def record_mic(duration, out):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((MCAST_GRP, MCAST_PORT))
    mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(NET_INTERFACE_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(0.2)
    buf = bytearray()
    end = time.time() + duration
    while time.time() < end:
        try:
            buf.extend(sock.recv(4096))
        except socket.timeout:
            pass
    sock.close()
    if len(buf) % 2:
        buf = buf[:-1]
    out.append(np.frombuffer(bytes(buf), dtype=np.int16))


def send_pcm(pcm_int16):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(AUDIO_SOCKET)
    s.sendall(pcm_int16.astype(np.int16).tobytes())
    s.close()


def get_camera_frame():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(HEAD_CAMERA_SOCK)
    hdr = s.recv(4)
    size = struct.unpack("I", hdr)[0] if len(hdr) == 4 else 0
    data = bytearray()
    while len(data) < size:
        chunk = s.recv(min(65536, size - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    s.close()
    return bytes(data) if size and len(data) == size else None


def tone_active_span(rec, freq=1000.0, frame_ms=20):
    """Returns (onset_sec, duration_sec) of the test tone in a mic recording."""
    n = int(SR * frame_ms / 1000)
    frames = len(rec) // n
    if frames == 0:
        return None, 0.0
    x = rec[:frames * n].astype(np.float64).reshape(frames, n) * np.hanning(n)
    spec = np.abs(np.fft.rfft(x, axis=1))
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    band = (freqs > freq - 150) & (freqs < freq + 150)
    ratio = spec[:, band].sum(axis=1) / (spec.sum(axis=1) + 1e-9)
    energy = spec[:, band].sum(axis=1)
    noise = np.median(energy[:10]) + 1e-9  # first 200 ms is before the tone
    active = np.where((energy > 8 * noise) & (ratio > 0.3))[0]
    if len(active) == 0:
        return None, 0.0
    return active[0] * frame_ms / 1000.0, (active[-1] - active[0] + 1) * frame_ms / 1000.0


# ---------------------------------------------------------------- tests
def test_processes():
    for name in ("riva_server", "llama-server", "unitree_audio_daemon", "unitree_head_camera_daemon"):
        ok = subprocess.call(["pgrep", "-f", name], stdout=subprocess.DEVNULL) == 0
        check("process running: %s" % name, ok)


def test_llm_health():
    try:
        r = requests.get(LLM_BASE + "/health", timeout=3)
        check("llama-server /health", r.status_code == 200, "HTTP %d" % r.status_code)
    except Exception as e:
        check("llama-server /health", False, str(e))


def test_riva():
    import riva.client
    auth = riva.client.Auth(uri=RIVA_URI)
    tts = riva.client.SpeechSynthesisService(auth)
    asr = riva.client.ASRService(auth)
    phrase = "What do you see in front of you"
    t0 = time.time()
    resp = tts.synthesize(text=phrase, voice_name="jason", language_code="en-US", sample_rate_hz=SR)
    tts_ms = (time.time() - t0) * 1000
    audio = np.frombuffer(resp.audio, dtype=np.int16)
    check("Riva TTS synthesize", len(audio) > SR // 2, "%.2fs audio in %.0f ms" % (len(audio) / float(SR), tts_ms))

    cfg = riva.client.RecognitionConfig(encoding=riva.client.AudioEncoding.LINEAR_PCM, sample_rate_hertz=SR,
                                        language_code="en-US", max_alternatives=1,
                                        enable_automatic_punctuation=True)
    t0 = time.time()
    r = asr.offline_recognize(audio.tobytes(), cfg)
    asr_ms = (time.time() - t0) * 1000
    text = r.results[0].alternatives[0].transcript if r.results else ""
    want = set(phrase.lower().split())
    got = set(w.strip(".,?!").lower() for w in text.split())
    overlap = len(want & got) / float(len(want))
    check("Riva ASR round-trip (TTS->ASR)", overlap >= 0.7, "'%s' (%.0f%% words, %.0f ms)" % (text, overlap * 100, asr_ms))
    return audio


def test_camera():
    frame = None
    try:
        frame = get_camera_frame()
    except Exception as e:
        check("camera daemon socket", False, str(e))
        return None
    ok = frame is not None and frame[:2] == b"\xff\xd8"
    check("camera daemon returns fresh JPEG", ok, "%d bytes" % (len(frame) if frame else 0))
    if os.path.exists(HEAD_CAMERA_SNAP):
        age = time.time() - os.path.getmtime(HEAD_CAMERA_SNAP)
        check("snapshot in /dev/shm is fresh", age < 1.0, "age %.2fs" % age)
    else:
        check("snapshot in /dev/shm is fresh", False, "missing")
    old_snap = "/tmp/unitree_head_camera.jpg"
    time.sleep(0.2)
    check("no snapshot writes to /tmp (flash)", not os.path.exists(old_snap)
          or time.time() - os.path.getmtime(old_snap) > 5)

    # SIGPIPE robustness: clients that disconnect immediately must not kill the daemon
    crashed_after = None
    for i in range(50):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(HEAD_CAMERA_SOCK)
        except socket.error:
            crashed_after = i
            break
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
    time.sleep(0.5)
    alive = subprocess.call(["pgrep", "-f", "bin/unitree_head_camera_daemon"], stdout=subprocess.DEVNULL) == 0
    check("camera daemon survives 50 abrupt disconnects (SIGPIPE)", alive and crashed_after is None,
          "" if crashed_after is None else "daemon died after %d disconnects" % crashed_after)
    try:
        check("camera daemon still serving after disconnects", get_camera_frame() is not None)
    except Exception as e:
        check("camera daemon still serving after disconnects", False, str(e))
    return frame


def _stream_llm(content, max_tokens=80):
    payload = {"model": "gemma", "max_tokens": max_tokens, "temperature": 0.1, "stream": True,
               "messages": [{"role": "system", "content": "You are a robot named Jason. Answer in under 35 words."},
                            {"role": "user", "content": content}]}
    t0 = time.time()
    first = None
    text = ""
    r = requests.post(LLM_BASE + "/v1/chat/completions", json=payload, stream=True, timeout=60)
    for line in r.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        d = line[6:]
        if d == b"[DONE]":
            break
        try:
            c = json.loads(d)["choices"][0].get("delta", {}).get("content", "")
        except Exception:
            c = ""
        if c:
            if first is None:
                first = time.time() - t0
            text += c
    return r.status_code, text.strip(), first, time.time() - t0


def test_llm(frame):
    code, text, ttft, total = _stream_llm([{"type": "text", "text": "What is the capital of France?"}])
    check("Gemma text query", code == 200 and "paris" in text.lower(),
          "'%s' (TTFT %.2fs, total %.2fs)" % (text, ttft or -1, total))
    if frame is None:
        check("Gemma vision query", False, "no camera frame")
        return
    import cv2
    img = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
    small = cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)
    b64 = base64.b64encode(cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 75])[1]).decode()
    cv2.imwrite("/home/unitree/integration_test_frame.jpg", img)
    code, text, ttft, total = _stream_llm([
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        {"type": "text", "text": "Describe what you see in one sentence."}])
    check("Gemma vision query (head camera)", code == 200 and len(text.split()) >= 3,
          "'%s' (TTFT %.2fs, total %.2fs)" % (text, ttft or -1, total))


def test_mic():
    out = []
    record_mic(2.0, out)
    rec = out[0]
    rate = len(rec) / 2.0
    check("mic multicast stream receiving", rate > SR * 0.7, "%.0f samples/s (expect ~%d)" % (rate, SR))


def test_router_loads_in_real_script():
    """The MiniLM router must load when the real assistant imports riva.client (import-order regression)."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "test_vision_voice_assistant.py")
    code = ("import importlib.util,sys; s=importlib.util.spec_from_file_location('a', %r); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "print('ROUTER_LOADED=%%s' %% (m.router_encoder.session is not None))" % script)
    out = subprocess.run([sys.executable, "-W", "ignore", "-c", code], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, timeout=120).stdout.decode(errors="replace")
    ok = "ROUTER_LOADED=True" in out
    warn = [l for l in out.splitlines() if "MiniLM ONNX init failed" in l]
    check("MiniLM router loads in real script (with riva imported)", ok, warn[0][:120] if warn else "")


def main():
    # Note: this test is silent. Speaker->mic tone tests were removed: the mic array suppresses the robot's
    # own speaker output (echo cancellation), so they were unreliable, and loud. Audio tail truncation was
    # validated by a listening A/B instead (see tests/README or the commit message).
    test_processes()
    test_llm_health()
    test_mic()
    test_riva()
    test_router_loads_in_real_script()
    frame = test_camera()
    test_llm(frame)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 60)
    print("SUMMARY: %d/%d checks passed" % (passed, len(RESULTS)))
    for name, ok, detail in RESULTS:
        if not ok:
            print("  FAIL: %s %s" % (name, detail))
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
