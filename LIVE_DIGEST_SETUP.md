# 双账号直播摘要服务

`live_digest_service.py` 固定监控 `xhls8888` 和 `569306820`，每场直播按 900 秒分段录制。开播立即发送飞书文本通知；下播后用本地 Whisper 转写全部分段、合并完整逐字稿，再推送完整文本和开头 15 分钟的视频片段。

## 环境

1. 安装 Python 3.10+、FFmpeg、Node.js，并安装上游项目依赖：
   `python3 -m pip install -r /path/to/DouyinLiveRecorder/requirements.txt openai-whisper requests`
2. 复制 `live_digest.json.example` 为 `live_digest.json`，填写 `recorder_root`、飞书 Webhook。
3. 可以直接通知个人：在 `feishu_open_ids` 填写一个或多个用户的 `open_id`；也可以填写 `feishu_chat_id` 通知群。若要上传视频到飞书，还需填写应用凭据并给应用发送消息/上传文件权限。仅填 Webhook 时会发送文字和本地文件路径。
4. 运行：`python3 live_digest_service.py --config live_digest.json`

Whisper 模型可改为 `tiny`、`base`、`small`、`medium`；中文通常从 `small` 起步。首次运行会下载模型。

## 云服务器

推荐安装到 `/opt/douyin-live-monitor`。真实的 `live_digest.json` 含 App Secret，不纳入 Git；服务器上单独创建并限制为仅服务用户可读。仓库附带 `live-digest.service`，用于 systemd 开机启动和异常重启。

当前飞书推送目标：覃洋、何秦、王宁，使用各自的 `user_id`。
