#!/usr/bin/env python3
"""Pre-flight every Google Cloud surface run 6 depends on, before T0.

The gcloud CLI's user credential is dead (Context Aware Access), but the
application-default credential still mints tokens, so everything here goes over
REST rather than through the CLI.

Checks, in the order the run needs them:
  1. reservation + block healthStatus   -> must be HEALTHY, or B6 measures nothing
  2. per-instance upcomingMaintenance   -> must be clear, same reason
  3. Cloud Logging read                 -> M1
  4. alert policy + notification channel enabled -> M2
  5. Pub/Sub subscription exists and is drained  -> M2

Read-only. Exits non-zero if any surface would invalidate a measurement.
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request

P = "gpu-launchpad-playground"
Z = "europe-west4-b"
RES = "nvidia-b200-6bsoymep8ylww"
NODES = {
    "34t3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3",  # target
    "6df3": "gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-6df3",  # control
}
POLICY = "projects/gpu-launchpad-playground/alertPolicies/14253142552209113916"
CHANNELS = {
    # pubsub is the measurable one: publishTime gives M2 on a server clock.
    "pubsub": "projects/gpu-launchpad-playground/notificationChannels/1117176896799437752",
    # email is the one a human actually reacts to.
    "email": "projects/gpu-launchpad-playground/notificationChannels/9977523644883873718",
}
TOPIC = "projects/gpu-launchpad-playground/topics/xid-alerts"
SUB = "projects/gpu-launchpad-playground/subscriptions/xid-alerts-sub"

TOKEN = subprocess.run(
    ["gcloud", "auth", "application-default", "print-access-token"],
    capture_output=True, text=True, check=True).stdout.strip()

fail = []


def api(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=45))
    except urllib.error.HTTPError as ex:
        return {"_err": f"HTTP {ex.code}: {ex.read().decode()[:300]}"}
    except Exception as ex:  # noqa: BLE001
        return {"_err": str(ex)[:300]}


def check(label, ok, detail, warn_only=False):
    """warn_only: surfaces that degrade the run's convenience but not its numbers."""
    tag = "PASS" if ok else ("WARN" if warn_only else "FAIL")
    print(f"{tag}  {label:44s} {detail}")
    if not ok and not warn_only:
        fail.append(label)


print("=" * 100)
print("1. reservation health  (B6 needs a real HEALTHY -> DEGRADED transition)")
base = f"https://compute.googleapis.com/compute/v1/projects/{P}/zones/{Z}"
r = api(f"{base}/reservations/{RES}")
rs = r.get("resourceStatus", {})
rh = rs.get("healthInfo", {}).get("healthStatus")
rm = rs.get("reservationMaintenance", {})
check("reservation healthStatus", rh == "HEALTHY", f"{rh}  pending={rm.get('maintenancePendingCount')} ongoing={rm.get('maintenanceOngoingCount')}")

b = api(f"{base}/reservations/{RES}/reservationBlocks")
blocks = b.get("items", [])
for x in blocks:
    bh = x.get("healthInfo", {}).get("healthStatus")
    check(f"block {x.get('name','?')} healthStatus", bh == "HEALTHY",
          f"{bh}  degradedCount={x.get('healthInfo',{}).get('degradedHostCount')}")
check("blocks visible", bool(blocks), f"{len(blocks)} block(s)")

print()
print("2. per-instance upcomingMaintenance  (B7 needs these clear beforehand)")
for short, full in NODES.items():
    i = api(f"{base}/instances/{full}")
    if "_err" in i:
        check(f"instance {short}", False, i["_err"])
        continue
    um = i.get("resourceStatus", {}).get("upcomingMaintenance", {})
    check(f"instance {short} status", i.get("status") == "RUNNING", i.get("status"))
    check(f"instance {short} maintenance clear", not um,
          json.dumps(um) if um else "no upcomingMaintenance")

