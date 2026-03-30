---
summary: "Wolf heartbeat tasks"
read_when:
  - Every heartbeat
---

# HEARTBEAT.md

## Overnight (23:00–08:00 ET) — MINIMAL MODE
- Reply HEARTBEAT_OK immediately. Do not run backups, do not check anything.
- Exception: only act if Wolf health_monitor already sent an alert (you'll know from context).

## Daytime (08:00–23:00 ET)
- Run backup at most once every 2 hours: check `git log --since='2 hours ago' --oneline | wc -l`
  - If 0 commits in last 2h AND Jefe has been active: bash /data/.openclaw/workspace/scripts/backup.sh
  - Otherwise: skip backup, reply HEARTBEAT_OK
- Check SESSION-STATE.md only if Jefe has sent a message in the last 10 minutes
- Never proactively message Jefe during heartbeat — wait for the 6AM report or a real event

## Cost rule
- Default response: HEARTBEAT_OK
- Only do work if there is a specific, concrete reason to act