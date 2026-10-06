#!/usr/bin/env python3
"""Two one-shot queries loop.sh needs, sharing the ADC token plumbing.

    probe.py maint            -> one JSON line: target upcomingMaintenance + reservation
                                 health + the k8s upcoming-maintenance annotation
    probe.py xidlog <iter>    -> the serial-console Xid lines carrying this iteration's
                                 marker, with both timestamp and receiveTimestamp
    probe.py logscan <since>  -> ANY log entry for this instance since <since> (RFC3339)
                                 mentioning ECC/DBE/Xid/GPU health, across every log_id
                                 rather than just the serial console

`maint` is the answer to run 8's question; `xidlog` is the proof the kmsg stimulus
reached the platform rather than dying in the guest's ring buffer. `logscan` is the
equivalent proof for run 8b, where the stimulus is a DCGM field injection and there is
no reason to assume it surfaces on the serial console -- if it is visible anywhere, this
is what finds it.
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request

P = "gpu-launchpad-playground"
Z = "europe-west4-b"
RES = "nvidia-b200-6bsoymep8ylww"
TARGET = "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3"

# Resolved at call time, not hardcoded. GKE node auto-repair deleted and recreated this
# instance at 19:20:52Z on 2026-08-27 after the run 8a reboot loop left it with a
# read-only local SSD, and the ID changed (8713135250882265303 -> 174490015860863227).
# A hardcoded ID would silently have made every log query search the wrong machine.
_iid = {"v": None}


def iid():
    if _iid["v"] is None:
        i = api(f"https://compute.googleapis.com/compute/v1/projects/{P}"
                f"/zones/{Z}/instances/{TARGET}")
        _iid["v"] = i.get("id")
    return _iid["v"]


def token():
    return subprocess.run(["gcloud", "auth", "application-default", "print-access-token"],
                          capture_output=True, text=True).stdout.strip()


def api(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Authorization": "Bearer " + token(),
                 "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as ex:
        return {"_err": f"HTTP {ex.code}", "_body": ex.read().decode()[:300]}
    except Exception as ex:  # noqa: BLE001
        return {"_err": str(ex)[:300]}


def maint():
    base = f"https://compute.googleapis.com/compute/v1/projects/{P}/zones/{Z}"
    i = api(f"{base}/instances/{TARGET}")
    r = api(f"{base}/reservations/{RES}")
    rs = r.get("resourceStatus", {})
    ann = subprocess.run(
        ["kubectl", "get", "node", TARGET, "-o",
         "jsonpath={.metadata.annotations.node\\.gke\\.io/upcoming-maintenance}"],
        capture_output=True, text=True).stdout.strip()
    print(json.dumps({
        "vm": i.get("status"),
        "upcomingMaintenance": i.get("resourceStatus", {}).get("upcomingMaintenance"),
        "res_health": rs.get("healthInfo", {}).get("healthStatus"),
        "res_pending": rs.get("reservationMaintenance", {}).get("maintenancePendingCount"),
        "res_ongoing": rs.get("reservationMaintenance", {}).get("maintenanceOngoingCount"),
        "k8s_annotation": json.loads(ann) if ann else None,
    }, sort_keys=True))


def xidlog(it):
    flt = ('log_id("serialconsole.googleapis.com/serial_port_1_output") '
           'resource.type="gce_instance" '
           f'resource.labels.instance_id="{iid()}" '
           f'"CLAUDE-SIM-RUN8-I{it}"')
    r = api("https://logging.googleapis.com/v2/entries:list",
            {"resourceNames": [f"projects/{P}"], "filter": flt,
             "orderBy": "timestamp desc", "pageSize": 10})
    ents = r.get("entries", [])
    if "_err" in r:
        print(f"XIDLOG_ERR {r['_err']} {r.get('_body','')}")
        return
    print(f"XIDLOG_COUNT {len(ents)}")
    for e in ents:
        print(f"  ts={e.get('timestamp')} recv={e.get('receiveTimestamp')} "
              f"| {(e.get('textPayload') or '').strip()[:160]}")


def logscan(since):
    # Deliberately not scoped to a log_id. A DCGM-level fault, if the platform notices it
    # at all, could surface via the guest agent, node-problem-detector, the GPU device
    # plugin or a GKE system event -- none of which are the serial console.
    flt = (f'resource.labels.instance_id="{iid()}" '
           f'timestamp>="{since}" '
           '("Xid" OR "DBE" OR "ECC" OR "ecc" OR "double bit" OR "uncorrectable" '
           'OR "GPU health" OR "unhealthy")')
    r = api("https://logging.googleapis.com/v2/entries:list",
            {"resourceNames": [f"projects/{P}"], "filter": flt,
             "orderBy": "timestamp desc", "pageSize": 25})
    if "_err" in r:
        print(f"LOGSCAN_ERR {r['_err']} {r.get('_body','')}")
        return
    ents = r.get("entries", [])
    print(f"LOGSCAN_COUNT {len(ents)} since={since}")
    for e in ents:
        payload = e.get("textPayload") or json.dumps(e.get("jsonPayload") or {})
        print(f"  ts={e.get('timestamp')} log={e.get('logName','').split('/')[-1]} "
              f"| {payload.strip()[:180]}")


if __name__ == "__main__":
    if sys.argv[1] == "maint":
        maint()
    elif sys.argv[1] == "xidlog":
        xidlog(sys.argv[2])
    elif sys.argv[1] == "logscan":
        logscan(sys.argv[2])
    else:
        sys.exit("usage: probe.py maint | probe.py xidlog <iter> | probe.py logscan <since>")
