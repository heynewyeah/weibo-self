"""
数据提取模块

支持三种数据源：
1. HiveExtractor —— 通过本地 hive 命令行执行 SQL，实时拉取数据
2. LocalTSVExtractor —— 读取本地 TSV 文件（兼容现有 HDFS 导出文件）
3. HDFSExtractor —— 通过 hdfs dfs -cat 直接读取 HDFS 上的 TSV

所有提取器统一返回 List[BlogItem]。
"""

import os
import re
import csv
import logging
import subprocess
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any

from .models import BlogItem
from .media_handler import ImageHandler


logger = logging.getLogger(__name__)


class BaseExtractor(ABC):
    """数据提取器抽象基类"""

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    @abstractmethod
    def extract(self) -> List[BlogItem]:
        """执行提取，返回博文列表"""
        pass

    def _parse_tsv_lines(self, lines: List[str],
                         field_mapping: Dict[str, int]) -> List[BlogItem]:
        """
        通用 TSV 解析器

        Args:
            lines: TSV 行列表（不含表头）
            field_mapping: 字段名到列索引的映射，如 {"mid": 0, "content": 1, "dt": 2}

        Returns:
            BlogItem 列表
        """
        items = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")

            mid = self._safe_get(parts, field_mapping.get("mid", 0))
            content = self._safe_get(parts, field_mapping.get("content", 1))
            dt = self._safe_get(parts, field_mapping.get("dt", 2))
            uid = self._safe_get(parts, field_mapping.get("uid", -1), default="")

            # 图片 pid：优先从独立字段获取，否则从 content 解析
            pic_ids = []
            pic_col = field_mapping.get("pic_ids", -1)
            if pic_col >= 0 and pic_col < len(parts) and parts[pic_col]:
                raw = parts[pic_col].strip()
                # HDFS 导出可能是 JSON 数组字符串，如 ["pid1","pid2"]
                if raw.startswith("[") and raw.endswith("]"):
                    try:
                        pic_ids = [str(p).strip() for p in json.loads(raw) if p]
                    except json.JSONDecodeError:
                        pic_ids = [p.strip() for p in raw.split(",") if p.strip()]
                else:
                    pic_ids = [p.strip() for p in raw.split(",") if p.strip()]
            else:
                # 从 content JSON 中解析 pid
                pic_ids = ImageHandler._extract_pids_from_text(content)

            # 视频 media_id：优先从独立字段获取
            media_ids = []
            media_col = field_mapping.get("media_ids", -1)
            if media_col >= 0 and media_col < len(parts) and parts[media_col]:
                raw = parts[media_col].strip()
                if raw.startswith("[") and raw.endswith("]"):
                    try:
                        media_ids = [str(m).strip() for m in json.loads(raw) if m]
                    except json.JSONDecodeError:
                        media_ids = [m.strip() for m in raw.split(",") if m.strip()]
                else:
                    media_ids = [m.strip() for m in raw.split(",") if m.strip()]

            items.append(BlogItem(
                mid=mid,
                content=content,
                dt=dt,
                uid=uid,
                pic_ids=pic_ids,
                media_ids=media_ids
            ))
        return items

    @staticmethod
    def _safe_get(parts: List[str], idx: int, default: str = "") -> str:
        if idx < 0 or idx >= len(parts):
            return default
        return parts[idx].strip()


