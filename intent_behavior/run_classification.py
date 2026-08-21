#!/usr/bin/env python3
"""
正式分类运行入口
================
生产环境主脚本，支持四种输入源：
  1. 单条：--mid + --uid
  2. 批量文件：--input-file（JSONL/TSV）
  3. MySQL 任务驱动：--from-tasks + --limit（查询 super_mid_task → 路由到 nature_ad_super_mid_x）
  4. MySQL 分表直读：--shard-index + --customer-id + --limit

支持三种处理模式：
  - text / image / video：强制按指定类型处理
  - auto：根据 mid 反解结果自动判断（video > image > text）

核心流程与 tests/08_mid_resolver 保持一致：
  mid 反解 → 构造 BlogItem → 分类 → 清理临时文件 → 结果输出/回写

运行示例：
  # 单条自动模式
  python3 run_classification.py --mid 5239345868702306 --uid 7008866503

  # 单条强制图片模式
  python3 run_classification.py --mid 5239345868702306 --uid 7008866503 --mode image

  # 批量文件（JSONL）
  python3 run_classification.py --input-file data/input.jsonl --workers 5

  # MySQL 任务驱动：自动查询 super_mid_task 有效任务，路由到分表，最多处理 100 条并回写
  python3 run_classification.py \
      --from-tasks --limit 100 \
      --mode auto --write-back

  # MySQL 分表直读：直接读取 nature_ad_super_mid_1 的 level=0 数据
  python3 run_classification.py \
      --shard-index 1 --customer-id 2608812381 --limit 100 \
      --mode auto --write-back

输出：
  - 终端：每条 mid 的处理结果和耗时
  - logs/YYYYMMDD_error.log：失败记录
  - output/run_classification_<timestamp>.json：完整结果
  - output/run_classification_<timestamp>_summary.txt：摘要
"""

import os
import sys
import json
import time
import argparse
import logging
import statistics
from datetime import datetime
from typing import Any, Dict, List, Tuple

# ── 路径设置 ──────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.pipeline import ClassifyPipeline, ProcessResult
from src.db_client import MySQLTaskRepository
from src.utils import setup_logger


