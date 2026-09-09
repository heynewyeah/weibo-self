#!/usr/bin/env python3
"""
原生内容站 AI 分层生产前自检（只读）。

检查内容：
1. 配置是否把 super_mid_task.operator_uid 用作 customer_id 路由；
2. 分表是否存在、待处理查询是否具备推荐组合索引；
3. 是否存在 (customer_id, super_task_id, mid) 重复数据；
4. task_id 是否重复；
5. 可选审计表是否存在（audit.mysql_enabled=true 时必检）。

用法：
  cd intent_behavior
  python3 scripts/production_preflight.py
  python3 scripts/production_preflight.py --strict

退出码：
  0：通过；1：发现阻断问题；2：配置/连接异常。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_DIR)

import yaml

from src.db_client import MySQLTaskRepository


def get_indexes(conn, table: str) -> Dict[str, List[str]]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW INDEX FROM {table}")
        rows = cur.fetchall() or []
    indexes = {}
    for row in rows:
        indexes.setdefault(row["Key_name"], []).append((row["Seq_in_index"], row["Column_name"]))
    return {name: [col for _, col in sorted(cols)] for name, cols in indexes.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="原生内容站 AI 分层生产前只读自检")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"))
    parser.add_argument("--strict", action="store_true", help="缺少推荐索引也视为失败")
    args = parser.parse_args()

    try:
        with open(args.config, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        mysql_cfg = config["mysql"]
    except Exception as exc:
        print(f"[FAIL] 配置读取失败: {exc}")
        sys.exit(2)

    failures = []
    warnings = []
    if mysql_cfg.get("task_customer_id_field") != "operator_uid":
        failures.append(
            "mysql.task_customer_id_field 必须为 operator_uid（原生内容站 customer_id 路由来源）"
        )

    repo = MySQLTaskRepository(mysql_cfg, app_config=config)
    try:
        with repo.connect() as conn:
            tables = ["super_mid_task", "nature_ad_super_mid_0", "nature_ad_super_mid_1"]
            for table in tables:
                if not repo.table_exists(conn, table):
                    failures.append(f"缺少表: {table}")

            for table in ("nature_ad_super_mid_0", "nature_ad_super_mid_1"):
                if not repo.table_exists(conn, table):
                    continue
                indexes = get_indexes(conn, table)
                recommended = ["customer_id", "super_task_id", "level", "id"]
                if recommended not in indexes.values():
                    warnings.append(f"{table} 缺少推荐索引 {recommended}")

                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT COUNT(*) AS c
                        FROM (
                            SELECT customer_id, super_task_id, mid
                            FROM {table}
                            GROUP BY customer_id, super_task_id, mid
                            HAVING COUNT(*) > 1
                        ) AS duplicated
                        """
                    )
                    duplicate_groups = int((cur.fetchone() or {}).get("c") or 0)
                if duplicate_groups:
                    failures.append(f"{table} 存在 {duplicate_groups} 组 task+mid 重复数据")

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM (
                        SELECT task_id FROM super_mid_task GROUP BY task_id HAVING COUNT(*) > 1
                    ) AS duplicated
                    """
                )
                task_dups = int((cur.fetchone() or {}).get("c") or 0)
            if task_dups:
                failures.append(f"super_mid_task 存在 {task_dups} 个重复 task_id")

            audit_cfg = config.get("audit", {})
            if audit_cfg.get("mysql_enabled"):
                table = audit_cfg.get("mysql_table", "nature_ad_mid_ai_audit")
                if not repo.table_exists(conn, table):
                    failures.append(f"audit.mysql_enabled=true 但审计表不存在: {table}")
    except Exception as exc:
        print(f"[FAIL] 数据库自检失败: {exc}")
        sys.exit(2)

    print("原生内容站 AI 分层生产前自检")
    print(f"路由字段: {mysql_cfg.get('task_customer_id_field')}")
    for item in warnings:
        print(f"[WARN] {item}")
    for item in failures:
        print(f"[FAIL] {item}")

    if failures or (args.strict and warnings):
        sys.exit(1)
    print("[PASS] 自检通过")


if __name__ == "__main__":
    main()
