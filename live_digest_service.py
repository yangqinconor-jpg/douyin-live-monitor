#!/usr/bin/env python3
"""Monitor two Douyin rooms, record them, transcribe locally, and notify Feishu."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


@dataclass
class Settings:
    recorder_root: Path
    output_dir: Path
    webhook: str
    app_id: str = ""
    app_secret: str = ""
    chat_id: str = ""
    recipient_open_ids: list[str] | None = None
    recipients: list[dict[str, str]] | None = None
    poll_seconds: int = 60
    segment_seconds: int = 900
    whisper_model: str = "small"
    whisper_language: str = "zh"
    transcription_mode: str = "server"
    proxy: str = ""
    cookie: str = ""
    bitable_app_token: str = ""
    account_table_id: str = ""
    record_table_id: str = ""
    config_poll_seconds: int = 60
    state_db: str = "./monitor_state.sqlite3"
    feishu_user_token_path: str = ""
    drive_root_folder_token: str = ""
    drive_platform_folder_name: str = "抖音"
    minutes_poll_seconds: int = 60
    minutes_timeout_seconds: int = 7200


@dataclass
class Account:
    account_id: str
    name: str
    room_url: str
    enabled: bool
    recipients: list[dict[str, str]]
    table_record_id: str = ""


class CompletionNotificationError(RuntimeError):
    """The recording was processed, but one or more completion messages failed."""


class LowDiskSpaceError(RuntimeError):
    """There is not enough free space to create the merged recording."""


class RecordingIntegrityError(RuntimeError):
    """A local or uploaded recording failed an integrity check."""


@dataclass(frozen=True)
class VideoMetadata:
    size_bytes: int
    duration_seconds: float


_VIDEO_ARCHIVE_LOCK = threading.Lock()


class DeliveryLedger:
    """Durable per-session/per-recipient/per-message idempotency ledger."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS deliveries (session_id TEXT, recipient_id TEXT, message_type TEXT, status TEXT, message_id TEXT, error TEXT, updated_at TEXT, PRIMARY KEY(session_id, recipient_id, message_type))")
            self.db.execute("CREATE TABLE IF NOT EXISTS sessions (account_id TEXT PRIMARY KEY, session_id TEXT, account_name TEXT, recipients TEXT, record_id TEXT, started_ms INTEGER, active INTEGER, ended_ms INTEGER, recording_end_ms INTEGER)")
            session_columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
            if "ended_ms" not in session_columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN ended_ms INTEGER")
            if "recording_end_ms" not in session_columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN recording_end_ms INTEGER")
            self.db.execute("CREATE TABLE IF NOT EXISTS session_artifacts (session_id TEXT PRIMARY KEY, archive_video_url TEXT, archive_video_size INTEGER, minutes_url TEXT, transcript_url TEXT, summary_url TEXT, video_name TEXT, minutes_title TEXT, minutes_created_at INTEGER, recording_status TEXT, integrity_note TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS minutes_submissions (session_id TEXT PRIMARY KEY, status TEXT, minutes_url TEXT, error TEXT, updated_at TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS service_flags (key TEXT PRIMARY KEY, value TEXT)")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(session_artifacts)")}
            for name, definition in {
                "archive_video_url": "TEXT", "archive_video_size": "INTEGER", "minutes_url": "TEXT", "summary_url": "TEXT",
                "minutes_title": "TEXT", "minutes_created_at": "INTEGER", "recording_status": "TEXT",
                "integrity_note": "TEXT",
            }.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE session_artifacts ADD COLUMN {name} {definition}")
            if "video_url" in columns:
                self.db.execute(
                    "UPDATE session_artifacts SET archive_video_url=video_url "
                    "WHERE COALESCE(archive_video_url, '')=''"
                )
            if "minute_url" in columns:
                self.db.execute(
                    "UPDATE session_artifacts SET minutes_url=minute_url "
                    "WHERE COALESCE(minutes_url, '')=''"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO minutes_submissions(session_id,status,minutes_url,error,updated_at) "
                "SELECT session_id,'completed',minutes_url,'',datetime('now') FROM session_artifacts "
                "WHERE COALESCE(minutes_url, '')<>''"
            )

    def claim(self, session_id: str, recipient_id: str, message_type: str) -> bool:
        with self.lock:
            try:
                # BEGIN IMMEDIATE serializes claims across the service and any
                # recovery worker that uses the same SQLite database.
                self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute(
                    "SELECT status, updated_at > datetime('now', '-10 minutes') FROM deliveries "
                    "WHERE session_id=? AND recipient_id=? AND message_type=?",
                    (session_id, recipient_id, message_type),
                ).fetchone()
                if row and (row[0] == "sent" or (row[0] == "sending" and row[1])):
                    self.db.commit()
                    return False
                self.db.execute(
                    "INSERT INTO deliveries VALUES(?,?,?,?,?,?,datetime('now')) "
                    "ON CONFLICT(session_id,recipient_id,message_type) DO UPDATE SET "
                    "status='sending', message_id='', error=NULL, updated_at=datetime('now')",
                    (session_id, recipient_id, message_type, "sending", "", ""),
                )
                self.db.commit()
                return True
            except Exception:
                self.db.rollback()
                raise

    def finish(self, session_id: str, recipient_id: str, message_type: str, message_id: str = "", error: str = "") -> None:
        status = "sent" if not error else "failed"
        with self.lock, self.db:
            self.db.execute("UPDATE deliveries SET status=?, message_id=?, error=?, updated_at=datetime('now') WHERE session_id=? AND recipient_id=? AND message_type=?", (status, message_id, error, session_id, recipient_id, message_type))

    def claim_minutes_submission(self, session_id: str) -> tuple[bool, str]:
        """Claim the one allowed Minutes creation call for a session."""
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute(
                    "SELECT status, minutes_url FROM minutes_submissions WHERE session_id=?", (session_id,),
                ).fetchone()
                if row and row[0] in {"submitting", "completed"}:
                    self.db.commit()
                    return False, str(row[1] or "")
                self.db.execute(
                    "INSERT INTO minutes_submissions VALUES(?, 'submitting', '', '', datetime('now')) "
                    "ON CONFLICT(session_id) DO UPDATE SET status='submitting', minutes_url='', "
                    "error='', updated_at=datetime('now')",
                    (session_id,),
                )
                self.db.commit()
                return True, ""
            except Exception:
                self.db.rollback()
                raise

    def finish_minutes_submission(self, session_id: str, *, minutes_url: str = "", error: str = "") -> None:
        status = "completed" if minutes_url else "failed"
        with self.lock, self.db:
            self.db.execute(
                "UPDATE minutes_submissions SET status=?, minutes_url=?, error=?, updated_at=datetime('now') "
                "WHERE session_id=?",
                (status, minutes_url, error, session_id),
            )

    def active_session(self, account_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT session_id, account_name, recipients, record_id, started_ms, ended_ms, recording_end_ms FROM sessions WHERE account_id=? AND active=1", (account_id,)).fetchone()
        if not row:
            return None
        return {"session_id": row[0], "account_name": row[1], "recipients": json.loads(row[2]), "record_id": row[3], "started_ms": row[4], "ended_ms": row[5], "recording_end_ms": row[6]}

    def start_session(self, account_id: str, session_id: str, account_name: str, recipients: list[dict[str, str]], record_id: str, started_ms: int) -> bool:
        with self.lock, self.db:
            blocked = self.db.execute("SELECT value FROM service_flags WHERE key='deployment_pending'").fetchone()
            if blocked and blocked[0] == "1":
                return False
            self.db.execute("INSERT INTO sessions(account_id,session_id,account_name,recipients,record_id,started_ms,active,ended_ms,recording_end_ms) VALUES(?,?,?,?,?,?,1,NULL,NULL) ON CONFLICT(account_id) DO UPDATE SET session_id=excluded.session_id, account_name=excluded.account_name, recipients=excluded.recipients, record_id=excluded.record_id, started_ms=excluded.started_ms, active=1, ended_ms=NULL, recording_end_ms=NULL", (account_id, session_id, account_name, json.dumps(recipients, ensure_ascii=False), record_id, started_ms))
            return True

    def set_session_record_id(self, account_id: str, record_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE sessions SET record_id=? WHERE account_id=? AND active=1", (record_id, account_id))

    def set_deployment_pending(self, value: bool) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO service_flags VALUES('deployment_pending', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if value else "0",))

    def end_session(self, account_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE sessions SET active=0 WHERE account_id=?", (account_id,))

    def record_session_end(self, account_id: str, ended_ms: int) -> int:
        """Persist the first observed end time; retries must reuse it."""
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT ended_ms, recording_end_ms FROM sessions WHERE account_id=? AND active=1", (account_id,),
            ).fetchone()
            if row and row[0]:
                return int(row[0])
            final_end = int(row[1] or ended_ms) if row else ended_ms
            self.db.execute(
                "UPDATE sessions SET ended_ms=? WHERE account_id=? AND active=1 AND ended_ms IS NULL",
                (final_end, account_id),
            )
            return final_end

    def record_recorder_stop(self, account_id: str, stopped_ms: int) -> None:
        """Remember the latest recorder exit without ending the live session."""
        with self.lock, self.db:
            self.db.execute(
                "UPDATE sessions SET recording_end_ms=? WHERE account_id=? AND active=1 AND ended_ms IS NULL",
                (stopped_ms, account_id),
            )

    def clear_recorder_stop(self, account_id: str) -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE sessions SET recording_end_ms=NULL WHERE account_id=? AND active=1 AND ended_ms IS NULL",
                (account_id,),
            )

    def session_artifacts(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT archive_video_url, archive_video_size, minutes_url, transcript_url, summary_url, video_name, "
                "minutes_title, minutes_created_at, recording_status, integrity_note "
                "FROM session_artifacts WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return dict(zip((
            "archive_video_url", "archive_video_size", "minutes_url", "transcript_url", "summary_url", "video_name",
            "minutes_title", "minutes_created_at", "recording_status", "integrity_note",
        ), row))

    def save_session_artifacts(self, session_id: str, **artifacts: str | int) -> None:
        allowed = {
            "archive_video_url", "archive_video_size", "minutes_url", "transcript_url", "summary_url", "video_name",
            "minutes_title", "minutes_created_at", "recording_status", "integrity_note",
        }
        unknown = set(artifacts) - allowed
        if unknown:
            raise TypeError(f"Unknown session artifact fields: {', '.join(sorted(unknown))}")
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO session_artifacts(session_id) VALUES(?)", (session_id,))
            if artifacts:
                assignments = ", ".join(f"{name}=?" for name in artifacts)
                self.db.execute(
                    f"UPDATE session_artifacts SET {assignments} WHERE session_id=?",
                    (*artifacts.values(), session_id),
                )

    def save_uploaded_video(self, session_id: str, *, archive_video_url: str, archive_video_size: int,
                            video_name: str) -> None:
        self.save_session_artifacts(
            session_id, archive_video_url=archive_video_url, archive_video_size=archive_video_size,
            video_name=video_name,
        )


