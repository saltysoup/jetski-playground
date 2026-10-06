# Run 10 — complete timeline (closed 2026-09-24 18:41Z)

T0 = 2026-09-24T06:31:04.445Z (synthetic Xid 79, run 7 format, GPU-7 PIDs killed)
B7 = 2026-09-24T14:39:44.185Z (perform-maintenance label)
Node -34t3, instance 174490015860863227. Poller 5 s (detection) / 10 s (repair); "poll" = first poll that saw it.

| Stage | Event | UTC | From T0 | From B7 |
| --- | --- | --- | --- | --- |
| B0 | Xid 79 written to /dev/kmsg, 2 GPU-7 PIDs killed | 06:31:04.445 | 0 | |
| B1 | M1 Xid in Cloud Logging (timestamp) | 06:31:04.507 | +0.062 s | |
| B1' | M1' Xid queryable (receiveTimestamp) | 06:31:06.512 | +2.07 s | |
| B2 | dapo-run9 FAILED (badput starts) | 06:31:15.892 | +11.4 s | |
| B4 | M3 reservation + block DEGRADED | 06:31:16.926 poll | +12.5 s | |
| gap | pollers died; last null poll | 10:28:17 | +3 h 57 m 13 s | |
| B5 | M4 upcomingMaintenance raised (UNSCHEDULED, PENDING, canReschedule true, window 10-01 12:00-16:00Z, FAILURE_GPU_XID+FAILURE_GPU) | 10:28:17 .. 10:55:01 | +3 h 57 m .. +4 h 24 m | |
| — | GKE gpu-maintenance-handler writes node annotation | 10:55:01 | +4 h 23 m 57 s | |
| — | event observed (pollers restarted 14:39:19) | 14:37:49 | +8 h 06 m 45 s | |
| B7 | perform-maintenance label applied | 14:39:44.185 | +8 h 08 m 40 s | 0 |
| B8 | PENDING->ONGOING, canReschedule false, window 14:40:00Z->18:39:50Z, taint maintenance-window-started, label consumed; type STAYS UNSCHEDULED | 14:40:06.363 poll | +8 h 09 m 02 s | +22.2 s |
| — | reservation + block pending 1->0, ongoing 0->1 | 14:40:28.651 poll | +8 h 09 m 24 s | +44.5 s |
| B10 | node NotReady (drained) | 14:55:23.498 poll | +8 h 24 m 19 s | +15 m 39 s |
| — | Ray worker -jq4jh evicted; replacement -k88n4 created (Pending) | 14:55:25 | +8 h 24 m 21 s | +15 m 41 s |
| — | node 0 GPUs, cluster 16->8 | 14:55:44.822 poll | +8 h 24 m 40 s | +16 m 01 s |
| B11 | VM RUNNING->REPAIRING | 14:56:48.946 poll | +8 h 25 m 45 s | +17 m 05 s |
| — | reservation + block DEGRADED->HEALTHY | 15:06:59.681 poll | +8 h 35 m 55 s | +27 m 15 s |
| B12 | VM REPAIRING->RUNNING (lastStartTimestamp) | 15:12:19.474 | +8 h 41 m 15 s | +32 m 35 s |
| B13 | node Ready, 8 GPUs, cluster 8->16; -k88n4 scheduled | 15:14:28 | +8 h 43 m 24 s | +34 m 44 s |
| — | Ray worker -k88n4 Ready, Ray 2/2 (cluster whole) | 15:26:20 | +8 h 55 m 16 s | +46 m 36 s |
| B14 | prep-pods.sh (-k88n4 PATCHED) | 16:42:47.708 | +10 h 11 m 43 s | +2 h 03 m 04 s |
| B14 | dapo-run10-recovered submitted | 16:42:51.947 | +10 h 11 m 48 s | +2 h 03 m 08 s |
| — | instance maintenance ONGOING->CLEAR, reasons null (5.2 s after windowEndTime 18:39:50Z) | 18:39:55.196 poll | +12 h 08 m 51 s | +4 h 00 m 11 s |
| — | reservation + block ongoing 1->null (record fully closed) | 18:41:03.107 poll | +12 h 09 m 59 s | +4 h 01 m 19 s |
