#!/usr/bin/env python3
"""
清理过期的 MySQL AI 分类审计记录。

用途：
  `nature_ad_mid_ai_audit` 用于按 task_id / mid 回溯运行结果。该表不应无限增长；
  本脚本按小批次删除超过保留期的记录，降低长事务和锁影响。

运行方式：
  # 默认只预览将要清理的记录数，不修改数据库
  python3 scripts/cleanup_mysql_audit.py

  # 删除超过 90 天的记录（每批最多 10000 条，直到清完）
  python3 scripts/cleanup_mysql_audit.py --execute

  # 临时改为保留 30 天、每批 5000 条
  python3 scripts/cleanup_mysql_audit.py --retention-days 30 --batch-size 5000 --execute

建议：
  由 cron / XXL 每天低峰执行一次：
  cd /path/to/intent_behavior && python3 scripts/cleanup_mysql_audit.py --execute
"""

from __future__ import annotations

import argparse
import os
import sys

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_DIR)

import yaml

from src.db_client import MySQLTaskRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="清理过期 MySQL AI 分类审计记录")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"))
    parser.add_argument("--retention-days", type=int, default=0,
                        help="保留天数（0=读取 audit.mysql_retention_days）")
    parser.add_argument("--batch-size", type=int, default=0,
                        help="单批删除条数（0=读取 audit.mysql_cleanup_batch_size）")
    parser.add_argument("--execute", action="store_true",
                        help="真正执行删除；默认仅预览")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    audit_cfg = config.get("audit", {})
    table = str(audit_cfg.get("mysql_table", "nature_ad_mid_ai_audit"))
    retention_days = args.retention_days or int(audit_cfg.get("mysql_retention_days", 90))
    batch_size = args.batch_size or int(audit_cfg.get("mysql_cleanup_batch_size", 10000))

    if retention_days <= 0 or batch_size <= 0:
        raise SystemExit("retention-days 和 batch-size 必须为正整数")

    repo = MySQLTaskRepository(config["mysql"], app_config=config)
    with repo.connect() as conn:
        if not repo.table_exists(conn, table):
            raise SystemExit(f"审计表不存在: {table}")
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS c FROM {table} "
                "WHERE ctime < DATE_SUB(NOW(), INTERVAL %s DAY)",
                (retention_days,),
            )
            expired_count = int((cur.fetchone() or {}).get("c") or 0)

    print(f"审计表: {table}")
    print(f"保留天数: {retention_days}")
    print(f"过期记录: {expired_count}")
    if not args.execute:
        print("预览完成；如需删除，请加 --execute。")
        return

    deleted = 0
    while True:
        with repo.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {table} "
                    "WHERE ctime < DATE_SUB(NOW(), INTERVAL %s DAY) "
                    "LIMIT %s",
                    (retention_days, batch_size),
                )
                affected = cur.rowcount
        deleted += affected
        if affected < batch_size:
            break

    print(f"已删除过期审计记录: {deleted}")


if __name__ == "__main__":
    main()
