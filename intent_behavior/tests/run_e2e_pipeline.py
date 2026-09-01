#!/usr/bin/env python3
"""
端到端流水线 - 持续处理 MySQL 中的待分类任务
===========================================
功能：
  1. 持续轮询 MySQL 中的 super_mid_task 表，查找待处理任务
  2. 对每个任务，处理其关联的所有 level=0 的记录
  3. 处理完成后自动回写结果到 HTTP 接口
  4. 当没有更多待处理任务时自动退出

运行方式：
  # 持续处理直到没有待处理任务
  python3 tests/run_e2e_pipeline.py

  # 限制最大处理轮数（用于测试）
  python3 tests/run_e2e_pipeline.py --max-rounds 3

  # 限制每轮处理的任务数
  python3 tests/run_e2e_pipeline.py --max-tasks-per-round 5

  # 自定义轮询间隔（秒）
  python3 tests/run_e2e_pipeline.py --poll-interval 30

  # 组合使用
  python3 tests/run_e2e_pipeline.py --max-rounds 5 --max-tasks-per-round 10 --poll-interval 20

输出：
  - 终端实时输出处理进度
  - logs/classify.log（轮转日志）
  - logs/反解失败汇总.txt（反解失败记录）
  - output/result.tsv（分类结果）

作者：xuanyu11
创建时间：2026-09-01
"""

import os
import sys
import argparse
import time
from datetime import datetime

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))

sys.path.insert(0, PROJECT_DIR)

from src.worker import MySQLShardWorker
from src.utils import setup_logger


def main():
    parser = argparse.ArgumentParser(
        description="端到端流水线 - 持续处理 MySQL 中的待分类任务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    parser.add_argument("--max-rounds", type=int, default=0,
                        help="最大处理轮数（0=无限，直到没有待处理任务）")
    parser.add_argument("--max-tasks-per-round", type=int, default=50,
                        help="每轮最多处理的任务数（默认50）")
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="轮询间隔秒数（默认10）")
    parser.add_argument("--batch-limit", type=int, default=100,
                        help="每个任务每轮最多处理的记录数（默认100）")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    logger = setup_logger("e2e_pipeline", log_dir=os.path.join(PROJECT_DIR, "logs"))
    
    logger.info("=" * 70)
    logger.info("端到端流水线启动")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")
    logger.info(f"最大轮数: {args.max_rounds if args.max_rounds > 0 else '无限'}")
    logger.info(f"每轮最大任务数: {args.max_tasks_per_round}")
    logger.info(f"轮询间隔: {args.poll_interval}s")
    logger.info(f"每任务每轮最大记录数: {args.batch_limit}")
    logger.info("=" * 70)

    # ── 创建 worker ───────────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 覆盖配置
    config["worker"]["active_task_limit"] = args.max_tasks_per_round
    config["worker"]["fetch_limit_per_task"] = args.batch_limit
    config["worker"]["poll_interval_sec"] = args.poll_interval
    config["worker"]["max_loops"] = args.max_rounds

    worker = MySQLShardWorker(config, logger)

    # ── 运行流水线 ────────────────────────────────────────────
    total_start = datetime.now()
    total_tasks = 0
    total_records = 0
    total_success = 0
    total_fail = 0
    round_count = 0
    empty_rounds = 0

    try:
        while True:
            round_count += 1
            logger.info(f"\n{'='*70}")
            logger.info(f"第 {round_count} 轮处理开始 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"{'='*70}")

            # 运行一轮
            summary = worker.run_once()

            tasks_processed = summary["tasks"]
            records_processed = summary["pending"]
            success_count = summary["success"]
            fail_count = summary["fail"]

            total_tasks += tasks_processed
            total_records += records_processed
            total_success += success_count
            total_fail += fail_count

            logger.info(f"\n第 {round_count} 轮处理完成:")
            logger.info(f"  处理任务数: {tasks_processed}")
            logger.info(f"  处理记录数: {records_processed}")
            logger.info(f"  成功: {success_count}")
            logger.info(f"  失败: {fail_count}")

            # 检查是否应该退出
            if tasks_processed == 0:
                empty_rounds += 1
                logger.info(f"本轮无待处理任务（连续 {empty_rounds} 轮）")
                
                if empty_rounds >= 2:
                    logger.info("连续 2 轮无待处理任务，流水线结束")
                    break
            else:
                empty_rounds = 0

            # 检查最大轮数限制
            if args.max_rounds > 0 and round_count >= args.max_rounds:
                logger.info(f"已达到最大轮数限制 ({args.max_rounds})，流水线结束")
                break

            # 等待下一轮
            logger.info(f"等待 {args.poll_interval}s 后开始下一轮...")
            time.sleep(args.poll_interval)

    except KeyboardInterrupt:
        logger.info("\n收到中断信号，流水线停止")

    # ── 汇总报告 ──────────────────────────────────────────────
    total_elapsed = (datetime.now() - total_start).total_seconds()
    success_rate = (total_success / total_records * 100) if total_records > 0 else 0

    logger.info(f"\n{'='*70}")
    logger.info("端到端流水线汇总报告")
    logger.info(f"{'='*70}")
    logger.info(f"运行时间:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"总耗时:       {total_elapsed:.1f}s")
    logger.info(f"处理轮数:     {round_count}")
    logger.info(f"处理任务总数: {total_tasks}")
    logger.info(f"处理记录总数: {total_records}")
    logger.info(f"成功:         {total_success}")
    logger.info(f"失败:         {total_fail}")
    logger.info(f"成功率:       {success_rate:.1f}%")
    logger.info(f"{'='*70}")

    logger.info(f"\n输出文件:")
    logger.info(f"  日志:       logs/classify.log")
    logger.info(f"  反解失败:   logs/反解失败汇总.txt")
    logger.info(f"  分类结果:   output/result.tsv")
    logger.info(f"  错误记录:   logs/error_records.tsv")

    # 退出码：成功率低于 80% 返回 1
    sys.exit(0 if success_rate >= 80 else 1)


if __name__ == "__main__":
    main()
