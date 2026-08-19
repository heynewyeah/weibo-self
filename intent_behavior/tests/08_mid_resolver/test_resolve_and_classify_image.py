#!/usr/bin/env python3
"""
图片博文 mid 反解 + 分类测试
============================
功能：通过 mid 调用微博内部反解接口获取博文真实内容（正文 + 图片 pid 列表），
      然后使用 Qwen 模型进行图文多模态分类。

为什么需要：
- MySQL 分表里的 mid_pids 字段可能和实际请求 API 所需不匹配。
- 通过 mid 反解可以拿到最新、最准确的图片 pid。

运行方式：
  # 单条图片博文测试
  python3 tests/08_mid_resolver/test_resolve_and_classify_image.py \
      --mid 5250292767523625 --uid 1647951825

  # 指定配置文件
  python3 tests/08_mid_resolver/test_resolve_and_classify_image.py \
      --mid 5250292767523625 --config config/config.yaml

  # 只反解，不做模型分类（用于验证反解结果）
  python3 tests/08_mid_resolver/test_resolve_and_classify_image.py \
      --mid 5250292767523625 --resolve-only

输出路径：
  tests/08_mid_resolver/output/resolve_image_<mid>_<timestamp>.json

作者：xuanyu11
创建时间：2026-08-19
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.classifier import BlogClassifier
from src.mid_resolver import MidResolverClient
from src.utils import setup_logger


# ── 主流程 ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="图片博文 mid 反解 + 分类测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mid", required=True, help="微博博文 mid")
    parser.add_argument("--uid", default="", help="微博作者 uid（可选，建议传入）")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    parser.add_argument("--resolve-only", action="store_true",
                        help="只执行反解，不调用模型分类")
    parser.add_argument("--timeout", type=int, default=0,
                        help="模型 API 单条超时（秒，0=使用配置）")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_resolve_image", log_dir=os.path.join(PROJECT_DIR, "logs"))

    logger.info("=" * 60)
    logger.info("图片博文 mid 反解 + 分类测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"mid: {args.mid}")
    logger.info(f"uid: {args.uid or '未传入'}")
    logger.info(f"配置文件: {args.config}")

    # ── 加载配置 ──────────────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.timeout > 0:
        config["api"]["timeout"] = args.timeout

    resolver_cfg = config.get("mid_resolver", {})
    resolver = MidResolverClient(
        url=resolver_cfg.get("url", "http://terra.biz.weibo.com/mid/media"),
        timeout=resolver_cfg.get("timeout", 30),
        max_retry=resolver_cfg.get("max_retry", 3),
        logger_=logger,
    )

    # ── 反解 mid ──────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        resolved = resolver.resolve(
            mid=args.mid,
            uid=args.uid or None,
            parse_component=resolver_cfg.get("parse_component", 1),
        )
    except Exception as e:
        logger.exception(f"mid 反解失败: {e}")
        sys.exit(1)

    resolve_elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info(f"反解耗时: {resolve_elapsed_ms:.0f}ms")
    logger.info(f"反解结果:")
    logger.info(f"  mid: {resolved.mid}")
    logger.info(f"  uid: {resolved.uid or ''}")
    logger.info(f"  是否转发: {resolved.is_retweeted}")
    logger.info(f"  正文预览: {(resolved.content or '')[:120]}")
    logger.info(f"  图片 pid 数量: {len(resolved.pic_ids)}")
    for idx, pid in enumerate(resolved.pic_ids, 1):
        logger.info(f"    [{idx}] {pid}")

    if args.resolve_only:
        logger.info("--resolve-only 模式，跳过模型分类")
        output_path = os.path.join(OUTPUT_DIR, f"resolve_image_{args.mid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({
                "mid": args.mid,
                "uid": args.uid,
                "resolved": resolved.to_dict(),
                "resolve_elapsed_ms": round(resolve_elapsed_ms, 1),
            }, f, ensure_ascii=False, indent=2)
        logger.info(f"反解结果已保存: {output_path}")
        sys.exit(0)

    if not resolved.has_image():
        logger.warning("反解结果中没有图片 pid，无法做图文分类，尝试降级为文本分类")

    # ── 分类 ──────────────────────────────────────────────────
    blog_item = resolved.to_blog_item()
    classifier = BlogClassifier(config, logger)

    t1 = time.perf_counter()
    try:
        result = classifier.classify_item(blog_item)
    except Exception as e:
        logger.exception(f"分类异常: {e}")
        sys.exit(1)
    classify_elapsed_ms = (time.perf_counter() - t1) * 1000

    logger.info("=" * 60)
    logger.info("分类结果:")
    logger.info(f"  层级: {result.layer}")
    logger.info(f"  媒体类型: {result.media_type}")
    logger.info(f"  成功: {result.success}")
    logger.info(f"  错误: {result.error or '无'}")
    logger.info(f"  分类耗时: {classify_elapsed_ms:.0f}ms")
    logger.info("=" * 60)

    # ── 保存结果 ──────────────────────────────────────────────
    output_path = os.path.join(OUTPUT_DIR, f"resolve_image_{args.mid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    output_data = {
        "mid": args.mid,
        "uid": args.uid,
        "resolved": resolved.to_dict(),
        "resolve_elapsed_ms": round(resolve_elapsed_ms, 1),
        "classify": {
            "layer": result.layer,
            "media_type": result.media_type,
            "success": result.success,
            "error": result.error,
            "elapsed_ms": round(classify_elapsed_ms, 1),
            "model_output": result.model_output if result.model_output else "",
        },
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    logger.info(f"结果已保存: {output_path}")


if __name__ == "__main__":
    main()
