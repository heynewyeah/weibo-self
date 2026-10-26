"""固定任务 888888 的独立分表消费者；复用普通 Worker 的汽车 Pipeline 和重试保护。"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import Any, Dict

from .db_client import MidRecord, MySQLTaskRepository, TaskRecord
from .worker import MySQLShardWorker


TASK_ID = 888888
SHARD_PREFIX = "nature_ad_super_mid_"
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@dataclass
class SpecialMidRecord(MidRecord):
    """特殊记录按实际来源表定位，不能从 customer_id 推导来源。"""

    source_shard: int = -1


def build_special_config(config: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """保留服务地址/汽车规则，隔离本地日志、审计、重试、锁和媒体缓存。"""
    isolated = copy.deepcopy(config)
    log_root = os.path.abspath(os.path.join(
        PROJECT_ROOT, config.get("logging", {}).get("dir", "logs"), "special_888888"
    ))
    if os.environ.get("DATAWORKS_SPECIAL_LOG_ROOT"):
        log_root = os.path.abspath(os.path.join(PROJECT_ROOT, os.environ["DATAWORKS_SPECIAL_LOG_ROOT"]))
        if log_root != os.path.join(PROJECT_ROOT, "logs", "special_888888"):
            raise ValueError("DataWorks 特殊任务日志必须独立放在 logs/special_888888")
    isolated.setdefault("logging", {}).update(
        dir=log_root, error_file=os.path.join(log_root, "error_records.tsv"),
        result_file=os.path.join(log_root, "result.tsv"),
    )
    isolated.setdefault("audit", {})["dir"] = os.path.join(log_root, "runs")
    if dry_run:
        isolated["audit"]["mysql_enabled"] = False
    isolated.setdefault("media", {})["cache_dir"] = os.path.join(
        PROJECT_ROOT, "output", ".cache_special_888888"
    )
    isolated.setdefault("worker", {}).update(
        single_instance=True, lock_file=os.path.join(log_root, "worker.lock"),
    )
    isolated.setdefault("mysql", {})["shard_table_prefix"] = SHARD_PREFIX
    isolated.setdefault("classification", {})["pending_level"] = 0
    if "汽车" not in isolated["classification"].get("supported_industries", []):
        raise ValueError("特殊任务需要配置现有的汽车行业规则和提示词")
    return isolated


def task_for_record(record: MidRecord) -> TaskRecord:
    """仅构造 Pipeline/审计需要的上下文，不读取或创建 super_mid_task。"""
    return TaskRecord(
        id=0, task_id=TASK_ID, customer_id=record.customer_id, task_type=1, exec_status=1,
        industry_name="汽车", industry_values=["汽车"], raw={"source": "special_888888"},
    )


class SpecialTaskRepository(MySQLTaskRepository):
    def fetch_special_pending(self, conn, shard: int, limit: int, after_id: int = 0):
        if not 0 <= shard < 20 or limit <= 0:
            raise ValueError("分表编号必须为 0～19，查询条数必须为正数")
        table = f"{SHARD_PREFIX}{shard}"
        if not self.table_exists(conn, table):
            self.logger.warning("特殊任务分表不存在，跳过: %s", table)
            return []
        # 固定 task_id / level；不使用任务表、operator_uid 或客户过滤条件。
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {table} "
                "WHERE super_task_id = %s AND level = 0 AND id > %s ORDER BY id ASC LIMIT %s",
                (TASK_ID, int(after_id), int(limit)),
            )
            rows = cur.fetchall() or []
        records = [SpecialMidRecord(**vars(self._row_to_mid_record(row)), source_shard=shard)
                   for row in rows]
        for record in records:
            record.task_industry_name = "汽车"
        return records

    @staticmethod
    def _source_table(record: MidRecord) -> str:
        shard = getattr(record, "source_shard", None)
        if record.super_task_id != TASK_ID or type(shard) is not int or not 0 <= shard < 20:
            raise ValueError("特殊任务必须有扫描时记录的合法来源分表，禁止按 customer_id 猜表")
        return f"{SHARD_PREFIX}{shard}"

    def _current_level(self, record: MidRecord):
        table = self._source_table(record)
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT level FROM {table} "
                    "WHERE id = %s AND super_task_id = %s AND mid = %s LIMIT 1",
                    (record.id, TASK_ID, record.mid),
                )
                row = cur.fetchone()
        return int(row["level"]) if row is not None and row.get("level") is not None else None

    def is_pending(self, record: MidRecord) -> bool:
        return record.super_task_id == TASK_ID and self._current_level(record) == 0

    def confirm_level(self, record: MidRecord, expected_level: int) -> bool:
        return self._current_level(record) == int(expected_level)

    def update_level_result(self, conn, record, result):
        if record.super_task_id != TASK_ID or record.level != 0 or record.customer_id <= 0:
            raise ValueError("特殊任务回写仅允许 task_id=888888、level=0 且 customer_id 有效的记录")
        if result.industry_name != "汽车":
            raise ValueError("特殊任务仅允许回写汽车行业分层")
        self._source_table(record)  # 在请求发出前验证来源，防止无来源记录产生外部回写。
        state = super().update_level_result(conn, record, result)
        # 特殊任务不能只信接口 data=1：接口可能仍按 customer_id 路由到别处。
        if not self.confirm_level(record, state["level"]):
            raise RuntimeError(
                f"回写接口返回成功，但实际来源表 {self._source_table(record)} 未达到目标状态: "
                f"id={record.id} mid={record.mid} target_level={state['level']}；停止扩大测试范围"
            )
        return state


class SpecialTaskWorker(MySQLShardWorker):
    def __init__(self, config, logger=None, *, dry_run=False, limit=0):
        if limit < 0:
            raise ValueError("limit 不能为负数")
        isolated = build_special_config(config, dry_run)
        super().__init__(isolated, logger)
        self.dry_run = dry_run
        self.limit = limit
        # 使用 Pipeline 已收敛为单次 HTTP 尝试的配置，重试仍由持久化预算负责。
        self.repo = SpecialTaskRepository(self.mysql_cfg, self.logger, app_config=self.pipeline.config)
        self.pipeline.repo = self.repo
        self.pipeline.error_log_dir = isolated["logging"]["dir"]

    def _valid_record(self, record, shard):
        valid = (record.super_task_id == TASK_ID and record.level == 0
                 and record.customer_id > 0 and 0 <= shard < 20
                 and getattr(record, "source_shard", None) == shard)
        if not valid:
            self.logger.error(
                "特殊任务记录来源/范围不合法，跳过且不回写: shard=%s id=%s customer_id=%s task_id=%s level=%s",
                shard, record.id, record.customer_id, record.super_task_id, record.level,
            )
        return valid

    def _fetch_shard_batch(self, shard, limit):
        selected, after_id = [], 0
        with self.repo.connect() as conn:
            while len(selected) < limit:
                page = self.repo.fetch_special_pending(conn, shard, limit, after_id)
                if not page:
                    break
                for record in page:
                    if self._valid_record(record, shard) and (
                        self.dry_run or self.retry_store.eligible(record)
                    ):
                        selected.append(record)
                        if len(selected) == limit:
                            break
                if len(page) < limit:
                    break
                # 冷却/耗尽/无效记录不能一直占住 LIMIT，阻塞其后的正常记录。
                after_id = max(record.id for record in page)
        return selected

    def _process_record(self, task, record):
        if record.super_task_id != TASK_ID or record.level != 0:
            return "skipped"
        if not self.dry_run:
            return super()._process_record(task, record)
        with self.repo.acquire_record_lock(record, timeout_sec=0) as acquired:
            if not acquired or not self.repo.is_pending(record):
                return "skipped"
            # 预演调用真实反解/模型，但不回写，也不消耗正式重试预算。
            result = self.pipeline.process_one(
                mid=record.mid, uid=record.mid_uid, mode="auto", write_back=False, record=record,
            )
            return "success" if result.success else "fail"

    def run_once(self) -> Dict[str, Any]:
        self.stats.loops += 1
        self.stats.task_count += 1
        summary = dict(loops=self.stats.loops, tasks=1, shards=0, pending=0,
                       success=0, fallback=0, fail=0, skipped=0, dry_run=self.dry_run)
        per_shard = int(self.worker_cfg.get("fetch_limit_per_task", 100))
        if per_shard <= 0:
            raise ValueError("fetch_limit_per_task 必须为正数（特殊入口按每分表限量）")
        for shard in range(20):
            remaining = self.limit - summary["pending"] if self.limit else per_shard
            if remaining <= 0:
                break
            records = self._fetch_shard_batch(shard, min(per_shard, remaining))
            summary["shards"] += 1
            if not records:
                continue
            self.logger.info("特殊任务 task_id=%s shard=%s%s 待处理=%s 模式=%s",
                             TASK_ID, SHARD_PREFIX, shard, len(records),
                             "预演（不回写）" if self.dry_run else "正式回写")
            summary["pending"] += len(records)
            self.stats.pending_count += len(records)
            shard_summary = dict(pending=len(records), success=0, fallback=0, fail=0, skipped=0)
            for index, record in enumerate(records, 1):
                self.logger.info("  [%s/%s] customer_id=%s mid=%s", index, len(records),
                                 record.customer_id, record.mid)
                outcome = self._process_record(task_for_record(record), record)
                summary[outcome] += 1
                shard_summary[outcome] += 1
                counter = {"success": "success_count", "fallback": "fallback_count",
                           "fail": "fail_count", "skipped": "skip_count"}[outcome]
                setattr(self.stats, counter, getattr(self.stats, counter) + 1)
            self.pipeline.audit.write_event("special_shard_completed", {
                "task_id": TASK_ID, "shard_table": f"{SHARD_PREFIX}{shard}",
                "summary": shard_summary, "dry_run": self.dry_run,
            })
        if summary["pending"]:
            self.logger.info("特殊任务第 %s 轮完成: task_id=%s 扫描分表=%s 记录=%s 成功=%s 失败=%s 跳过=%s",
                             summary["loops"], TASK_ID, summary["shards"], summary["pending"],
                             summary["success"], summary["fail"], summary["skipped"])
        self.pipeline.audit.write_live_status("running", loop_summary=summary, worker_stats=vars(self.stats))
        return summary
