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

set_deployment_gate() {
  /opt/douyin-live-monitor/.venv/bin/python - "$STATE_DB" "$1" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
with conn:
    conn.execute("CREATE TABLE IF NOT EXISTS service_flags (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO service_flags VALUES('deployment_pending', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sys.argv[2],))
PY
}

recording_active() {
  pgrep -af '[f]fmpeg' | grep -F -- '-f segment' | grep -Fq 'recordings/'
}

session_active() {
  /opt/douyin-live-monitor/.venv/bin/python - "$STATE_DB" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT EXISTS(SELECT 1 FROM sessions WHERE active=1)").fetchone()
raise SystemExit(0 if row and row[0] else 1)
PY
}

recovery_active() {
  pgrep -af '[r]ecover_failed_session.py' >/dev/null
}

processing_active() {
  pgrep -x ffmpeg >/dev/null || recovery_active || session_active
}

while true; do
  while recording_active || processing_active; do
    echo "A live session or post-processing task is still active; checking again in 60 seconds."
    sleep 60
  done

  # Close the race where a new room starts between the process check and the
  # restart. Existing recording workers are not affected by this gate.
  set_deployment_gate 1
  sleep 75
  if recording_active || processing_active; then
    set_deployment_gate 0
    continue
  fi
  break
done

trap 'set_deployment_gate 0' EXIT

install -o douyin-live -g douyin-live -m 644 "$STAGED_FILE" "$TARGET_FILE"
rm -f "$STAGED_FILE"
systemctl restart "$SERVICE"
systemctl is-active --quiet "$SERVICE"
set_deployment_gate 0
trap - EXIT
echo "Release applied successfully."
