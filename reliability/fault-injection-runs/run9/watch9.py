#!/usr/bin/env python3
"""Run 9 poller — reservation, block, both instances, AND Kubernetes node state.

Differences from watch8d.py, each one a lesson from a previous run:

1. **Node state is polled.** Run 9's headline metric is "label -> node Ready with 8
   allocatable GPUs". watch8d.py only read the GCE API, so 8d's B12/B13 had to be
   reconstructed afterwards from lastTransitionTime. Here `node_ready`, `node_gpus`,
   `cluster_gpus` and maintenance-related taints are first-class polled fields.

2. **`lastStartTimestamp` is recorded.** This is the one field that let us recover 8f's
   repair completion after the poller died. Having it in-line means the log is
   self-sufficient even if the process is killed before the end.

3. **Append-safe.** Launched with >>, so a supervisor restart never truncates history.
   Every restart writes a RESTART line, so gaps in coverage are explicit rather than
   silent -- an unmarked gap is what made 8f's timeline ambiguous.

4. **Cadence is an argument and expected to change mid-run.** 5 s for detection, 10 s for
   the repair. The 15 s used in 8f was wider than the effect we were trying to measure.

Usage: watch9.py [EVERY] [BUDGET]
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

P = "gpu-launchpad-playground"
Z = "europe-west4-b"
RES = "nvidia-b200-6bsoymep8ylww"
NODES = {
    "34t3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3",   # target
    "6df3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3",   # control
}
TARGET = NODES["34t3"]

EVERY = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
BUDGET = int(sys.argv[2]) if len(sys.argv) > 2 else 8 * 3600
HEARTBEAT = 300

_tok = {"v": None, "exp": 0.0}


def token():
    now = time.time()
    if _tok["v"] is None or now > _tok["exp"]:
        _tok["v"] = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True).stdout.strip()
        _tok["exp"] = now + 3000
    return _tok["v"]


def api(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token()})
    try:
        return json.load(urllib.request.urlopen(req, timeout=45))
    except urllib.error.HTTPError as ex:
        if ex.code == 401:
            _tok["v"] = None
        return {"_err": f"HTTP {ex.code}"}
    except Exception as ex:  # noqa: BLE001
        return {"_err": str(ex)[:120]}


def kget(args, timeout=20):
    try:
        r = subprocess.run(["kubectl", "get"] + args, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def k8s(out):
    """Node readiness, allocatable GPUs and maintenance taints/labels."""
    jp = ('{.status.conditions[?(@.type=="Ready")].status}|'
          '{.status.allocatable.nvidia\\.com/gpu}|'
          '{range .spec.taints[*]}{.key}={.effect},{end}|'
          '{.metadata.labels.cloud\\.google\\.com/perform-maintenance}')
    raw = kget(["node", TARGET, "-o", f"jsonpath={jp}"])
    parts = (raw.split("|") + ["", "", "", ""])[:4]
    out["node_ready"] = parts[0] or None
    out["node_gpus"] = parts[1] or None
    # Only maintenance-relevant taints; the standard not-ready/unreachable ones are noise
    # until B10, where they are exactly what we want to timestamp.
    taints = [t for t in parts[2].split(",") if t and
              any(k in t for k in ("maintenance", "not-ready", "unreachable", "unschedulable"))]
    out["node_taints"] = ",".join(sorted(taints)) or None
    out["node_pm_label"] = parts[3] or None

    tot = 0
    for short, full in NODES.items():
        g = kget(["node", full, "-o",
                  "jsonpath={.status.allocatable.nvidia\\.com/gpu}"])
        out[f"{short}_gpus"] = g or None
        if g.isdigit():
            tot += int(g)
    out["cluster_gpus"] = tot


def snap():
    base = f"https://compute.googleapis.com/compute/v1/projects/{P}/zones/{Z}"
    r = api(f"{base}/reservations/{RES}")
    rs = r.get("resourceStatus", {})
    rm = rs.get("reservationMaintenance", {})
    out = {
        "res_health": rs.get("healthInfo", {}).get("healthStatus"),
        "pending": rm.get("maintenancePendingCount"),
        "ongoing": rm.get("maintenanceOngoingCount"),
    }
    b = api(f"{base}/reservations/{RES}/reservationBlocks")
    for x in b.get("items", []):
        hi = x.get("healthInfo", {})
        out["block_health"] = hi.get("healthStatus")
        out["block_degraded"] = hi.get("degradedCount")
        bm = x.get("reservationMaintenance", {})
        out["block_reasons"] = bm.get("maintenanceReasons")
        out["block_pending"] = bm.get("maintenancePendingCount")
        out["block_ongoing"] = bm.get("maintenanceOngoingCount")
    for short, full in NODES.items():
        i = api(f"{base}/instances/{full}")
        um = i.get("resourceStatus", {}).get("upcomingMaintenance") or {}
        out[f"{short}_vm"] = i.get("status")
        out[f"{short}_maint"] = um.get("maintenanceStatus", "CLEAR")
        out[f"{short}_reasons"] = um.get("maintenanceReasons")
        out[f"{short}_type"] = um.get("type")
        out[f"{short}_resched"] = um.get("canReschedule")
        out[f"{short}_win"] = um.get("windowStartTime")
        out[f"{short}_winend"] = um.get("windowEndTime")
        if short == "34t3":
            out["34t3_laststart"] = i.get("lastStartTimestamp")
    k8s(out)
    return out


def main():
    t0 = time.time()
    print(f"# RESTART pid={__import__('os').getpid()} every={EVERY}s budget={BUDGET}s "
          f"at {datetime.now(timezone.utc).isoformat()}", flush=True)
    prev = None
    last_hb = 0.0
    while time.time() - t0 < BUDGET:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        el = time.time() - t0
        cur = snap()
        if prev is None:
            print(f"{ts} | T {el:+9.1f}s | BASELINE | {json.dumps(cur, sort_keys=True)}",
                  flush=True)
        else:
            diff = {k: [prev.get(k), v] for k, v in cur.items() if prev.get(k) != v}
            if diff:
                print(f"{ts} | T {el:+9.1f}s | CHANGE   | {json.dumps(diff, sort_keys=True)}",
                      flush=True)
            elif time.time() - last_hb > HEARTBEAT:
                print(f"{ts} | T {el:+9.1f}s | heartbeat | "
                      f"res={cur.get('res_health')} block={cur.get('block_health')} "
                      f"34t3={cur.get('34t3_maint')} vm={cur.get('34t3_vm')} "
                      f"ready={cur.get('node_ready')} gpus={cur.get('node_gpus')}",
                      flush=True)
                last_hb = time.time()
        prev = cur
        time.sleep(EVERY)
    print(f"# budget {BUDGET}s exhausted", flush=True)


if __name__ == "__main__":
    main()
