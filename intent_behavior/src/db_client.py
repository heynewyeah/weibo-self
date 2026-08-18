"""
MySQL 分表任务消费与 level 回写实现。

核心职责：
1. 连接 clue_collect_common 库
2. 查询 [`super_mid_task`](intent_behavior/src/db_client.py:1) 中有效任务
3. 根据 customer_id % 20 路由到 [`nature_ad_super_mid_{shard}`](intent_behavior/src/db_client.py:1)
4. 拉取 level=0 的待处理记录
5. 映射为 [`BlogItem`](intent_behavior/src/models.py:28)
6. 将分类结果回写到分表 level / level_time

说明：
- 当前开发环境通常仅有 `nature_ad_super_mid_1` 可用，因此代码默认做“表存在性检测”。
- 为保证兼容性，优先使用 `pymysql`。若环境未安装，运行时会抛出明确错误。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import BlogItem, ClassifyResult


logger = logging.getLogger(__name__)


LEVEL_MAPPING = {
    "认知层": 1,
    "兴趣层": 2,
    "考虑层": 3,
    "未识别": 0,
}


@dataclass
class TaskRecord:
    """[`super_mid_task`](intent_behavior/src/db_client.py:1) 中的有效任务记录。"""

    id: int
    customer_id: int
    task_type: int
    exec_status: int
    raw: Dict[str, Any]

    @property
    def shard_index(self) -> int:
        return int(self.customer_id) % 20

    @property
    def shard_table(self) -> str:
        return f"nature_ad_super_mid_{self.shard_index}"


@dataclass
class MidRecord:
    """[`nature_ad_super_mid_x`](intent_behavior/src/db_client.py:1) 中待处理记录。"""

    id: int
    customer_id: int
    super_task_id: int
    mid: str
    mid_uid: str
    mid_text: str
    mid_pids: str
    mid_fids: str
    level: int
    raw: Dict[str, Any]

    def to_blog_item(self) -> BlogItem:
        return BlogItem(
            mid=str(self.mid),
            uid=str(self.mid_uid),
            content=self.mid_text or "",
            pic_ids=_parse_media_ids(self.mid_pids),
            media_ids=_parse_media_ids(self.mid_fids),
            extra={
                "row_id": self.id,
                "customer_id": self.customer_id,
                "super_task_id": self.super_task_id,
                "source": "mysql_shard",
            },
        )


class MySQLTaskRepository:
    """MySQL 任务仓储。"""

    def __init__(self, config: Dict[str, Any], logger_: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger_ or logging.getLogger(__name__)
        self._driver = None

    def _get_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            import pymysql  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "未安装 pymysql，请先执行: pip install pymysql"
            ) from exc
        self._driver = pymysql
        return self._driver

    @contextmanager
    def connect(self):
        pymysql = self._get_driver()
        conn = pymysql.connect(
            host=self.config["host"],
            port=int(self.config.get("port", 3306)),
            user=self.config["user"],
            password=self.config["password"],
            database=self.config["database"],
            charset=self.config.get("charset", "utf8mb4"),
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def table_exists(self, conn, table_name: str) -> bool:
        sql = """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """
        with conn.cursor() as cur:
            cur.execute(sql, (self.config["database"], table_name))
            return cur.fetchone() is not None

    def fetch_active_tasks(self, conn, limit: int = 100) -> List[TaskRecord]:
        table = self.config.get("task_table", "super_mid_task")
        sql = f"""
        SELECT *
        FROM {table}
        WHERE task_type = %s
          AND exec_status != %s
        ORDER BY id ASC
        LIMIT %s
        """
        task_type = int(self.config.get("active_task_type", 1))
        exec_status_done = int(self.config.get("inactive_exec_status", 5))

        with conn.cursor() as cur:
            cur.execute(sql, (task_type, exec_status_done, limit))
            rows = cur.fetchall() or []

        tasks: List[TaskRecord] = []
        customer_field = self.config.get("task_customer_id_field", "customer_id")
        for row in rows:
            customer_id = int(row.get(customer_field, 0) or 0)
            if customer_id <= 0:
                self.logger.warning("任务缺少有效 customer_id，跳过: id=%s", row.get("id"))
                continue
            tasks.append(
                TaskRecord(
                    id=int(row.get("id", 0)),
                    customer_id=customer_id,
                    task_type=int(row.get("task_type", 0) or 0),
                    exec_status=int(row.get("exec_status", 0) or 0),
                    raw=row,
                )
            )
        return tasks

    def fetch_pending_mids(
        self,
        conn,
        task: TaskRecord,
        limit: int = 100,
        only_level_zero: bool = True,
    ) -> List[MidRecord]:
        table = task.shard_table
        if not self.table_exists(conn, table):
            self.logger.warning("分表不存在，跳过: %s", table)
            return []

        level_cond = "AND level = 0" if only_level_zero else ""
        sql = f"""
        SELECT *
        FROM {table}
        WHERE customer_id = %s
          AND super_task_id = %s
          {level_cond}
        ORDER BY id ASC
        LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (task.customer_id, task.id, limit))
            rows = cur.fetchall() or []

        return [self._row_to_mid_record(row) for row in rows]

    def update_level_result(self, conn, record: MidRecord, result: ClassifyResult) -> None:
        table = f"nature_ad_super_mid_{int(record.customer_id) % 20}"
        if not self.table_exists(conn, table):
            raise RuntimeError(f"回写失败，分表不存在: {table}")

        level_value = LEVEL_MAPPING.get(result.layer, 0)
        sql = f"""
        UPDATE {table}
        SET level = %s,
            level_time = %s,
            mtime = CURRENT_TIMESTAMP
        WHERE id = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (level_value, datetime.now(), record.id))
        self.logger.info(
            "回写成功 table=%s id=%s mid=%s layer=%s(%s)",
            table,
            record.id,
            record.mid,
            result.layer,
            level_value,
        )

    def _row_to_mid_record(self, row: Dict[str, Any]) -> MidRecord:
        return MidRecord(
            id=int(row.get("id", 0)),
            customer_id=int(row.get("customer_id", 0)),
            super_task_id=int(row.get("super_task_id", 0)),
            mid=str(row.get("mid", "") or ""),
            mid_uid=str(row.get("mid_uid", "") or ""),
            mid_text=str(row.get("mid_text", "") or ""),
            mid_pids=str(row.get("mid_pids", "") or ""),
            mid_fids=str(row.get("mid_fids", "") or ""),
            level=int(row.get("level", 0) or 0),
            raw=row,
        )


def _parse_media_ids(raw: str) -> List[str]:
    """兼容逗号分隔、JSON数组、单值字符串。"""
    if not raw:
        return []
    text = str(raw).strip()
    if not text:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass

    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]

    return [text]


def build_blog_items(records: Sequence[MidRecord]) -> List[Tuple[MidRecord, BlogItem]]:
    return [(record, record.to_blog_item()) for record in records]
