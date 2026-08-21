"""
MySQL 分表持续消费 worker。

流程：
1. 查询 `super_mid_task` 中有效任务
2. 动态路由到 `nature_ad_super_mid_{customer_id % 20}`
3. 拉取 `level=0` 的待分类 mid
4. 调用 `BlogClassifier.classify_item()` 进行图文视频分类
5. 将分类结果通过 HTTP 接口回写到王燕威服务
   （POST /api/v1/super-mid/update-level，含 customer_id/task_id/mid/level/update_time）
6. 异常场景记录错误日志，不再写回 MySQL 错误字段

当前实现为单进程串行版本，优先保证：
- 路由正确
- 字段映射正确
- 回写正确
- 与现有分类链路解耦复用
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .classifier import BlogClassifier
from .db_client import MySQLTaskRepository, TaskRecord, MidRecord, build_blog_items


@dataclass
class WorkerStats:
    loops: int = 0
    task_count: int = 0
    pending_count: int = 0
    success_count: int = 0
    fail_count: int = 0


class MySQLShardWorker:
    """MySQL 分表分类 worker。"""

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        self.config = config
        self.worker_cfg = config.get("worker", {})
        self.mysql_cfg = config.get("mysql", {})
        self.logger = logger or logging.getLogger(__name__)
        self.repo = MySQLTaskRepository(self.mysql_cfg, self.logger, app_config=config)
        self.classifier = BlogClassifier(config, self.logger)
        self.stats = WorkerStats()

    def run_once(self) -> Dict[str, Any]:
        self.stats.loops += 1
        batch_limit = int(self.worker_cfg.get("fetch_limit_per_task", 100))
        active_task_limit = int(self.worker_cfg.get("active_task_limit", 50))

        loop_summary = {
            "loops": self.stats.loops,
            "tasks": 0,
            "pending": 0,
            "success": 0,
            "fail": 0,
        }

        with self.repo.connect() as conn:
            tasks = self.repo.fetch_active_tasks(conn, limit=active_task_limit)
            self.stats.task_count += len(tasks)
            loop_summary["tasks"] = len(tasks)

            if not tasks:
                self.logger.info("未发现有效任务，结束本轮轮询")
                return loop_summary

            for task in tasks:
                task_summary = self._process_task(conn, task, batch_limit)
                loop_summary["pending"] += task_summary["pending"]
                loop_summary["success"] += task_summary["success"]
                loop_summary["fail"] += task_summary["fail"]

        return loop_summary

    def run_forever(self) -> None:
        poll_interval = float(self.worker_cfg.get("poll_interval_sec", 10))
        max_loops = int(self.worker_cfg.get("max_loops", 0))

        self.logger.info("MySQL 分表 worker 启动")
        self.logger.info(
            "配置: poll_interval=%ss fetch_limit_per_task=%s active_task_limit=%s",
            poll_interval,
            self.worker_cfg.get("fetch_limit_per_task", 100),
            self.worker_cfg.get("active_task_limit", 50),
        )

        loop_idx = 0
        while True:
            loop_idx += 1
            try:
                summary = self.run_once()
                self.logger.info(
                    "轮询完成 loop=%s tasks=%s pending=%s success=%s fail=%s",
                    summary["loops"],
                    summary["tasks"],
                    summary["pending"],
                    summary["success"],
                    summary["fail"],
                )
            except KeyboardInterrupt:
                self.logger.info("收到中断信号，worker 退出")
                break
            except Exception as exc:
                self.logger.exception("worker 轮询异常: %s", exc)

            if max_loops > 0 and loop_idx >= max_loops:
                self.logger.info("达到最大轮询次数 max_loops=%s，退出", max_loops)
                break

            time.sleep(poll_interval)

    def _process_task(self, conn, task: TaskRecord, batch_limit: int) -> Dict[str, int]:
        pending_records = self.repo.fetch_pending_mids(conn, task, limit=batch_limit)
        pending_pairs = build_blog_items(pending_records)

        task_summary = {
            "pending": len(pending_pairs),
            "success": 0,
            "fail": 0,
        }
        self.stats.pending_count += len(pending_pairs)

        if not pending_pairs:
            self.logger.info(
                "任务无待处理记录 task_id=%s customer_id=%s shard=%s",
                task.id,
                task.customer_id,
                task.shard_table,
            )
            return task_summary

        self.logger.info(
            "开始处理任务 task_id=%s customer_id=%s shard=%s count=%s",
            task.id,
            task.customer_id,
            task.shard_table,
            len(pending_pairs),
        )

        for record, item in pending_pairs:
            ok = self._process_record(conn, task, record, item=item)
            if ok:
                task_summary["success"] += 1
                self.stats.success_count += 1
            else:
                task_summary["fail"] += 1
                self.stats.fail_count += 1

        return task_summary

    def _process_record(self, conn, task: TaskRecord, record: MidRecord, item) -> bool:
        self.logger.info(
            "处理中 task_id=%s row_id=%s mid=%s uid=%s",
            task.id,
            record.id,
            record.mid,
            record.mid_uid,
        )

        try:
            result = self.classifier.classify_item(item)
            self.repo.update_level_result(conn, record, result)
            return True
        except Exception as exc:
            self.logger.exception(
                "记录处理失败 task_id=%s row_id=%s mid=%s error=%s",
                task.id,
                record.id,
                record.mid,
                exc,
            )
            try:
                self.repo.update_record_failure(conn, record, str(exc))
            except Exception as write_exc:
                self.logger.exception(
                    "失败回写再次失败 task_id=%s row_id=%s mid=%s error=%s",
                    task.id,
                    record.id,
                    record.mid,
                    write_exc,
                )
            return False


def create_worker(config: Dict[str, Any], logger: Optional[logging.Logger] = None) -> MySQLShardWorker:
    return MySQLShardWorker(config, logger)
