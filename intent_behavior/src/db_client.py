"""
MySQL 分表任务消费与分类结果回写实现。

核心职责：
1. 连接 clue_collect_common 库
2. 查询 `super_mid_task` 中有效任务
3. 根据 customer_id % 20 路由到 `nature_ad_super_mid_{shard}`
4. 拉取 level=0 的待处理记录
5. 映射为 BlogItem
6. 将分类结果通过 HTTP 接口回写到王燕威服务
   （POST /api/v1/super-mid/update-level，含 customer_id/task_id/mid/level/update_time）
7. 处理失败时记录错误日志，不再写回 MySQL 错误字段

说明：
- 当前开发环境通常仅有 `nature_ad_super_mid_1` 可用，因此代码默认做“表存在性检测”。
- 为保证兼容性，优先使用 `pymysql`。若环境未安装，运行时会抛出明确错误。
- `super_mid_task` 当前真实表结构不包含 `customer_id` 字段，因此支持从配置指定测试字段，
  也支持通过 join / mock / 扩展字段方式兼容测试阶段。
- MySQL UPDATE 回写方式已废弃，统一改为 `result_writer` 配置的 HTTP 接口。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import BlogItem, ClassifyResult
from .result_writer import LevelUpdateClient


logger = logging.getLogger(__name__)


LEVEL_MAPPING = {
    "认知层": 1,
    "兴趣层": 2,
    "考虑层": 3,
    "未识别": 0,
}


@dataclass
class TaskRecord:
    """`super_mid_task` 中的有效任务记录。"""

    id: int
    task_id: int
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
    """`nature_ad_super_mid_x` 中待处理记录。"""

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

    def __init__(
        self,
        config: Dict[str, Any],
        logger_: Optional[logging.Logger] = None,
        app_config: Optional[Dict[str, Any]] = None,
    ):
        """
        Args:
            config: mysql 配置段
            logger_: 日志器
            app_config: 完整应用配置（用于读取 result_writer 等平级配置）
        """
        self.config = config
        self.app_config = app_config or {}
        self.logger = logger_ or logging.getLogger(__name__)
        self._driver = None

        # 初始化 HTTP 结果回写客户端（替代原 MySQL UPDATE）
        self.writer: Optional[LevelUpdateClient] = None
        writer_cfg = self.app_config.get("result_writer")
        if writer_cfg and writer_cfg.get("url"):
            self.writer = LevelUpdateClient(
                url=writer_cfg["url"],
                timeout=writer_cfg.get("timeout", 30),
                max_retry=writer_cfg.get("max_retry", 3),
                retry_backoff_base=writer_cfg.get("retry_backoff_base", 2.0),
                logger_=self.logger,
            )

    def _get_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            import pymysql  # type: ignore
        except ImportError as exc:
            raise RuntimeError("未安装 pymysql，请先执行: pip install pymysql") from exc
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
        task_id_field = self.config.get("task_id_field", "task_id")
        fallback_customer_id = int(self.config.get("test_customer_id", 0) or 0)

        for row in rows:
            customer_id = int(row.get(customer_field, 0) or fallback_customer_id or 0)
            if customer_id <= 0:
                self.logger.warning(
                    "任务缺少有效 customer_id，跳过: id=%s，可通过 mysql.test_customer_id 做测试注入",
                    row.get("id"),
                )
                continue
            tasks.append(
                TaskRecord(
                    id=int(row.get("id", 0)),
                    task_id=int(row.get(task_id_field, 0) or 0),
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

        task_match_field = self.config.get("shard_task_match_field", "super_task_id")
        task_match_value = task.task_id if task_match_field == "super_task_id" else task.id
        level_cond = "AND level = 0" if only_level_zero else ""
        sql = f"""
        SELECT *
        FROM {table}
        WHERE customer_id = %s
          AND {task_match_field} = %s
          {level_cond}
        ORDER BY id ASC
        LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (task.customer_id, task_match_value, limit))
            rows = cur.fetchall() or []

        return [self._row_to_mid_record(row) for row in rows]

    def fetch_pending_mids_by_table(
        self,
        conn,
        table_name: str,
        customer_id: Optional[int] = None,
        limit: int = 100,
        only_level_zero: bool = True,
    ) -> List[MidRecord]:
        """
        直接按分表名拉取待处理记录，不依赖 super_mid_task。

        适用于测试阶段直接消费某张分表中的 level=0 数据。
        """
        if not self.table_exists(conn, table_name):
            self.logger.warning("分表不存在，跳过: %s", table_name)
            return []

        level_cond = "WHERE level = 0" if only_level_zero else "WHERE 1=1"
        params: List[Any] = []
        if customer_id is not None:
            level_cond += " AND customer_id = %s"
            params.append(customer_id)
        params.append(limit)

        sql = f"""
        SELECT *
        FROM {table_name}
        {level_cond}
        ORDER BY id ASC
        LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []

        return [self._row_to_mid_record(row) for row in rows]

    def update_level_result(self, conn, record: MidRecord, result: ClassifyResult) -> Any:
        """
        将分类结果回写到 HTTP 接口（替代原 MySQL UPDATE）。

        Args:
            conn: 保留参数以兼容旧调用，实际不再使用
        """
        if self.writer is None:
            raise RuntimeError(
                "result_writer 未配置，无法回写结果。请在 config.yaml 中配置 result_writer.url"
            )

        return self.writer.update_level_from_result(
            customer_id=record.customer_id,
            task_id=record.super_task_id,
            mid=record.mid,
            layer=result.layer,
            update_time=datetime.now().isoformat(),
        )

    def update_record_failure(self, conn, record: MidRecord, error_msg: str) -> None:
        """
        记录处理失败信息到日志（原 MySQL 错误字段回写已废弃）。

        Args:
            conn: 保留参数以兼容旧调用，实际不再使用
        """
        self.logger.warning(
            "记录处理失败（未调用结果回写接口） mid=%s customer_id=%s task_id=%s error=%s",
            record.mid,
            record.customer_id,
            record.super_task_id,
            (error_msg or "未知异常")[:300],
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
