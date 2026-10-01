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


if __name__ == "__main__":
    unittest.main(verbosity=2)