_USER_TOKEN_LOCK = threading.Lock()


def recipient_targets(recipients: list[dict[str, str]] | None) -> list[tuple[str, str, str]]:
    result = []
    seen = set()
    for item in recipients or []:
        rid = item.get("id") or item.get("open_id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        result.append((item.get("id_type", "open_id"), rid, item.get("name", rid)))
    return result


def message_url(receive_type: str, session_id: str, recipient_id: str, message_type: str) -> str:
    """Use Feishu's request UUID so a transport retry cannot duplicate a message."""
    key = hashlib.sha256(f"{session_id}:{recipient_id}:{message_type}".encode()).hexdigest()
    return f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}&uuid={key}"


def feishu_response_data(response: requests.Response) -> dict[str, Any]:
    """Fail on Feishu business errors, which can still use an HTTP 200 response."""
    response.raise_for_status()
    data = response.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"Feishu API error {data.get('code')}: {data.get('msg', 'unknown error')}")
    return data


def feishu_text(webhook: str, text: str) -> None:
    if not webhook:
        print(text)
        return
    response = requests.post(webhook, json={"msg_type": "text", "content": {"text": text}}, timeout=20)
    feishu_response_data(response)


def send_text(settings: Settings, text: str, *, recipients: list[dict[str, str]] | None = None, session_id: str = "", message_type: str = "text", ledger: DeliveryLedger | None = None) -> None:
    """Send through the app bot when configured, otherwise use a webhook."""
    token = tenant_token(settings)
    targets = recipient_targets(recipients if recipients is not None else settings.recipients)
    if recipients is None:
        targets.extend(("open_id", value, value) for value in (settings.recipient_open_ids or []) if value)
    if settings.chat_id:
        targets.append(("chat_id", settings.chat_id, settings.chat_id))
    if token and targets:
        # A recipient may be present in both legacy and named settings. Keep
        # one delivery target per id so a single event cannot fan out twice.
        seen: set[tuple[str, str]] = set()
        errors: list[str] = []
        for receive_type, receive_id, _ in targets:
            if (receive_type, receive_id) in seen:
                continue
            seen.add((receive_type, receive_id))
            if ledger and session_id and not ledger.claim(session_id, receive_id, message_type):
                continue
            try:
                endpoint = message_url(receive_type, session_id, receive_id, message_type) if session_id else f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}"
                response = requests.post(endpoint, headers={"Authorization": f"Bearer {token}"}, json={"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}, timeout=30)
                result = feishu_response_data(response)
                if ledger and session_id:
                    ledger.finish(session_id, receive_id, message_type, result.get("data", {}).get("message_id", ""))
            except Exception as exc:
                if ledger and session_id:
                    ledger.finish(session_id, receive_id, message_type, error=str(exc))
                errors.append(f"{receive_id}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return
    feishu_text(settings.webhook, text)


def send_post(settings: Settings, content: dict[str, Any], fallback_text: str, *,
              recipients: list[dict[str, str]] | None = None, session_id: str = "",
              message_type: str = "post", ledger: DeliveryLedger | None = None) -> None:
    """Send a rich-text post so link labels do not inherit old Drive file names."""
    token = tenant_token(settings)
    targets = recipient_targets(recipients if recipients is not None else settings.recipients)
    if recipients is None:
        targets.extend(("open_id", value, value) for value in (settings.recipient_open_ids or []) if value)
    if settings.chat_id:
        targets.append(("chat_id", settings.chat_id, settings.chat_id))
    if not token or not targets:
        feishu_text(settings.webhook, fallback_text)
        return
    seen: set[tuple[str, str]] = set()
    errors: list[str] = []
    for receive_type, receive_id, _ in targets:
        if (receive_type, receive_id) in seen:
            continue
        seen.add((receive_type, receive_id))
        if ledger and session_id and not ledger.claim(session_id, receive_id, message_type):
            continue
        try:
            endpoint = message_url(receive_type, session_id, receive_id, message_type) if session_id else f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}"
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": receive_id, "msg_type": "post", "content": json.dumps(content, ensure_ascii=False)},
                timeout=30,
            )
            result = feishu_response_data(response)
            if ledger and session_id:
                ledger.finish(session_id, receive_id, message_type, result.get("data", {}).get("message_id", ""))
        except Exception as exc:
            if ledger and session_id:
                ledger.finish(session_id, receive_id, message_type, error=str(exc))
            errors.append(f"{receive_id}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))


def tenant_token(settings: Settings) -> str | None:
    if not settings.app_id or not settings.app_secret:
        return None
    response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.app_id, "app_secret": settings.app_secret}, timeout=20,
    )
    data = feishu_response_data(response)
    return data.get("tenant_access_token")


def app_access_token(settings: Settings) -> str:
    if not settings.app_id or not settings.app_secret:
        raise RuntimeError("Feishu application credentials are not configured")
    response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": settings.app_id, "app_secret": settings.app_secret}, timeout=20,
    )
    data = feishu_response_data(response)
    token = data.get("app_access_token")
    if not token:
        raise RuntimeError("Feishu did not return an app access token")
    return str(token)


