"""持久化重试、完整失败档案及队列防阻塞测试；不访问任何外部服务。"""

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.db_client import MidRecord, MySQLTaskRepository, TaskRecord
from src.mid_resolver import MidResolveError, MidResolverClient
from src.pipeline import ProcessResult
from src.retry_state import FailureRetryStore, RetryPersistenceError, record_key
from src.worker import MySQLShardWorker


def task_record():
    return TaskRecord(1, 20, 21, 1, 1, industry_name="美食", raw={"context": "完整任务"})


def mid_record():
    return MidRecord(1, 21, 20, "m1", "u1", "作者", "完整正文" * 1000, "p1,p2", "f1",
                     raw={"complete_column": "原始字段" * 1000})


def failed_result():
    return ProcessResult("m1", "u1", "auto", error="反解失败" * 1000, error_stage="resolve",
                         model_output="完整模型输出" * 1000, error_traceback="完整异常栈",
                         error_details={"response_body": "完整响应" * 1000})


class RetryStoreTests(unittest.TestCase):
    def test_initial_plus_three_retries_survive_restart_and_preserve_all_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, task, result = mid_record(), task_record(), failed_result()
            for attempt, now in enumerate((0, 60, 360, 2160), 1):
                store = FailureRetryStore(tmp)
                self.assertTrue(store.eligible(record, now=now))
                self.assertEqual(attempt, store.reserve(task, record, now=now))
                saved = store.finish(record, result, now=now)
                if attempt < 4:
                    self.assertEqual("waiting", saved["status"])
                    self.assertFalse(store.eligible(record, now=now + 1))
                else:
                    self.assertEqual("exhausted", saved["status"])
            store = FailureRetryStore(tmp)
            self.assertFalse(store.eligible(record, now=10**9))
            self.assertIsNone(store.reserve(task, record, now=10**9))
            payload = json.loads(Path(saved["failure_log"]).read_text())
            self.assertEqual(4, len(payload["history"]))
            self.assertEqual(record.raw, payload["record"]["raw"])
            self.assertEqual(record.mid_text, payload["record"]["mid_text"])
            self.assertEqual(task.raw, payload["task"]["raw"])
            self.assertEqual(result.error, payload["history"][0]["result"]["error"])
            self.assertEqual(result.error_details, payload["history"][0]["result"]["error_details"])
            self.assertEqual(result.model_output, payload["history"][0]["result"]["model_output"])
            self.assertEqual(0o600, Path(saved["failure_log"]).stat().st_mode & 0o777)
            self.assertEqual(0o600, store.path.stat().st_mode & 0o777)

    def test_crashed_attempts_cannot_reset_budget_or_ignore_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, record = task_record(), mid_record()
            for attempt, now in enumerate((0, 60, 360, 2160), 1):
                store = FailureRetryStore(tmp)
                self.assertEqual(attempt, store.reserve(task, record, now=now))
                self.assertIsNone(store.reserve(task, record, now=now + 1))
            self.assertFalse(FailureRetryStore(tmp).eligible(record, now=10**9))
            with sqlite3.connect(store.path) as conn:
                payload = json.loads(conn.execute("SELECT payload FROM retry_state").fetchone()[0])
            self.assertEqual(4, len(payload["history"]))
            self.assertEqual(record.raw, payload["current_record"]["raw"])

    def test_success_and_interruption_do_not_become_technical_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, task, record = FailureRetryStore(tmp), task_record(), mid_record()
            store.reserve(task, record, now=0)
            store.finish(record, interrupted=True, now=1)
            self.assertEqual(1, store.reserve(task, record, now=2))
            saved = store.finish(record, ProcessResult("m1", "u1", "auto", success=True), now=3)
            self.assertEqual("resolved", saved["status"])
            self.assertFalse(store.eligible(record, now=10**9))
            payload = json.loads(Path(saved["failure_log"]).read_text())
            self.assertEqual("interrupted", payload["history"][0]["status"])

    def test_first_success_has_no_failure_file_or_state_bloat(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, task, record = FailureRetryStore(tmp), task_record(), mid_record()
            store.reserve(task, record, now=0)
            saved = store.finish(record, ProcessResult("m1", "u1", "auto", success=True), now=1)
            self.assertEqual("", saved["failure_log"])
            self.assertFalse(list(Path(tmp).glob("*.json")))
            with sqlite3.connect(store.path) as conn:
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM retry_state").fetchone()[0])

    def test_identity_is_scoped_to_customer_task_record_and_mid(self):
        record = mid_record()
        self.assertEqual(5, len({record_key(item) for item in (
            record, replace(record, customer_id=22), replace(record, super_task_id=22),
            replace(record, id=2), replace(record, mid="m2"),
        )}))

    def test_json_export_failure_retains_full_sqlite_copy_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, task, record = FailureRetryStore(tmp), task_record(), mid_record()
            store.reserve(task, record, now=0)
            with patch("src.retry_state.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(RetryPersistenceError):
                    store.finish(record, failed_result(), now=0)
            with sqlite3.connect(store.path) as conn:
                payload = json.loads(conn.execute("SELECT payload FROM retry_state").fetchone()[0])
            self.assertEqual(failed_result().model_output, payload["history"][0]["result"]["model_output"])

    def test_corrupt_database_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FailureRetryStore(tmp)
            store.path.write_bytes(b"corrupted sqlite")
            with self.assertRaises(RetryPersistenceError):
                store.eligible(mid_record())

    def test_more_than_three_retries_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            FailureRetryStore(tmp, max_retries=4)


class WorkerRetryTests(unittest.TestCase):
    def test_real_pipeline_produces_only_four_http_calls_and_a_report_with_archive_paths(self):
        from scripts.generate_weekly_report import build_report
        from tests.test_production_guards import minimal_config
        from datetime import date

        with tempfile.TemporaryDirectory() as tmp:
            config = minimal_config()
            config["audit"] = {"local_enabled": True, "dir": str(Path(tmp) / "runs")}
            config["logging"]["error_file_enabled"] = False
            config["mid_resolver"] = {"max_retry": 3}
            response = MagicMock(status_code=200, text="full resolver error body")
            response.json.return_value = {"code": 1, "message": "test failure"}
            with patch("src.worker.MySQLTaskRepository"), \
                 patch("src.pipeline.ClassifyPipeline._cleanup_expired_cache"), \
                 patch("src.pipeline.ClassifyPipeline._cleanup_temp_files"), \
                 patch("src.mid_resolver.requests.get", return_value=response) as request:
                worker = MySQLShardWorker(config, MagicMock())
                worker.repo.acquire_record_lock.return_value.__enter__.return_value = True
                worker.repo.is_pending.return_value = True
                for now in (0, 60, 360, 2160):
                    with patch("src.retry_state.time.time", return_value=now):
                        worker._process_record(task_record(), mid_record())
                for _ in range(100):
                    with patch("src.retry_state.time.time", return_value=10**9):
                        worker._process_record(task_record(), mid_record())
            self.assertEqual(4, request.call_count)
            events = [json.loads(line) for line in Path(worker.pipeline.audit.path).read_text().splitlines()]
            self.assertEqual(4, sum(event["event"] == "mid_processed" for event in events))
            day = date.fromisoformat(events[0]["event_time"][:10])
            report = build_report(events, day, day, Path(tmp) / "runs", "日报")
            self.assertIn("- 处理尝试：4\n", report)
            self.assertIn("- 本窗口新增停止自动重试：1 条", report)
            self.assertIn("- 当前累计预算已耗尽：1 条", report)
            archive = next((Path(tmp) / "failures").glob("*.json"))
            self.assertIn(str(archive), report)
            payload = json.loads(archive.read_text())
            first_failure = payload["history"][0]["result"]
            self.assertEqual("full resolver error body", first_failure["error_details"]["attempts"][0]["response_body"])
            self.assertIn("MidResolveError", first_failure["error_traceback"])

    def make_worker(self, root, max_retries=3):
        config = {"mysql": {}, "audit": {"dir": str(Path(root) / "runs")},
                  "api": {"max_retry": 3}, "mid_resolver": {"max_retry": 3},
                  "result_writer": {"max_retry": 3}, "worker": {"max_retries": max_retries}}
        with patch("src.worker.MySQLTaskRepository"), patch("src.worker.ClassifyPipeline") as pipeline:
            worker = MySQLShardWorker(config, MagicMock())
            for section in ("api", "mid_resolver", "result_writer"):
                self.assertEqual(1, pipeline.call_args.args[0][section]["max_retry"])
                self.assertEqual(3, config[section]["max_retry"])
        worker.repo.acquire_record_lock.return_value.__enter__.return_value = True
        worker.repo.is_pending.return_value = True
        worker.pipeline.process_one.return_value = failed_result()
        return worker

    def test_worker_stops_calling_pipeline_after_four_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = self.make_worker(tmp)
            for now in (0, 60, 360, 2160):
                with patch("src.retry_state.time.time", return_value=now):
                    self.assertEqual("fail", worker._process_record(task_record(), mid_record()))
            with patch("src.retry_state.time.time", return_value=10**9):
                self.assertEqual("skipped", worker._process_record(task_record(), mid_record()))
            self.assertEqual(4, worker.pipeline.process_one.call_count)
            self.assertEqual("mid_retry_exhausted", worker.pipeline.audit.write_event.call_args.args[0])
            worker.repo.update_record_failure.assert_not_called()
            self.assertEqual(0, mid_record().level)

    def test_exhausted_and_cooling_records_do_not_starve_later_pages_or_log_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker, task = self.make_worker(tmp), task_record()
            blocked, cooling, healthy = mid_record(), replace(mid_record(), id=2), replace(mid_record(), id=3)
            with patch("src.retry_state.time.time", return_value=0):
                for now in (0, 60, 360, 2160):
                    worker.retry_store.reserve(task, blocked, now=now)
                    worker.retry_store.finish(blocked, failed_result(), now=now)
                worker.retry_store.reserve(task, cooling, now=2160)
                worker.retry_store.finish(cooling, failed_result(), now=2160)
            worker.repo.fetch_pending_mids.side_effect = [[blocked, cooling], [healthy]]
            worker._process_record = MagicMock(return_value="success")
            with patch("src.retry_state.time.time", return_value=2161):
                summary = worker._process_task(task, 2, 1, 1)
            self.assertEqual(1, summary["pending"])
            self.assertEqual(1, summary["success"])
            self.assertEqual(2, worker.repo.fetch_pending_mids.call_args.kwargs["after_id"])
            worker._process_record.assert_called_once_with(task, healthy)
            worker.repo.fetch_pending_mids.side_effect = [[blocked, cooling], []]
            worker.logger.reset_mock()
            with patch("src.retry_state.time.time", return_value=2161):
                summary = worker._process_task(task, 2, 1, 1)
            self.assertEqual(0, summary["pending"])
            worker.logger.info.assert_not_called()

    def test_unexpected_exception_is_archived_and_budgeted(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = self.make_worker(tmp, max_retries=0)
            worker.pipeline.process_one.side_effect = RuntimeError("unexpected resolver failure")
            self.assertEqual("fail", worker._process_record(task_record(), mid_record()))
            self.assertEqual("skipped", worker._process_record(task_record(), mid_record()))
            payload = json.loads(next((Path(tmp) / "failures").glob("*.json")).read_text())
            self.assertIn("unexpected resolver failure", payload["history"][0]["exception"])

    def test_persistence_failure_exits_forever_loop_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = self.make_worker(tmp)
            worker.run_once = MagicMock(side_effect=RetryPersistenceError("disk full"))
            with self.assertRaises(RetryPersistenceError):
                worker.run_forever()
            worker.run_once.assert_called_once()
            worker.pipeline.audit.finalize.assert_called_once()

    def test_paging_sql_uses_bound_numeric_cursor(self):
        repo = MySQLTaskRepository({})
        repo.table_exists = MagicMock(return_value=True)
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []
        repo.fetch_pending_mids(conn, task_record(), limit=2, after_id=100)
        sql, params = cur.execute.call_args.args
        self.assertIn("AND id > %s", sql)
        self.assertEqual((21, 20, 100, 2), params)

    def test_resolver_failure_preserves_request_and_full_response(self):
        response = MagicMock(status_code=200, text="完整反解失败响应" * 1000)
        response.json.return_value = {"code": 1, "message": "无法反解"}
        with patch("src.mid_resolver.requests.get", return_value=response) as request:
            with self.assertRaises(MidResolveError) as error:
                MidResolverClient(max_retry=1, logger_=MagicMock()).resolve("m1", "u1")
        request.assert_called_once()
        self.assertEqual("m1", error.exception.details["request"]["params"]["mid"])
        self.assertEqual(response.text, error.exception.details["attempts"][0]["response_body"])


if __name__ == "__main__":
    unittest.main()
