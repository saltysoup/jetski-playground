#!/usr/bin/env python3
"""Pull Monitoring alert notifications off Pub/Sub and timestamp them.

This is how M2 (T0 -> alert fires) gets measured. The Pub/Sub message carries a
publishTime, and the notification payload carries incident.started_at, so we can
separate "Monitoring decided the condition matched" from "the notification was
delivered" — two quite different numbers that get conflated as "alert latency".

Usage: pull-alerts.py [max_minutes]
Appends to run4/alert-timeline.log. Safe under nohup.
"""
import json, sys, time, base64, datetime, subprocess, urllib.request, os

P   = "gpu-launchpad-playground"
SUB = f"projects/{P}/subscriptions/xid-alerts-sub"
MAXM = float(sys.argv[1]) if len(sys.argv) > 1 else 480.0
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert-timeline.log")

_tok = {"v": "", "t": 0.0}
def token():
    if time.time() - _tok["t"] > 1800:
        _tok["v"] = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True).stdout.strip()
        _tok["t"] = time.time()
    return _tok["v"]

def post(url, body):
    h = {"Authorization": "Bearer " + token(), "Content-Type": "application/json"}
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, data=json.dumps(body).encode(), headers=h), timeout=45)
        return json.loads(r.read().decode() or "{}")
    except Exception as ex:
        return {"_err": (ex.read().decode()[:200] if hasattr(ex, "read") else str(ex)[:200])}

def emit(msg):
    line = "%s | %s" % (datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

emit(f"START pulling {SUB} for {MAXM} min")
deadline = time.time() + MAXM * 60

while time.time() < deadline:
    d = post(f"https://pubsub.googleapis.com/v1/{SUB}:pull",
             {"maxMessages": 10, "returnImmediately": True})
    if "_err" in d:
        emit("pull error: " + d["_err"])
        time.sleep(15)
        continue

    acks = []
    for m in d.get("receivedMessages", []):
        acks.append(m["ackId"])
        msg = m.get("message", {})
        pub = msg.get("publishTime")
        try:
            payload = json.loads(base64.b64decode(msg.get("data", "")).decode())
        except Exception:
            payload = {}
        inc = payload.get("incident", {})
        emit("ALERT | publishTime=%s | state=%s | started_at=%s | condition=%s | policy=%s | summary=%s"
             % (pub, inc.get("state"), inc.get("started_at"),
                (inc.get("condition") or {}).get("displayName"),
                inc.get("policy_name"), str(inc.get("summary"))[:200]))
        emit("      | incident_url=%s" % inc.get("url"))

    if acks:
        post(f"https://pubsub.googleapis.com/v1/{SUB}:acknowledge", {"ackIds": acks})
    else:
        time.sleep(5)

emit("END")
