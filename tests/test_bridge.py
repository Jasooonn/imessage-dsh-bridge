import sys
import io
import hashlib
import os
import subprocess
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
    APPROVAL_PREFIX,
    HIGH_RISK,
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
        self.assertEqual(normalize_handle("+86 138-0013-8000"), "+8613800138000")

    def test_normalize_email(self):
        self.assertEqual(normalize_handle(" User@Example.COM "), "user@example.com")

    def test_chinese_phone_variants_match(self):
        self.assertTrue(handles_match("+8613800138000", "13800138000"))
        self.assertTrue(handles_match("008613800138000", "+8613800138000"))
        self.assertFalse(handles_match("+13800138000", "+8613800138000"))
        self.assertFalse(handles_match("+4413800138000", "+8613800138000"))
        self.assertFalse(handles_match("+8613800138001", "+8613800138000"))

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

    def test_risk_filter_avoids_common_programming_terms(self):
        for task in ("格式化 JSON", "解释 format 函数", "做一个 token 解析器",
                     "构建发消息队列", "说明 sudo 的作用", "API key 轮换策略"):
            self.assertIsNone(HIGH_RISK.search(task), task)
        for task in ("格式化系统盘", "执行 sudo diskutil list", "读取登录密码", "帮我抢票"):
            self.assertIsNotNone(HIGH_RISK.search(task), task)

    def test_dsh_plugin_waits_for_matching_one_shot_decision(self):
        plugin = Path(__file__).resolve().parents[1] / "dsh_approval.mjs"
        program = (
            "import {apply} from " + json.dumps(plugin.as_uri()) + ";"
            "let answer; apply({on(_name, callback){answer=callback}});"
            "const session={id:'session-a',seq:1,eventAt(){return {type:'tool/call',"
            "data:{callId:'call-a',name:'bash',arguments:'{\"command\":\"pwd\"}'}}}};"
            "answer({agent:{session},callId:'call-a',toolName:'bash'})"
            ".then(value=>console.log('OUTCOME='+value));"
        )
        proc = subprocess.Popen(
            ["node", "--input-type=module", "-e", program],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, "DS_BRIDGE_APPROVAL": "1"},
        )
        try:
            assert proc.stdout is not None and proc.stdin is not None
            request_line = proc.stdout.readline()
            self.assertTrue(request_line.startswith(APPROVAL_PREFIX))
            request = json.loads(request_line[len(APPROVAL_PREFIX):])
            self.assertEqual(request["arguments"], '{"command":"pwd"}')
            proc.stdin.write(json.dumps({"type": "ds-approval-decision",
                                         "id": request["id"],
                                         "outcome": "allowed-once"}) + "\n")
            proc.stdin.flush()
            self.assertEqual(proc.stdout.readline().strip(), "OUTCOME=allowed-once")
            self.assertEqual(proc.wait(timeout=3), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            if proc.stdin: proc.stdin.close()
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()


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
            sample, enabled=True, allowed_sender="+8613800138000",
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
            "guid": guid, "chat_id": 1, "sender": "+8613800138000",
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

    def test_existing_outbox_keeps_file_return_instructions(self):
        outbox = self.config.outbox_root / "abcd1234"
        outbox.mkdir(parents=True)
        (outbox / "pending.txt").write_text("keep", encoding="utf-8")
        self.assertEqual(self.bridge.prepare_outbox("abcd1234"), outbox)
        task = Task("g", "abcd1234", 1, self.config.allowed_sender, "导出")
        prompt = build_harness_prompt(task, outbox)
        self.assertIn("发件目录", prompt)
        self.assertIn("回传文件给用户", prompt)
        self.assertEqual((outbox / "pending.txt").read_text(), "keep")

    def test_pending_confirm_can_reissue_code(self):
        self.event("risky-1", "/ds 格式化系统盘")
        task_id = hashlib.sha256(b"risky-1").hexdigest()[:8]
        self.assertEqual(self.bridge.db.pending_confirmations(1), [task_id])
        self.event("pending-1", "/ds pending")
        self.assertTrue(any(task_id in message for message in self.messages))
        before = self.bridge.db.conn.execute(
            "SELECT confirm_hash FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        self.event("resend-1", f"/ds confirm resend {task_id}")
        after = self.bridge.db.conn.execute(
            "SELECT confirm_hash FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        self.assertNotEqual(before, after)

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
        task = Task("g", "abcd1234", 1, "+8613800138000", "导出")
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
        task = Task("g-run", "abcddcba", 1, "+8613800138000", "测试")
        self.bridge.db.add(task)
        with mock.patch.object(self.bridge, "register_new_sessions", return_value=["session-test"]):
            with mock.patch.object(self.bridge, "register_workspace_sessions", return_value=True):
                self.bridge.run_task(task)
        self.assertEqual(self.bridge.db.task_status(task.task_id), "completed")
        self.assertEqual(self.bridge.db.get_setting("session_id", ""), "session-test")
        self.assertTrue((self.config.workspace / "projects" / task.task_id).is_dir())
        self.assertTrue(any("FAKE_RESULT" in message for message in self.messages))

    def test_dash_prefixed_prompt_is_after_option_terminator(self):
        fake = Path(self._tmp.name) / "fake_argv.py"
        fake.write_text("import json,sys\nprint(json.dumps(sys.argv[1:],ensure_ascii=False))\n",
                        encoding="utf-8")
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        task = Task("argv", "defa1234", 1, self.config.allowed_sender, "--help")
        self.bridge.db.add(task)
        with mock.patch.object(self.bridge, "register_new_sessions", return_value=["session-test"]), \
             mock.patch.object(self.bridge, "register_workspace_sessions", return_value=True):
            self.bridge.run_task(task)
        result = next(message for message in self.messages if message.startswith("结果 1/"))
        args = json.loads(result.split("\n", 1)[1])
        self.assertEqual(args[-2], "--")
        self.assertTrue(args[-1].startswith("--help\n"))

    def test_unlinked_session_still_returns_result_and_blocks_next_task(self):
        fake = Path(self._tmp.name) / "fake_unlinked.py"
        fake.write_text("print('RESULT_SURVIVES')\n", encoding="utf-8")
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        first = Task("lost", "defa5678", 1, self.config.allowed_sender, "测试")
        second = Task("later", "defa5679", 1, self.config.allowed_sender, "继续")
        self.bridge.db.add(first)
        self.bridge.db.add(second)
        with mock.patch.object(self.bridge, "register_new_sessions", return_value=[]):
            self.bridge.run_task(first)
        self.assertEqual(self.bridge.db.task_status(first.task_id), "completed_unlinked")
        self.assertTrue(any("RESULT_SURVIVES" in message for message in self.messages))
        self.assertEqual(self.bridge.db.get_setting("session_link_failed", "0"), "1")
        self.bridge.run_task(second)
        self.assertEqual(self.bridge.db.task_status(second.task_id), "failed")

    def test_workspace_index_without_global_is_backed_up_and_repaired(self):
        home = Path(self._tmp.name) / "home"
        index = home / ".dsh" / "storages" / "workspace.json"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({"tables": {"workspaces": {"one": {
            "path": str(self.config.workspace), "sessionIds": []}}}}), encoding="utf-8")
        with mock.patch("bridge.Path.home", return_value=home):
            self.assertTrue(self.bridge.register_workspace_sessions(["session-abc123"]))
        result = json.loads(index.read_text(encoding="utf-8"))
        self.assertIn("session-abc123", result["tables"]["workspaces"]["one"]["sessionIds"])
        self.assertIn("global", result)
        self.assertEqual(len(list(index.parent.glob("workspace.bridge-backup-*.json"))), 1)

    def test_stream_on_uses_stream_messages(self):
        fake = Path(self._tmp.name) / "fake_stream.py"
        fake.write_text("print('S' * 500)\n", encoding="utf-8")
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        self.event("stream-2", "/ds stream on")
        task = Task("g-stream", "abcdef12", 1, "+8613800138000", "测试")
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
        first = Task("first", "aaaa1111", 1, "+8613800138000", "第一项")
        second = Task("second", "bbbb2222", 1, "+8613800138000", "第二项")
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
        task = Task("missing", "cccc3333", 1, "+8613800138000", "继续")
        self.bridge.db.add(task)
        with mock.patch.object(self.bridge, "session_exists_in_workspace", return_value=False):
            self.bridge.run_task(task)
        self.assertFalse(marker.exists())
        self.assertEqual(self.bridge.db.task_status(task.task_id), "failed")
        self.assertTrue(any("没有另开聊天" in message for message in self.messages))

    def test_approval_is_one_shot_and_bound_to_active_chat(self):
        request_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        session_id = "session-11111111-2222-4333-8444-555555555555"
        class FakeProcess:
            stdin = io.BytesIO()
            def poll(self):
                return None
        process = FakeProcess()
        self.bridge.active_process = process
        self.bridge.active_task_id = "abcd1234"
        self.bridge.current_session_id = session_id
        task = Task("g", "abcd1234", 1, self.config.allowed_sender, "测试")
        event = {"id": request_id, "sessionId": session_id, "callId": "call-1",
                 "tool": "bash", "arguments": '{"command":"touch /tmp/approved-test"}'}
        self.bridge.handle_approval_request(APPROVAL_PREFIX + json.dumps(event), task)
        self.assertIsNotNone(self.bridge.pending_approval)
        self.assertTrue(any("操作参数（完整）" in message for message in self.messages))
        self.bridge.handle_event({"guid": "wrong-chat", "chat_id": 2,
                                  "sender": self.config.allowed_sender,
                                  "text": "/ds approve aaaaaaaa",
                                  "is_from_me": False, "is_group": False})
        self.assertIsNotNone(self.bridge.pending_approval)
        self.event("approval-1", "/ds approve aaaaaaaa")
        self.assertIsNone(self.bridge.pending_approval)
        self.assertIn(b'"outcome":"allowed-once"', process.stdin.getvalue())
        self.event("approval-2", "/ds approve aaaaaaaa")
        self.assertTrue(any("审批编号无效" in message for message in self.messages))

    def test_sensitive_approval_is_rejected_without_prompt(self):
        request_id = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        class FakeProcess:
            stdin = io.BytesIO()
            def poll(self):
                return None
        process = FakeProcess()
        self.bridge.active_process = process
        self.bridge.active_task_id = "abcd1234"
        task = Task("g", "abcd1234", 1, self.config.allowed_sender, "测试")
        event = {"id": request_id,
                 "sessionId": "session-11111111-2222-4333-8444-555555555555",
                 "tool": "bash", "arguments": '{"command":"cat ~/.ssh/id_ed25519"}'}
        self.bridge.handle_approval_request(APPROVAL_PREFIX + json.dumps(event), task)
        self.assertIsNone(self.bridge.pending_approval)
        self.assertIn(b'"outcome": "rejected"', process.stdin.getvalue())

    def test_running_task_routes_approval_over_stdio(self):
        request_id = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        session_id = "session-11111111-2222-4333-8444-555555555555"
        fake = Path(self._tmp.name) / "fake_approval.py"
        approval = {"id": request_id, "sessionId": session_id,
                    "callId": "call-1", "tool": "bash",
                    "arguments": '{"command":"touch /tmp/approved-test"}'}
        fake.write_text(
            "import json,sys\n"
            f"print({(APPROVAL_PREFIX + json.dumps(approval))!r},flush=True)\n"
            "decision=json.loads(sys.stdin.readline())\n"
            "print('DECISION='+decision['outcome'],flush=True)\n",
            encoding="utf-8",
        )
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        self.bridge.current_session_id = session_id
        task = Task("stdio-approval", "abcd1234", 1, self.config.allowed_sender, "测试")
        self.bridge.db.add(task)
        with mock.patch.object(self.bridge, "session_exists_in_workspace", return_value=True), \
             mock.patch.object(self.bridge, "register_workspace_sessions", return_value=True):
            worker = threading.Thread(target=self.bridge.run_task, args=(task,), daemon=True)
            worker.start()
            for _ in range(200):
                if self.bridge.pending_approval is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(self.bridge.pending_approval)
            self.event("stdio-decision", "/ds approve cccccccc")
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.bridge.db.task_status(task.task_id), "completed")
        self.assertTrue(any("DECISION=allowed-once" in message for message in self.messages))

    def test_idle_watcher_stops_without_waiting_for_a_message(self):
        marker = Path(self._tmp.name) / "watcher.pid"
        fake = Path(self._tmp.name) / "fake_imsg"
        fake.write_text(
            "import os,time\n"
            f"open({str(marker)!r},'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        real_popen = subprocess.Popen
        with mock.patch("bridge.subprocess.Popen", side_effect=lambda _cmd, **kwargs:
                        real_popen([sys.executable, str(fake)], **kwargs)):
            watcher = threading.Thread(target=self.bridge.run, daemon=True)
            watcher.start()
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            started = time.monotonic()
            self.bridge.stop_event.set()
            watcher.join(timeout=3)
        self.assertFalse(watcher.is_alive())
        self.assertLess(time.monotonic() - started, 3)

    def test_emergency_stop_terminates_active_task(self):
        fake = Path(self._tmp.name) / "fake_slow.py"
        fake.write_text("import time\nprint('started', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
        self.bridge.config = replace(self.config, node=Path(sys.executable), dsh_entry=fake)
        task = Task("g-slow", "11223344", 1, "+8613800138000", "等待")
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
