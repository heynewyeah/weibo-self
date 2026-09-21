#!/usr/bin/env python3
"""
指定 mid 的人工分类与受控回写脚本。

用途：
  - 对某一条具体博文排障、复测、人工复跑；
  - 默认只预演，不修改 level；
  - 显式传 --write-back 后才会调用回写接口；
  - 默认只允许处理 level=0，防止误覆盖客户可见的 level=1/2/3。

和正式 worker 的区别：
  - 本脚本只处理你指定的一个 task_id + mid，处理完立即退出；
  - 不会持续查询，不会处理其他任务；
  - 正式批量持续处理仍只使用 worker.py。

正式 worker 默认行为（供对照）：
  默认会持续查询 → 处理 level=0 → 回写 → 等 10 秒 → 下一轮；
  直到按 Ctrl+C 停止；
  --once 只跑一轮；
  首次上线推荐执行顺序：
    1. python3 scripts/production_preflight.py --strict
    2. python3 worker.py --config config/config.yaml --once
    3. python3 worker.py --config config/config.yaml

运行方式：
  # 只预演（推荐先执行；不回写）
  python3 scripts/manual_classify_mid.py \
    --task-id 1302305683722469377 --mid 5279586697085338

  # 对当前 level=0 的指定 mid 分类并回写
  python3 scripts/manual_classify_mid.py \
    --task-id 1302305683722469377 --mid 5279586697085338 --write-back

  # 已被错误兜底为 level=6 的记录：先人工恢复为 level=0，再分类并回写
  # 仅允许 level=6 → 0，绝不修改客户可见的 1/2/3。
  python3 scripts/manual_classify_mid.py \
    --task-id 1302305683722469377 --mid 5279586697085338 \
    --retry-level-6 --write-back

  # 强制文本/图片/视频模式（默认 auto）
  python3 scripts/manual_classify_mid.py \
    --task-id 1302305683722469377 --mid 5279586697085338 --mode image

中止与技术失败行为：
  - Ctrl+C、反解失败、媒体处理失败或模型调用失败都不会被自动回写为 level=6；
  - 当前 mid 保持 level=0（或保持原 level），JSONL 审计保留真实失败阶段与原因；
  - 下次运行 worker.py 时，仍会重新处理该 level=0 的 mid。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import yaml

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_DIR)

from src.db_client import MySQLTaskRepository
from src.pipeline import ClassifyPipeline
from src.utils import setup_logger


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="指定 mid 的人工分类与受控回写（默认只预演）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--task-id", type=int, required=True, help="super_mid_task.task_id")
    parser.add_argument("--mid", required=True, help="要处理的博文 mid")
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "text", "image", "video"],
        help="处理模式（默认 auto）",
    )
    parser.add_argument(
        "--write-back",
        action="store_true",
        help="调用 level 回写接口；默认关闭",
    )
    parser.add_argument(
        "--retry-level-6",
        action="store_true",
        help="仅当当前 level=6 时，将其恢复为 0 后再处理；必须配合 --write-back",
    )
    parser.add_argument(
        "--config",
        default=os.path.join(PROJECT_DIR, "config/config.yaml"),
        help="配置文件路径",
    )
    args = parser.parse_args()

    if args.task_id <= 0 or not str(args.mid).strip():
        parser.error("--task-id 必须为正整数，--mid 不能为空")
    if args.retry_level_6 and not args.write_back:
        parser.error("--retry-level-6 必须与 --write-back 一起使用")

    config = load_config(args.config)
    logger = setup_logger(
        "manual_classify_mid",
        log_dir=os.path.join(PROJECT_DIR, config.get("logging", {}).get("dir", "logs")),
        level=config.get("logging", {}).get("level", "INFO"),
        retention_days=int(config.get("logging", {}).get("retention_days", 30)),
        console_enabled=bool(config.get("logging", {}).get("console_enabled", True)),
        file_enabled=bool(config.get("logging", {}).get("file_enabled", True)),
        storage_config=config.get("storage", {}),
    )
    repo = MySQLTaskRepository(config["mysql"], logger, app_config=config)
    pipeline = ClassifyPipeline(config, logger)

    logger.info("=" * 72)
    logger.info("指定 mid 人工分类启动")
    logger.info("运行时间: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("task_id=%s mid=%s mode=%s write_back=%s retry_level_6=%s",
                args.task_id, args.mid, args.mode, args.write_back, args.retry_level_6)
    logger.info("=" * 72)

    with repo.connect() as conn:
        task = repo.fetch_task_by_id(conn, args.task_id)
        record = repo.fetch_mid_by_value(conn, task, args.mid) if task else None

    if task is None:
        pipeline.audit.finalize({"mode": "manual_mid", "reason": "task_not_found", "task_id": args.task_id})
        raise SystemExit(f"未找到 task_id={args.task_id}，或该任务缺少 operator_uid。")
    if record is None:
        pipeline.audit.finalize({
            "mode": "manual_mid",
            "reason": "mid_not_found",
            "task_id": task.task_id,
            "mid": args.mid,
        })
        raise SystemExit(f"在 {task.shard_table} 未找到 task_id={task.task_id}、mid={args.mid}。")

    logger.info(
        "命中记录: record_id=%s customer_id=%s shard=%s current_level=%s uid=%s",
        record.id, record.customer_id, task.shard_table, record.level, record.mid_uid,
    )

    if args.retry_level_6:
        if record.level != 6:
            pipeline.audit.finalize({
                "mode": "manual_mid",
                "reason": "retry_level_6_rejected",
                "task_id": task.task_id,
                "mid": record.mid,
                "current_level": record.level,
            })
            raise SystemExit(
                f"--retry-level-6 仅允许当前 level=6；当前 level={record.level}，未修改。"
            )
        if not repo.reset_to_pending_for_manual_retry(record):
            raise SystemExit("level=6 → 0 恢复失败：记录可能已被其他进程修改，请重新查询。")
        record.level = 0
        logger.warning("已受控恢复 level=6 → 0，开始重新分类 mid=%s", record.mid)

    if args.write_back and record.level != 0:
        pipeline.audit.finalize({
            "mode": "manual_mid",
            "reason": "writeback_rejected_non_pending",
            "task_id": task.task_id,
            "mid": record.mid,
            "current_level": record.level,
        })
        raise SystemExit(
            f"当前 level={record.level}，为避免覆盖已有结果，拒绝回写。"
            "如需重跑 level=6，请显式传 --retry-level-6 --write-back。"
        )

    try:
        # 与正式 worker 一样使用命名锁和 level=0 二次确认。
        with repo.acquire_record_lock(record) as acquired:
            if not acquired:
                raise SystemExit("当前 mid 正在被其他 worker 处理，未执行。")
            if args.write_back and not repo.is_pending(record):
                raise SystemExit("当前 mid 已不再是 level=0，未执行回写。")
            result = pipeline.process_one(
                mid=record.mid,
                uid=record.mid_uid,
                mode=args.mode,
                write_back=args.write_back,
                record=record,
            )
    except KeyboardInterrupt:
        logger.warning("收到 Ctrl+C：mid=%s 未完成，未额外回写 level=6。", record.mid)
        pipeline.audit.finalize({
            "mode": "manual_mid_interrupted",
            "task_id": task.task_id,
            "mid": record.mid,
        })
        raise SystemExit(130)

    pipeline.audit.finalize({
        "mode": "manual_mid",
        "task_id": task.task_id,
        "mid": record.mid,
        "write_back": args.write_back,
        "result": result.to_dict(),
    })

    logger.info(
        "处理结束: mid=%s success=%s fallback=%s layer=%s stage=%s",
        result.mid, result.success, result.fallback_level_written,
        result.layer, result.error_stage or "none",
    )
    logger.info("运行审计: %s", pipeline.audit.path)
    logger.info("运行汇总: %s", pipeline.audit.summary_path)
    raise SystemExit(0 if result.success else 1)


if __name__ == "__main__":
    main()