class HiveExtractor(BaseExtractor):
    """
    Hive 实时提取器

    通过本地 hive 命令执行 SQL，将结果拉取到内存后解析。
    适用于小批量调试；全量数据建议先用 run_hive.sh 导出到 HDFS/本地。
    """

    def extract(self) -> List[BlogItem]:
        cfg = self.config.get("hive", {})
        start_dt = cfg.get("start_dt", "")
        end_dt = cfg.get("end_dt", "")
        industry = cfg.get("industry", "汽车")
        bid_type = cfg.get("bid_type", 4)
        table_ad = cfg.get("table_ad", "dplus_dm.dm_wb_ad_sfst_multi_day")
        table_content = cfg.get("table_content", "ods_tblog_content")
        table_media = cfg.get("table_media", "ods_ad_sfst_media_info")

        # 允许用户完全自定义 SQL（覆盖默认逻辑）
        custom_sql = cfg.get("query_override", "").strip()

        if custom_sql:
            sql = custom_sql
        else:
            sql = self._build_default_sql(
                start_dt, end_dt, industry, bid_type,
                table_ad, table_content, table_media
            )

        self.logger.info(f"[Hive] 执行 SQL 提取数据: {start_dt} ~ {end_dt}")
        self.logger.debug(f"[Hive] SQL:\n{sql}")

        lines = self._run_hive_cli(sql)
        if not lines:
            self.logger.warning("[Hive] 未返回任何数据")
            return []

        # 解析表头，建立字段映射
        field_mapping = self._build_field_mapping(lines[0])
        data_lines = lines[1:] if lines else []

        self.logger.info(f"[Hive] 获取原始行数: {len(data_lines)}")
        items = self._parse_tsv_lines(data_lines, field_mapping)
        self.logger.info(f"[Hive] 解析成功: {len(items)} 条")
        return items

    def _build_default_sql(self, start_dt: str, end_dt: str,
                           industry: str, bid_type: int,
                           table_ad: str, table_content: str,
                           table_media: str) -> str:
        """
        构建默认 Hive SQL。

        默认只查询 mid + content + dt；
        若配置中启用媒体字段，则自动扩展 JOIN media 表。
        """
        enable_media = self.config.get("hive", {}).get("enable_media_join", False)

        if enable_media:
            # 扩展版：关联媒体表提取 media_id（视频）
            # 注：若字段名不同，请通过 query_override 自定义 SQL
            sql = f"""
SET hive.cli.print.header=true;
SELECT DISTINCT
  t.mid,
  t.content,
  t.dt,
  m.media_id,
  m.fid
FROM (
    SELECT DISTINCT mid, dt
    FROM {table_ad}
    WHERE dt >= '{start_dt}' AND dt <= '{end_dt}'
      AND market_industry_name = '{industry}'
      AND bid_type = {bid_type}
) ad
INNER JOIN {table_content} t
  ON ad.mid = t.mid AND ad.dt = t.dt
LEFT JOIN {table_media} m
  ON ad.mid = m.mid AND ad.dt = m.dt
WHERE t.dt >= '{start_dt}' AND t.dt <= '{end_dt}'
"""
        else:
            sql = f"""
SET hive.cli.print.header=true;
SELECT DISTINCT
  t.mid,
  t.content,
  t.dt
FROM (
    SELECT DISTINCT mid, dt
    FROM {table_ad}
    WHERE dt >= '{start_dt}' AND dt <= '{end_dt}'
      AND market_industry_name = '{industry}'
      AND bid_type = {bid_type}
) ad
INNER JOIN {table_content} t
  ON ad.mid = t.mid AND ad.dt = t.dt
WHERE t.dt >= '{start_dt}' AND t.dt <= '{end_dt}'
"""
        return sql.strip()

    def _run_hive_cli(self, sql: str) -> List[str]:
        """调用本地 hive 命令执行 SQL，返回输出行列表"""
        try:
            # 设置 Hive 参数
            hadoop_opts = os.environ.get("HADOOP_CLIENT_OPTS", "")
            if "-Xmx" not in hadoop_opts:
                os.environ["HADOOP_CLIENT_OPTS"] = "-Xmx2048m " + hadoop_opts

            cmd = ["hive", "-e", sql]
            self.logger.debug(f"[Hive] CMD: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800  # 30分钟超时
            )

            if result.returncode != 0:
                self.logger.error(f"[Hive] 执行失败: {result.stderr[:500]}")
                raise RuntimeError(f"Hive CLI 返回非零: {result.returncode}")

            lines = [ln.rstrip("\n") for ln in result.stdout.splitlines() if ln.strip()]
            return lines

        except subprocess.TimeoutExpired:
            self.logger.error("[Hive] 执行超时（30分钟）")
            raise
        except FileNotFoundError:
            self.logger.error("[Hive] 未找到 hive 命令，请确认 Hive 客户端已安装并在 PATH 中")
            raise

    def _build_field_mapping(self, header_line: str) -> Dict[str, int]:
        """根据表头建立字段映射"""
        cols = [c.strip().lower() for c in header_line.split("\t")]
        mapping = {}
        for idx, col in enumerate(cols):
            if col in ("mid", "content", "dt", "uid", "pic_ids", "media_ids", "fid"):
                mapping[col] = idx
        # 兜底：若没有识别到，按默认顺序
        if "mid" not in mapping and len(cols) >= 1:
            mapping["mid"] = 0
        if "content" not in mapping and len(cols) >= 2:
            mapping["content"] = 1
        if "dt" not in mapping and len(cols) >= 3:
            mapping["dt"] = 2
        return mapping


