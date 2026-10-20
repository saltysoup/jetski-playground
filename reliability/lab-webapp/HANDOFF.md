# Lab Web App Handoff (`reliability/lab-webapp/`)

See [`../HANDOFF.md`](../HANDOFF.md) and [`../CE_BREADCRUMB_LAB_GUIDE.md`](../CE_BREADCRUMB_LAB_GUIDE.md) for the complete 3-Day (10-Breadcrumb) Live Cloud Console Detective Lab documentation, Cloud Logging queries, Veritas link (`MACHINE:yugrqvu-tl3`), and instructor answer keys.

## Quick Commands

```bash
# Start the web app & real-time scoreboard server on port 8787:
python3 server.py

# Pre-stage / refresh all Day 1, Day 2, and Day 3 pods & serial console logs on the live ikwak-reliability cluster:
bash prestage-scenarios.sh
```

- **Instructor Password:** `ilovejensen`
- **Total Clues:** `10` (`Day 1 Lab`: 3 clues · `Day 2 Lab`: 3 clues · `Day 3 Lab`: 4 clues; `10,000` max points)
