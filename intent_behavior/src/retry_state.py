"""单条消费的持久化重试预算与完整失败档案（不访问业务库）。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


class RetryPersistenceError(RuntimeError):
    """无法可靠保存预算/失败信息时停止消费，不能退化为无限重试。"""


def failure_directory(config):
    project = Path(__file__).resolve().parents[1]
    audit = Path(config.get("audit", {}).get("dir", "logs/runs"))
    if not audit.is_absolute():
        audit = project / audit
    return audit.resolve().parent / "failures"


def record_key(record):
    identity = [str(record.customer_id), str(record.super_task_id), str(record.id), str(record.mid)]
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


class FailureRetryStore:
    def __init__(self, directory, max_retries=3, delays=(60, 300, 1800)):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "retry_state.sqlite3"
        self.max_attempts = 1 + int(max_retries)
        self.delays = tuple(float(delay) for delay in delays)
        if (not 1 <= self.max_attempts <= 4 or not self.delays
                or any(not math.isfinite(d) or d < 0 for d in self.delays)):
            raise ValueError("最多允许 3 次重试；重试间隔必须为非负数")
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS retry_state (
                        record_key TEXT PRIMARY KEY,
                        attempts INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        next_retry_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        failure_log TEXT NOT NULL,
                        payload TEXT NOT NULL
                    )
                """)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise RetryPersistenceError(f"无法创建重试状态目录：{self.directory}") from exc

    @contextmanager
    def _connect(self):
        conn = None
        try:
            conn = sqlite3.connect(str(self.path), timeout=10)
            conn.row_factory = sqlite3.Row
            # 只在短事务中写状态，不在外部接口调用期间持锁。
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                yield conn
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise RetryPersistenceError(f"重试状态持久化失败：{self.path}: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    def eligible(self, record, now=None):
        now = time.time() if now is None else now
        with self._connect() as conn:
            row = conn.execute("SELECT attempts, status, next_retry_at FROM retry_state WHERE record_key=?",
                               (record_key(record),)).fetchone()
        return row is None or (
            row["status"] not in ("exhausted", "resolved")
            and row["attempts"] < self.max_attempts
            and row["next_retry_at"] <= now
        )

    def reserve(self, task, record, now=None):
        """调用前预占一次预算；进程崩溃也不会凭空恢复处理次数。"""
        now = time.time() if now is None else now
        key = record_key(record)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM retry_state WHERE record_key=?", (key,)).fetchone()
            if row is not None:
                if (row["status"] in ("exhausted", "resolved")
                        or row["attempts"] >= self.max_attempts or row["next_retry_at"] > now):
                    return None
                attempt = row["attempts"] + 1
                payload = json.loads(row["payload"])
            else:
                attempt = 1
                payload = {"schema_version": 1, "record_key": key, "task": asdict(task),
                           "record": asdict(record), "history": []}
            # 保留每次实际取到的完整输入，不截断 raw 字段或模型输出。
            payload["current_task"] = asdict(task)
            payload["current_record"] = asdict(record)
            deadline = now + self.delays[min(attempt - 1, len(self.delays) - 1)]
            status = "exhausted" if attempt >= self.max_attempts else "inflight"
            payload.update(attempts=attempt, max_retries=self.max_attempts - 1,
                           status=status, updated_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
            payload["history"].append({"attempt": attempt, "started_at": payload["updated_at"],
                                       "status": "inflight", "task": asdict(task), "record": asdict(record)})
            log_path = str(self.directory / f"{key}.json")
            conn.execute("""
                INSERT INTO retry_state VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_key) DO UPDATE SET attempts=excluded.attempts,
                    status=excluded.status, next_retry_at=excluded.next_retry_at,
                    updated_at=excluded.updated_at, payload=excluded.payload
            """, (key, attempt, status, deadline, now, log_path,
                  json.dumps(payload, ensure_ascii=False, default=str)))
        return attempt

    def finish(self, record, result=None, exception="", interrupted=False, now=None):
        now = time.time() if now is None else now
        key = record_key(record)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM retry_state WHERE record_key=?", (key,)).fetchone()
            if row is None:
                raise RetryPersistenceError("处理结束时重试预算不存在，停止消费")
            payload = json.loads(row["payload"])
            attempt = row["attempts"]
            success = result is not None and (result.success or result.fallback_level_written)
            if interrupted:
                # 人工停止不消耗自动重试次数。
                attempts = max(0, attempt - 1)
                status, next_at = "waiting", now
            else:
                attempts = attempt
                status = "resolved" if success else ("exhausted" if attempt >= self.max_attempts else "waiting")
                next_at = now + self.delays[min(attempt - 1, len(self.delays) - 1)]
            payload["history"][-1].update(
                status="interrupted" if interrupted else ("success" if success else "failed"),
                finished_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                result=asdict(result) if result is not None else None,
                exception=exception,
            )
            payload.update(attempts=attempts, status=status,
                           updated_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                           next_retry_at=None if status in ("exhausted", "resolved") else next_at)
            # 首次成功没有失败档案，避免为所有正常博文制造冗余文件/状态。
            if success and attempt == 1 and len(payload["history"]) == 1:
                conn.execute("DELETE FROM retry_state WHERE record_key=?", (key,))
                return {"status": "resolved", "attempt": attempt, "failure_log": ""}
            conn.execute("""UPDATE retry_state SET attempts=?, status=?, next_retry_at=?,
                         updated_at=?, payload=? WHERE record_key=?""",
                         (attempts, status, next_at, now, json.dumps(payload, ensure_ascii=False, default=str), key))
        # SQLite 是原子保存的权威副本；JSON 是方便人工阅读的完整档案。
        # 若导出失败，保留数据库中的详情并停止消费，绝不带着缺失档案继续重试。
        self._export(row["failure_log"], payload)
        return {"status": status, "attempt": attempt, "failure_log": row["failure_log"],
                "next_retry_at": payload["next_retry_at"]}

    def _export(self, path, payload):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".failure-", delete=False) as handle:
                temporary = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise RetryPersistenceError(f"失败详情已保存在 {self.path}，JSON 导出失败：{path}") from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
