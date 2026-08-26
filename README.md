# Douyin Live Monitor

一个面向长期运行的抖音直播监控与内容交付服务。它使用
[DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 解析直播流，用 FFmpeg
分段录制，下播后通过本地 Qwen3-ASR 转写，并由飞书应用机器人通知指定用户。

## 功能

- 同时监控多个抖音直播间，默认每 60 秒轮询一次。
- 检测到开播后，立即向多个飞书用户或群聊发送通知。
- FFmpeg 按固定时长分段录制，默认每段 15 分钟。
- 下播后由本地 Mac 上的 Qwen3-ASR 转写全部分段；Mac 关机时任务保留，开机后自动继续。
- 录像按 `直播视频-账号名称-直播时间-序号.mp4` 保存，逐字稿按
  `直播逐字稿-账号名称-直播时间.txt` 保存。
- 下播后生成 `直播截图-账号名称-直播时间.jpg`，并把截图和完整逐字稿作为飞书消息推送。
- 支持 systemd 开机自启、异常重启和日志查询。

## 配置来源与飞书多维表格

线上监控服务会定时读取服务器 `live_digest.json` 中的飞书多维表格配置，自动同步“监控账号列表”
中的启用账号、账号名称、直播间链接和接收人。服务会创建和更新每场“直播记录”，并写入状态、
失败原因、截图和逐字稿附件。

推荐的管理方式是：在“监控账号列表”维护账号和接收人，在“直播记录”查看每一场直播的处理结果。
首次部署时需要在 `live_digest.json` 填写多维表格 App Token、表 ID 和数据表 ID；之后账号列表的
启用/停用和接收人变更会在下一次同步周期生效。

### 监控账号列表字段

| 字段 | 说明 |
| --- | --- |
| 账号名称 | 展示名称，可修改；历史直播记录保留当时名称 |
| 抖音号 | 唯一标识，例如 `xhls8888`、`569306820`；录入后不修改 |
| 监控开关 | `启用` / `停用`。停用只对新场次生效，进行中的录制、转写和推送完成后再停用 |
| 监控接收人 | 一个或多个飞书用户；新任务创建时保存接收人快照 |
| 服务状态 | `未运行`、`检查中`、`直播中`、`录制中`、`异常` |
| 最近检查时间 | 服务最近一次检查账号的时间 |
| 最近直播时间 | 最近一场直播的开始和结束时间 |
| 最近错误 | 最近一次解析、录制或推送错误 |

直播状态和录制状态是同步进行的：检测到直播后立即录制；一场直播结束后算新的一场，上一场的
转写可以和下一场录制并行。

### 直播记录字段

每次下播新增一行，主字段格式为 `【账号名称】YYYYMMDD_HHMM-HHMM`，例如
`【胡小群讲数学】20260825_0800-1035`。字段包括：

`直播记录`、`账号名称`、`抖音号`、`直播标题`、`开播时间`、`下播时间`、`直播时长（分钟）`、
`录制状态`、`转写状态`、`推送状态`、`截图`、`逐字稿`、`原始录像链接`、`推送时间`、
`失败原因`、`任务 ID`。

- `录制状态`：`待录制`、`录制中`、`已完成`、`部分录制`、`录制失败`。
- `转写状态`：`待下载`、`下载中`、`转写中`、`已完成`、`转写失败`。
- `推送状态`：`待推送`、`推送中`、`已推送`、`推送失败`。
- `任务 ID` 使用场次开始时间，作为重试、排障和幂等判断依据。
- `原始录像链接` 当前暂留空；录像文件保存在服务器和本地转写目录。

账号名称变更只影响展示，抖音号负责关联；接收人变更不追溯已创建任务，已创建任务按接收人快照发送。

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
                                            +--> 生成直播截图
                                            +--> 飞书发送截图和逐字稿附件
```

## 仓库结构

```text
.
├── live_digest_service.py   # 监控、录制、转写和飞书推送主程序
├── live_digest.json.example # 安全的配置模板
├── local_transcriber.py     # 本地 Qwen3-ASR 转写 worker
├── local_transcriber.json.example
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

### 现在如何通过表格调整配置

1. 在飞书多维表格的“监控账号列表”新增或修改账号名称、抖音号、监控开关和监控接收人。
2. 服务每 60 秒自动同步，无需手动改 `rooms` 或接收人列表，也无需重启服务。
3. 只有修改 App Token、数据表 ID 或其他基础配置时，才需要重启服务：

```bash
sudo systemctl restart live-digest.service
sudo systemctl is-active live-digest.service
```

服务写入“直播记录”不会触发新的推送；手工新增或修改直播记录也不会触发推送。每个任务保存创建时的
账号名称、抖音号和接收人快照，账号或接收人后续变更不会影响已创建任务。

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
cd /path/to/live-digest-service
/path/to/qwen_env/bin/python local_transcriber.py --config local_transcriber.json --once
```

复制并填写 `local_transcriber.json.example` 后，把 `live_digest.json` 放在同一目录（只保存在本机）。
其中 `asr_model_path` 指向本机的 Qwen3-ASR-1.7B 模型目录，运行 Python 必须是已安装 `qwen-asr` 的环境。
macOS 可将仓库中的 `com.douyin-live-monitor.local-transcriber.plist` 复制到 `~/Library/LaunchAgents/`，即可在登录时自动启动；
复制前先把其中的 `YOUR_USER` 和路径占位符改为本机实际路径。它每 5 分钟检查一次云端待转写任务；
任务成功后云端清单会改名为 `.done`，避免重复推送。

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
- 飞书表格只负责配置和结果展示，抖音直播内容的实际录制仍依赖服务器和 FFmpeg；请确保服务器磁盘
  空间充足。

## 依赖与责任边界

本项目仅用于对已获得授权的直播内容进行录制、转写和内部通知。使用时请遵守相关平台条款、
内容版权、隐私和当地法律法规。
