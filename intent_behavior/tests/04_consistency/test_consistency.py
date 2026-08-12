#!/usr/bin/env python3
"""
分类一致性测试
==============
功能：对同一条博文（文本/图片/视频）重复调用分类接口 N 次，验证结果是否完全一致。
      这是项目的两个硬要求之一：「同一条博文/图/视频必须给出一致的分类结果」。

一致性保障机制：
  - temperature=0.0（贪心解码，确定性输出）
  - enable_thinking=false（关闭 thinking 模式，避免输出被截断）
  - 相同 prompt 模板

测试策略：
  - 每条博文重复调用 N 次（默认 5 次）
  - 统计每次结果，计算一致率
  - 若所有次结果相同 → 一致性通过 ✅
  - 若出现不同结果 → 一致性失败 ❌（需排查原因）

数据来源：
  tests/01_prepare_data/fixtures/all_samples.jsonl（若存在）
  或内置 fallback 样本（每种类型各取 1 条）

输出路径：
  tests/04_consistency/output/consistency_<timestamp>.json
  tests/04_consistency/output/consistency_<timestamp>_report.txt

运行方式：
  # 默认：每条重复 5 次，测试 fixtures 中所有样本
  python3 tests/04_consistency/test_consistency.py

  # 指定重复次数
  python3 tests/04_consistency/test_consistency.py --repeat 10

  # 只测试文本类型
  python3 tests/04_consistency/test_consistency.py --type text

  # 只测试指定 mid
  python3 tests/04_consistency/test_consistency.py --mid 5250218712893321

  # 显示每次模型原始输出（排查不一致时使用）
  python3 tests/04_consistency/test_consistency.py --verbose

运行时间预估：
  - 3条样本 × 5次 = 15次调用：约 1~5 分钟
  - 3条样本 × 10次 = 30次调用：约 2~10 分钟

作者：xuanyu11
创建时间：2026-08-12
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime
from typing import List, Dict, Optional
from collections import Counter

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
FIXTURES_DIR = os.path.join(PROJECT_DIR, "tests/01_prepare_data/fixtures")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.classifier import BlogClassifier
from src.models import BlogItem
from src.utils import setup_logger


# ── 内置 fallback 样本（每种类型各 1 条）──────────────────────
FALLBACK_SAMPLES = [
    {
        "mid": "5250218712893321",
        "uid": "1647951825",
        "content": "#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里，第五代智能电混双擎加持，WLTC综合油耗低至4.59L/100km，通勤一月一加油、自驾跨省不补能。TSS 4.0智驾+15.6英寸大屏，新车已经到店了，家用还是挺好的",
        "media_type": "text",
        "media_info": None,
        "expected_layer": "考虑层",
        "note": "文本样本（含价格/参数/到店信息）"
    },
    {
        "mid": "5250292767523625",
        "uid": "1647951825",
        "content": "比亚迪宋L实拍来了！外观绝了，这个颜色真的太好看了，内饰也很精致，大家觉得怎么样？",
        "media_type": "image",
        "media_info": [
            {
                "media_type": "1",
                "customer_info": '["6239bfd1ly1glk3gl3bqfj20gg08843c"]'
            }
        ],
        "expected_layer": "兴趣层",
        "note": "图片样本（实拍种草）"
    },
    {
        "mid": "5250301234567890",
        "uid": "1647951825",
        "content": "吉利银河M9极寒测试，零下40度挑战！看看新能源旗舰SUV在极端低温下的真实表现",
        "media_type": "video",
        "media_info": [
            {
                "media_type": "2",
                "customer_info": '{"cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg","fid":"2362904:4666847103221848","url":"https://video.weibo.com/show?fid=2362904:4666847103221848"}'
            }
        ],
        "expected_layer": "兴趣层",
        "note": "视频样本（产品测试）"
    },
]


def load_samples(media_type_filter: Optional[str], mid_filter: Optional[str]) -> List[Dict]:
    """加载测试样本"""
    fixtures_path = os.path.join(FIXTURES_DIR, "all_samples.jsonl")
    if os.path.exists(fixtures_path):
        samples = []
        with open(fixtures_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    samples.append(json.loads(line))
    else:
        samples = FALLBACK_SAMPLES

    # 过滤
    if mid_filter:
        samples = [s for s in samples if str(s.get("mid", "")) == mid_filter]
    if media_type_filter:
        samples = [s for s in samples if s.get("media_type", "text") == media_type_filter]

    # 每种类型最多取 1 条（避免测试时间过长）
    if not mid_filter and not media_type_filter:
        seen_types = set()
        filtered = []
        for s in samples:
            mt = s.get("media_type", "text")
            if mt not in seen_types:
                seen_types.add(mt)
                filtered.append(s)
        samples = filtered

    return samples


def jsonl_item_to_blog_item(item: Dict) -> BlogItem:
    """将 JSONL 数据项转换为 BlogItem"""
    mid = str(item.get("mid", ""))
    uid = str(item.get("uid", ""))
    content = item.get("content", "") or ""
    media_info = item.get("media_info") or []

    pic_ids = []
    media_ids = []

    if isinstance(media_info, list):
        for m in media_info:
            mt = str(m.get("media_type", ""))
            customer_info = m.get("customer_info", "")
            if mt == "1":
                try:
                    pid_list = json.loads(customer_info)
                    if isinstance(pid_list, list):
                        pic_ids.extend(pid_list)
                except (json.JSONDecodeError, TypeError):
                    pass
            elif mt == "2":
                try:
                    video_info = json.loads(customer_info)
                    fid = video_info.get("fid", "")
                    if fid:
                        media_ids.append(fid)
                except (json.JSONDecodeError, TypeError):
                    pass

    return BlogItem(
        mid=mid,
        uid=uid,
        content=content,
        pic_ids=pic_ids,
        media_ids=media_ids,
        dt=item.get("dt", ""),
    )


def test_consistency_for_sample(
    sample: Dict,
    classifier: BlogClassifier,
    repeat: int,
    sleep_sec: float,
    verbose: bool,
    logger: logging.Logger
) -> Dict:
    """
    对单条样本重复调用 repeat 次，返回一致性测试结果。
    """
    mid = sample["mid"]
    uid = sample["uid"]
    content = sample.get("content", "")
    media_type = sample.get("media_type", "text")
    expected = sample.get("expected_layer", "未知")
    note = sample.get("note", "")

    logger.info(f"  样本: mid={mid}  类型={media_type}  预期={expected}")
    logger.info(f"  内容: {content[:60]}...")
    logger.info(f"  重复次数: {repeat}")

    blog_item = jsonl_item_to_blog_item(sample)

    call_results = []
    layers = []
    errors = []
    elapsed_list = []

    for i in range(1, repeat + 1):
        t0 = datetime.now()
        result = classifier.classify_item(blog_item)
        elapsed_ms = (datetime.now() - t0).total_seconds() * 1000

        layers.append(result.layer)
        elapsed_list.append(elapsed_ms)

        call_record = {
            "call_index": i,
            "layer": result.layer,
            "success": result.success,
            "error": result.error,
            "elapsed_ms": round(elapsed_ms, 1),
        }
        if verbose:
            call_record["model_output"] = result.model_output

        call_results.append(call_record)

        if result.error:
            errors.append(f"第{i}次: {result.error}")

        status = "✅" if result.success else "❌"
        logger.info(f"    第{i}次: {status} {result.layer}  ({elapsed_ms:.0f}ms)")

        if sleep_sec > 0 and i < repeat:
            time.sleep(sleep_sec)

    # ── 一致性分析 ────────────────────────────────────────────
    layer_counter = Counter(layers)
    unique_layers = list(layer_counter.keys())
    is_consistent = len(unique_layers) == 1
    majority_layer = layer_counter.most_common(1)[0][0]
    consistency_rate = layer_counter[majority_layer] / repeat

    avg_elapsed = sum(elapsed_list) / len(elapsed_list) if elapsed_list else 0
    max_elapsed = max(elapsed_list) if elapsed_list else 0
    min_elapsed = min(elapsed_list) if elapsed_list else 0

    # 一致性判定
    if is_consistent:
        consistency_status = "PASS"
        logger.info(f"  ✅ 一致性通过：{repeat}次调用结果均为【{majority_layer}】")
    else:
        consistency_status = "FAIL"
        logger.warning(f"  ❌ 一致性失败：结果不一致！分布: {dict(layer_counter)}")
        for layer, cnt in layer_counter.items():
            logger.warning(f"    {layer}: {cnt}次 ({cnt/repeat*100:.0f}%)")

    return {
        "mid": mid,
        "uid": uid,
        "media_type": media_type,
        "content_preview": content[:100],
        "expected_layer": expected,
        "note": note,
        "repeat": repeat,
        "consistency_status": consistency_status,
        "is_consistent": is_consistent,
        "consistency_rate": round(consistency_rate, 4),
        "majority_layer": majority_layer,
        "layer_distribution": dict(layer_counter),
        "unique_layers": unique_layers,
        "avg_elapsed_ms": round(avg_elapsed, 1),
        "max_elapsed_ms": round(max_elapsed, 1),
        "min_elapsed_ms": round(min_elapsed, 1),
        "errors": errors,
        "calls": call_results,
    }


def main():
    parser = argparse.ArgumentParser(
        description="分类一致性测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--repeat", type=int, default=5,
                        help="每条博文重复调用次数（默认5）")
    parser.add_argument("--type", choices=["text", "image", "video"],
                        default="", help="只测试指定类型（不指定则每种各取1条）")
    parser.add_argument("--mid", default="", help="只测试指定 mid")
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="每次调用间隔秒数（默认0.5，避免过快请求）")
    parser.add_argument("--verbose", action="store_true",
                        help="在结果文件中保存每次模型原始输出")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_consistency", log_dir=os.path.join(PROJECT_DIR, "logs"))

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("=" * 60)
    logger.info("分类一致性测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")
    logger.info(f"重复次数: {args.repeat}")

    # ── 加载配置和分类器 ──────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"temperature: {config['api'].get('temperature', 0.0)}")
    logger.info(f"thinking 模式: {config['api'].get('enable_thinking', False)}")

    # 关键检查：temperature 必须为 0.0
    temp = config["api"].get("temperature", 0.0)
    if temp != 0.0:
        logger.warning(f"⚠️  temperature={temp}，非0时结果可能不一致！建议设为 0.0")

    # 关键检查：thinking 必须关闭
    thinking = config["api"].get("enable_thinking", False)
    if thinking:
        logger.warning("⚠️  enable_thinking=true，可能导致输出被截断，建议关闭！")

    classifier = BlogClassifier(config, logger)

    # ── 加载测试样本 ──────────────────────────────────────────
    samples = load_samples(
        media_type_filter=args.type or None,
        mid_filter=args.mid or None
    )

    if not samples:
        logger.error("未找到匹配的测试样本")
        sys.exit(1)

    logger.info(f"测试样本数: {len(samples)}")
    logger.info("=" * 60)

    # ── 执行一致性测试 ────────────────────────────────────────
    all_results = []
    total_start = datetime.now()

    for idx, sample in enumerate(samples, 1):
        logger.info(f"\n[{idx}/{len(samples)}] 开始测试样本")
        result = test_consistency_for_sample(
            sample, classifier, args.repeat, args.sleep, args.verbose, logger
        )
        all_results.append(result)

    total_elapsed = (datetime.now() - total_start).total_seconds()

    # ── 汇总报告 ──────────────────────────────────────────────
    pass_count = sum(1 for r in all_results if r["is_consistent"])
    fail_count = len(all_results) - pass_count
    overall_pass = fail_count == 0

    report_lines = [
        "=" * 60,
        "一致性测试报告",
        "=" * 60,
        f"运行时间:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"模型:         {config['api']['model']}",
        f"temperature:  {config['api'].get('temperature', 0.0)}",
        f"thinking:     {config['api'].get('enable_thinking', False)}",
        f"重复次数:     {args.repeat}",
        f"测试样本数:   {len(all_results)}",
        f"总耗时:       {total_elapsed:.1f}s",
        "",
        f"一致性结果:   {'✅ 全部通过' if overall_pass else '❌ 存在不一致'}",
        f"  通过: {pass_count}/{len(all_results)}",
        f"  失败: {fail_count}/{len(all_results)}",
        "",
        "各样本详情:",
    ]

    for r in all_results:
        status = "✅ PASS" if r["is_consistent"] else "❌ FAIL"
        report_lines.append(
            f"  [{status}] mid={r['mid']}  类型={r['media_type']}  "
            f"结果={r['majority_layer']}  一致率={r['consistency_rate']*100:.0f}%  "
            f"分布={r['layer_distribution']}"
        )
        if not r["is_consistent"]:
            report_lines.append(f"    ⚠️  不一致！出现了 {r['unique_layers']} 等不同结果")
        if r["errors"]:
            for err in r["errors"]:
                report_lines.append(f"    ⚠️  错误: {err}")

    report_lines.append("")
    report_lines.append("一致性保障配置检查:")
    report_lines.append(f"  temperature=0.0: {'✅' if config['api'].get('temperature', 0.0) == 0.0 else '❌ 非0，存在随机性风险'}")
    report_lines.append(f"  enable_thinking=false: {'✅' if not config['api'].get('enable_thinking', False) else '❌ 已开启，可能截断'}")
    report_lines.append("=" * 60)

    for line in report_lines:
        logger.info(line)

    # ── 保存结果 ──────────────────────────────────────────────
    output_json = os.path.join(OUTPUT_DIR, f"consistency_{run_ts}.json")
    output_report = os.path.join(OUTPUT_DIR, f"consistency_{run_ts}_report.txt")

    output_data = {
        "test_type": "consistency",
        "run_time": datetime.now().isoformat(),
        "config": {
            "model": config["api"]["model"],
            "temperature": config["api"].get("temperature", 0.0),
            "enable_thinking": config["api"].get("enable_thinking", False),
        },
        "params": {
            "repeat": args.repeat,
            "sleep_sec": args.sleep,
        },
        "summary": {
            "total_samples": len(all_results),
            "pass_count": pass_count,
            "fail_count": fail_count,
            "overall_pass": overall_pass,
            "total_elapsed_s": round(total_elapsed, 1),
        },
        "results": all_results,
    }

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    with open(output_report, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")

    logger.info(f"结果文件:")
    logger.info(f"  JSON:   {output_json}")
    logger.info(f"  报告:   {output_report}")

    # 有不一致时非零退出
    if not overall_pass:
        logger.error(f"一致性测试失败：{fail_count} 条样本结果不一致")
        sys.exit(1)

    logger.info("✅ 一致性测试全部通过")


if __name__ == "__main__":
    main()
