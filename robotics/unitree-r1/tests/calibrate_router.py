#!/usr/bin/env python3
"""
Router calibration: scores labeled utterances with the real MiniLM ONNX model using the
old (whole-word) tokenizer and the new WordPiece tokenizer, and suggests a threshold.

Run on the robot:  python3 tests/calibrate_router.py
"""
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_assistant_units import assistant  # noqa: E402  (loads assistant with a stubbed Riva)

VISION = [
    "What do you see?",
    "What am I holding?",
    "Can you tell me what's on my desk?",
    "Describe the room.",
    "How many people are in front of you?",
    "What color is this cup?",
    "Read this sign for me.",
    "Do you see my keys anywhere?",
    "Look at this and tell me what it is.",
    "What is this thing in my hand?",
    "Is there anyone behind me?",
    "What does my whiteboard say?",
    "Take a look around.",
    "Am I wearing glasses?",
]
TEXT_ONLY = [
    "What's your name?",
    "Tell me a joke.",
    "What is the capital of France?",
    "How are you today?",
    "What time is it?",
    "Explain how a robot works.",
    "Who wrote Romeo and Juliet?",
    "Can you sing a song?",
    "What is two plus two?",
    "Tell me about yourself.",
    "What's the weather like tomorrow?",
    "Give me a fun fact about space.",
    "I see, that makes sense.",
    "Let's see what happens next week.",
]


def old_tokenize(enc, text, max_len=64):
    """Original tokenizer from the repo (whole-word lookup, no WordPiece)."""
    tokens = ["[CLS]"] + re.findall(r'\w+|[^\w\s]', text.lower())[:max_len - 2] + ["[SEP]"]
    input_ids = [enc.vocab.get(t, enc.vocab.get("[UNK]", 100)) for t in tokens]
    attention_mask = [1] * len(input_ids)
    pad = max_len - len(input_ids)
    return {
        'input_ids': np.array([input_ids + [0] * pad], dtype=np.int64),
        'attention_mask': np.array([attention_mask + [0] * pad], dtype=np.int64),
        'token_type_ids': np.array([[0] * max_len], dtype=np.int64),
    }


def scores_with(tokenize_fn):
    enc = assistant.router_encoder
    original = enc.tokenize
    enc.tokenize = tokenize_fn
    try:
        anchors = [enc.encode(a) for a in assistant.VISUAL_ANCHORS]

        def score(t):
            q = enc.encode(t)
            return max(float(np.dot(q, a)) for a in anchors)

        return [score(t) for t in VISION], [score(t) for t in TEXT_ONLY]
    finally:
        enc.tokenize = original


def best_threshold(vis, txt):
    best = (0, 0.0)
    for th in np.arange(0.20, 0.90, 0.01):
        acc = sum(s >= th for s in vis) + sum(s < th for s in txt)
        if acc > best[0] or (acc == best[0] and abs(th - 0.5) < abs(best[1] - 0.5)):
            best = (acc, float(th))
    return best


def report(name, vis, txt, threshold):
    total = len(vis) + len(txt)
    acc = sum(s >= threshold for s in vis) + sum(s < threshold for s in txt)
    print("  %-28s accuracy @ %.2f: %d/%d" % (name, threshold, acc, total))
    return acc


def main():
    if not assistant.router_encoder.session:
        print("[FAIL] MiniLM ONNX model not loaded (%s)" % assistant.ROUTER_MODEL_PATH)
        sys.exit(1)
    enc = assistant.router_encoder
    old_vis, old_txt = scores_with(lambda t, max_len=64: old_tokenize(enc, t, max_len))
    new_vis, new_txt = scores_with(enc.tokenize)

    print("\n%-45s %8s %8s" % ("Utterance", "old", "new"))
    print("-" * 63)
    for label, texts, o, n in (("VISION", VISION, old_vis, new_vis), ("TEXT", TEXT_ONLY, old_txt, new_txt)):
        print("[%s]" % label)
        for t, a, b in zip(texts, o, n):
            print("  %-43s %8.3f %8.3f" % (t, a, b))

    print("\nSeparation (min vision - max text):  old %.3f   new %.3f" % (
        min(old_vis) - max(old_txt), min(new_vis) - max(new_txt)))
    print("\nAccuracy:")
    report("old tokenizer (orig 0.35)", old_vis, old_txt, 0.35)
    report("new tokenizer (orig 0.35)", new_vis, new_txt, 0.35)
    acc, th = best_threshold(new_vis, new_txt)
    report("new tokenizer (best)", new_vis, new_txt, th)
    report("new tokenizer (configured)", new_vis, new_txt, assistant.ROUTER_THRESHOLD)

    # What the original script actually ran: the ONNX model failed to load (riva imported before
    # onnxruntime), so every query went through the keyword fallback.
    saved = assistant.ANCHOR_EMBEDDINGS
    assistant.ANCHOR_EMBEDDINGS = []
    try:
        kw_vis = [assistant.calculate_vision_similarity(t) for t in VISION]
        kw_txt = [assistant.calculate_vision_similarity(t) for t in TEXT_ONLY]
    finally:
        assistant.ANCHOR_EMBEDDINGS = saved
    report("keyword fallback (original)", kw_vis, kw_txt, 0.35)
    print("\nSuggested ROUTER_THRESHOLD for new tokenizer: %.2f" % th)


if __name__ == "__main__":
    main()
