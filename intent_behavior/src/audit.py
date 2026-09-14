"""
生产运行审计。

主日志适合人读；本模块写入结构化 JSONL，供问题回溯、统计、重跑与后续接入日志平台。
目录格式：
  logs/runs/YYYYMMDD/<run_id>.jsonl
  logs/runs/YYYYMMDD/<run_id>_summary.json
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .utils import local_file_writes_allowed


class RunAudit:
    """一个进程运行实例对应一个审计文件。"""

    def __init__(self, config: Dict[str, Any], logger_: Optional[logging.Logger] = None):
        cfg = config.get("audit", {})
        # local_enabled 取代历史 enabled；保留 enabled 作为兼容别名，避免旧配置失效。
        self.local_enabled = bool(cfg.get("local_enabled", cfg.get("enabled", True)))
        self.logger = logger_ or logging.getLogger(__name__)
        self.record_model_output = bool(cfg.get("record_model_output", True))
        self.max_model_output_chars = int(cfg.get("max_model_output_chars", 2000))
        self.retention_days = int(cfg.get("retention_days", 30))
        self.mysql_enabled = bool(cfg.get("mysql_enabled", False))
        self.mysql_table = str(cfg.get("mysql_table", "nature_ad_mid_ai_audit"))
        self.mysql_flush_batch_size = max(1, int(cfg.get("mysql_flush_batch_size", 1)))
        self.mysql_cfg = config.get("mysql", {})
        self.storage_cfg = config.get("storage", {})
        self._local_disk_warning_reported = False
        self._db_rows = []

        project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        raw_dir = cfg.get("dir", "logs/runs")
        self.base_dir = raw_dir if os.path.isabs(raw_dir) else os.path.join(project_dir, raw_dir)
        now = datetime.now()
        self.run_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
        self.started_at = now
        self.path = ""
        self.summary_path = ""
        self.counts: Counter = Counter()
        self.task_counts: Dict[str, Counter] = {}
        self._finalized = False

        if self.local_enabled and self._can_write_local():
            try:
                day_dir = os.path.join(self.base_dir, now.strftime("%Y%m%d"))
                os.makedirs(day_dir, exist_ok=True)
                self.path = os.path.join(day_dir, f"{self.run_id}.jsonl")
                self.summary_path = os.path.join(day_dir, f"{self.run_id}_summary.json")
                self.cleanup_expired()
                self.write_event("run_started", {
                    "run_id": self.run_id,
                    "started_at": now.isoformat(),
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                })
            except OSError as exc:
                self.path = ""
                self.summary_path = ""
                self.logger.warning(
                    "创建本地 JSONL 审计目录失败，已仅保留控制台/MySQL 审计: %s", exc
                )
        elif self.local_enabled:
            self.logger.warning(
                "本地 JSONL 审计因磁盘空间阈值被关闭；MySQL 审计=%s",
                "开启" if self.mysql_enabled else "关闭",
            )

    def _can_write_local(self) -> bool:
        """低磁盘时停止 JSONL/summary 写入，避免审计文件耗尽运行盘。"""
        allowed = local_file_writes_allowed(
            {"storage": self.storage_cfg},
            self.base_dir,
        )
        if not allowed and not self._local_disk_warning_reported:
            self._local_disk_warning_reported = True
            self.logger.warning(
                "可用磁盘低于 storage.min_free_mb，本地 JSONL 审计将不再写入: %s",
                self.base_dir,
            )
        return allowed

    def _queue_db_result(self, payload: Dict[str, Any]) -> None:
        """可选写 MySQL 审计表；写失败不影响主链路，JSONL 始终作为保底。"""
        if not self.mysql_enabled:
            return
        self._db_rows.append(payload)
        if len(self._db_rows) >= self.mysql_flush_batch_size:
            self.flush_db()

    def flush_db(self) -> None:
        if not self.mysql_enabled or not self._db_rows:
            return
        rows, self._db_rows = self._db_rows, []
        try:
            import pymysql
            conn = pymysql.connect(
                host=self.mysql_cfg["host"],
                port=int(self.mysql_cfg.get("port", 3306)),
                user=self.mysql_cfg["user"],
                password=self.mysql_cfg["password"],
                database=self.mysql_cfg["database"],
                charset=self.mysql_cfg.get("charset", "utf8mb4"),
                autocommit=True,
            )
            sql = f"""
                INSERT INTO {self.mysql_table} (
                    run_id, event_time, customer_id, task_id, record_id, mid, uid,
                    status, industry_name, hit_mid_tag, hit_brand_name, is_forward,
                    forward_mid, forward_status, layer_name, media_type,
                    error_stage, error_detail, model_output, timings_json
                ) VALUES (
                    %(run_id)s, %(event_time)s, %(customer_id)s, %(task_id)s, %(record_id)s, %(mid)s, %(uid)s,
                    %(status)s, %(industry_name)s, %(hit_mid_tag)s, %(hit_brand_name)s, %(is_forward)s,
                    %(forward_mid)s, %(forward_status)s, %(layer_name)s, %(media_type)s,
                    %(error_stage)s, %(error_detail)s, %(model_output)s, %(timings_json)s
                )
            """
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.close()
        except Exception as exc:
            self.logger.warning("写入 MySQL 分类审计失败（JSONL 已保留）: %s", exc)

    def write_event(self, event: str, payload: Dict[str, Any]) -> None:
        if not self.local_enabled or not self.path or not self._can_write_local():
            return
        data = {
            "event": event,
            "event_time": datetime.now().isoformat(),
            "run_id": self.run_id,
            **payload,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            self.logger.error("写入运行审计失败 path=%s error=%s", self.path, exc)

    def record_result(self, result, record=None) -> None:
        """记录一条 mid 的完整处理结果。"""
        status = (
            "interrupted"
            if getattr(result, "interrupted", False)
            else (
                "fallback"
                if getattr(result, "fallback_level_written", False)
                else ("success" if result.success else "failed")
            )
        )
        task_id = str(record.super_task_id) if record is not None else ""
        self.counts[status] += 1
        self.counts[f"stage:{result.error_stage or 'none'}"] += 1
        if task_id:
            task_counter = self.task_counts.setdefault(task_id, Counter())
            task_counter[status] += 1
            task_counter[f"layer:{result.layer}"] += 1

        model_output = result.model_output or ""
        if self.record_model_output:
            model_output = model_output[:self.max_model_output_chars]
        else:
            model_output = ""

        payload = {
            "status": status,
            "customer_id": record.customer_id if record else None,
            "task_id": record.super_task_id if record else None,
            "record_id": record.id if record else None,
            "mid": result.mid,
            "uid": result.uid,
            "industry": result.industry_name,
            "hit_mid_tag": result.hit_mid_tag,
            "hit_brand_name": result.hit_brand_name,
            "is_forward": result.is_forward,
            "forward_mid": result.forward_mid,
            "forward_status": result.forward_status,
            "layer": result.layer,
            "media_type": result.media_type,
            "error_stage": result.error_stage,
            "error": (result.error or "")[:2000],
            "model_output": model_output,
            "timings_ms": result.timings.to_dict(),
            "fallback_level_written": bool(getattr(result, "fallback_level_written", False)),
            "interrupted": bool(getattr(result, "interrupted", False)),
        }
        self.write_event("mid_processed", payload)
        # MySQL 审计表面向分表消费记录，字段要求 customer/task/record 均存在。
        # 本地单条或文件预演没有分表 record 时只写 JSONL，避免无意义的写库失败日志。
        if record is None:
            return
        self._queue_db_result({
            "run_id": self.run_id,
            "event_time": datetime.now(),
            "customer_id": record.customer_id if record else None,
            "task_id": record.super_task_id if record else None,
            "record_id": record.id if record else None,
            "mid": result.mid,
            "uid": result.uid,
            "status": status,
            "industry_name": result.industry_name,
            "hit_mid_tag": result.hit_mid_tag,
            "hit_brand_name": result.hit_brand_name,
            "is_forward": int(bool(result.is_forward)),
            "forward_mid": result.forward_mid,
            "forward_status": result.forward_status,
            "layer_name": result.layer,
            "media_type": result.media_type,
            "error_stage": result.error_stage,
            "error_detail": (result.error or "")[:2000],
            "model_output": model_output,
            "timings_json": json.dumps(result.timings.to_dict(), ensure_ascii=False),
        })

    def record_task_summary(self, task, summary: Dict[str, int]) -> None:
        self.write_event("task_completed", {
            "task_id": task.task_id,
            "customer_id": task.customer_id,
            "industry": task.industry_name,
            "shard_table": task.shard_table,
            "summary": summary,
        })

    def finalize(self, extra: Optional[Dict[str, Any]] = None) -> None:
        """写入运行结束事件和便于读取的汇总 JSON；可重复调用。"""
        if self._finalized:
            return
        self._finalized = True
        self.flush_db()
        payload = {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now().isoformat(),
            "elapsed_s": round(time.time() - self.started_at.timestamp(), 1),
            "counts": dict(self.counts),
            "task_counts": {task_id: dict(cnt) for task_id, cnt in self.task_counts.items()},
            **(extra or {}),
        }
        self.write_event("run_finished", payload)
        if not self.local_enabled or not self.summary_path or not self._can_write_local():
            return
        try:
            with open(self.summary_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        except Exception as exc:
            self.logger.error("写入运行审计汇总失败 path=%s error=%s", self.summary_path, exc)

    def cleanup_expired(self) -> None:
        """按天删除过期审计目录，避免长期运行无限占磁盘。"""
        if self.retention_days <= 0 or not os.path.isdir(self.base_dir):
            return
        cutoff = (datetime.now() - timedelta(days=self.retention_days)).date()
        try:
            for name in os.listdir(self.base_dir):
                if len(name) != 8 or not name.isdigit():
                    continue
                try:
                    day = datetime.strptime(name, "%Y%m%d").date()
                except ValueError:
                    continue
                if day >= cutoff:
                    continue
                path = os.path.join(self.base_dir, name)
                if not os.path.isdir(path):
                    continue
                for root, dirs, files in os.walk(path, topdown=False):
                    for filename in files:
                        os.remove(os.path.join(root, filename))
                    for dirname in dirs:
                        os.rmdir(os.path.join(root, dirname))
                os.rmdir(path)
                self.logger.info("已清理过期运行审计目录: %s", path)
        except Exception as exc:
            self.logger.warning("清理过期运行审计失败: %s", exc)
