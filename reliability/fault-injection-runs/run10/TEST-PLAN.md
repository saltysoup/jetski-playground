# Runs 10 & 11 — Xid 79 by injection, then Xid 79 for real

**Status: awaiting approval. Nothing injected, nothing reset, nothing labelled.**

Target `gke-ikwak-reliabilit-a4-highgpu-8g-a4-0256e2f1-34t3` (instance `174490015860863227`),
GPU 7 = PCI `0000:cc:00.0`. Control `-6df3`. Zone `europe-west4-b`, project
`gpu-launchpad-playground`, reservation `nvidia-b200-6bsoymep8ylww`.

Two runs, in order. Run 11 depends on run 10 having returned the node to a clean state.

---

## 1. Why we are switching away from Xid 63

Run 9 Phase 1 closed with a clean null and that changes the plan.

| Run | Stimulus | Result |
| --- | --- | --- |
| 8a | Xid 48 × 5 + `perform-reboot` | nothing |
| 8b | Xid 48 × 5 (DCGM injection) | nothing |
| 8d | Xid 63 × 5, no reset | DEGRADED on #3 (+16.6 s), reason at i1 + 12 m 32 s |
| 8f | Xid 63 × 5 + forced GPU reset each round | DEGRADED on #2 (+1 m 54 s), reason at i1 + 11 m 03 s |
| **9** | **Xid 63 × 2, no reset** | **nothing in 15 min — zero CHANGE lines** |
| 7 | **Xid 79 × 1** | **DEGRADED at +35.6 s, reason at +19 m 03 s** |

Run 9 answers its own question and retires it: **two Xid 63s are not sufficient.** 8f's
degradation on the second injection was reset-assisted — the driver unload/reload feeds the
same detector. The "three consecutive" rule stands as stated.

That leaves Xid 63 as a slow, multi-shot, threshold-dependent stimulus. Xid 79 is a
**single-shot** trigger on one of the three classes Google proactively repairs (74, 79, 140),
and run 7 measured it reaching DEGRADED in **35.6 seconds** off one line. For the two things
we still need — a fast repair measurement, and a verdict on the synthetic-log filter — Xid 79
is the better instrument by a wide margin.

## 2. The two open questions these runs close

**Q1 — is Google filtering our synthetic Xid lines?** This has hung over the whole run 8
series. Run 7's Xid 79 worked on 2026-08-24. The GCE engineer said filtering of Xids carrying
a fake PCIe address was added "recently", possibly inside the 08-24 → 08-27 window that
separates run 7 from run 8a. Every null since could be the filter rather than the platform.
**Run 10 is the controlled replication**: same node, same GPU, same message format, 31 days
later. One variable changed.

**Q2 — does a synthetic line behave like a real fault?** Run 11 answers it by producing a
**genuine** driver-emitted Xid 79 via a PCIe secondary bus reset, which no filter can reject
because the driver wrote it. If run 10 fires and run 11 fires the same way, synthetic
injection is validated as a methodology and everything in runs 3–9 keeps its weight. If run 10
is silent and run 11 fires, we have proof of filtering and a written-up caveat on the earlier
results.

Run 10 also delivers the measurement run 9 was built for and never reached: **label → node
usable**, against the 4 h 36 m 03 s / ≈4 h 47 m baseline.

---

## 3. Current state, verified 2026-09-24 06:19Z

```
34t3   Ready, 8 allocatable GPUs, VM RUNNING, upcomingMaintenance null
6df3   Ready, VM RUNNING
res_health HEALTHY, block HEALTHY, pending/ongoing null
no perform-maintenance or perform-reboot labels
dapo-run9 training, pollers + instance probe live since 05:31Z
```

**One contaminant to declare:** `-34t3`'s kernel log already contains two Xid 63 lines from
today (06:01:53.613Z, 06:03:31.289Z). They produced nothing in 15 minutes, but if a
"consecutive Xid" counter exists they are still on it. Handling is in §4.3.

