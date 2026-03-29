---
summary: "Wolf heartbeat tasks"
read_when:
  - Every heartbeat
---

# HEARTBEAT.md

## Every heartbeat
- Run workspace backup: bash /data/.openclaw/workspace/scripts/backup.sh
- Check SESSION-STATE.md for any pending actions that need attention
- If Jefe has been quiet for >8 hours and it's not late night, check if anything needs a proactive update

## Do not run if
- It's between 23:00 and 08:00 EST and nothing is urgent
- Backup already ran in the last 30 minutes (check git log)