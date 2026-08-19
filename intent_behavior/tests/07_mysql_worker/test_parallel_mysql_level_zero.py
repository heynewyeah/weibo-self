#!/usr/bin/env python3
"""
MySQL 分表 level=0 批量并行分类测试
=====================================
功能：直接读取某张 `nature_ad_super_mid_{shard}` 分表中 `level=0` 的记录，
      使用 ThreadPoolExecutor 并发调用 Qwen 模型分类，并可选把结果回写到
      分表的 `level` / `level_time` 字段。

核心特点：
  - 并发请求：默认 5 并发（图文/视频下载重，建议不超过 10）
  - 数据来源：MySQL 分表 `level=0` 的记录
  - 结果回写：可选 `--write-back`，写入 level / level_time / 错误字段
  - 线程安全：每个工作线程持有独立 MySQL 连接；BlogClassifier 可安全共享
  - 结果落盘：JSON 完整结果 + TSV 汇总 + 摘要文本
  - 稳定性指标：成功率、P50/P95/P99 延迟、吞吐（条/秒）

运行方式：
  # 先预览分表中 level=0 的记录
  python3 tests/07_mysql_worker/test_parallel_mysql_level_zero.py \
      --shard-index 1 --limit 10

  # 并发分类并回写结果（默认 5 并发）
  python3 tests/07_mysql_worker/test_parallel_mysql_level_zero.py \
      --shard-index 1 --run-classify --write-back --limit 100

  # 提高并发（仅文本/图文场景，视频建议 3~5）
  python3 tests/07_mysql_worker/test_parallel_mysql_level_zero.py \
      --shard-index 1 --run-classify --write-back --workers 10 --limit 500

  # 指定 customer_id 过滤 + 视频 frame 模式
  python3 tests/07_mysql_worker/test_parallel_mysql_level_zero.py \
      --shard-index 1 --customer-id 2608812381 \
      --run-classify --write-back --workers 3 --video-mode frame --limit 20

运行时间预估：
  - 100 条纯文本、5 并发：约 30~90 秒
  - 100 条图文、5 并发：约 60~180 秒
  - 20 条视频 cover、3 并发：约 60~120 秒

作者：xuanyu11
创建时间：2026-08-19
"""

import os
import sys
import json
import time
import argparse
import logging
import statistics
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.classifier import BlogClassifier
from src.db_client import MySQLTaskRepository, MidRecord, build_blog_items
from src.models import ClassifyResult
from src.utils import setup_logger


# ── 统计工具 ──────────────────────────────────────────────────
def percentile(values: List[float], p: float) -> float:
    """计算百分位数（线性插值）"""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * p / 100.0
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_values) else f
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])


# ── 单条处理 ──────────────────────────────────────────────────
def process_one(
    record: MidRecord,
    classifier: BlogClassifier,
    mysql_cfg: Dict,
    write_back: bool,
) -> Dict:
    """
    处理单条记录：分类 + 可选回写。

    每个任务独立创建 MySQL 连接，避免连接跨线程使用。
    """
    mid = record.mid
    uid = record.mid_uid

    t0 = time.perf_counter()
    result: ClassifyResult

    try:
        item = record.to_blog_item()
        result = classifier.classify_item(item)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if write_back:
            repo = MySQLTaskRepository(mysql_cfg)
            with repo.connect() as conn:
                try:
                    repo.update_level_result(conn, record, result)
                except Exception as write_exc:
                    # 结果回写失败，再次尝试写入失败字段
                    try:
                        repo.update_record_failure(conn, record, f"结果回写失败: {write_exc}")
                    except Exception:
                        pass
                    result.success = False
                    result.error = f"结果回写失败: {write_exc}"

        return {
            "row_id": record.id,
            "mid": mid,
            "uid": uid,
            "customer_id": record.customer_id,
            "super_task_id": record.super_task_id,
            "content_preview": (record.mid_text or "")[:80],
            "actual_layer": result.layer,
            "success": result.success,
            "error": result.error,
            "elapsed_ms": round(elapsed_ms, 1),
            "media_type": result.media_type,
            "write_back": write_back,
        }

    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        error_msg = f"异常: {str(e)}"

        if write_back:
            try:
                repo = MySQLTaskRepository(mysql_cfg)
                with repo.connect() as conn:
                    repo.update_record_failure(conn, record, error_msg)
            except Exception:
                pass

        return {
            "row_id": record.id,
            "mid": mid,
            "uid": uid,
            "customer_id": record.customer_id,
            "super_task_id": record.super_task_id,
            "content_preview": (record.mid_text or "")[:80],
            "actual_layer": "未识别",
            "success": False,
            "error": error_msg,
            "elapsed_ms": round(elapsed_ms, 1),
            "media_type": "unknown",
            "write_back": write_back,
        }


