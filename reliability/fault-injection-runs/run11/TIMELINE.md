# Run 11 — genuine Xid 79 via PCIe secondary bus reset, end to end

| | |
| --- | --- |
| Instance | `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3` |
| Instance ID | `174490015860863227` |
| Zone | `europe-west4-b` (project `gpu-launchpad-playground`) |
| Machine | a4-highgpu-8g, 8× B200; reservation `nvidia-b200-6bsoymep8ylww` |
| Fault | PCIe SBR on `0000:ca:01.0`, upstream port of GPU 7 (`0000:cc:00.0`) |
| Workload | `dapo-run10-recovered` (killed by the fault) → `dapo-run11-recovered` (B14) |
| Automation | B7 label and B14 resubmit fired by `orchestrate.sh` on precondition — no operator latency |

**T0 = 2026-09-24T20:31:37.078Z** (SBR asserted; released 20:31:39.040Z).
"poll" = first 5 s poll that saw the change.

## Every stage

| Stage | Event | UTC | From T0 | From B7 | Stage Δ |
| --- | --- | --- | --- | --- | --- |
| B0 | SBR asserted on GPU 7 upstream port | 20:31:37.078 | 0 | | |
| B1 | **Genuine Xid 79** `(PCI:0000:cc:00): 79, GPU has fallen off the bus.` in serial console / Cloud Logging | 20:31:38.086 | +1.0 s | | 1.0 s |
| B1′ | Queryable in Cloud Logging (`receiveTimestamp`) | 20:31:39.093 | +2.0 s | | 1.0 s |
| — | Xid 154 "Node Reboot Required" on all 8 GPUs; Xid 145 NVLink + Xid 45 on GPUs 1/5/6 (252 lines) | 20:31:38.7–41.1 | +1.7–4.1 s | | |
| B2 | Training job `dapo-run10-recovered` FAILED | 20:31:46.469 | +9.4 s | | 7.4 s |
| B4 | Reservation + block `DEGRADED` | 20:32:07.466 poll | +30.4 s | | 21.0 s |
| — | Reservation + block `pending 0→1` | 20:57:18.783 poll | +25 m 41.7 s | | 25 m 11.3 s |
| **B5** | **Maintenance reason on instance**: `UNSCHEDULED`, `PENDING`, `canReschedule true`, `FAILURE_GPU_XID, FAILURE_GPU`, window 2026-10-02T00:00Z–04:00Z | **20:57:49.444 poll** | **+26 m 12.4 s** | | 30.7 s |
| **B7** | **`perform-maintenance=true` applied** (sent 20:57:56.081, ack) | **20:57:59.390** | +26 m 22.3 s | **0** | 9.9 s |
| B9 | Taint `maintenance-window-started=PreferNoSchedule` | 20:58:20.188 poll | +26 m 43.1 s | +20.8 s | 20.8 s |
| B8 | `PENDING→ONGOING`, `canReschedule→false`, window 20:58:11Z→00:58:01Z, pending 1→0 ongoing 0→1; type stays `UNSCHEDULED` | 20:58:50.568 poll | +27 m 13.5 s | +51.2 s | 30.4 s |
| B10 | Node `NotReady` (drained); Ray worker replaced (`-lsfdm` created 21:12:19) | 21:12:22.597 poll | +40 m 45.5 s | +14 m 23.2 s | 13 m 32.0 s |
| — | Node 0 GPUs, cluster 16→8 | 21:12:40.763 poll | +41 m 03.7 s | +14 m 41.4 s | 18.2 s |
| B11 | VM `RUNNING → REPAIRING` | 21:15:06.114 poll | +43 m 29.0 s | +17 m 06.7 s | 2 m 25.4 s |
| — | Reservation + block `DEGRADED → HEALTHY` | 21:25:57.265 poll | +54 m 20.2 s | +27 m 57.9 s | 10 m 51.2 s |
| B12 | VM `REPAIRING → RUNNING` (`lastStartTimestamp`) | 21:31:26.374 | +59 m 49.3 s | +33 m 27.0 s | 5 m 29.1 s |
| **B13** | **Node Ready, 8 allocatable GPUs, cluster 8→16** (`lastTransitionTime`) | **21:33:24** | **+1 h 01 m 46.9 s** | **+35 m 24.6 s** | 1 m 57.6 s |
| B13b | Ray worker `-lsfdm` Ready — Ray 2/2 | 21:37:42 | +1 h 06 m 04.9 s | +39 m 42.6 s | 4 m 18.0 s |
| B14 | `prep-pods.sh` 21:37:48.385 → 21:38:23.965 (`-lsfdm` PATCHED) | 21:38:23.965 | +1 h 06 m 46.9 s | +40 m 24.6 s | 42.0 s |
| B14 | `dapo-run11-recovered` submitted | 21:38:34.397 | +1 h 06 m 57.3 s | +40 m 35.0 s | 10.4 s |
| **B14** | **Training Step 1/500 begins** | **21:49:55.573** | **+1 h 18 m 18.5 s** | +51 m 56.2 s | 11 m 21.2 s (job setup) |
| B14 | Step 1 complete (120.56 s), Step 2 begins | 21:52:15.793 | +1 h 20 m 38.7 s | +54 m 16.4 s | 2 m 20.2 s |
| — | Maintenance `ONGOING → CLEAR`, reasons/type/window → null (**6.1 s after `windowEndTime` 00:58:01Z**, 3 h 24 m after node Ready) | 09-25 00:58:07.127 poll | +4 h 26 m 30.0 s | +4 h 00 m 07.7 s | |
| — | Reservation + block `pending/ongoing` counters → null | 09-25 00:59:12.228 poll | +4 h 27 m 35.2 s | +4 h 01 m 12.8 s | 1 m 05.1 s |
| — | Monitors last sample (session ended); node CLEAR/Ready/8 GPUs; training at Step 149/500 | 09-25 02:37:42 | | | |

