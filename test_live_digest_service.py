import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_digest_service import (
    Account, CompletionNotificationError, DeliveryLedger, Settings, artifact_path, attach_session_artifacts,
    complete_with_feishu_minutes, concat_segments, create_live_record, drive_file_url, message_url,
    recording_complete_message, recording_complete_post, send_post, session_segments, sync_accounts,
)


class DeliveryLedgerTest(unittest.TestCase):
    def test_sent_delivery_is_not_claimed_again_and_failed_delivery_is(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "state.sqlite3")
            self.assertTrue(ledger.claim("session", "recipient", "transcript"))
            ledger.finish("session", "recipient", "transcript")
            self.assertFalse(ledger.claim("session", "recipient", "transcript"))
            ledger.finish("session", "other", "transcript", error="network")
            self.assertTrue(ledger.claim("session", "other", "transcript"))

    def test_active_session_keeps_name_and_recipient_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "state.sqlite3")
            recipients = [{"id_type": "open_id", "id": "ou_1", "name": "用户"}]
            ledger.start_session("account", "session", "原名称", recipients, "record", 123)
            snapshot = ledger.active_session("account")
            self.assertEqual(snapshot["account_name"], "原名称")
            self.assertEqual(snapshot["recipients"], recipients)
            ledger.end_session("account")
            self.assertIsNone(ledger.active_session("account"))

    def test_artifact_checkpoint_survives_a_notification_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "state.sqlite3")
            ledger.save_session_artifacts(
                "session", video_url="https://video", transcript_url="https://transcript",
                minute_url="https://minutes", video_name="video.mp4", transcript_name="transcript.docx",
            )
            self.assertEqual(ledger.session_artifacts("session"), {
                "video_url": "https://video", "transcript_url": "https://transcript",
                "minute_url": "https://minutes", "video_name": "video.mp4", "transcript_name": "transcript.docx",
            })


