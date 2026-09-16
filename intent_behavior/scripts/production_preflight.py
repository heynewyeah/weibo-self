#!/usr/bin/env python3
"""
上线前检查脚本（只读，不修改数据）。

什么时候运行：
  - 第一次启动 worker 前；
  - 修改 MySQL 路由、表结构、索引或配置后；
  - 排查 worker 为什么查不到任务/数据时。

它会做什么：
  - 检查是否按 operator_uid 路由分表；
  - 检查任务表、现有分表和审计表是否存在；
  - 检查同一 customer_id + task_id + mid 是否有重复数据；
  - 检查 worker 查询 level=0 所需的索引是否存在。
  - 检查当前账号是否具备必要的读权限；若启用 MySQL 审计，检查 INSERT 权限。

它不会做什么：
  - 不调用模型、不下载媒体、不回写 level；
  - 不删除、不更新、不插入任何业务数据；
  - 不创建或修改表/索引。

检查内容：
1. 配置是否把 super_mid_task.operator_uid 用作 customer_id 路由；
2. 所有实际存在的 0~19 分表是否具备推荐组合索引，并确认活跃任务路由分表存在；
3. 是否存在 (customer_id, super_task_id, mid) 重复数据；
4. task_id 是否重复；
5. 可选审计表是否存在，且当前账号具备 INSERT 权限（audit.mysql_enabled=true 时必检）。

用法：
  cd intent_behavior
  # 常规检查：索引缺失只警告
  python3 scripts/production_preflight.py

  # 上线前推荐：索引缺失也视为检查失败
  python3 scripts/production_preflight.py --strict

退出码：
  0：通过；1：发现阻断问题；2：配置/连接异常。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, List, Set

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


def has_privilege(conn, schema: str, table: str, privilege: str) -> bool:
    """检查当前 MySQL 账号对目标表/库是否具备指定权限。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.table_privileges
            WHERE table_schema = %s
              AND table_name = %s
              AND privilege_type = %s
            UNION ALL
            SELECT 1 FROM information_schema.schema_privileges
            WHERE table_schema = %s
              AND privilege_type = %s
            LIMIT 1
            """,
            (schema, table, privilege.upper(), schema, privilege.upper()),
        )
        return cur.fetchone() is not None


def fetch_shard_tables(conn, schema: str, prefix: str) -> Dict[int, str]:
    """返回当前库实际存在的合法 0~19 分表，表名仅来自 information_schema。"""
    shard_table_re = re.compile(rf"^{re.escape(prefix)}([0-9]|1[0-9])$")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name LIKE %s
            """,
            (schema, f"{prefix}%"),
        )
        rows = cur.fetchall() or []

    shards = {}
    for row in rows:
        table_name = str(row["table_name"])
        match = shard_table_re.fullmatch(table_name)
        if match:
            shards[int(match.group(1))] = table_name
    return shards


def fetch_active_task_shards(conn, mysql_cfg: Dict[str, object]) -> Set[int]:
    """读取全部当前有效任务实际会路由到的分表编号，不受 worker 单轮 limit 影响。"""
    task_table = str(mysql_cfg["task_table"])
    customer_field = str(mysql_cfg["task_customer_id_field"])
    task_type = int(mysql_cfg.get("active_task_type", 1))
    inactive_status = int(mysql_cfg.get("inactive_exec_status", 5))
    end_time_field = str(mysql_cfg.get("task_end_time_field", "end_time"))

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT MOD({customer_field}, 20) AS shard_index
            FROM {task_table}
            WHERE task_type = %s
              AND (
                exec_status != %s
                OR (
                  exec_status = %s
                  AND {end_time_field} > DATE_SUB(NOW(), INTERVAL 1 DAY)
                )
              )
            """,
            (task_type, inactive_status, inactive_status),
        )
        return {
            int(row["shard_index"])
            for row in (cur.fetchall() or [])
            if row.get("shard_index") is not None
        }


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
            task_table = str(mysql_cfg.get("task_table", "super_mid_task"))
            shard_prefix = str(mysql_cfg.get("shard_table_prefix", "nature_ad_super_mid_"))
            schema = str(mysql_cfg["database"])

            for table in (task_table,):
                if not repo.table_exists(conn, table):
                    failures.append(f"缺少表: {table}")

            shard_tables = fetch_shard_tables(conn, schema, shard_prefix)
            if not shard_tables:
                failures.append(f"未发现任何合法分表: {shard_prefix}0 ~ {shard_prefix}19")

            active_task_shards = (
                fetch_active_task_shards(conn, mysql_cfg)
                if not failures
                else set()
            )
            missing_active_shards = sorted(active_task_shards - set(shard_tables))
            if missing_active_shards:
                failures.append(
                    "当前有效任务会路由到不存在的分表: "
                    + ", ".join(f"{shard_prefix}{index}" for index in missing_active_shards)
                )

            missing_shards = sorted(set(range(20)) - set(shard_tables))
            if missing_shards:
                warnings.append(
                    "未创建的分表（当前无有效任务路由到这些表时不阻断）："
                    + ", ".join(f"{shard_prefix}{index}" for index in missing_shards)
                )

            for shard_index, table in sorted(shard_tables.items()):
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
                    f"""
                    SELECT COUNT(*) AS c
                    FROM (
                        SELECT task_id FROM {task_table} GROUP BY task_id HAVING COUNT(*) > 1
                    ) AS duplicated
                    """
                )
                task_dups = int((cur.fetchone() or {}).get("c") or 0)
            if task_dups:
                failures.append(f"{task_table} 存在 {task_dups} 个重复 task_id")

            audit_cfg = config.get("audit", {})
            if audit_cfg.get("mysql_enabled"):
                table = audit_cfg.get("mysql_table", "nature_ad_mid_ai_audit")
                if not repo.table_exists(conn, table):
                    failures.append(f"audit.mysql_enabled=true 但审计表不存在: {table}")
                elif not has_privilege(conn, mysql_cfg["database"], table, "INSERT"):
                    failures.append(
                        f"audit.mysql_enabled=true 但当前账号无 {table} 的 INSERT 权限"
                    )
    except Exception as exc:
        print(f"[FAIL] 数据库自检失败: {exc}")
        sys.exit(2)

    print("原生内容站 AI 分层生产前自检")
    print(f"路由字段: {mysql_cfg.get('task_customer_id_field')}")
    print(
        "已检查分表: "
        + (
            ", ".join(
                table for _, table in sorted(shard_tables.items())
            )
            if shard_tables
            else "无"
        )
    )
    print(
        "当前有效任务路由分表: "
        + (
            ", ".join(f"{shard_prefix}{index}" for index in sorted(active_task_shards))
            if active_task_shards
            else "无"
        )
    )
    for item in warnings:
        print(f"[WARN] {item}")
    for item in failures:
        print(f"[FAIL] {item}")

    if failures or (args.strict and warnings):
        sys.exit(1)
    print("[PASS] 自检通过")


if __name__ == "__main__":
    main()
