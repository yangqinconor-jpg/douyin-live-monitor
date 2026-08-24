# Douyin Live Monitor

一个面向长期运行的抖音直播监控与内容交付服务。它使用
[DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 解析直播流，用 FFmpeg
分段录制，下播后通过本地 Qwen3-ASR 转写，并由飞书应用机器人通知指定用户。

## 功能

- 同时监控多个抖音直播间，默认每 60 秒轮询一次。
- 检测到开播后，立即向多个飞书用户或群聊发送通知。
- FFmpeg 按固定时长分段录制，默认每段 15 分钟。
- 下播后由本地 Mac 上的 Qwen3-ASR 转写全部分段；Mac 关机时任务保留，开机后自动继续。
- 合并整场逐字稿，分段发送到飞书，避免单条消息过长。
- 上传本场直播的第一个 15 分钟视频分段。
- 支持 systemd 开机自启、异常重启和日志查询。

## 工作流程

```text
轮询直播状态
      |
      +-- 未开播 --> 等待下一次轮询
      |
      +-- 已开播 --> 飞书开播通知
                         |
                         +--> FFmpeg 每 15 分钟分段录制
                                      |
                                      +--> 检测下播
                                            |
                                            +--> 写入待转写任务
                                                  |
                                                  +--> Mac 开机后拉取录像
                                                        +--> 本地 Qwen3-ASR 逐段转写
                                            +--> 合并完整逐字稿
                                            +--> 飞书发送首段视频和逐字稿
```

## 仓库结构

```text
.
├── live_digest_service.py   # 监控、录制、转写和飞书推送主程序
├── live_digest.json.example # 安全的配置模板
├── live-digest.service      # systemd 服务文件
├── requirements.txt         # Python 依赖
└── LIVE_DIGEST_SETUP.md     # 简要安装说明
```

本仓库是自动化服务层，不复制上游录制器源码。部署时需要将
`DouyinLiveRecorder` 放在与本项目并列的目录中。

## 运行环境

- Linux（推荐 Ubuntu 22.04+）或 macOS
- Python 3.10+
- FFmpeg
- Node.js（DouyinLiveRecorder 的签名解析依赖）
- 建议至少 2 vCPU、4 GB 内存和足够的录播磁盘空间

云服务器只负责监控和录制。转写由本机已部署的 Qwen3-ASR 执行，既避免占用服务器资源，
也获得更好的中文逐字稿质量。

## 快速安装

```bash
sudo apt-get update
sudo apt-get install -y git ffmpeg nodejs python3-venv

sudo mkdir -p /opt/douyin-live-monitor
sudo chown "$USER":"$USER" /opt/douyin-live-monitor
cd /opt/douyin-live-monitor

git clone https://github.com/ihmily/DouyinLiveRecorder.git
git clone https://github.com/yangqinconor-jpg/douyin-live-monitor.git live-digest-service

python3 -m venv .venv
. .venv/bin/activate
pip install -r DouyinLiveRecorder/requirements.txt
pip install -r live-digest-service/requirements.txt

cd live-digest-service
cp live_digest.json.example live_digest.json
```

然后编辑 `live_digest.json`，再运行：

```bash
/opt/douyin-live-monitor/.venv/bin/python live_digest_service.py --config live_digest.json
```

## 配置

| 字段 | 含义 | 示例 |
| --- | --- | --- |
| `recorder_root` | DouyinLiveRecorder 绝对路径 | `/opt/douyin-live-monitor/DouyinLiveRecorder` |
| `output_dir` | 录播和逐字稿输出目录 | `./recordings` |
| `rooms` | 要监控的抖音直播间 URL 数组 | `https://live.douyin.com/...` |
| `poll_seconds` | 开播状态检查间隔 | `60` |
| `segment_seconds` | 录制分段时长 | `900` |
| `whisper_model` | Whisper 模型 | `small` |
| `whisper_language` | 转写语言 | `zh` |
| `transcription_mode` | 转写执行位置：`server` 或 `local_pull` | `local_pull` |
| `douyin_cookie` | 可选的抖音 Cookie，用于提高解析稳定性 | 字符串 |
| `feishu_app_id` | 飞书应用 App ID | `cli_xxx` |
| `feishu_app_secret` | 飞书应用 App Secret | 仅写入私密配置 |
| `feishu_recipients` | 多个用户接收目标 | 见配置模板 |
| `feishu_chat_id` | 可选的群聊 ID | `oc_xxx` |

`feishu_recipients` 支持 `open_id`、`user_id` 和 `union_id`：

```json
{
  "feishu_recipients": [
    {"name": "接收人1", "id_type": "open_id", "id": "ou_xxx"},
    {"name": "接收人2", "id_type": "user_id", "id": "user_xxx"}
  ]
}
```

## 飞书应用要求

1. 在飞书开放平台创建企业自建应用，并开启机器人能力。
2. 开通发送消息和上传文件所需权限。
3. 将应用发布，并把接收人加入应用的可用范围。
4. 在服务器私密配置中填入 App ID、App Secret 和接收人 ID。

只使用 Webhook 时可以发送文字，但直接通知个人和上传视频需要应用机器人。

## systemd 常驻运行

## 本地 Qwen3-ASR 转写（推荐）

如果不希望占用云服务器 CPU，将云端 `transcription_mode` 设为 `local_pull`。云端下播后仅写入 `*_pending_transcription.json`，Mac 开机后由 `local_transcriber.py` 通过 SSH 拉取 MP4，用本机 Qwen3-ASR 转写并推送飞书。

```bash
cd /opt/douyin-live-monitor/live-digest-service
/path/to/qwen_env/bin/python local_transcriber.py --config local_transcriber.json --once
```

复制并填写 `local_transcriber.json.example` 后，把 `live_digest.json` 放在同一目录（只保存在本机）。
其中 `asr_model_path` 指向本机的 Qwen3-ASR-1.7B 模型目录，运行 Python 必须是已安装 `qwen-asr` 的环境。
macOS 可将仓库中的 `com.douyin-live-monitor.local-transcriber.plist` 复制到 `~/Library/LaunchAgents/`，即可在登录时自动启动；
它每 5 分钟检查一次云端待转写任务。任务成功后云端清单会改名为 `.done`，避免重复推送。

`live-digest.service` 默认使用下列路径：

- 项目根目录：`/opt/douyin-live-monitor`
- 自动化服务：`/opt/douyin-live-monitor/live-digest-service`
- Python 虚拟环境：`/opt/douyin-live-monitor/.venv`
- 运行用户：`douyin-live`

```bash
sudo useradd --system --home /opt/douyin-live-monitor --shell /usr/sbin/nologin douyin-live
sudo chown -R douyin-live:douyin-live /opt/douyin-live-monitor
sudo chmod 600 /opt/douyin-live-monitor/live-digest-service/live_digest.json
sudo cp live-digest.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now live-digest.service
```

常用运维命令：

```bash
sudo systemctl status live-digest.service
sudo journalctl -u live-digest.service -f
sudo systemctl restart live-digest.service
sudo systemctl stop live-digest.service
```

## 输出文件

```text
recordings/
└── <抖音号>/
├── 20260824_210000_segment_000.mp4
├── 20260824_210000_segment_001.mp4
    ├── transcripts/
    │   ├── 20260824_210000_segment_000.txt
    │   └── 20260824_210000_segment_001.txt
    └── 20260824_220500_full.txt
```

项目不会自动删除历史录播。长期运行时应配置磁盘监控、定期归档或清理策略。

## 安全说明

- `live_digest.json` 已加入 `.gitignore`，不应提交到 GitHub。
- App Secret、Cookie 和真实用户 ID 只应存放在服务器的私密配置中。
- 建议将配置权限设为 `600`，并使用独立的低权限系统用户运行服务。

## 已知限制

- 抖音页面或签名规则变化时，需要跟随更新 DouyinLiveRecorder。
- 未配置有效 Cookie 时，部分直播间的解析稳定性可能下降。
- 视频上传受飞书文件大小和接口限额影响；高码率的 15 分钟分段可能超限。
- 当前转写是下播后串行处理分段，长时直播的交付会有延迟。

## 依赖与责任边界

本项目仅用于对已获得授权的直播内容进行录制、转写和内部通知。使用时请遵守相关平台条款、
内容版权、隐私和当地法律法规。