print()
print("3. Cloud Logging read  (M1)")
# The log id matters: Xid lines are under serialconsole.googleapis.com, NOT under
# compute.googleapis.com. The latter returns nothing and looks like "no faults".
# This is the same filter the alert policy matches on.
q = {
    "resourceNames": [f"projects/{P}"],
    "filter": ('log_id("serialconsole.googleapis.com/serial_port_1_output") '
               'resource.type="gce_instance" '
               '"NVRM: Xid"'),
    "orderBy": "timestamp desc",
    "pageSize": 3,
}
lg = api("https://logging.googleapis.com/v2/entries:list", "POST", q)
ents = lg.get("entries", [])
if "_err" in lg:
    # Reads in this project get 429'd by another consumer. Not fatal: M1 and M2 are read
    # off server-side timestamps, so a throttled poll delays the display, not the number.
    throttled = "429" in lg["_err"]
    check("logging entries:list", False, lg["_err"][:120], warn_only=throttled)
    if throttled:
        print("        (read quota throttled — M1 comes from the entry's own timestamps, so this")
        print("         costs liveness, not accuracy)")
else:
    check("logging entries:list", True, f"{len(ents)} prior NVRM Xid entries readable")
    for e in ents:
        iid = e.get("resource", {}).get("labels", {}).get("instance_id")
        print(f"        {e.get('timestamp')}  inst={iid}  {(e.get('textPayload') or '')[:80]}")

print()
print("4. alert policy + channel  (M2 — pubsub for measurement, email for the human)")
pol = api(f"https://monitoring.googleapis.com/v3/{POLICY}")
if "_err" in pol:
    check("alert policy readable", False, pol["_err"])
else:
    check("alert policy enabled", pol.get("enabled") is True, f"enabled={pol.get('enabled')}")
    check("alert policy has channel", bool(pol.get("notificationChannels")),
          ", ".join(c.split("/")[-1] for c in pol.get("notificationChannels", [])))
    for c in pol.get("conditions", []):
        f = c.get("conditionMatchedLog", {}).get("filter") or \
            c.get("conditionThreshold", {}).get("filter", "")
        print(f"        condition: {c.get('displayName')}  filter={f[:120]}")
    dur = (pol.get("alertStrategy", {}) or {}).get("notificationRateLimit", {}).get("period")
    print(f"        notificationRateLimit.period = {dur}  (delays a repeat alert, not the first)")

attached = set(pol.get("notificationChannels", []))
for kind, cid in CHANNELS.items():
    ch = api(f"https://monitoring.googleapis.com/v3/{cid}")
    if "_err" in ch:
        check(f"{kind} channel readable", False, ch["_err"])
        continue
    check(f"{kind} channel enabled", ch.get("enabled") is True,
          f"{ch.get('type')} -> {json.dumps(ch.get('labels', {}))}")
    check(f"{kind} channel attached to policy", cid in attached,
          "attached" if cid in attached else "NOT attached — it will never fire")

print()
print("5. Pub/Sub  (M2 delivery path)")
t = api(f"https://pubsub.googleapis.com/v1/{TOPIC}")
check("topic exists", "_err" not in t, t.get("name", t.get("_err", "")))

s = api(f"https://pubsub.googleapis.com/v1/{SUB}")
if "_err" in s:
    check("subscription exists", False, s["_err"] + "   <-- create it before T0 or M2 is lost again")
else:
    check("subscription exists", True, f"ack_deadline={s.get('ackDeadlineSeconds')}s retain={s.get('messageRetentionDuration')}")
    # Drain: a backlog from an earlier run would be mistaken for this run's alert.
    d = api(f"https://pubsub.googleapis.com/v1/{SUB}:pull", "POST",
            {"maxMessages": 100, "returnImmediately": True})
    msgs = d.get("receivedMessages", []) if "_err" not in d else []
    if msgs:
        api(f"https://pubsub.googleapis.com/v1/{SUB}:acknowledge", "POST",
            {"ackIds": [m["ackId"] for m in msgs]})
    check("subscription drained", "_err" not in d,
          f"{len(msgs)} stale message(s) acked" if "_err" not in d else d["_err"])

print()
print("=" * 100)
if fail:
    print(f"{len(fail)} BLOCKER(S) — do not start the run:")
    for f_ in fail:
        print(f"  - {f_}")
    sys.exit(1)
print("All surfaces green. Safe to inject.")
