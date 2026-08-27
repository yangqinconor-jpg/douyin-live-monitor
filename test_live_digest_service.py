import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_digest_service import (
    Account, CompletionNotificationError, DeliveryLedger, LowDiskSpaceError, Settings, artifact_path,
    attach_session_artifacts, cleanup_uploaded_recordings, complete_with_feishu_minutes, concat_segments,
    create_live_record, drive_file_url, ensure_merge_space, find_minutes_documents, message_url,
    RecordingIntegrityError, VideoMetadata, recording_complete_message, recording_complete_post,
    recording_integrity_result, send_post, session_segments, sync_accounts, upload_drive_file,
    stable_file_sizes, verify_drive_file_size, video_is_readable,
)


class DeliveryLedgerTest(unittest.TestCase):
    def test_old_artifact_database_is_migrated_without_losing_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE session_artifacts ("
                "session_id TEXT PRIMARY KEY, archive_video_url TEXT, minutes_url TEXT, "
                "transcript_url TEXT, summary_url TEXT, video_name TEXT, minutes_title TEXT, "
                "minutes_created_at INTEGER)"
            )
            connection.execute(
                "INSERT INTO session_artifacts VALUES(?,?,?,?,?,?,?,?)",
                ("session", "https://archive", "https://minutes", "https://transcript",
                 "https://summary", "video.mp4", "video", 123),
            )
            connection.commit()
            connection.close()

            ledger = DeliveryLedger(path)
            artifacts = ledger.session_artifacts("session")
            self.assertEqual(artifacts["archive_video_url"], "https://archive")
            self.assertEqual(artifacts["minutes_url"], "https://minutes")
            self.assertIsNone(artifacts["archive_video_size"])
            self.assertIsNone(artifacts["recording_status"])

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
                "session", archive_video_url="https://archive", minutes_url="https://minutes",
                transcript_url="https://transcript", summary_url="https://summary", video_name="video.mp4",
                minutes_title="video", minutes_created_at=123,
            )
            self.assertEqual(ledger.session_artifacts("session"), {
                "archive_video_url": "https://archive", "minutes_url": "https://minutes",
                "transcript_url": "https://transcript", "summary_url": "https://summary",
                "video_name": "video.mp4", "minutes_title": "video", "minutes_created_at": 123,
                "archive_video_size": None, "recording_status": None, "integrity_note": None,
            })

    def test_in_flight_delivery_cannot_be_claimed_by_a_second_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = DeliveryLedger(path)
            second = DeliveryLedger(path)
            self.assertTrue(first.claim("session", "recipient", "recording_complete"))
            self.assertFalse(second.claim("session", "recipient", "recording_complete"))

    def test_minutes_creation_is_claimed_once_and_completed_url_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = DeliveryLedger(path)
            second = DeliveryLedger(path)
            self.assertEqual(first.claim_minutes_submission("session"), (True, ""))
            self.assertEqual(second.claim_minutes_submission("session"), (False, ""))
            first.finish_minutes_submission("session", minutes_url="https://tenant/minutes/token")
            self.assertEqual(
                second.claim_minutes_submission("session"),
                (False, "https://tenant/minutes/token"),
            )


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
        manifests = []

        def capture_manifest(command, **_kwargs):
            manifests.append(Path(command[command.index("-i") + 1]).read_text(encoding="utf-8"))
            result = unittest.mock.Mock()
            result.returncode = 0
            return result

        run.side_effect = capture_manifest
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
        self.assertEqual(len(manifests[0].splitlines()), 2)
        self.assertNotIn("\\nfile", manifests[0])

    def test_cleanup_removes_only_uploaded_session_recordings(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = folder / "session_000.mp4"
            second = folder / "session_001.mp4"
            complete = folder / "session_00.mp4"
            current_recording = folder / "current_000.mp4"
            for path in (first, second, complete, current_recording):
                path.write_bytes(b"data")
            cleanup_uploaded_recordings([first, second], complete, 4)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertFalse(complete.exists())
            self.assertTrue(current_recording.exists())

    def test_cleanup_preserves_files_when_verified_size_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            segment = folder / "segment.mp4"
            complete = folder / "complete.mp4"
            segment.write_bytes(b"segment")
            complete.write_bytes(b"video")
            with self.assertRaisesRegex(RecordingIntegrityError, "删除前校验失败"):
                cleanup_uploaded_recordings([segment], complete, 999)
            self.assertTrue(segment.exists())
            self.assertTrue(complete.exists())

    def test_short_or_damaged_recording_is_marked_partial(self):
        status, note = recording_integrity_result(
            [VideoMetadata(100, 7200)], [Path("broken_008.mp4")], 12_480,
        )
        self.assertEqual(status, "部分录制")
        self.assertIn("broken_008.mp4", note)
        self.assertIn("缺失约 88.0 分钟", note)

    def test_changing_file_is_rejected_before_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "recording.mp4"
            video.write_bytes(b"before")
            with patch("live_digest_service.time.sleep", side_effect=lambda _seconds: video.write_bytes(b"after-data")):
                with self.assertRaisesRegex(RecordingIntegrityError, "仍在写入"):
                    stable_file_sizes([video])

    @patch("live_digest_service.drive_download_size", return_value=99)
    def test_remote_size_must_equal_local_size(self, _size):
        with self.assertRaisesRegex(RecordingIntegrityError, "本地 100 字节，云端 99 字节"):
            verify_drive_file_size(Settings(Path("."), Path("."), ""), "token", 100)

    @patch("live_digest_service.shutil.disk_usage")
    def test_merge_waits_when_disk_cannot_hold_a_complete_copy(self, disk_usage):
        with tempfile.TemporaryDirectory() as directory:
            segment = Path(directory) / "segment.mp4"
            segment.write_bytes(b"x" * 1024)
            disk_usage.return_value = unittest.mock.Mock(free=1024)
            with self.assertRaises(LowDiskSpaceError):
                ensure_merge_space([segment], Path(directory), reserve_bytes=1024)

    @patch("live_digest_service.subprocess.run")
    def test_existing_complete_video_is_reused_only_when_readable(self, run):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "complete.mp4"
            video.write_bytes(b"x" * 1024)
            run.return_value = unittest.mock.Mock(returncode=0, stdout="123.4\n")
            self.assertTrue(video_is_readable(video))
            run.return_value = unittest.mock.Mock(returncode=1, stdout="")
            self.assertFalse(video_is_readable(video))

    @patch("live_digest_service.user_feishu_request")
    @patch("live_digest_service.user_token", return_value="token")
    @patch("live_digest_service.requests.post")
    @patch("live_digest_service.verify_drive_file_size")
    def test_drive_upload_retries_a_transient_part_failure(self, verify_size, post, _token, request):
        request.side_effect = [
            {"data": {"upload_id": "upload", "block_size": 4, "block_num": 1}},
            {"data": {"file_token": "file-token"}},
        ]
        success = unittest.mock.Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"code": 0, "data": {}}
        post.side_effect = [__import__("requests").exceptions.SSLError("temporary"), success]
        with tempfile.TemporaryDirectory() as directory, patch("live_digest_service.time.sleep"):
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"data")
            self.assertEqual(upload_drive_file(Settings(Path("."), Path("."), ""), video, "folder"), "file-token")
        self.assertEqual(post.call_count, 2)
        _, kwargs = post.call_args
        self.assertEqual(post.call_args.args[0], "https://open.feishu.cn/open-apis/drive/v1/files/upload_part")
        self.assertEqual(kwargs["data"], {
            "upload_id": "upload", "seq": "0", "size": "4", "checksum": "67109275",
        })
        self.assertNotIn("params", kwargs)
        verify_size.assert_called_once_with(unittest.mock.ANY, "file-token", 4)

    @patch("live_digest_service.user_feishu_request")
    @patch("live_digest_service.user_token", return_value="token")
    @patch("live_digest_service.requests.post")
    def test_drive_upload_stops_when_local_file_changes(self, post, _token, request):
        request.return_value = {"data": {"upload_id": "upload", "block_size": 4, "block_num": 1}}
        success = unittest.mock.Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"code": 0, "data": {}}
        with tempfile.TemporaryDirectory() as directory, patch("live_digest_service.time.sleep"):
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"data")

            def mutate_after_part(*_args, **_kwargs):
                video.write_bytes(b"changed")
                return success

            post.side_effect = mutate_after_part
            with self.assertRaisesRegex(RecordingIntegrityError, "上传期间发生变化"):
                upload_drive_file(Settings(Path("."), Path("."), ""), video, "folder")
        self.assertEqual(request.call_count, 1)

    @patch("live_digest_service.verify_drive_file_size")
    @patch("live_digest_service.user_feishu_request")
    @patch("live_digest_service.user_token", return_value="token")
    @patch("live_digest_service.requests.post")
    def test_drive_upload_retry_reuses_token_after_verification_failure(
        self, post, _token, request, verify_size,
    ):
        request.side_effect = [
            {"data": {"upload_id": "upload", "block_size": 4, "block_num": 1}},
            {"data": {"file_token": "file-token"}},
        ]
        verify_size.side_effect = [RecordingIntegrityError("云端大小查询失败"), None]
        success = unittest.mock.Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"code": 0, "data": {}}
        post.return_value = success
        with tempfile.TemporaryDirectory() as directory, patch("live_digest_service.time.sleep"):
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"data")
            with self.assertRaisesRegex(RecordingIntegrityError, "云端大小查询失败"):
                upload_drive_file(Settings(Path("."), Path("."), ""), video, "folder")
            self.assertEqual(
                upload_drive_file(Settings(Path("."), Path("."), ""), video, "folder"), "file-token",
            )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(verify_size.call_count, 2)

    @patch("live_digest_service.user_feishu_request")
    @patch("live_digest_service.user_token", return_value="token")
    @patch("live_digest_service.requests.post")
    @patch("live_digest_service.verify_drive_file_size")
    def test_drive_upload_resumes_only_missing_parts(self, _verify, post, _token, request):
        request.side_effect = [
            {"data": {"upload_id": "upload", "block_size": 4, "block_num": 2}},
            {"data": {"file_token": "file-token"}},
        ]
        success = unittest.mock.Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"code": 0, "data": {}}
        def first_attempt(*_args, **kwargs):
            if kwargs["data"]["seq"] == "0":
                return success
            raise __import__("requests").exceptions.SSLError("offline")

        post.side_effect = first_attempt
        with tempfile.TemporaryDirectory() as directory, patch("live_digest_service.time.sleep"):
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"12345678")
            with self.assertRaises(__import__("requests").exceptions.SSLError):
                upload_drive_file(Settings(Path("."), Path("."), ""), video, "folder")
            post.reset_mock()
            post.side_effect = [success]
            self.assertEqual(upload_drive_file(Settings(Path("."), Path("."), ""), video, "folder"), "file-token")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["data"]["seq"], "1")
            self.assertFalse((video.parent / f".{video.name}.feishu-upload.json").exists())

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
    def test_session_artifacts_write_minutes_and_docx_links(self, update):
        attach_session_artifacts(
            Settings(Path("."), Path("."), ""), "record",
            minutes_url="https://shenyidushu.feishu.cn/minutes/video",
            transcript_url="https://shenyidushu.feishu.cn/docx/transcript",
            summary_url="https://shenyidushu.feishu.cn/docx/summary",
        )
        fields = update.call_args.args[2]
        self.assertEqual(fields["录制视频链接"]["link"], "https://shenyidushu.feishu.cn/minutes/video")
        self.assertEqual(fields["智能纪要链接"]["link"], "https://shenyidushu.feishu.cn/docx/summary")
        self.assertEqual(fields["文字记录链接"]["link"], "https://shenyidushu.feishu.cn/docx/transcript")
        self.assertNotIn("截图", fields)

    def test_minutes_documents_are_matched_by_type_title_and_creation_time(self):
        files = [
            {"name": "文字记录：直播视频-账号-时间 2026-08-27", "type": "docx",
             "created_time": "200", "url": "https://tenant/docx/transcript"},
            {"name": "智能纪要：直播视频-账号-时间 2026年8月27日", "type": "docx",
             "created_time": "201", "url": "https://tenant/docx/summary"},
            {"name": "文字记录：直播视频-账号-时间 old", "type": "docx",
             "created_time": "99", "url": "https://tenant/docx/old"},
            {"name": "智能纪要：直播视频-账号-时间 fake", "type": "file",
             "created_time": "202", "url": "https://tenant/file/fake"},
        ]
        transcript, summary = find_minutes_documents(
            files, title="直播视频-账号-时间", created_at_ms=100_000,
        )
        self.assertEqual(transcript["url"], "https://tenant/docx/transcript")
        self.assertEqual(summary["url"], "https://tenant/docx/summary")

    def test_drive_file_link_is_clickable(self):
        self.assertEqual(drive_file_url("file-token"), "https://shenyidushu.feishu.cn/file/file-token")

    @patch("live_digest_service.user_feishu_request")
    def test_minutes_token_is_derived_from_returned_url(self, request):
        request.return_value = {"code": 0, "data": {
            "minute_url": "https://shenyidushu.feishu.cn/minutes/minute-token",
        }}
        from live_digest_service import upload_minutes
        self.assertEqual(
            upload_minutes(Settings(Path("."), Path("."), ""), "file-token"),
            ("minute-token", "https://shenyidushu.feishu.cn/minutes/minute-token"),
        )

    def test_completion_message_contains_all_three_finished_assets(self):
        message = recording_complete_message(
            "示例账号", "20260826_102012", "https://video", "https://transcript", "https://minutes",
        )
        self.assertEqual(message, (
            "【直播录制完成提醒】\n"
            "“示例账号”在“2026年8月26日 10点20”的直播录制已完成，请查收。\n"
            "1.录制视频：\nhttps://video\n"
            "2.文字记录：\nhttps://transcript\n"
            "3.智能纪要：\nhttps://minutes"
        ))

    def test_completion_post_uses_numbered_feishu_generated_links(self):
        post = recording_complete_post(
            "示例账号", "20260826_102012", "https://minutes/video", "https://docx/transcript",
            "https://docx/summary",
        )
        labels = [line[0]["text"] for line in post["zh_cn"]["content"] if line[0]["tag"] == "text"]
        link_labels = [line[0]["text"] for line in post["zh_cn"]["content"] if line[0]["tag"] == "a"]
        self.assertEqual(labels[-3:], ["1.录制视频：", "2.文字记录：", "3.智能纪要："])
        self.assertEqual(link_labels, [
            "https://minutes/video", "https://docx/transcript", "https://docx/summary",
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

    @patch("live_digest_service.feishu_response_data", return_value={"data": {"message_id": "message"}})
    @patch("live_digest_service.requests.post")
    @patch("live_digest_service.tenant_token", return_value="token")
    def test_completed_notification_retry_does_not_send_again(self, _token, post, _response):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "state.sqlite3")
            settings = Settings(Path("."), Path("."), "")
            recipients = [{"id_type": "open_id", "id": "ou_recipient", "name": "管理员"}]
            content = {"zh_cn": {"content": []}}
            send_post(
                settings, content, "fallback", recipients=recipients, session_id="session",
                message_type="recording_complete", ledger=ledger,
            )
            send_post(
                settings, content, "fallback", recipients=recipients, session_id="session",
                message_type="recording_complete", ledger=ledger,
            )
        self.assertEqual(post.call_count, 1)

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
    @patch("live_digest_service.cleanup_uploaded_recordings")
    @patch("live_digest_service.video_is_readable", return_value=False)
    @patch("live_digest_service.publish_finished_session", side_effect=[RuntimeError("network"), None])
    @patch("live_digest_service.wait_for_minutes_documents", return_value={
        "minutes_url": "https://minutes", "minutes_title": "video", "minutes_created_at": 123,
        "transcript_url": "https://docx/transcript", "summary_url": "https://docx/summary",
    })
    @patch("live_digest_service.upload_minutes", return_value=("minute-token", "https://minutes"))
    @patch("live_digest_service.upload_drive_file", return_value="video-token")
    @patch("live_digest_service.drive_file_url", return_value="https://archive")
    @patch("live_digest_service.session_drive_folder", return_value="folder")
    @patch("live_digest_service.concat_segments")
    @patch("live_digest_service.ensure_merge_space")
    @patch("live_digest_service.verify_drive_file_size")
    @patch("live_digest_service.verify_merged_video", return_value=VideoMetadata(123, 60))
    @patch("live_digest_service.inspect_recording_segments", return_value=(
        [Path("segment.mp4")], [VideoMetadata(123, 60)], [],
    ))
    def test_notification_retry_reuses_finished_artifacts(self, _inspect, _merged, _verify, _space, _concat, _folder, _url, uploads,
                                                          _minutes, _documents, _publish, _readable, cleanup,
                                                          _attach, _update):
        with tempfile.TemporaryDirectory() as directory:
            room = Path(directory)
            ledger = DeliveryLedger(room / "state.sqlite3")
            settings = Settings(Path("."), room, "")
            segment = room / "segment.mp4"
            segment.touch()
            args = dict(room_dir=room, segments=[segment], account_name="示例账号", session_id="20260826_100012", record_id="record", title="", url="", recipients=[], ledger=ledger)
            with self.assertRaises(CompletionNotificationError):
                complete_with_feishu_minutes(settings, **args)
            complete_with_feishu_minutes(settings, **args)
            self.assertEqual(uploads.call_count, 1)
            self.assertEqual(cleanup.call_count, 2)

    @patch("live_digest_service.publish_finished_session")
    @patch("live_digest_service.update_live_record")
    @patch("live_digest_service.attach_session_artifacts")
    @patch("live_digest_service.wait_for_minutes_documents", return_value={
        "minutes_url": "https://tenant/minutes/minute-token", "minutes_title": "video",
        "minutes_created_at": 123, "transcript_url": "https://docx/transcript",
        "summary_url": "https://docx/summary",
    })
    @patch("live_digest_service.upload_minutes")
    @patch("live_digest_service.cleanup_uploaded_recordings")
    def test_minutes_checkpoint_is_reused_without_creating_a_duplicate(
        self, _cleanup, minutes, _wait, _attach, _update, _publish,
    ):
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "state.sqlite3")
            ledger.save_session_artifacts(
                "session", archive_video_url="https://drive/file/video",
                minutes_url="https://tenant/minutes/minute-token", video_name="video.mp4",
            )
            complete_with_feishu_minutes(
                Settings(Path("."), Path(directory), ""), room_dir=Path(directory), segments=[],
                account_name="账号", session_id="session", record_id="record", title="", url="",
                recipients=[], ledger=ledger,
            )
        minutes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
