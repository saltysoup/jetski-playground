#!/usr/bin/env python3
"""Run 8 poller: does a repeated Xid 48 (Double Bit ECC) ever produce a maintenance event?

Run 7's watch.py was built to measure latencies (M1..M4) off a single T0 and had a fixed
tick budget, which is how it managed to die twelve minutes before B12. Run 8 asks a much
narrower question over a much longer wall clock -- five inject/reboot iterations, roughly
25 minutes each -- so this is a plain change detector with a generous runtime and no
Pub/Sub draining.

What it watches, every POLL seconds:

  * -34t3 (target)   VM status, resourceStatus.upcomingMaintenance VERBATIM
  * -6df3 (control)  VM status, maintenance status -- if this moves, the signal is not ours
  * reservation      healthStatus / pending / ongoing, block health + degradedCount
  * node -34t3       Ready condition, bootID (the reboot signal: a guest OS reboot leaves
                     the instance RUNNING and lastStartTimestamp untouched, so bootID is
                     the only honest server-side "it actually rebooted" marker),
                     allocatable GPUs, maintenance/reboot labels, taints, and the
                     node.gke.io/upcoming-maintenance annotation verbatim

It logs only on change, plus a heartbeat every HEARTBEAT ticks so a silent log is
distinguishable from a dead poller -- run 7's failure mode.

`phase.txt` in this directory is read every tick and stamped into each line, so the
loop driver can mark which iteration and sub-stage produced a given change.

    python3 watch8.py <T0-iso> [max-hours]
"""
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

P = "gpu-launchpad-playground"
Z = "europe-west4-b"
RES = "nvidia-b200-6bsoymep8ylww"
NODES = {
    "34t3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3",  # target
    "6df3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3",  # control
}
TARGET = "34t3"

POLL = 20.0        # seconds between snapshots
HEARTBEAT = 15     # ticks between "still alive" lines (= 5 min)

T0 = datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
MAXH = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
HERE = os.path.dirname(os.path.abspath(__file__))
PHASE = os.path.join(HERE, "phase.txt")

_tok = {"v": "", "t": 0.0}


def token():
    # ADC tokens last ~60 min. The gcloud *user* credential is dead (Context Aware
    # Access); ADC is separate and still mints tokens, which is why this works.
    if time.time() - _tok["t"] > 1800:
        _tok["v"] = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True).stdout.strip()
        _tok["t"] = time.time()
    return _tok["v"]


def api(url):
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token()})
    try:
        return json.load(urllib.request.urlopen(req, timeout=45))
    except urllib.error.HTTPError as ex:
        return {"_err": f"HTTP {ex.code}", "_body": ex.read().decode()[:200]}
    except Exception as ex:  # noqa: BLE001
        return {"_err": str(ex)[:200]}


def kubectl(*a, timeout=30):
    try:
        return subprocess.run(["kubectl", *a], capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def phase():
    try:
        with open(PHASE) as f:
            return f.read().strip() or "-"
    except Exception:  # noqa: BLE001
        return "-"


def emit(kind, msg, ts=None):
    ts = ts or datetime.datetime.now(datetime.timezone.utc)
    print("%s | T%+9.1fs | %-6s | %-13s | %s" % (
        ts.strftime("%H:%M:%S.%f")[:-3], (ts - T0).total_seconds(),
        phase(), kind, msg), flush=True)


def snap():
    """One full snapshot, flattened to comparable scalars."""
    base = f"https://compute.googleapis.com/compute/v1/projects/{P}/zones/{Z}"
    out = {}

    r = api(f"{base}/reservations/{RES}")
    rs = r.get("resourceStatus", {})
    rm = rs.get("reservationMaintenance", {})
    out["res_health"] = rs.get("healthInfo", {}).get("healthStatus")
    out["res_degraded_blocks"] = rs.get("healthInfo", {}).get("degradedBlockCount")
    out["res_pending"] = rm.get("maintenancePendingCount")
    out["res_ongoing"] = rm.get("maintenanceOngoingCount")

    b = api(f"{base}/reservations/{RES}/reservationBlocks")
    for x in b.get("items", []):
        hi = x.get("healthInfo", {})
        out["block_health"] = hi.get("healthStatus")
        out["block_degraded"] = hi.get("degradedCount")

    for short, full in NODES.items():
        i = api(f"{base}/instances/{full}")
        um = i.get("resourceStatus", {}).get("upcomingMaintenance") or {}
        out[f"{short}_vm"] = i.get("status")
        out[f"{short}_maint"] = um.get("maintenanceStatus", "CLEAR")
        if short == TARGET:
            # The whole point of run 8. Kept verbatim rather than field-by-field: run 6
            # discarded `type` by only recording the fields it thought mattered, and the
            # ECC reason -- if it ever shows up -- may well arrive in a field nobody has
            # thought to name yet.
            out["gce_upcoming"] = json.dumps(um, sort_keys=True) if um else None

    # Kubernetes side of the same node.
    raw = kubectl("get", "node", NODES[TARGET], "-o", "json")
    try:
        n = json.loads(raw)
    except Exception:  # noqa: BLE001
        n = {}
    if n:
        md = n.get("metadata", {})
        conds = {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}
        out["node_ready"] = conds.get("Ready")
        out["node_bootid"] = n.get("status", {}).get("nodeInfo", {}).get("bootID")
        out["node_gpu"] = n.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu")
        out["node_labels"] = json.dumps(
            {k: v for k, v in md.get("labels", {}).items()
             if "maintenance" in k or "reboot" in k or "repair" in k}, sort_keys=True)
        out["node_taints"] = json.dumps(
            sorted(t["key"] for t in (n.get("spec", {}).get("taints") or [])))
        out["k8s_upcoming"] = md.get("annotations", {}).get(
            "node.gke.io/upcoming-maintenance")

    # Cluster-wide allocatable GPUs -- 16 when whole, 8 while -34t3 is rebooting.
    tot = 0
    for l in kubectl("get", "nodes", "-o",
                     "jsonpath={range .items[*]}{.status.allocatable.nvidia\\.com/gpu}"
                     "{\"\\n\"}{end}").splitlines():
        try:
            tot += int(l.strip())
        except Exception:  # noqa: BLE001
            pass
    out["cluster_gpu"] = tot
    return out


def main():
    emit("START", f"T0={T0.isoformat()} poll={POLL}s max={MAXH}h")
    prev = {}
    tick = 0
    deadline = time.time() + MAXH * 3600
    while time.time() < deadline:
        tick += 1
        cur = snap()
        # A transient API error should not read as a state change in both directions.
        diff = {k: v for k, v in cur.items() if prev.get(k) != v}
        if prev and diff:
            emit("CHANGE", json.dumps({k: [prev.get(k), v] for k, v in diff.items()}))
        elif not prev:
            emit("BASELINE", json.dumps(cur, sort_keys=True))
        elif tick % HEARTBEAT == 0:
            emit("heartbeat", "tick=%d %s vm=%s ready=%s maint=%s gpu=%s" % (
                tick, TARGET, cur.get(f"{TARGET}_vm"), cur.get("node_ready"),
                cur.get(f"{TARGET}_maint"), cur.get("cluster_gpu")))
        prev = cur
        time.sleep(POLL)
    emit("END", f"budget exhausted after {MAXH}h")


if __name__ == "__main__":
    main()
