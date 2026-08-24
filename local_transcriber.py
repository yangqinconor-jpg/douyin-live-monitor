#!/usr/bin/env python3
"""Pull completed cloud recordings to this Mac, transcribe locally, then notify Feishu."""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from live_digest_service import Settings, feishu_file, send_text, upload_video


_QWEN_MODEL: Any | None = None


def qwen_model(config: dict[str, Any]) -> Any:
    """Load the local Qwen model once and keep it in memory for all segments."""
    global _QWEN_MODEL
    if _QWEN_MODEL is not None:
        return _QWEN_MODEL

    import torch
    from qwen_asr import Qwen3ASRModel

    model_path = Path(config["asr_model_path"]).expanduser()
    if not model_path.is_dir():
        raise RuntimeError(f"Qwen3-ASR model directory not found: {model_path}")
    device = config.get("asr_device", "mps" if torch.backends.mps.is_available() else "cpu")
    dtype = torch.float16 if device == "mps" else torch.float32
    print(f"Loading Qwen3-ASR from {model_path} on {device}", flush=True)
    _QWEN_MODEL = Qwen3ASRModel.from_pretrained(
        str(model_path), dtype=dtype, device_map=device,
        max_new_tokens=int(config.get("asr_max_new_tokens", 8192)),
    )
    return _QWEN_MODEL


def transcribe_locally(path: Path, config: dict[str, Any]) -> str:
    """Extract a compatible WAV track and transcribe it with local Qwen3-ASR."""

    out_dir = path.parent / "transcripts"
    out_dir.mkdir(exist_ok=True)
    txt = out_dir / f"{path.stem}.txt"
    if txt.exists():
        return txt.read_text(encoding="utf-8")
    wav = out_dir / f"{path.stem}.wav"
    try:
        run([
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn",
            "-ar", "16000", "-ac", "1", str(wav),
        ], timeout=1800)
        result = qwen_model(config).transcribe(
            audio=str(wav), language=config.get("asr_language", "Chinese"),
        )[0]
        text = result.text.strip()
    finally:
        wav.unlink(missing_ok=True)
    txt.write_text(text, encoding="utf-8")
    return text


def run(command: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True, timeout=timeout)


def ssh_args(server: str, key: Path) -> list[str]:
    return ["ssh", "-i", str(key.expanduser()), "-o", "IdentitiesOnly=yes", server]


def scp_args(key: Path) -> list[str]:
    return ["scp", "-i", str(key.expanduser()), "-o", "IdentitiesOnly=yes"]


def remote_manifests(server: str, key: Path, remote_root: str) -> list[str]:
    command = f"find {shlex.quote(remote_root)} -type f -name '*_pending_transcription.json' -print"
    result = run([*ssh_args(server, key), command], timeout=60)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def local_settings(config: dict[str, Any], config_dir: Path) -> Settings:
    feishu_path = (config_dir / config.get("feishu_config", "live_digest.json")).resolve()
    feishu = json.loads(feishu_path.read_text(encoding="utf-8"))
    output_dir = (config_dir / config.get("local_output_dir", "recordings-local")).resolve()
    return Settings(
        recorder_root=Path("."), output_dir=output_dir, webhook=feishu.get("feishu_webhook", ""),
        app_id=feishu.get("feishu_app_id", ""), app_secret=feishu.get("feishu_app_secret", ""),
        chat_id=feishu.get("feishu_chat_id", ""), recipient_open_ids=feishu.get("feishu_open_ids", []),
        recipients=feishu.get("feishu_recipients", []),
    )


def process_manifest(config: dict[str, Any], config_dir: Path, manifest_path: str) -> None:
    server = config["server"]
    key = Path(config["ssh_key"]).expanduser()
    settings = local_settings(config, config_dir)
    session_dir = settings.output_dir / Path(manifest_path).parent.name
    session_dir.mkdir(parents=True, exist_ok=True)
    local_manifest = session_dir / Path(manifest_path).name
    run([*scp_args(key), f"{server}:{manifest_path}", str(local_manifest)], timeout=120)
    manifest = json.loads(local_manifest.read_text(encoding="utf-8"))
    remote_dir = str(Path(manifest_path).parent)
    segments: list[Path] = []
    for name in manifest.get("segments", []):
        local_file = session_dir / Path(name).name
        if not local_file.exists():
            run([*scp_args(key), f"{server}:{remote_dir}/{Path(name).name}", str(local_file)], timeout=7200)
        segments.append(local_file)
    if not segments:
        raise RuntimeError(f"No recordings listed in {manifest_path}")

    transcripts = [transcribe_locally(segment, config) for segment in segments]
    full = "\n\n".join(text for text in transcripts if text)
    full_path = session_dir / f"{manifest['session_id']}_full.txt"
    full_path.write_text(full, encoding="utf-8")
    first_segment = segments[0]
    file_key = upload_video(settings, first_segment)
    title = manifest.get("title", "")
    anchor = manifest.get("anchor_name", manifest.get("url", ""))
    if file_key:
        feishu_file(settings, file_key, f"【开头15分钟片段】{anchor}\n{title}\n完整逐字稿已生成。")
    else:
        send_text(settings, f"【开头15分钟片段】{anchor}\n本地文件：{first_segment}")
    message = f"【下播，完整逐字稿】{anchor}\n{manifest.get('url', '')}\n共 {len(full)} 字。\n{full}"
    for offset in range(0, len(message), 6000):
        send_text(settings, message[offset:offset + 6000])
    done_path = f"{manifest_path}.done"
    run([*ssh_args(server, key), f"mv {shlex.quote(manifest_path)} {shlex.quote(done_path)}"], timeout=60)
    print(f"Completed local transcription: {full_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="local_transcriber.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config_dir = config_path.parent
    poll_seconds = int(config.get("poll_seconds", 300))
    while True:
        try:
            manifests = remote_manifests(config["server"], Path(config["ssh_key"]), config["remote_recordings_dir"])
            for manifest in manifests:
                process_manifest(config, config_dir, manifest)
        except Exception as exc:
            print(f"Local transcription worker: {exc}", flush=True)
        if args.once:
            return
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
