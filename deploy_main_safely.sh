#!/usr/bin/env bash
# Run from a trusted Mac checkout after main has passed review and tests.
set -euo pipefail

SERVER="ubuntu@42.193.246.49"
SSH_KEY="${DOUYIN_MONITOR_SSH_KEY:-$HOME/.ssh/douyin_live_monitor_ed25519}"
REMOTE_DIR="/opt/douyin-live-monitor/live-digest-service"

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "Refusing deployment: switch to the reviewed main branch first."
  exit 2
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Refusing deployment: commit or discard local changes first."
  exit 2
fi

scp -i "$SSH_KEY" live_digest_service.py safe_apply_update.sh "$SERVER:/tmp/"
ssh -i "$SSH_KEY" "$SERVER" "sudo install -o douyin-live -g douyin-live -m 644 /tmp/live_digest_service.py '$REMOTE_DIR/live_digest_service.py.next' && sudo install -o root -g root -m 755 /tmp/safe_apply_update.sh '$REMOTE_DIR/safe_apply_update.sh' && sudo rm -f /tmp/live_digest_service.py /tmp/safe_apply_update.sh && sudo systemctl start --no-block douyin-safe-deploy.service"
echo "Release staged. It will apply automatically after all active live sessions finish."
