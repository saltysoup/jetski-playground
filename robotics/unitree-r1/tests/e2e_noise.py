#!/usr/bin/env python3
"""
Noise benchmark for the listening front-end (silent: nothing is played on the robot speaker).

Mixes synthesized questions with real crowd noise (DEMAND corpus: PCAFETER = cafeteria, PRESTO = restaurant)
at several signal-to-noise ratios and runs the same frame pipeline as the assistant:
    Silero VAD -> proximity gate (GATE_DB) -> endpointer        (hands-free / --vad)
    button press -> TapRecorder (VAD ends the turn)             (tap-to-talk / --tap)
then sends the captured audio to Riva ASR.

Reports per noise / SNR / gate:
  vad found   questions whose start AND natural end were detected (not cut by the max-length cap)
  vad end     median delay from end of speech to end-of-utterance decision
  vad wake    wake word ("Jason, ...") recognized in the captured audio
  vad WER     word error rate of the captured question
  false/min   utterances triggered by noise alone (per minute of noise-only audio)
  tap ok      tap turns that ended by themselves (not via timeout / cap)
  tap end     median end delay in tap mode
  tap WER     word error rate in tap mode

Requires riva_server (ASR + TTS). Noise WAVs: /home/unitree/robot_assets/models/noise/*.wav (16 kHz mono).
Usage: python3 tests/e2e_noise.py                    # all noise files, SNR clean/15/10/5/0, GATE_DB 0/6/10
       SNRS=10,5 GATES=0,6 python3 tests/e2e_noise.py
"""
import glob
import importlib.util
import os
import re
import sys
import warnings

warnings.filterwarnings("ignore")
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "test_vision_voice_assistant.py")
spec = importlib.util.spec_from_file_location("assistant", SCRIPT)
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

SR = 16000
FRAME = A.SileroVAD.FRAME
FRAME_SEC = FRAME / float(SR)
MIC_LEVEL = float(os.getenv("MIC_LEVEL", "0.35"))
NOISE_DIR = os.getenv("NOISE_DIR", "/home/unitree/robot_assets/models/noise")
SNRS = [s.strip() for s in os.getenv("SNRS", "clean,15,10,5,0").split(",")]
GATES = [float(g) for g in os.getenv("GATES", "0,6,10").split(",")]
NOISE_ONLY_SEC = float(os.getenv("NOISE_ONLY_SEC", "60"))

QUESTIONS = [
    "What is the capital of France?",
    "Can you tell me a joke about robots?",
    "What do you see in front of you?",
    "How tall are you?",
    "What is the weather usually like in London?",
    "Who made you and what can you do?",
]


def synth(text):
    a = np.frombuffer(A.riva_tts.synthesize(text=text, voice_name="jason", language_code="en-US",
                                            sample_rate_hz=SR).audio, dtype=np.int16).astype(np.float32)
    a = A.trim_silence_padding(a.astype(np.int16)).astype(np.float32) if hasattr(A, "trim_silence_padding") else a
    return a * MIC_LEVEL


