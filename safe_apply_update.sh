#!/usr/bin/env bash
# Apply a staged main-branch release only after all live sessions have ended.
set -euo pipefail

APP_DIR="/opt/douyin-live-monitor/live-digest-service"
SERVICE="live-digest.service"
STATE_DB="$APP_DIR/monitor_state.sqlite3"
STAGED_FILE="$APP_DIR/live_digest_service.py.next"
TARGET_FILE="$APP_DIR/live_digest_service.py"

if [[ ! -f "$STAGED_FILE" ]]; then
  echo "No staged release found; nothing to deploy."
  exit 0
fi

echo "Release staged. Waiting for active live sessions to finish."

until /opt/douyin-live-monitor/.venv/bin/python - "$STATE_DB" <<'PY'
import sqlite3
import sys

try:
    conn = sqlite3.connect(sys.argv[1])
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS service_flags (key TEXT PRIMARY KEY, value TEXT)")
        active = conn.execute("SELECT COUNT(*) FROM sessions WHERE active=1").fetchone()[0]
        if active == 0:
            conn.execute("INSERT INTO service_flags VALUES('deployment_pending', '1') ON CONFLICT(key) DO UPDATE SET value='1'")
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
/opt/douyin-live-monitor/.venv/bin/python - "$STATE_DB" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
with conn:
    conn.execute("CREATE TABLE IF NOT EXISTS service_flags (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO service_flags VALUES('deployment_pending', '0') ON CONFLICT(key) DO UPDATE SET value='0'")
PY
echo "Release applied successfully."
