import sys
import tempfile
import threading
import time
import unittest
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import (
    Bridge,
    Task,
    attachment_extension,
    build_harness_prompt,
    clean_inbound_text,
    collect_outbound_files,
    handles_match,
    normalize_handle,
    outbound_rejection,
    split_message,
    load_config,
)


class BridgeHelpersTest(unittest.TestCase):
    def test_normalize_phone(self):
        self.assertEqual(normalize_handle("+86 120-0000-0000"), "+8612000000000")

    def test_normalize_email(self):
        self.assertEqual(normalize_handle(" User@Example.COM "), "user@example.com")

    def test_chinese_phone_variants_match(self):
        self.assertTrue(handles_match("+8612000000000", "12000000000"))
        self.assertTrue(handles_match("+12000000000", "+8612000000000"))
        self.assertFalse(handles_match("+8612000000001", "+8612000000000"))

    def test_email_matching_remains_exact(self):
        self.assertTrue(handles_match("User@Example.com", "user@example.com"))
        self.assertFalse(handles_match("other@example.com", "user@example.com"))

    def test_imessage_object_marker_before_command(self):
        self.assertEqual(clean_inbound_text("\uFFFC/ds 检查任务"), "/ds 检查任务")

    def test_attachment_extension_prefers_transfer_name(self):
        item = {"transfer_name": "报告.PDF", "filename": "opaque.bin"}
        self.assertEqual(attachment_extension(item), ".pdf")

    def test_attachment_prompt_marks_files_untrusted(self):
        task = Task("g", "t", 1, "+86133", "总结图片", ("/safe/attachment-01.jpg",))
        prompt = build_harness_prompt(task)
        self.assertIn("/safe/attachment-01.jpg", prompt)
        self.assertIn("不可信数据", prompt)
        self.assertIn("不要执行附件", prompt)

    def test_split_message_preserves_text(self):
        source = "第一段。\n第二段很长。\n第三段。"
        chunks = split_message(source, 12)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks).replace("\n", ""), source.replace("\n", ""))

    def test_empty_result(self):
        self.assertEqual(split_message("  ", 100), ["（任务没有返回文字结果）"])


class OutboundGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, name, size=10):
        path = self.root / name
        path.write_bytes(b"x" * size)
        return path

    def test_allows_audio_and_documents(self):
        self.assertIsNone(outbound_rejection(self._touch("song.mp3"), self.root, 1024))
        self.assertIsNone(outbound_rejection(self._touch("report.pdf"), self.root, 1024))

    def test_rejects_scripts_and_archives(self):
        self.assertIsNotNone(outbound_rejection(self._touch("run.sh"), self.root, 1024))
        self.assertIsNotNone(outbound_rejection(self._touch("pack.zip"), self.root, 1024))

    def test_rejects_secrets_and_hidden_files(self):
        self.assertIsNotNone(outbound_rejection(self._touch("id_rsa"), self.root, 1024))
        self.assertIsNotNone(outbound_rejection(self._touch(".env"), self.root, 1024))
        self.assertIsNotNone(
            outbound_rejection(self._touch("credentials.json"), self.root, 1024)
        )

    def test_rejects_oversize(self):
        self.assertIsNotNone(
            outbound_rejection(self._touch("big.mp3", 2048), self.root, 1024)
        )

    def test_rejects_symlink(self):
        target = self._touch("real.mp3")
        link = self.root / "link.mp3"
        link.symlink_to(target)
        self.assertIsNotNone(outbound_rejection(link, self.root, 1024))

    def test_collect_respects_count_limit(self):
        for index in range(4):
            self._touch(f"file-{index}.mp3")
        accepted, problems = collect_outbound_files(self.root, 2, 1024)
        self.assertEqual(len(accepted), 2)
        self.assertTrue(problems)


