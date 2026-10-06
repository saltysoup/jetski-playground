#!/usr/bin/env python3
"""Watch the GCS checkpoint prefix and time the write.

Phase A wants three numbers that nothing else in the stack reports:
  A2  wall-clock at which the first checkpoint object appears
  A3  how long the write takes to quiesce, and how big it ends up

Size is sampled until it stops growing for `STABLE_FOR` consecutive polls; that
quiescence point is treated as write-complete. Appends to run5/ckpt-timeline.log.

Usage: watch-ckpt.py [max_minutes]
"""
import json, os, sys, time, datetime, subprocess, urllib.request, urllib.parse

BUCKET = "ikwak-reliability-ckpt"
PREFIX = "dapo-gemma3-27b-it-2n8g/"
POLL = 15.0
STABLE_FOR = 3
MAXM = float(sys.argv[1]) if len(sys.argv) > 1 else 240.0
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ckpt-timeline.log")

_tok = {"v": "", "t": 0.0}
def token():
    if time.time() - _tok["t"] > 1800:
        _tok["v"] = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True).stdout.strip()
        _tok["t"] = time.time()
    return _tok["v"]

def emit(msg):
    line = "%s | %s" % (datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

def listing():
    """All objects under the checkpoint prefix, paginated."""
    objs, tok_page = [], None
    while True:
        url = ("https://storage.googleapis.com/storage/v1/b/%s/o"
               "?prefix=%s&fields=items(name,size,timeCreated),nextPageToken&maxResults=1000"
               % (BUCKET, urllib.parse.quote(PREFIX, safe="")))
        if tok_page:
            url += "&pageToken=" + tok_page
        try:
            d = json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers={"Authorization": "Bearer " + token()}),
                timeout=45))
        except Exception as ex:
            return None, str(ex)[:120]
        objs.extend(d.get("items", []))
        tok_page = d.get("nextPageToken")
        if not tok_page:
            return objs, None

emit("START watching gs://%s/%s" % (BUCKET, PREFIX))
deadline = time.time() + MAXM * 60
seen_steps, stable, last_total = set(), 0, -1
first_seen_at = None

while time.time() < deadline:
    objs, err = listing()
    if err:
        emit("list error: " + err)
        time.sleep(POLL)
        continue

    if objs:
        total = sum(int(o.get("size", 0)) for o in objs)
        steps = set()
        for o in objs:
            rest = o["name"][len(PREFIX):]
            if rest.startswith("step_"):
                steps.add(rest.split("/")[0])

        if first_seen_at is None:
            first_seen_at = time.time()
            earliest = min(o.get("timeCreated", "") for o in objs)
            emit("FIRST OBJECT | %d objs | %.1f MiB | steps=%s | earliest timeCreated=%s"
                 % (len(objs), total / 2**20, sorted(steps), earliest))

        for s in sorted(steps - seen_steps):
            emit("NEW CHECKPOINT DIR: %s" % s)
        seen_steps |= steps

        if total == last_total:
            stable += 1
            if stable == STABLE_FOR:
                emit("WRITE QUIESCED | %d objs | %.2f GiB | steps=%s | %.0fs after first object"
                     % (len(objs), total / 2**30, sorted(seen_steps),
                        time.time() - first_seen_at))
        else:
            if stable >= STABLE_FOR:
                emit("growing again (new checkpoint starting)")
            stable = 0
            emit("size %.2f GiB across %d objs | steps=%s"
                 % (total / 2**30, len(objs), sorted(steps)))
        last_total = total

    time.sleep(POLL)

emit("END")
