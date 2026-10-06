#!/bin/bash
# Independent 60 s ground-truth probe: raw instance JSON, one line per poll, append-only.
#
# This is deliberately dumber than watch9.py and shares no code with it. If the poller dies,
# has a bug, or the supervisor itself fails, this still leaves an unambiguous record of
# instance status / upcomingMaintenance / lastStartTimestamp across the whole run. It is the
# artifact that would have saved 8f's repair completion time.
set -uo pipefail
HERE=$(dirname "$(readlink -f "$0")")
OUT="$HERE/instance-10.jsonl"
N=gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3
U="https://compute.googleapis.com/compute/v1/projects/gpu-launchpad-playground/zones/europe-west4-b/instances/$N"
DEADLINE=$(( $(date +%s) + ${TOTAL:-36000} ))

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  TOK=$(gcloud auth application-default print-access-token 2>/dev/null)
  curl -s -H "Authorization: Bearer $TOK" "$U" | python3 -c "
import json,sys,datetime
try:
    d=json.load(sys.stdin)
except Exception:
    d={'_parse_error':True}
print(json.dumps({
 'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'status': d.get('status'),
 'lastStartTimestamp': d.get('lastStartTimestamp'),
 'upcomingMaintenance': d.get('resourceStatus',{}).get('upcomingMaintenance'),
}))" >> "$OUT"
  sleep 60
done
