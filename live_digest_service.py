#!/usr/bin/env python3
"""Monitor two Douyin rooms, record them, transcribe locally, and notify Feishu."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import threading
import time
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


def send_text(settings: Settings, text: str) -> None:
    """Send through the app bot when configured, otherwise use a webhook."""
    token = tenant_token(settings)
    targets = [("open_id", value) for value in (settings.recipient_open_ids or []) if value]
    targets.extend((item.get("id_type", "open_id"), item["id"]) for item in (settings.recipients or []) if item.get("id"))
    if settings.chat_id:
        targets.append(("chat_id", settings.chat_id))
    if token and targets:
        for receive_type, receive_id in targets:
            response = requests.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": receive_id, "msg_type": "text",
                      "content": json.dumps({"text": text}, ensure_ascii=False)},
                timeout=30,
            )
            feishu_response_data(response)
        return
    feishu_text(settings.webhook, text)


def tenant_token(settings: Settings) -> str | None:
    if not settings.app_id or not settings.app_secret:
        return None
    response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.app_id, "app_secret": settings.app_secret}, timeout=20,
    )
    data = feishu_response_data(response)
    return data.get("tenant_access_token")


def upload_file(settings: Settings, path: Path) -> str | None:
    """Upload a message file. Feishu limits this endpoint to 30 MB."""
    token = tenant_token(settings)
    if not token:
        return None
    with path.open("rb") as video:
        response = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/files",
            headers={"Authorization": f"Bearer {token}"},
            data={"file_type": "stream", "file_name": path.name, "file_size": str(path.stat().st_size)},
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


def feishu_file(settings: Settings, file_key: str, text: str) -> None:
    token = tenant_token(settings)
    targets = [("open_id", value) for value in (settings.recipient_open_ids or []) if value]
    targets.extend((item.get("id_type", "open_id"), item["id"]) for item in (settings.recipients or []) if item.get("id"))
    if settings.chat_id:
        targets.append(("chat_id", settings.chat_id))
    if not token or not targets:
        send_text(settings, text)
        return
    for receive_type, receive_id in targets:
        response = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}",
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": receive_id, "msg_type": "file", "content": json.dumps({"file_key": file_key})},
            timeout=30,
        )
        feishu_response_data(response)
    send_text(settings, text)


def feishu_image(settings: Settings, image_key: str) -> None:
    token = tenant_token(settings)
    targets = [("open_id", value) for value in (settings.recipient_open_ids or []) if value]
    targets.extend((item.get("id_type", "open_id"), item["id"]) for item in (settings.recipients or []) if item.get("id"))
    if settings.chat_id:
        targets.append(("chat_id", settings.chat_id))
    if not token or not targets:
        return
    for receive_type, receive_id in targets:
        response = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}",
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": receive_id, "msg_type": "image",
                  "content": json.dumps({"image_key": image_key})},
            timeout=30,
        )
        feishu_response_data(response)


def artifact_timestamp(session_id: str) -> str:
    """Make the session start time readable while keeping it filename-safe."""
    try:
        return datetime.strptime(session_id, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return session_id


def artifact_path(directory: Path, kind: str, account_id: str, session_id: str, suffix: str) -> Path:
    return directory / f"{kind}-{account_id}-{artifact_timestamp(session_id)}{suffix}"


def capture_screenshot(video: Path, screenshot: Path) -> None:
    """Capture a readable frame near the beginning of the first recording segment."""
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "5", "-i", str(video),
        "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(screenshot),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def publish_finished_session(
    settings: Settings, *, first_segment: Path, transcript: Path, account_id: str,
    session_id: str, anchor: str, title: str, url: str, transcript_length: int,
) -> None:
    """Send the requested screenshot and named transcript, never the oversized MP4."""
    screenshot = artifact_path(transcript.parent, "直播截图", account_id, session_id, ".jpg")
    capture_screenshot(first_segment, screenshot)
    image_key = upload_image(settings, screenshot)
    if image_key:
        feishu_image(settings, image_key)
    else:
        send_text(settings, f"【直播截图】{anchor}\n本地文件：{screenshot}")

    transcript_key = upload_file(settings, transcript)
    caption = f"【下播，完整逐字稿】{anchor}\n{title}\n{url}\n共 {transcript_length} 字。"
    if transcript_key:
        feishu_file(settings, transcript_key, caption)
    else:
        send_text(settings, f"{caption}\n本地文件：{transcript}")


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


def run_room(settings: Settings, url: str) -> None:
    account_id = url.rstrip("/").split("/")[-1]
    room_dir = settings.output_dir / account_id
    room_dir.mkdir(parents=True, exist_ok=True)
    active = False
    process: subprocess.Popen[bytes] | None = None
    session_stamp: str | None = None
    print(f"Monitoring {url} every {settings.poll_seconds}s", flush=True)
    while True:
        try:
            info = stream_info(settings, url)
            live = bool(info.get("is_live") and info.get("record_url"))
            if live and not active:
                active = True
                session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                video_base = artifact_path(room_dir, "直播视频", account_id, session_stamp, "")
                command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", info["record_url"],
                           "-c", "copy", "-f", "segment", "-segment_time", str(settings.segment_seconds),
                           "-segment_format", "mp4", "-reset_timestamps", "1", "-movflags", "+faststart",
                           f"{video_base}_%03d.mp4"]
                process = subprocess.Popen(command)
                send_text(settings, f"【开播】{info.get('anchor_name', url)}\n{info.get('title', '')}\n{url}")
                print(f"Live started: {url} ({session_stamp})", flush=True)
            if active and not live:
                if process:
                    process.terminate()
                    process.wait(timeout=30)
                pattern = f"直播视频-{account_id}-{artifact_timestamp(session_stamp)}_*.mp4"
                segments = [segment for segment in sorted(room_dir.glob(pattern))
                            if segment.stat().st_size >= 1024]
                if settings.transcription_mode == "local_pull":
                    manifest = room_dir / f"{session_stamp}_pending_transcription.json"
                    manifest.write_text(json.dumps({
                        "session_id": session_stamp,
                        "account_id": account_id,
                        "url": url,
                        "anchor_name": info.get("anchor_name", url),
                        "title": info.get("title", ""),
                        "segments": [segment.name for segment in segments],
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    send_text(settings, f"【下播】{info.get('anchor_name', url)}\n录像已保留，等本地电脑开机后将自动转录并推送。")
                    print(f"Local transcription queued: {manifest}", flush=True)
                    active, process, session_stamp = False, None, None
                    time.sleep(settings.poll_seconds)
                    continue
                transcripts = []
                for segment in segments:
                    transcripts.append(transcribe(segment, settings))
                full = "\n\n".join(text for text in transcripts if text)
                full_path = artifact_path(room_dir, "直播逐字稿", account_id, session_stamp or "unknown", ".txt")
                full_path.write_text(full, encoding="utf-8")
                first_segment = segments[0] if segments else None
                if first_segment:
                    publish_finished_session(
                        settings, first_segment=first_segment, transcript=full_path, account_id=account_id,
                        session_id=session_stamp or "unknown", anchor=info.get("anchor_name", url),
                        title=info.get("title", ""), url=url, transcript_length=len(full),
                    )
                print(f"Live finished: {url}; {len(segments)} segments, {len(full)} chars", flush=True)
                active, process, session_stamp = False, None, None
            time.sleep(settings.poll_seconds)
        except Exception as exc:
            print(f"{url}: {exc}", flush=True)
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
                        cookie=cfg.get("douyin_cookie", ""))
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    threads = [threading.Thread(target=run_room, args=(settings, url), daemon=False)
               for url in cfg.get("rooms", [])]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
