import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_digest_service import DeliveryLedger, Settings, artifact_path, deployment_lock_path, message_url, sync_accounts


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

    def test_deployment_lock_lives_beside_state_database(self):
        with tempfile.TemporaryDirectory() as directory:
            state_db = Path(directory) / "monitor_state.sqlite3"
            settings = Settings(Path("."), Path("."), "", state_db=str(state_db))
            self.assertEqual(deployment_lock_path(settings), (Path(directory) / ".deployment-pending").resolve())

    def test_message_uuid_is_stable_per_delivery(self):
        first = message_url("open_id", "session", "recipient", "screenshot")
        second = message_url("open_id", "session", "recipient", "screenshot")
        other = message_url("open_id", "session", "recipient", "transcript")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)


if __name__ == "__main__":
    unittest.main()
