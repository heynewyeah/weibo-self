"""
数据模型模块

定义项目中所有核心实体，使用 dataclass 保证类型安全与代码可读性。
"""

from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum


class MediaType(str, Enum):
    """博文媒体类型"""
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    UNKNOWN = "unknown"


class Layer(str, Enum):
    """营销分层标签"""
    COGNITIVE = "认知层"
    INTEREST = "兴趣层"
    CONSIDERATION = "考虑层"
    UNKNOWN = "未识别"


@dataclass
class BlogItem:
    """
    博文数据项（从 Hive / TSV / HDFS 提取的原始数据）

    Attributes:
        mid: 博文ID
        uid: 用户ID（部分数据源可能没有，用空字符串占位）
        content: 博文文字内容
        dt: 日期分区，如 20260701
        pic_ids: 图片pid列表（若数据源提供独立字段）
        media_ids: 视频media_id列表（若数据源提供独立字段）
        extra: 其他扩展字段
    """
    mid: str
    content: str
    dt: str = ""
    uid: str = ""
    pic_ids: List[str] = field(default_factory=list)
    media_ids: List[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def is_empty_content(self) -> bool:
        return not self.content or not self.content.strip()


@dataclass
class ClassifyResult:
    """
    分类结果实体

    Attributes:
        mid: 博文ID
        uid: 用户ID
        layer: 分类层级
        media_type: 媒体类型
        success: 是否成功
        error: 失败原因
        model_output: 模型原始输出（调试用）
    """
    mid: str
    uid: str
    layer: str
    media_type: str
    success: bool
    error: str = ""
    model_output: str = ""

    def to_tsv_row(self) -> str:
        """转为 TSV 行格式"""
        return f"{self.mid}\t{self.uid}\t{self.layer}\t{self.media_type}\t{self.success}"


@dataclass
class ExtractConfig:
    """数据提取配置"""
    source_type: str  # hive / local / hdfs
    start_dt: str
    end_dt: str
    industry: str = "汽车"
    bid_type: int = 4
    # 字段映射：告诉提取器各列含义
    field_mapping: dict = field(default_factory=dict)
    # 本地/HDFS 文件路径模板
    file_path_template: str = ""
    # Hive 相关
    hive_table_ad: str = "dplus_dm.dm_wb_ad_sfst_multi_day"
    hive_table_content: str = "ods_tblog_content"
    hive_table_media: str = "ods_ad_sfst_media_info"
