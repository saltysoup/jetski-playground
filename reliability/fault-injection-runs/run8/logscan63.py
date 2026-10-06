#!/usr/bin/env python3
"""Confirm run 8d's Xid 63 lines reached Cloud Logging, and say which ones.

This is the filter check. The engineer warned that Xids carrying a fake PCIe address are
now stripped, so there are three distinguishable outcomes and they mean different things:

    5 lines found   -> the stimulus reached the platform; any null result downstream is
                       the platform choosing not to act
    0 lines found   -> the lines were filtered before reaching the log, and run 8d says
                       nothing about Xid 63 -- it only re-tests the filter
    partial         -> something is rate-limiting or sampling, worth reporting as-is

Correlation is on the remapped-row address rather than a marker string, because run 8d
deliberately removed the 'name=CLAUDE-SIM-...' field to keep the line byte-authentic.
Row addresses are the same ones inject63.sh derives from the iteration number.
"""
import json
import os
import subprocess
import urllib.request

P = "gpu-launchpad-playground"
Z = "europe-west4-b"
TARGET = "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3"

ROW_BASE = int(os.environ.get("ROW_BASE", "0x1026b79c40"), 16)
ROWS = {i: f"0x{ROW_BASE + i * 4096:016x}" for i in range(1, 6)}


def token():
    return subprocess.run(["gcloud", "auth", "application-default", "print-access-token"],
                          capture_output=True, text=True).stdout.strip()


def api(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def main():
    iid = api(f"https://compute.googleapis.com/compute/v1/projects/{P}"
              f"/zones/{Z}/instances/{TARGET}").get("id")
    flt = ('log_id("serialconsole.googleapis.com/serial_port_1_output") '
           'resource.type="gce_instance" '
           f'resource.labels.instance_id="{iid}" '
           '"Row Remapper"')
    r = api("https://logging.googleapis.com/v2/entries:list",
            {"resourceNames": [f"projects/{P}"], "filter": flt,
             "orderBy": "timestamp desc", "pageSize": 20})
    ents = r.get("entries", [])
    text = "\n".join((e.get("textPayload") or "") for e in ents)

    print(f"XID63_LOG_HITS {len(ents)} instance_id={iid}")
    for i, row in ROWS.items():
        print(f"  i{i} row={row} {'FOUND' if row in text else 'MISSING'}")
    for e in ents:
        print(f"  ts={e.get('timestamp')} | {(e.get('textPayload') or '').strip()[:170]}")


if __name__ == "__main__":
    main()