### PCIe topology for GPU 7 — mapped, and favourable

```
pci0000:c8
└─ 0000:c8:00.0   PCI bridge, Google Inc [1ae0:a010]      (virtual root port)
   └─ 0000:c9:00.0   PLX PEX8796 switch, upstream port
      ├─ 0000:ca:00.0  downstream port
      ├─ 0000:ca:01.0  downstream port  ←── SBR target
      │  └─ 0000:cc:00.0  NVIDIA GB100 [B200]  = GPU 7   (SOLE CHILD)
      ├─ 0000:ca:02.0  downstream port
      └─ 0000:ca:03.0  downstream port
```

Three facts that make run 11 viable rather than reckless:

1. **GPU 7 is the only device behind `0000:ca:01.0`.** No NIC, no NVMe, nothing else shares
   the port. A secondary bus reset there has a blast radius of exactly one GPU on the PCIe side.
2. **No AER or DPC capability is exposed** on `0000:c9:00.0` or `0000:c8:00.0`. A surprise
   link-down should not trip Downstream Port Containment and disable the rest of the switch.
3. `setpci` and `lspci` are present on the host; kernel is 6.12.85+; `BRIDGE_CONTROL` on
   `ca:01.0` currently reads `0002` (SERR enable, SBR bit clear).

---

## 4. Run 10 — synthetic Xid 79, full cycle to B14

### 4.1 Stimulus

One line, byte-identical to the one that worked in run 7, on the real PCI address:

```
[  0.000000] NVRM: Xid (PCI:0000:cc:00): 79, pid=0, name=CLAUDE-SIM-RUN10, GPU has fallen off the bus.
```

**Deliberately keeping run 7's format, including the leading `[  0.000000]` and the
`CLAUDE-SIM` marker**, even though `inject63.sh` later stripped both as "cheap tells". The
question is not "can we write a more convincing line" — it is "did what used to work stop
working". Changing the format would confound the one variable we are testing. If this line is
silent, attempt #2 uses the clean `inject63.sh`-style format and the difference between the
two *is* the filter's signature.

Also kill the GPU-7 PIDs, as run 7 did. A GPU that has fallen off the bus takes its processes
with it, and it starts the job-death clock for the badput metric.

### 4.2 Ladder

| Step | Action | Then |
| --- | --- | --- |
| 1 | Inject Xid 79 (run 7 format) + kill GPU-7 PIDs | observe **20 min** |
| 2 | If no DEGRADED and no reason → inject #2 in clean format, no marker, no fake timestamp | observe **20 min** |
| 3 | If still nothing → `logscan79.py` over Cloud Logging + serial console | **abort before B7** |

Run 7 reached DEGRADED at +35.6 s and the maintenance reason at +19 m 03 s, so 20 minutes is
roughly M4 + 1 min. Two attempts maximum; this is a discriminator, not a volume test.

Reading the outcomes:

- **Fires on #1** → no filter, or the filter does not apply to Xid 79. Q1 answered negative:
  the 8a/8b nulls were about **Xid class**, not about detection of synthetic lines. Every run
  8 result keeps its weight.
- **Silent on #1, fires on #2** → filtering is real and keys on format. Run 7's numbers stay
  valid for their date; 8a/8b become uninterpretable and get a caveat in the post.
- **Silent on both** → either the filter is stricter than format, or Xid 79 was de-listed.
  `logscan79.py` separates "our line never reached Cloud Logging" (filtered) from "it is
  there and the platform declined" (policy). Abort before B7, escalate to the engineer, and
  run 11 becomes the decisive test rather than a confirmation.

### 4.3 Handling the two leftover Xid 63s

