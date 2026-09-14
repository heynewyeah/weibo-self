"""
工具模块：日志、输入校验、标签提取
"""

import os
import re
import logging
import shutil
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from typing import Any, Optional, List, Dict


class DiskGuardedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """空间低于阈值时停止本地日志写入，避免日志把运行盘写满。"""

    def __init__(self, *args, storage_config: Optional[Dict[str, Any]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage_config = storage_config or {}
        self._low_disk_reported = False

    def emit(self, record: logging.LogRecord) -> None:
        if not local_file_writes_allowed({"storage": self.storage_config}, self.baseFilename):
            # 这里不能再通过本 logger 记录 warning，否则会递归写入同一文件。
            # 控制台由业务入口的启动提示和后续日志继续提供。
            self._low_disk_reported = True
            return
        super().emit(record)


def setup_logger(
    name: str = "classifier",
    log_dir: str = "logs",
    level: str = "INFO",
    retention_days: int = 30,
    console_enabled: bool = True,
    file_enabled: bool = True,
    storage_config: Optional[Dict[str, Any]] = None,
) -> logging.Logger:
    """
    初始化日志器，可分别输出到控制台和本地文件。

    文件日志启用时按自然日自动轮转，默认保留 30 天：
    - 当前日志：{log_dir}/{name}.log
    - 历史日志：{name}.log.YYYY-MM-DD

    Args:
        name: 日志器名称
        log_dir: 日志目录
        level: 日志级别
        retention_days: 保留的历史天数
        console_enabled: 是否输出到终端
        file_enabled: 是否写入本地日志文件
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    if console_enabled:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    if file_enabled:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"{name}.log")
        file_handler = DiskGuardedTimedRotatingFileHandler(
            log_file,
            when="midnight",
            interval=1,
            backupCount=retention_days,
            encoding="utf-8",
            storage_config=storage_config,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # 禁用全部输出时，避免 logging 的 lastResort handler 向 stderr 额外写入。
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger


def local_file_writes_allowed(
    config: Dict,
    target_path: str,
) -> bool:
    """
    判断当前磁盘空间是否允许继续写本地文件。

    本地文件包括主日志、JSONL 审计、错误汇总与媒体缓存。空间不足时，调用方
    应保留控制台输出；若 MySQL 审计已配置且具备写权限，仍可保留远端审计。
    """
    storage_cfg = config.get("storage", {}) if config else {}
    if not bool(storage_cfg.get("low_disk_disable_local_writes", True)):
        return True

    min_free_mb = max(0, int(storage_cfg.get("min_free_mb", 0) or 0))
    if min_free_mb <= 0:
        return True

    probe_path = os.path.abspath(target_path)
    while not os.path.exists(probe_path):
        parent = os.path.dirname(probe_path)
        if parent == probe_path:
            break
        probe_path = parent

    try:
        free_mb = shutil.disk_usage(probe_path).free / (1024 * 1024)
    except OSError:
        # 无法检查空间时不静默阻断运行，实际写入异常仍由调用方记录。
        return True
    return free_mb >= min_free_mb


def extract_label(
    model_output: str,
    valid_labels: List[str],
    keyword_map: Optional[Dict[str, List[str]]] = None,
) -> Optional[str]:
    """
    从模型输出中提取分类标签。

    提取策略（按优先级）：
    1. 查找 "最终分类结果：【xxx】" 格式
    2. 查找所有 【xxx】 格式，取最后一个
    3. 取最后一行文本，尝试模糊匹配
    4. 在全文中搜索有效标签
    5. 按行业关键词映射兜底
    """
    if not model_output or not model_output.strip():
        return None

    label_set = set(valid_labels)

    matches = re.findall(r'最终分类结果：【([^】]+)】', model_output)
    if matches:
        for m in reversed(matches):
            if m in label_set:
                return m

    bracket_matches = re.findall(r'【([^】]+)】', model_output)
    for m in reversed(bracket_matches):
        if m in label_set:
            return m

    lines = [line.strip() for line in model_output.strip().split('\n') if line.strip()]
    if lines:
        last_line = lines[-1]
        for label in valid_labels:
            if label in last_line:
                return label

    for label in reversed(valid_labels):
        if label in model_output:
            return label

    if keyword_map:
        lowered = model_output.lower()
        for label, keywords in keyword_map.items():
            for kw in keywords:
                if kw.lower() in lowered:
                    return label

    return None


def extract_forward_status(model_output: str) -> Optional[str]:
    """从模型输出中提取转发判定结果。"""
    if not model_output or not model_output.strip():
        return None

    matches = re.findall(r'转发判定：【([^】]+)】', model_output)
    if matches:
        value = matches[-1].strip()
        if value in {"异常", "正常"}:
            return value

    bracket_matches = re.findall(r'【([^】]+)】', model_output)
    for value in reversed(bracket_matches):
        value = value.strip()
        if value in {"异常", "正常"}:
            return value

    # 宽松输出只做“明确正常”识别；不能因为“未发现异常/不存在异常”包含“异常”
    # 两个字，就反向误判为异常。异常必须由严格标签格式确认。
    text = model_output.strip()
    normal_markers = ("未发现异常", "不存在异常", "无异常", "正常转发", "判断正常")
    if any(marker in text for marker in normal_markers):
        return "正常"
    return None


def validate_input(mid: str, uid: str, content: str = "") -> tuple:
    """
    校验输入数据
    """
    if not mid or not mid.strip():
        return False, "mid为空"
    if not uid or not uid.strip():
        return False, "uid为空"
    if not content or not content.strip():
        pass
    return True, ""


def write_error_record(error_file: str, mid: str, uid: str,
                       error_type: str, error_detail: str):
    """写入错误记录到TSV文件"""
    os.makedirs(os.path.dirname(error_file), exist_ok=True)
    error_detail = error_detail.replace('\n', ' ').replace('\t', ' ')[:500]
    with open(error_file, "a", encoding="utf-8") as f:
        f.write(f"{mid}\t{uid}\t{error_type}\t{error_detail}\n")


def write_result(result_file: str, mid: str, uid: str,
                 layer: str, media_type: str = "text", confidence: str = ""):
    """写入分类结果到TSV文件"""
    os.makedirs(os.path.dirname(result_file), exist_ok=True)
    with open(result_file, "a", encoding="utf-8") as f:
        f.write(f"{mid}\t{uid}\t{layer}\t{media_type}\t{confidence}\n")
