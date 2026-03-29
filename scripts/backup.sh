#!/bin/bash
# Wolf workspace backup to GitHub
# Runs automatically via heartbeat or cron

WORKSPACE="/data/.openclaw/workspace"
ENV_FILE="$HOME/.openclaw/.env"

# Load GitHub token from env
if [ -f "$ENV_FILE" ]; then
  export $(grep -E '^GITHUB_TOKEN=' "$ENV_FILE" | xargs)
fi

if [ -z "$GITHUB_TOKEN" ]; then
  echo "[backup] ERROR: GITHUB_TOKEN not set in ~/.openclaw/.env"
  exit 1
fi

cd "$WORKSPACE" || exit 1

# Stage all changes
git add -A

# Only commit if there are changes
if git diff --cached --quiet; then
  echo "[backup] No changes to commit"
  exit 0
fi

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
git commit -m "Auto-backup: $TIMESTAMP"

# Push using token from env
git push https://x-token-auth:${GITHUB_TOKEN}@github.com/generationn015-cmyk/Wolf-Trading-Bot.git main 2>&1

if [ $? -eq 0 ]; then
  echo "[backup] Pushed successfully at $TIMESTAMP"
else
  echo "[backup] Push failed at $TIMESTAMP"
  exit 1
fi
