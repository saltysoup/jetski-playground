#!/usr/bin/env python3
"""Rebalances the light agents across the Substrate nodes.

An agent is pinned to the node that holds its local snapshot, and a node
restores at most `restoreSem` (12) agents at a time, so the busiest node sets
the "Wake 1,000" time. Re-created agents land on a random free worker at their
first wake, so a batch re-creation can leave some nodes with far more agents
than others (32-50 per node was seen after re-creating 257 agents).

Each round re-creates the agents above TARGET on the busiest nodes (through
the driver's reconcile), wakes the whole fleet so the pinned agents occupy
their nodes' workers (which biases new placements toward emptier nodes), and
pauses it again. Run it only while the driver is idle and the stage is not
in use.

Usage: CTX_SUB=<kube context> [ATE_ENDPOINT=host:port] [DRIVER_URL=...] \
         python3 rebalance.py [TARGET=41] [ROUNDS=10]
"""
import collections
import json
import os
import subprocess
import sys
import time
import urllib.request

TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 41
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 10
BASE = os.environ.get("DRIVER_URL", "http://localhost:8090/")
ATESPACE = os.environ.get("ATESPACE", "ate-demo-sandbox")
KATE = ["kubectl", "ate", f"--context={os.environ['CTX_SUB']}"]
if os.environ.get("ATE_ENDPOINT"):
    KATE.append(f"--endpoint={os.environ['ATE_ENDPOINT']}")
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def api(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def wait_phase(want, timeout, need_all_running=False):
    end = time.time() + timeout
    while time.time() < end:
        s = api("api/state")
        if s.get("phase") == want and (not need_all_running or (s.get("burst") or {}).get("all_running_ms")):
            return s
        time.sleep(0.5)
    raise SystemExit(f"timeout waiting for phase {want}")


def placement():
    out = subprocess.run(KATE + ["get", "actors", "-a", ATESPACE, "-o", "json"],
                         check=True, capture_output=True, text=True).stdout
    acts = json.loads(out)
    acts = acts if isinstance(acts, list) else acts.get("actors", [])
    per, states = collections.defaultdict(list), collections.Counter()
    for a in acts:
        states[a["status"]["state"]] += 1
        nodes = a["status"].get("localSnapshotInfo", {}).get("nodeVmsWithLocalSnapshots", [])
        per[nodes[0] if nodes else "none"].append(a["metadata"]["name"])
    return per, states


def show(per):
    counts = sorted((len(v) for k, v in per.items() if k != "none"), reverse=True)
    return f"nodes={len(counts)} max={counts[0]} min={counts[-1]} top={counts[:8]}" if counts else "no placements"


for rnd in range(1, ROUNDS + 1):
    if api("api/state").get("phase") != "idle":
        raise SystemExit("driver not idle")
    per, states = placement()
    log(f"round {rnd}: {show(per)} states={dict(states)}")
    move = []
    for node, names in per.items():
        if node == "none":
            move += names
        elif len(names) > TARGET:
            move += sorted(names)[: len(names) - TARGET]
    if not move:
        log("balanced")
        break
    log(f"round {rnd}: re-creating {len(move)} agents")
    procs = [subprocess.Popen(KATE + ["delete", "actor", "--any-state", n, "-a", ATESPACE],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) for n in move]
    bad = [p.stderr.read().strip() for p in procs if p.wait() != 0]
    if bad:
        log(f"delete errors: {bad[:3]}")
    api("api/reconcile", {})
    s = wait_phase("idle", 180)
    log(f"reconcile: {s.get('note')}")
    api("api/burst", {"hold": True, "wake_only": True})
    s = wait_phase("running", 180, need_all_running=True)
    b = s.get("burst") or {}
    log(f"wake: all_running_ms={b.get('all_running_ms')} wake_failed={b.get('wake_failed')}")
    api("api/suspend", {})
    s = wait_phase("idle", 120)
    log(f"suspend: all_suspended_ms={(s.get('burst') or {}).get('all_suspended_ms')} note={s.get('note')}")

per, states = placement()
log(f"final: {show(per)} states={dict(states)}")