# ── 摘要 ──────────────────────────────────────────────────────
def print_summary(
    results: List[Dict],
    total_elapsed_s: float,
    table_name: str,
    workers: int,
    write_back: bool,
    logger: logging.Logger,
) -> Tuple[List[str], Dict]:
    """打印并返回摘要信息"""
    total = len(results)
    success_count = sum(1 for r in results if r["success"])
    fail_count = total - success_count
    success_rate = success_count / total * 100 if total > 0 else 0

    latencies = [r["elapsed_ms"] for r in results if r["elapsed_ms"] > 0]
    avg_latency = statistics.mean(latencies) if latencies else 0
    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    p99 = percentile(latencies, 99)

    throughput = total / total_elapsed_s if total_elapsed_s > 0 else 0

    layer_dist: Dict[str, int] = {}
    media_type_dist: Dict[str, int] = {}
    for r in results:
        layer_dist[r["actual_layer"]] = layer_dist.get(r["actual_layer"], 0) + 1
        media_type_dist[r["media_type"]] = media_type_dist.get(r["media_type"], 0) + 1

    summary_lines = [
        "=" * 60,
        "MySQL 分表 level=0 并行分类结果摘要",
        "=" * 60,
        f"运行时间:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"数据来源:     {table_name} (level=0)",
        f"并发数:       {workers}",
        f"回写数据库:   {'是' if write_back else '否'}",
        f"总条数:       {total}",
        f"成功:         {success_count}  ({success_rate:.1f}%)",
        f"失败:         {fail_count}  ({100 - success_rate:.1f}%)",
        f"总耗时:       {total_elapsed_s:.1f}s",
        f"吞吐:         {throughput:.1f} 条/秒",
        f"平均延迟:     {avg_latency:.0f}ms",
        f"P50 延迟:     {p50:.0f}ms",
        f"P95 延迟:     {p95:.0f}ms",
        f"P99 延迟:     {p99:.0f}ms",
        "",
        "媒体类型分布:",
    ]
    for mt, cnt in sorted(media_type_dist.items(), key=lambda x: -x[1]):
        pct = cnt / total * 100
        summary_lines.append(f"  {mt}: {cnt} 条 ({pct:.1f}%)")

    summary_lines.append("")
    summary_lines.append("分类结果分布:")
    for layer, cnt in sorted(layer_dist.items(), key=lambda x: -x[1]):
        pct = cnt / total * 100
        summary_lines.append(f"  {layer}: {cnt} 条 ({pct:.1f}%)")

    summary_lines.append("=" * 60)

    for line in summary_lines:
        logger.info(line)

    return summary_lines, {
        "total": total,
        "success": success_count,
        "fail": fail_count,
        "success_rate": round(success_rate / 100, 4),
        "total_elapsed_s": round(total_elapsed_s, 1),
        "throughput": round(throughput, 1),
        "avg_latency_ms": round(avg_latency, 1),
        "p50_latency_ms": round(p50, 1),
        "p95_latency_ms": round(p95, 1),
        "p99_latency_ms": round(p99, 1),
        "layer_distribution": layer_dist,
        "media_type_distribution": media_type_dist,
    }


