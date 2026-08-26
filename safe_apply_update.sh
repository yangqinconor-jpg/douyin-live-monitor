#!/usr/bin/env bash
# Apply a staged main-branch release only after all live sessions have ended.
set -euo pipefail

APP_DIR="/opt/douyin-live-monitor/live-digest-service"
SERVICE="live-digest.service"
STATE_DB="$APP_DIR/monitor_state.sqlite3"
STAGED_FILE="$APP_DIR/live_digest_service.py.next"
TARGET_FILE="$APP_DIR/live_digest_service.py"
LOCK_FILE="$APP_DIR/.deployment-pending"

if [[ ! -f "$STAGED_FILE" ]]; then
  echo "No staged release found; nothing to deploy."
  exit 0
fi

touch "$LOCK_FILE"
echo "Release staged. Waiting for active live sessions to finish."

until /opt/douyin-live-monitor/.venv/bin/python - "$STATE_DB" <<'PY'
import sqlite3
import sys

try:
    conn = sqlite3.connect(sys.argv[1])
    active = conn.execute("SELECT COUNT(*) FROM sessions WHERE active=1").fetchone()[0]
except Exception:
    active = 1
raise SystemExit(0 if active == 0 else 1)
PY
do
  echo "A live session is still active; checking again in 60 seconds."
  sleep 60
done

install -o douyin-live -g douyin-live -m 644 "$STAGED_FILE" "$TARGET_FILE"
rm -f "$STAGED_FILE"
systemctl restart "$SERVICE"
systemctl is-active --quiet "$SERVICE"
rm -f "$LOCK_FILE"
echo "Release applied successfully."