Do not attempt to clear them — there is no API for it and we do not know the counter's
window. Instead, **disambiguate by latency**. A single Xid 79 in run 7 reached DEGRADED in
35.6 s. Xid 63 needs three and took 16.6 s *after the third*. If DEGRADED lands within ~60 s
of the Xid 79, attribution is unambiguous. If it lands several minutes later, the "63+63+79 =
three consecutive" reading is live and gets recorded as an alternative explanation rather
than dismissed. Either way `logscan79.py` dumps every Xid line on the node for the preceding
hour so the reader can see exactly what was on the log.

### 4.4 Phases

- **Phase 0** — verify baseline, confirm `dapo-run9` is still stepping, capture
  `pre-gpu-health-10.txt` (all 8 GPUs: ECC, remapped rows, persistence), confirm pollers alive,
  drop cadence to 5 s.
- **Phase 1** — the ladder above.
- **Phase 2** — detection measurement, no action. Serial-console `timestamp` is authoritative.
- **Phase 3** — **APPROVAL GATE (B7). Full stop.** I present the raised event verbatim and the
  detection numbers and wait for an explicit go/no-go. **After the label there is no abort** —
  `canReschedule` goes `false` within ~1 min.
- **Phase 4** — apply `perform-maintenance=true`, 10 s polling, measure B7 → B14.
- **Phase 5** — re-test the three secondary findings (`windowEndTime` advisory,
  `maintenanceOngoingCount` early clear, reservation-leads-VM gap).
- **Phase 6** — `run10/RESULTS.md` + `run10/TIMELINE.md`. B14 recovery is pre-approved.

---

## 5. Run 11 — PCIe secondary bus reset, a genuine Xid 79

Runs only after run 10 has returned `-34t3` to Ready/8 GPUs/`upcomingMaintenance: null`.
A pending maintenance event from run 10 would mask run 11's detection signal entirely.

### 5.1 Method

Assert the Secondary Bus Reset bit on GPU 7's upstream downstream-port while the NVIDIA
driver holds the device. The GPU disappears from under the driver, which is exactly the
condition Xid 79 exists to report.

```bash
# state before
lspci -D -vv -s 0000:cc:00.0 > pre-bridge-gpu7.txt
setpci -s 0000:ca:01.0 BRIDGE_CONTROL          # expect 0002

T0=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
setpci -s 0000:ca:01.0 BRIDGE_CONTROL=0x42     # set bit 6 (SBR), keep SERR
sleep 1
setpci -s 0000:ca:01.0 BRIDGE_CONTROL=0x02     # release
```

**Why `setpci` and not `/sys/.../reset_subordinate`.** The sysfs path exists on `ca:01.0` and
is the tidy option, but the kernel routes it through `pci_reset_bus()`, which calls the
NVIDIA driver's `reset_prepare`/`reset_done` callbacks. That is a *cooperative* reset — the
driver is told, quiesces, and recovers, and it will most likely produce no Xid at all.
`setpci` writes the bridge register directly and the driver learns about it the way it learns
about a real failure: the device stops answering. That is the fault we want.

`reset_subordinate` is kept as a documented fallback if `setpci` produces nothing.

### 5.2 What we expect, and what else might happen

Expected on the serial console within seconds:

```
NVRM: Xid (PCI:0000:cc:00): 79, GPU has fallen off the bus.
NVRM: GPU 0000:cc:00.0: GPU has fallen off the bus.
NVRM: A GPU crash dump has been created. ...
```

Additional outcomes to instrument for, all of them informative:

| Outcome | Meaning |
| --- | --- |
| Xid 79 only on GPU 7, other 7 healthy | clean single-GPU fault, best case for comparison with run 10 |
| **Xid 74 / NVSwitch errors on the other GPUs** | GPU 7 is an NVLink peer of all 7 others; losing it may fault the fabric. Blast radius becomes the whole node. Realistic, and worth measuring, but it is not the same stimulus as run 10 |
| Node hangs or panics | possible; the instance probe runs outside the node and still records it |
| GPU returns on rescan, no Xid | SBR was absorbed cleanly; fall back to `reset_subordinate`, then reconsider |