def active_rms(x):
    """RMS over the loud part of the signal (speech level, ignoring pauses)."""
    frames = x[:len(x) // FRAME * FRAME].reshape(-1, FRAME)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    loud = rms[rms > 0.1 * rms.max()]
    return float(np.sqrt(np.mean(loud ** 2)))


def norm_words(t):
    return re.sub(r"[^a-z0-9' ]", " ", t.lower().replace("jason", " ")).split()


def wer(ref, hyp):
    r, h = norm_words(ref), norm_words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / float(max(1, len(r)))


def to_int16(x):
    return np.clip(x, -32767, 32767).astype(np.int16)


class Scene(object):
    """lead-in noise, then each question followed by noise; remembers where each question is."""
    def __init__(self, clips, noise, snr, lead=6.0, gap=4.0):
        speech_rms = np.mean([active_rms(c) for c in clips])
        if snr == "clean":
            gain = 0.0
        else:
            gain = speech_rms / (np.sqrt(np.mean(noise ** 2)) * 10 ** (float(snr) / 20.0))
        parts, self.spans, pos = [], [], 0
        parts.append(np.zeros(int(lead * SR), np.float32)); pos += parts[-1].size
        for c in clips:
            self.spans.append((pos, pos + c.size))
            parts.append(c); pos += c.size
            parts.append(np.zeros(int(gap * SR), np.float32)); pos += parts[-1].size
        speech = np.concatenate(parts)
        bed = np.resize(noise, speech.size) * gain
        self.audio = to_int16(speech + bed + np.random.RandomState(1).randn(speech.size) * 30)
        self.noise_only = to_int16(np.resize(np.roll(noise, SR * 37), int(NOISE_ONLY_SEC * SR)) * gain
                                   + np.random.RandomState(2).randn(int(NOISE_ONLY_SEC * SR)) * 30)


def frames_of(audio):
    n = len(audio) // FRAME
    for i in range(n):
        yield i, audio[i * FRAME:(i + 1) * FRAME].tobytes()


def new_scorer(gate_db):
    vad = A.SileroVAD(A.VAD_MODEL_PATH)
    return A.VoiceFrameScorer(vad, A.ProximityGate(gate_db=gate_db))


def run_vad(scene, gate_db, refs):
    """Hands-free pipeline over the whole scene. Returns per-question results."""
    scorer, ep = new_scorer(gate_db), A.UtteranceEndpointer()
    utts = []  # (start_frame, end_frame, audio, forced)
    start = None
    for i, frame in frames_of(scene.audio):
        ev, payload = ep.process(frame, scorer(frame, ep.active))
        if ev == "start":
            start = i
        elif ev == "end":
            forced = (i - start) >= ep.max_frames - 1
            utts.append((start, i, payload, forced))
    results = []
    for (s0, s1), ref in zip(scene.spans, refs):
        f0, f1 = s0 // FRAME, s1 // FRAME
        hit = [u for u in utts if u[0] <= f1 and u[1] >= f0]
        if not hit:
            results.append(dict(found=False, wake=False, wer=1.0, delay=None, forced=False))
            continue
        u = hit[0]
        text = A.transcribe_audio_bytes(A.apply_agc(u[2]), verbose=False)
        results.append(dict(found=not u[3], forced=u[3], wake=A.match_wake_word(text, ["jason", "jayson", "jaysen", "jaison"])[0], wer=wer(ref, text),
                            delay=(u[1] - f1) * FRAME_SEC, text=text))
    return results


def run_noise_only(scene, gate_db):
    scorer, ep = new_scorer(gate_db), A.UtteranceEndpointer()
    n = 0
    for _, frame in frames_of(scene.noise_only):
        if ep.process(frame, scorer(frame, ep.active))[0] == "end":
            n += 1
    return n / (NOISE_ONLY_SEC / 60.0)


def run_tap(scene, gate_db, refs):
    """Tap mode: the gate learns from the lead-in, then a press 0.3 s before each question."""
    scorer = new_scorer(gate_db)
    ep = A.UtteranceEndpointer()
    starts = {s0 // FRAME - int(0.3 / FRAME_SEC): k for k, (s0, _) in enumerate(scene.spans)}
    rec, k, results = None, None, [None] * len(refs)
    for i, frame in frames_of(scene.audio):
        if rec is None and i in starts:
            k, rec = starts[i], A.TapRecorder(ep, hold=False)
        prob = scorer(frame, ep.active)
        if rec is None:
            continue
        ev, payload = rec.feed(frame, prob)
        if ev in ("end", "cancel"):
            f1 = scene.spans[k][1] // FRAME
            if ev == "end":
                natural = len(rec.frames) < rec.max_frames
                text = A.transcribe_audio_bytes(A.apply_agc(payload), verbose=False)
                results[k] = dict(ok=natural, wer=wer(refs[k], text), delay=(i - f1) * FRAME_SEC, text=text)
            else:
                results[k] = dict(ok=False, wer=1.0, delay=None, text="")
            rec = None
            scorer.reset()
    return [r or dict(ok=False, wer=1.0, delay=None, text="") for r in results]


def med(xs):
    xs = [x for x in xs if x is not None]
    return "%.2fs" % float(np.median(xs)) if xs else "  -  "


def main():
    if not os.path.exists(A.VAD_MODEL_PATH):
        sys.exit("Silero model missing: %s" % A.VAD_MODEL_PATH)
    noises = sorted(glob.glob(os.path.join(NOISE_DIR, "*.wav")))
    if not noises:
        sys.exit("No noise WAVs in %s" % NOISE_DIR)
    print("[NOISE] Synthesizing %d questions..." % len(QUESTIONS))
    wake_clips = [synth("Jason, " + q) for q in QUESTIONS]
    tap_clips = [synth(q) for q in QUESTIONS]
    n = len(QUESTIONS)
    print("\n%-9s %-5s %-4s | %-9s %-7s %-8s %-7s %-9s | %-6s %-7s %-7s" % (
        "noise", "snr", "gate", "vad found", "vad end", "vad wake", "vad WER", "false/min",
        "tap ok", "tap end", "tap WER"))
    print("-" * 104)
    for path in noises:
        noise, sr = sf.read(path, dtype="float32")
        if noise.ndim > 1:
            noise = noise[:, 0]
        assert sr == SR, "%s must be 16 kHz" % path
        noise = noise * 32768.0
        name = os.path.basename(path).split("_")[0][:9]
        for snr in SNRS:
            wake_scene = Scene(wake_clips, noise, snr)
            tap_scene = Scene(tap_clips, noise, snr)
            for g in GATES:
                v = run_vad(wake_scene, g, QUESTIONS)
                fpm = run_noise_only(wake_scene, g) if snr != "clean" else 0.0
                t = run_tap(tap_scene, g, QUESTIONS)
                print("%-9s %-5s %-4.0f | %-9s %-7s %-8s %-7.2f %-9.1f | %-6s %-7s %-7.2f" % (
                    name, snr, g,
                    "%d/%d" % (sum(r["found"] for r in v), n), med([r["delay"] for r in v if r["found"]]),
                    "%d/%d" % (sum(r["wake"] for r in v), n), np.mean([r["wer"] for r in v]), fpm,
                    "%d/%d" % (sum(r["ok"] for r in t), n), med([r["delay"] for r in t if r["ok"]]),
                    np.mean([r["wer"] for r in t])))
                sys.stdout.flush()
                if os.getenv("NOISE_VERBOSE") == "1":
                    for r in t:
                        print("      tap: %r" % r.get("text"))


if __name__ == "__main__":
    main()