class LocalTSVExtractor(BaseExtractor):
    """本地 TSV 文件提取器"""

    def extract(self) -> List[BlogItem]:
        cfg = self.config.get("local", {})
        file_path = cfg.get("file_path", "")
        has_header = cfg.get("has_header", False)
        field_mapping = cfg.get("field_mapping", {})

        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError(f"本地文件不存在: {file_path}")

        self.logger.info(f"[Local] 读取本地文件: {file_path}")

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]

        if not lines:
            return []

        if has_header:
            # 有表头时，用表头覆盖 field_mapping
            header_mapping = self._parse_header(lines[0])
            header_mapping.update(field_mapping)
            field_mapping = header_mapping
            data_lines = lines[1:]
        else:
            data_lines = lines

        items = self._parse_tsv_lines(data_lines, field_mapping)
        self.logger.info(f"[Local] 解析成功: {len(items)} 条")
        return items

    def _parse_header(self, header_line: str) -> Dict[str, int]:
        cols = [c.strip().lower() for c in header_line.split("\t")]
        mapping = {}
        for idx, col in enumerate(cols):
            mapping[col] = idx
        return mapping


class HDFSExtractor(BaseExtractor):
    """
    HDFS 文件提取器

    通过 hdfs dfs -cat 直接读取 HDFS 上的 TSV 文件，无需先下载到本地。
    """

    def extract(self) -> List[BlogItem]:
        cfg = self.config.get("hdfs", {})
        hdfs_path = cfg.get("file_path", "")
        has_header = cfg.get("has_header", False)
        field_mapping = cfg.get("field_mapping", {})

        if not hdfs_path:
            raise ValueError("HDFS 路径未配置: hdfs.file_path")

        self.logger.info(f"[HDFS] 读取 HDFS 文件: {hdfs_path}")
        lines = self._run_hdfs_cat(hdfs_path)

        if not lines:
            return []

        if has_header:
            header_mapping = self._parse_header(lines[0])
            header_mapping.update(field_mapping)
            field_mapping = header_mapping
            data_lines = lines[1:]
        else:
            data_lines = lines

        items = self._parse_tsv_lines(data_lines, field_mapping)
        self.logger.info(f"[HDFS] 解析成功: {len(items)} 条")
        return items

    def _run_hdfs_cat(self, hdfs_path: str) -> List[str]:
        """调用 hdfs dfs -cat 读取文件内容"""
        try:
            # 支持通配符 part-*
            if not hdfs_path.endswith("*") and "/part-" not in hdfs_path:
                # 自动探测 part 文件
                check_cmd = ["hdfs", "dfs", "-ls", hdfs_path]
                check_result = subprocess.run(
                    check_cmd, capture_output=True, text=True, timeout=60
                )
                if "part-" in check_result.stdout:
                    hdfs_path = f"{hdfs_path.rstrip('/')}/part-*"

            cmd = ["hdfs", "dfs", "-cat", hdfs_path]
            self.logger.debug(f"[HDFS] CMD: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600
            )

            if result.returncode != 0:
                self.logger.error(f"[HDFS] 读取失败: {result.stderr[:500]}")
                raise RuntimeError(f"hdfs dfs -cat 返回非零: {result.returncode}")

            lines = [ln.rstrip("\n") for ln in result.stdout.splitlines() if ln.strip()]
            return lines

        except subprocess.TimeoutExpired:
            self.logger.error("[HDFS] 读取超时（10分钟）")
            raise
        except FileNotFoundError:
            self.logger.error("[HDFS] 未找到 hdfs 命令，请确认 Hadoop 客户端已安装并在 PATH 中")
            raise

    def _parse_header(self, header_line: str) -> Dict[str, int]:
        cols = [c.strip().lower() for c in header_line.split("\t")]
        mapping = {}
        for idx, col in enumerate(cols):
            mapping[col] = idx
        return mapping


def create_extractor(config: Dict[str, Any],
                     logger: Optional[logging.Logger] = None) -> BaseExtractor:
    """
    工厂函数：根据配置自动创建对应的提取器

    Args:
        config: 完整配置字典，需包含 extractor 段
        logger: 日志器

    Returns:
        BaseExtractor 实例
    """
    extractor_cfg = config.get("extractor", {})
    source_type = extractor_cfg.get("source_type", "local")

    if source_type == "hive":
        return HiveExtractor(extractor_cfg, logger)
    elif source_type == "hdfs":
        return HDFSExtractor(extractor_cfg, logger)
    elif source_type == "local":
        return LocalTSVExtractor(extractor_cfg, logger)
    else:
        raise ValueError(f"不支持的 source_type: {source_type}")