def bitable_request(settings: Settings, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    token = tenant_token(settings)
    if not token or not settings.bitable_app_token:
        raise RuntimeError("Bitable credentials are not configured")
    response = requests.request(method, f"https://open.feishu.cn/open-apis{path}", headers={"Authorization": f"Bearer {token}"}, json=body, timeout=30)
    return feishu_response_data(response)


def user_token(settings: Settings) -> str:
    """Return the delegated token required by Feishu Minutes and Drive APIs."""
    if not settings.feishu_user_token_path:
        raise RuntimeError("Feishu user authorization token path is not configured")
    token_path = Path(settings.feishu_user_token_path)
    with _USER_TOKEN_LOCK:
        try:
            tokens = json.loads(token_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError("Feishu user authorization is missing") from exc
        expires_at = float(tokens.get("expires_at", 0))
        if tokens.get("access_token") and expires_at > time.time() + 90:
            return str(tokens["access_token"])
        refresh_token = tokens.get("refresh_token")
        if not refresh_token or not settings.app_id or not settings.app_secret:
            raise RuntimeError("Feishu user authorization needs offline_access and a new authorization")
        response = requests.post(
            "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token",
            json={"grant_type": "refresh_token", "refresh_token": refresh_token,
                  "app_access_token": app_access_token(settings)}, timeout=30,
        )
        data = feishu_response_data(response).get("data", {})
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError("Feishu did not return a refreshed user token")
        tokens.update(data)
        tokens["expires_at"] = time.time() + int(data.get("expires_in", 7200))
        temporary = token_path.with_suffix(token_path.suffix + ".tmp")
        temporary.write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(token_path)
        return str(access_token)


def user_feishu_request(settings: Settings, method: str, path: str, *, body: dict[str, Any] | None = None,
                        params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(
        method, f"https://open.feishu.cn/open-apis{path}", headers={"Authorization": f"Bearer {user_token(settings)}"},
        json=body, params=params, timeout=60,
    )
    return feishu_response_data(response)


def sync_accounts(settings: Settings) -> list[Account]:
    data = bitable_request(settings, "GET", f"/bitable/v1/apps/{settings.bitable_app_token}/tables/{settings.account_table_id}/records?page_size=500")
    accounts = []
    for item in data.get("data", {}).get("items", []):
        fields = item.get("fields", {})
        account_id = str(fields.get("抖音号", "")).strip()
        if not account_id:
            continue
        enabled = fields.get("监控开关") == "启用"
        url_value = fields.get("直播间链接")
        if isinstance(url_value, dict):
            url_value = url_value.get("link") or url_value.get("text")
        url = str(url_value or f"https://live.douyin.com/{account_id}")
        users = fields.get("监控接收人") or []
        recipients = [{"name": u.get("name", ""), "id_type": "open_id", "id": u.get("id", "")} for u in users if u.get("id")]
        accounts.append(Account(account_id, str(fields.get("监控账号", account_id)), url, enabled, recipients, item.get("record_id", item.get("id", ""))))
    return accounts


def update_account_state(settings: Settings, account: Account, *, status: str, started: int | None = None, ended: int | None = None, error: str | None = None) -> None:
    fields: dict[str, Any] = {"服务状态": status, "最后同步时间": int(time.time() * 1000)}
    if started:
        fields["最后开播时间"] = started
    if ended:
        fields["最后下播时间"] = ended
    if error:
        fields["服务状态"] = "异常"
    try:
        bitable_request(settings, "PUT", f"/bitable/v1/apps/{settings.bitable_app_token}/tables/{settings.account_table_id}/records/{account.table_record_id}", {"fields": fields})
    except Exception as exc:
        print(f"Account state update failed for {account.account_id}: {exc}", flush=True)


def create_live_record(settings: Settings, account: Account, session_id: str, title: str, started: int, recipients: list[dict[str, str]]) -> str:
    label = f"【{account.name}】{datetime.fromtimestamp(started / 1000).strftime('%Y%m%d_%H%M')}-进行中"
    fields = {"直播记录": label, "账号名称": account.name, "抖音号": account.account_id, "直播标题": title, "开播时间": started, "录制状态": "录制中", "转写状态": "待转写", "完成提醒状态": "待发送", "任务 ID": session_id}
    data = bitable_request(settings, "POST", f"/bitable/v1/apps/{settings.bitable_app_token}/tables/{settings.record_table_id}/records", {"fields": fields})
    return data.get("data", {}).get("record", {}).get("record_id", "")


def update_live_record(settings: Settings, record_id: str, fields: dict[str, Any]) -> None:
    if not record_id:
        return
    try:
        bitable_request(settings, "PUT", f"/bitable/v1/apps/{settings.bitable_app_token}/tables/{settings.record_table_id}/records/{record_id}", {"fields": fields})
    except Exception as exc:
        print(f"Live record update failed: {exc}", flush=True)


def attach_session_artifacts(settings: Settings, record_id: str, *, minutes_url: str = "",
                             transcript_url: str = "", summary_url: str = "") -> None:
    """Write the three Feishu-generated session links to the live record."""
    fields: dict[str, Any] = {}
    if minutes_url:
        fields["录制视频链接"] = {"link": minutes_url, "text": "打开录制视频"}
    if summary_url:
        fields["智能纪要链接"] = {"link": summary_url, "text": "打开智能纪要"}
    if transcript_url:
        fields["文字记录链接"] = {"link": transcript_url, "text": "打开文字记录"}
    if fields:
        update_live_record(settings, record_id, fields)


def concat_segments(segments: list[Path], output: Path) -> Path:
    """Join recorder segments into the one complete MP4 that gets archived."""
    if not segments:
        raise RuntimeError("No recording segments to merge")
    output.parent.mkdir(parents=True, exist_ok=True)
    if len(segments) == 1:
        if segments[0].resolve() != output.resolve():
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(segments[0]),
                            "-c", "copy", "-movflags", "+faststart", str(output)], check=True)
        return output
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".ffconcat", delete=False) as handle:
        concat_file = Path(handle.name)
        for segment in segments:
            # ffconcat requires single quotes to be escaped in file paths.
            handle.write(f"file '{str(segment.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
    try:
        fast_command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(concat_file), "-c", "copy", "-movflags", "+faststart", str(output)]
        copied = subprocess.run(fast_command, check=False)
        if copied.returncode != 0:
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                            "-i", str(concat_file), "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
                            str(output)], check=True)
    finally:
        concat_file.unlink(missing_ok=True)
    return output


def video_metadata(path: Path) -> VideoMetadata | None:
    """Return local byte size and playable duration, or None for an invalid video."""
    if not path.is_file() or path.stat().st_size < 1024:
        return None
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        text=True, capture_output=True, check=False,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    if result.returncode != 0 or duration <= 0:
        return None
    return VideoMetadata(path.stat().st_size, duration)


def stable_file_sizes(paths: list[Path], wait_seconds: float = 1.0) -> dict[Path, int]:
    """Return sizes only after proving that every file stopped changing."""
    before: dict[Path, os.stat_result] = {}
    for path in paths:
        try:
            before[path] = path.stat()
        except OSError as exc:
            raise RecordingIntegrityError(f"文件大小校验失败：无法读取 {path.name}") from exc
    if before and wait_seconds > 0:
        time.sleep(wait_seconds)
    sizes: dict[Path, int] = {}
    for path, initial in before.items():
        try:
            current = path.stat()
        except OSError as exc:
            raise RecordingIntegrityError(f"文件大小校验失败：无法再次读取 {path.name}") from exc
        if current.st_size != initial.st_size or current.st_mtime_ns != initial.st_mtime_ns:
            raise RecordingIntegrityError(
                f"文件大小校验失败：{path.name} 仍在写入（{initial.st_size} -> {current.st_size} 字节）"
            )
        sizes[path] = current.st_size
    return sizes


