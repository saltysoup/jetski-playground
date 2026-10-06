#!/bin/bash
# Keep watch9.py alive for the whole run.
#
# WHY. Run 8f's repair completion was lost because the poller died at ~02:32Z and nobody
# noticed until the run was over; the end time had to be recovered afterwards from the
# instance's lastStartTimestamp. A repair takes hours and spans session boundaries, so the
# poller cannot be a foreground process nobody is watching.
#
# The supervisor restarts watch9.py whenever it exits, appending to the same log. watch9.py
# prints a "# RESTART" line on every start, so coverage gaps are explicit in the timeline
# rather than silent -- an unmarked gap is indistinguishable from "nothing changed", which
# is exactly the ambiguity that made 8f's log unusable.
#
# Cadence is re-read from cadence.txt on every restart, so the run can be sped up or slowed
# down mid-flight (5 s for detection, 10 s for the repair) without killing the supervisor.
set -uo pipefail

HERE=$(dirname "$(readlink -f "$0")")
LOG="$HERE/timeline-9.log"
CADENCE_FILE="$HERE/cadence.txt"
BUDGET=${BUDGET:-3600}     # per-child budget; the supervisor just starts another one
DEADLINE=$(( $(date +%s) + ${TOTAL:-36000} ))   # 10 h of supervision by default

echo "# SUPERVISOR START $(date -u +%Y-%m-%dT%H:%M:%SZ) pid=$$ deadline=$(date -u -d @$DEADLINE +%H:%M:%SZ)" >> "$LOG"

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  EVERY=$(cat "$CADENCE_FILE" 2>/dev/null || echo 10)
  python3 "$HERE/watch9.py" "$EVERY" "$BUDGET" >> "$LOG" 2>&1
  echo "# child exited rc=$? at $(date -u +%Y-%m-%dT%H:%M:%SZ); restarting in 5s" >> "$LOG"
  sleep 5
done
echo "# SUPERVISOR DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