# ── 数据加载 ──────────────────────────────────────────────────
def load_jsonl(filepath: str) -> List[Dict[str, Any]]:
    """加载 JSONL 文件，每行必须包含 mid"""
    items = []
    with open(filepath, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "mid" not in obj:
                    print(f"  ⚠️  第 {lineno} 行缺少 mid，跳过")
                    continue
                items.append(obj)
            except json.JSONDecodeError as e:
                print(f"  ⚠️  第 {lineno} 行 JSON 解析失败: {e}")
    return items


def load_tsv(filepath: str) -> List[Dict[str, Any]]:
    """加载 TSV 文件，支持 mid \t uid 格式"""
    items = []
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    has_header = lines and lines[0].strip().startswith("mid")
    data_lines = lines[1:] if has_header else lines

    for line in data_lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        item = {"mid": parts[0]}
        if len(parts) > 1:
            item["uid"] = parts[1]
        items.append(item)
    return items


def load_input_file(filepath: str) -> List[Dict[str, Any]]:
    """根据后缀加载文件"""
    if filepath.endswith(".jsonl") or filepath.endswith(".json"):
        return load_jsonl(filepath)
    return load_tsv(filepath)


def load_from_mysql(
    repo: MySQLTaskRepository,
    table_name: str,
    customer_id: int = None,
    limit: int = 0,
    logger: logging.Logger = None,
) -> List[Dict[str, Any]]:
    """从 MySQL 分表读取 level=0 记录"""
    inputs = []
    with repo.connect() as conn:
        records = repo.fetch_pending_mids_by_table(
            conn,
            table_name=table_name,
            customer_id=customer_id,
            limit=limit if limit > 0 else 10000,
            only_level_zero=True,
        )
    for record in records:
        inputs.append({
            "mid": record.mid,
            "uid": record.mid_uid,
            "record": record,
        })
    if logger:
        logger.info(f"从 {table_name} 读取到 {len(inputs)} 条 level=0 记录")
    return inputs


def load_from_tasks(
    repo: MySQLTaskRepository,
    config: Dict[str, Any],
    total_limit: int = 0,
    per_task_limit: int = 0,
    logger: logging.Logger = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    从 super_mid_task 查询有效任务，按 customer_id 路由到分表读取 level=0 记录。

    Returns:
        (inputs, data_source_description)
    """
    worker_cfg = config.get("worker", {})
    active_task_limit = int(worker_cfg.get("active_task_limit", 50))
    if per_task_limit <= 0:
        per_task_limit = int(worker_cfg.get("fetch_limit_per_task", 100))

    inputs: List[Dict[str, Any]] = []
    task_summaries = []

    with repo.connect() as conn:
        tasks = repo.fetch_active_tasks(conn, limit=active_task_limit)
        if logger:
            logger.info(f"从 super_mid_task 查询到 {len(tasks)} 个有效任务")

        for task in tasks:
            if total_limit > 0 and len(inputs) >= total_limit:
                break

            remaining = total_limit - len(inputs) if total_limit > 0 else per_task_limit
            fetch_limit = min(per_task_limit, remaining) if total_limit > 0 else per_task_limit

            records = repo.fetch_pending_mids(conn, task, limit=fetch_limit, only_level_zero=True)
            for record in records:
                inputs.append({
                    "mid": record.mid,
                    "uid": record.mid_uid,
                    "record": record,
                })
                if total_limit > 0 and len(inputs) >= total_limit:
                    break

            task_summaries.append(f"{task.shard_table}(task_id={task.task_id}): {len(records)} 条")
            if logger:
                logger.info(
                    f"任务 task_id={task.task_id} customer_id={task.customer_id} "
                    f"shard={task.shard_table} 读取 {len(records)} 条"
                )

    data_source = "mysql:tasks -> " + "; ".join(task_summaries) if task_summaries else "mysql:tasks"
    if logger:
        logger.info(f"任务驱动模式共读取 {len(inputs)} 条 level=0 记录")
    return inputs, data_source


# ── 摘要 ──────────────────────────────────────────────────────
def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * p / 100.0
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_values) else f
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])


def print_summary(
    results: List[ProcessResult],
    total_elapsed_s: float,
    mode: str,
    data_source: str,
    logger: logging.Logger,
) -> Tuple[List[str], Dict[str, Any]]:
    total = len(results)
    success_count = sum(1 for r in results if r.success)
    fail_count = total - success_count
    success_rate = success_count / total * 100 if total > 0 else 0

    total_ms_list = [r.timings.total_ms for r in results]
    resolve_ms_list = [r.timings.resolve_ms for r in results]
    classify_ms_list = [r.timings.classify_ms for r in results]

    def _avg(values: List[float]) -> float:
        return statistics.mean(values) if values else 0.0

    layer_dist: Dict[str, int] = {}
    media_type_dist: Dict[str, int] = {}
    error_stage_dist: Dict[str, int] = {}
    for r in results:
        layer_dist[r.layer] = layer_dist.get(r.layer, 0) + 1
        media_type_dist[r.media_type] = media_type_dist.get(r.media_type, 0) + 1
        if not r.success and r.error_stage:
            error_stage_dist[r.error_stage] = error_stage_dist.get(r.error_stage, 0) + 1

    summary_lines = [
        "=" * 70,
        "正式分类运行摘要",
        "=" * 70,
        f"运行时间:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"数据来源:     {data_source}",
        f"处理模式:     {mode}",
        f"总条数:       {total}",
        f"成功:         {success_count}  ({success_rate:.1f}%)",
        f"失败:         {fail_count}  ({100 - success_rate:.1f}%)",
        f"总耗时:       {total_elapsed_s:.1f}s",
        f"平均总耗时:   {_avg(total_ms_list):.0f}ms",
        f"平均反解耗时: {_avg(resolve_ms_list):.0f}ms",
        f"平均分类耗时: {_avg(classify_ms_list):.0f}ms",
        f"P95 总耗时:   {percentile(total_ms_list, 95):.0f}ms",
        "",
        "媒体类型分布:",
    ]
    for mt, cnt in sorted(media_type_dist.items(), key=lambda x: -x[1]):
        summary_lines.append(f"  {mt}: {cnt} 条")

    summary_lines.append("")
    summary_lines.append("分类层级分布:")
    for layer, cnt in sorted(layer_dist.items(), key=lambda x: -x[1]):
        summary_lines.append(f"  {layer}: {cnt} 条")

    if error_stage_dist:
        summary_lines.append("")
        summary_lines.append("失败阶段分布:")
        for stage, cnt in sorted(error_stage_dist.items(), key=lambda x: -x[1]):
            summary_lines.append(f"  {stage}: {cnt} 条")

    summary_lines.append("=" * 70)

    for line in summary_lines:
        logger.info(line)

    return summary_lines, {
        "total": total,
        "success": success_count,
        "fail": fail_count,
        "success_rate": round(success_rate / 100, 4),
        "total_elapsed_s": round(total_elapsed_s, 1),
        "avg_total_ms": round(_avg(total_ms_list), 1),
        "avg_resolve_ms": round(_avg(resolve_ms_list), 1),
        "avg_classify_ms": round(_avg(classify_ms_list), 1),
        "p95_total_ms": round(percentile(total_ms_list, 95), 1),
        "layer_distribution": layer_dist,
        "media_type_distribution": media_type_dist,
        "error_stage_distribution": error_stage_dist,
    }


# ── 主流程 ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="正式分类运行入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # 输入源
    parser.add_argument("--mid", default="", help="单条处理：博文 mid")
    parser.add_argument("--uid", default="", help="单条处理：博文作者 uid")
    parser.add_argument("--input-file", default="",
                        help="批量处理：本地 JSONL/TSV 文件路径")
    parser.add_argument("--from-tasks", action="store_true",
                        help="MySQL 输入：从 super_mid_task 查询任务并路由到 nature_ad_super_mid_x")
    parser.add_argument("--shard-index", type=int, default=0,
                        help="MySQL 输入：直接读取指定分表索引，如 1 表示 nature_ad_super_mid_1")
    parser.add_argument("--customer-id", type=int, default=None,
                        help="MySQL 输入：按 customer_id 过滤（仅 --shard-index 模式有效）")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制总处理条数（0=不限制）")
    parser.add_argument("--limit-per-task", type=int, default=0,
                        help="任务驱动模式下每个任务最多读取条数（0=使用 worker.fetch_limit_per_task）")

    # 处理模式
    parser.add_argument("--mode", default="auto", choices=["auto", "text", "image", "video"],
                        help="处理模式：auto 自动判断 / text / image / video")

    # 输出与回写
    parser.add_argument("--write-back", action="store_true",
                        help="将结果通过 HTTP 接口回写（仅 MySQL 输入且 workers=1 时有效）")
    parser.add_argument("--workers", type=int, default=1,
                        help="并发数（默认 1；>1 时不支持结果回写）")

    # 配置
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    parser.add_argument("--video-mode", default="", choices=["", "cover", "frame"],
                        help="视频处理模式：cover / frame，空则使用配置")
    parser.add_argument("--timeout", type=int, default=0,
                        help="模型 API 单条超时（秒，0=使用配置）")

    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("run_classification", log_dir=os.path.join(PROJECT_DIR, "logs"))

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("=" * 70)
    logger.info("正式分类运行入口启动")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")
    logger.info(f"处理模式: {args.mode}")
    logger.info(f"并发数: {args.workers}")

    # ── 加载配置 ──────────────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.timeout > 0:
        config["api"]["timeout"] = args.timeout
    if args.video_mode:
        config["media"]["video"]["video_mode"] = args.video_mode

    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"视频模式: {config['media']['video'].get('video_mode', 'cover')}")

    if config["media"]["video"].get("video_mode") == "frame":
        try:
            import cv2  # noqa: F401
        except ImportError:
            logger.error("frame 模式需要 opencv-python-headless，请先安装")
            sys.exit(1)

    # ── 构建输入列表 ──────────────────────────────────────────
    inputs: List[Dict[str, Any]] = []
    data_source = "unknown"

    if args.mid:
        inputs.append({"mid": args.mid, "uid": args.uid})
        data_source = f"single_mid:{args.mid}"
    elif args.input_file:
        if not os.path.exists(args.input_file):
            logger.error(f"输入文件不存在: {args.input_file}")
            sys.exit(1)
        inputs = load_input_file(args.input_file)
        data_source = f"file:{args.input_file}"
        logger.info(f"从文件加载 {len(inputs)} 条记录")
    elif args.from_tasks:
        mysql_cfg = config.get("mysql")
        if not mysql_cfg:
            logger.error("配置中缺少 mysql 段")
            sys.exit(1)
        # 必须传入 app_config，否则 result_writer 不会初始化
        repo = MySQLTaskRepository(mysql_cfg, logger, app_config=config)
        inputs, data_source = load_from_tasks(
            repo,
            config,
            total_limit=args.limit,
            per_task_limit=args.limit_per_task,
            logger=logger,
        )
    elif args.shard_index > 0:
        mysql_cfg = config.get("mysql")
        if not mysql_cfg:
            logger.error("配置中缺少 mysql 段")
            sys.exit(1)
        repo = MySQLTaskRepository(mysql_cfg, logger, app_config=config)
        table_name = f"{mysql_cfg.get('shard_table_prefix', 'nature_ad_super_mid_')}{args.shard_index}"
        inputs = load_from_mysql(
            repo, table_name,
            customer_id=args.customer_id,
            limit=args.limit,
            logger=logger,
        )
        data_source = f"mysql:{table_name}"
    else:
        logger.error("必须指定输入源：--mid / --input-file / --from-tasks / --shard-index")
        parser.print_help()
        sys.exit(1)

    if args.limit and args.limit > 0:
        inputs = inputs[:args.limit]
        logger.info(f"--limit {args.limit}，截取前 {len(inputs)} 条")

    total = len(inputs)
    if total == 0:
        logger.warning("没有待处理数据")
        sys.exit(0)

    logger.info(f"数据来源: {data_source}")
    logger.info(f"待处理条数: {total}")
    logger.info("=" * 70)

    # ── 执行分类 ──────────────────────────────────────────────
    pipeline = ClassifyPipeline(config, logger)
    t_start = time.perf_counter()
    results = pipeline.process_batch(
        inputs=inputs,
        mode=args.mode,
        write_back=args.write_back,
        workers=args.workers,
    )
    t_elapsed = time.perf_counter() - t_start

    # ── 摘要 ──────────────────────────────────────────────────
    summary_lines, summary_dict = print_summary(
        results, t_elapsed, args.mode, data_source, logger
    )

    # ── 保存结果 ──────────────────────────────────────────────
    output_json = os.path.join(OUTPUT_DIR, f"run_classification_{run_ts}.json")
    output_summary = os.path.join(OUTPUT_DIR, f"run_classification_{run_ts}_summary.txt")

    output_data = {
        "run_time": datetime.now().isoformat(),
        "data_source": data_source,
        "mode": args.mode,
        "config": {
            "model": config["api"]["model"],
            "video_mode": config["media"]["video"].get("video_mode", "cover"),
            "workers": args.workers,
            "write_back": args.write_back,
        },
        "summary": summary_dict,
        "results": [r.to_dict() for r in results],
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    with open(output_summary, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines) + "\n")

    logger.info("=" * 70)
    logger.info("结果文件:")
    logger.info(f"  JSON:    {output_json}")
    logger.info(f"  摘要:    {output_summary}")
    logger.info(f"  错误日志: logs/{datetime.now().strftime('%Y%m%d')}_error.log")
    logger.info("=" * 70)

    if summary_dict["success_rate"] < 0.8:
        logger.warning(f"成功率 {summary_dict['success_rate']*100:.1f}% 低于 80%，退出码 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