def inspect_recording_segments(segments: list[Path]) -> tuple[list[Path], list[VideoMetadata], list[Path]]:
    valid: list[Path] = []
    metadata: list[VideoMetadata] = []
    invalid: list[Path] = []
    try:
        stable_sizes = stable_file_sizes(segments)
    except RecordingIntegrityError:
        # A changing final segment means recording has not safely finished yet.
        raise
    for segment in segments:
        details = video_metadata(segment)
        if details is None or details.size_bytes != stable_sizes.get(segment):
            invalid.append(segment)
        else:
            valid.append(segment)
            metadata.append(details)
    return valid, metadata, invalid


def recording_integrity_result(metadata: list[VideoMetadata], invalid: list[Path],
                               expected_duration_seconds: float | None) -> tuple[str, str]:
    captured = sum(item.duration_seconds for item in metadata)
    notes: list[str] = []
    if invalid:
        notes.append("损坏分段：" + "、".join(path.name for path in invalid))
    if expected_duration_seconds and expected_duration_seconds > 0:
        tolerance = max(180.0, expected_duration_seconds * 0.05)
        if captured + tolerance < expected_duration_seconds:
            missing = max(0.0, expected_duration_seconds - captured)
            notes.append(
                f"有效录像 {captured / 60:.1f} 分钟，直播场次 {expected_duration_seconds / 60:.1f} 分钟，"
                f"缺失约 {missing / 60:.1f} 分钟"
            )
    return ("部分录制", "；".join(notes)) if notes else ("已完成", "")


def verify_merged_video(path: Path, segment_metadata: list[VideoMetadata]) -> VideoMetadata:
    stable_size = stable_file_sizes([path]).get(path, 0)
    merged = video_metadata(path)
    if merged is None or merged.size_bytes != stable_size:
        raise RecordingIntegrityError(f"上传前校验失败：合并录像不可读取或文件过小：{path.name}")
    expected_duration = sum(item.duration_seconds for item in segment_metadata)
    tolerance = max(5.0, len(segment_metadata) * 1.0)
    if expected_duration and abs(merged.duration_seconds - expected_duration) > tolerance:
        raise RecordingIntegrityError(
            f"上传前校验失败：合并录像时长 {merged.duration_seconds:.1f} 秒，"
            f"有效分段合计 {expected_duration:.1f} 秒"
        )
    return merged


def drive_download_size(settings: Settings, file_token: str) -> int:
    """Read the exact remote byte count without downloading the whole Drive file."""
    response = requests.get(
        f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}/download",
        headers={"Authorization": f"Bearer {user_token(settings)}", "Range": "bytes=0-0"},
        stream=True, timeout=60,
    )
    try:
        response.raise_for_status()
        content_range = response.headers.get("Content-Range", "")
        if response.status_code != 206 or "/" not in content_range:
            raise RecordingIntegrityError("飞书未返回可校验的录像文件大小")
        return int(content_range.rsplit("/", 1)[-1])
    except ValueError as exc:
        raise RecordingIntegrityError("飞书返回了无效的录像文件大小") from exc
    finally:
        response.close()


def verify_drive_file_size(settings: Settings, file_token: str, expected_size: int) -> None:
    remote_size = drive_download_size(settings, file_token)
    if remote_size != expected_size:
        raise RecordingIntegrityError(
            f"飞书上传校验失败：本地 {expected_size} 字节，云端 {remote_size} 字节；保留服务器录像"
        )


def cleanup_uploaded_recordings(segments: list[Path], complete_video: Path, verified_size: int) -> None:
    """Delete local files only after the remote byte count has been verified."""
    if not complete_video.exists() and not any(path.exists() for path in segments):
        return
    current_size = stable_file_sizes([complete_video]).get(complete_video, 0) if complete_video.exists() else 0
    if complete_video.exists() and current_size != verified_size:
        raise RecordingIntegrityError(
            f"删除前校验失败：当前合并录像 {current_size} 字节，"
            f"已验证上传录像 {verified_size} 字节"
        )
    if not complete_video.exists() and any(path.exists() for path in segments):
        raise RecordingIntegrityError("删除前校验失败：合并录像不存在，保留原始分段")
    for path in [*segments, complete_video]:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Uploaded recording cleanup failed for {path}: {exc}", flush=True)


def video_is_readable(path: Path) -> bool:
    return video_metadata(path) is not None


def ensure_merge_space(segments: list[Path], output_dir: Path, reserve_bytes: int = 512 * 1024 * 1024) -> None:
    required = sum(path.stat().st_size for path in segments) + reserve_bytes
    available = shutil.disk_usage(output_dir).free
    if available < required:
        raise LowDiskSpaceError(
            f"合并录像所需空间不足：需要约 {required / 1024**3:.1f} GB，当前可用 {available / 1024**3:.1f} GB；等待其他已上传场次清理后重试"
        )


def drive_list(settings: Settings, folder_token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 200}
        if folder_token:
            params["folder_token"] = folder_token
        if page_token:
            params["page_token"] = page_token
        data = user_feishu_request(settings, "GET", "/drive/v1/files", params=params).get("data", {})
        items.extend(data.get("files", []))
        if not data.get("has_more"):
            return items
        page_token = data.get("next_page_token", "")
        if not page_token:
            return items


def ensure_drive_folder(settings: Settings, parent_token: str, name: str) -> str:
    for item in drive_list(settings, parent_token):
        if item.get("name") == name and item.get("type") == "folder":
            return str(item.get("token"))
    data = user_feishu_request(settings, "POST", "/drive/v1/files/create_folder",
                               body={"name": name, "folder_token": parent_token})
    token = data.get("data", {}).get("token")
    if not token:
        raise RuntimeError(f"Could not create Feishu folder: {name}")
    return str(token)


def session_drive_folder(settings: Settings, account_name: str) -> str:
    if not settings.drive_root_folder_token:
        raise RuntimeError("Feishu Drive root folder token is not configured")
    platform = ensure_drive_folder(settings, settings.drive_root_folder_token, settings.drive_platform_folder_name)
    account = ensure_drive_folder(settings, platform, account_name)
    return ensure_drive_folder(settings, account, "直播录像")


def drive_file_url(file_token: str) -> str:
    return f"https://shenyidushu.feishu.cn/file/{file_token}"


def _upload_checkpoint_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.feishu-upload.json")


def _write_upload_checkpoint(checkpoint_path: Path, checkpoint: dict[str, Any]) -> None:
    temporary = checkpoint_path.with_suffix(f"{checkpoint_path.suffix}.tmp")
    temporary.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
    temporary.replace(checkpoint_path)


