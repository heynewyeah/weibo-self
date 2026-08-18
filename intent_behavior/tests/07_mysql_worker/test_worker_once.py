#!/usr/bin/env python3
"""
MySQL worker 单轮联调脚本。

用途：
1. 验证 [`worker.py`](intent_behavior/worker.py) 的单轮执行能力
2. 配合 [`setup_test_data.sql`](intent_behavior/tests/07_mysql_worker/setup_test_data.sql) 进行联调
3. 输出单轮执行摘要

运行示例：
  python3 tests/07_mysql_worker/test_worker_once.py
"""

import os
import sys
import yaml

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.worker import create_worker
from src.utils import setup_logger


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    config = load_config(os.path.join(PROJECT_ROOT, "config/config.yaml"))
    logger = setup_logger(
        name="test_mysql_worker_once",
        log_dir=os.path.join(PROJECT_ROOT, config.get("logging", {}).get("dir", "logs")),
        level=config.get("logging", {}).get("level", "INFO"),
    )

    worker = create_worker(config, logger)
    summary = worker.run_once()

    print("=" * 80)
    print("MySQL worker 单轮联调结果")
    print("=" * 80)
    print(f"loops   : {summary['loops']}")
    print(f"tasks   : {summary['tasks']}")
    print(f"pending : {summary['pending']}")
    print(f"success : {summary['success']}")
    print(f"fail    : {summary['fail']}")
    print("=" * 80)


if __name__ == "__main__":
    main()
