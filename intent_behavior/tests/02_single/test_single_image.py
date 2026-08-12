#!/usr/bin/env python3
"""
单条图文博文分类测试
====================
功能：对单条图文博文（含图片 pid）调用分类接口，验证多模态分类链路。
      链路：pid → URL → 下载图片 → base64 → Qwen3.6 多模态分类

数据来源：
  tests/01_prepare_data/fixtures/image_samples.jsonl
  （若文件不存在，使用内置 fallback 样本）

输出路径：
  tests/02_single/output/single_image_<timestamp>.json

运行方式：
  # 使用默认样本（第一条）
  python3 tests/02_single/test_single_image.py

  # 指定样本索引
  python3 tests/02_single/test_single_image.py --index 1

  # 指定自定义 pid（逗号分隔）
  python3 tests/02_single/test_single_image.py --mid xxx --uid yyy --content "博文内容" --pids "pid1,pid2"

  # 显示模型原始输出
  python3 tests/02_single/test_single_image.py --verbose

运行时间预估：约 5~20 秒（含图片下载 + vLLM 推理）

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

# ── 内置 fallback 样本（已验证 pid 可下载）──────────────────
FALLBACK_SAMPLES = [
    {
        "mid": "5250292767523625",
        "uid": "1647951825",
        "content": "比亚迪宋L实拍来了！外观绝了，这个颜色真的太好看了，内饰也很精致，大家觉得怎么样？",
        "media_type": "image",
        "media_info": [
            {
                "media_type": "1",
                "customer_info": '["6239bfd1ly1glk3gl3bqfj20gg08843c","6239bfd1ly1glk3jvoyqkj20gg08878t"]'
            }
        ],
        "expected_layer": "兴趣层",
        "note": "图文实拍种草，引发兴趣，典型兴趣层"
    },
    {
        "mid": "5250292767523626",
        "uid": "1647951826",
        "content": "威兰达3天免费用车券限时抽取！参与话题活动#威兰达1212宠粉计划#，转发本微博即可参与抽奖，中奖率超高！",
        "media_type": "image",
        "media_info": [
            {
                "media_type": "1",
                "customer_info": '["6239bfd1ly1glk49t82q0j20gg0880x6"]'
            }
        ],
        "expected_layer": "考虑层",
        "note": "促销活动+图片，典型考虑层"
    },
    {
        "mid": "5250292767523627",
        "uid": "1647951827",
        "content": "理想L9官方宣传大片，六座旗舰SUV，家的感觉",
        "media_type": "image",
        "media_info": [
            {
                "media_type": "1",
                "customer_info": '["62e00111ly1fvco1lvodmj20gg088gol"]'
            }
        ],
        "expected_layer": "认知层",
        "note": "品牌宣传图文，典型认知层"
    },
]


def extract_pids_from_media_info(media_info: list) -> list:
    """从 media_info 中提取图片 pid 列表"""
    pids = []
    if not media_info:
        return pids
    for item in media_info:
        if item.get("media_type") == "1":
            customer_info = item.get("customer_info", "")
            try:
                pid_list = json.loads(customer_info)
                if isinstance(pid_list, list):
                    pids.extend(pid_list)
            except (json.JSONDecodeError, TypeError):
                pass
    return pids


def load_samples():
    """从 fixtures 加载图片样本，不存在则用 fallback"""
    fixtures_path = os.path.join(FIXTURES_DIR, "image_samples.jsonl")
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
    """执行单条图文分类测试，返回测试结果字典"""
    mid = sample["mid"]
    uid = sample["uid"]
    content = sample["content"]
    media_info = sample.get("media_info") or []
    expected = sample.get("expected_layer", "未知")
    note = sample.get("note", "")

    # 提取 pid 列表
    pids = extract_pids_from_media_info(media_info)

    logger.info(f"测试样本: mid={mid}")
    logger.info(f"  内容预览: {content[:80]}")
    logger.info(f"  图片 pid 数量: {len(pids)}")
    if pids:
        logger.info(f"  pid 列表: {pids}")
    logger.info(f"  预期结果: {expected}")
    logger.info(f"  备注: {note}")

    t_start = datetime.now()
    result = classifier.classify(
        mid=mid,
        uid=uid,
        content=content,
        image_pids=pids if pids else None
    )
    elapsed_ms = (datetime.now() - t_start).total_seconds() * 1000

    is_correct = (result.layer == expected) if expected != "未知" else None

    test_result = {
        "mid": mid,
        "uid": uid,
        "content_preview": content[:100],
        "pids": pids,
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
    logger.info(f"  实际媒体类型: {result.media_type}")
    if result.error:
        logger.warning(f"  错误: {result.error}")
    if verbose and result.model_output:
        logger.info(f"  模型输出: {result.model_output[:300]}")

    return test_result


def main():
    parser = argparse.ArgumentParser(
        description="单条图文博文分类测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--index", type=int, default=0,
                        help="使用 fixtures 中第几条样本（从0开始，默认0）")
    parser.add_argument("--mid", default="", help="自定义博文ID")
    parser.add_argument("--uid", default="", help="自定义用户ID")
    parser.add_argument("--content", default="", help="自定义博文内容")
    parser.add_argument("--pids", default="", help="图片 pid 列表，逗号分隔")
    parser.add_argument("--verbose", action="store_true", help="显示模型原始输出")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_single_image", log_dir=os.path.join(PROJECT_DIR, "logs"))

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("=" * 60)
    logger.info("单条图文博文分类测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")

    # ── 加载配置和分类器 ──────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"thinking 模式: {config['api'].get('enable_thinking', False)}")
    logger.info(f"图片 URL 模板: {config['media']['image']['url_pattern']}")
    classifier = BlogClassifier(config, logger)

    # ── 确定测试样本 ──────────────────────────────────────────
    if args.mid and args.content:
        pids = [p.strip() for p in args.pids.split(",") if p.strip()] if args.pids else []
        sample = {
            "mid": args.mid,
            "uid": args.uid or "custom_uid",
            "content": args.content,
            "media_type": "image",
            "media_info": [{"media_type": "1", "customer_info": json.dumps(pids)}] if pids else [],
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
    output_file = os.path.join(OUTPUT_DIR, f"single_image_{run_ts}.json")
    output_data = {
        "test_type": "single_image",
        "run_time": datetime.now().isoformat(),
        "data_source": data_source,
        "config": {
            "model": config["api"]["model"],
            "enable_thinking": config["api"].get("enable_thinking", False),
            "temperature": config["api"].get("temperature", 0.0),
            "image_url_pattern": config["media"]["image"]["url_pattern"],
        },
        "result": test_result
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info(f"测试完成，结果已保存: {output_file}")
    logger.info("=" * 60)

    if not test_result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
