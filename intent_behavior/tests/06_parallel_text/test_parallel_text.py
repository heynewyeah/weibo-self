#!/usr/bin/env python3
"""
并行批量文本分类测试
====================
功能：从 HDFS/本地批量读取纯文本博文，使用 ThreadPoolExecutor 并发调用
      Qwen 模型进行文本分类，验证并发稳定性和吞吐性能。

核心特点：
  - 并发请求：默认 10 并发（与平台推荐一致）
  - 数据来源：优先 HDFS，支持本地 JSONL/TSV fallback
  - 结果落盘：JSON 完整结果 + TSV 汇总 + 摘要文本
  - 稳定性指标：成功率、P50/P95/P99 延迟、吞吐（条/秒）

数据来源（按优先级）：
  1. --input-hdfs 指定的 HDFS 目录/文件
  2. --input 指定的本地 JSONL/TSV 文件
  3. tests/01_prepare_data/fixtures/text_samples.jsonl
  4. 内置 fallback 样本

输出路径：
  tests/06_parallel_text/output/parallel_text_<timestamp>.json    — 完整结果
  tests/06_parallel_text/output/parallel_text_<timestamp>.tsv     — TSV 汇总
  tests/06_parallel_text/output/parallel_text_<timestamp>_summary.txt — 摘要

运行方式：
  # 从 HDFS 读取文本博文并并发分类（推荐）
  python3 tests/06_parallel_text/test_parallel_text.py \
      --input-hdfs /dw_ext/ad/person/xuanyu11/intent_behavior/data/text_weibo_ad_20260701_20260701 \
      --workers 10 --limit 100

  # 本地 JSONL 测试
  python3 tests/06_parallel_text/test_parallel_text.py \
      --input tests/01_prepare_data/fixtures/text_samples.jsonl \
      --workers 10 --limit 50

  # 显示模型原始输出
  python3 tests/06_parallel_text/test_parallel_text.py --verbose

运行时间预估：
  - 100 条纯文本、10 并发：约 20~60 秒（取决于模型响应速度）
  - 相比串行（sleep 0.3s）可节约 60%~80% 时间

作者：xuanyu11
创建时间：2026-08-17
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
from typing import List, Dict, Optional, Tuple

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
FIXTURES_DIR = os.path.join(PROJECT_DIR, "tests/01_prepare_data/fixtures")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.classifier import BlogClassifier
from src.models import BlogItem
from src.utils import setup_logger


# ── 内置 fallback 样本 ────────────────────────────────────────
FALLBACK_SAMPLES = [
    {
        "mid": "5250218712893321",
        "uid": "1647951825",
        "content": "#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里，第五代智能电混双擎加持，WLTC综合油耗低至4.59L/100km，通勤一月一加油、自驾跨省不补能。TSS 4.0智驾+15.6英寸大屏，新车已经到店了，家用还是挺好的",
        "expected_layer": "考虑层",
    },
    {
        "mid": "5250218712893322",
        "uid": "1647951826",
        "content": "今天带大家体验东风日产N6的零压云毯大沙发，坐进去整个人都放松了，这个座椅真的绝了！内饰质感也很在线，感兴趣的朋友可以去店里体验一下",
        "expected_layer": "兴趣层",
    },
    {
        "mid": "5250218712893323",
        "uid": "1647951827",
        "content": "比亚迪全新品牌形象发布！「在路上」——这不只是一句口号，更是比亚迪对每一位用户的承诺。新的征程，新的出发。#比亚迪# #新能源汽车#",
        "expected_layer": "认知层",
    },
]


# ── 数据加载 ──────────────────────────────────────────────────
def load_jsonl(filepath: str) -> List[Dict]:
    """加载 JSONL 文件"""
    items = []
    with open(filepath, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  ⚠️  第 {lineno} 行 JSON 解析失败: {e}")
    return items


def load_tsv(filepath: str) -> List[Dict]:
    """加载 TSV 文件（兼容 HDFS 导出格式）"""
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
        items.append({
            "mid": parts[0] if len(parts) > 0 else "",
            "uid": parts[1] if len(parts) > 1 else "",
            "content": parts[2] if len(parts) > 2 else "",
        })
    return items


def load_local_data(input_path: str) -> Tuple[List[Dict], str]:
    """加载本地数据文件"""
    if input_path.endswith(".jsonl") or input_path.endswith(".json"):
        return load_jsonl(input_path), input_path
    else:
        return load_tsv(input_path), input_path


def load_hdfs_data(hdfs_path: str, logger: logging.Logger) -> Tuple[List[Dict], str]:
    """通过 HDFSExtractor 读取 HDFS 上的 TSV 数据，并转换为 Dict 列表"""
    from src.data_extractor import HDFSExtractor

    # 构造临时 extractor 配置
    extractor_cfg = {
        "hdfs": {
            "file_path": hdfs_path,
            "has_header": False,
            "field_mapping": {"mid": 0, "content": 1, "dt": 2},
        }
    }

    extractor = HDFSExtractor(extractor_cfg, logger)
    blog_items = extractor.extract()

    # 转为 Dict 列表，保持与本地数据一致
    items = []
    for bi in blog_items:
        items.append({
            "mid": bi.mid,
            "uid": bi.uid,
            "content": bi.content or "",
            "dt": bi.dt,
        })
    return items, hdfs_path


def load_data(args, logger: logging.Logger) -> Tuple[List[Dict], str]:
    """按优先级加载测试数据"""
    # 1. HDFS
    if args.input_hdfs:
        logger.info(f"从 HDFS 加载数据: {args.input_hdfs}")
        items, source = load_hdfs_data(args.input_hdfs, logger)
        return items, source

    # 2. 本地文件
    if args.input:
        if not os.path.exists(args.input):
            logger.error(f"输入文件不存在: {args.input}")
            sys.exit(1)
        logger.info(f"从本地文件加载数据: {args.input}")
        items, source = load_local_data(args.input)
        return items, source

    # 3. fixtures
    fixtures_path = os.path.join(FIXTURES_DIR, "text_samples.jsonl")
    if os.path.exists(fixtures_path):
        logger.info(f"从 fixtures 加载数据: {fixtures_path}")
        return load_jsonl(fixtures_path), fixtures_path

    # 4. fallback
    logger.info("使用内置 fallback 样本")
    return FALLBACK_SAMPLES, "内置 fallback 样本"


# ── 分类任务 ──────────────────────────────────────────────────
def classify_one(
    item: Dict,
    classifier: BlogClassifier,
    verbose: bool,
) -> Dict:
    """
    对单条文本博文执行分类（线程安全）。

    直接调用 BlogClassifier._classify_text，避免 _persist_result 的文件写入竞争。
    """
    mid = str(item.get("mid", ""))
    uid = str(item.get("uid", ""))
    content = item.get("content", "") or ""
    expected = item.get("expected_layer", "未知")

    blog_item = BlogItem(mid=mid, uid=uid, content=content)

    t0 = time.perf_counter()
    try:
        result = classifier._classify_text(blog_item)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        is_correct = (result.layer == expected) if expected != "未知" else None

        record = {
            "index": item.get("index", 0),
            "mid": mid,
            "uid": uid,
            "content_preview": content[:80],
            "expected_layer": expected,
            "actual_layer": result.layer,
            "is_correct": is_correct,
            "success": result.success,
            "error": result.error,
            "elapsed_ms": round(elapsed_ms, 1),
            "media_type": result.media_type,
        }
        if verbose:
            record["model_output"] = result.model_output
            record["content"] = content

        return record

    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "index": item.get("index", 0),
            "mid": mid,
            "uid": uid,
            "content_preview": content[:80],
            "expected_layer": expected,
            "actual_layer": "未识别",
            "is_correct": False if expected != "未知" else None,
            "success": False,
            "error": f"异常: {str(e)}",
            "elapsed_ms": round(elapsed_ms, 1),
            "media_type": "text",
        }


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


def print_summary(
    results: List[Dict],
    total_elapsed_s: float,
    data_source: str,
    workers: int,
    logger: logging.Logger,
):
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
    correct_count = 0
    has_expected = 0
    for r in results:
        layer_dist[r["actual_layer"]] = layer_dist.get(r["actual_layer"], 0) + 1
        if r["expected_layer"] != "未知":
            has_expected += 1
            if r["is_correct"]:
                correct_count += 1

    accuracy = correct_count / has_expected * 100 if has_expected > 0 else None

    summary_lines = [
        "=" * 60,
        "并行批量文本分类测试结果摘要",
        "=" * 60,
        f"运行时间:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"数据来源:     {data_source}",
        f"并发数:       {workers}",
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
        "分类结果分布:",
    ]
    for layer, cnt in sorted(layer_dist.items(), key=lambda x: -x[1]):
        pct = cnt / total * 100
        summary_lines.append(f"  {layer}: {cnt} 条 ({pct:.1f}%)")

    if accuracy is not None:
        summary_lines.append("")
        summary_lines.append(f"准确率（有预期标签的 {has_expected} 条）: {accuracy:.1f}%")

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
        "accuracy": round(accuracy / 100, 4) if accuracy is not None else None,
    }


# ── 主流程 ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="并行批量文本分类测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input-hdfs", default="",
                        help="HDFS 目录/文件路径（优先）")
    parser.add_argument("--input", default="",
                        help="本地 JSONL/TSV 文件路径")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制处理条数（0=不限制）")
    parser.add_argument("--workers", type=int, default=10,
                        help="并发数（默认 10，平台推荐 8~12）")
    parser.add_argument("--verbose", action="store_true",
                        help="在结果中保存模型原始输出与完整正文")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    parser.add_argument("--timeout", type=int, default=0,
                        help="单条请求超时（秒，0=使用配置文件）")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_parallel_text", log_dir=os.path.join(PROJECT_DIR, "logs"))

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("=" * 60)
    logger.info("并行批量文本分类测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")
    logger.info(f"并发数: {args.workers}")

    # ── 加载配置和分类器 ──────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 若命令行指定 timeout，覆盖配置
    if args.timeout > 0:
        config["api"]["timeout"] = args.timeout

    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"thinking 模式: {config['api'].get('enable_thinking', False)}")
    logger.info(f"单条超时: {config['api'].get('timeout', 60)}s")

    classifier = BlogClassifier(config, logger)

    # ── 加载数据 ──────────────────────────────────────────────
    raw_items, data_source = load_data(args, logger)

    # 补充 index 字段
    for i, item in enumerate(raw_items):
        item["index"] = i + 1

    if args.limit and args.limit > 0:
        raw_items = raw_items[:args.limit]
        logger.info(f"--limit {args.limit}，截取前 {len(raw_items)} 条")

    total = len(raw_items)
    logger.info(f"数据来源: {data_source}")
    logger.info(f"待处理条数: {total}")
    logger.info("=" * 60)

    if total == 0:
        logger.warning("没有待处理数据，退出")
        sys.exit(0)

    # ── 并行分类 ──────────────────────────────────────────────
    results: List[Dict] = []
    start_time = time.perf_counter()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_item = {
            executor.submit(classify_one, item, classifier, args.verbose): item
            for item in raw_items
        }

        completed = 0
        for future in as_completed(future_to_item):
            record = future.result()
            results.append(record)
            completed += 1

            # 每 10% 或每 10 条打印一次进度
            if completed % max(1, total // 10) == 0 or completed == total or completed % 10 == 0:
                success_so_far = sum(1 for r in results if r["success"])
                fail_so_far = completed - success_so_far
                elapsed_so_far = time.perf_counter() - start_time
                eta = (elapsed_so_far / completed) * (total - completed) if completed > 0 else 0
                print(f"\r  进度: {completed}/{total}  ✅{success_so_far} ❌{fail_so_far}  "
                      f"已用:{elapsed_so_far:.0f}s  预计剩余:{eta:.0f}s", end="", flush=True)

    print()  # 换行
    total_elapsed_s = time.perf_counter() - start_time

    # 按原始顺序排序
    results.sort(key=lambda x: x["index"])

    # ── 统计与摘要 ────────────────────────────────────────────
    summary_lines, summary_dict = print_summary(
        results, total_elapsed_s, data_source, args.workers, logger
    )

    # ── 保存结果 ──────────────────────────────────────────────
    output_json = os.path.join(OUTPUT_DIR, f"parallel_text_{run_ts}.json")
    output_tsv = os.path.join(OUTPUT_DIR, f"parallel_text_{run_ts}.tsv")
    output_summary = os.path.join(OUTPUT_DIR, f"parallel_text_{run_ts}_summary.txt")

    # 1. JSON 完整结果
    output_data = {
        "test_type": "parallel_text",
        "run_time": datetime.now().isoformat(),
        "data_source": data_source,
        "config": {
            "model": config["api"]["model"],
            "enable_thinking": config["api"].get("enable_thinking", False),
            "temperature": config["api"].get("temperature", 0.0),
            "workers": args.workers,
            "timeout": config["api"].get("timeout", 60),
        },
        "summary": summary_dict,
        "results": results,
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    # 2. TSV 汇总
    with open(output_tsv, "w", encoding="utf-8") as f:
        f.write("index\tmid\tuid\tactual_layer\tsuccess\terror\telapsed_ms\n")
        for r in results:
            f.write(
                f"{r['index']}\t{r['mid']}\t{r['uid']}\t{r['actual_layer']}\t"
                f"{r['success']}\t{r['error'] or ''}\t{r['elapsed_ms']}\n"
            )

    # 3. 摘要文本
    with open(output_summary, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines) + "\n")

    logger.info("=" * 60)
    logger.info("结果文件:")
    logger.info(f"  JSON:    {output_json}")
    logger.info(f"  TSV:     {output_tsv}")
    logger.info(f"  摘要:    {output_summary}")
    logger.info("=" * 60)

    # 失败率超过 20% 时非零退出
    if summary_dict["success_rate"] < 0.8:
        logger.warning(f"成功率 {summary_dict['success_rate']*100:.1f}% 低于 80%，退出码 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
