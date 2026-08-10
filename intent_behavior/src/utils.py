"""
工具模块：日志、输入校验、标签提取
"""

import os
import re
import logging
from datetime import datetime
from typing import Optional, List


def setup_logger(name: str = "classifier", log_dir: str = "logs",
                 level: str = "INFO") -> logging.Logger:
    """
    初始化日志器，同时输出到控制台和文件

    Args:
        name: 日志器名称
        log_dir: 日志目录
        level: 日志级别

    Returns:
        logging.Logger 实例
    """
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"classify_{datetime.now().strftime('%Y%m%d')}.log")

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 避免重复添加handler
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def extract_label(model_output: str, valid_labels: List[str]) -> Optional[str]:
    """
    从模型输出中提取分类标签

    提取策略（按优先级）：
    1. 查找 "最终分类结果：【xxx】" 格式
    2. 查找所有 【xxx】 格式，取最后一个
    3. 取最后一行文本，尝试模糊匹配

    Args:
        model_output: 模型返回的完整文本
        valid_labels: 有效的标签列表，如 ["认知层", "兴趣层", "考虑层"]

    Returns:
        匹配到的标签字符串，未匹配返回 None
    """
    if not model_output or not model_output.strip():
        return None

    label_set = set(valid_labels)

    # 策略1：查找 "最终分类结果：【xxx】"
    matches = re.findall(r'最终分类结果：【([^】]+)】', model_output)
    if matches:
        for m in reversed(matches):  # 取最后一个匹配
            if m in label_set:
                return m

    # 策略2：查找所有 【xxx】，取最后一个有效标签
    bracket_matches = re.findall(r'【([^】]+)】', model_output)
    for m in reversed(bracket_matches):
        if m in label_set:
            return m

    # 策略3：取最后一行非空文本，模糊匹配
    lines = [line.strip() for line in model_output.strip().split('\n') if line.strip()]
    if lines:
        last_line = lines[-1]
        for label in valid_labels:
            if label in last_line:
                return label

    return None


def validate_input(mid: str, uid: str, content: str = "") -> tuple:
    """
    校验输入数据

    Args:
        mid: 博文ID
        uid: 用户ID
        content: 博文文字内容

    Returns:
        (is_valid: bool, error_msg: str)
    """
    if not mid or not mid.strip():
        return False, "mid为空"
    if not uid or not uid.strip():
        return False, "uid为空"
    if not content or not content.strip():
        # content 可以为空（纯图片/视频博文），但需要给出警告
        pass
    return True, ""


def write_error_record(error_file: str, mid: str, uid: str,
                       error_type: str, error_detail: str):
    """
    写入错误记录到TSV文件

    Args:
        error_file: 错误记录文件路径
        mid: 博文ID
        uid: 用户ID
        error_type: 错误类型
        error_detail: 错误详情
    """
    os.makedirs(os.path.dirname(error_file), exist_ok=True)
    # 截断过长的错误详情
    error_detail = error_detail.replace('\n', ' ').replace('\t', ' ')[:500]
    with open(error_file, "a", encoding="utf-8") as f:
        f.write(f"{mid}\t{uid}\t{error_type}\t{error_detail}\n")


def write_result(result_file: str, mid: str, uid: str,
                 layer: str, media_type: str = "text", confidence: str = ""):
    """
    写入分类结果到TSV文件

    Args:
        result_file: 结果文件路径
        mid: 博文ID
        uid: 用户ID
        layer: 分类结果
        media_type: 媒体类型 (text/image/video)
        confidence: 置信度（可选）
    """
    os.makedirs(os.path.dirname(result_file), exist_ok=True)
    with open(result_file, "a", encoding="utf-8") as f:
        f.write(f"{mid}\t{uid}\t{layer}\t{media_type}\t{confidence}\n")
