import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_digest_service import (
    DeliveryLedger, Settings, artifact_path, attach_session_artifacts, concat_segments,
    drive_file_url, message_url, session_segments, sync_accounts,
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
        path = artifact_path(Path("."), "直播视频", "胡小群讲数学", "20260826_100012", "_00.mp4")
        self.assertEqual(path.name, "直播视频-胡小群讲数学-2026-08-26_10-00-12_00.mp4")

    @patch("live_digest_service.subprocess.run")
    def test_multiple_segments_are_merged_with_ffconcat(self, run):
        run.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = folder / "直播视频-胡小群讲数学-2026-08-26_10-00-12_000.mp4"
            second = folder / "直播视频-胡小群讲数学-2026-08-26_10-00-12_001.mp4"
            first.touch()
            second.touch()
            output = folder / "直播视频-胡小群讲数学-2026-08-26_10-00-12.mp4"
            concat_segments([first, second], output)
        command = run.call_args.args[0]
        self.assertIn("concat", command)
        self.assertIn("-c", command)
        self.assertIn("copy", command)
        self.assertEqual(command[-1], str(output))

    def test_final_video_is_not_treated_as_an_unmerged_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            segment = folder / "直播视频-胡小群讲数学-2026-08-26_10-00-12_000.mp4"
            final = folder / "直播视频-胡小群讲数学-2026-08-26_10-00-12_00.mp4"
            segment.write_bytes(b"x" * 1024)
            final.write_bytes(b"x" * 1024)
            self.assertEqual(session_segments(folder, "胡小群讲数学", "20260826_100012"), [segment])

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


if __name__ == "__main__":
    unittest.main()
