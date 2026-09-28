"""Warms the Hermes gateway up before the golden snapshot, then reports ready.

Hermes builds its agent lazily: the first chat turn in a fresh process takes
~3 s of CPU (imports, client and prompt setup). Every keynote agent is restored
from one golden snapshot, so if that snapshot is taken cold, all 1,000 agents
pay those 3 s on their first turn at the same moment, and a node full of
agents runs out of CPU. So the template's readyz points here instead of at the
gateway's /health: this script waits for the gateway, runs a few throwaway
turns (the driver's LLM proxy answers them locally, without calling the
model), and only then answers GET /ready with 200. The snapshot is therefore
taken warm, and every restored agent starts with the work already done.

Usage: warmup.py <gateway pid>. Runs until the gateway exits, so it can be
the container's main process.
"""

import http.server
import json
import os
import signal
import sys
import threading
import time
import urllib.request

GATEWAY = "http://127.0.0.1:%s" % os.environ.get("API_SERVER_PORT", "80")
READY_PORT = int(os.environ.get("READY_PORT", "8081"))
KEY = os.environ["API_SERVER_KEY"]
# The driver's LLM proxy answers requests carrying this marker itself.
MARKER = "keynote-warmup"

ready = threading.Event()


def log(msg):
    print("[keynote-warmup] %s" % msg, file=sys.stderr, flush=True)


def wait_health(deadline):
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(GATEWAY + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def turn(session, text):
    body = json.dumps({
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": "%s: %s" % (MARKER, text)}],
    }).encode()
    req = urllib.request.Request(GATEWAY + "/v1/chat/completions", data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + KEY,
        "X-Hermes-Session-Id": session,
    })
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()
    return (time.time() - t0) * 1000


def warm():
    if not wait_health(time.time() + 300):
        log("gateway never became healthy; reporting ready anyway")
        ready.set()
        return
    # A new session twice, then a second session: covers both the
    # first-turn and the follow-up code paths.
    for session, text in ((MARKER + "-a", "say OK"), (MARKER + "-a", "say OK again"),
                          (MARKER + "-b", "say OK")):
        for attempt in range(1, 4):
            try:
                log("%s turn: %.0f ms" % (session, turn(session, text)))
                break
            except Exception as e:  # keep going: a cold agent still works
                log("%s turn failed (attempt %d): %s" % (session, attempt, e))
                time.sleep(1)
    ready.set()
    log("warm; reporting ready on :%d/ready" % READY_PORT)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = ready.is_set()
        self.send_response(200 if ok else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def main():
    gw = int(sys.argv[1])
    signal.signal(signal.SIGTERM, lambda *_: os.kill(gw, signal.SIGTERM))
    threading.Thread(target=warm, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", READY_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # Exit with the gateway, so the sandbox fails visibly if Hermes dies.
    _, status = os.waitpid(gw, 0)
    log("gateway exited (status %d)" % status)
    sys.exit(1)


if __name__ == "__main__":
    main()
