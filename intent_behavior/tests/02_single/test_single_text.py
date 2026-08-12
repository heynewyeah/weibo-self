#!/usr/bin/env python3
"""
单条文本博文分类测试
====================
功能：对单条纯文字博文调用分类接口，验证分类结果和 thinking 模式关闭。

数据来源：
  tests/01_prepare_data/fixtures/text_samples.jsonl
  （若文件不存在，使用内置 fallback 样本）

输出路径：
  tests/02_single/output/single_text_<timestamp>.json

运行方式：
  # 使用默认样本（第一条）
  python3 tests/02_single/test_single_text.py

  # 指定样本索引
  python3 tests/02_single/test_single_text.py --index 2

  # 指定自定义内容
  python3 tests/02_single/test_single_text.py --mid xxx --uid yyy --content "博文内容"

  # 显示模型原始输出
  python3 tests/02_single/test_single_text.py --verbose

运行时间预估：约 2~10 秒（取决于 vLLM 服务响应速度）

作者：xuanyu11
创建时间：2026-08-12
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
FIXTURES_DIR = os.path.join(PROJECT_DIR, "tests/01_prepare_data/fixtures")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.classifier import BlogClassifier
from src.utils import setup_logger

# ── 内置 fallback 样本（当 fixtures 文件不存在时使用）──────────
FALLBACK_SAMPLES = [
    {
        "mid": "5250218712893321",
        "uid": "1647951825",
        "content": "#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里，第五代智能电混双擎加持，WLTC综合油耗低至4.59L/100km，通勤一月一加油、自驾跨省不补能。TSS 4.0智驾+15.6英寸大屏，新车已经到店了，家用还是挺好的",
        "media_type": "text",
        "media_info": None,
        "expected_layer": "考虑层",
        "note": "含价格/参数/到店信息，典型考虑层"
    },
    {
        "mid": "5250218712893322",
        "uid": "1647951826",
        "content": "今天带大家体验东风日产N6的零压云毯大沙发，坐进去整个人都放松了，这个座椅真的绝了！内饰质感也很在线，感兴趣的朋友可以去店里体验一下",
        "media_type": "text",
        "media_info": None,
        "expected_layer": "兴趣层",
        "note": "产品体验分享，引发兴趣，典型兴趣层"
    },
    {
        "mid": "5250218712893323",
        "uid": "1647951827",
        "content": "比亚迪全新品牌形象发布！「在路上」——这不只是一句口号，更是比亚迪对每一位用户的承诺。新的征程，新的出发。#比亚迪# #新能源汽车#",
        "media_type": "text",
        "media_info": None,
        "expected_layer": "认知层",
        "note": "品牌曝光宣传，无购买引导，典型认知层"
    },
]


def load_config():
    """加载配置文件"""
    import yaml
    config_path = os.path.join(PROJECT_DIR, "config/config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_samples():
    """从 fixtures 加载文本样本，不存在则用 fallback"""
    fixtures_path = os.path.join(FIXTURES_DIR, "text_samples.jsonl")
    if os.path.exists(fixtures_path):
        samples = []
        with open(fixtures_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    samples.append(json.loads(line))
        return samples, fixtures_path
    else:
        return FALLBACK_SAMPLES, "内置 fallback 样本"


def run_test(sample: dict, classifier: BlogClassifier, verbose: bool, logger: logging.Logger) -> dict:
    """执行单条分类测试，返回测试结果字典"""
    mid = sample["mid"]
    uid = sample["uid"]
    content = sample["content"]
    expected = sample.get("expected_layer", "未知")
    note = sample.get("note", "")

    logger.info(f"测试样本: mid={mid}")
    logger.info(f"  内容预览: {content[:80]}...")
    logger.info(f"  预期结果: {expected}")
    logger.info(f"  备注: {note}")

    t_start = datetime.now()
    result = classifier.classify(mid=mid, uid=uid, content=content)
    elapsed_ms = (datetime.now() - t_start).total_seconds() * 1000

    is_correct = (result.layer == expected) if expected != "未知" else None

    test_result = {
        "mid": mid,
        "uid": uid,
        "content_preview": content[:100],
        "expected_layer": expected,
        "actual_layer": result.layer,
        "is_correct": is_correct,
        "success": result.success,
        "error": result.error,
        "elapsed_ms": round(elapsed_ms, 1),
        "media_type": result.media_type,
        "note": note,
    }

    if verbose:
        test_result["model_output"] = result.model_output

    # 打印结果
    status_icon = "✅" if result.success else "❌"
    match_icon = "✅" if is_correct else ("❓" if is_correct is None else "❌")
    logger.info(f"  {status_icon} 分类结果: {result.layer}  {match_icon} 预期: {expected}  耗时: {elapsed_ms:.0f}ms")
    if result.error:
        logger.warning(f"  错误: {result.error}")
    if verbose and result.model_output:
        logger.info(f"  模型输出: {result.model_output[:300]}")

    return test_result


def main():
    parser = argparse.ArgumentParser(
        description="单条文本博文分类测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--index", type=int, default=0,
                        help="使用 fixtures 中第几条样本（从0开始，默认0）")
    parser.add_argument("--mid", default="", help="自定义博文ID（覆盖 fixtures）")
    parser.add_argument("--uid", default="", help="自定义用户ID（覆盖 fixtures）")
    parser.add_argument("--content", default="", help="自定义博文内容（覆盖 fixtures）")
    parser.add_argument("--verbose", action="store_true", help="显示模型原始输出")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_single_text", log_dir=os.path.join(PROJECT_DIR, "logs"))

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("=" * 60)
    logger.info("单条文本博文分类测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")

    # ── 加载配置和分类器 ──────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"thinking 模式: {config['api'].get('enable_thinking', False)}")
    classifier = BlogClassifier(config, logger)

    # ── 确定测试样本 ──────────────────────────────────────────
    if args.mid and args.content:
        # 用户自定义
        sample = {
            "mid": args.mid,
            "uid": args.uid or "custom_uid",
            "content": args.content,
            "media_type": "text",
            "media_info": None,
            "expected_layer": "未知",
            "note": "用户自定义输入"
        }
        data_source = "命令行参数"
    else:
        samples, data_source = load_samples()
        if args.index >= len(samples):
            logger.error(f"--index {args.index} 超出范围（共 {len(samples)} 条）")
            sys.exit(1)
        sample = samples[args.index]

    logger.info(f"数据来源: {data_source}")
    logger.info("=" * 60)

    # ── 执行测试 ──────────────────────────────────────────────
    test_result = run_test(sample, classifier, args.verbose, logger)

    # ── 保存结果 ──────────────────────────────────────────────
    output_file = os.path.join(OUTPUT_DIR, f"single_text_{run_ts}.json")
    output_data = {
        "test_type": "single_text",
        "run_time": datetime.now().isoformat(),
        "data_source": data_source,
        "config": {
            "model": config["api"]["model"],
            "enable_thinking": config["api"].get("enable_thinking", False),
            "temperature": config["api"].get("temperature", 0.0),
        },
        "result": test_result
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info(f"测试完成，结果已保存: {output_file}")
    logger.info("=" * 60)

    # 非零退出码表示测试失败
    if not test_result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
