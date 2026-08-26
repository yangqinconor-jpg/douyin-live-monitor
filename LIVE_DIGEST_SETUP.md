# 双账号直播摘要服务

`live_digest_service.py` 按配置监控一个或多个抖音直播间，默认每场直播按 900 秒分段录制。开播立即发送飞书文本通知；下播后用本地 Whisper 转写全部分段、合并完整逐字稿，再推送完整文本和开头 15 分钟的视频片段。

## 环境

1. 安装 Python 3.10+、FFmpeg、Node.js，并安装上游项目依赖：
   `python3 -m pip install -r /path/to/DouyinLiveRecorder/requirements.txt openai-whisper requests`
2. 复制 `live_digest.json.example` 为 `live_digest.json`，填写 `recorder_root`、飞书 Webhook。
3. 可以直接通知个人：在 `feishu_open_ids` 填写一个或多个用户的 `open_id`；也可以填写 `feishu_chat_id` 通知群。若要上传视频到飞书，还需填写应用凭据并给应用发送消息/上传文件权限。仅填 Webhook 时会发送文字和本地文件路径。
4. 运行：`python3 live_digest_service.py --config live_digest.json`

## 飞书多维表格管理

项目使用两张表：

- **监控账号列表**：维护账号名称、抖音号（唯一标识）、监控开关和监控接收人。
- **直播记录**：每场直播一行，记录录制、转写、推送状态，以及截图和逐字稿附件。

目前多维表格还不是服务的实时配置中心。修改账号或接收人后，需要同步修改服务器上的
`live_digest.json` 并重启 `live-digest.service`；手工补录直播记录不会触发新的推送。
账号名称可以修改，但抖音号不能修改；已创建任务继续使用创建时的接收人快照。

Whisper 模型可改为 `tiny`、`base`、`small`、`medium`；中文通常从 `small` 起步。首次运行会下载模型。

## 云服务器

推荐安装到 `/opt/douyin-live-monitor`。真实的 `live_digest.json` 含 App Secret，不纳入 Git；服务器上单独创建并限制为仅服务用户可读。仓库附带 `live-digest.service`，用于 systemd 开机启动和异常重启。

真实飞书接收人应只写入未纳入 Git 的 `live_digest.json`。
