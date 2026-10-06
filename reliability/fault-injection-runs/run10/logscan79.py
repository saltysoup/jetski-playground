#!/usr/bin/env python3
"""Did run 10's Xid 79 reach Cloud Logging, and what else is on the node's log?

This is the filter check, and for run 10 it is the whole point rather than a footnote.
Run 7 injected an Xid 79 on 2026-08-24 and it triggered maintenance in 35.6 s. The GCE
engineer said Xid lines carrying a fake PCIe address are now filtered, possibly from
inside the 08-24 -> 08-27 window. Three outcomes, three different meanings:

    line found, platform acted     -> no filter on Xid 79; the 8a/8b nulls were about
                                      Xid *class*, not about synthetic detection
    line found, platform silent    -> the platform saw it and declined. Policy, not filter
    line absent                    -> filtered before reaching the log. Run 10 says nothing
                                      about Xid 79; it only re-tests the filter

Unlike logscan63.py this does not correlate on a row address -- an Xid 79 has no such
field. Correlation is on the CLAUDE-SIM-RUN10 marker for attempt 1, and on timestamp
proximity for attempt 2 (clean format, no marker).

It also dumps EVERY Xid line on the node for the trailing hour, because -34t3 still
carries two Xid 63s from run 9 and the reader needs to see exactly what the detector had
in front of it.
"""
import json
import subprocess
import sys
import urllib.request

P = "gpu-launchpad-playground"
Z = "europe-west4-b"
TARGET = "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-24T05:00:00Z"


def token():
    return subprocess.run(["gcloud", "auth", "application-default", "print-access-token"],
                          capture_output=True, text=True).stdout.strip()


def api(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def query(iid, extra):
    flt = ('log_id("serialconsole.googleapis.com/serial_port_1_output") '
           'resource.type="gce_instance" '
           f'resource.labels.instance_id="{iid}" '
           f'timestamp>="{SINCE}" {extra}')
    r = api("https://logging.googleapis.com/v2/entries:list",
            {"resourceNames": [f"projects/{P}"], "filter": flt,
             "orderBy": "timestamp desc", "pageSize": 100})
    return r.get("entries", [])


def main():
    iid = api(f"https://compute.googleapis.com/compute/v1/projects/{P}"
              f"/zones/{Z}/instances/{TARGET}").get("id")
    print(f"instance_id={iid} since={SINCE}")

    xids = query(iid, '"NVRM: Xid"')
    print(f"\nALL_XID_LINES {len(xids)}")
    for e in reversed(xids):
        print(f"  ts={e.get('timestamp')} recv={e.get('receiveTimestamp')} | "
              f"{(e.get('textPayload') or '').strip()[:190]}")

    text = "\n".join((e.get("textPayload") or "") for e in xids)
    print("\nVERDICT")
    print(f"  Xid 79 present     : {'YES' if ' 79,' in text else 'NO'}")
    print(f"  RUN10 marker present: {'YES' if 'CLAUDE-SIM-RUN10' in text else 'NO'}")
    print(f"  fell off the bus    : {'YES' if 'fallen off the bus' in text else 'NO'}")
    print(f"  leftover Xid 63s    : {text.count('Row Remapper')}")

    # Anything the driver said that is not an Xid -- crash dumps, bus errors, NVLink.
    other = query(iid, '("NVRM" OR "nvidia" OR "pcieport") NOT "NVRM: Xid"')
    print(f"\nOTHER_DRIVER_LINES {len(other)}")
    for e in reversed(other[:40]):
        print(f"  ts={e.get('timestamp')} | {(e.get('textPayload') or '').strip()[:190]}")


if __name__ == "__main__":
    main()
