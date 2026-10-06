#!/usr/bin/env python3
"""Run 8d cloud-state poller: reservation, reservation block, and both instances.

The question run 8d asks is whether five Xid 63s produce emergent maintenance, and the
engineer named the reservation block as the place to verify it. So unlike probe.py -- which
only reads the reservation's top-level healthInfo -- this polls the block sub-resource too,
the same way run 7's watch.py did:

    res_health    resourceStatus.healthInfo.healthStatus         (reservation)
    block_health  reservationBlocks[].healthInfo.healthStatus    (the block)
    block_degraded  ...healthInfo.degradedCount

Run 6/7 measured the block flipping to DEGRADED in 24.5 s and 35.6 s from a kmsg Xid 79,
so the poll interval is 15 s: fast enough to bound that latency to the same order, slow
enough not to matter against the API.

Only CHANGE lines and a periodic heartbeat are written, so the log stays readable across a
multi-hour run. -6df3 is polled as a control: if both nodes degrade together it is not our
injection.
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

EVERY = int(sys.argv[1]) if len(sys.argv) > 1 else 15
BUDGET = int(sys.argv[2]) if len(sys.argv) > 2 else 4 * 3600
HEARTBEAT = 300

_tok = {"v": None, "exp": 0.0}


def token():
    # Cached for 50 min; ADC tokens last an hour and shelling out every 15 s is wasteful.
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
        # The field the engineer pointed at: a maintenance reason surfacing on the block.
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
    return out


def main():
    t0 = time.time()
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
                      f"34t3={cur.get('34t3_maint')}", flush=True)
                last_hb = time.time()
        prev = cur
        time.sleep(EVERY)
    print(f"# budget {BUDGET}s exhausted", flush=True)


if __name__ == "__main__":
    main()
