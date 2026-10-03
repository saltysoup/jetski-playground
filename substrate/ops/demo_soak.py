#!/usr/bin/env python3
"""Replays the stage flow against the driver and samples its state.

Wake Agents (wake_only) -> Simulate Traffic (Default 50:50, today's Stage 2) ->
Priority (Stage 3) -> Default 50:50 -> Suspend all, each at the driver's own
stage rate (config.stage_rates: 200 req/s in every stage at the default 80%
fleet idle; older drivers 200 and 400). Prints one line per sample, so stuck
agents, failed calls and flow-control saturation are visible. BAL_S=0
suspends straight from Priority, the worst case for calls in flight during
the pause. At 80% fleet idle the step back to Default 50:50 can fail a few
dozen Free-User requests that were still queued from Priority (README §9);
the show only goes forward.

Usage: python3 demo_soak.py [DRIVER_URL=http://localhost:8090/] [WARM_S=30] [PRIO_S=60] [BAL_S=20]
"""
import collections
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8090/"
WARM_S = float(sys.argv[2]) if len(sys.argv) > 2 else 30
PRIO_S = float(sys.argv[3]) if len(sys.argv) > 3 else 60
BAL_S = float(sys.argv[4]) if len(sys.argv) > 4 else 20
T0 = time.time()


def get():
    with urllib.request.urlopen(BASE + "api/state", timeout=5) as r:
        return json.load(r)


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.load(r)
    print(f"[{time.time() - T0:7.1f}s] POST {path} {body} -> {out}", flush=True)
    return out


def n0(v):
    return "-" if v is None else f"{v:.0f}"


def line(tag):
    s = get()
    a = collections.Counter(s.get("agents", ""))
    tr = s.get("traffic", {})
    fl = s.get("llmd", {}).get("flow", {}) or {}
    bands = " ".join(f"{(b.get('name') or '?')[:4]}:q{n0(b.get('queue'))}/w{n0(b.get('wait_ms'))}/r{n0(b.get('req_s'))}"
                     for b in fl.get("bands") or [])
    pods = " ".join(f"{p.get('name')}:{n0(p.get('req_s'))}rps/e2e{n0(p.get('e2e_ms'))}/run{n0(p.get('running'))}/wait{n0(p.get('waiting'))}"
                    for p in s.get("llmd", {}).get("pods") or [])
    b = s.get("burst", {}) or {}
    sat = fl.get("saturation")
    print(f"[{time.time() - T0:7.1f}s] {tag:9s} phase={s.get('phase')} agents={dict(sorted(a.items()))} "
          f"tr(rate={tr.get('rate')} sent={tr.get('sent')} rep={tr.get('replies')} fail={tr.get('failed')} "
          f"skip={tr.get('skipped')} infl={tr.get('inflight')}) sat={'-' if sat is None else f'{sat:.2f}'} [{bands}] {pods} "
          f"allrun={b.get('all_running_ms')} allsusp={b.get('all_suspended_ms')} note={s.get('note')}", flush=True)
    return s


def run_for(tag, secs, every=5):
    end = time.time() + secs
    while time.time() < end:
        line(tag)
        time.sleep(min(every, max(0, end - time.time())))


s = line("start")
if s.get("phase") != "idle":
    sys.exit("driver not idle")
post("api/strategy", {"mode": "balanced"})
post("api/burst", {"hold": True, "wake_only": True})
for _ in range(60):
    time.sleep(0.5)
    s = get()
    if s.get("phase") == "running" and s.get("burst", {}).get("all_running_ms"):
        break
line("woke")
post("api/simulate_traffic", {"toggle": True})
run_for("default", WARM_S)
post("api/strategy", {"mode": "priority"})    # the driver switches to the stage's rate itself
run_for("priority", PRIO_S)
if BAL_S > 0:
    post("api/strategy", {"mode": "balanced"})
    run_for("default2", BAL_S)
t_susp = time.time()
post("api/suspend", {})
while True:
    time.sleep(0.5)
    s = get()
    if s.get("phase") == "idle":
        break
    if time.time() - t_susp > 120:
        print("SUSPEND STILL RUNNING AFTER 120 s", flush=True)
        break
line("suspended")
print(f"suspend wall time (client) {time.time() - t_susp:.1f}s", flush=True)
time.sleep(5)
line("after5s")
