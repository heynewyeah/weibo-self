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

import os
import sys
import logging
import time
from datetime import date
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
    fallback_count: int = 0
    fail_count: int = 0
    skip_count: int = 0


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

    def _acquire_instance_lock(self):
        """可选：同机单实例互斥（避免误启两份长期运行进程）。"""
        if not bool(self.worker_cfg.get("single_instance", False)):
            return None
        project_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        lock_path = self.worker_cfg.get("lock_file", "logs/worker.lock")
        if not os.path.isabs(lock_path):
            lock_path = os.path.join(project_dir, lock_path)
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        try:
            import fcntl
        except ImportError:
            self.logger.warning("当前平台不支持 flock，跳过单实例锁")
            return None
        lock_fd = open(lock_path, "a+")
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.logger.error("检测到另一个 worker 实例正在运行（锁文件 %s），本进程退出", lock_path)
            lock_fd.close()
            sys.exit(1)
        self.logger.info("已获取单实例锁: %s", lock_path)
        return lock_fd

    def run_once(self) -> Dict[str, Any]:
        self.stats.loops += 1
        batch_limit = int(self.worker_cfg.get("fetch_limit_per_task", 100))
        active_task_limit = int(self.worker_cfg.get("active_task_limit", 50))

        loop_summary = {
            "loops": self.stats.loops,
            "tasks": 0,
            "pending": 0,
            "success": 0,
            "fallback": 0,
            "fail": 0,
            "skipped": 0,
        }

        # 任务读取只占用短事务。后续网络 I/O（反解/下载/模型/回写）不持有数据库行锁。
        with self.repo.connect() as conn:
            tasks = self.repo.fetch_active_tasks(conn, limit=active_task_limit)

        self.stats.task_count += len(tasks)
        loop_summary["tasks"] = len(tasks)

        for task_idx, task in enumerate(tasks, 1):
            task_summary = self._process_task(task, batch_limit, task_idx, len(tasks))
            loop_summary["pending"] += task_summary["pending"]
            loop_summary["success"] += task_summary["success"]
            loop_summary["fallback"] += task_summary["fallback"]
            loop_summary["fail"] += task_summary["fail"]
            loop_summary["skipped"] += task_summary["skipped"]
            if task_summary["pending"]:
                self.pipeline.audit.record_task_summary(task, task_summary)

        if loop_summary["pending"]:
            self.logger.info("")
            self.logger.info(f"第 {self.stats.loops} 轮轮询完成: "
                             f"任务数={loop_summary['tasks']} "
                             f"记录数={loop_summary['pending']} "
                             f"成功={loop_summary['success']} "
                             f"兜底={loop_summary['fallback']} "
                             f"失败={loop_summary['fail']} "
                             f"跳过={loop_summary['skipped']}")

        return loop_summary

    def run_forever(self) -> None:
        poll_interval = float(self.worker_cfg.get("poll_interval_sec", 10))
        idle_heartbeat_sec = max(0.0, float(self.worker_cfg.get("idle_heartbeat_sec", 1800)))
        max_loops = int(self.worker_cfg.get("max_loops", 0))
        daily_reset = bool(self.worker_cfg.get("daily_reset", True))
        day_started = date.today()

        self.logger.info("MySQL 分表 worker 启动")
        self.logger.info(
            "配置: poll_interval=%ss fetch_limit_per_task=%s active_task_limit=%s "
            "idle_heartbeat_sec=%s daily_reset=%s single_instance=%s",
            poll_interval,
            self.worker_cfg.get("fetch_limit_per_task", 100),
            self.worker_cfg.get("active_task_limit", 50),
            idle_heartbeat_sec,
            daily_reset,
            bool(self.worker_cfg.get("single_instance", False)),
        )

        lock_fd = self._acquire_instance_lock()
        loop_idx = 0
        idle_loops = 0
        last_report_at = time.monotonic()
        try:
            while True:
                if daily_reset:
                    today = date.today()
                    if today != day_started:
                        day_started = today
                        loop_idx = 0
                        self.stats.loops = 0
                        self.logger.info("进入新的一天（%s），轮询计数已重置", today)
                loop_idx += 1
                try:
                    summary = self.run_once()
                    if summary["pending"]:
                        idle_loops = 0
                        last_report_at = time.monotonic()
                    else:
                        idle_loops += 1
                        now = time.monotonic()
                        if idle_heartbeat_sec and now - last_report_at >= idle_heartbeat_sec:
                            self.logger.info(
                                "空闲心跳: 最近 %s 轮无待处理记录，当前有效任务数=%s，worker 仍在运行",
                                idle_loops,
                                summary["tasks"],
                            )
                            idle_loops = 0
                            last_report_at = now
                except KeyboardInterrupt:
                    self.logger.info("收到中断信号，worker 退出")
                    break
                except Exception as exc:
                    self.logger.exception("worker 轮询异常: %s", exc)
                    idle_loops = 0
                    last_report_at = time.monotonic()

                if max_loops > 0 and loop_idx >= max_loops:
                    self.logger.info("达到最大轮询次数 max_loops=%s，退出", max_loops)
                    break

                time.sleep(poll_interval)
        finally:
            # 即使其他入口直接调用 run_forever，也要落运行汇总。
            self.pipeline.audit.finalize({"mode": "forever", "loops": loop_idx})
            if lock_fd is not None:
                lock_fd.close()

    def _process_task(self, task: TaskRecord, batch_limit: int,
                      task_idx: int, total_tasks: int) -> Dict[str, int]:
        # 仅作短时间只读拉取；真正处理前会用命名锁互斥并再次检查 level=0。
        with self.repo.connect() as conn:
            pending_records = self.repo.fetch_pending_mids(
                conn, task, limit=batch_limit, only_level_zero=True, for_update=False
            )

        task_summary = {
            "pending": len(pending_records),
            "success": 0,
            "fallback": 0,
            "fail": 0,
            "skipped": 0,
        }
        self.stats.pending_count += len(pending_records)

        if not pending_records:
            return task_summary

        self.logger.info("")
        self.logger.info("-" * 80)
        self.logger.info(f"任务 [{task_idx}/{total_tasks}] "
                         f"task_id={task.task_id} "
                         f"customer_id={task.customer_id} "
                         f"industry={task.industry_name} "
                         f"shard={task.shard_table}")
        self.logger.info("-" * 80)
        self.logger.info(f"  └─ 待处理记录数: {len(pending_records)}")

        for record_idx, record in enumerate(pending_records, 1):
            self.logger.info("")
            record_header = (
                f"  ┌─ [{record_idx}/{len(pending_records)}] "
                f"mid={record.mid} uid={record.mid_uid}"
            )
            if record.has_forward():
                record_header += f" forward_mid={record.forward_mid}"
            self.logger.info(record_header)

            try:
                outcome = self._process_record(task, record)
            except KeyboardInterrupt:
                self.logger.warning(
                    "收到 Ctrl+C，当前 mid=%s 未完成；保留 level=0，停止本轮处理",
                    record.mid,
                )
                raise
            if outcome == "success":
                task_summary["success"] += 1
                self.stats.success_count += 1
            elif outcome == "fallback":
                task_summary["fallback"] += 1
                self.stats.fallback_count += 1
            elif outcome == "skipped":
                task_summary["skipped"] += 1
                self.stats.skip_count += 1
                self.logger.info(f"  └─ ⏭ 已被其他实例处理或不再待处理，跳过")
            else:
                task_summary["fail"] += 1
                self.stats.fail_count += 1

        self.logger.info("")
        self.logger.info(f"  任务完成: task_id={task.task_id} "
                         f"成功={task_summary['success']} "
                         f"兜底={task_summary['fallback']} "
                         f"失败={task_summary['fail']} "
                         f"跳过={task_summary['skipped']}")

        return task_summary

    def _process_record(self, task: TaskRecord, record: MidRecord) -> str:
        try:
            lock_timeout = int(self.worker_cfg.get("record_lock_timeout_sec", 0))
            with self.repo.acquire_record_lock(record, timeout_sec=lock_timeout) as acquired:
                if not acquired:
                    return "skipped"
                if not self.repo.is_pending(record):
                    return "skipped"

                # 命名锁覆盖整条外部处理链路，但不持有数据库事务/行锁。
                process_result = self.pipeline.process_one(
                    mid=record.mid,
                    uid=record.mid_uid,
                    mode="auto",
                    write_back=True,
                    record=record,
                )
                if process_result.fallback_level_written:
                    return "fallback"
                return "success" if process_result.success else "fail"
        except KeyboardInterrupt:
            # 交给入口统一做审计 finalize 和进程退出；不能转成业务失败或回写 level=6。
            raise
        except Exception as exc:
            self.logger.exception(
                "记录处理异常 task_id=%s mid=%s error=%s",
                task.task_id,
                record.mid,
                exc,
            )
            try:
                self.repo.update_record_failure(None, record, str(exc))
            except Exception as write_exc:
                self.logger.exception(
                    "失败回写再次失败 task_id=%s mid=%s error=%s",
                    task.task_id,
                    record.mid,
                    write_exc,
                )
            return "fail"


def create_worker(config: Dict[str, Any], logger: Optional[logging.Logger] = None) -> MySQLShardWorker:
    return MySQLShardWorker(config, logger)
