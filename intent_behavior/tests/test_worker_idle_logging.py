"""Worker 空闲日志与审计的离线回归测试。"""

import unittest
from unittest.mock import MagicMock, patch

from src.worker import MySQLShardWorker, WorkerStats


def make_worker():
    worker = MySQLShardWorker.__new__(MySQLShardWorker)
    worker.worker_cfg = {
        "poll_interval_sec": 10,
        "idle_heartbeat_sec": 30,
        "max_loops": 5,
        "daily_reset": False,
    }
    worker.logger = MagicMock()
    worker.repo = MagicMock()
    worker.pipeline = MagicMock()
    worker.stats = WorkerStats()
    return worker


def make_task(task_id):
    task = MagicMock()
    task.task_id = task_id
    task.customer_id = 21
    task.industry_name = "汽车"
    task.shard_table = "nature_ad_super_mid_1"
    return task


class WorkerIdleLoggingTests(unittest.TestCase):
    def test_empty_poll_is_silent_and_does_not_write_task_audit(self):
        worker = make_worker()
        worker.repo.fetch_active_tasks.return_value = [make_task(1), make_task(2)]
        worker.repo.fetch_pending_mids.return_value = []

        summary = worker.run_once()

        self.assertEqual(2, summary["tasks"])
        self.assertEqual(0, summary["pending"])
        worker.logger.info.assert_not_called()
        worker.pipeline.audit.record_task_summary.assert_not_called()

    def test_active_task_keeps_detail_and_summary(self):
        worker = make_worker()
        tasks = [make_task(1), make_task(2)]
        worker.repo.fetch_active_tasks.return_value = tasks
        record = MagicMock(mid="5281224466635091", mid_uid="1027738525")
        record.has_forward.return_value = False
        worker.repo.fetch_pending_mids.side_effect = [[], [record]]
        worker._process_record = MagicMock(return_value="success")

        summary = worker.run_once()

        self.assertEqual(1, summary["pending"])
        self.assertEqual(1, summary["success"])
        worker.pipeline.audit.record_task_summary.assert_called_once()
        self.assertIs(worker.pipeline.audit.record_task_summary.call_args.args[0], tasks[1])
        messages = [call.args[0] for call in worker.logger.info.call_args_list]
        self.assertTrue(any("task_id=2" in message for message in messages))
        self.assertTrue(any("第 1 轮轮询完成" in message for message in messages))
        self.assertFalse(any("task_id=1" in message for message in messages))

    def test_idle_heartbeat_is_periodic_and_resets_after_activity(self):
        worker = make_worker()
        idle = {"pending": 0, "tasks": 19}
        active = {"pending": 1, "tasks": 19}
        worker.run_once = MagicMock(side_effect=[idle, idle, active, idle, idle])

        with patch("src.worker.time.monotonic", side_effect=[0, 10, 31, 35, 40, 75]), \
             patch("src.worker.time.sleep"):
            worker.run_forever()

        heartbeats = [
            call for call in worker.logger.info.call_args_list
            if call.args and str(call.args[0]).startswith("空闲心跳:")
        ]
        self.assertEqual(2, len(heartbeats))
        self.assertEqual(2, heartbeats[0].args[1])
        self.assertEqual(2, heartbeats[1].args[1])


if __name__ == "__main__":
    unittest.main()
