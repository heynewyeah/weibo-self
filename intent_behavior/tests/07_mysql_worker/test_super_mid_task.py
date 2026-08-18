#!/usr/bin/env python3
"""
super_mid_task 测试脚本。

用途：
1. 验证 [`super_mid_task`](intent_behavior/tests/07_mysql_worker/test_super_mid_task.py:1) 真实表结构能否被当前仓储正确读取
2. 在测试阶段通过 [`mysql.test_customer_id`](intent_behavior/config/config.yaml:157) 注入 customer_id，绕过主表暂时缺失该字段的问题
3. 输出有效任务、路由分表、待处理记录预览
4. 仅作为主表读取与路由验证脚本；若要直接验证分表样例，请使用 [`test_nature_ad_super_mid.py`](intent_behavior/tests/07_mysql_worker/test_nature_ad_super_mid.py:1)

运行示例：
  python3 tests/07_mysql_worker/test_super_mid_task.py --limit 10
"""

import argparse
import os
import sys
import yaml

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.db_client import MySQLTaskRepository
from src.utils import setup_logger


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="super_mid_task 测试脚本")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--limit", type=int, default=10, help="最多读取多少条任务")
    args = parser.parse_args()

    config = load_config(os.path.join(PROJECT_ROOT, args.config))
    logger = setup_logger(
        name="test_super_mid_task",
        log_dir=os.path.join(PROJECT_ROOT, config.get("logging", {}).get("dir", "logs")),
        level=config.get("logging", {}).get("level", "INFO"),
    )

    repo = MySQLTaskRepository(config.get("mysql", {}), logger)

    with repo.connect() as conn:
        tasks = repo.fetch_active_tasks(conn, limit=args.limit)
        print("=" * 80)
        print(f"有效任务数: {len(tasks)}")
        print("=" * 80)

        for idx, task in enumerate(tasks, 1):
            print(f"[{idx}] id={task.id} task_id={task.task_id} customer_id={task.customer_id} shard={task.shard_table}")
            print(f"    task_type={task.task_type} exec_status={task.exec_status}")
            pending = repo.fetch_pending_mids(conn, task, limit=3)
            print(f"    待处理记录预览: {len(pending)} 条")
            for row in pending:
                print(
                    f"      - row_id={row.id} mid={row.mid} uid={row.mid_uid} "
                    f"pids={row.mid_pids[:60]} fids={row.mid_fids[:60]}"
                )
            print("-" * 80)


if __name__ == "__main__":
    main()