# ── 主流程 ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="MySQL 分表 level=0 批量并行分类测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--shard-index", type=int, default=1,
                        help="分表索引，如 1 表示 nature_ad_super_mid_1")
    parser.add_argument("--customer-id", type=int, default=None,
                        help="可选：按 customer_id 过滤")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制处理条数（0=不限制）")
    parser.add_argument("--workers", type=int, default=5,
                        help="并发数（默认 5；文本/图文可 8~10，视频建议 3~5）")
    parser.add_argument("--run-classify", action="store_true",
                        help="执行分类（默认只拉取预览）")
    parser.add_argument("--write-back", action="store_true",
                        help="将结果回写到分表 level / level_time 字段（需配合 --run-classify）")
    parser.add_argument("--video-mode", default="", choices=["", "cover", "frame"],
                        help="视频处理模式：cover（封面图）或 frame（OpenCV抽帧），空则使用配置")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    parser.add_argument("--timeout", type=int, default=0,
                        help="单条请求超时（秒，0=使用配置文件）")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_parallel_mysql_level_zero", log_dir=os.path.join(PROJECT_DIR, "logs"))

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("=" * 60)
    logger.info("MySQL 分表 level=0 批量并行分类测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")
    logger.info(f"分表索引: {args.shard_index}")
    logger.info(f"customer_id 过滤: {args.customer_id if args.customer_id else '无'}")
    logger.info(f"并发数: {args.workers}")
    logger.info(f"执行分类: {args.run_classify}")
    logger.info(f"回写数据库: {args.write_back}")

    # ── 加载配置 ──────────────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.timeout > 0:
        config["api"]["timeout"] = args.timeout
    if args.video_mode:
        config["media"]["video"]["video_mode"] = args.video_mode

    mysql_cfg = config.get("mysql", {})
    table_name = f"{mysql_cfg.get('shard_table_prefix', 'nature_ad_super_mid_')}{args.shard_index}"

    logger.info(f"目标分表: {table_name}")
    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"thinking 模式: {config['api'].get('enable_thinking', False)}")
    logger.info(f"视频处理模式: {config['media']['video'].get('video_mode', 'cover')}")

    if config["media"]["video"].get("video_mode") == "frame":
        try:
            import cv2  # noqa: F401
        except ImportError:
            logger.error("frame 模式需要 opencv-python-headless，请先安装")
            sys.exit(1)

    # ── 拉取待处理记录 ────────────────────────────────────────
    repo = MySQLTaskRepository(mysql_cfg, logger)
    with repo.connect() as conn:
        records = repo.fetch_pending_mids_by_table(
            conn,
            table_name=table_name,
            customer_id=args.customer_id,
            limit=args.limit if args.limit > 0 else 10000,
            only_level_zero=True,
        )

    total = len(records)
    logger.info(f"拉取到 {total} 条 level=0 记录")
    logger.info("=" * 60)

    if total == 0:
        logger.warning("没有待处理记录，退出")
        sys.exit(0)

    if not args.run_classify:
        logger.info("仅预览模式，前 5 条示例：")
        for r in records[:5]:
            logger.info(f"  row_id={r.id} mid={r.mid} uid={r.mid_uid} customer_id={r.customer_id}")
        logger.info("如需分类，请加 --run-classify；如需回写，再加 --write-back")
        sys.exit(0)

    # ── 并行分类 ──────────────────────────────────────────────
    classifier = BlogClassifier(config, logger)
    results: List[Dict] = []
    start_time = time.perf_counter()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_record = {
            executor.submit(process_one, record, classifier, mysql_cfg, args.write_back): record
            for record in records
        }

        completed = 0
        for future in as_completed(future_to_record):
            record = future_to_record[future]
            try:
                res = future.result()
            except Exception as e:
                res = {
                    "row_id": record.id,
                    "mid": record.mid,
                    "uid": record.mid_uid,
                    "customer_id": record.customer_id,
                    "super_task_id": record.super_task_id,
                    "content_preview": (record.mid_text or "")[:80],
                    "actual_layer": "未识别",
                    "success": False,
                    "error": f"Future 异常: {str(e)}",
                    "elapsed_ms": 0.0,
                    "media_type": "unknown",
                    "write_back": args.write_back,
                }
            results.append(res)
            completed += 1

            if completed % max(1, total // 10) == 0 or completed == total or completed % 10 == 0:
                success_so_far = sum(1 for r in results if r["success"])
                fail_so_far = completed - success_so_far
                elapsed_so_far = time.perf_counter() - start_time
                eta = (elapsed_so_far / completed) * (total - completed) if completed > 0 else 0
                print(f"\r  进度: {completed}/{total}  ✅{success_so_far} ❌{fail_so_far}  "
                      f"已用:{elapsed_so_far:.0f}s  预计剩余:{eta:.0f}s", end="", flush=True)

    print()  # 换行
    total_elapsed_s = time.perf_counter() - start_time

    # 按 row_id 排序
    results.sort(key=lambda x: x["row_id"])

    # ── 统计与摘要 ────────────────────────────────────────────
    summary_lines, summary_dict = print_summary(
        results, total_elapsed_s, table_name, args.workers, args.write_back, logger
    )

    # ── 保存结果 ──────────────────────────────────────────────
    output_json = os.path.join(OUTPUT_DIR, f"parallel_mysql_level_zero_{run_ts}.json")
    output_tsv = os.path.join(OUTPUT_DIR, f"parallel_mysql_level_zero_{run_ts}.tsv")
    output_summary = os.path.join(OUTPUT_DIR, f"parallel_mysql_level_zero_{run_ts}_summary.txt")

    output_data = {
        "test_type": "parallel_mysql_level_zero",
        "run_time": datetime.now().isoformat(),
        "data_source": table_name,
        "config": {
            "model": config["api"]["model"],
            "enable_thinking": config["api"].get("enable_thinking", False),
            "temperature": config["api"].get("temperature", 0.0),
            "workers": args.workers,
            "timeout": config["api"].get("timeout", 60),
            "video_mode": config["media"]["video"].get("video_mode", "cover"),
            "write_back": args.write_back,
        },
        "summary": summary_dict,
        "results": results,
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    with open(output_tsv, "w", encoding="utf-8") as f:
        f.write("row_id\tmid\tuid\tcustomer_id\tactual_layer\tsuccess\terror\telapsed_ms\tmedia_type\n")
        for r in results:
            f.write(
                f"{r['row_id']}\t{r['mid']}\t{r['uid']}\t{r['customer_id']}\t"
                f"{r['actual_layer']}\t{r['success']}\t{r['error'] or ''}\t"
                f"{r['elapsed_ms']}\t{r['media_type']}\n"
            )

    with open(output_summary, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines) + "\n")

    logger.info("=" * 60)
    logger.info("结果文件:")
    logger.info(f"  JSON:    {output_json}")
    logger.info(f"  TSV:     {output_tsv}")
    logger.info(f"  摘要:    {output_summary}")
    logger.info("=" * 60)

    if summary_dict["success_rate"] < 0.8:
        logger.warning(f"成功率 {summary_dict['success_rate']*100:.1f}% 低于 80%，退出码 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
