import contextlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import dataworks_monitor as monitor
from scripts import dataworks_supervisor as supervisor
from src.audit import RunAudit
from src.pipeline import ProcessResult


class DataworksMonitorTests(unittest.TestCase):
    def test_dead_or_stale_process_is_not_reported_healthy(self):
        base = {"state": "running", "updated_at": 1000}
        self.assertIn("过期", monitor.assess(base, now=1200)[0])
        self.assertEqual("运行中", monitor.assess(base, now=1001)[0])
        self.assertEqual("已停止", monitor.assess(dict(base, state="stopped"), now=1200)[0])
        self.assertEqual("运行失败", monitor.assess(dict(base, state="failed"), now=1001)[0])

    def test_live_audit_counts_and_stop_without_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"audit": {"dir": tmp + "/runs"}, "storage": {"min_free_mb": 0},
                   "monitor": {"enabled": True}}
            audit = RunAudit(cfg)
            result = ProcessResult(mid="123", uid="456", mode="auto", write_back=True)
            result.success = True
            result.model_output = "private raw model content"
            audit.record_result(result)
            live = Path(tmp) / "live_worker.json"
            payload = json.loads(live.read_text())
            self.assertEqual(1, payload["counts"]["success"])
            self.assertEqual("123", payload["last_result"]["mid"])
            self.assertNotIn("private raw model content", live.read_text())
            audit.finalize()
            self.assertEqual("stopped", json.loads(live.read_text())["state"])

    def test_supervisor_snapshot_reports_retry_counts_and_bounded_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs/failures").mkdir(parents=True)
            (root / "logs/live_worker.json").write_text('{"counts":{"failed":4}}')
            (root / "logs/mysql_worker.log").write_text("\n".join(str(i) for i in range(100)))
            with sqlite3.connect(str(root / "logs/failures/retry_state.sqlite3")) as conn:
                conn.execute("CREATE TABLE retry_state(status TEXT)")
                conn.executemany("INSERT INTO retry_state VALUES(?)", [("exhausted",), ("waiting",)])
            child = MagicMock(pid=12)
            child.poll.return_value = None
            with patch.object(supervisor, "PROJECT", root):
                status = supervisor.status_payload(child, "hdfs://logs/run.tar.gz")
            self.assertEqual({"exhausted": 1, "waiting": 1}, status["retry_counts"])
            self.assertEqual("running", status["state"])
            self.assertEqual("40", status["log_tail"][0])
            self.assertEqual("99", status["log_tail"][-1])
            self.assertEqual(60, len(status["log_tail"]))

    def test_failure_lookup_downloads_only_state_and_preserves_full_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "remote.sqlite3"
            payload = {"record": {"mid": "123", "super_task_id": 7}, "history": [{"error": "full detail"}]}
            with sqlite3.connect(str(db)) as conn:
                conn.execute("CREATE TABLE retry_state(record_key TEXT, attempts INTEGER, status TEXT, payload TEXT)")
                conn.execute("INSERT INTO retry_state VALUES('key',4,'exhausted',?)", (json.dumps(payload),))
            def download(command, **kwargs):
                self.assertEqual(["hdfs", "dfs", "-get"], command[:3])
                shutil.copyfile(db, command[-1])
                return subprocess.CompletedProcess(command, 0)
            output = io.StringIO()
            with patch.object(monitor.subprocess, "run", side_effect=download), contextlib.redirect_stdout(output):
                self.assertTrue(monitor.read_failure("hdfs://unit", "123", "7"))
            self.assertIn("full detail", output.getvalue())


if __name__ == "__main__":
    unittest.main()
