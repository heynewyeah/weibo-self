#!/usr/bin/env python3
"""
MySQL 分表 worker 启动入口。

示例：
  python3 worker.py --config config/config.yaml --once
  python3 worker.py --config config/config.yaml
"""

import argparse
import os
import sys
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.worker import create_worker
from src.utils import setup_logger


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="MySQL 分表 worker 启动入口")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="仅执行一轮轮询")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logger(
        name="mysql_worker",
        log_dir=config.get("logging", {}).get("dir", "logs"),
        level=config.get("logging", {}).get("level", "INFO"),
    )

    worker = create_worker(config, logger)
    if args.once:
        summary = worker.run_once()
        print(summary)
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