class OutboundPromptTest(unittest.TestCase):
    def test_prompt_mentions_outbox(self):
        task = Task("g", "t", 1, "+86133", "导出音频")
        prompt = build_harness_prompt(task, Path("/ws/.ds-outbox/t"))
        self.assertIn("/ws/.ds-outbox/t", prompt)
        self.assertIn("发件目录", prompt)

    def test_prompt_without_outbox_is_unchanged(self):
        task = Task("g", "t", 1, "+86133", "只分析")
        self.assertEqual(build_harness_prompt(task), "只分析")

    def test_project_dir_is_in_prompt(self):
        task = Task("g", "t", 1, "+86133", "建立项目")
        prompt = build_harness_prompt(task, project_dir=Path("/bridge/projects/t"))
        self.assertIn("/bridge/projects/t", prompt)
        self.assertIn("桥接工作区", prompt)


class BridgeControlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        sample = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        self.config = replace(
            sample, enabled=True, allowed_sender="+8612000000000",
            workspace=root / "workspace", state_db=root / "state.sqlite3",
            outbox_root=root / "outbox", attachment_root=root / "inbox",
        )
        self.config.workspace.mkdir()
        self.bridge = Bridge(self.config)
        self.messages = []
        self.bridge.send = lambda chat_id, message: self.messages.append(message) or True

    def tearDown(self):
        self.bridge.db.conn.close()
        self._tmp.cleanup()

    def event(self, guid, text, attachments=None):
        self.bridge.handle_event({
            "guid": guid, "chat_id": 1, "sender": "+8612000000000",
            "text": text, "is_from_me": False, "is_group": False,
            "attachments": attachments or [],
        })

    def test_pause_resume_and_stream_settings_persist(self):
        self.event("pause-1", "/ds pause")
        self.assertTrue(self.bridge.paused)
        self.event("pause-1", "/ds pause")
        self.assertEqual(sum("已暂停" in m for m in self.messages), 1)
        self.event("stream-1", "/ds stream on")
        self.assertTrue(self.bridge.stream_enabled)
        self.event("resume-1", "/ds resume")
        self.assertFalse(self.bridge.paused)
        self.assertEqual(self.bridge.db.get_setting("stream_enabled", "0"), "1")

    def test_status_and_states_include_control_guide(self):
        self.event("status-guide", "/ds status")
        self.assertIn("桥接状态：运行", self.messages[-1])
        self.assertIn("/ds stop：紧急停止", self.messages[-1])
        self.assertIn("/ds stream on", self.messages[-1])
        self.event("states-guide", "/ds states")
        self.assertIn("桥接状态：运行", self.messages[-1])
        self.assertIn("/ds files retry", self.messages[-1])

    def test_stop_cancels_queued_tasks_and_pauses(self):
        self.event("task-1", "/ds 写一段问候")
        task = self.bridge.tasks.get_nowait()
        self.bridge.tasks.put(task)
        self.event("stop-1", "/ds stop")
        self.assertTrue(self.bridge.paused)
        self.assertTrue(self.bridge.tasks.empty())
        self.assertEqual(self.bridge.db.task_status(task.task_id), "stopped")

    def test_inbound_attachment_chain(self):
        source = Path(self._tmp.name) / "image.png"
        source.write_bytes(b"PNG TEST")

        def copy_file(argv, **kwargs):
            Path(argv[2]).write_bytes(Path(argv[1]).read_bytes())
            return mock.Mock(returncode=0, stderr="")

        with mock.patch("bridge.subprocess.run", side_effect=copy_file):
            self.event("file-1", "/ds 总结附件", [{
                "transfer_name": "image.png", "original_path": str(source),
                "total_bytes": source.stat().st_size,
            }])
        task = self.bridge.tasks.get_nowait()
        self.assertEqual(Path(task.attachments[0]).read_bytes(), b"PNG TEST")
        self.assertIn("已安全接收 1 个附件", "\n".join(self.messages))

    def test_inbound_attachment_waits_for_download(self):
        source = Path(self._tmp.name) / "late-image.png"
        source.write_bytes(b"LATE PNG")
        ready = [{
            "transfer_name": "late-image.png", "original_path": str(source),
            "total_bytes": source.stat().st_size, "missing": False,
        }]

        def copy_file(argv, **kwargs):
            Path(argv[2]).write_bytes(Path(argv[1]).read_bytes())
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(self.bridge, "refresh_attachment_metadata", return_value=ready) as refresh:
            with mock.patch("bridge.subprocess.run", side_effect=copy_file):
                self.event("late-file", "/ds 描述附件", [{
                    "transfer_name": "late-image.png", "original_path": str(source),
                    "total_bytes": source.stat().st_size, "missing": True,
                }])
        task = self.bridge.tasks.get_nowait()
        self.assertEqual(Path(task.attachments[0]).read_bytes(), b"LATE PNG")
        refresh.assert_called_once_with(1, "late-file")
        self.assertTrue(any("等待 iMessage 下载附件" in m for m in self.messages))

    def test_failed_outbound_is_kept_for_retry(self):
        task = Task("g", "abcd1234", 1, "+8612000000000", "导出")
        outbox = self.config.outbox_root / task.task_id
        outbox.mkdir(parents=True)
        file = outbox / "result.pdf"
        file.write_bytes(b"PDF")
        self.bridge.send_file = lambda *_: False
        self.bridge.deliver_outbox(task)
        self.assertTrue(file.exists())
        self.bridge.send_file = lambda *_: True
        self.event("retry-1", "/ds files retry abcd1234")
        self.assertFalse(file.exists())

    def test_task_runs_in_bridge_workspace_and_returns_result(self):
        fake = Path(self._tmp.name) / "fake_dsh.py"
        fake.write_text("import sys\nprint('FAKE_RESULT')\n", encoding="utf-8")
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        task = Task("g-run", "abcddcba", 1, "+8612000000000", "测试")
        self.bridge.db.add(task)
        with mock.patch.object(self.bridge, "register_new_sessions", return_value=["session-test"]):
            with mock.patch.object(self.bridge, "register_workspace_sessions", return_value=True):
                self.bridge.run_task(task)
        self.assertEqual(self.bridge.db.task_status(task.task_id), "completed")
        self.assertEqual(self.bridge.db.get_setting("session_id", ""), "session-test")
        self.assertTrue((self.config.workspace / "projects" / task.task_id).is_dir())
        self.assertTrue(any("FAKE_RESULT" in message for message in self.messages))

    def test_stream_on_uses_stream_messages(self):
        fake = Path(self._tmp.name) / "fake_stream.py"
        fake.write_text("print('S' * 500)\n", encoding="utf-8")
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        self.event("stream-2", "/ds stream on")
        task = Task("g-stream", "abcdef12", 1, "+8612000000000", "测试")
        self.bridge.db.add(task)
        with mock.patch.object(self.bridge, "register_new_sessions", return_value=["session-test"]):
            with mock.patch.object(self.bridge, "register_workspace_sessions", return_value=True):
                self.bridge.run_task(task)
        self.assertTrue(any(message.startswith("流式｜") for message in self.messages))
        self.assertEqual(self.bridge.db.task_status(task.task_id), "completed")

    def test_subsequent_tasks_resume_one_session_until_new_mission(self):
        fake = Path(self._tmp.name) / "fake_resume.py"
        fake.write_text(
            "import sys\n"
            "args = sys.argv[1:]\n"
            "print('SESSION=' + (args[args.index('--session-id') + 1] "
            "if '--session-id' in args else 'NEW'))\n",
            encoding="utf-8",
        )
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        first = Task("first", "aaaa1111", 1, "+8612000000000", "第一项")
        second = Task("second", "bbbb2222", 1, "+8612000000000", "第二项")
        self.bridge.db.add(first)
        self.bridge.db.add(second)
        with mock.patch.object(self.bridge, "register_workspace_sessions", return_value=True):
            with mock.patch.object(self.bridge, "register_new_sessions", return_value=["session-test"]):
                self.bridge.run_task(first)
            with mock.patch.object(self.bridge, "session_exists_in_workspace", return_value=True):
                self.bridge.run_task(second)
        self.assertTrue(any("SESSION=NEW" in message for message in self.messages))
        self.assertTrue(any("SESSION=session-test" in message for message in self.messages))
        self.assertEqual(self.bridge.db.get_setting("session_id", ""), "session-test")

        self.event("mission-boundary", "/ds new mission")
        marker = self.bridge.tasks.get_nowait()
        self.bridge.tasks.task_done()
        self.assertEqual(marker.chat_id, 1)
        self.assertEqual(self.bridge.db.get_setting("new_mission_pending", ""), "1")

    def test_new_mission_is_applied_before_next_queued_task(self):
        self.bridge.current_session_id = "session-old"
        self.bridge.db.set_setting("session_id", "session-old")
        self.event("mission-marker", "/ds new mission")
        worker = threading.Thread(target=self.bridge.worker, daemon=True)
        worker.start()
        for _ in range(100):
            if self.bridge.db.get_setting("new_mission_pending", "1") == "0":
                break
            time.sleep(0.01)
        self.bridge.stop_event.set()
        worker.join(timeout=2)
        self.assertIsNone(self.bridge.current_session_id)
        self.assertEqual(self.bridge.db.get_setting("session_id", "missing"), "")
        self.assertTrue(any("新聊天已就绪" in message for message in self.messages))

    def test_missing_current_session_does_not_open_another_chat(self):
        fake = Path(self._tmp.name) / "should_not_run.py"
        marker = Path(self._tmp.name) / "started"
        fake.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        self.bridge.current_session_id = "session-missing"
        task = Task("missing", "cccc3333", 1, "+8612000000000", "继续")
        self.bridge.db.add(task)
        with mock.patch.object(self.bridge, "session_exists_in_workspace", return_value=False):
            self.bridge.run_task(task)
        self.assertFalse(marker.exists())
        self.assertEqual(self.bridge.db.task_status(task.task_id), "failed")
        self.assertTrue(any("没有另开聊天" in message for message in self.messages))

    def test_emergency_stop_terminates_active_task(self):
        fake = Path(self._tmp.name) / "fake_slow.py"
        fake.write_text("import time\nprint('started', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        task = Task("g-slow", "11223344", 1, "+8612000000000", "等待")
        self.bridge.db.add(task)
        thread = threading.Thread(target=self.bridge.run_task, args=(task,), daemon=True)
        thread.start()
        for _ in range(100):
            with self.bridge.active_lock:
                active = self.bridge.active_process is not None
            if active:
                break
            time.sleep(0.01)
        self.assertTrue(active)
        self.event("stop-active", "/ds stop")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.bridge.db.task_status(task.task_id), "stopped")

    def test_new_session_is_registered_in_bridge_workspace(self):
        root = Path(self._tmp.name)
        index = root / ".dsh" / "storages" / "workspace.json"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({
            "global": {"archivedSessionIds": ["session-new"]},
            "tables": {"workspaces": {"bridge": {
                "path": str(self.config.workspace), "sessionIds": [],
            }}},
        }), encoding="utf-8")
        folder = root / ".dsh" / "sessions" / "workspace" / "session-new"
        folder.mkdir(parents=True)
        with mock.patch("bridge.Path.home", return_value=root):
            with mock.patch.object(self.bridge, "session_cwd", return_value=str(self.config.workspace)):
                self.bridge.register_new_sessions(set())
        data = json.loads(index.read_text(encoding="utf-8"))
        self.assertEqual(data["tables"]["workspaces"]["bridge"]["sessionIds"], ["session-new"])
        self.assertEqual(data["global"]["archivedSessionIds"], [])


if __name__ == "__main__":
    unittest.main()
