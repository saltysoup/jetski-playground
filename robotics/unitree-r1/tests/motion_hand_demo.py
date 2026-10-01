#!/usr/bin/env python3
"""Hand motion demo for the Unitree R1 (BrainCo Revo2 hands) - run it while WATCHING the robot.

Needs brainco_hand_server + unitree_hand_bridge running (bash app.sh --services-only starts both).
Plays: open -> flex (finger ripple) -> fist -> thumbs up -> count to five -> relax, printing hand state.

    python3 tests/motion_hand_demo.py            # asks before moving
    python3 tests/motion_hand_demo.py --yes      # no prompt
    python3 tests/motion_hand_demo.py --gesture flex
"""
import argparse
import importlib.util
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def load_assistant():
    """Import the assistant module for its poses/gestures/engine without connecting to Riva."""
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
    path = os.path.join(HERE, "..", "test_vision_voice_assistant.py")
    if not os.path.exists(path):
        path = "/home/unitree/test_vision_voice_assistant.py"
    spec = importlib.util.spec_from_file_location("assistant", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fmt(q):
    return " ".join("%.2f" % v for v in q)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="don't ask before moving")
    ap.add_argument("--gesture", action="append", help="gesture(s) to play (default: the full demo)")
    args = ap.parse_args()

    a = load_assistant()
    hands = a.HandMotion(enabled=True, talk_gestures=False)
    state = hands.query_state()
    if state is None:
        print("[FAIL] Hand bridge not running (%s). Start: bash app.sh --services-only" % a.HAND_SOCKET)
        return 1
    for side in ("left", "right"):
        age, q = state.get(side, (-1, [0] * 6))
        print("[STATE] %-5s %s" % (side, "q=%s (%d ms old)" % (fmt(q), age) if age >= 0 else "NOT SEEN"))
    live = hands.connected()
    if not live:
        print("[FAIL] No hand state from brainco_hand_server - is it running? (see /home/unitree/brainco_hand.log)")
        return 1

    names = args.gesture or ["open", "flex", "fist", "thumbs_up", "count"]
    for n in names:
        if n not in a.HAND_GESTURES:
            print("[FAIL] Unknown gesture %r. Choose from: %s" % (n, ", ".join(sorted(a.HAND_GESTURES))))
            return 1
    if not args.yes:
        sys.stdout.write("The %s hand(s) will move: %s. Keep clear of the fingers. Press ENTER to start... "
                         % (" + ".join(live), ", ".join(names)))
        sys.stdout.flush()
        sys.stdin.readline()

    try:
        for n in names:
            print("[DEMO] %s" % n)
            hands.play(n)
            time.sleep(0.2)
            while hands.busy():
                time.sleep(0.05)
            st = hands.query_state() or {}
            for side in live:
                print("        %-5s q=%s" % (side, fmt(st.get(side, (0, [0] * 6))[1])))
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n[DEMO] Interrupted - relaxing hands.")
    hands.relax()
    time.sleep(1.0)
    print("[OK] Done (hands relaxed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