class FeishuConfigTest(unittest.TestCase):
    def test_account_table_is_the_runtime_configuration_source(self):
        settings = Settings(Path("."), Path("."), "", bitable_app_token="app", account_table_id="accounts")
        response = {"data": {"items": [{"record_id": "rec", "fields": {
            "监控账号": "账号名称", "抖音号": "douyin-id", "监控开关": "启用",
            "直播间链接": {"link": "https://live.douyin.com/douyin-id"},
            "监控接收人": [{"id": "ou_1", "name": "接收人"}],
        }}]}}
        with patch("live_digest_service.bitable_request", return_value=response):
            account = sync_accounts(settings)[0]
        self.assertTrue(account.enabled)
        self.assertEqual(account.name, "账号名称")
        self.assertEqual(account.recipients[0]["id"], "ou_1")

    def test_names_are_used_in_artifact_paths(self):
        path = artifact_path(Path("."), "直播逐字稿", "账号名称", "20260825_080020", ".txt")
        self.assertEqual(path.name, "直播逐字稿-账号名称-2026-08-25_08-00-20.txt")

    def test_complete_video_name_uses_account_name_and_final_suffix(self):
        path = artifact_path(Path("."), "直播视频", "示例账号", "20260826_100012", "_00.mp4")
        self.assertEqual(path.name, "直播视频-示例账号-2026-08-26_10-00-12_00.mp4")

    @patch("live_digest_service.subprocess.run")
    def test_multiple_segments_are_merged_with_ffconcat(self, run):
        run.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = folder / "直播视频-示例账号-2026-08-26_10-00-12_000.mp4"
            second = folder / "直播视频-示例账号-2026-08-26_10-00-12_001.mp4"
            first.touch()
            second.touch()
            output = folder / "直播视频-示例账号-2026-08-26_10-00-12.mp4"
            concat_segments([first, second], output)
        command = run.call_args.args[0]
        self.assertIn("concat", command)
        self.assertIn("-c", command)
        self.assertIn("copy", command)
        self.assertEqual(command[-1], str(output))

    def test_final_video_is_not_treated_as_an_unmerged_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            segment = folder / "直播视频-示例账号-2026-08-26_10-00-12_000.mp4"
            final = folder / "直播视频-示例账号-2026-08-26_10-00-12_00.mp4"
            segment.write_bytes(b"x" * 1024)
            final.write_bytes(b"x" * 1024)
            self.assertEqual(session_segments(folder, "示例账号", "20260826_100012"), [segment])

    @patch("live_digest_service.bitable_request")
    def test_new_live_record_uses_current_status_fields(self, request):
        request.return_value = {"data": {"record": {"record_id": "record"}}}
        account = Account("example_live_id", "示例账号", "https://live.douyin.com/example_live_id", True, [])
        create_live_record(Settings(Path("."), Path("."), "", bitable_app_token="app", record_table_id="records"), account, "20260826_100012", "标题", 123, [])
        fields = request.call_args.args[3]["fields"]
        self.assertEqual(fields["转写状态"], "待转写")
        self.assertEqual(fields["完成提醒状态"], "待发送")
        self.assertNotIn("推送状态", fields)

    @patch("live_digest_service.update_live_record")
    @patch("live_digest_service.upload_bitable_attachment", return_value="image-token")
    def test_session_artifacts_write_minutes_and_docx_links(self, _upload, update):
        attach_session_artifacts(
            Settings(Path("."), Path("."), ""), "record", Path("screenshot.jpg"),
            minute_url="https://shenyidushu.feishu.cn/minutes/example",
            transcript_url="https://shenyidushu.feishu.cn/drive/file/example",
        )
        fields = update.call_args.args[2]
        self.assertEqual(fields["智能纪要链接"]["link"], "https://shenyidushu.feishu.cn/minutes/example")
        self.assertEqual(fields["文字记录链接"]["link"], "https://shenyidushu.feishu.cn/drive/file/example")

    def test_drive_file_link_is_clickable(self):
        self.assertEqual(drive_file_url("file-token"), "https://shenyidushu.feishu.cn/drive/file/file-token")

    def test_completion_message_contains_all_three_finished_assets(self):
        message = recording_complete_message(
            "示例账号", "20260826_102012", "https://video", "https://transcript", "https://minutes",
        )
        self.assertEqual(message, (
            "【直播录制完成提醒】\n"
            "“示例账号”在“2026年8月26日 10点20”的直播录制已完成，请查收。\n"
            "录制视频：\nhttps://video\n"
            "文字记录：\nhttps://transcript\n"
            "智能纪要：\nhttps://minutes"
        ))

    def test_completion_post_uses_account_name_instead_of_source_file_name(self):
        post = recording_complete_post(
            "示例账号", "20260826_102012", "https://video", "https://transcript", "https://minutes",
            "直播视频-示例账号-2026-08-26_10-20-12_00.mp4",
            "直播逐字稿-示例账号-2026-08-26_10-20-12.docx",
        )
        link_labels = [line[0]["text"] for line in post["zh_cn"]["content"] if line[0]["tag"] == "a"]
        self.assertEqual(link_labels, [
            "直播视频-示例账号-2026-08-26_10-20-12_00.mp4",
            "直播逐字稿-示例账号-2026-08-26_10-20-12.docx",
            "智能纪要-示例账号-2026-08-26_10-20-12",
        ])

    def test_deployment_gate_blocks_new_session_only_during_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "state.sqlite3")
            ledger.set_deployment_pending(True)
            self.assertFalse(ledger.start_session("account", "session", "名称", [], "", 123))
            ledger.set_deployment_pending(False)
            self.assertTrue(ledger.start_session("account", "session", "名称", [], "", 123))

    def test_message_uuid_is_stable_per_delivery(self):
        first = message_url("open_id", "session", "recipient", "screenshot")
        second = message_url("open_id", "session", "recipient", "screenshot")
        other = message_url("open_id", "session", "recipient", "transcript")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    @patch("live_digest_service.feishu_response_data", side_effect=[{"data": {"message_id": "first"}}, RuntimeError("network")])
    @patch("live_digest_service.requests.post")
    @patch("live_digest_service.tenant_token", return_value="token")
    def test_failed_recipient_does_not_block_other_recipient(self, _token, _post, _response):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "state.sqlite3")
            recipients = [
                {"id_type": "open_id", "id": "ou_first", "name": "管理员甲"},
                {"id_type": "open_id", "id": "ou_second", "name": "管理员乙"},
            ]
            with self.assertRaisesRegex(RuntimeError, "network"):
                send_post(Settings(Path("."), Path("."), ""), {"zh_cn": {"content": []}}, "fallback", recipients=recipients, session_id="session", message_type="recording_complete", ledger=ledger)
            self.assertFalse(ledger.claim("session", "ou_first", "recording_complete"))
            self.assertTrue(ledger.claim("session", "ou_second", "recording_complete"))

    @patch("live_digest_service.update_live_record")
    @patch("live_digest_service.attach_session_artifacts")
    @patch("live_digest_service.publish_finished_session", side_effect=[RuntimeError("network"), None])
    @patch("live_digest_service.upload_drive_file", side_effect=["video-token", "transcript-token"])
    @patch("live_digest_service.create_transcript_docx")
    @patch("live_digest_service.wait_for_transcript", return_value="逐字稿")
    @patch("live_digest_service.upload_minutes", return_value=("minute-token", "https://minutes"))
    @patch("live_digest_service.drive_file_url", side_effect=["https://video", "https://transcript"])
    @patch("live_digest_service.session_drive_folder", return_value="folder")
    @patch("live_digest_service.capture_screenshot")
    @patch("live_digest_service.concat_segments")
    def test_notification_retry_reuses_finished_artifacts(self, _concat, _screenshot, _folder, _urls, _minutes,
                                                          _transcript, _docx, uploads, _publish, _attach, _update):
        with tempfile.TemporaryDirectory() as directory:
            room = Path(directory)
            ledger = DeliveryLedger(room / "state.sqlite3")
            settings = Settings(Path("."), room, "")
            args = dict(room_dir=room, segments=[room / "segment.mp4"], account_name="示例账号", session_id="20260826_100012", record_id="record", title="", url="", recipients=[], ledger=ledger)
            with self.assertRaises(CompletionNotificationError):
                complete_with_feishu_minutes(settings, **args)
            complete_with_feishu_minutes(settings, **args)
            self.assertEqual(uploads.call_count, 2)


if __name__ == "__main__":
    unittest.main()
