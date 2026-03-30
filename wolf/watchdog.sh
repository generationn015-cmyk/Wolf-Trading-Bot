#!/bin/bash
# Wolf Watchdog — keeps Wolf running forever.
# Restarts on crash with exponential backoff (cap 60s).
# Logs to /data/.openclaw/workspace/wolf/wolf.log
# Usage: bash watchdog.sh &

WOLF_DIR="/data/.openclaw/workspace/wolf"
LOG="$WOLF_DIR/wolf.log"
PIDFILE="$WOLF_DIR/wolf.pid"
MAX_BACKOFF=60
BACKOFF=2

echo "$(date) [watchdog] Starting Wolf watchdog" | tee -a "$LOG"

# Start native monitor in background (zero API cost — pure Python)
pkill -f "native_monitor.py" 2>/dev/null
sleep 1
python3 -u "$WOLF_DIR/scripts/native_monitor.py" >> /tmp/wolf_monitor.log 2>&1 &
echo "$(date) [watchdog] Native monitor PID: $!" | tee -a "$LOG"

while true; do
    cd "$WOLF_DIR" || exit 1

    # Kill any stale wolf process
    if [ -f "$PIDFILE" ]; then
        OLD_PID=$(cat "$PIDFILE")
        kill -0 "$OLD_PID" 2>/dev/null && kill "$OLD_PID" 2>/dev/null
        rm -f "$PIDFILE"
    fi

    echo "$(date) [watchdog] Launching Wolf..." | tee -a "$LOG"
    python3 -u main.py 2>&1 &
    WOLF_PID=$!
    echo $WOLF_PID > "$PIDFILE"
    echo "$(date) [watchdog] Wolf PID: $WOLF_PID" | tee -a "$LOG"

    wait $WOLF_PID
    EXIT_CODE=$?

    echo "$(date) [watchdog] Wolf exited (code $EXIT_CODE). Restarting in ${BACKOFF}s..." | tee -a "$LOG"

    # Notify via Telegram on unexpected crash (not clean exit)
    if [ $EXIT_CODE -ne 0 ]; then
        BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN "$WOLF_DIR/.env" | cut -d= -f2)
        CHAT_ID=$(grep TELEGRAM_CHAT_ID "$WOLF_DIR/.env" | cut -d= -f2)
        if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
            curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
                -d "chat_id=${CHAT_ID}" \
                -d "text=🚨 Wolf crashed (exit $EXIT_CODE). Restarting in ${BACKOFF}s..." \
                -d "parse_mode=Markdown" > /dev/null 2>&1
        fi
        # Exponential backoff on repeated crashes
        sleep "$BACKOFF"
        BACKOFF=$((BACKOFF * 2))
        [ $BACKOFF -gt $MAX_BACKOFF ] && BACKOFF=$MAX_BACKOFF
    else
        # Clean exit (e.g. kill switch, maintenance) — short pause then restart
        BACKOFF=2
        sleep 3
    fi
done