**Hardware safety:** SBR is a standard PCIe mechanism used by every VM passthrough stack on
every boot. It does not damage the GPU. The risk here is availability, not silicon.

### 5.3 The honest difference from every previous run

Runs 3–10 wrote a log line. **Run 11 breaks the machine.** Two consequences the approval needs
to cover explicitly:

1. **The node may be genuinely unusable afterwards, with no path back except the host repair.**
   In every prior run B7 was a choice. Here it may be the only exit. *Approving run 11 should
   be read as approving B7 for run 11 in advance* — I will still report the event before
   labelling, but "abort and leave the node as it is" may not be an available answer.
2. **`dapo` dies immediately and hard**, not via a kill we issued. That is the point — it is
   the first time we will observe the platform's reaction to a fault we did not simulate.

If the GPU survives and the platform does *not* escalate, the recovery ladder before
requesting a repair is: PCI rescan → driver reload → `perform-reboot=true` label → then B7.

### 5.4 Phases

Same shape as run 10: Phase 0 baseline (plus full `nvidia-smi -q` and `lspci -vv` dumps for
all 8 GPUs, and an NVLink status capture so fabric damage is provable), Phase 1 the SBR,
Phase 2 detection measurement, Phase 3 the B7 report — **notification rather than gate, per
§5.3** — Phase 4 repair, Phase 5 secondary findings, Phase 6 write-up.

---

## 6. What the results will contain

Both runs produce `RESULTS.md` and `TIMELINE.md` in the same shape as run 7 and run 8 — every
stage with an absolute timestamp and **both** elapsed columns, then the derived intervals.

### Stage ladder

| Stage | Event |
| --- | --- |
| — | Baseline: reservation + block `HEALTHY`, `upcomingMaintenance: null` |
| **B0** | **T0** — Xid 79 written to `/dev/kmsg` (run 10) / SBR asserted (run 11) |
| B0a | *(run 11)* SBR released; GPU present/absent on the bus; driver's own Xid text verbatim |
| B0b | *(run 11)* other 7 GPUs: NVLink state, any Xid 74/SXID |
| B1 | **M1** Xid emitted to Cloud Logging (`timestamp`) |
| B1′ | **M1′** Xid queryable (`receiveTimestamp`) |
| B2 | Ray job `FAILED` — badput starts |
| B4 | **M3** reservation + block → `DEGRADED` |
| B3a | **M2** alert delivered to Pub/Sub (`publishTime`) |
| **B5** | **M4** `upcomingMaintenance` populated — `type`, `maintenanceStatus`, `maintenanceReasons`, `canReschedule`, `windowStartTime`, `windowEndTime`, `latestWindowStartTime`, all verbatim |
| B6 | Operator decision |
| **B7** | **T0′** — `perform-maintenance=true` applied, `LABEL_SENT`/`LABEL_ACK` to ms |
| B8 | `PENDING`→`ONGOING`, `UNSCHEDULED`→`SCHEDULED`, `canReschedule`→`false`, window rewritten |
| — | reservation + block `pending 1→0`, `ongoing 0→1` |
| B9 | taint `maintenance-window-started=PreferNoSchedule` |
| **B10** | node `Shutdown` + `NodeNotReady`; drain duration; pod evictions; cluster GPUs 16→8 |
| **B11** | VM `RUNNING` → `REPAIRING` |
| — | block `maintenanceOngoingCount 1 → null` |
| — | maintenance `ONGOING` → `CLEAR`, reasons → null, reservation `HEALTHY` |
| **B12** | VM `REPAIRING` → `RUNNING` (cross-checked against `lastStartTimestamp`) |
| **B13** | node `Ready`, 8 allocatable GPUs, cluster 8→16 |
| **B14** | `prep-pods.sh`, resubmit, first training step, throughput back to 116 s/step |

### Headline intervals, with the numbers they are measured against