def upload_drive_file(settings: Settings, path: Path, folder_token: str) -> str:
    """Upload a file with a durable per-part checkpoint for interrupted transfers."""
    if not path.is_file():
        raise RuntimeError(f"Artifact does not exist: {path}")
    stable_file_sizes([path])
    initial_stat = path.stat()
    checkpoint_path = _upload_checkpoint_path(path)
    checkpoint: dict[str, Any] = {}
    if checkpoint_path.is_file():
        try:
            candidate = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if (
                candidate.get("file_size") == initial_stat.st_size
                and candidate.get("file_mtime_ns") == initial_stat.st_mtime_ns
                and candidate.get("folder_token") == folder_token
            ):
                checkpoint = candidate
        except (OSError, ValueError, TypeError):
            checkpoint = {}
    if not checkpoint:
        prepared = user_feishu_request(settings, "POST", "/drive/v1/files/upload_prepare", body={
            "file_name": path.name, "parent_type": "explorer", "parent_node": folder_token,
            "size": initial_stat.st_size,
        }).get("data", {})
        upload_id = prepared.get("upload_id")
        block_size = int(prepared.get("block_size", 0))
        block_num = int(prepared.get("block_num", 0))
        if not upload_id or block_size <= 0 or block_num <= 0:
            raise RuntimeError("Feishu did not prepare the file upload")
        checkpoint = {
            "upload_id": str(upload_id), "block_size": block_size, "block_num": block_num,
            "file_size": initial_stat.st_size, "file_mtime_ns": initial_stat.st_mtime_ns,
            "folder_token": folder_token,
            "completed_parts": [],
        }
        _write_upload_checkpoint(checkpoint_path, checkpoint)
    upload_id = str(checkpoint["upload_id"])
    block_size = int(checkpoint["block_size"])
    block_num = int(checkpoint["block_num"])
    completed_parts = {int(sequence) for sequence in checkpoint.get("completed_parts", [])}
    checkpoint_lock = threading.Lock()
    token = user_token(settings)

    def upload_part(sequence: int) -> None:
        with path.open("rb") as source:
            source.seek(sequence * block_size)
            block = source.read(block_size)
        if not block:
            raise RuntimeError("Recording ended before all upload blocks were read")
        for attempt in range(1, 6):
            try:
                response = requests.post(
                    "https://open.feishu.cn/open-apis/drive/v1/files/upload_part",
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "upload_id": upload_id,
                        "seq": str(sequence),
                        "size": str(len(block)),
                        "checksum": str(zlib.adler32(block) & 0xFFFFFFFF),
                    },
                    files={"file": (path.name, block)}, timeout=300,
                )
                feishu_response_data(response)
                with checkpoint_lock:
                    completed_parts.add(sequence)
                    checkpoint["completed_parts"] = sorted(completed_parts)
                    _write_upload_checkpoint(checkpoint_path, checkpoint)
                return
            except requests.RequestException:
                if attempt == 5:
                    raise
                time.sleep(min(30, 2 ** attempt))

    pending_parts = [sequence for sequence in range(block_num) if sequence not in completed_parts]
    # A large recording is split into thousands of parts; a small bounded
    # worker pool keeps uploads moving without creating an unbounded request
    # burst against Feishu.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(pending_parts) or 1)) as executor:
        list(executor.map(upload_part, pending_parts))
    current_stat = path.stat()
    if (
        current_stat.st_size != initial_stat.st_size
        or current_stat.st_mtime_ns != initial_stat.st_mtime_ns
    ):
        raise RecordingIntegrityError(
            f"上传前后校验失败：{path.name} 在上传期间发生变化；保留服务器录像"
        )
    file_token = checkpoint.get("file_token")
    if not file_token:
        finished = user_feishu_request(settings, "POST", "/drive/v1/files/upload_finish",
                                       body={"upload_id": upload_id, "block_num": block_num}).get("data", {})
        file_token = finished.get("file_token")
        if file_token:
            # Keep the cloud token until byte verification succeeds. A retry can
            # then verify this exact file instead of finishing or uploading twice.
            checkpoint["file_token"] = str(file_token)
            _write_upload_checkpoint(checkpoint_path, checkpoint)
    if not file_token:
        raise RuntimeError("Feishu did not return the uploaded file token")
    # Upload_finish proves all declared blocks arrived. The ranged download
    # additionally proves that the assembled cloud file has the exact local size.
    verify_drive_file_size(settings, str(file_token), initial_stat.st_size)
    checkpoint_path.unlink(missing_ok=True)
    return str(file_token)


def upload_minutes(settings: Settings, file_token: str) -> tuple[str, str]:
    data = user_feishu_request(settings, "POST", "/minutes/v1/minutes/upload", body={"file_token": file_token}).get("data", {})
    url = data.get("minute_url")
    token = data.get("minute_token") or (str(url).rstrip("/").rsplit("/", 1)[-1] if url else "")
    if not token or not url:
        raise RuntimeError("Feishu did not create a Minutes record for this video")
    return str(token), str(url)


def minutes_detail(settings: Settings, minute_token: str) -> dict[str, Any]:
    data = user_feishu_request(settings, "GET", f"/minutes/v1/minutes/{minute_token}").get("data", {})
    minute = data.get("minute", {})
    if not minute.get("title") or not minute.get("create_time"):
        raise RuntimeError("Feishu Minutes did not return title and creation time")
    return minute


