"""特殊任务范围、汽车链路、预演保护、分页及并行运行隔离；全部离线。"""

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.classifier import DEFAULT_CACHE_DIR
from src.mid_resolver import ResolvedBlog
from src.models import ClassifyResult
from src.pipeline import ClassifyPipeline, ProcessResult
from src.retry_state import failure_directory
from src.special_task_worker import (
    TASK_ID, SpecialTaskRepository, SpecialTaskWorker, build_special_config, task_for_record,
)
from src.worker import WorkerStats
from tests.test_production_guards import make_record, minimal_config


def special_record(row_id=1, customer_id=21):
    return replace(make_record(), id=row_id, customer_id=customer_id, super_task_id=TASK_ID,
                   task_industry_name="汽车")


def bare_worker(dry_run=False, limit=0):
    worker = SpecialTaskWorker.__new__(SpecialTaskWorker)
    worker.worker_cfg = {"fetch_limit_per_task": 2}
    worker.repo = MagicMock()
    worker.logger = MagicMock()
    worker.pipeline = MagicMock()
    worker.retry_store = MagicMock()
    worker.retry_store.eligible.return_value = True
    worker.stats = WorkerStats()
    worker.dry_run, worker.limit = dry_run, limit
    return worker


class SpecialTaskTests(unittest.TestCase):
    def test_real_worker_initializes_isolated_retry_and_single_attempt_writer(self):
        with tempfile.TemporaryDirectory() as root:
            config = minimal_config()
            config["logging"]["dir"] = root
            config["result_writer"]["max_retry"] = 3
            with patch.object(ClassifyPipeline, "_cleanup_expired_cache"):
                worker = SpecialTaskWorker(config, MagicMock())
            self.assertIsInstance(worker.repo, SpecialTaskRepository)
            self.assertIs(worker.repo, worker.pipeline.repo)
            self.assertEqual(1, worker.repo.writer.max_retry)
            self.assertTrue(worker.retry_store.path.exists())
            self.assertEqual(str(Path(root) / "special_888888"), worker.pipeline.error_log_dir)
            self.assertEqual(3, config["result_writer"]["max_retry"])

    def test_preview_cli_runs_once_and_finalizes_without_starting_forever(self):
        from worker_special_task import main

        with patch("worker_special_task.load_config", return_value=minimal_config()), \
             patch("worker_special_task.setup_logger"), \
             patch("worker_special_task.signal.signal"), \
             patch("worker_special_task.print"), \
             patch("worker_special_task.SpecialTaskWorker") as factory:
            worker = factory.return_value
            worker.run_once.return_value = {"fail": 0}
            self.assertEqual(0, main(["--dry-run", "--limit", "1"]))
            self.assertTrue(factory.call_args.kwargs["dry_run"])
            self.assertEqual(1, factory.call_args.kwargs["limit"])
            worker.run_once.assert_called_once()
            worker.run_forever.assert_not_called()
            worker.pipeline.audit.finalize.assert_called_once()
            worker._acquire_instance_lock.return_value.close.assert_called_once()

    def test_config_is_isolated_without_changing_normal_worker_configuration(self):
        config = minimal_config()
        before = copy.deepcopy(config)
        isolated = build_special_config(config)
        self.assertEqual(before, config)
        self.assertTrue(isolated["logging"]["dir"].endswith("logs/special_888888"))
        self.assertEqual(Path(isolated["logging"]["dir"]) / "failures", failure_directory(isolated))
        self.assertNotEqual(DEFAULT_CACHE_DIR, isolated["media"]["cache_dir"])
        self.assertEqual(config["prompts"], isolated["prompts"])
        self.assertEqual(config["api"], isolated["api"])
        self.assertTrue(isolated["worker"]["single_instance"])

    def test_query_uses_only_fixed_task_and_level_zero_with_keyset_paging(self):
        config = minimal_config()
        repo = SpecialTaskRepository(config["mysql"], app_config=config)
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [{
            "id": 7, "customer_id": 21, "super_task_id": TASK_ID, "mid": "m1",
            "mid_uid": "u1", "mid_uid_name": "作者", "forward_mid": "f1",
            "forward_mid_text": "原博", "level": 0,
        }]
        repo.config["shard_forward_text_field"] = "forward_mid_text"
        with patch.object(repo, "table_exists", return_value=True):
            records = repo.fetch_special_pending(conn, 1, 2, after_id=6)
        sql, params = cur.execute.call_args.args
        self.assertIn("nature_ad_super_mid_1", sql)
        self.assertIn("super_task_id = %s AND level = 0 AND id > %s", sql)
        self.assertEqual((TASK_ID, 6, 2), params)
        self.assertNotIn("super_mid_task", sql)
        self.assertNotIn("customer_id =", sql)
        record = records[0]
        self.assertEqual("汽车", record.task_industry_name)
        self.assertEqual([], record.task_brand_values)
        self.assertEqual([], record.task_topic_values)
        self.assertEqual("原博", record.forward_text)
        self.assertEqual(21, record.customer_id)
        with self.assertRaises(ValueError):
            repo.fetch_special_pending(conn, 20, 2)

    def test_all_twenty_shards_are_checked_without_reading_task_table(self):
        worker = bare_worker()
        worker.repo.fetch_special_pending.return_value = []
        summary = worker.run_once()
        self.assertEqual(20, summary["shards"])
        self.assertEqual(list(range(20)), [c.args[1] for c in worker.repo.fetch_special_pending.call_args_list])
        worker.repo.fetch_active_tasks.assert_not_called()
        worker.repo.fetch_task_by_id.assert_not_called()
        worker.logger.info.assert_not_called()

    def test_customers_in_different_shards_use_their_own_writeback_context(self):
        worker = bare_worker()
        rows = {1: [special_record(1, 21)], 2: [special_record(2, 42)]}
        worker.repo.fetch_special_pending.side_effect = lambda conn, shard, limit, after: rows.get(shard, [])
        worker._process_record = MagicMock(return_value="success")
        summary = worker.run_once()
        self.assertEqual(2, summary["success"])
        contexts = [c.args[0] for c in worker._process_record.call_args_list]
        self.assertEqual([21, 42], [t.customer_id for t in contexts])
        self.assertTrue(all(t.task_id == TASK_ID and t.industry_name == "汽车" for t in contexts))

    def test_global_limit_does_not_become_a_limit_for_each_shard(self):
        worker = bare_worker(limit=1)
        worker.repo.fetch_special_pending.side_effect = lambda conn, shard, limit, after: [special_record(1, 20)]
        worker._process_record = MagicMock(return_value="success")
        summary = worker.run_once()
        self.assertEqual(1, summary["pending"])
        worker._process_record.assert_called_once()

    def test_exhausted_rows_do_not_block_later_pending_rows(self):
        worker = bare_worker()
        records = [special_record(i) for i in range(1, 4)]
        worker.repo.fetch_special_pending.side_effect = [records[:2], records[2:]]
        worker.retry_store.eligible.side_effect = lambda record: record.id == 3
        self.assertEqual([records[2]], worker._fetch_shard_batch(1, 2))
        self.assertEqual(2, worker.repo.fetch_special_pending.call_args.args[3])

    def test_out_of_scope_nonzero_and_wrong_shard_rows_are_never_processed(self):
        worker = bare_worker()
        worker.repo.fetch_special_pending.side_effect = [[
            replace(special_record(), super_task_id=123), replace(special_record(), level=2),
            replace(special_record(), customer_id=0), replace(special_record(), customer_id=22),
        ], []]
        self.assertEqual([], worker._fetch_shard_batch(1, 4))
        worker.pipeline.process_one.assert_not_called()

    def test_dry_run_has_no_writeback_or_retry_budget_side_effect(self):
        worker = bare_worker(dry_run=True)
        worker.repo.acquire_record_lock.return_value.__enter__.return_value = True
        worker.repo.is_pending.return_value = True
        worker.pipeline.process_one.return_value = ProcessResult("m", "u", "auto", success=True)
        record = special_record()
        self.assertEqual("success", worker._process_record(task_for_record(record), record))
        self.assertFalse(worker.pipeline.process_one.call_args.kwargs["write_back"])
        worker.retry_store.reserve.assert_not_called()
        worker.repo.update_level_result.assert_not_called()
        config = minimal_config()
        config["audit"]["mysql_enabled"] = True
        self.assertFalse(build_special_config(config, dry_run=True)["audit"]["mysql_enabled"])

    def test_formal_mode_reuses_locked_level_check_and_persistent_retry(self):
        worker = bare_worker()
        record = special_record()
        task = task_for_record(record)
        worker.repo.acquire_record_lock.return_value.__enter__.return_value = True
        worker.repo.is_pending.return_value = True
        worker.retry_store.finish.return_value = {"status": "resolved"}
        worker.pipeline.process_one.return_value = ProcessResult("m", "u", "auto", success=True)
        self.assertEqual("success", worker._process_record(task, record))
        self.assertTrue(worker.pipeline.process_one.call_args.kwargs["write_back"])
        worker.retry_store.reserve.assert_called_once_with(task, record)
        worker.repo.is_pending.return_value = False
        self.assertEqual("skipped", worker._process_record(task, record))
        worker.pipeline.process_one.assert_called_once()

    def test_writer_uses_detail_customer_and_fixed_task_id_and_rejects_other_tasks(self):
        cfg = minimal_config()
        repo = SpecialTaskRepository(cfg["mysql"], app_config=cfg)
        repo.writer = MagicMock()
        repo.writer.update_level.return_value = {"code": 0, "data": 1}
        result = ClassifyResult("m", "u", "兴趣层", "text", success=True, industry_name="汽车")
        repo.update_level_result(None, special_record(), result)
        kwargs = repo.writer.update_level.call_args.kwargs
        self.assertEqual((21, TASK_ID, 2), (kwargs["customer_id"], kwargs["task_id"], kwargs["level"]))
        for record in (replace(special_record(), super_task_id=7), replace(special_record(), level=1)):
            with self.assertRaises(ValueError):
                repo.update_level_result(None, record, result)
        self.assertEqual(1, repo.writer.update_level.call_count)

    def test_same_car_pipeline_can_classify_without_brand_or_task_table(self):
        cfg = minimal_config()
        cfg["logging"]["error_file_enabled"] = False
        pipeline = ClassifyPipeline(cfg)
        pipeline.repo = MagicMock()
        pipeline.repo.get_level_code.return_value = 2
        pipeline.repo.update_level_result.return_value = {"state": "applied", "level": 2}
        pipeline.resolver = MagicMock()
        pipeline.resolver.resolve.return_value = ResolvedBlog("m", "u", "这辆汽车的试驾体验非常不错")
        pipeline.classifier.api_client = MagicMock()
        pipeline.classifier.api_client.classify_text.return_value = "最终分类结果：【兴趣层】"
        with patch.object(pipeline, "_cleanup_temp_files"):
            result = pipeline.process_one("m", "u", record=special_record(), write_back=True)
        self.assertTrue(result.success)
        self.assertEqual("汽车", result.industry_name)
        self.assertEqual("兴趣层", result.layer)
        self.assertEqual([], result.brand_terms)
        self.assertEqual(2, result.level_code)

    def test_media_cache_and_cleanup_are_isolated_from_normal_worker(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = minimal_config()
            cfg["media"]["cache_dir"] = str(Path(root) / "special_cache")
            cfg["logging"]["error_file_enabled"] = False
            pipeline = ClassifyPipeline(cfg)
            self.assertEqual(pipeline.cache_dir, pipeline.classifier.cache_dir)
            self.assertEqual(pipeline.cache_dir, pipeline.classifier.image_handler.cache_dir)
            self.assertEqual(pipeline.cache_dir, pipeline.classifier.video_handler.cache_dir)
            normal = Path(root) / "normal_cache" / "blog_images" / "m"
            special = Path(pipeline.cache_dir) / "blog_images" / "m"
            normal.mkdir(parents=True)
            special.mkdir(parents=True)
            (normal / "keep.jpg").touch()
            (special / "delete.jpg").touch()
            pipeline._cleanup_temp_files("m")
            self.assertTrue((normal / "keep.jpg").exists())
            self.assertFalse(special.exists())
        normal_pipeline = ClassifyPipeline(minimal_config())
        self.assertEqual(DEFAULT_CACHE_DIR, normal_pipeline.cache_dir)


if __name__ == "__main__":
    unittest.main()
