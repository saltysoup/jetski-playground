#!/usr/bin/env python3
"""Re-creates the agents whose local snapshot was on a node that no longer exists.

With -rest-mode=pause an agent's snapshot lives on one node's local disk, and its
next wake is pinned to that node. When GKE recreates the node (upgrade, repair,
maintenance) it comes back under a new name, so every wake of those agents fails
with `ResourceExhausted: no free workers available` (no worker can ever free up
on a node that is gone). They are still PAUSED in ate-api, so the driver's
reconcile sees nothing wrong.

This script finds them (status.localSnapshotInfo.nodeVmsWithLocalSnapshots
names a node that `kubectl get nodes` doesn't list), deletes them, and then
runs the driver's reconcile, which re-creates every missing agent from the
template (and, as always, every agent whose restore failed or that is not at
rest). Their first wake then restores from the golden snapshot (slower, 7.0 s
for the light fleet on 2026-10-05), and they land on random nodes: run the
health check and rebalance.py afterwards (USER_GUIDE.md §4.5).

The driver must be idle (Suspend all first). DRY_RUN=1 only reports.

Usage: CTX_SUB=<kube context> [ATESPACE=ate-demo-sandbox] [DRIVER_URL=http://localhost:8090/] \
         [ATE_ENDPOINT=host:port] [PARALLEL=16] [DRY_RUN=1] python3 recreate_lost.py
For the Hermes fleet: ATESPACE=keynote-hermes DRIVER_URL=http://localhost:8092/
"""
import collections
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
import urllib.request

CTX = os.environ["CTX_SUB"]
ATESPACE = os.environ.get("ATESPACE", "ate-demo-sandbox")
BASE = os.environ.get("DRIVER_URL", "http://localhost:8090/")
PARALLEL = int(os.environ.get("PARALLEL", "16"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
KATE = ["kubectl", "ate", f"--context={CTX}"]
if os.environ.get("ATE_ENDPOINT"):
    KATE.append(f"--endpoint={os.environ['ATE_ENDPOINT']}")
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def sh(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def api(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def lost_agents():
    nodes = set(sh(["kubectl", f"--context={CTX}", "get", "nodes", "-o",
                    "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"]).split())
    acts = json.loads(sh(KATE + ["get", "actors", "-a", ATESPACE, "-o", "json"]))
    acts = acts if isinstance(acts, list) else acts.get("actors", [])
    lost, gone_nodes = [], collections.Counter()
    for a in acts:
        snap = a["status"].get("localSnapshotInfo", {}).get("nodeVmsWithLocalSnapshots", [])
        if snap and snap[0] not in nodes:
            lost.append(a["metadata"]["name"])
            gone_nodes[snap[0]] += 1
    return len(acts), sorted(lost), gone_nodes


def delete(name):
    err = ""
    for _ in range(3):
        try:
            p = subprocess.run(KATE + ["delete", "actor", "--any-state", name, "-a", ATESPACE],
                               capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            err = "timeout"
            continue
        low = p.stderr.lower()
        if p.returncode == 0 or "not found" in low or "notfound" in low:
            return ""
        err = p.stderr.strip().splitlines()[-1] if p.stderr.strip() else f"exit {p.returncode}"
        time.sleep(2)
    return err


total, lost, gone = lost_agents()
log(f"{ATESPACE}: {total} agents, {len(lost)} with their snapshot on {len(gone)} node(s) that no longer exist")
if DRY_RUN:
    if lost:
        log(f"[dry-run] would delete {len(lost)} agents ({', '.join(lost[:5])}{', …' if len(lost) > 5 else ''})")
    log("[dry-run] would run the driver's reconcile")
    sys.exit(0)
phase = api("api/state").get("phase")
if phase != "idle":
    raise SystemExit(f"driver at {BASE} is {phase!r}, not idle: run Suspend all first (post suspend)")

if lost:
    with cf.ThreadPoolExecutor(PARALLEL) as ex:
        errs = [(n, e) for n, e in zip(lost, ex.map(delete, lost)) if e]
    log(f"deleted {len(lost) - len(errs)} of {len(lost)}" + (f"; failed: {errs[:3]}" if errs else ""))
    if errs:
        raise SystemExit("some deletes failed: run this script again")

# The reconcile re-creates the deleted agents, plus any whose restore failed
# (`runsc restore`) and any not at rest.
api("api/reconcile", {})
time.sleep(2)
end = time.time() + 300
while time.time() < end:
    s = api("api/state")
    if s.get("phase") == "idle":
        log(f"reconcile: {s.get('note')}")
        break
    time.sleep(3)
else:
    raise SystemExit("timeout waiting for the reconcile")
