#!/usr/bin/env python3
"""Lists the light-fleet worker pods to replace when agents won't come to rest.

Run it after Suspend all, with the driver idle. Each Substrate node runs an
ate-node-tuner pod (hostPID), which lists the node's gVisor sandboxes
(`runsc-sandbox` processes) and the worker pod each one belongs to (the
`--pod-uid` of its parent `ateom`). With every agent at rest, no worker should
run a sandbox. A worker pod is listed when:
  - it runs a sandbox that ate-api assigns to no agent (a leftover sandbox);
  - ate-api assigns it to an agent that is not at rest, for example one stuck
    PAUSING after `runsc checkpoint` failed or after its sandbox died;
  - one of its containers restarted (an OOM kill, README §9).
Delete the listed pods (the WorkerPool recreates them), then run the driver's
reconcile, which re-creates every agent that is not at rest (README §7).

On 2026-10-07, after a 21-hour Simulate Traffic run, the same check (then run
by hand) found 22 workers to replace: 20 with leftover sandboxes and the
workers of the 2 agents stuck PAUSING.

Read-only. NAMES_ONLY=1 prints only the pod names, for xargs.

Usage: CTX_SUB=<kube context> [ATE_ENDPOINT=host:port] [DRIVER_URL=http://localhost:8090/] \\
         [NODE_MATCH=substrate-c4] [NAMES_ONLY=1] python3 sandbox_scan.py
"""
import collections
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import urllib.request

ATESPACE = os.environ.get("ATESPACE", "ate-demo-sandbox")
WORKER_NS = os.environ.get("WORKER_NS", "ate-demo-sandbox")
NODE_MATCH = os.environ.get("NODE_MATCH", "substrate-c4")
BASE = os.environ.get("DRIVER_URL", "http://localhost:8090/")
NAMES_ONLY = os.environ.get("NAMES_ONLY", "0") == "1"
AT_REST = {"ACTOR_STATE_PAUSED", "ACTOR_STATE_SUSPENDED", "ACTOR_STATE_UNSPECIFIED"}
# Runs in a node-tuner pod. One line per gVisor sandbox: "<age> <worker pod uid>".
SCAN = r"""PS=$(ps -eo pid,ppid,etime)
for d in /proc/[0-9]*; do
  [ "$(tr '\0' '\n' < $d/cmdline 2>/dev/null | head -n 1)" = runsc-sandbox ] || continue
  set -- $(echo "$PS" | awk -v p=${d#/proc/} '$1==p{print $2, $3}') - -
  echo "$2 $(tr '\0' '\n' < /proc/$1/cmdline 2>/dev/null | sed -n 's/^--pod-uid=//p')"
done"""


def classify(sandboxes, pods, actors):
    """Picks the worker pods to replace.

    sandboxes: [(node, age, worker pod uid or "")]; pods: {uid: (name, restarts)}
    for the light fleet's workers; actors: ate-api actors (dicts).
    Returns ({pod name: reason}, [notes]).
    """
    assigned = {}
    for a in actors:
        wa = a["status"].get("workerAssignment") or {}
        if wa.get("workerPodUid"):
            assigned[wa["workerPodUid"]] = (a, wa)
    replace, notes = {}, []
    for node, age, uid in sandboxes:
        if not uid:
            notes.append(f"sandbox on {node} (up {age}) with an unknown worker; not listed")
        elif uid in pods and uid not in assigned:
            replace[pods[uid][0]] = f"leftover sandbox on {node}, up {age}"
    for uid, (a, wa) in sorted(assigned.items(), key=lambda kv: kv[1][0]["metadata"]["name"]):
        state = a["status"].get("state", "")
        if state in AT_REST:
            continue
        if uid in pods:
            replace[pods[uid][0]] = f"{a['metadata']['name']} is {state}"
        else:
            notes.append(f"{a['metadata']['name']} is {state} on worker {wa.get('workerPod')}, "
                         "which no longer exists; reconcile alone re-creates it")
    for uid, (name, restarts) in pods.items():
        if restarts:
            replace.setdefault(name, f"restarted {restarts} times")
    return replace, notes


def sh(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def main():
    ctx = os.environ["CTX_SUB"]
    kate = ["kubectl", "ate", f"--context={ctx}"]
    if os.environ.get("ATE_ENDPOINT"):
        kate.append(f"--endpoint={os.environ['ATE_ENDPOINT']}")
    with urllib.request.urlopen(BASE + "api/state", timeout=10) as r:
        phase = json.load(r).get("phase")
    if phase != "idle":
        sys.exit(f"driver phase is {phase!r}, not idle: run this after Suspend all")

    tuners = [l.split() for l in sh(["kubectl", "--context", ctx, "-n", "ate-system", "get", "pods",
                                      "-l", "app=ate-node-tuner", "-o", "wide", "--no-headers"]).splitlines()]
    tuners = [(t[0], t[6]) for t in tuners if len(t) > 6 and NODE_MATCH in t[6]]
    with cf.ThreadPoolExecutor(32) as ex:
        outs = list(ex.map(lambda t: sh(["kubectl", "--context", ctx, "-n", "ate-system", "exec", t[0],
                                         "--", "sh", "-c", SCAN]), tuners))
    sandboxes = []
    for (_, node), out in zip(tuners, outs):
        for l in out.splitlines():
            p = l.split()
            if p:
                sandboxes.append((node, p[0], p[1] if len(p) > 1 else ""))

    pods = {}
    for l in sh(["kubectl", "--context", ctx, "-n", WORKER_NS, "get", "pods", "-o",
                 "jsonpath={range .items[*]}{.metadata.uid} {.metadata.name} "
                 "{.status.containerStatuses[*].restartCount}{\"\\n\"}{end}"]).splitlines():
        p = l.split()
        if len(p) >= 2:
            pods[p[0]] = (p[1], sum(int(x) for x in p[2:] if x.isdigit()))
    acts = json.loads(sh(kate + ["get", "actors", "-a", ATESPACE, "-o", "json"]))
    acts = acts if isinstance(acts, list) else acts.get("actors", [])

    replace, notes = classify(sandboxes, pods, acts)
    if NAMES_ONLY:
        for name in sorted(replace):
            print(name)
        return
    per_node = collections.Counter(node for node, _, _ in sandboxes)
    states = collections.Counter(a["status"].get("state", "") for a in acts)
    print(f"tuner pods {len(tuners)}, sandboxes {len(sandboxes)} on {len(per_node)} nodes, "
          f"workers {len(pods)}, agents {dict(states)}")
    for n in notes:
        print("note:", n)
    print(f"worker pods to replace: {len(replace)}")
    for name, why in sorted(replace.items(), key=lambda kv: kv[1]):
        print(f"  {name}  {why}")


if __name__ == "__main__":
    main()
