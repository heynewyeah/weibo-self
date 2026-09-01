"""
MySQL 分表持续消费 worker。

流程：
1. 查询 `super_mid_task` 中有效任务
2. 动态路由到 `nature_ad_super_mid_{customer_id % 20}`
3. 拉取 `level=0` 的待分类 mid
4. 通过 ClassifyPipeline 执行完整链路：
   mid 反解 → 媒体类型判定 → 转发异常判断 → 分类 → 临时文件清理 → HTTP 回写
5. 异常场景记录错误日志

当前实现为单进程串行版本，优先保证：
- 完整链路（反解 + 分类 + 回写）
- 路由正确
- 字段映射正确
- 与 pipeline 统一复用
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .pipeline import ClassifyPipeline
from .db_client import MySQLTaskRepository, TaskRecord, MidRecord


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
        self.pipeline = ClassifyPipeline(config, self.logger)
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

        self.logger.info("")
        self.logger.info("#" * 80)
        self.logger.info(f"# 第 {self.stats.loops} 轮轮询开始")
        self.logger.info("#" * 80)

        with self.repo.connect() as conn:
            tasks = self.repo.fetch_active_tasks(conn, limit=active_task_limit)
            self.stats.task_count += len(tasks)
            loop_summary["tasks"] = len(tasks)

            if not tasks:
                self.logger.info("未发现有效任务，结束本轮轮询")
                return loop_summary

            self.logger.info(f"发现 {len(tasks)} 个有效任务")

            for task_idx, task in enumerate(tasks, 1):
                task_summary = self._process_task(conn, task, batch_limit, task_idx, len(tasks))
                loop_summary["pending"] += task_summary["pending"]
                loop_summary["success"] += task_summary["success"]
                loop_summary["fail"] += task_summary["fail"]

        self.logger.info("")
        self.logger.info(f"第 {self.stats.loops} 轮轮询完成: "
                         f"任务数={loop_summary['tasks']} "
                         f"记录数={loop_summary['pending']} "
                         f"成功={loop_summary['success']} "
                         f"失败={loop_summary['fail']}")

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
            except KeyboardInterrupt:
                self.logger.info("收到中断信号，worker 退出")
                break
            except Exception as exc:
                self.logger.exception("worker 轮询异常: %s", exc)

            if max_loops > 0 and loop_idx >= max_loops:
                self.logger.info("达到最大轮询次数 max_loops=%s，退出", max_loops)
                break

            time.sleep(poll_interval)

    def _process_task(self, conn, task: TaskRecord, batch_limit: int,
                      task_idx: int, total_tasks: int) -> Dict[str, int]:
        self.logger.info("")
        self.logger.info("-" * 80)
        self.logger.info(f"任务 [{task_idx}/{total_tasks}] "
                         f"task_id={task.task_id} "
                         f"customer_id={task.customer_id} "
                         f"industry={task.industry_name} "
                         f"shard={task.shard_table}")
        self.logger.info("-" * 80)

        pending_records = self.repo.fetch_pending_mids(conn, task, limit=batch_limit)

        task_summary = {
            "pending": len(pending_records),
            "success": 0,
            "fail": 0,
        }
        self.stats.pending_count += len(pending_records)

        if not pending_records:
            self.logger.info(f"  └─ 无待处理记录")
            return task_summary

        self.logger.info(f"  └─ 待处理记录数: {len(pending_records)}")

        for record_idx, record in enumerate(pending_records, 1):
            self.logger.info("")
            self.logger.info(f"  ┌─ mid [{record_idx}/{len(pending_records)}] "
                             f"mid={record.mid} uid={record.mid_uid} "
                             f"forward_mid={record.forward_mid or '无'}")

            ok = self._process_record(conn, task, record)
            if ok:
                task_summary["success"] += 1
                self.stats.success_count += 1
                self.logger.info(f"  └─ ✅ 处理成功")
            else:
                task_summary["fail"] += 1
                self.stats.fail_count += 1
                self.logger.info(f"  └─ ❌ 处理失败")

        self.logger.info("")
        self.logger.info(f"  任务完成: task_id={task.task_id} "
                         f"成功={task_summary['success']} "
                         f"失败={task_summary['fail']}")

        return task_summary

    def _process_record(self, conn, task: TaskRecord, record: MidRecord) -> bool:
        try:
            # 通过 ClassifyPipeline 执行完整链路：
            # mid 反解 → 分类 → 临时文件清理 → HTTP 回写
            process_result = self.pipeline.process_one(
                mid=record.mid,
                uid=record.mid_uid,
                mode="auto",
                write_back=True,
                record=record,
            )
            return process_result.success
        except Exception as exc:
            self.logger.exception(
                "记录处理异常 task_id=%s mid=%s error=%s",
                task.task_id,
                record.mid,
                exc,
            )
            try:
                self.repo.update_record_failure(conn, record, str(exc))
            except Exception as write_exc:
                self.logger.exception(
                    "失败回写再次失败 task_id=%s mid=%s error=%s",
                    task.task_id,
                    record.mid,
                    write_exc,
                )
            return False


def create_worker(config: Dict[str, Any], logger: Optional[logging.Logger] = None) -> MySQLShardWorker:
    return MySQLShardWorker(config, logger)
