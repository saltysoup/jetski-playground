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


if __name__ == "__main__":
    unittest.main(verbosity=2)