| Interval | run 7 (Xid 79) | 8d | 8f |
| --- | --- | --- | --- |
| T0 → job dead | 8.1 s | — | — |
| **T0 → M3 DEGRADED** | **35.6 s** | i3 + 16.6 s | i2 + 1 m 54.2 s |
| **T0 → M4 reason (B5)** | **19 m 03 s** | i1 + 12 m 32.1 s | i1 + 11 m 03 s |
| B7 → type flip (B8) | 52.7 s | 20.9 s | 56.3 s |
| B9 taint → B10 drained | 16 m 54 s | — | — |
| B10 → B11 `REPAIRING` | 2 m 05 s | — | — |
| **Host repair (B11 → B12)** | **4 h 09 m 49 s** | 4 h 08 m 01 s | — |
| **B7 → node usable (B13)** | 4 h 29 m 55 s | **4 h 36 m 03 s** | **≈4 h 47 m** |
| **T0 → cluster whole** | **5 h 00 m 26 s** | — | — |
| Repair overrun past `windowEndTime` | +27 m 21 s | +27 m 46 s | — |

Plus, new for these runs: **total outage — job death (B2) → job stepping again (B14)**, which
is the number that actually matters to a training team and which no previous run recorded
end to end.

---

## 7. Instrumentation

Unchanged from run 9 and already live — this is the part run 9 got right and 8f got wrong:

- `watch-supervisor.sh` → `watch9.py` at 5 s (detection) / 10 s (repair), restarted on exit,
  `# RESTART` markers so coverage gaps are explicit. New log per run.
- `instance-probe.sh` — 60 s, independent code path, raw instance JSON including
  `lastStartTimestamp`. This is the artifact that would have saved 8f, and in run 11 it is
  also the only thing that survives the node hanging.
- Serial console via Cloud Logging:
  `log_id("serialconsole.googleapis.com/serial_port_1_output") resource.type="gce_instance" resource.labels.instance_id="174490015860863227"`
- `train.log` tail for B2 and B14.

---

## 8. Abort conditions

| Condition | Action |
| --- | --- |
| Xid 79 absent from Cloud Logging (run 10) | stop before B7 — we would be measuring the filter, not the platform |
| No DEGRADED after both run 10 attempts | stop before B7, escalate, go to run 11 |
| Control node `-6df3` degrades | stop everything, this is not our stimulus |
| Run 11 faults all 8 GPUs / the fabric | **record and continue** — the node needs the repair regardless |
| Anything unexpected between B7 and B10 | **no abort exists** — record and continue |

---

## 9. Cost

$90.22/node-hour × 2 nodes = **$180.44/hour**, billed whether the node is repairing or not.

| Scenario | Run 10 | Run 11 | Total |
| --- | --- | --- | --- |
| Repairs are genuinely fast (~30 min) | ~1.5 h → $271 | ~1.5 h → $271 | **~$542** |
| Repairs unchanged (~4.8 h) | ~5.5 h → $992 | ~5.5 h → $992 | **~$1,984** |
| Run 10 aborts before B7, run 11 full | ~0.7 h → $126 | ~5.5 h → $992 | **~$1,118** |

Each run that raises a maintenance event costs a repair cycle — there is no API to dismiss a
pending event, so the only way to clear it is to let the repair happen. That is the main
driver of the number above, and the reason the two runs cannot be collapsed into one.

---

## 10. Decisions needed before I start

1. **Approve run 10** — synthetic Xid 79, full cycle through B14, with the B7 gate intact.
2. **Approve run 11 in principle** — PCIe SBR, accepting §5.3: the node may be genuinely
   broken and B7 may be the only exit.
3. **Confirm the run 10 message format choice** — run 7's exact string first (my
   recommendation, it is the controlled variable), clean format as attempt #2.
4. **Confirm both runs stay on `-34t3`** rather than moving run 11 to the control node.

Nothing runs until these are answered.
