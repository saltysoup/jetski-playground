#!/usr/bin/env python3
"""Reports every light agent's local checkpoint size, per node.

Each Substrate node runs an ate-node-tuner pod (hostPath access), which can
`du` the gVisor local checkpoints under /var/lib/ateom-gvisor/actors/<uid>.
Sizes are mapped back to agent names through `kubectl ate get actors`.

A fresh light agent checkpoints at 1.4-3.0 MiB. Bigger checkpoints restore
more slowly; on 2026-10-02 they came from zombie `wget` processes left in the
snapshot (see README §9), and re-creating those agents fixed it. The report
also counts checkpoint directories left behind by deleted actors.

Usage: CTX_SUB=<kube context> [ATE_ENDPOINT=host:port] [NODE_MATCH=substrate-c4] \
         python3 ck_scan.py
"""
import collections
import concurrent.futures as cf
import json
import os
import subprocess

CTX = os.environ["CTX_SUB"]
ATESPACE = os.environ.get("ATESPACE", "ate-demo-sandbox")
NODE_MATCH = os.environ.get("NODE_MATCH", "substrate-c4")
KATE = ["kubectl", "ate", f"--context={CTX}"]
if os.environ.get("ATE_ENDPOINT"):
    KATE.append(f"--endpoint={os.environ['ATE_ENDPOINT']}")
SCAN = ("cd /var/lib/ateom-gvisor/actors && for d in *; do "
        "echo \"$d $(du -sk $d/local-checkpoint 2>/dev/null | cut -f1)\"; done")


def sh(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


pods = [l.split()[0] for l in sh(["kubectl", "--context", CTX, "-n", "ate-system", "get", "pods",
                                   "-l", "app=ate-node-tuner", "-o", "wide", "--no-headers"]).splitlines()
        if NODE_MATCH in l]
acts = json.loads(sh(KATE + ["get", "actors", "-a", ATESPACE, "-o", "json"]))
acts = acts if isinstance(acts, list) else acts.get("actors", [])
byuid = {a["metadata"]["uid"]: a for a in acts}

with cf.ThreadPoolExecutor(32) as ex:
    outs = list(ex.map(lambda p: sh(["kubectl", "--context", CTX, "-n", "ate-system", "exec", p,
                                     "--", "sh", "-c", SCAN]), pods))

size, stale, stale_kb = {}, 0, 0
for out in outs:
    for l in out.splitlines():
        p = l.split()
        if len(p) != 2 or not p[1].isdigit():
            continue
        a = byuid.get(p[0])
        if a is None:
            stale += 1
            stale_kb += int(p[1])
            continue
        size[a["metadata"]["name"]] = int(p[1]) / 1024

v = sorted(size.values())
print(f"tuner pods {len(pods)}, agents {len(acts)}, with local checkpoint {len(v)}, "
      f"stale dirs (deleted actors) {stale} using {stale_kb / 1024:.0f} MiB")
if v:
    q = lambda f: v[min(len(v) - 1, int(f * len(v)))]
    print(f"checkpoint MiB: min {v[0]:.1f} p50 {q(.5):.1f} p90 {q(.9):.1f} "
          f"p99 {q(.99):.1f} max {v[-1]:.1f}")
    print("histogram (MiB bucket: count):", dict(sorted(collections.Counter(int(x) for x in v).items())))
    print("largest:", [(n, round(mb, 1)) for n, mb in sorted(size.items(), key=lambda kv: -kv[1])[:5]])
