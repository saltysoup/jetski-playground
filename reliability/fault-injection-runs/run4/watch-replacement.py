#!/usr/bin/env python3
"""Run-4 replacement poller.

Watches every surface that moves during a label-triggered node replacement, from the
moment `cloud.google.com/perform-maintenance=true` is applied until the cluster is fully
healthy again. Emits one line per observed change to run4/replacement-timeline.log.

Milestones this is designed to capture (see plan.MD section 9):
  R1  label accepted / maintenance_status PENDING -> ONGOING
  R2  cloud.google.com/active-node-maintenance label appears
  R3  impending-node-termination taint applied
  R4  Ray worker pod terminated (gracefully or hard-killed)
  R5  kubelet stops posting status / node NotReady
  R6  VM leaves RUNNING
  R7  VM back to RUNNING
  R8  node Ready again, 8 GPUs allocatable, taints cleared
  R9  Ray worker 2/2 Running, 16 GPUs cluster-wide
  R10 reservation block back to HEALTHY

Both GPU nodes are watched, not just the target: the reservation uses GROUPED maintenance
scheduling, so the blast radius of a single-node label is an open question.

Usage: watch-replacement.py <T0-iso8601> [max_minutes]
Runs unattended; safe under nohup. Never raises on a transient API error.
"""
import json, sys, time, datetime, subprocess, urllib.request, os

P    = "gpu-launchpad-playground"
Z    = "europe-west4-b"
RES  = "nvidia-b200-6bsoymep8ylww"
NODES = {
    "34t3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3",   # target: XID injected 00:45:12Z
    "6df3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3",   # control
}
SUBMISSION = "dapo-run4-baseline"

T0   = datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
MAXM = float(sys.argv[2]) if len(sys.argv) > 2 else 480.0
HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(HERE, "replacement-timeline.log")

_tok = {"v": "", "t": 0.0}
def token():
    if time.time() - _tok["t"] > 1800:
        _tok["v"] = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True).stdout.strip()
        _tok["t"] = time.time()
    return _tok["v"]

def api(url):
    try:
        return json.load(urllib.request.urlopen(
            urllib.request.Request(url, headers={"Authorization": "Bearer " + token()}),
            timeout=30))
    except Exception as ex:
        return {"_err": (ex.read().decode()[:150] if hasattr(ex, "read") else str(ex)[:150])}

