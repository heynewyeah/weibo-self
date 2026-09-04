"""
MySQL 分表任务消费与分类结果回写实现。

核心职责：
1. 连接 clue_collect_common 库
2. 查询 `super_mid_task` 中有效任务
3. 根据 customer_id % 20 路由到 `nature_ad_super_mid_{shard}`
4. 拉取 level=0 的待处理记录
5. 解析行业 / 品牌标签、转发字段，并映射为 BlogItem
6. 将分类结果通过 HTTP 接口回写到王燕威服务
   （POST /api/v1/super-mid/update-level，含 customer_id/task_id/mid/level/update_time）
7. 处理失败时记录错误日志，不再写回 MySQL 错误字段
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import BlogItem, ClassifyResult
from .result_writer import LevelUpdateClient


logger = logging.getLogger(__name__)


@dataclass
class TaskRecord:
    """`super_mid_task` 中的有效任务记录。"""

    id: int
    task_id: int
    customer_id: int
    task_type: int
    exec_status: int
    industry_tag_raw: str = ""
    brand_tag_raw: str = ""
    industry_values: List[str] = field(default_factory=list)
    brand_values: List[str] = field(default_factory=list)
    industry_name: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

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
    forward_mid: str = ""
    forward_text: str = ""
    hit_mid_tag: str = ""
    level: int = 0
    task_industry_name: str = ""
    task_brand_values: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def has_forward(self) -> bool:
        value = str(self.forward_mid or "").strip()
        return value not in {"", "0", "None", "null"}

    def to_blog_item(self) -> BlogItem:
        return BlogItem(
            mid=str(self.mid),
            uid=str(self.mid_uid),
            content=self.mid_text or "",
            pic_ids=_parse_media_ids(self.mid_pids),
            media_ids=_parse_media_ids(self.mid_fids),
            industry_name=self.task_industry_name,
            brand_values=list(self.task_brand_values),
            forward_mid=str(self.forward_mid or ""),
            forward_content=self.forward_text or "",
            extra={
                "row_id": self.id,
                "customer_id": self.customer_id,
                "super_task_id": self.super_task_id,
                "source": "mysql_shard",
                "has_forward": self.has_forward(),
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
        self.config = config
        self.app_config = app_config or {}
        self.logger = logger_ or logging.getLogger(__name__)
        self._driver = None

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

        cls_cfg = self.app_config.get("classification", {})
        self.supported_industries = set(cls_cfg.get("supported_industries", []))
        self.default_industry = cls_cfg.get("default_industry", "汽车")
        self.pending_level = int(cls_cfg.get("pending_level", 0))
        self.failure_label = cls_cfg.get("failure_label", "未识别")
        self.industry_rules = cls_cfg.get("industry_rules", {})

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
        task_type = int(self.config.get("active_task_type", 1))
        exec_status_done = int(self.config.get("inactive_exec_status", 5))
        end_time_field = self.config.get("task_end_time_field", "end_time")

        sql = f"""
        SELECT *
        FROM {table}
        WHERE task_type = %s
          AND (
                exec_status != %s
                OR (exec_status = %s AND {end_time_field} > DATE_SUB(NOW(), INTERVAL 1 DAY))
              )
        ORDER BY id ASC
        LIMIT %s
        """

        with conn.cursor() as cur:
            cur.execute(sql, (task_type, exec_status_done, exec_status_done, limit))
            rows = cur.fetchall() or []

        tasks: List[TaskRecord] = []
        for row in rows:
            task = self._row_to_task_record(row)
            if task is not None:
                tasks.append(task)
        return tasks

    def fetch_task_by_id(self, conn, task_id: int) -> Optional[TaskRecord]:
        """
        按 task_id 精确查询 `super_mid_task` 中的单条任务。

        与 fetch_active_tasks 使用相同的字段解析逻辑；
        返回 None 表示任务不存在，或任务缺少有效 customer_id 无法路由。
        """
        table = self.config.get("task_table", "super_mid_task")
        task_id_field = self.config.get("task_id_field", "task_id")
        sql = f"""
        SELECT *
        FROM {table}
        WHERE {task_id_field} = %s
        ORDER BY id ASC
        LIMIT 1
        """
        with conn.cursor() as cur:
            cur.execute(sql, (task_id,))
            row = cur.fetchone()
        if not row:
            return None
        return self._row_to_task_record(row)

    def _row_to_task_record(self, row: Dict[str, Any]) -> Optional[TaskRecord]:
        customer_field = self.config.get("task_customer_id_field", "customer_id")
        task_id_field = self.config.get("task_id_field", "task_id")
        industry_tag_field = self.config.get("task_industry_tag_field", "industry_tag")
        brand_tag_field = self.config.get("task_brand_tag_field", "brand_tag")
        fallback_customer_id = int(self.config.get("test_customer_id", 0) or 0)

        customer_id = int(row.get(customer_field, 0) or fallback_customer_id or 0)
        if customer_id <= 0:
            self.logger.warning(
                "任务缺少有效 customer_id，跳过: id=%s，可通过 mysql.test_customer_id 做测试注入",
                row.get("id"),
            )
            return None

        industry_tag_raw = str(row.get(industry_tag_field, "") or "")
        brand_tag_raw = str(row.get(brand_tag_field, "") or "")
        industry_values = parse_tag_json_values(industry_tag_raw)
        brand_values = parse_tag_json_values(brand_tag_raw)
        industry_name = self.resolve_industry(industry_values)

        # 所有任务都处理，不跳过任何行业
        # 非支持行业（如数码）会在分类阶段直接归为 level=6
        return TaskRecord(
            id=int(row.get("id", 0)),
            task_id=int(row.get(task_id_field, 0) or 0),
            customer_id=customer_id,
            task_type=int(row.get("task_type", 0) or 0),
            exec_status=int(row.get("exec_status", 0) or 0),
            industry_tag_raw=industry_tag_raw,
            brand_tag_raw=brand_tag_raw,
            industry_values=industry_values,
            brand_values=brand_values,
            industry_name=industry_name,
            raw=row,
        )

    def fetch_pending_mids(
        self,
        conn,
        task: TaskRecord,
        limit: int = 100,
        only_level_zero: bool = True,
        for_update: bool = True,
    ) -> List[MidRecord]:
        """
        拉取待处理记录。

        Args:
            for_update: 是否加行级锁（SELECT ... FOR UPDATE SKIP LOCKED）。
                        开启后多个 worker 实例不会重复处理同一条记录。
        """
        table = task.shard_table
        if not self.table_exists(conn, table):
            self.logger.warning("分表不存在，跳过: %s", table)
            return []

        task_match_field = self.config.get("shard_task_match_field", "super_task_id")
        task_match_value = task.task_id if task_match_field == "super_task_id" else task.id
        level_cond = f"AND level = {self.pending_level}" if only_level_zero else ""
        lock_clause = "FOR UPDATE" if for_update else ""
        sql = f"""
        SELECT *
        FROM {table}
        WHERE customer_id = %s
          AND {task_match_field} = %s
          {level_cond}
        ORDER BY id ASC
        LIMIT %s
        {lock_clause}
        """
        with conn.cursor() as cur:
            cur.execute(sql, (task.customer_id, task_match_value, limit))
            rows = cur.fetchall() or []

        return [self._row_to_mid_record(row, task=task) for row in rows]

    def fetch_pending_mids_by_table(
        self,
        conn,
        table_name: str,
        customer_id: Optional[int] = None,
        limit: int = 100,
        only_level_zero: bool = True,
        task: Optional[TaskRecord] = None,
    ) -> List[MidRecord]:
        if not self.table_exists(conn, table_name):
            self.logger.warning("分表不存在，跳过: %s", table_name)
            return []

        level_cond = f"WHERE level = {self.pending_level}" if only_level_zero else "WHERE 1=1"
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

        return [self._row_to_mid_record(row, task=task) for row in rows]

    def update_level_result(self, conn, record: MidRecord, result: ClassifyResult) -> Any:
        if self.writer is None:
            raise RuntimeError(
                "result_writer 未配置，无法回写结果。请在 config.yaml 中配置 result_writer.url"
            )

        level = self.get_level_code(record.task_industry_name or result.industry_name, result.layer)
        return self.writer.update_level(
            customer_id=record.customer_id,
            task_id=record.super_task_id,
            mid=record.mid,
            level=level,
            update_time=datetime.now().isoformat(),
        )

    def update_record_failure(self, conn, record: MidRecord, error_msg: str) -> None:
        self.logger.warning(
            "记录处理失败（未调用结果回写接口） mid=%s customer_id=%s task_id=%s error=%s",
            record.mid,
            record.customer_id,
            record.super_task_id,
            (error_msg or "未知异常")[:300],
        )

    def resolve_industry(self, industry_values: List[str]) -> str:
        """
        解析行业名称。
        - 如果 industry_values 中有支持的行业，直接返回
        - 如果 industry_values 非空但没有支持的行业，返回第一个值（如"数码"），后续归为 level=6
        - 如果 industry_values 为空（无行业标签），返回空字符串
        """
        for value in industry_values:
            if value in self.supported_industries:
                return value
        # 有行业标签但都不支持 → 返回原始值，后续归为 level=6
        if industry_values:
            return industry_values[0]
        # 无行业标签 → 返回空字符串
        return ""

    def get_level_code(self, industry_name: str, layer: str) -> int:
        """
        根据行业和层级获取 level 数值。
        - 支持的行业：从 industry_rules 中查找映射
        - 不支持的行业：统一返回 6（其他）
        """
        if industry_name in self.industry_rules:
            mapping = self.industry_rules[industry_name].get("level_mapping", {})
            if layer in mapping:
                return int(mapping[layer])
        # 不支持的行业或未知层级 → 统一归为 6（其他）
        other_code = 6
        # 尝试从默认行业规则中获取"其他"的映射值
        default_rules = self.industry_rules.get(self.default_industry, {})
        default_mapping = default_rules.get("level_mapping", {})
        if "其他" in default_mapping:
            other_code = int(default_mapping["其他"])
        return other_code if layer == "其他" else self.pending_level

    def _row_to_mid_record(self, row: Dict[str, Any], task: Optional[TaskRecord] = None) -> MidRecord:
        forward_mid_field = self.config.get("shard_forward_mid_field", "forward_mid")
        forward_text_field = self.config.get("shard_forward_text_field", "forward_text")
        hit_mid_tag_field = self.config.get("shard_hit_mid_tag_field", "hit_mid_tag")
        return MidRecord(
            id=int(row.get("id", 0)),
            customer_id=int(row.get("customer_id", 0)),
            super_task_id=int(row.get("super_task_id", 0)),
            mid=str(row.get("mid", "") or ""),
            mid_uid=str(row.get("mid_uid", "") or ""),
            mid_text=str(row.get("mid_text", "") or ""),
            mid_pids=str(row.get("mid_pids", "") or ""),
            mid_fids=str(row.get("mid_fids", "") or ""),
            forward_mid=str(row.get(forward_mid_field, "") or ""),
            forward_text=str(row.get(forward_text_field, "") or ""),
            hit_mid_tag=str(row.get(hit_mid_tag_field, "") or ""),
            level=int(row.get("level", 0) or 0),
            task_industry_name=task.industry_name if task else "",
            task_brand_values=list(task.brand_values) if task else [],
            raw=row,
        )


def parse_tag_json_values(raw: str) -> List[str]:
    """解析 industry_tag / brand_tag 的 JSON map，只取 value。"""
    if not raw:
        return []
    text = str(raw).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            values = []
            for value in data.values():
                text_value = str(value).strip()
                if text_value and text_value not in values:
                    values.append(text_value)
            return values
        if isinstance(data, list):
            values = []
            for value in data:
                text_value = str(value).strip()
                if text_value and text_value not in values:
                    values.append(text_value)
            return values
    except json.JSONDecodeError:
        pass
    return []


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
