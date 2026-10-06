#!/usr/bin/env python3
"""Run-6 poller: one process watching every surface from before T0 to full recovery.

Supersedes run3/poll.py (M1/M3) and run4/watch-replacement.py (repair timeline) by
merging them, so a single clock stamps every milestone and the phases can overlap.

What it fixes relative to those two:

  M1  Run 3 reported the *entry* timestamp. That is the serial console's own clock, so it
      measures console->log wall time only if the two clocks agree. This records
      receiveTimestamp (Logging's ingest clock) alongside it and reports both. The pair is
      also immune to the read-quota 429s this project is currently throwing: the numbers
      come from the entry, not from when the poll happened to succeed.

  M2  Never measured, in three runs. Measured here from the Pub/Sub message publishTime,
      again a server clock rather than a poll time.

  M3  Run 3's <=17.5 s was an artefact of a 30 s cloud cadence whose first tick landed at
      T+17.5 s. Cloud surfaces are polled at 5 s for the first CLOUD_FAST_MIN minutes.

  B7  Run 3 recovered the reason after the fact from managedFields. maintenanceReasons and
      canReschedule are polled directly here.

Both GPU nodes are watched. The reservation schedules GROUPED maintenance, so whether a
single-node label drags the healthy node with it is the open question assertion 3 tests.

Usage: watch.py <T0-iso8601> [max_minutes]
Unattended-safe: never raises on a transient API error, logs it and carries on.
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
    "34t3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3",  # target of the injection
    "6df3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3",  # control
}
IIDS = {"34t3": "8713135250882265303", "6df3": "921428950843228374"}
TARGET = "34t3"
SUB = "projects/gpu-launchpad-playground/subscriptions/xid-alerts-sub"
SUBMISSION = os.environ.get("SUBMISSION", "dapo-run6")

CLOUD_FAST_MIN = 20.0   # 5 s cloud cadence for this long after T0, then 30 s
M1_WINDOW_MIN = 30.0    # stop hunting the log entry after this
M2_WINDOW_MIN = 60.0    # stop draining the subscription after this

T0 = datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
MAXM = float(sys.argv[2]) if len(sys.argv) > 2 else 720.0
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "timeline.log")

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


def api(url, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET"),
        headers={"Authorization": "Bearer " + token(),
                 "Content-Type": "application/json"})
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


def emit(kind, msg, ts=None):
    ts = ts or datetime.datetime.now(datetime.timezone.utc)
    line = "%s | T%+10.3fs | %-14s | %s" % (
        ts.strftime("%H:%M:%S.%f")[:-3], (ts - T0).total_seconds(), kind, msg)
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- snapshots

def snap_nodes():
    raw = kubectl("get", "nodes", "-o", "json")
    try:
        byname = {n["metadata"]["name"]: n for n in json.loads(raw)["items"]}
    except Exception:  # noqa: BLE001
        return {"_err": "kubectl unreadable"}
    out = {}
    for short, full in NODES.items():
        n = byname.get(full)
        if n is None:
            out[short] = {"present": False}   # a missing node is itself the signal
            continue
        md, st, spec = n["metadata"], n["status"], n["spec"]
        um = md.get("annotations", {}).get("node.gke.io/upcoming-maintenance")
        try:
            um = json.loads(um).get("maintenance_status") if um else None
        except Exception:  # noqa: BLE001
            um = "unparseable"
        lb = md.get("labels", {})
        out[short] = {
            "ready": next((c["status"] for c in st["conditions"] if c["type"] == "Ready"), "?"),
            "gpu": st.get("allocatable", {}).get("nvidia.com/gpu", "0"),
            "taints": sorted(t["key"].split("/")[-1] for t in spec.get("taints", [])),
            "unsched": spec.get("unschedulable", False),
            "maint_status": um,
            "active_maint": lb.get("cloud.google.com/active-node-maintenance"),
            "perform_label": lb.get("cloud.google.com/perform-maintenance"),
            "uid": md.get("uid", "")[:8],       # changes only on a real node replacement
        }
    return out


def snap_pods():
    raw = kubectl("get", "pods", "-n", "default", "-l", "ray.io/is-ray-node=yes", "-o", "json")
    try:
        items = json.loads(raw)["items"]
    except Exception:  # noqa: BLE001
        return {"_err": "pods unreadable"}
    out = {}
    for p in items:
        st = p["status"]
        cs = st.get("containerStatuses", [])
        node = p["spec"].get("nodeName", "?")
        out[p["metadata"]["name"][-12:]] = {
            "phase": st.get("phase"),
            "ready": f"{sum(1 for c in cs if c.get('ready'))}/{len(cs) or 1}",
            "node": next((s for s, f in NODES.items() if f == node), node[-4:] if node else "?"),
            "deleting": p["metadata"].get("deletionTimestamp") is not None,
        }
    return out


def snap_cluster_gpu():
    raw = kubectl("get", "nodes", "-o",
                  "jsonpath={range .items[*]}{.status.allocatable.nvidia\\.com/gpu}{\"\\n\"}{end}")
    tot = 0
    for l in raw.splitlines():
        try:
            tot += int(l.strip())
        except Exception:  # noqa: BLE001
            pass
    return tot


def snap_cloud():
    base = f"https://compute.googleapis.com/compute/v1/projects/{P}/zones/{Z}"
    r = api(f"{base}/reservations/{RES}")
    rs = r.get("resourceStatus", {})
    rm = rs.get("reservationMaintenance", {})
    out = {"res_health": rs.get("healthInfo", {}).get("healthStatus"),
           "pending": rm.get("maintenancePendingCount"),
           "ongoing": rm.get("maintenanceOngoingCount")}
    b = api(f"{base}/reservations/{RES}/reservationBlocks")
    for x in b.get("items", []):
        hi = x.get("healthInfo", {})
        out["block_health"] = hi.get("healthStatus")
        out["block_degraded"] = hi.get("degradedCount")
    for short, full in NODES.items():
        i = api(f"{base}/instances/{full}")
        um = i.get("resourceStatus", {}).get("upcomingMaintenance", {})
        out[f"{short}_vm"] = i.get("status")
        out[f"{short}_maint"] = um.get("maintenanceStatus", "CLEAR")
        # B7: the reason is the whole point — one bit of health vs. an actual failure class.
        out[f"{short}_reasons"] = um.get("maintenanceReasons")
        out[f"{short}_resched"] = um.get("canReschedule")
        out[f"{short}_win"] = um.get("windowStartTime")
    return out


def snap_job(head):
    raw = kubectl("exec", "-n", "default", "-c", "ray-head", head, "--",
                  "ray", "job", "status", SUBMISSION, timeout=60)
    for t in ("SUCCEEDED", "FAILED", "STOPPED", "RUNNING", "PENDING"):
        if t in raw:
            return t
    return "?"


def check_m1():
    """First NVRM Xid on the target since T0. Filter is run 3's, proven to match."""
    flt = ('log_id("serialconsole.googleapis.com/serial_port_1_output") '
           'resource.type="gce_instance" '
           f'resource.labels.instance_id="{IIDS[TARGET]}" '
           '"NVRM: Xid" '
           f'timestamp >= "{T0.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    d = api("https://logging.googleapis.com/v2/entries:list",
            {"resourceNames": [f"projects/{P}"], "filter": flt,
             "orderBy": "timestamp asc", "pageSize": 5})
    if "_err" in d:
        return None
    for e in d.get("entries", []):
        return e
    return None