def kubectl(*a, timeout=30):
    try:
        r = subprocess.run(["kubectl", *a], capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""

def emit(kind, msg, ts=None):
    ts = ts or datetime.datetime.now(datetime.timezone.utc)
    el = (ts - T0).total_seconds()
    line = "%s | T+%9.1fs | %-14s | %s" % (ts.strftime("%H:%M:%S"), el, kind, msg)
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

# ---------------------------------------------------------------- snapshots

def snap_nodes():
    """Per-node k8s state. Missing node (deleted/recreated) is itself a signal."""
    out = {}
    raw = kubectl("get", "nodes", "-o", "json")
    try:
        items = json.loads(raw)["items"]
    except Exception:
        return {"_err": "kubectl unreadable"}
    byname = {n["metadata"]["name"]: n for n in items}
    for short, full in NODES.items():
        n = byname.get(full)
        if n is None:
            out[short] = {"present": False}
            continue
        md, st = n["metadata"], n["status"]
        ann = md.get("annotations", {})
        um = ann.get("node.gke.io/upcoming-maintenance")
        um_status = None
        if um:
            try:
                um_status = json.loads(um).get("maintenance_status")
            except Exception:
                um_status = "unparseable"
        out[short] = {
            "present": True,
            "ready": next((c["status"] for c in st["conditions"] if c["type"] == "Ready"), "?"),
            "gpu": st.get("allocatable", {}).get("nvidia.com/gpu", "0"),
            "taints": sorted(t["key"].split("/")[-1] for t in n["spec"].get("taints", [])),
            "unsched": n["spec"].get("unschedulable", False),
            "maint_status": um_status,
            "active_maint": md.get("labels", {}).get("cloud.google.com/active-node-maintenance"),
            "perform_label": md.get("labels", {}).get("cloud.google.com/perform-maintenance"),
            "uid": md.get("uid", "")[:8],
        }
    return out

def snap_pods():
    raw = kubectl("get", "pods", "-n", "default", "-l", "ray.io/is-ray-node=yes",
                  "-o", "json")
    try:
        items = json.loads(raw)["items"]
    except Exception:
        return {"_err": "pods unreadable"}
    out = {}
    for p in items:
        st = p["status"]
        ready = sum(1 for c in st.get("containerStatuses", []) if c.get("ready"))
        total = len(st.get("containerStatuses", [])) or 1
        node = p["spec"].get("nodeName", "?")
        short = next((s for s, f in NODES.items() if f == node), node[-4:] if node else "?")
        out[p["metadata"]["name"][-12:]] = {
            "phase": st.get("phase"), "ready": f"{ready}/{total}",
            "node": short, "deleting": p["metadata"].get("deletionTimestamp") is not None,
        }
    return out

def snap_cluster_gpu():
    raw = kubectl("get", "nodes", "-o",
                  "jsonpath={range .items[*]}{.status.allocatable.nvidia\\.com/gpu}{\"\\n\"}{end}")
    tot = 0
    for l in raw.splitlines():
        try:
            tot += int(l.strip())
        except Exception:
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
        out["block_health"] = x.get("healthInfo", {}).get("healthStatus")
    for short, full in NODES.items():
        i = api(f"{base}/instances/{full}")
        um = i.get("resourceStatus", {}).get("upcomingMaintenance", {})
        out[f"{short}_vm"] = i.get("status")
        out[f"{short}_maint"] = um.get("maintenanceStatus", "CLEAR")
    return out

def snap_job():
    raw = kubectl("exec", "-n", "default", "-c", "ray-head",
                  os.environ.get("HEAD_POD", ""), "--",
                  "ray", "job", "status", SUBMISSION, timeout=60)
    for tokn in ("SUCCEEDED", "FAILED", "STOPPED", "RUNNING", "PENDING"):
        if tokn in raw:
            return tokn
    return "?"

# ---------------------------------------------------------------- main loop

head = kubectl("get", "pods", "-n", "default", "-l", "ray.io/node-type=head",
               "-o", "jsonpath={.items[0].metadata.name}").strip()
os.environ["HEAD_POD"] = head

emit("START", f"T0={T0.isoformat()} head={head} watching {list(NODES)} submission={SUBMISSION}")

last = {}
tick = 0
last_beat = 0.0
deadline = time.time() + MAXM * 60

while time.time() < deadline:
    tick += 1

    # k8s surfaces every ~5s — these are the fast-moving ones
    for name, fn in (("NODE", snap_nodes), ("POD", snap_pods)):
        cur = fn()
        s = json.dumps(cur, sort_keys=True)
        if s != last.get(name):
            emit(f"CHANGE-{name}", json.dumps(cur, sort_keys=True))
            last[name] = s

    g = snap_cluster_gpu()
    if str(g) != last.get("GPU"):
        emit("CHANGE-GPU", f"cluster allocatable nvidia.com/gpu = {g}")
        last["GPU"] = str(g)

    # compute + job surfaces every ~30s
    if tick % 6 == 1:
        c = snap_cloud()
        s = json.dumps(c, sort_keys=True)
        if s != last.get("CLOUD"):
            emit("CHANGE-CLOUD", json.dumps(c, sort_keys=True))
            last["CLOUD"] = s

        j = snap_job()
        if j != last.get("JOB"):
            emit("CHANGE-JOB", f"ray job {SUBMISSION} status = {j}")
            last["JOB"] = j

    # heartbeat every 5 min so a silent window is distinguishable from a dead poller
    if time.time() - last_beat > 300:
        emit("heartbeat", f"tick={tick} gpu={last.get('GPU')} job={last.get('JOB')}")
        last_beat = time.time()

    time.sleep(5)

emit("END", f"poller exit after {MAXM} min")
