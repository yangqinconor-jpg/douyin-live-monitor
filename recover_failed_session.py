#!/usr/bin/env python3
"""Recover one stopped live session without restarting the monitoring service."""
from __future__ import annotations

import argparse
import importlib.machinery
import json
import sqlite3
from pathlib import Path


def load_service(path: Path):
    return importlib.machinery.SourceFileLoader("recovery_live_digest_service", str(path)).load_module()


def build_settings(service, cfg: dict):
    return service.Settings(
        recorder_root=Path(cfg["recorder_root"]), output_dir=Path(cfg.get("output_dir", "./recordings")),
        webhook=cfg.get("feishu_webhook", ""), app_id=cfg.get("feishu_app_id", ""),
        app_secret=cfg.get("feishu_app_secret", ""), chat_id=cfg.get("feishu_chat_id", ""),
        recipient_open_ids=cfg.get("feishu_open_ids", []), recipients=cfg.get("feishu_recipients", []),
        poll_seconds=int(cfg.get("poll_seconds", 60)), segment_seconds=int(cfg.get("segment_seconds", 900)),
        whisper_model=cfg.get("whisper_model", "small"), whisper_language=cfg.get("whisper_language", "zh"),
        transcription_mode=cfg.get("transcription_mode", "server"), proxy=cfg.get("proxy", ""),
        cookie=cfg.get("douyin_cookie", ""), bitable_app_token=cfg.get("bitable_app_token", ""),
        account_table_id=cfg.get("account_table_id", ""), record_table_id=cfg.get("record_table_id", ""),
        config_poll_seconds=int(cfg.get("config_poll_seconds", 60)),
        state_db=cfg.get("state_db", "./monitor_state.sqlite3"),
        feishu_user_token_path=cfg.get("feishu_user_token_path", ""),
        drive_root_folder_token=cfg.get("drive_root_folder_token", ""),
        drive_platform_folder_name=cfg.get("drive_platform_folder_name", "抖音"),
        minutes_poll_seconds=int(cfg.get("minutes_poll_seconds", 60)),
        minutes_timeout_seconds=int(cfg.get("minutes_timeout_seconds", 7200)),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", default="live_digest_service.py")
    parser.add_argument("--config", default="live_digest.json")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--recovery-dir", required=True)
    args = parser.parse_args()

    service = load_service(Path(args.module).resolve())
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    settings = build_settings(service, cfg)
    ledger = service.DeliveryLedger(Path(settings.state_db))
    conn = sqlite3.connect(settings.state_db)
    row = conn.execute(
        "SELECT account_name, recipients, record_id FROM sessions WHERE account_id=? AND session_id=?",
        (args.account_id, args.session_id),
    ).fetchone()
    if not row:
        raise RuntimeError("Session snapshot was not found")
    account_name, recipients_json, record_id = row
    segments = sorted(Path(args.recovery_dir).glob("*.mp4"))
    if not segments and not ledger.session_artifacts(args.session_id):
        raise RuntimeError("No recording segments or archived video checkpoint was found")

    print(
        f"RECOVERY_START session={args.session_id} segments={len(segments)} "
        f"bytes={sum(path.stat().st_size for path in segments)}",
        flush=True,
    )
    service.complete_with_feishu_minutes(
        settings, room_dir=settings.output_dir / args.account_id, segments=segments,
        account_name=account_name, session_id=args.session_id, record_id=record_id,
        title="", url=f"https://live.douyin.com/{args.account_id}",
        recipients=json.loads(recipients_json), ledger=ledger,
    )
    print(f"RECOVERY_DONE session={args.session_id}", flush=True)


if __name__ == "__main__":
    main()