## Where the 78 minutes went

| Phase | Duration | Share |
| --- | --- | --- |
| Fault → maintenance reason (B0 → B5) | 26 m 12 s | 33 % |
| Reason → label (automated) | 10 s | 0 % |
| Label → drain complete (B7 → B10) | 14 m 23 s | 18 % |
| Drain → node Ready (B10 → B13), of which REPAIRING ~16 m 20 s | 21 m 01 s | 27 % |
| Node Ready → Ray whole (B13 → B13b) | 4 m 18 s | 5 % |
| prep + submit + job setup → Step 1 (B13b → Step 1) | 12 m 14 s | 16 % |
| **Fault → training stepping again** | **1 h 18 m 18 s** | |

## Against expectation and prior runs

| Interval | Expected | Run 11 | Run 10 | 8d | Run 7 |
| --- | --- | --- | --- | --- | --- |
| Fault → Cloud Logging | few min | **1.0 s** | 0.06 s | — | 0.06 s |
| Fault → DEGRADED | — | 30.4 s | 12.5 s | — | 35.6 s |
| Fault → maintenance reason | 10–15 min | **26 m 12 s** | ~4 h 00–24 m | 12 m 32 s | 19 m 03 s |
| Label → VM REPAIRING | — | 17 m 07 s | 17 m 05 s | 25 m 52 s | 20 m 22 s |
| Time in REPAIRING | — | ~16 m 20 s | ~15 m 30 s | 4 h 08 m 01 s | 4 h 09 m 49 s |
| **Label → node Ready, 8 GPUs** | "much shorter" | **35 m 25 s** | **34 m 44 s** | 4 h 36 m 03 s | 4 h 29 m 55 s |
| Reservation HEALTHY leads VM | — | 5 m 29 s | 5 m 20 s | 5 m 53 s | — |
| **Fault → job stepping** | < 60 min | **1 h 18 m 18 s** | (operator latency) | — | 16 h 04 m |
