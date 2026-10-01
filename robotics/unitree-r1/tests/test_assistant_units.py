#!/usr/bin/env python3
"""
Offline unit tests for test_vision_voice_assistant.py (no Riva / llama-server / robot needed).

Run:  python3 tests/test_assistant_units.py
      VOCAB_PATH=/path/to/vocab.txt python3 tests/test_assistant_units.py
"""
import importlib.util
import os
import sys
import time
import types
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "test_vision_voice_assistant.py")

VOCAB_CANDIDATES = [
    os.getenv("VOCAB_PATH", ""),
    "/home/unitree/robot_assets/models/vocab.txt",
    os.path.expanduser("~/robot_assets/models/vocab.txt"),
]
VOCAB_PATH = next((p for p in VOCAB_CANDIDATES if p and os.path.exists(p)), None)


def _install_fake_riva():
    """Stub riva.client so importing the assistant doesn't need a Riva server."""
    class _Resp(object):
        audio = b""

    class _TTS(object):
        def __init__(self, auth):
            pass

        def synthesize(self, **kw):
            return _Resp()

    riva = types.ModuleType("riva")
    client = types.ModuleType("riva.client")
    client.Auth = lambda uri=None: object()
    client.ASRService = lambda auth: object()
    client.SpeechSynthesisService = _TTS
    riva.client = client
    sys.modules["riva"] = riva
    sys.modules["riva.client"] = client


