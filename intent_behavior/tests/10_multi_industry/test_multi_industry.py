#!/usr/bin/env python3
"""
多行业统一测试脚本
==================

目标：
1. 用一个脚本覆盖任务预览、分表预览、单条分类、批量分类、转发场景检查。
2. 通过不同参数调用，减少脚本分散。
3. 兼容汽车 / 奶茶双行业与转发博文场景。

示例：
  # 预览任务
  python3 tests/10_multi_industry/test_multi_industry.py --mode preview-tasks --limit 10

  # 预览分表记录
  python3 tests/10_multi_industry/test_multi_industry.py --mode preview-records --shard-index 1 --customer-id 2608812381 --limit 10

  # 从任务表批量分类
  python3 tests/10_multi_industry/test_multi_industry.py --mode batch-from-tasks --limit 20

  # 从分表批量分类并回写
  python3 tests/10_multi_industry/test_multi_industry.py --mode batch-from-shard --shard-index 1 --customer-id 2608812381 --limit 20 --write-back

  # 仅验证转发博文
  python3 tests/10_multi_industry/test_multi_industry.py --mode forward-check --shard-index 1 --customer-id 2608812381 --limit 20

  # 单条指定 mid 验证
  python3 tests/10_multi_industry/test_multi_industry.py --mode classify --shard-index 1 --customer-id 2608812381 --mid 5239377989207686
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

import yaml

from src.db_client import MySQLTaskRepository, TaskRecord, MidRecord
from src.pipeline import ClassifyPipeline, ProcessResult
from src.utils import setup_logger


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def task_to_dict(task: TaskRecord) -> Dict[str, Any]:
    return {
        "id": task.id,
        "task_id": task.task_id,
        "customer_id": task.customer_id,
        "task_type": task.task_type,
        "exec_status": task.exec_status,
        "industry_values": task.industry_values,
        "brand_values": task.brand_values,
        "industry_name": task.industry_name,
        "shard_table": task.shard_table,
    }


def record_to_dict(record: MidRecord) -> Dict[str, Any]:
    return {
        "row_id": record.id,
        "customer_id": record.customer_id,
        "super_task_id": record.super_task_id,
        "mid": record.mid,
        "uid": record.mid_uid,
        "industry_name": record.task_industry_name,
        "brand_values": record.task_brand_values,
        "forward_mid": record.forward_mid,
        "has_forward": record.has_forward(),
        "text_preview": (record.mid_text or "")[:120],
        "forward_text_preview": (record.forward_text or "")[:120],
        "level": record.level,
    }


def load_tasks(repo: MySQLTaskRepository, limit: int) -> List[TaskRecord]:
    with repo.connect() as conn:
        return repo.fetch_active_tasks(conn, limit=limit)


def load_records_by_table(
    repo: MySQLTaskRepository,
    table_name: str,
    customer_id: Optional[int],
    limit: int,
    only_level_zero: bool = True,
) -> List[MidRecord]:
    with repo.connect() as conn:
        return repo.fetch_pending_mids_by_table(
            conn,
            table_name=table_name,
            customer_id=customer_id,
            limit=limit,
            only_level_zero=only_level_zero,
        )


def load_records_from_tasks(
    repo: MySQLTaskRepository,
    limit: int,
    per_task_limit: int,
    industry: str,
) -> Tuple[List[MidRecord], List[TaskRecord]]:
    records: List[MidRecord] = []
    selected_tasks: List[TaskRecord] = []
    with repo.connect() as conn:
        tasks = repo.fetch_active_tasks(conn, limit=max(limit, 50))
        for task in tasks:
            if industry and task.industry_name != industry:
                continue
            selected_tasks.append(task)
            fetched = repo.fetch_pending_mids(conn, task, limit=per_task_limit, only_level_zero=True)
            records.extend(fetched)
            if len(records) >= limit:
                records = records[:limit]
                break
    return records, selected_tasks


def run_pipeline(
    pipeline: ClassifyPipeline,
    records: List[MidRecord],
    mode: str,
    write_back: bool,
    only_forward: bool = False,
    target_mid: str = "",
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    filtered = []
    for record in records:
        if target_mid and record.mid != target_mid:
            continue
        if only_forward and not record.has_forward():
            continue
        filtered.append(record)

    for record in filtered:
        process_result = pipeline.process_one(
            mid=record.mid,
            uid=record.mid_uid,
            mode=mode,
            write_back=write_back,
            record=record,
        )
        results.append({
            "record": record_to_dict(record),
            "result": process_result.to_dict(),
        })
    return results


def save_output(mode_name: str, payload: Dict[str, Any]) -> Tuple[str, str]:
    ensure_output_dir()
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(OUTPUT_DIR, f"{mode_name}_{run_ts}.json")
    txt_path = os.path.join(OUTPUT_DIR, f"{mode_name}_{run_ts}_summary.txt")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(payload.get("summary_text", "") + "\n")
    return json_path, txt_path


def build_summary_text(title: str, lines: List[str]) -> str:
    return "\n".join(["=" * 70, title, "=" * 70] + lines)


def main():
    parser = argparse.ArgumentParser(description="多行业统一测试脚本")
    parser.add_argument("--mode", required=True, choices=[
        "preview-tasks",
        "preview-records",
        "classify",
        "batch-from-tasks",
        "batch-from-shard",
        "forward-check",
    ])
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"))
    parser.add_argument("--shard-index", type=int, default=1)
    parser.add_argument("--customer-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--limit-per-task", type=int, default=10)
    parser.add_argument("--industry", default="", choices=["", "汽车", "奶茶"])
    parser.add_argument("--mid", default="")
    parser.add_argument("--write-back", action="store_true")
    parser.add_argument("--classify-mode", default="auto", choices=["auto", "text", "image", "video"])
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logger("test_multi_industry", log_dir=os.path.join(PROJECT_DIR, "logs"))
    repo = MySQLTaskRepository(config.get("mysql", {}), logger, app_config=config)
    pipeline = ClassifyPipeline(config, logger)
    table_name = f"{config.get('mysql', {}).get('shard_table_prefix', 'nature_ad_super_mid_')}{args.shard_index}"

    start = time.perf_counter()
    payload: Dict[str, Any] = {
        "mode": args.mode,
        "run_time": datetime.now().isoformat(),
        "params": vars(args),
    }

    if args.mode == "preview-tasks":
        tasks = load_tasks(repo, args.limit)
        if args.industry:
            tasks = [t for t in tasks if t.industry_name == args.industry]
        payload["tasks"] = [task_to_dict(t) for t in tasks]
        payload["summary_text"] = build_summary_text(
            "任务预览",
            [f"任务数: {len(tasks)}"] + [
                f"task_id={t.task_id} customer_id={t.customer_id} industry={t.industry_name} shard={t.shard_table} brands={t.brand_values}"
                for t in tasks
            ],
        )

    elif args.mode == "preview-records":
        records = load_records_by_table(repo, table_name, args.customer_id, args.limit, only_level_zero=False)
        if args.industry:
            records = [r for r in records if r.task_industry_name == args.industry]
        payload["records"] = [record_to_dict(r) for r in records]
        payload["summary_text"] = build_summary_text(
            "分表记录预览",
            [f"表名: {table_name}", f"记录数: {len(records)}"] + [
                f"row_id={r.id} mid={r.mid} industry={r.task_industry_name} forward_mid={r.forward_mid} level={r.level}"
                for r in records
            ],
        )

    elif args.mode == "classify":
        records = load_records_by_table(repo, table_name, args.customer_id, args.limit, only_level_zero=False)
        results = run_pipeline(
            pipeline,
            records,
            mode=args.classify_mode,
            write_back=args.write_back,
            only_forward=False,
            target_mid=args.mid,
        )
        payload["results"] = results
        payload["summary_text"] = build_summary_text(
            "单条/定向分类",
            [f"结果数: {len(results)}"] + [
                f"mid={x['record']['mid']} industry={x['result']['industry_name']} layer={x['result']['layer']} success={x['result']['success']} forward_status={x['result']['forward_status']}"
                for x in results
            ],
        )

    elif args.mode == "batch-from-tasks":
        records, tasks = load_records_from_tasks(repo, args.limit, args.limit_per_task, args.industry)
        results = run_pipeline(
            pipeline,
            records,
            mode=args.classify_mode,
            write_back=args.write_back,
            only_forward=False,
        )
        payload["tasks"] = [task_to_dict(t) for t in tasks]
        payload["results"] = results
        payload["summary_text"] = build_summary_text(
            "任务驱动批量分类",
            [f"任务数: {len(tasks)}", f"记录数: {len(results)}"] + [
                f"mid={x['record']['mid']} industry={x['result']['industry_name']} layer={x['result']['layer']} success={x['result']['success']}"
                for x in results[:20]
            ],
        )

    elif args.mode == "batch-from-shard":
        records = load_records_by_table(repo, table_name, args.customer_id, args.limit, only_level_zero=True)
        results = run_pipeline(
            pipeline,
            records,
            mode=args.classify_mode,
            write_back=args.write_back,
            only_forward=False,
        )
        payload["results"] = results
        payload["summary_text"] = build_summary_text(
            "分表批量分类",
            [f"表名: {table_name}", f"记录数: {len(results)}"] + [
                f"mid={x['record']['mid']} industry={x['result']['industry_name']} layer={x['result']['layer']} success={x['result']['success']}"
                for x in results[:20]
            ],
        )

    elif args.mode == "forward-check":
        records = load_records_by_table(repo, table_name, args.customer_id, args.limit, only_level_zero=False)
        results = run_pipeline(
            pipeline,
            records,
            mode=args.classify_mode,
            write_back=args.write_back,
            only_forward=True,
        )
        payload["results"] = results
        payload["summary_text"] = build_summary_text(
            "转发场景检查",
            [f"表名: {table_name}", f"转发记录数: {len(results)}"] + [
                f"mid={x['record']['mid']} forward_mid={x['record']['forward_mid']} industry={x['result']['industry_name']} forward_status={x['result']['forward_status']} layer={x['result']['layer']} success={x['result']['success']}"
                for x in results[:20]
            ],
        )

    payload["elapsed_s"] = round(time.perf_counter() - start, 2)
    json_path, txt_path = save_output(args.mode, payload)
    logger.info(payload["summary_text"])
    logger.info(f"JSON输出: {json_path}")
    logger.info(f"摘要输出: {txt_path}")


if __name__ == "__main__":
    main()
