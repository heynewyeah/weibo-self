#!/usr/bin/env python3
"""
单任务流水线 v1 - 指定 task_id，只处理该任务中固定数量的待分类 mid
====================================================================
功能：
  1. 按 --task-id 精确查询 super_mid_task 中的任务
  2. 根据 customer_id % 20 路由到 nature_ad_super_mid_x 分表
  3. 只读取该任务下 level=0 的记录，最多 --limit 条
  4. 对每条 mid 走完整链路：
     mid 反解 → 转发判断 → 分类 → 临时文件清理 → HTTP 回写
  5. 处理完即退出（不轮询、不触碰其他任务）

运行方式：
  # 仅预演指定任务下的 10 条待分类 mid（默认不回写）
  python3 run_single_task.py --task-id 1301222511089811457 --limit 10

  # 明确开启回写（仅用于受控联调；正式持续处理请使用 worker.py）
  python3 run_single_task.py --task-id 1301222511089811457 --limit 10 --write-back

  # 自定义模式 / 配置文件
  python3 run_single_task.py --task-id 1301222511089811457 --limit 20 --mode auto
  python3 run_single_task.py --task-id 1301222511089811457 --limit 20 --config config/config.yaml

输出：
  - 终端实时输出每条 mid 的处理进度与耗时
  - logs/runs/YYYYMMDD/<run_id>.jsonl（每条处理的结构化审计）
  - logs/runs/YYYYMMDD/<run_id>_summary.json（本次运行汇总）

作者：xuanyu11
版本：v1（2026-09-04）
"""

import os
import sys
import argparse
from datetime import datetime

import yaml

# ── 路径设置 ──────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, PROJECT_DIR)