def check_m2():
    """Alert notifications land on a Pub/Sub topic; publishTime is the server clock."""
    d = api(f"https://pubsub.googleapis.com/v1/{SUB}:pull",
            {"maxMessages": 10, "returnImmediately": True})
    if "_err" in d:
        return []
    msgs = d.get("receivedMessages", [])
    if msgs:
        api(f"https://pubsub.googleapis.com/v1/{SUB}:acknowledge",
            {"ackIds": [m["ackId"] for m in msgs]})
    return msgs


# ---------------------------------------------------------------- main loop

head = kubectl("get", "pods", "-n", "default", "-l", "ray.io/node-type=head",
               "-o", "jsonpath={.items[0].metadata.name}").strip()

emit("START", f"T0={T0.isoformat()} head={head} target={TARGET} submission={SUBMISSION} "
              f"cloud_fast={CLOUD_FAST_MIN}min")

last, tick, last_beat = {}, 0, 0.0
m1_done = m2_done = False
deadline = time.time() + MAXM * 60

while time.time() < deadline:
    tick += 1
    now = datetime.datetime.now(datetime.timezone.utc)
    since_t0 = (now - T0).total_seconds() / 60.0

    for name, fn in (("NODE", snap_nodes), ("POD", snap_pods)):
        cur = json.dumps(fn(), sort_keys=True)
        if cur != last.get(name):
            emit(f"CHANGE-{name}", cur)
            last[name] = cur

    g = str(snap_cluster_gpu())
    if g != last.get("GPU"):
        emit("CHANGE-GPU", f"cluster allocatable nvidia.com/gpu = {g}")
        last["GPU"] = g

    # 5 s while the fast transitions are in play (M3), 30 s once we are just waiting on a host
    cloud_every = 1 if since_t0 <= CLOUD_FAST_MIN else 6
    if tick % cloud_every == 0:
        c = json.dumps(snap_cloud(), sort_keys=True)
        if c != last.get("CLOUD"):
            emit("CHANGE-CLOUD", c)
            last["CLOUD"] = c

    if tick % 6 == 1 and head:
        j = snap_job(head)
        if j != last.get("JOB"):
            emit("CHANGE-JOB", f"ray job {SUBMISSION} status = {j}")
            last["JOB"] = j

    if not m1_done and 0 <= since_t0 <= M1_WINDOW_MIN:
        e = check_m1()
        if e:
            ets = datetime.datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
            rts = e.get("receiveTimestamp")
            rd = ((datetime.datetime.fromisoformat(rts.replace("Z", "+00:00")) - T0).total_seconds()
                  if rts else None)
            emit("M1-LOG", f"entry_ts={e['timestamp']} (T+{(ets - T0).total_seconds():.3f}s) "
                           f"receive_ts={rts} (T+{rd if rd is None else round(rd, 3)}s) :: "
                           f"{(e.get('textPayload') or '')[:150]}", ets)
            m1_done = True

    if not m2_done and 0 <= since_t0 <= M2_WINDOW_MIN:
        for m in check_m2():
            pt = m.get("message", {}).get("publishTime")
            pd = ((datetime.datetime.fromisoformat(pt.replace("Z", "+00:00")) - T0).total_seconds()
                  if pt else None)
            emit("M2-ALERT", f"publishTime={pt} (T+{pd if pd is None else round(pd, 3)}s) "
                             f"attrs={json.dumps(m.get('message', {}).get('attributes', {}))[:200]}")
            m2_done = True

    if time.time() - last_beat > 300:
        emit("heartbeat", f"tick={tick} gpu={last.get('GPU')} job={last.get('JOB')} "
                          f"m1={m1_done} m2={m2_done}")
        last_beat = time.time()

    time.sleep(5)

emit("END", f"poller exit after {MAXM} min")
