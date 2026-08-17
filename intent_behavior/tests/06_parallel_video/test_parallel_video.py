#!/usr/bin/env python3
"""
并行批量视频分类测试
====================
功能：从 HDFS/本地批量读取视频博文，使用 ThreadPoolExecutor 并发调用
      Qwen 模型进行视频多模态分类，验证并发稳定性和吞吐性能。

核心特点：
  - 并发请求：默认 10 并发（与平台推荐一致）
  - 数据来源：优先 HDFS，支持本地 JSONL/TSV fallback
  - 结果落盘：JSON 完整结果 + TSV 汇总 + 摘要文本
  - 稳定性指标：成功率、P50/P95/P99 延迟、吞吐（条/秒）

数据来源（按优先级）：
  1. --input-hdfs 指定的 HDFS 目录/文件
  2. --input 指定的本地 JSONL/TSV 文件
  3. tests/01_prepare_data/fixtures/video_samples.jsonl
  4. 内置 fallback 样本

HDFS 字段说明（query_video.sh 导出格式）：
  mid \t uid \t content \t media_id \t customer_info(JSON含fid/cover) \t dt
  其中 customer_info 为 JSON 对象，如 {"fid":"2362904:...","cover":"http://..."}

输出路径：
  tests/06_parallel_video/output/parallel_video_<timestamp>.json
  tests/06_parallel_video/output/parallel_video_<timestamp>.tsv
  tests/06_parallel_video/output/parallel_video_<timestamp>_summary.txt

运行方式：
  # 从 HDFS 读取视频博文并并发分类（默认 cover 模式）
  python3 tests/06_parallel_video/test_parallel_video.py \
      --input-hdfs /dw_ext/ad/person/xuanyu11/intent_behavior/data/video_weibo_ad_20260701_20260701/000000_0 \
      --workers 10 --limit 20

  # frame 模式（下载视频 + OpenCV 抽帧）
  python3 tests/06_parallel_video/test_parallel_video.py \
      --input-hdfs /dw_ext/ad/person/xuanyu11/intent_behavior/data/video_weibo_ad_20260701_20260701/000000_0 \
      --video-mode frame --workers 3 --limit 10

运行时间预估：
  - cover 模式 100 条、10 并发：约 2~5 分钟
  - frame 模式 10 条、3 并发：约 3~10 分钟（取决于视频大小和下载速度）

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
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple

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
        "mid": "5250301234567890",
        "uid": "2608812381",
        "content": "吉利银河M9极寒测试，零下40度挑战！看看新能源旗舰SUV在极端低温下的真实表现",
        "media_type": "video",
        "media_info": [{"media_type": "2", "customer_info": '{"fid":"2362904:4666847103221848","cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg"}'}],
        "expected_layer": "兴趣层",
    },
    {
        "mid": "5250301234567891",
        "uid": "2608812381",
        "content": "比亚迪汉EV 2026款正式上市！全新外观设计，续航突破800km，售价18.98万起",
        "media_type": "video",
        "media_info": [{"media_type": "2", "customer_info": '{"fid":"2362904:4826598285967434","cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg"}'}],
        "expected_layer": "考虑层",
    },
    {
        "mid": "5250301234567892",
        "uid": "2608812381",
        "content": "小鹏X9品牌TVC大片，未来已来，智能出行新纪元",
        "media_type": "video",
        "media_info": [{"media_type": "2", "customer_info": '{"fid":"2362904:4826598285967435","cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg"}'}],
        "expected_layer": "认知层",
    },
]


# ── 数据加载 ──────────────────────────────────────────────────
def read_hdfs_or_local_lines(path: str, logger: logging.Logger) -> List[str]:
    """读取本地文件或 HDFS 文件内容，返回非空行列表"""
    if os.path.exists(path):
        logger.info(f"[Local] 读取本地文件: {path}")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]

    logger.info(f"[HDFS] 尝试读取 HDFS 文件: {path}")
    try:
        result = subprocess.run(
            ["hdfs", "dfs", "-cat", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if result.returncode != 0:
            logger.warning(f"[HDFS] 读取失败: {result.stderr[:300]}")
            return []
        return [ln.rstrip("\n") for ln in result.stdout.splitlines() if ln.strip()]
    except FileNotFoundError:
        logger.warning("[HDFS] 未找到 hdfs 命令")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("[HDFS] 读取超时")
        return []


def parse_video_hdfs_lines(lines: List[str]) -> List[Dict]:
    """解析视频 HDFS 导出文件：mid\tuid\tcontent\tmedia_id\tcustomer_info\tdt"""
    items = []
    for line in lines:
        if line.strip().lower().startswith("mid"):
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        mid, uid, content = parts[0], parts[1], parts[2]
        customer_info = parts[4] if len(parts) > 4 else "{}"
        dt = parts[5] if len(parts) > 5 else ""

        # 从 customer_info 解析 fid（showBatch API 需要的参数）
        fid = ""
        try:
            info = json.loads(customer_info)
            if isinstance(info, dict):
                fid = info.get("fid", "")
        except (json.JSONDecodeError, TypeError):
            pass

        if not fid:
            continue

        items.append({
            "mid": mid,
            "uid": uid,
            "content": content,
            "media_type": "video",
            "media_info": [{"media_type": "2", "customer_info": customer_info}],
            "fid": fid,
            "expected_layer": "未知",
            "dt": dt,
        })
    return items


def load_jsonl(filepath: str) -> List[Dict]:
    """加载 JSONL 文件"""
    items = []
    with open(filepath, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                # 兼容旧格式：若 media_info 中存在 video，提取 fid
                if "media_info" in item and not item.get("fid"):
                    for mi in item.get("media_info", []):
                        if mi.get("media_type") == "2":
                            try:
                                ci = json.loads(mi.get("customer_info", "{}"))
                                item["fid"] = ci.get("fid", "")
                            except Exception:
                                pass
                items.append(item)
            except json.JSONDecodeError as e:
                print(f"  ⚠️  第 {lineno} 行 JSON 解析失败: {e}")
    return items


def load_tsv(filepath: str) -> List[Dict]:
    """加载 TSV 文件（兼容 HDFS 导出格式）"""
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    has_header = lines and lines[0].strip().startswith("mid")
    data_lines = lines[1:] if has_header else lines
    return parse_video_hdfs_lines(data_lines)


def load_local_data(input_path: str) -> Tuple[List[Dict], str]:
    """加载本地数据文件"""
    if input_path.endswith(".jsonl") or input_path.endswith(".json"):
        return load_jsonl(input_path), input_path
    else:
        return load_tsv(input_path), input_path


def load_hdfs_data(hdfs_path: str, logger: logging.Logger) -> Tuple[List[Dict], str]:
    """通过 hdfs dfs -cat 读取 HDFS 上的视频 TSV 数据"""
    lines = read_hdfs_or_local_lines(hdfs_path, logger)
    items = parse_video_hdfs_lines(lines)
    return items, hdfs_path


def load_data(args, logger: logging.Logger) -> Tuple[List[Dict], str]:
    """按优先级加载测试数据"""
    if args.input_hdfs:
        logger.info(f"从 HDFS 加载数据: {args.input_hdfs}")
        items, source = load_hdfs_data(args.input_hdfs, logger)
        return items, source

    if args.input:
        if not os.path.exists(args.input):
            logger.error(f"输入文件不存在: {args.input}")
            sys.exit(1)
        logger.info(f"从本地文件加载数据: {args.input}")
        items, source = load_local_data(args.input)
        return items, source

    fixtures_path = os.path.join(FIXTURES_DIR, "video_samples.jsonl")
    if os.path.exists(fixtures_path):
        logger.info(f"从 fixtures 加载数据: {fixtures_path}")
        return load_jsonl(fixtures_path), fixtures_path

    logger.info("使用内置 fallback 样本")
    return FALLBACK_SAMPLES, "内置 fallback 样本"


# ── 数据转换 ──────────────────────────────────────────────────
def raw_to_blog_item(raw: Dict) -> BlogItem:
    """将原始数据项转换为 BlogItem，视频 media_ids 使用 fid"""
    mid = str(raw.get("mid", ""))
    uid = str(raw.get("uid", ""))
    content = raw.get("content", "") or ""
    fid = raw.get("fid", "")

    # 兜底：从 media_info 中提取 fid
    if not fid:
        media_info = raw.get("media_info") or []
        for m in media_info:
            if m.get("media_type") == "2":
                try:
                    ci = json.loads(m.get("customer_info", "{}"))
                    if isinstance(ci, dict) and ci.get("fid"):
                        fid = ci["fid"]
                        break
                except (json.JSONDecodeError, TypeError):
                    pass

    return BlogItem(
        mid=mid,
        uid=uid,
        content=content,
        pic_ids=[],
        media_ids=[fid] if fid else [],
        dt=raw.get("dt", ""),
    )


# ── 分类任务 ──────────────────────────────────────────────────
def classify_one(
    item: Dict,
    classifier: BlogClassifier,
    verbose: bool,
) -> Dict:
    """对单条视频博文执行分类（线程安全，直接调用 _classify_video）"""
    mid = str(item.get("mid", ""))
    uid = str(item.get("uid", ""))
    content = item.get("content", "") or ""
    fid = item.get("fid", "")
    expected = item.get("expected_layer", "未知")

    blog_item = raw_to_blog_item(item)

    t0 = time.perf_counter()
    try:
        result = classifier._classify_video(blog_item)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        is_correct = (result.layer == expected) if expected != "未知" else None

        record = {
            "index": item.get("index", 0),
            "mid": mid,
            "uid": uid,
            "content_preview": content[:80],
            "fid": fid,
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
            "fid": fid,
            "expected_layer": expected,
            "actual_layer": "未识别",
            "is_correct": False if expected != "未知" else None,
            "success": False,
            "error": f"异常: {str(e)}",
            "elapsed_ms": round(elapsed_ms, 1),
            "media_type": "video",
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
    video_mode: str,
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
        "并行批量视频分类测试结果摘要",
        "=" * 60,
        f"运行时间:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"数据来源:     {data_source}",
        f"视频模式:     {video_mode}",
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
        description="并行批量视频分类测试",
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
    parser.add_argument("--video-mode", default="", choices=["", "cover", "frame"],
                        help="视频处理模式：cover（封面图）或 frame（OpenCV抽帧），空则使用配置")
    parser.add_argument("--verbose", action="store_true",
                        help="在结果中保存模型原始输出与完整正文")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    parser.add_argument("--timeout", type=int, default=0,
                        help="单条请求超时（秒，0=使用配置文件）")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_parallel_video", log_dir=os.path.join(PROJECT_DIR, "logs"))

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("=" * 60)
    logger.info("并行批量视频分类测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")
    logger.info(f"并发数: {args.workers}")

    # ── 加载配置和分类器 ──────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.timeout > 0:
        config["api"]["timeout"] = args.timeout

    if args.video_mode:
        config["media"]["video"]["video_mode"] = args.video_mode

    video_mode = config["media"]["video"].get("video_mode", "cover")
    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"thinking 模式: {config['api'].get('enable_thinking', False)}")
    logger.info(f"视频处理模式: {video_mode}")
    logger.info(f"单条超时: {config['api'].get('timeout', 60)}s")

    if video_mode == "frame":
        try:
            import cv2  # noqa: F401
        except ImportError:
            logger.error("frame 模式需要 opencv-python-headless，请先安装")
            sys.exit(1)

    classifier = BlogClassifier(config, logger)

    # ── 加载数据 ──────────────────────────────────────────────
    raw_items, data_source = load_data(args, logger)

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

            if completed % max(1, total // 10) == 0 or completed == total or completed % 10 == 0:
                success_so_far = sum(1 for r in results if r["success"])
                fail_so_far = completed - success_so_far
                elapsed_so_far = time.perf_counter() - start_time
                eta = (elapsed_so_far / completed) * (total - completed) if completed > 0 else 0
                print(f"\r  进度: {completed}/{total}  ✅{success_so_far} ❌{fail_so_far}  "
                      f"已用:{elapsed_so_far:.0f}s  预计剩余:{eta:.0f}s", end="", flush=True)

    print()
    total_elapsed_s = time.perf_counter() - start_time

    # 按原始顺序排序
    results.sort(key=lambda x: x["index"])

    # ── 统计与摘要 ────────────────────────────────────────────
    summary_lines, summary_dict = print_summary(
        results, total_elapsed_s, data_source, args.workers, video_mode, logger
    )

    # ── 保存结果 ──────────────────────────────────────────────
    output_json = os.path.join(OUTPUT_DIR, f"parallel_video_{run_ts}.json")
    output_tsv = os.path.join(OUTPUT_DIR, f"parallel_video_{run_ts}.tsv")
    output_summary = os.path.join(OUTPUT_DIR, f"parallel_video_{run_ts}_summary.txt")

    output_data = {
        "test_type": "parallel_video",
        "run_time": datetime.now().isoformat(),
        "data_source": data_source,
        "config": {
            "model": config["api"]["model"],
            "enable_thinking": config["api"].get("enable_thinking", False),
            "temperature": config["api"].get("temperature", 0.0),
            "workers": args.workers,
            "timeout": config["api"].get("timeout", 60),
            "video_mode": video_mode,
        },
        "summary": summary_dict,
        "results": results,
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    with open(output_tsv, "w", encoding="utf-8") as f:
        f.write("index\tmid\tuid\tfid\tactual_layer\tsuccess\terror\telapsed_ms\n")
        for r in results:
            f.write(
                f"{r['index']}\t{r['mid']}\t{r['uid']}\t{r['fid']}\t"
                f"{r['actual_layer']}\t{r['success']}\t{r['error'] or ''}\t{r['elapsed_ms']}\n"
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
