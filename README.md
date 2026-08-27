# Douyin Live Monitor

一个面向长期运行的抖音直播监控与内容交付服务。它使用
[DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 解析直播流，用 FFmpeg
分段录制，下播后合并完整录像，通过飞书妙记转写，并由飞书应用机器人通知指定用户。

## 功能

- 同时监控多个抖音直播间，默认每 60 秒轮询一次。
- 检测到开播后，立即向多个飞书用户或群聊发送通知。
- FFmpeg 按固定时长分段录制，默认每段 15 分钟。
- 下播后先合并全部分段，完整录像上传到飞书云盘后由飞书妙记转写。
- 完整视频上传飞书并保存文件凭证后，自动删除服务器上的本场 MP4 分段与合并文件；上传失败时保留录像以便重试，妙记失败时直接复用已上传视频。
- 录制中的临时片段带三位序号；最终合并录像为
  `直播视频-账号名称-直播时间_00.mp4`。原始 MP4 只用于云盘归档，不作为完成提醒链接。
- 下播后生成 `直播截图-账号名称-直播时间.jpg` 写回直播记录；飞书只发送一条“直播录制完成提醒”，其中“录制视频”指向妙记播放页，“文字记录”和“智能纪要”分别指向飞书自动生成的两个 DOCX。
- 同一场次按“接收人 + 消息类型”持久化去重；发送中任务不能被其他工作进程重复领取，失败任务只重试失败的接收人。
- 支持 systemd 开机自启、异常重启和日志查询。

## 配置来源与飞书多维表格

线上监控服务会定时读取服务器 `live_digest.json` 中的飞书多维表格配置，自动同步“监控账号列表”
中的启用账号、账号名称、直播间链接和接收人。服务会创建和更新每场“直播记录”，并写入状态、
失败原因、截图、智能纪要链接和文字记录链接。

推荐的管理方式是：在“监控账号列表”维护账号和接收人，在“直播记录”查看每一场直播的处理结果。
首次部署时需要在 `live_digest.json` 填写多维表格 App Token、表 ID 和数据表 ID；之后账号列表的
启用/停用和接收人变更会在下一次同步周期生效。

### 监控账号列表字段

| 字段 | 说明 |
| --- | --- |
| 账号名称 | 展示名称，可修改；历史直播记录保留当时名称 |
| 抖音号 | 唯一标识，例如 `example_live_id`；录入后不修改 |
| 直播间链接 | 直播间的完整 URL；未填写时服务会按抖音号生成默认链接 |
| 监控开关 | `启用` / `停用`。停用只对新场次生效，进行中的录制、转写和推送完成后再停用 |
| 监控接收人 | 一个或多个飞书用户；新任务创建时保存接收人快照 |
| 服务状态 | `正常使用`、`未使用`、`异常`。只表示该账号的监控服务是否可用，不表示是否正在直播 |
| 最后同步时间 | 服务最近一次读取并同步该账号配置的时间 |
| 最后开播时间 | 最近一场直播的开播时间 |
| 最后下播时间 | 最近一场直播的下播时间 |
| 监控场次 | 由系统自动累计；每创建一场新的直播记录时加一 |

直播状态和录制状态是同步进行的：检测到直播后立即录制；一场直播结束后算新的一场，上一场的
转写可以和下一场录制并行。某一场的录制、转写与推送进度只在“直播记录”中查看。

### 直播记录字段

每次下播新增一行，主字段格式为 `【账号名称】YYYYMMDD_HHMM-HHMM`，例如
`【示例账号】20260825_0800-1035`。字段包括：

`直播记录`、`账号名称`、`抖音号`、`直播标题`、`开播时间`、`下播时间`、`直播时长（分钟）`、
`录制状态`、`转写状态`、`完成提醒状态`、`截图`、`智能纪要链接`、`文字记录链接`、`完成提醒时间`、
`失败原因`、`任务 ID`。

- `录制状态`：`待录制`、`录制中`、`已完成`、`部分录制`、`录制失败`。
- `转写状态`：`待转写`、`转写中`、`已完成`、`转写失败`。
- `完成提醒状态`：`待发送`、`发送中`、`已发送`、`发送失败`、`无需发送`。
- `任务 ID` 使用场次开始时间，作为重试、排障和幂等判断依据。
- `智能纪要链接` 直达飞书自动生成的智能纪要 DOCX；`文字记录链接` 直达飞书自动生成的文字记录 DOCX。
  完整 MP4 归档在飞书云盘 `直播监控台/抖音/账号名称/直播录像`，但不出现在机器人完成提醒中。

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
                                            +--> 合并全部录像为完整 MP4
                                            +--> 上传飞书云盘并提交飞书妙记
                                            +--> 等待飞书生成文字记录与智能纪要 DOCX
                                            +--> 生成直播截图并写回记录
                                            +--> 写回文字记录、智能纪要链接
                                            +--> 发送一条含妙记播放页和两个 DOCX 的完成提醒
```

## 仓库结构

```text
.
├── live_digest_service.py   # 监控、录制、转写和飞书推送主程序
├── live_digest.json.example # 安全的配置模板
├── local_transcriber.py     # 旧的本地 Qwen3-ASR 兼容 worker
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

云服务器负责监控、录制、完整文件归档和提交飞书妙记；无需等待本机开机或下载 MP4。

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
| `transcription_mode` | 正式模式为 `feishu_minutes`；兼容 `server`、`local_pull` | `feishu_minutes` |
| `douyin_cookie` | 可选的抖音 Cookie，用于提高解析稳定性 | 字符串 |
| `feishu_app_id` | 飞书应用 App ID | `cli_xxx` |
| `feishu_app_secret` | 飞书应用 App Secret | 仅写入私密配置 |
| `feishu_recipients` | 多个用户接收目标 | 见配置模板 |
| `feishu_chat_id` | 可选的群聊 ID | `oc_xxx` |
| `feishu_user_token_path` | 管理员飞书用户授权令牌的私密路径 | `/etc/douyin-live-monitor/feishu_user_tokens.json` |
| `drive_root_folder_token` | “直播监控台”文件夹 token | 飞书云盘文件夹 token |
| `minutes_poll_seconds` | 妙记转写结果轮询间隔 | `60` |

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

## 分支与发布

GitHub 提交不会自动改动服务器。只有将已验证的 `main` 分支明确部署并重启服务，线上版本才会变化。

- `main`：生产分支，内容应与线上已验证版本一致。
- `develop`：日常开发和本地验证分支；默认在这里修改。
- `feature/<功能名>`：较大或独立的功能分支，例如 `feature/session-counter`；完成后合并到 `develop`。

发布流程：先在 `develop` 或功能分支完成修改和测试，确认不会造成重复推送后合并至 `main`；再单独确认
要部署的提交，最后更新服务器并重启 `live-digest.service`。普通代码提交、推送 `develop`、或创建功能分支
均不会触发线上部署。

### 不打断直播的安全发布

不能直接在有直播录制时重启 `live-digest.service`：重启会终止当前 FFmpeg 进程，造成一场直播被拆成多段。
项目提供 `safe_apply_update.sh` 与 `douyin-safe-deploy.service`。首次安装这两个文件后，日后只从干净的
`main` 分支执行 `bash deploy_main_safely.sh`：新版本先放入待发布区；系统持续正常监控，直到识别到没有活动
场次，才在数据库中加一个极短的发布锁、替换主程序并重启。发布锁只覆盖重启瞬间，不会因为等待升级而暂停
其他账号的监控。

`develop` 和功能分支永远不允许直接部署。紧急维护也应先确认没有活动场次；禁止在直播中执行
`sudo systemctl restart live-digest.service`。

## 飞书应用要求

1. 在飞书开放平台创建企业自建应用，并开启机器人能力。
2. 开通发送消息和上传文件所需权限。
3. 将应用发布，并把接收人加入应用的可用范围。
4. 为飞书妙记流程同时开通 `drive:drive`、妙记上传与逐字稿读取权限，以及用户授权范围
   `offline_access`。管理员完成一次授权后，服务才能自动续期并长期运行。
5. 在服务器私密配置中填入 App ID、App Secret 和接收人 ID。

授权回调服务应以管理员身份保存令牌，但令牌文件须只交给 `douyin-live` 服务用户读取和写入；
仓库的回调程序会在授权完成时自动设置为该归属。

只使用 Webhook 时可以发送文字，但直接通知个人和上传视频需要应用机器人。

## systemd 常驻运行

## 旧版本地 Qwen3-ASR 转写（兼容）

正式环境应使用 `feishu_minutes`，不依赖本机开机。只有需要临时回退时，才将
`transcription_mode` 设为 `local_pull`；云端会写入 `*_pending_transcription.json`，再由
`local_transcriber.py` 从服务器拉取 MP4 并用本机 Qwen3-ASR 转写。

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
