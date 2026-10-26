"""特殊任务发布、路径隔离和启动接线；全部离线，不调用业务或 HDFS 服务。"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from scripts import build_dataworks_bundle as bundle
from scripts import build_dataworks_release as release
from scripts import dataworks_supervisor as supervisor
from src.special_task_worker import build_special_config
from tests.test_production_guards import minimal_config

PROJECT = Path(__file__).resolve().parents[1]
SPECIAL_HDFS = "viewfs://unit/project/dataworks/special_888888"


class DataworksSpecialTests(unittest.TestCase):
    def test_both_packages_include_special_entry_and_verified_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "src").mkdir()
            (root / "config").mkdir()
            for name in ("worker.py", "worker_special_task.py", "run_dataworks.sh", "check_model_service.py",
                         "config/config.yaml", "src/special_task_worker.py", "scripts/dataworks_bundle_start.sh",
                         "scripts/dataworks_cshell_entry.sh"):
                (root / name).write_text("unit-test\n")
            assets = root / "assets"
            (assets / "wheels").mkdir(parents=True)
            (assets / "python-linux.tar.gz").write_bytes(b"offline fixture")
            wheel = assets / "wheels/unit.whl"
            with ZipFile(wheel, "w") as archive:
                archive.writestr("unit.dist-info/METADATA", "Name: unit\nVersion: 1\n")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            (root / "requirements.dataworks.lock").write_text(f"unit==1 --hash=sha256:{digest}\n")
            for builder, prefix, flags in ((release, "unit", ["--prefix", "unit"]),
                                            (bundle, "intent_behavior_dataworks", ["--assets", str(assets)])):
                output = root / (prefix + ".zip")
                with patch.object(builder, "PROJECT", root), \
                     patch.object(builder.subprocess, "check_output", return_value="commit\n"), \
                     patch.object(bundle, "EXPECTED", {"unit"}), \
                     patch.object(bundle, "PYTHON_SHA256", hashlib.sha256(b"offline fixture").hexdigest()), \
                     patch.object(sys, "argv", ["builder", "--output", str(output)] + flags), \
                     contextlib.redirect_stdout(io.StringIO()):
                    builder.main()
                with ZipFile(output) as archive:
                    self.assertIn(prefix + "/app/worker_special_task.py", archive.namelist())
                    self.assertIn(prefix + "/app/src/special_task_worker.py", archive.namelist())
                    for line in archive.read(prefix + "/SHA256SUMS").decode().splitlines():
                        digest, name = line.split("  ", 1)
                        self.assertEqual(digest, hashlib.sha256(archive.read(prefix + "/" + name)).hexdigest())

    def test_special_status_and_snapshot_never_include_ordinary_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normal = root / "logs"
            special = normal / "special_888888"
            (special / "failures").mkdir(parents=True)
            (normal / "live_worker.json").write_text('{"counts":{"ordinary":99}}')
            (normal / "mysql_worker.log").write_text("ordinary private data")
            (special / "live_worker.json").write_text('{"counts":{"special":2}}')
            (special / "special_worker.log").write_text("special only")
            with sqlite3.connect(special / "failures/retry_state.sqlite3") as conn:
                conn.execute("CREATE TABLE retry_state(status TEXT)")
                conn.execute("INSERT INTO retry_state VALUES('exhausted')")
            child = MagicMock(pid=42)
            child.poll.return_value = None
            captured = []

            def upload(command, **kwargs):
                with tarfile.open(command[-2]) as archive:
                    captured.extend(archive.getnames())

            with patch.object(supervisor, "PROJECT", root), \
                 patch.dict(os.environ, {"DATAWORKS_STATE_HDFS": ""}), \
                 patch.object(supervisor.subprocess, "run", side_effect=upload):
                status = supervisor.status_payload(child, "hdfs://unit/special.tar.gz", entry="special")
                supervisor.snapshot("hdfs://unit/special.tar.gz", entry="special")
                self.assertEqual(normal / "failures/retry_state.sqlite3", supervisor.retry_database())
            self.assertEqual({"special": 2}, status["worker"]["counts"])
            self.assertEqual(888888, status["task_id"])
            self.assertEqual(["special only"], status["log_tail"])
            self.assertEqual({"exhausted": 1}, status["retry_counts"])
            self.assertTrue(all(name.startswith("logs/special_888888/") for name in captured))

    def test_special_restore_preserves_budget_and_never_overwrites_ordinary_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normal = root / "logs/failures/retry_state.sqlite3"
            normal.parent.mkdir(parents=True)
            normal.write_bytes(b"ordinary must stay unchanged")
            remote = root / "remote.sqlite3"
            with sqlite3.connect(remote) as conn:
                conn.execute("CREATE TABLE retry_state(record_key TEXT, attempts INTEGER, status TEXT, failure_log TEXT, payload TEXT)")
                conn.execute("INSERT INTO retry_state VALUES('special',4,'exhausted','old',?)", (json.dumps({"history": [1, 2, 3, 4]}),))

            def hdfs(command, **kwargs):
                if command[2] == "-ls":
                    return subprocess.CompletedProcess(command, 0, stdout=SPECIAL_HDFS + "/state/retry_state.sqlite3\n")
                if command[2] == "-get":
                    shutil.copyfile(remote, command[-1])
                return subprocess.CompletedProcess(command, 0)

            with patch.object(supervisor, "PROJECT", root), patch.object(supervisor.subprocess, "run", side_effect=hdfs):
                supervisor.restore_retry_state(SPECIAL_HDFS + "/state/retry_state.sqlite3", "special")
                special = supervisor.retry_database("special")
                with sqlite3.connect(special) as conn:
                    self.assertEqual((4, "exhausted"), conn.execute("SELECT attempts,status FROM retry_state").fetchone())
            self.assertEqual(b"ordinary must stay unchanged", normal.read_bytes())
            self.assertTrue((special.parent / "special.json").exists())

    def test_special_rejects_ordinary_hdfs_namespace_before_any_external_operation(self):
        with patch.dict(os.environ, {"DATAWORKS_LOG_HDFS": "viewfs://unit/project/logs",
                                     "DATAWORKS_STATE_HDFS": "viewfs://unit/project/state/retry_state.sqlite3"}), \
             patch.object(supervisor.subprocess, "run") as run, \
             patch.object(supervisor.subprocess, "Popen") as start:
            with self.assertRaisesRegex(ValueError, "独立"):
                supervisor.main("special")
            run.assert_not_called()
            start.assert_not_called()

    def test_supervisor_selects_special_child_and_fixed_local_root(self):
        child = MagicMock(returncode=0)
        child.poll.return_value = 0
        with patch.dict(os.environ, {"DATAWORKS_LOG_HDFS": SPECIAL_HDFS + "/logs",
                                     "DATAWORKS_STATE_HDFS": SPECIAL_HDFS + "/state/retry_state.sqlite3"}), \
             patch.object(supervisor.subprocess, "run"), \
             patch.object(supervisor, "restore_retry_state") as restore, \
             patch.object(supervisor, "snapshot"), \
             patch.object(supervisor, "publish_status"), \
             patch.object(supervisor.signal, "signal"), \
             patch.object(supervisor.subprocess, "Popen", return_value=child) as start:
            self.assertEqual(0, supervisor.main("special"))
        self.assertEqual("worker_special_task.py", start.call_args.args[0][2])
        self.assertEqual(str(PROJECT / "logs/special_888888"), start.call_args.kwargs["env"]["DATAWORKS_SPECIAL_LOG_ROOT"])
        restore.assert_called_once_with(SPECIAL_HDFS + "/state/retry_state.sqlite3", "special")

    def test_dataworks_layout_matches_real_special_worker_config(self):
        with patch.dict(os.environ, {"DATAWORKS_SPECIAL_LOG_ROOT": str(PROJECT / "logs/special_888888")}):
            isolated = build_special_config(minimal_config())
        self.assertEqual(str(PROJECT / "logs/special_888888"), isolated["logging"]["dir"])
        self.assertEqual(str(PROJECT / "logs/special_888888/runs"), isolated["audit"]["dir"])
        with patch.dict(os.environ, {"DATAWORKS_SPECIAL_LOG_ROOT": str(PROJECT / "logs")}):
            with self.assertRaisesRegex(ValueError, "独立"):
                build_special_config(minimal_config())

    def test_shell_launcher_selects_entry_without_running_python_business_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_python = root / "python"
            fake_python.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
            fake_python.chmod(0o700)
            env = dict(os.environ, PYTHON_BIN=str(fake_python), DATAWORKS_LOG_HDFS=SPECIAL_HDFS + "/logs")
            special = subprocess.run(["bash", "run_dataworks.sh", "--special"], cwd=PROJECT, env=env,
                                     capture_output=True, text=True, check=True)
            normal = subprocess.run(["bash", "run_dataworks.sh"], cwd=PROJECT, env=env,
                                    capture_output=True, text=True, check=True)
            preview = subprocess.run(["bash", "run_dataworks.sh", "--special", "--dry-run", "--limit", "1"],
                                     cwd=PROJECT, env=env, capture_output=True, text=True, check=True)
        self.assertIn("scripts/dataworks_supervisor.py --entry special", special.stdout)
        self.assertNotIn("--entry special", normal.stdout)
        self.assertIn("worker_special_task.py --config config/config.yaml --dry-run --limit 1", preview.stdout)


if __name__ == "__main__":
    unittest.main()
