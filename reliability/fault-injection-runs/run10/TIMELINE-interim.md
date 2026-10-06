# Run 10 — interim timeline (written 14:43Z, repair in progress)

T0 = 2026-09-24T06:31:04.445Z  synthetic Xid 79 (run 7 format) + GPU-7 PIDs killed

| Stage | UTC | From T0 | From B7 | Source |
| --- | --- | --- | --- | --- |
| B0 T0 | 06:31:04.445 | 0 | | inject79-a1.txt |
| B1 M1 Xid in Cloud Logging | 06:31:04.507 | +61.7 ms | | logscan79 |
| B1' M1' queryable | 06:31:06.512 | +2.07 s | | logscan79 |
| B2 dapo-run9 FAILED | 06:31:15.892 | +11.4 s | | ray job list end_time |
| B4 M3 res+block DEGRADED | 06:31:16.926 | +12.5 s | | timeline-10.log (5 s) |
| B5 M4 upcomingMaintenance | between 10:28:17 and 10:55:01 | +3 h 57 m 13 s .. +4 h 23 m 57 s | | last null poll / gpu-maintenance-handler managedFields time |
| B7 label | 14:39:44.185 | +8 h 08 m 40 s | 0 | t-label-10.txt |
| B8 PENDING->ONGOING, canReschedule false, window 14:40:00Z->18:39:50Z, taint maintenance-window-started, label consumed; type STAYS UNSCHEDULED | poll 14:40:06.363 | | +22.2 s | timeline-10.log (10 s) |
| res+block pending 1->0 ongoing 0->1 | poll 14:40:28.651 | | +44.5 s | timeline-10.log |

Poller + probe coverage gap 10:28:17Z -> 14:39:19Z (both processes died; cause unknown). Restarted with setsid.
Event verbatim at 14:37:49Z: type UNSCHEDULED, PENDING, canReschedule true, window 2026-10-01T12:00:00Z->16:00:00Z, latestWindowStartTime 2026-10-01T12:00:01Z, reasons [FAILURE_GPU_XID, FAILURE_GPU].

## Repair (B7 = 14:39:44.185)

| Stage | UTC | From B7 | 8d |
| --- | --- | --- | --- |
| node NotReady (drained) | 14:55:23.498 poll | +15 m 39 s | — |
| node 0 GPUs, cluster 16->8 | 14:55:44.822 poll | +16 m 01 s | — |
| VM RUNNING -> REPAIRING | 14:56:48.946 poll | +17 m 05 s | +25 m 52 s |
| reservation + block DEGRADED -> HEALTHY | 15:06:59.681 poll | +27 m 15 s | +4 h 28 m 00 s |
| VM REPAIRING -> RUNNING (lastStartTimestamp) | 15:12:19.474 | +32 m 35.3 s | +4 h 33 m 54 s |
| node Ready (lastTransitionTime), 8 GPUs, cluster 8->16 | 15:14:28 (poll 15:14:27.293) | **+34 m 44 s** | **+4 h 36 m 03 s** |
| instance/reservation maintenanceStatus still ONGOING | as of 15:16:25 | +36 m 41 s | (cleared before VM in 8d) |

Time in REPAIRING ~15 m 30 s (8d: 4 h 08 m 01 s). Reservation health leads VM by 5 m 20 s (8d: 5 m 53 s).
15:14:27 poll returned res_health/pending/ongoing null for one cycle, back at 15:15:38 -- API blip, not a state change.