def find_minutes_documents(files: list[dict[str, Any]], *, title: str,
                           created_at_ms: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Match only the two DOCX files automatically generated for this Minutes item."""
    created_at_seconds = created_at_ms // 1000

    def newest(prefix: str) -> dict[str, Any] | None:
        matches = []
        for item in files:
            try:
                created_time = int(item.get("created_time", 0))
            except (TypeError, ValueError):
                continue
            if (
                item.get("type") == "docx"
                and str(item.get("name", "")).startswith(f"{prefix}{title}")
                and created_time >= created_at_seconds
                and item.get("url")
            ):
                matches.append(item)
        return max(matches, key=lambda item: int(item.get("created_time", 0)), default=None)

    return newest("文字记录："), newest("智能纪要：")


def wait_for_minutes_documents(settings: Settings, minute_token: str) -> dict[str, Any]:
    """Wait until Feishu has produced both its transcript and smart-summary documents."""
    deadline = time.monotonic() + settings.minutes_timeout_seconds
    minute: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if minute is None:
            minute = minutes_detail(settings, minute_token)
        transcript, summary = find_minutes_documents(
            drive_list(settings, ""), title=str(minute["title"]), created_at_ms=int(minute["create_time"]),
        )
        if transcript and summary:
            return {
                "minutes_url": str(minute.get("url", "")),
                "minutes_title": str(minute["title"]),
                "minutes_created_at": int(minute["create_time"]),
                "transcript_url": str(transcript["url"]),
                "summary_url": str(summary["url"]),
            }
        time.sleep(settings.minutes_poll_seconds)
    raise RuntimeError("Feishu Minutes document generation timed out")


def upload_file(settings: Settings, path: Path) -> str | None:
    """Upload a message file. Feishu limits this endpoint to 30 MB."""
    token = tenant_token(settings)
    if not token:
        return None
    with path.open("rb") as video:
        response = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/files",
            headers={"Authorization": f"Bearer {token}"},
            # `file_size` is not accepted by this endpoint and turns an
            # otherwise valid upload into a 400 business error.
            data={"file_type": "stream", "file_name": path.name},
            files={"file": (path.name, video)}, timeout=300,
        )
    return feishu_response_data(response).get("data", {}).get("file_key")


def upload_image(settings: Settings, path: Path) -> str | None:
    """Upload a JPG screenshot for an inline Feishu image message."""
    token = tenant_token(settings)
    if not token:
        return None
    with path.open("rb") as image:
        response = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": (path.name, image, "image/jpeg")}, timeout=60,
        )
    return feishu_response_data(response).get("data", {}).get("image_key")


def feishu_file(settings: Settings, file_key: str, text: str, *, recipients: list[dict[str, str]] | None = None, session_id: str = "", ledger: DeliveryLedger | None = None) -> None:
    token = tenant_token(settings)
    targets = recipient_targets(recipients if recipients is not None else settings.recipients)
    if recipients is None:
        targets.extend(("open_id", value, value) for value in (settings.recipient_open_ids or []) if value)
    if settings.chat_id:
        targets.append(("chat_id", settings.chat_id, settings.chat_id))
    if not token or not targets:
        send_text(settings, text)
        return
    seen: set[tuple[str, str]] = set()
    for receive_type, receive_id, _ in targets:
        if (receive_type, receive_id) in seen:
            continue
        seen.add((receive_type, receive_id))
        if ledger and session_id and not ledger.claim(session_id, receive_id, "transcript"):
            continue
        try:
            response = requests.post(
            message_url(receive_type, session_id, receive_id, "transcript") if session_id else f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}",
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": receive_id, "msg_type": "file", "content": json.dumps({"file_key": file_key})},
            timeout=30,
        )
            result = feishu_response_data(response)
            if ledger and session_id:
                ledger.finish(session_id, receive_id, "transcript", result.get("data", {}).get("message_id", ""))
        except Exception as exc:
            if ledger and session_id:
                ledger.finish(session_id, receive_id, "transcript", error=str(exc))
            raise
    # The attachment itself is the transcript delivery. Do not call
    # send_text here: that used to create a duplicate notification per user.


def feishu_image(settings: Settings, image_key: str, *, recipients: list[dict[str, str]] | None = None, session_id: str = "", ledger: DeliveryLedger | None = None) -> None:
    token = tenant_token(settings)
    targets = recipient_targets(recipients if recipients is not None else settings.recipients)
    if recipients is None:
        targets.extend(("open_id", value, value) for value in (settings.recipient_open_ids or []) if value)
    if settings.chat_id:
        targets.append(("chat_id", settings.chat_id, settings.chat_id))
    if not token or not targets:
        return
    seen: set[tuple[str, str]] = set()
    for receive_type, receive_id, _ in targets:
        if (receive_type, receive_id) in seen:
            continue
        seen.add((receive_type, receive_id))
        if ledger and session_id and not ledger.claim(session_id, receive_id, "screenshot"):
            continue
        try:
            response = requests.post(
            message_url(receive_type, session_id, receive_id, "screenshot") if session_id else f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}",
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": receive_id, "msg_type": "image",
                  "content": json.dumps({"image_key": image_key})},
            timeout=30,
        )
            result = feishu_response_data(response)
            if ledger and session_id:
                ledger.finish(session_id, receive_id, "screenshot", result.get("data", {}).get("message_id", ""))
        except Exception as exc:
            if ledger and session_id:
                ledger.finish(session_id, receive_id, "screenshot", error=str(exc))
            raise


def artifact_timestamp(session_id: str) -> str:
    """Make the session start time readable while keeping it filename-safe."""
    try:
        return datetime.strptime(session_id, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return session_id


def artifact_path(directory: Path, kind: str, account_id: str, session_id: str, suffix: str) -> Path:
    return directory / f"{kind}-{account_id}-{artifact_timestamp(session_id)}{suffix}"


def recording_complete_message(account_name: str, session_id: str, minutes_url: str, transcript_url: str,
                               summary_url: str) -> str:
    """Build the single post-processing notification required for a live session."""
    try:
        started = datetime.strptime(session_id, "%Y%m%d_%H%M%S")
        started_label = f"{started.year}年{started.month}月{started.day}日 {started.hour}点{started.minute}"
    except ValueError:
        started_label = session_id
    return (
        "【直播录制完成提醒】\n"
        f"“{account_name}”在“{started_label}”的直播录制已完成，请查收。\n"
        f"1.录制视频：\n{minutes_url}\n"
        f"2.文字记录：\n{transcript_url}\n"
        f"3.智能纪要：\n{summary_url}"
    )


def recording_complete_post(account_name: str, session_id: str, minutes_url: str, transcript_url: str,
                            summary_url: str) -> dict[str, Any]:
    """Build the numbered completion message with the three Feishu-generated assets."""
    try:
        started = datetime.strptime(session_id, "%Y%m%d_%H%M%S")
        started_label = f"{started.year}年{started.month}月{started.day}日 {started.hour}点{started.minute}"
    except ValueError:
        started_label = session_id
    return {"zh_cn": {"title": "", "content": [
        [{"tag": "text", "text": "【直播录制完成提醒】"}],
        [{"tag": "text", "text": f"“{account_name}”在“{started_label}”的直播录制已完成，请查收。"}],
        [{"tag": "text", "text": "1.录制视频："}],
        [{"tag": "a", "text": minutes_url, "href": minutes_url}],
        [{"tag": "text", "text": "2.文字记录："}],
        [{"tag": "a", "text": transcript_url, "href": transcript_url}],
        [{"tag": "text", "text": "3.智能纪要："}],
        [{"tag": "a", "text": summary_url, "href": summary_url}],
    ]}}


def publish_finished_session(
    settings: Settings, *, account_name: str, session_id: str, minutes_url: str, transcript_url: str,
    summary_url: str, recipients: list[dict[str, str]], ledger: DeliveryLedger,
) -> None:
    """Send one completion message, with all finished-session assets as links."""
    send_post(
        settings,
        recording_complete_post(account_name, session_id, minutes_url, transcript_url, summary_url),
        recording_complete_message(account_name, session_id, minutes_url, transcript_url, summary_url),
        recipients=recipients,
        session_id=session_id,
        message_type="recording_complete",
        ledger=ledger,
    )


def complete_with_feishu_minutes(
    settings: Settings, *, room_dir: Path, segments: list[Path], account_name: str, session_id: str,
    record_id: str, title: str, url: str, recipients: list[dict[str, str]], ledger: DeliveryLedger,
    expected_duration_seconds: float | None = None,
) -> None:
    """Create one complete video, archive it, transcribe it in Minutes, then publish once."""
    complete_video = artifact_path(room_dir, "直播视频", account_name, session_id, "_00.mp4")
    artifacts = ledger.session_artifacts(session_id) or {}
    valid_segments, segment_metadata, invalid_segments = inspect_recording_segments(segments)
    recording_status = str(artifacts.get("recording_status") or "已完成")
    integrity_note = str(artifacts.get("integrity_note") or "")
    if segments:
        if not valid_segments:
            raise RecordingIntegrityError("录制结束校验失败：没有可读取的录像分段")
        recording_status, integrity_note = recording_integrity_result(
            segment_metadata, invalid_segments, expected_duration_seconds,
        )
        ledger.save_session_artifacts(
            session_id, recording_status=recording_status, integrity_note=integrity_note,
        )
    archive_video_url = artifacts.get("archive_video_url", "")
    archive_video_size = int(artifacts.get("archive_video_size") or 0)
    video_name = artifacts.get("video_name", "")
    if not archive_video_url:
        with _VIDEO_ARCHIVE_LOCK:
            # Another recovery worker may have completed the upload while this
            # task waited for the bounded disk-intensive archive section.
            artifacts = ledger.session_artifacts(session_id) or {}
            archive_video_url = artifacts.get("archive_video_url", "")
            archive_video_size = int(artifacts.get("archive_video_size") or 0)
            video_name = artifacts.get("video_name", "")
            if not archive_video_url:
                if not video_is_readable(complete_video):
                    ensure_merge_space(valid_segments, room_dir)
                    concat_segments(valid_segments, complete_video)
                merged_metadata = verify_merged_video(complete_video, segment_metadata)
                update_live_record(settings, record_id, {
                    "录制状态": recording_status, "转写状态": "转写中", "失败原因": integrity_note,
                })
                archive_folder = session_drive_folder(settings, account_name)
                video_token = upload_drive_file(settings, complete_video, archive_folder)
                archive_video_size = merged_metadata.size_bytes
                archive_video_url = drive_file_url(video_token)
                video_name = complete_video.name
                ledger.save_uploaded_video(
                    session_id, archive_video_url=archive_video_url, archive_video_size=archive_video_size,
                    video_name=video_name,
                )
    video_token = archive_video_url.rstrip("/").rsplit("/", 1)[-1]
    if archive_video_size <= 0:
        if complete_video.exists():
            archive_video_size = complete_video.stat().st_size
            verify_drive_file_size(settings, video_token, archive_video_size)
            ledger.save_session_artifacts(session_id, archive_video_size=archive_video_size)
        elif segments:
            raise RecordingIntegrityError("删除前校验失败：缺少已上传录像的本地大小凭证，保留原始分段")
    else:
        verify_drive_file_size(settings, video_token, archive_video_size)
    cleanup_uploaded_recordings(segments, complete_video, archive_video_size)

    artifacts = ledger.session_artifacts(session_id) or artifacts
    transcript_url = artifacts.get("transcript_url", "")
    summary_url = artifacts.get("summary_url", "")
    minutes_url = artifacts.get("minutes_url", "")
    if not transcript_url or not summary_url:
        if minutes_url:
            minute_token = minutes_url.rstrip("/").rsplit("/", 1)[-1]
        else:
            claimed, submitted_url = ledger.claim_minutes_submission(session_id)
            if submitted_url:
                minutes_url = submitted_url
                minute_token = minutes_url.rstrip("/").rsplit("/", 1)[-1]
            elif not claimed:
                raise RuntimeError(
                    "该场次的飞书妙记创建请求已在执行中；为防止重复创建，本次任务不会再次提交"
                )
            else:
                video_token = archive_video_url.rstrip("/").rsplit("/", 1)[-1]
                try:
                    minute_token, minutes_url = upload_minutes(settings, video_token)
                except Exception as exc:
                    ledger.finish_minutes_submission(session_id, error=str(exc))
                    raise
                # Persist the returned URL in the submission ledger first. If
                # later processing crashes, a recovery worker reuses this URL.
                ledger.finish_minutes_submission(session_id, minutes_url=minutes_url)
            ledger.save_session_artifacts(
                session_id, minutes_url=minutes_url,
            )
        minutes_artifacts = wait_for_minutes_documents(settings, minute_token)
        minutes_url = str(minutes_artifacts["minutes_url"] or minutes_url)
        transcript_url = str(minutes_artifacts["transcript_url"])
        summary_url = str(minutes_artifacts["summary_url"])
        ledger.save_session_artifacts(session_id, **minutes_artifacts)
    attach_session_artifacts(
        settings, record_id, minutes_url=minutes_url, transcript_url=transcript_url, summary_url=summary_url,
    )
    update_live_record(settings, record_id, {
        "录制状态": recording_status, "转写状态": "已完成", "完成提醒状态": "发送中",
        "失败原因": integrity_note,
    })
    try:
        publish_finished_session(
            settings, account_name=account_name, session_id=session_id, minutes_url=minutes_url,
            transcript_url=transcript_url, summary_url=summary_url,
            recipients=recipients, ledger=ledger,
        )
    except Exception as exc:
        update_live_record(settings, record_id, {
            "录制状态": recording_status, "转写状态": "已完成", "完成提醒状态": "发送失败",
            "失败原因": (integrity_note + "；" if integrity_note else "") + str(exc)[:1000],
        })
        raise CompletionNotificationError(str(exc)) from exc
    update_live_record(settings, record_id, {
        "录制状态": recording_status, "转写状态": "已完成", "完成提醒状态": "已发送",
        "完成提醒时间": int(time.time() * 1000), "失败原因": integrity_note,
    })


def transcribe(path: Path, settings: Settings) -> str:
    """Use openai-whisper's CLI; output is kept beside the media for recovery."""
    out_dir = path.parent / "transcripts"
    out_dir.mkdir(exist_ok=True)
    stem = out_dir / path.stem
    txt = stem.with_suffix(".txt")
    if txt.exists():
        return txt.read_text(encoding="utf-8")
    wav = path.with_suffix(".wav")
    subprocess.run(["ffmpeg", "-y", "-i", str(path), "-vn", "-ar", "16000", "-ac", "1", str(wav)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["whisper", str(wav), "--model", settings.whisper_model, "--language",
                    settings.whisper_language, "--output_format", "txt", "--output_dir", str(out_dir)], check=True)
    wav.unlink(missing_ok=True)
    return txt.read_text(encoding="utf-8") if txt.exists() else ""


def stream_info(settings: Settings, url: str) -> dict[str, Any]:
    import sys
    sys.path.insert(0, str(settings.recorder_root))
    from src import spider, stream  # type: ignore
    data = asyncio.run(spider.get_douyin_web_stream_data(url, proxy_addr=settings.proxy or None,
                                                         cookies=settings.cookie or None))
    return asyncio.run(stream.get_douyin_stream_url(data, "OD", settings.proxy or None))


def start_recorder(
    settings: Settings, room_dir: Path, account_id: str, session_stamp: str, record_url: str,
) -> subprocess.Popen[bytes]:
    """Start or resume a segmented recording without overwriting existing segments."""
    video_base = artifact_path(room_dir, "直播视频", account_id, session_stamp, "")
    existing = [segment for segment in room_dir.glob(f"{video_base.name}_*.mp4")
                if len(segment.stem.rsplit("_", 1)[-1]) == 3 and segment.stem.rsplit("_", 1)[-1].isdigit()]
    indices = []
    for segment in existing:
        try:
            indices.append(int(segment.stem.rsplit("_", 1)[-1]))
        except ValueError:
            continue
    next_index = max(indices, default=-1) + 1
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", record_url,
        "-c", "copy", "-f", "segment", "-segment_time", str(settings.segment_seconds),
        "-segment_format", "mp4", "-segment_start_number", str(next_index),
        "-reset_timestamps", "1", "-movflags", "+faststart", f"{video_base}_%03d.mp4",
    ]
    return subprocess.Popen(command)


def session_segments(room_dir: Path, account_name: str, session_id: str) -> list[Path]:
    """Return only temporary recorder segments, never the final merged MP4."""
    prefix = f"直播视频-{account_name}-{artifact_timestamp(session_id)}_"
    return [segment for segment in sorted(room_dir.glob(f"{prefix}*.mp4"))
            if segment.stat().st_size >= 1024
            and len(segment.stem.rsplit("_", 1)[-1]) == 3
            and segment.stem.rsplit("_", 1)[-1].isdigit()]


def inferred_recording_end_ms(segments: list[Path]) -> int | None:
    """Use the newest completed segment as a recovery end-time hint."""
    if not segments:
        return None
    return int(max(segment.stat().st_mtime for segment in segments) * 1000)


def run_room(settings: Settings, account_id: str, registry: dict[str, Account], registry_lock: threading.Lock, ledger: DeliveryLedger) -> None:
    room_dir = settings.output_dir / account_id
    room_dir.mkdir(parents=True, exist_ok=True)
    active = False
    process: subprocess.Popen[bytes] | None = None
    session_stamp: str | None = None
    session_snapshot: dict[str, Any] | None = ledger.active_session(account_id)
    if session_snapshot:
        active = True
        session_stamp = session_snapshot["session_id"]
    print(f"Monitoring {account_id} every {settings.poll_seconds}s", flush=True)
    while True:
        try:
            with registry_lock:
                account = registry.get(account_id)
            if not account:
                time.sleep(settings.poll_seconds)
                continue
            url = account.room_url
            if not account.enabled and not active:
                update_account_state(settings, account, status="未使用")
                time.sleep(settings.poll_seconds)
                continue
            info = stream_info(settings, url)
            live = bool(info.get("is_live") and info.get("record_url"))
            if live and active and process and process.poll() is None:
                # A restarted recorder has survived one polling interval, so
                # a previous process exit was transient rather than the end
                # of the stream.
                ledger.clear_recorder_stop(account_id)
            if live and not active:
                active = True
                session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                started_ms = int(time.time() * 1000)
                if not ledger.start_session(account_id, session_stamp, account.name, account.recipients, "", started_ms):
                    active, session_stamp = False, None
                    time.sleep(settings.poll_seconds)
                    continue
                process = start_recorder(settings, room_dir, account.name, session_stamp, info["record_url"])
                record_id = create_live_record(settings, account, session_stamp, info.get("title", ""), started_ms, account.recipients)
                ledger.set_session_record_id(account_id, record_id)
                session_snapshot = {"account_name": account.name, "recipients": account.recipients, "record_id": record_id, "started_ms": started_ms, "ended_ms": None}
                update_account_state(settings, account, status="正常使用", started=started_ms)
                send_text(settings, f"【开播】{account.name}\n{info.get('title', '')}\n{url}", recipients=account.recipients, session_id=session_stamp, message_type="live_start", ledger=ledger)
                print(f"Live started: {url} ({session_stamp})", flush=True)
            elif live and active and process and process.poll() is not None and not (session_snapshot or {}).get("ended_ms"):
                exit_code = process.returncode
                ledger.record_recorder_stop(account_id, int(time.time() * 1000))
                process = start_recorder(
                    settings, room_dir, account.name, session_stamp or datetime.now().strftime("%Y%m%d_%H%M%S"),
                    info["record_url"],
                )
                print(f"Recorder restarted: {url}; previous exit code {exit_code}", flush=True)
            elif live and active and process is None and not (session_snapshot or {}).get("ended_ms"):
                snap_name = (session_snapshot or {}).get("account_name", account.name)
                process = start_recorder(settings, room_dir, snap_name, session_stamp or datetime.now().strftime("%Y%m%d_%H%M%S"), info["record_url"])
            if active and not live:
                if process:
                    process.terminate()
                    process.wait(timeout=30)
                snap_name = (session_snapshot or {}).get("account_name", account.name)
                snap_recipients = (session_snapshot or {}).get("recipients", account.recipients)
                record_id = (session_snapshot or {}).get("record_id", "")
                started_ms = (session_snapshot or {}).get("started_ms", int(time.time() * 1000))
                segments = session_segments(room_dir, snap_name, session_stamp or "unknown")
                # Record the stream end before any merge/upload/transcription
                # work. A retry may run hours later, but must keep this value.
                end_hint = (session_snapshot or {}).get("ended_ms") or inferred_recording_end_ms(segments)
                ended_ms = ledger.record_session_end(account_id, int(end_hint or time.time() * 1000))
                if session_snapshot is not None:
                    session_snapshot["ended_ms"] = ended_ms
                if settings.transcription_mode == "local_pull":
                    manifest = room_dir / f"{session_stamp}_pending_transcription.json"
                    manifest.write_text(json.dumps({
                        "session_id": session_stamp,
                        "account_id": account_id,
                        "account_name": snap_name,
                        "recipient_snapshot": snap_recipients,
                        "record_id": record_id,
                        "url": url,
                        "anchor_name": account.name,
                        "title": info.get("title", ""),
                        "segments": [segment.name for segment in segments],
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    update_live_record(settings, record_id, {"直播记录": f"【{snap_name}】{datetime.fromtimestamp(started_ms / 1000).strftime('%Y%m%d_%H%M')}-{datetime.fromtimestamp(ended_ms / 1000).strftime('%H%M')}", "下播时间": ended_ms, "直播时长（分钟）": max(0, int((ended_ms - started_ms) / 60000)), "录制状态": "已完成" if segments else "录制失败", "转写状态": "待转写" if segments else "转写失败", "完成提醒状态": "待发送" if segments else "无需发送"})
                    ledger.end_session(account_id)
                    update_account_state(settings, account, status="正常使用", ended=ended_ms)
                    print(f"Local transcription queued: {manifest}", flush=True)
                    active, process, session_stamp = False, None, None
                    time.sleep(settings.poll_seconds)
                    continue
                if settings.transcription_mode == "feishu_minutes":
                    update_live_record(settings, record_id, {
                        "直播记录": f"【{snap_name}】{datetime.fromtimestamp(started_ms / 1000).strftime('%Y%m%d_%H%M')}-{datetime.fromtimestamp(ended_ms / 1000).strftime('%H%M')}",
                        "下播时间": ended_ms, "直播时长（分钟）": max(0, int((ended_ms - started_ms) / 60000)),
                        "录制状态": "已完成" if segments else "录制失败", "转写状态": "转写中" if segments else "转写失败",
                    })
                    if not segments:
                        update_live_record(settings, record_id, {"失败原因": "下播后未发现有效录像分段", "完成提醒状态": "无需发送"})
                    else:
                        try:
                            complete_with_feishu_minutes(
                                settings, room_dir=room_dir, segments=segments, account_name=snap_name,
                                session_id=session_stamp or "unknown", record_id=record_id,
                                title=info.get("title", ""), url=url, recipients=snap_recipients, ledger=ledger,
                                expected_duration_seconds=max(0.0, (ended_ms - started_ms) / 1000),
                            )
                        except CompletionNotificationError:
                            raise
                        except LowDiskSpaceError as exc:
                            update_live_record(settings, record_id, {
                                "转写状态": "待转写", "完成提醒状态": "待发送", "失败原因": str(exc)[:1000],
                            })
                            raise
                        except Exception as exc:
                            update_live_record(settings, record_id, {
                                "转写状态": "转写失败", "完成提醒状态": "无需发送", "失败原因": str(exc)[:1000],
                            })
                            raise
                    ledger.end_session(account_id)
                    update_account_state(settings, account, status="正常使用", ended=ended_ms)
                    print(f"Feishu Minutes processing finished: {url}; {len(segments)} segments", flush=True)
                    active, process, session_stamp, session_snapshot = False, None, None, None
                    time.sleep(settings.poll_seconds)
                    continue
                transcripts = []
                for segment in segments:
                    transcripts.append(transcribe(segment, settings))
                full = "\n\n".join(text for text in transcripts if text)
                full_path = artifact_path(room_dir, "直播逐字稿", account.name, session_stamp or "unknown", ".txt")
                full_path.write_text(full, encoding="utf-8")
                first_segment = segments[0] if segments else None
                if first_segment:
                    publish_finished_session(
                        settings, account_name=snap_name, session_id=session_stamp or "unknown",
                        minutes_url=str(first_segment), transcript_url=str(full_path), summary_url="",
                        recipients=snap_recipients, ledger=ledger,
                    )
                update_live_record(settings, record_id, {"录制状态": "已完成", "转写状态": "已完成", "完成提醒状态": "已发送", "完成提醒时间": int(time.time() * 1000)})
                print(f"Live finished: {url}; {len(segments)} segments, {len(full)} chars", flush=True)
                active, process, session_stamp, session_snapshot = False, None, None, None
            time.sleep(settings.poll_seconds)
        except Exception as exc:
            print(f"{account_id}: {exc}", flush=True)
            time.sleep(settings.poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="live_digest.json")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    settings = Settings(recorder_root=Path(cfg["recorder_root"]), output_dir=Path(cfg.get("output_dir", "./recordings")),
                        webhook=cfg.get("feishu_webhook", ""), app_id=cfg.get("feishu_app_id", ""),
                        app_secret=cfg.get("feishu_app_secret", ""), chat_id=cfg.get("feishu_chat_id", ""),
                        recipient_open_ids=cfg.get("feishu_open_ids", []),
                        recipients=cfg.get("feishu_recipients", []),
                        poll_seconds=int(cfg.get("poll_seconds", 60)),
                        segment_seconds=int(cfg.get("segment_seconds", 900)),
                        whisper_model=cfg.get("whisper_model", "small"),
                        whisper_language=cfg.get("whisper_language", "zh"), proxy=cfg.get("proxy", ""),
                        transcription_mode=cfg.get("transcription_mode", "server"),
                        cookie=cfg.get("douyin_cookie", ""), bitable_app_token=cfg.get("bitable_app_token", ""),
                        account_table_id=cfg.get("account_table_id", ""), record_table_id=cfg.get("record_table_id", ""),
                        config_poll_seconds=int(cfg.get("config_poll_seconds", 60)), state_db=cfg.get("state_db", "./monitor_state.sqlite3"),
                        feishu_user_token_path=cfg.get("feishu_user_token_path", ""),
                        drive_root_folder_token=cfg.get("drive_root_folder_token", ""),
                        drive_platform_folder_name=cfg.get("drive_platform_folder_name", "抖音"),
                        minutes_poll_seconds=int(cfg.get("minutes_poll_seconds", 60)),
                        minutes_timeout_seconds=int(cfg.get("minutes_timeout_seconds", 7200)))
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    registry: dict[str, Account] = {}
    registry_lock = threading.Lock()
    ledger = DeliveryLedger(Path(settings.state_db))
    if settings.bitable_app_token and settings.account_table_id:
        for account in sync_accounts(settings):
            registry[account.account_id] = account
    else:
        for url in cfg.get("rooms", []):
            account_id = url.rstrip("/").split("/")[-1]
            registry[account_id] = Account(account_id, account_id, url, True, settings.recipients or [])

    started: set[str] = set()
    threads: list[threading.Thread] = []
    stop = threading.Event()

    def ensure_threads() -> None:
        while not stop.is_set():
            if settings.bitable_app_token and settings.account_table_id:
                try:
                    accounts = sync_accounts(settings)
                    with registry_lock:
                        registry.clear()
                        registry.update({a.account_id: a for a in accounts})
                    # This column represents whether the monitoring service is
                    # available for an account, not its transient live state.
                    for account in accounts:
                        update_account_state(
                            settings, account,
                            status="正常使用" if account.enabled else "未使用",
                        )
                except Exception as exc:
                    print(f"Account config sync failed: {exc}", flush=True)
            with registry_lock:
                ids = list(registry)
            for account_id in ids:
                if account_id not in started:
                    started.add(account_id)
                    thread = threading.Thread(target=run_room, args=(settings, account_id, registry, registry_lock, ledger), daemon=True)
                    thread.start()
                    threads.append(thread)
            stop.wait(settings.config_poll_seconds)

    manager = threading.Thread(target=ensure_threads, daemon=True)
    manager.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