from src.db_client import MySQLTaskRepository
from src.pipeline import ClassifyPipeline
from src.utils import setup_logger


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(
        description="单任务流水线 v1 - 指定 task_id，只处理该任务中固定数量的待分类 mid",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--task-id", type=int, required=True,
                        help="要处理的 super_mid_task.task_id")
    parser.add_argument("--limit", type=int, default=10,
                        help="最多处理该任务下多少条 level=0 的 mid（默认10）")
    parser.add_argument("--mode", default="auto", choices=["auto", "text", "image", "video"],
                        help="处理模式：auto 自动判断 / text / image / video（默认 auto）")
    parser.add_argument("--write-back", action="store_true",
                        help="回写结果到 HTTP 接口（默认关闭；正式运行请使用 worker.py）")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    args = parser.parse_args()

    if args.task_id <= 0:
        print("task_id 必须为正整数")
        sys.exit(2)
    if args.limit <= 0:
        print("limit 必须为正整数")
        sys.exit(2)

    # ── 初始化 ────────────────────────────────────────────────
    config = load_config(args.config)
    logger = setup_logger(
        "single_task_pipeline",
        log_dir=os.path.join(PROJECT_DIR, config.get("logging", {}).get("dir", "logs")),
        level=config.get("logging", {}).get("level", "INFO"),
        retention_days=int(config.get("logging", {}).get("retention_days", 30)),
        console_enabled=bool(config.get("logging", {}).get("console_enabled", True)),
        file_enabled=bool(config.get("logging", {}).get("file_enabled", True)),
        storage_config=config.get("storage", {}),
    )

    logger.info("=" * 70)
    logger.info("单任务流水线启动")
    logger.info(f"运行时间:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"task_id:    {args.task_id}")
    logger.info(f"mid 上限:   {args.limit}")
    logger.info(f"处理模式:   {args.mode}")
    logger.info(f"结果回写:   {'开启' if args.write_back else '关闭'}")
    logger.info(f"配置文件:   {args.config}")
    logger.info("=" * 70)

    repo = MySQLTaskRepository(config.get("mysql", {}), logger, app_config=config)
    pipeline = ClassifyPipeline(config, logger)

    # ── 查询任务并拉取待处理 mid ──────────────────────────────
    # 只在该短事务内读任务和待处理记录；外部反解/模型/回写绝不能占用此连接。
    with repo.connect() as conn:
        task = repo.fetch_task_by_id(conn, args.task_id)
        records = (
            repo.fetch_pending_mids(
                conn, task, limit=args.limit, only_level_zero=True, for_update=False
            )
            if task is not None else []
        )

    if task is None:
        logger.error(
            f"未找到可处理的 task_id={args.task_id}"
            "（任务不存在，或缺少有效 customer_id 无法路由）"
        )
        pipeline.audit.finalize({"mode": "single_task", "task_id": args.task_id, "reason": "task_not_found"})
        sys.exit(2)

    logger.info("-" * 70)
    logger.info(f"任务信息: id={task.id} task_id={task.task_id} "
                f"customer_id={task.customer_id} shard={task.shard_table}")
    logger.info(f"          task_type={task.task_type} exec_status={task.exec_status} "
                f"industry={task.industry_name or '无'}")
    logger.info("-" * 70)
    logger.info(f"task_id={task.task_id} 下待处理（level=0）记录数: {len(records)}")

    if not records:
        logger.info("该任务下没有待处理记录，直接结束")
        logger.info("=" * 70)
        pipeline.audit.finalize({"mode": "single_task", "task_id": task.task_id, "processed": 0})
        sys.exit(0)

    total_start = datetime.now()
    total_success = 0
    total_fallback = 0
    total_fail = 0
    total_skipped = 0

    # ── 逐条处理 ──────────────────────────────────────────
    for record_idx, record in enumerate(records, 1):
        logger.info("")
        logger.info(f"[{record_idx}/{len(records)}] 开始处理 "
                    f"mid={record.mid} uid={record.mid_uid} "
                    f"forward_mid={record.forward_mid or '无'}")
        try:
            # 调试入口也使用与正式 worker 相同的命名锁，避免联调时重复送模型。
            with repo.acquire_record_lock(record) as acquired:
                if not acquired or not repo.is_pending(record):
                    logger.info("  └─ ⏭ 已被其他实例处理或不再待处理，跳过")
                    total_skipped += 1
                    continue
                result = pipeline.process_one(
                    mid=record.mid,
                    uid=record.mid_uid,
                    mode=args.mode,
                    write_back=args.write_back,
                    record=record,
                )
            if result.fallback_level_written:
                total_fallback += 1
                logger.warning(f"  └─ ⚠️ 失败已兜底回写 level=6 stage={result.error_stage}")
            elif result.success:
                total_success += 1
                logger.info(f"  └─ ✅ 成功  layer={result.layer} "
                            f"media_type={result.media_type} "
                            f"total={result.timings.total_ms:.0f}ms")
            else:
                total_fail += 1
                logger.info(f"  └─ ❌ 失败  stage={result.error_stage or 'unknown'} "
                            f"error={(result.error or '')[:200]}")
        except KeyboardInterrupt:
            logger.warning(
                "收到 Ctrl+C，当前 mid=%s 未完成；保留 level=0，停止单任务处理。",
                record.mid,
            )
            pipeline.audit.finalize({
                "mode": "single_task_interrupted",
                "task_id": task.task_id,
                "processed_success": total_success,
                "processed_fallback": total_fallback,
                "processed_fail": total_fail,
                "skipped": total_skipped,
            })
            sys.exit(130)
        except Exception as exc:
            total_fail += 1
            logger.exception(f"  └─ ❌ 处理异常 mid={record.mid} error={exc}")
            try:
                repo.update_record_failure(None, record, str(exc))
            except Exception as write_exc:
                logger.exception(f"    失败记录再次写入异常 mid={record.mid} error={write_exc}")

    # ── 汇总报告 ──────────────────────────────────────────
    total_elapsed = (datetime.now() - total_start).total_seconds()
    completed_count = total_success + total_fallback
    attempted_count = total_success + total_fallback + total_fail
    success_rate = completed_count / attempted_count * 100 if attempted_count else 100.0

    logger.info("")
    logger.info("=" * 70)
    logger.info("单任务流水线汇总报告")
    logger.info("=" * 70)
    logger.info(f"task_id:      {task.task_id}")
    logger.info(f"分表:         {task.shard_table}")
    logger.info(f"运行时间:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"总耗时:       {total_elapsed:.1f}s")
    logger.info(f"处理记录数:   {len(records)}")
    logger.info(f"成功:         {total_success}")
    logger.info(f"失败兜底(6):  {total_fallback}")
    logger.info(f"失败:         {total_fail}")
    logger.info(f"跳过:         {total_skipped}")
    logger.info(f"成功率:       {success_rate:.1f}%")
    logger.info("=" * 70)

    logger.info("\n输出文件:")
    logger.info("  主日志:     logs/single_task_pipeline.log")
    logger.info(f"  运行审计:   {pipeline.audit.path}")
    logger.info(f"  运行汇总:   {pipeline.audit.summary_path}")

    # 退出码：成功率低于 80% 返回 1
    pipeline.audit.finalize({
        "mode": "single_task",
        "task_id": task.task_id,
        "processed": len(records),
        "success": total_success,
        "fallback": total_fallback,
        "fail": total_fail,
        "skipped": total_skipped,
    })
    sys.exit(0 if success_rate >= 80 else 1)


if __name__ == "__main__":
    main()
