#!/usr/bin/env python3
"""
正式 MySQL 持续消费入口。

这是生产环境唯一允许“持续查询 + 分类 + 回写”的脚本。

默认运行行为（不传 --once）：
  1. 查询当前有效任务；
  2. 查询每个任务下 level=0 的待处理博文；
  3. 依次完成反解、转发判断、媒体处理、AI 分类和结果回写；
  4. 等待 10 秒（由 config.yaml 的 worker.poll_interval_sec 配置）；
  5. 回到第 1 步，持续循环，直到人工按 Ctrl+C 停止进程。

首次上线推荐顺序：
  1. python3 scripts/production_preflight.py --strict
     仅检查配置、表、索引和重复数据；不会分类、不会回写、不会修改业务数据。
  2. python3 worker.py --config config/config.yaml --once
     只执行一轮，用于小批量观察日志、审计和回写结果。
  3. 确认无误后，执行下方默认命令持续运行。

运行示例：
  # 只跑一轮（首次验证）
  python3 worker.py --config config/config.yaml --once

  # 正式持续运行；按 Ctrl+C 停止
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
    parser = argparse.ArgumentParser(
        description="正式 MySQL 持续消费入口（默认持续轮询；Ctrl+C 停止）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅执行一轮查询、处理和回写后退出（首次上线建议先使用）",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logger(
        name="mysql_worker",
        log_dir=config.get("logging", {}).get("dir", "logs"),
        level=config.get("logging", {}).get("level", "INFO"),
        retention_days=int(config.get("logging", {}).get("retention_days", 30)),
    )

    worker = create_worker(config, logger)
    if args.once:
        summary = worker.run_once()
        worker.pipeline.audit.finalize({"worker_summary": summary, "mode": "once"})
        print(summary)
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
