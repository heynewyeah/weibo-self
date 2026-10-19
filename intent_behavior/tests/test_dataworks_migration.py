"""迁移保护的离线检查；不连接数据库、不调用模型、不上传日志。"""
import os
from pathlib import Path
import signal
import sqlite3
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import dataworks_supervisor as supervisor
from worker import interrupt_on_termination
from tests.test_worker_idle_logging import make_worker

PROJECT = Path(__file__).resolve().parents[1]


class DataworksMigrationTests(unittest.TestCase):
    def test_retry_backup_and_restore_preserves_exhausted_budget_and_rebases_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = root / "remote.sqlite3"
            source = root / "old/logs/failures/retry_state.sqlite3"
            source.parent.mkdir(parents=True)
            with sqlite3.connect(str(source)) as conn:
                conn.execute("CREATE TABLE retry_state(record_key TEXT, attempts INTEGER, status TEXT, failure_log TEXT, payload TEXT)")
                conn.execute("INSERT INTO retry_state VALUES(?,4,'exhausted',?,?)",
                             ("abc", "/old/abc.json", json.dumps({"history": ["full details"]})))

            def hdfs(command, **kwargs):
                if command[2] == "-put":
                    shutil.copyfile(command[-2], saved)
                elif command[2] == "-ls":
                    return subprocess.CompletedProcess(command, 0, stdout="-rw------- 1 ads_fst 123 /state/retry.sqlite3\n")
                elif command[2] == "-get":
                    shutil.copyfile(saved, command[-1])
                return subprocess.CompletedProcess(command, 0)

            with patch.object(supervisor, "PROJECT", root / "old"), \
                 patch.object(supervisor.subprocess, "run", side_effect=hdfs):
                supervisor.sync_retry_state("hdfs://unit/state/retry.sqlite3")
            with patch.object(supervisor, "PROJECT", root / "new"), \
                 patch.object(supervisor.subprocess, "run", side_effect=hdfs):
                supervisor.restore_retry_state("hdfs://unit/state/retry.sqlite3")
                with sqlite3.connect(str(supervisor.retry_database())) as conn:
                    row = conn.execute("SELECT attempts,status,failure_log FROM retry_state").fetchone()
                self.assertEqual((4, "exhausted"), row[:2])
                self.assertEqual(root / "new/logs/failures/abc.json", Path(row[2]))
                self.assertEqual(["full details"], json.loads(Path(row[2]).read_text())["history"])

    def test_retry_restore_access_failure_prevents_launch(self):
        with patch.dict(os.environ, {"DATAWORKS_LOG_HDFS": "hdfs://unit/logs",
                                     "DATAWORKS_STATE_HDFS": "hdfs://unit/state/retry.sqlite3"}), \
             patch.object(supervisor, "restore_retry_state", side_effect=RuntimeError("state unavailable")), \
             patch.object(supervisor.subprocess, "run"), \
             patch.object(supervisor.subprocess, "Popen") as start:
            with self.assertRaisesRegex(RuntimeError, "state unavailable"):
                supervisor.main()
            start.assert_not_called()

    def test_repeated_database_errors_exit_and_finalize_audit(self):
        worker = make_worker()
        worker.worker_cfg["max_consecutive_loop_errors"] = 3
        worker.run_once = MagicMock(side_effect=RuntimeError("database unavailable"))
        with patch("src.worker.time.sleep"), self.assertRaisesRegex(RuntimeError, "database unavailable"):
            worker.run_forever()
        self.assertEqual(3, worker.run_once.call_count)
        worker.pipeline.audit.finalize.assert_called_once()

    def test_sigterm_uses_existing_interruption_path(self):
        with self.assertRaises(KeyboardInterrupt):
            interrupt_on_termination(signal.SIGTERM, None)

    def test_launcher_rejects_old_date_arguments(self):
        result = subprocess.run(["bash", "run_dataworks.sh", "YYYYMMDD", "HH"],
                                cwd=PROJECT, capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn("Usage", result.stderr)

    def test_snapshot_contains_logs_only_and_has_no_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs/runs/20261016").mkdir(parents=True)
            (root / "logs/runs/20261016/test.jsonl").write_text('{"event":"run_started"}\n')
            (root / "config.yaml").write_text("secret: exclude")
            (root / "logs/link").symlink_to(root / "config.yaml")
            captured = []

            def upload(command, **kwargs):
                self.assertEqual(["hdfs", "dfs", "-put", "-f"], command[:4])
                self.assertEqual("hdfs://unit-test/logs.tar.gz", command[-1])
                with tarfile.open(command[-2]) as archive:
                    captured.extend(archive.getnames())
                    self.assertEqual(b'{"event":"run_started"}\n', archive.extractfile(captured[0]).read())

            with patch.object(supervisor, "PROJECT", root), patch.object(supervisor.subprocess, "run", side_effect=upload):
                supervisor.snapshot("hdfs://unit-test/logs.tar.gz")
            self.assertEqual(["logs/runs/20261016/test.jsonl"], captured)

    def test_hdfs_failure_prevents_worker_start(self):
        with patch.dict(os.environ, {"DATAWORKS_LOG_HDFS": "hdfs://unit-test/logs"}), \
             patch.object(supervisor.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "hdfs")), \
             patch.object(supervisor.subprocess, "Popen") as start:
            with self.assertRaises(subprocess.CalledProcessError):
                supervisor.main()
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
