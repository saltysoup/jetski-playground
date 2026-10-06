#!/usr/bin/env python3
"""Run-3 poller. Watches, from a single T0, the surfaces that should react to an
injected XID 79 on node -34t3:

  * Cloud Logging serial-console output (keyed by numeric instance ID)   -> M1
  * reservation block healthStatus / maintenance counters                -> M3
  * per-instance resourceStatus.upcomingMaintenance + maintenanceReasons -> M4
  * the GKE node annotation / taints written by gpu-maintenance-handler

Usage: poll.py <T0-iso8601> [max_minutes]
Appends one line per observation to run3/timeline.log and prints the same.
"""
import json, sys, time, datetime, subprocess, urllib.request, urllib.parse, os

P   = "gpu-launchpad-playground"
Z   = "europe-west4-b"
RES = "nvidia-b200-6bsoymep8ylww"
INST = "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3"
IID  = "8713135250882265303"

T0  = datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
MAXM = float(sys.argv[2]) if len(sys.argv) > 2 else 360.0
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timeline.log")

_tok = {"v": "", "t": 0.0}
def token():
    # ADC tokens last ~60 min; refresh every 30.
    if time.time() - _tok["t"] > 1800:
        _tok["v"] = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True).stdout.strip()
        _tok["t"] = time.time()
    return _tok["v"]

def api(url, body=None):
    hdr = {"Authorization": "Bearer " + token()}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    try:
        return json.load(urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=hdr), timeout=30))
    except Exception as ex:
        detail = ex.read().decode()[:200] if hasattr(ex, "read") else str(ex)[:200]
        return {"_err": detail}

def elapsed(ts):
    return (ts - T0).total_seconds()

def emit(kind, msg, ts=None):
    ts = ts or datetime.datetime.now(datetime.timezone.utc)
    line = "%s | T+%8.1fs | %-14s | %s" % (ts.strftime("%H:%M:%S"), elapsed(ts), kind, msg)
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

def check_log():
    """First serial-console XID line at or after T0. Returns its timestamp."""
    flt = ('log_id("serialconsole.googleapis.com/serial_port_1_output") '
           'resource.type="gce_instance" '
           f'resource.labels.instance_id="{IID}" '
           '"NVRM: Xid" '
           f'timestamp >= "{T0.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    d = api("https://logging.googleapis.com/v2/entries:list",
            {"resourceNames": [f"projects/{P}"], "filter": flt,
             "orderBy": "timestamp asc", "pageSize": 5})
    if "_err" in d:
        return None, d["_err"]
    for e in d.get("entries", []):
        return datetime.datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")), \
               (e.get("textPayload") or "")[:160]
    return None, None

def check_cloud():
    base = f"https://compute.googleapis.com/compute/v1/projects/{P}/zones/{Z}"
    r = api(f"{base}/reservations/{RES}")
    rs = r.get("resourceStatus", {})
    rm = rs.get("reservationMaintenance", {})
    res = {"health": rs.get("healthInfo", {}).get("healthStatus"),
           "pending": rm.get("maintenancePendingCount"),
           "ongoing": rm.get("maintenanceOngoingCount")}
    b = api(f"{base}/reservations/{RES}/reservationBlocks")
    blk = {}
    for x in b.get("items", []):
        blk = {"health": x.get("healthInfo", {}).get("healthStatus"),
               "healthy": x.get("healthInfo", {}).get("healthyCount"),
               "degraded": x.get("healthInfo", {}).get("degradedCount")}
    i = api(f"{base}/instances/{INST}")
    um = i.get("resourceStatus", {}).get("upcomingMaintenance", {})
    ins = {"status": i.get("status"),
           "maint": um.get("maintenanceStatus", "CLEAR"),
           "reasons": um.get("maintenanceReasons"),
           "canResched": um.get("canReschedule"),
           "winStart": um.get("windowStartTime")}
    return res, blk, ins

def check_node():
    try:
        n = json.loads(subprocess.run(
            ["kubectl", "get", "node", INST, "-o", "json"],
            capture_output=True, text=True, timeout=30).stdout)
    except Exception:
        return None
    ready = next((c["status"] for c in n["status"]["conditions"] if c["type"] == "Ready"), "?")
    return {"ready": ready,
            "gpu": n["status"]["allocatable"].get("nvidia.com/gpu", "0"),
            "taints": [t["key"].split("/")[-1] for t in n["spec"].get("taints", [])],
            "annot": n["metadata"].get("annotations", {}).get("node.gke.io/upcoming-maintenance")}

emit("START", f"T0={T0.isoformat()} watching {INST} ({IID})")

seen = {"log": False}
last = {"cloud": None, "node": None}
tick = 0
deadline = time.time() + MAXM * 60

while time.time() < deadline:
    tick += 1

    if not seen["log"]:
        ts, payload = check_log()
        if ts:
            seen["log"] = True
            emit("M1-LOG", f"FIRST XID IN CLOUD LOGGING at {ts.isoformat()} :: {payload}", ts)
        elif payload:
            emit("log-err", payload)

    # cloud + node state every 3rd tick (~30s)
    if tick % 3 == 1:
        res, blk, ins = check_cloud()
        cur = json.dumps([res, blk, ins], sort_keys=True)
        if cur != last["cloud"]:
            emit("CHANGE-CLOUD",
                 f"res={res} block={blk} inst={ins}")
            last["cloud"] = cur

        nd = check_node()
        curn = json.dumps(nd, sort_keys=True)
        if curn != last["node"]:
            a = nd.get("annot") if nd else None
            emit("CHANGE-NODE",
                 f"ready={nd['ready']} gpu={nd['gpu']} taints={nd['taints']} "
                 f"annot={'yes' if a else 'no'}{' '+a[:150] if a else ''}" if nd
                 else "node unreadable")
            last["node"] = curn

    time.sleep(10)

emit("END", f"poller exit after {MAXM} min")