def _load_assistant():
    _install_fake_riva()
    spec = importlib.util.spec_from_file_location("assistant", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


assistant = _load_assistant()


class SentenceSplitTests(unittest.TestCase):
    def split_stream(self, tokens):
        """Simulates token streaming and returns what would be sent to TTS."""
        spoken, buf = [], ""
        for t in tokens:
            buf += t
            ready, buf = assistant.split_speakable_text(buf)
            spoken.extend(ready)
        if buf.strip():
            spoken.append(buf.strip())
        return spoken

    def test_text_after_period_stays_with_next_sentence(self):
        # Old code emitted "I am Jason. The" as the first sentence
        out = self.split_stream(["I am", " Jason", ". The", " robot", " is", " here", "."])
        self.assertEqual(out, ["I am Jason.", "The robot is here."])

    def test_decimal_not_split(self):
        out = self.split_stream(["Pi is", " 3", ".", "14", " roughly", "."])
        self.assertEqual(out, ["Pi is 3.14 roughly."])

    def test_short_clause_not_split(self):
        out = self.split_stream(["Sure", ",", " I can", " help", "."])
        self.assertEqual(out, ["Sure, I can help."])

    def test_long_clause_split(self):
        text = "I can see a table with a red cup, and a laptop next to it."
        out = self.split_stream([c for c in text])
        self.assertEqual(out, ["I can see a table with a red cup,", "and a laptop next to it."])

    def test_newline_is_boundary(self):
        ready, rest = assistant.split_speakable_text("Hello there\nHow")
        self.assertEqual(ready, ["Hello there"])
        self.assertEqual(rest, "How")

    def test_multiple_sentences_in_one_chunk(self):
        ready, rest = assistant.split_speakable_text("One. Two! Three? Fo")
        self.assertEqual(ready, ["One.", "Two!", "Three?"])
        self.assertEqual(rest, " Fo")


class PlaybackTimingTests(unittest.TestCase):
    def test_wait_blocks_until_estimated_playback_end(self):
        assistant._playback_until = 0.0
        assistant._register_playback(int(0.4 * assistant.TTS_SAMPLE_RATE))  # 0.4 s of audio
        t0 = time.time()
        assistant.wait_for_all_tts_to_finish()
        self.assertGreaterEqual(time.time() - t0, 0.35)

    def test_back_to_back_chunks_accumulate(self):
        assistant._playback_until = 0.0
        now = time.time()
        assistant._register_playback(assistant.TTS_SAMPLE_RATE)  # 1 s
        assistant._register_playback(assistant.TTS_SAMPLE_RATE)  # +1 s
        self.assertAlmostEqual(assistant._playback_until - now, 2.0, delta=0.05)
        assistant._playback_until = 0.0

    def test_audio_daemon_stale_socket_returns_false(self):
        import socket as _s
        path = "/tmp/_test_stale_audio.sock"
        if os.path.exists(path):
            os.unlink(path)
        srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        srv.bind(path)
        srv.close()  # socket file exists but nobody is listening (like a killed daemon)
        old = assistant.AUDIO_SOCKET
        try:
            assistant.AUDIO_SOCKET = path
            self.assertFalse(assistant._send_to_audio_daemon(b"\x00\x00"))
        finally:
            assistant.AUDIO_SOCKET = old
            os.unlink(path)


@unittest.skipIf(VOCAB_PATH is None, "vocab.txt not found (set VOCAB_PATH)")
class WordPieceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enc = assistant.MiniLMEncoder.__new__(assistant.MiniLMEncoder)
        enc.session = None
        enc.vocab = {}
        with open(VOCAB_PATH, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                enc.vocab[line.strip()] = idx
        cls.enc = enc
        cls.inv = {v: k for k, v in enc.vocab.items()}

    SENTENCES = [
        "What do you see in front of you?",
        "Can you describe the surroundings?",
        "What's the weather like in San Francisco today?",
        "Tell me a joke about robots.",
        "Is my coffee mug on the table?",
        "Recognize the handwriting on this whiteboard, please!",
        "Café naïve résumé",
        "unbelievably overcomplicated transcription",
    ]

    def ids(self, text):
        arr = self.enc.tokenize(text)["input_ids"][0]
        return [int(i) for i in arr if i != 0]

    def test_no_unknown_tokens_for_common_english(self):
        unk = self.enc.vocab["[UNK]"]
        for s in self.SENTENCES:
            self.assertNotIn(unk, self.ids(s), s)

    def test_subword_split(self):
        # "whiteboard" is not a vocab entry: old tokenizer -> [UNK], WordPiece -> white ##board
        self.assertNotIn("whiteboard", self.enc.vocab)
        toks = [self.inv[i] for i in self.ids("whiteboard")]
        self.assertEqual(toks[0], "[CLS]")
        self.assertEqual(toks[-1], "[SEP]")
        self.assertNotIn("[UNK]", toks)
        self.assertGreater(len(toks), 3)  # split into word pieces instead of [UNK]
        self.assertTrue(any(t.startswith("##") for t in toks))

    def test_matches_huggingface_tokenizer(self):
        try:
            from tokenizers import BertWordPieceTokenizer
        except ImportError:
            self.skipTest("tokenizers not installed")
        ref = BertWordPieceTokenizer(VOCAB_PATH, lowercase=True, strip_accents=True, clean_text=True)
        for s in self.SENTENCES:
            self.assertEqual(self.ids(s), ref.encode(s).ids, s)


class WakeWordTests(unittest.TestCase):
    W = ["jason", "jayson"]

    def m(self, text):
        return assistant.match_wake_word(text, self.W)

    def test_leading_name_is_stripped(self):
        self.assertEqual(self.m("Jason, what do you see?"), (True, "what do you see?"))

    def test_hey_prefix(self):
        self.assertEqual(self.m("Hey Jason what time is it?"), (True, "what time is it?"))

    def test_alternate_spelling(self):
        self.assertEqual(self.m("OK Jayson. Tell me a joke."), (True, "Tell me a joke."))

    def test_name_only(self):
        self.assertEqual(self.m("Jason."), (True, ""))

    def test_not_addressed(self):
        self.assertEqual(self.m("I think the meeting is at three."), (False, ""))

    def test_name_too_late_in_sentence(self):
        self.assertFalse(self.m("yesterday I went to lunch with my friend Jason")[0])

    def test_name_inside_other_word_does_not_match(self):
        self.assertFalse(self.m("Jasonville is a town.")[0])

    def test_empty_wake_list_accepts_everything(self):
        self.assertEqual(assistant.match_wake_word(" hello there ", []), (True, "hello there"))


class WakeSessionTests(unittest.TestCase):
    def test_first_request_needs_wake_word_then_conversation_stays_open(self):
        s = assistant.WakeSession(["jason"], follow_up_sec=30)
        self.assertFalse(s.is_open(100.0))           # first request: say the name
        s.answered(100.0)
        self.assertTrue(s.is_open(110.0))            # follow-ups: no name needed
        s.answered(125.0)                            # each reply extends the conversation
        self.assertTrue(s.is_open(150.0))
        self.assertFalse(s.is_open(155.0))           # 30 s of quiet: name needed again

    def test_closed_reported_once(self):
        s = assistant.WakeSession(["jason"], follow_up_sec=30)
        self.assertFalse(s.just_closed(50.0))        # never opened: nothing to report
        s.answered(100.0)
        self.assertFalse(s.just_closed(120.0))
        self.assertTrue(s.just_closed(131.0))
        self.assertFalse(s.just_closed(140.0))

    def test_no_wake_words_always_open(self):
        s = assistant.WakeSession([], follow_up_sec=30)
        self.assertTrue(s.is_open(0.0))
        self.assertFalse(s.just_closed(1000.0))

    def test_defaults(self):
        self.assertEqual(assistant.WAKE_WORDS[0], "jason")
        self.assertEqual(assistant.FOLLOW_UP_SEC, 30)


class EndpointerTests(unittest.TestCase):
    FRAME = b"\x01\x00" * 512

    def feed(self, ep, probs):
        events = []
        for p in probs:
            ev, payload = ep.process(self.FRAME, p)
            if ev not in (None, "speech"):
                events.append((ev, payload))
        return events

    def make(self):
        return assistant.UtteranceEndpointer(frame_sec=0.032, start_threshold=0.5, end_threshold=0.35,
                                             start_sec=0.15, end_silence_sec=0.6, pre_roll_sec=0.3,
                                             min_speech_sec=0.4, max_speech_sec=15.0, keep_tail_sec=0.2)

    def test_utterance_detected_with_preroll_and_trimmed_tail(self):
        ep = self.make()
        probs = [0.0] * 30 + [0.9] * 40 + [0.0] * 30
        events = self.feed(ep, probs)
        self.assertEqual([e for e, _ in events], ["start", "end"])
        n_frames = len(events[1][1]) // len(self.FRAME)
        # 40 speech frames + ~9 pre-roll frames + ~6 kept tail frames
        self.assertGreaterEqual(n_frames, 40 + 9)
        self.assertLessEqual(n_frames, 40 + 9 + 8)

    def test_short_blip_ignored(self):
        ep = self.make()
        self.assertEqual(self.feed(ep, [0.0] * 10 + [0.9] * 3 + [0.0] * 40), [])

    def test_too_short_utterance_discarded(self):
        ep = self.make()
        events = self.feed(ep, [0.9] * 8 + [0.0] * 30)
        self.assertEqual([e for e, _ in events], ["start", "discard"])

    def test_short_pause_does_not_end_utterance(self):
        ep = self.make()
        probs = [0.9] * 20 + [0.1] * 10 + [0.9] * 20 + [0.0] * 30  # 0.32 s pause < 0.6 s
        self.assertEqual([e for e, _ in self.feed(ep, probs)], ["start", "end"])

    def test_max_length_forces_end(self):
        ep = assistant.UtteranceEndpointer(frame_sec=0.032, max_speech_sec=1.0)
        events = self.feed(ep, [0.9] * 60)
        self.assertIn("end", [e for e, _ in events])


class StreamChunkerTests(unittest.TestCase):
    def tone(self, n, amp=5000):
        return (np.sin(np.arange(n) / 5.0) * amp).astype(np.int16)

    def test_prebuffer_then_per_chunk(self):
        c = assistant.StreamChunker(prebuffer=4000)
        self.assertEqual(c.push(self.tone(2229)), [])          # below pre-buffer
        out = c.push(self.tone(2229))
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(len(out[0]), 4000)
        self.assertEqual(len(c.push(self.tone(2229))), 1)     # afterwards: sent immediately

    def test_leading_silence_dropped(self):
        c = assistant.StreamChunker(prebuffer=0)
        self.assertEqual(c.push(np.zeros(2000, np.int16)), [])
        out = c.push(np.concatenate([np.zeros(1000, np.int16), self.tone(1000)]))
        self.assertLessEqual(len(out[0]), 1000 + 160)

    def test_trailing_silence_dropped_but_inner_pause_kept(self):
        c = assistant.StreamChunker(prebuffer=0)
        sent = c.push(self.tone(1000))
        sent += c.push(np.zeros(1000, np.int16))   # pause: held
        sent += c.push(self.tone(1000))            # more speech: pause released
        sent += c.push(np.zeros(1000, np.int16))   # trailing: held
        sent += c.flush()
        self.assertEqual(sum(len(s) for s in sent), 3000)

    def test_soft_limiter_never_clips(self):
        x = np.array([0, 5000, 15000, 22000, -22000, 32767], dtype=np.int16)
        y = assistant.apply_tts_gain(x, gain=2.0)
        self.assertEqual(int(y[1]), 10000)                      # below knee: plain gain
        self.assertLess(int(np.max(np.abs(y.astype(np.int32)))), 32767 + 1)
        self.assertGreater(int(y[3]), int(y[2]))                # still monotonic above the knee


class MemoryTests(unittest.TestCase):
    def tearDown(self):
        assistant._history = []
        assistant._history_time = 0.0

    def test_history_included_then_expires(self):
        assistant.remember_exchange("What is the capital of France?", "Paris.")
        msgs = assistant.build_llm_messages("And Germany?")
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        assistant._history_time -= assistant.CONVERSATION_MEMORY_SEC + 1
        self.assertEqual([m["role"] for m in assistant.build_llm_messages("x")], ["system", "user"])

    def test_memory_bounded(self):
        for i in range(10):
            assistant.remember_exchange("q%d" % i, "a%d" % i)
        self.assertEqual(len(assistant._history), assistant.MEMORY_TURNS)
        self.assertEqual(assistant._history[-1], ("q9", "a9"))

    def test_prefill_prompt_is_prefix_of_final_prompt(self):
        assistant.remember_exchange("Hi", "Hello there.")
        hist = assistant.history_messages()
        pre = assistant.build_llm_messages(None, "IMG", hist)
        final = assistant.build_llm_messages("What is this?", "IMG", hist)
        self.assertEqual(pre[:-1], final[:-1])
        self.assertEqual(pre[-1]["content"][0], final[-1]["content"][0])  # image first in both


class FirstClauseTests(unittest.TestCase):
    def test_min_clause_words_parameter(self):
        ready, rest = assistant.split_speakable_text("Sure thing, my friend, here", min_clause_words=2)
        self.assertEqual(ready, ["Sure thing,", "my friend,"])


def _tone(level_db, n=512, seed=0):
    """Noise frame at roughly the given dBFS RMS level."""
    rng = np.random.RandomState(seed)
    x = rng.randn(n)
    x = x / np.sqrt(np.mean(x * x)) * (10 ** (level_db / 20.0) * 32768.0)
    return np.clip(x, -32767, 32767).astype(np.int16)


class ProximityGateTests(unittest.TestCase):
    def test_level_db(self):
        self.assertAlmostEqual(assistant.frame_level_db(_tone(-30)), -30, delta=0.5)
        self.assertEqual(round(assistant.frame_level_db(np.zeros(512, np.int16))), -96)

    def test_quiet_room_until_enough_background(self):
        gate = assistant.ProximityGate(gate_db=6)
        self.assertEqual(gate.floor_db(), -70.0)
        self.assertTrue(gate.passes(-50))

    def test_babble_rejected_close_voice_accepted(self):
        gate = assistant.ProximityGate(gate_db=6)
        for i in range(200):
            gate.observe_background(-35 + (i % 5) - 2)  # crowd at ~-35 dBFS
        self.assertAlmostEqual(gate.floor_db(), -35, delta=1)
        self.assertFalse(gate.passes(-33))  # chatter a bit louder than average
        self.assertTrue(gate.passes(-25))   # visitor in front of the robot

    def test_gate_disabled(self):
        gate = assistant.ProximityGate(gate_db=0)
        for _ in range(100):
            gate.observe_background(-20)
        self.assertTrue(gate.passes(-60))

    def test_scorer_zeroes_gated_frames_and_learns_only_outside_utterances(self):
        class FakeVad(object):
            def __call__(self, frame, gain=1.0):
                return 0.9

            def reset(self):
                pass
        scorer = assistant.VoiceFrameScorer(FakeVad(), assistant.ProximityGate(gate_db=6))
        babble = _tone(-35).tobytes()
        for _ in range(100):  # learn the crowd level
            scorer(babble, False)
        self.assertEqual(scorer(babble, False), 0.0)
        self.assertEqual(scorer(_tone(-24).tobytes(), True), 0.9)
        floor = scorer.gate.floor_db()
        for _ in range(400):  # a long loud utterance must not raise the floor
            scorer(_tone(-15).tobytes(), True)
        self.assertEqual(scorer.gate.floor_db(), floor)


PROC_INPUT_DEVICES = """I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="gpio-keys"
H: Handlers=kbd event0
B: KEY=10000000000000 0

I: Bus=0003 Vendor=1234 Product=5678 Version=0111
N: Name="Abham Image: Abham Image"
H: Handlers=kbd event5
B: KEY=10000000

I: Bus=0003 Vendor=1d57 Product=ad03 Version=0110
N: Name="Wireless Presenter Receiver"
H: Handlers=sysrq kbd leds event7
B: KEY=210000000000 0
"""


class ButtonDeviceTests(unittest.TestCase):
    def test_parse_key_bitmap(self):
        devs = assistant.parse_input_devices(PROC_INPUT_DEVICES)
        self.assertEqual([d[1] for d in devs], ["/dev/input/event0", "/dev/input/event5", "/dev/input/event7"])
        self.assertEqual(devs[0][2], {116})       # KEY_POWER
        self.assertEqual(devs[1][2], {28})        # KEY_ENTER
        self.assertEqual(devs[2][2], {104, 109})  # PageUp / PageDown

    def test_finds_clicker_and_skips_cameras_and_gpio(self):
        name, path = assistant.find_button_device(PROC_INPUT_DEVICES, assistant.BUTTON_KEYS)
        self.assertEqual(path, "/dev/input/event7")
        self.assertIn("Presenter", name)

    def test_none_when_only_cameras(self):
        text = PROC_INPUT_DEVICES.split("\n\nI: Bus=0003 Vendor=1d57")[0]
        self.assertEqual(assistant.find_button_device(text, assistant.BUTTON_KEYS), (None, None))


class TapRecorderTests(unittest.TestCase):
    FRAME = b"\x00\x00" * 512

    def make(self, hold=False):
        ep = assistant.UtteranceEndpointer(frame_sec=0.032, start_threshold=0.5, end_threshold=0.35,
                                           start_sec=0.15, end_silence_sec=0.6, pre_roll_sec=0.3,
                                           min_speech_sec=0.4, max_speech_sec=15.0, keep_tail_sec=0.2)
        return assistant.TapRecorder(ep, frame_sec=0.032, max_sec=8.0, no_speech_sec=5.0, hold=hold)

    def run_probs(self, rec, probs):
        events = []
        for p in probs:
            ev, payload = rec.feed(self.FRAME, p)
            if ev:
                events.append(ev)
                if ev in ("end", "cancel"):
                    return events, payload
        return events, None

    def test_ends_when_visitor_stops_talking(self):
        events, audio = self.run_probs(self.make(), [0.0] * 20 + [0.9] * 40 + [0.0] * 40)
        self.assertEqual(events, ["start", "end"])
        self.assertGreater(len(audio), 40 * len(self.FRAME))

    def test_cancel_when_nobody_speaks(self):
        events, reason = self.run_probs(self.make(), [0.0] * 200)
        self.assertEqual(events, ["cancel"])
        self.assertIn("No speech", reason)

    def test_second_tap_uses_everything_since_first(self):
        rec = self.make()
        self.run_probs(rec, [0.0] * 10 + [0.9] * 20)
        ev, audio = rec.button("press")
        self.assertEqual(ev, "end")
        self.assertEqual(len(audio), 30 * len(self.FRAME))

    def test_cough_does_not_end_turn(self):
        events, _ = self.run_probs(self.make(), [0.9] * 8 + [0.0] * 30 + [0.9] * 40 + [0.0] * 40)
        self.assertEqual(events, ["start", "start", "end"])

    def test_short_first_word_is_kept(self):
        # "Hi." (discarded as too short by the endpointer) + pause + the real question
        rec = self.make()
        events, audio = self.run_probs(rec, [0.0] * 5 + [0.9] * 8 + [0.0] * 25 + [0.9] * 40 + [0.0] * 40)
        self.assertEqual(events, ["start", "start", "end"])
        self.assertGreaterEqual(len(audio), (5 + 8 + 25 + 40) * len(self.FRAME))

    def test_max_length(self):
        events, audio = self.run_probs(self.make(), [0.9] * 400)
        self.assertEqual(events[-1], "end")
        self.assertEqual(len(audio), 250 * len(self.FRAME))  # 8 s / 32 ms

    def test_hold_mode_ignores_pauses_and_ends_on_release(self):
        rec = self.make(hold=True)
        events, _ = self.run_probs(rec, [0.9] * 30 + [0.0] * 60 + [0.9] * 30 + [0.0] * 50)
        self.assertNotIn("end", events)
        self.assertNotIn("cancel", events)
        self.assertEqual(rec.button("press"), (None, None))
        ev, audio = rec.button("release")
        self.assertEqual(ev, "end")
        self.assertEqual(len(audio), 170 * len(self.FRAME))

    def test_reset_cancels(self):
        rec = self.make()
        self.run_probs(rec, [0.9] * 10)
        self.assertEqual(rec.button("reset")[0], "cancel")

    def test_release_ignored_in_tap_mode(self):
        rec = self.make()
        self.run_probs(rec, [0.9] * 10)
        self.assertEqual(rec.button("release"), (None, None))


class LedTests(unittest.TestCase):
    def setUp(self):
        assistant._playback_until = 0.0
        assistant._playback_run_start = 0.0

    tearDown = setUp

    def test_talking_window_includes_speaker_latency(self):
        t0 = time.time()
        assistant._register_playback(16000)  # 1 s of audio
        lat = assistant.SPEAKER_LATENCY_SEC
        self.assertFalse(assistant.robot_is_talking(t0 + lat - 0.1))  # not audible yet
        self.assertTrue(assistant.robot_is_talking(t0 + lat + 0.1))
        self.assertTrue(assistant.robot_is_talking(t0 + 1.0 + lat - 0.05))
        self.assertFalse(assistant.robot_is_talking(t0 + 1.0 + lat + 0.1))

    def test_state_colours_sent_to_daemon_socket(self):
        import socket
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "led.sock")
        daemon = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        daemon.bind(path)
        daemon.settimeout(1.0)
        led = assistant.LedIndicator(enabled=True, path=path)

        def latest():
            msg = daemon.recv(64)
            daemon.setblocking(False)
            try:
                while True:
                    msg = daemon.recv(64)
            except (BlockingIOError, socket.error):
                pass
            daemon.settimeout(1.0)
            return tuple(int(v) for v in msg.decode().split())

        led.set("listening")
        self.assertEqual(latest(), assistant.LED_LISTEN)
        assistant._register_playback(16000)
        time.sleep(assistant.SPEAKER_LATENCY_SEC + 0.15)
        self.assertEqual(latest(), assistant.LED_TALK)
        led.enabled = False
        daemon.close()


class ClosingOfferTests(unittest.TestCase):
    def test_closers_detected(self):
        for t in ["Can I help you with anything else today?", "Do you have another question for me?",
                  "Is there anything else I can do?", "Let me know if you need more help.",
                  "Please tell me what you would like assistance with now.",
                  "Can you tell me what you are looking for?", "How can I help you further?",
                  "Feel free to ask more questions.", "Please state what you require.",
                  "Is that what you were asking about?", "Does that answer your question?",
                  "Would you like to know more?"]:
            self.assertTrue(assistant.is_closing_offer(t), t)

    def test_real_answers_kept(self):
        for t in ["The capital of France is Paris.", "I see a table with some papers in front of me.",
                  "Both series are excellent science fiction.", "Do you mean the Star Wars movies?",
                  "My name is Jason.", "Berlin is known for its rich history and culture."]:
            self.assertFalse(assistant.is_closing_offer(t), t)


class ModeSelectionTests(unittest.TestCase):
    def test_tap_mode_uses_booth_profile(self):
        saved = (assistant.SYSTEM_PROMPT, assistant.CONVERSATION_MEMORY_SEC)
        try:
            assistant.apply_tap_mode_profile({})
            self.assertEqual(assistant.SYSTEM_PROMPT, assistant.BOOTH_SYSTEM_PROMPT)
            self.assertEqual(assistant.CONVERSATION_MEMORY_SEC, 30)
            self.assertEqual(assistant.build_llm_messages("hi")[0]["content"], assistant.BOOTH_SYSTEM_PROMPT)
            assistant.apply_tap_mode_profile({"SYSTEM_PROMPT": "custom"})
            self.assertEqual(assistant.SYSTEM_PROMPT, "custom")
        finally:
            assistant.SYSTEM_PROMPT, assistant.CONVERSATION_MEMORY_SEC = saved

    def test_modes(self):
        self.assertEqual(assistant.select_mode([], {}), "ptt")
        self.assertEqual(assistant.select_mode(["--vad"], {}), "vad")
        self.assertEqual(assistant.select_mode(["--tap"], {"ASSISTANT_MODE": "vad"}), "tap")
        self.assertEqual(assistant.select_mode([], {"ASSISTANT_MODE": "TAP"}), "tap")
        self.assertEqual(assistant.select_mode([], {"ASSISTANT_MODE": "bogus"}), "ptt")


class HandMotionTests(unittest.TestCase):
    def setUp(self):
        self.saved = (assistant._playback_run_start, assistant._playback_until)
        assistant._playback_run_start = assistant._playback_until = 0.0  # not talking

    def tearDown(self):
        assistant._playback_run_start, assistant._playback_until = self.saved

    def _run(self, hm, t0, seconds, dt=0.02):
        t = t0
        while t < t0 + seconds:
            hm.step(t)
            t += dt
        return t

    def test_smoothstep_pose(self):
        a, b = (0.0,) * 6, (1.0,) * 6
        self.assertEqual(assistant.smoothstep_pose(a, b, 0.0), a)
        self.assertEqual(assistant.smoothstep_pose(a, b, 1.0), b)
        self.assertEqual(assistant.smoothstep_pose(a, b, 2.0), b)
        self.assertAlmostEqual(assistant.smoothstep_pose(a, b, 0.5)[0], 0.5)
        self.assertLess(assistant.smoothstep_pose(a, b, 0.1)[0], 0.1)  # eases in

    def test_all_gestures_are_valid(self):
        for name, sides in assistant.HAND_GESTURES.items():
            for side, frames in sides.items():
                self.assertIn(side, ("left", "right"))
                self.assertEqual(tuple(frames[-1][0]), assistant.HAND_POSES["relax"], name)  # ends relaxed
                for pose, dur in frames:
                    self.assertEqual(len(pose), 6)
                    self.assertTrue(all(0.0 <= v <= 1.0 for v in pose))
                    self.assertGreater(dur, 0)

    def test_gesture_reaches_targets_and_ends_relaxed(self):
        hm = assistant.HandMotion(enabled=False, talk_gestures=False, seed=1)
        hm.play("thumbs_up")
        t = self._run(hm, 1000.0, 0.55)
        self.assertEqual(hm.pose["right"], assistant.HAND_POSES["thumbs_up"])
        self.assertEqual(hm.pose["left"], assistant.HAND_POSES["open"])  # one-handed gesture
        self._run(hm, t, 4.0)
        self.assertEqual(hm.pose["right"], assistant.HAND_POSES["relax"])
        self.assertFalse(hm.busy())

    def test_flex_closes_fully_both_hands(self):
        hm = assistant.HandMotion(enabled=False, talk_gestures=False)
        hm.play("flex")
        closed = {"left": False, "right": False}
        t = 1000.0
        while t < 1006.0:
            for side, pose in hm.step(t).items():
                closed[side] |= min(pose) > 0.99
            t += 0.02
        self.assertTrue(all(closed.values()))
        self.assertFalse(hm.busy())

    def test_talking_hands_move_only_while_talking(self):
        hm = assistant.HandMotion(enabled=False, talk_gestures=True, seed=2)
        self.assertEqual(hm.step(1000.0), {})  # idle and silent: nothing to send
        assistant._playback_run_start, assistant._playback_until = 0.0, 1006.0
        seen = {"left": [], "right": []}
        t = 1000.0
        while t < 1005.5:
            for side, pose in hm.step(t).items():
                seen[side].append(pose)
            t += 0.02
        for side, poses in seen.items():
            self.assertTrue(all(max(p) <= 0.85 for p in poses))  # never a hard fist while talking
            span = max(max(p[2:]) for p in poses) - min(min(p[2:]) for p in poses)
            self.assertGreater(span, 0.5, side)  # clearly visible motion
        self.assertNotEqual(seen["left"][-1], seen["right"][-1])  # hands move independently
        self._run(hm, t, 4.0)  # stopped talking -> settles to relax
        self.assertEqual(hm.pose["left"], assistant.HAND_POSES["relax"])
        self.assertEqual(hm.step(t + 5.0), {})

    def test_gesture_wins_over_talking_hands(self):
        hm = assistant.HandMotion(enabled=False, talk_gestures=True, seed=3)
        assistant._playback_run_start, assistant._playback_until = 0.0, 1010.0
        hm.play("fist")
        self._run(hm, 1000.0, 0.6)
        self.assertEqual(hm.pose["right"], assistant.HAND_POSES["fist"])

    def test_parse_hand_state(self):
        st = assistant.parse_hand_state("left 12 0 0.1 0.2 0.3 0.4 0.5 | right -1 0 0 0 0 0 0")
        self.assertEqual(st["left"], (12, [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]))
        self.assertEqual(st["right"][0], -1)
        self.assertEqual(assistant.parse_hand_state("garbage"), {})

    def test_bridge_protocol_with_fake_bridge(self):
        import socket as _s
        import threading as _t
        path = "/tmp/_test_hand_%d.sock" % os.getpid()
        if os.path.exists(path):
            os.unlink(path)
        srv = _s.socket(_s.AF_UNIX, _s.SOCK_DGRAM)
        srv.bind(path)
        cmds = []

        def bridge():  # replies like unitree_hand_bridge: only the right hand is connected
            while True:
                try:
                    data, addr = srv.recvfrom(512)
                except OSError:
                    return
                if data == b"state":
                    srv.sendto(b"left -1 0 0 0 0 0 0 | right 5 0 0 0 0 0 0", addr)
                else:
                    cmds.append(data.decode())
        _t.Thread(target=bridge, daemon=True).start()
        try:
            hm = assistant.HandMotion(enabled=True, path=path, talk_gestures=False)
            self.assertEqual(hm.connected(), ["right"])
            hm.play("fist")
            time.sleep(0.8)
            self.assertTrue(cmds)
            self.assertTrue(all(c.startswith("right ") for c in cmds))  # nothing sent to the absent hand
            last = cmds[-1].split()
            self.assertEqual(len(last), 8)
            self.assertAlmostEqual(float(last[1]), 1.0, places=2)
        finally:
            srv.close()
            os.unlink(path)


class GestureCommandTests(unittest.TestCase):
    def test_commands(self):
        m = assistant.match_gesture_command
        self.assertEqual(m("Can you flex your hands?")[0], "flex")
        self.assertEqual(m("Jason, flex.")[0], "flex")
        self.assertEqual(m("Hey Jason could you please wiggle your fingers for me")[0], "flex")
        self.assertEqual(m("Make a fist!")[0], "fist")
        self.assertEqual(m("Give me a thumbs up")[0], "thumbs_up")
        self.assertEqual(m("show me a peace sign")[0], "peace")
        self.assertEqual(m("Rock on!")[0], "rock")
        self.assertEqual(m("Count to five")[0], "count")
        self.assertEqual(m("count to 5 please")[0], "count")
        self.assertEqual(m("open your hands")[0], "open")
        self.assertEqual(m("Wave at everyone")[0], "wave")
        self.assertEqual(m("say hi")[0], "wave")
        self.assertEqual(m("point")[0], "point")
        self.assertTrue(m("flex")[1])

    def test_questions_are_not_commands(self):
        m = assistant.match_gesture_command
        for text in ("What is the point of life?", "Count the people in front of you", "How do you flex?",
                     "Can you move?", "What rock is this?", "Tell me about your hands", "", None,
                     "Can you see my thumbs up?", "Wave hello to the crowd and tell a joke"):
            self.assertEqual(m(text), (None, None), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

