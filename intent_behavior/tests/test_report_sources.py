"""HDFS 报告输入离线回归；全部使用临时归档，不连接 HDFS/钉钉/业务库。

从 intent_behavior 目录运行：python3 -m unittest tests.test_report_sources -v
"""

import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts import report_sources as sources
from scripts import send_weekly_report as sender


def make_tar(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            data = content if isinstance(content, bytes) else content.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def jsonl(events):
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


class FakeClient:
    def __init__(self, root):
        self.now = datetime.now(sources.TIMEZONE)
        self.day = self.now.date() - timedelta(days=1)
        self.root = Path(root)
        first = datetime.combine(self.day - timedelta(days=1), datetime.min.time(), sources.TIMEZONE) + timedelta(hours=17)
        second = datetime.combine(self.day, datetime.min.time(), sources.TIMEZONE) + timedelta(hours=17, minutes=10)
        self.entries = [sources.Archive(first.strftime("%Y%m%d_%H%M%S") + "_aaaa.tar.gz", 100, second, first),
                        sources.Archive(second.strftime("%Y%m%d_%H%M%S") + "_bbbb.tar.gz", 100, self.now, second)]
        self.names = ["logs/runs/" + first.strftime("%Y%m%d") + "/r1.jsonl",
                      "logs/runs/" + second.strftime("%Y%m%d") + "/r2.jsonl"]
        self.old_events = [{"event": "run_started", "event_time": first.isoformat(), "run_id": "r1"},
                           {"event": "mid_processed", "event_time": f"{self.day}T09:00:00", "status": "success", "run_id": "r1", "mid": "m1"}]
        self.new_events = [{"event": "run_started", "event_time": second.isoformat(), "run_id": "r2"},
                           {"event": "mid_processed", "event_time": f"{self.day}T21:44:34", "status": "success", "run_id": "r2", "mid": "m2"}]
        make_tar(self.root / self.entries[0].name, {self.names[0]: jsonl(self.old_events)})
        make_tar(self.root / self.entries[1].name, {
            self.names[0]: jsonl(self.old_events[:1]), self.names[1]: jsonl(self.new_events),
            "logs/runs/" + second.strftime("%Y%m%d") + "/r2_summary.json": "{}",
            "logs/live_worker.json": '{"private_content":"不复制"}',
            "config/config.yaml": "password: 不复制",
        })
        self.downloaded = []
        self.payload = {"updated_at": time.time(), "state": "running",
                        "archive": sources.DEFAULT_HDFS_ROOT + "/logs/" + self.entries[-1].name,
                        "retry_counts": {"exhausted": 4}, "log_tail": ["不应写入报告来源元数据的正文"]}

    def status(self, root):
        return self.payload

    def archives(self, root):
        return self.entries

    def download(self, uri, target):
        self.downloaded.append(uri)
        shutil.copyfile(self.root / uri.rsplit("/", 1)[1], target)


class ReportSourceTests(unittest.TestCase):
    def test_cache_uses_uid_sibling_not_a_foreign_private_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, client = Path(tmp), FakeClient(tmp)
            old = root / "cache"
            old.mkdir(mode=0o700)
            (old / "sync.lock").write_text("foreign user's lock")
            for uid in (111, 222):
                with patch.object(sources.os, "geteuid", return_value=uid):
                    with self.prepare(root, client) as prepared:
                        self.assertEqual(root / f"cache_uid_{uid}", prepared.audit_dir.parents[1])
            self.assertEqual("foreign user's lock", (old / "sync.lock").read_text())
            self.assertTrue((root / "cache_uid_111/sync.lock").is_file())
            self.assertTrue((root / "cache_uid_222/sync.lock").is_file())

    def prepare(self, root, client):
        return sources.prepare_report_inputs("dataworks_hdfs", client.day, client.day,
            legacy_dir=Path(root) / "legacy", cache_dir=Path(root) / "cache", client=client)

    def test_real_render_merges_all_instances_and_deduplicates_prefix_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = FakeClient(root)
            legacy = root / "legacy" / client.names[0].split("/")[2] / "r1.jsonl"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(jsonl(client.old_events))
            original = legacy.read_bytes()
            with self.prepare(root, client) as prepared:
                info = json.loads(prepared.manifest.read_text())
                self.assertNotIn("log_tail", info)
                self.assertFalse((prepared.audit_dir.parent / "config").exists())
                report = sender.render_report(sender.ReportSpec("daily", "日报", client.day, client.day),
                                              prepared.audit_dir, prepared.manifest)
                self.assertIn("- 处理尝试：2\n", report)
                self.assertIn("- 正常分类 success：2（100.0%）", report)
                self.assertIn("当前累计预算已耗尽：4", report)
                self.assertIn(sources.DEFAULT_HDFS_ROOT + "/logs/", report)
                self.assertNotIn("snapshot-", report)
                self.assertNotIn("不应写入报告来源元数据的正文", report)
                self.assertEqual(2, len(list(prepared.audit_dir.glob("*/*.jsonl"))))
            self.assertEqual(original, legacy.read_bytes())

    def test_current_archive_is_downloaded_again_even_with_same_size_and_minute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, client = Path(tmp), FakeClient(tmp)
            for _ in range(2):
                with self.prepare(root, client):
                    pass
            self.assertEqual(3, len(client.downloaded))
            self.assertEqual(2, sum(uri.endswith(client.entries[-1].name) for uri in client.downloaded))

    def test_verified_empty_window_is_a_real_zero_not_a_missing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(tmp)
            for index, events in enumerate((client.old_events[:1], client.new_events[:1])):
                make_tar(Path(tmp) / client.entries[index].name, {client.names[index]: jsonl(events)})
            with self.prepare(tmp, client) as prepared:
                report = sender.render_report(sender.ReportSpec("daily", "日报", client.day, client.day),
                                              prepared.audit_dir, prepared.manifest)
            self.assertIn("- 处理尝试：0\n", report)
            self.assertIn("本窗口没有 mid_processed 事件", report)
            self.assertNotIn("请确认 `audit.local_enabled", report)

    def test_linked_audit_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(tmp)
            with tarfile.open(Path(tmp) / client.entries[-1].name, "w:gz") as archive:
                member = tarfile.TarInfo(client.names[1])
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                archive.addfile(member)
            with self.assertRaisesRegex(sources.ReportSourceError, "不是常规"):
                with self.prepare(tmp, client):
                    self.fail()

    def test_hdfs_listing_parses_only_real_instance_archives(self):
        output = ("Found 3 items\n"
                  "-rw------- 1 ads_fst group 123 2026-10-26 09:59 viewfs://c9/test/20261025_171002_ab12.tar.gz\n"
                  "-rw------- 1 ads_fst group 10 2026-10-26 09:59 /test/status.json\n"
                  "drwx------ - ads_fst group 0 2026-10-26 09:59 /test/dir\n")
        with patch.object(sources.HdfsClient, "run", return_value=output):
            entries = sources.HdfsClient().archives(sources.DEFAULT_HDFS_ROOT)
        self.assertEqual(1, len(entries))
        self.assertEqual("20261025_171002_ab12.tar.gz", entries[0].name)
        self.assertEqual(123, entries[0].size)

    def test_stale_heartbeat_never_uses_cached_or_legacy_data_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(tmp)
            with self.prepare(tmp, client):
                pass
            client.payload["updated_at"] -= 3600
            with self.assertRaisesRegex(sources.ReportSourceError, "心跳"):
                with self.prepare(tmp, client):
                    self.fail("必须拒绝陈旧输入")

    def test_missing_current_archive_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(tmp)
            client.entries = []
            with self.assertRaises(sources.ReportSourceError):
                with self.prepare(tmp, client):
                    self.fail()

    def test_missing_beginning_of_daily_window_is_not_called_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(tmp)
            client.entries = client.entries[1:]
            with self.assertRaisesRegex(sources.ReportSourceError, "覆盖不足"):
                with self.prepare(tmp, client):
                    self.fail()

    def test_corrupt_and_incomplete_jsonl_are_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(tmp)
            make_tar(Path(tmp) / client.entries[-1].name, {client.names[1]: b'{"event":'})
            with self.assertRaisesRegex(sources.ReportSourceError, "JSONL"):
                with self.prepare(tmp, client):
                    self.fail()

    def test_conflicting_same_name_audit_is_not_arbitrarily_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(tmp)
            make_tar(Path(tmp) / client.entries[-1].name, {client.names[0]: jsonl([
                {"event": "run_started", "event_time": client.now.isoformat(), "run_id": "different"}])})
            with self.assertRaisesRegex(sources.ReportSourceError, "冲突"):
                with self.prepare(tmp, client):
                    self.fail()

    def test_unsafe_tar_path_is_never_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(tmp)
            make_tar(Path(tmp) / client.entries[-1].name, {"../../escape": "unsafe"})
            with self.assertRaisesRegex(sources.ReportSourceError, "不安全"):
                with self.prepare(tmp, client):
                    self.fail()
            self.assertFalse((Path(tmp) / "escape").exists())

    def test_local_forensics_needs_no_hdfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with sources.prepare_report_inputs("local", datetime.now().date(), datetime.now().date(), legacy_dir=tmp) as prepared:
                self.assertEqual(Path(tmp), prepared.audit_dir)
                self.assertIsNone(prepared.manifest)

    def test_hdfs_client_uses_argv_timeouts_and_checks_return_code(self):
        with patch.object(sources.subprocess, "run") as run:
            run.return_value.stdout = "{}"
            sources.HdfsClient("/safe/hdfs").status(sources.DEFAULT_HDFS_ROOT)
        args, kwargs = run.call_args
        self.assertEqual(["/safe/hdfs", "dfs", "-cat", sources.DEFAULT_HDFS_ROOT + "/logs/status.json"], args[0])
        self.assertTrue(kwargs["check"])
        self.assertLessEqual(kwargs["timeout"], 90)
        self.assertNotIn("shell", kwargs)
        with patch.object(sources.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(sources.ReportSourceError, "HDFS_RUNNER"):
                sources.HdfsClient().status(sources.DEFAULT_HDFS_ROOT)
        with patch.object(sources.subprocess, "run", side_effect=subprocess.CalledProcessError(
                1, "hdfs", stderr="WARN noise\nPermission denied: user=wrong password=private-value\n")):
            with self.assertRaises(sources.ReportSourceError) as error:
                sources.HdfsClient().status(sources.DEFAULT_HDFS_ROOT)
        self.assertIn("Permission denied", str(error.exception))
        self.assertNotIn("private-value", str(error.exception))


class SenderSourceFailureTests(unittest.TestCase):
    def test_wrong_linux_account_stops_before_hdfs_cache_or_message(self):
        config = {"enabled": True, "execution_user": "ads_fst"}
        with patch("sys.argv", ["send", "--dry-run"]), \
             patch.object(sender, "load_dingtalk_config", return_value=config), \
             patch.object(sender.pwd, "getpwuid") as user, \
             patch.object(sender, "prepare_report_inputs") as prepare, \
             patch.object(sender, "send_report") as send:
            user.return_value.pw_name = "xuanyu11"
            with self.assertRaisesRegex(SystemExit, "su - ads_fst"):
                sender.main()
        prepare.assert_not_called()
        send.assert_not_called()

    def test_dry_run_source_failure_shows_missing_data_and_never_sends(self):
        config = {"enabled": True, "robot_code": "test", "recipient_user_ids": ["test"]}
        output = io.StringIO()
        with patch("sys.argv", ["send", "--run-date", "2026-10-26", "--dry-run"]), \
             patch.object(sender, "load_dingtalk_config", return_value=config), \
             patch.object(sender, "prepare_report_inputs", side_effect=sources.ReportSourceError("心跳过期")), \
             patch.object(sender, "send_report") as send, redirect_stdout(output):
            with self.assertRaises(SystemExit) as error:
                sender.main()
        self.assertEqual(2, error.exception.code)
        self.assertIn("报告数据缺失", output.getvalue())
        self.assertNotIn("处理尝试：0", output.getvalue())
        send.assert_not_called()


class ReportCronTests(unittest.TestCase):
    def test_installer_pins_hdfs_path_is_idempotent_and_keeps_unrelated_jobs(self):
        with tempfile.TemporaryDirectory(prefix="report-cron-test-") as tmp:
            root = Path(tmp)
            project = root / "project"
            (project / "scripts").mkdir(parents=True)
            (project / "scripts/send_weekly_report.py").write_text("# fixture")
            binary = root / "bin"
            binary.mkdir()
            hdfs = binary / "hdfs"
            hdfs.write_text("#!/bin/sh\nexit 0\n")
            hdfs.chmod(0o700)
            state = root / "fake-crontab"
            unrelated = "15 2 * * * /safe/unrelated-job\n"
            state.write_text(unrelated)
            cron = binary / "crontab"
            cron.write_text(f"#!{sys.executable}\nimport os,sys\nfrom pathlib import Path\np=Path(os.environ['TEST_CRONTAB'])\nif sys.argv[1]=='-l': print(p.read_text(),end='')\nelse: p.write_text(sys.stdin.read())\n")
            cron.chmod(0o700)
            env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"],
                       TEST_CRONTAB=str(state), TMPDIR=str(root))
            installer = Path(sender.PROJECT_DIR) / "scripts/install_weekly_report_cron.sh"
            command = ["bash", str(installer), "--project-dir", str(project),
                       "--expected-user", pwd.getpwuid(os.geteuid()).pw_name, "--hdfs-runner", str(hdfs)]
            for _ in range(2):
                subprocess.run(command, check=True, capture_output=True, text=True, env=env)
            result = state.read_text()
            self.assertIn(unrelated, result)
            self.assertEqual(1, result.count("scripts/send_weekly_report.py"))
            self.assertIn(f"HDFS_RUNNER='{hdfs}'", result)
            self.assertIn("CRON_TZ=Asia/Shanghai", result)
            subprocess.run(command[:-1] + [str(binary / "missing")], capture_output=True, env=env)
            self.assertEqual(result, state.read_text())
            wrong_user = command + ["--expected-user", "not-the-current-user"]
            rejected = subprocess.run(wrong_user, capture_output=True, env=env)
            self.assertEqual(2, rejected.returncode, rejected.stderr.decode("utf-8", "replace"))
            self.assertEqual(result, state.read_text())
            removed = subprocess.run(wrong_user + ["--remove"], check=True, capture_output=True, text=True, env=env)
            self.assertNotIn("scripts/send_weekly_report.py", state.read_text())
            self.assertIn(unrelated, state.read_text())
            backups = list(root.glob("intent-behavior-report-cron.*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(result, backups[0].read_text())
            self.assertIn(str(backups[0]), removed.stdout)


class SenderProductionSourceTests(unittest.TestCase):
    def test_production_source_failure_sends_one_alert_not_three_zero_reports(self):
        config = {"enabled": True, "robot_code": "test", "recipient_user_ids": ["test"]}
        with patch("sys.argv", ["send", "--run-date", "2026-07-31"]), \
             patch.object(sender, "load_dingtalk_config", return_value=config), \
             patch.object(sender, "prepare_report_inputs", side_effect=sources.ReportSourceError("归档不存在")), \
             patch.object(sender, "send_report") as send, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                sender.main()
        send.assert_called_once()
        self.assertIn("数据异常", send.call_args.args[3])
        self.assertNotIn("处理尝试：0", send.call_args.args[4])

    def test_all_periods_share_one_source_snapshot_and_render_before_sending(self):
        config = {"enabled": True, "robot_code": "test", "recipient_user_ids": ["test"]}

        @contextmanager
        def prepared(*args, **kwargs):
            yield sources.ReportInput(Path("/test/runs"), Path("/test/source.json"))

        with patch("sys.argv", ["send", "--run-date", "2026-07-31"]), \
             patch.object(sender, "load_dingtalk_config", return_value=config), \
             patch.object(sender, "prepare_report_inputs", side_effect=prepared) as prepare, \
             patch.object(sender, "render_report", return_value="valid report") as render, \
             patch.object(sender, "send_report") as send, redirect_stdout(io.StringIO()):
            sender.main()
        prepare.assert_called_once()
        self.assertEqual("2026-07-01", prepare.call_args.args[1].isoformat())
        self.assertEqual(3, render.call_count)
        self.assertEqual(3, send.call_count)

        with patch("sys.argv", ["send", "--run-date", "2026-07-31"]), \
             patch.object(sender, "load_dingtalk_config", return_value=config), \
             patch.object(sender, "prepare_report_inputs", side_effect=prepared), \
             patch.object(sender, "render_report", side_effect=["daily report", subprocess.CalledProcessError(1, "generator")]), \
             patch.object(sender, "send_report") as send, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                sender.main()
        send.assert_called_once()
        self.assertIn("数据异常", send.call_args.args[3])

    def test_local_source_cannot_be_sent_as_production(self):
        config = {"enabled": True, "robot_code": "test", "recipient_user_ids": ["test"]}
        with patch("sys.argv", ["send", "--source", "local"]), \
             patch.object(sender, "load_dingtalk_config", return_value=config), \
             patch.object(sender, "send_report") as send:
            with self.assertRaises(SystemExit):
                sender.main()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
