#!/usr/bin/env python3
"""
统一媒体反解测试脚本
==================
支持三种输入方式，用于手动排查反解失败问题：

1. mid 反解：通过 mid + uid 调用微博反解接口，获取真实 content/pid/fid
2. pid 测试：直接通过 pid 尝试下载图片，验证图片是否可访问
3. fid 测试：直接通过 fid 调用 showBatch API，验证视频封面/视频是否可获取

运行示例：
  # mid 反解（只反解，不分类）
  python3 tests/08_mid_resolver/test_media_resolve.py --mid 5239377989207686 --uid 2050767771

  # mid 反解 + 分类
  python3 tests/08_mid_resolver/test_media_resolve.py --mid 5239377989207686 --uid 2050767771 --classify

  # 直接测试 pid 图片下载
  python3 tests/08_mid_resolver/test_media_resolve.py --pid 006mX07Rly8ifv3xs5535j30ud0plk1m

  # 直接测试 fid 视频信息获取
  python3 tests/08_mid_resolver/test_media_resolve.py --fid "2362904:4826598285967434"

  # 批量测试（从文件读取 mid 列表）
  python3 tests/08_mid_resolver/test_media_resolve.py --input-file mids.txt

作者：xuanyu11
创建时间：2026-08-26
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.mid_resolver import MidResolverClient
from src.media_handler import ImageHandler, VideoHandler
from src.classifier import BlogClassifier
from src.utils import setup_logger


def test_mid_resolve(
    mid: str,
    uid: str,
    resolver: MidResolverClient,
    logger: logging.Logger,
    classify: bool = False,
    config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """测试 mid 反解"""
    result = {
        "type": "mid_resolve",
        "mid": mid,
        "uid": uid,
        "success": False,
        "error": "",
    }

    t0 = time.perf_counter()
    try:
        resolved = resolver.resolve(
            mid=mid,
            uid=uid or None,
            parse_component=1,
        )
        resolve_ms = (time.perf_counter() - t0) * 1000

        result["success"] = True
        result["resolve_ms"] = round(resolve_ms, 1)
        result["resolved"] = resolved.to_dict()

        logger.info(f"✅ mid 反解成功 mid={mid}")
        logger.info(f"  uid: {resolved.uid or ''}")
        logger.info(f"  是否转发: {resolved.is_retweeted}")
        logger.info(f"  正文预览: {(resolved.content or '')[:120]}")
        logger.info(f"  图片 pid 数量: {len(resolved.pic_ids)}")
        for idx, pid in enumerate(resolved.pic_ids, 1):
            logger.info(f"    [{idx}] {pid}")
        if resolved.video_fid:
            logger.info(f"  视频 fid: {resolved.video_fid}")
            logger.info(f"  视频封面: {resolved.video_cover_url[:80]}")
        if resolved.forward_mid:
            logger.info(f"  转发原博文 mid: {resolved.forward_mid}")
            logger.info(f"  转发原博文内容: {(resolved.forward_content or '')[:120]}")

        if classify and config:
            logger.info("开始分类...")
            blog_item = resolved.to_blog_item()
            classifier = BlogClassifier(config, logger)
            t1 = time.perf_counter()
            classify_result = classifier.classify_item(blog_item)
            classify_ms = (time.perf_counter() - t1) * 1000

            result["classify"] = {
                "layer": classify_result.layer,
                "media_type": classify_result.media_type,
                "success": classify_result.success,
                "error": classify_result.error,
                "industry_name": classify_result.industry_name,
                "forward_status": classify_result.forward_status,
                "elapsed_ms": round(classify_ms, 1),
                "model_output": classify_result.model_output[:500] if classify_result.model_output else "",
            }
            logger.info(f"  分类结果: layer={classify_result.layer} media_type={classify_result.media_type} success={classify_result.success}")

    except Exception as e:
        resolve_ms = (time.perf_counter() - t0) * 1000
        result["resolve_ms"] = round(resolve_ms, 1)
        result["error"] = str(e)
        logger.error(f"❌ mid 反解失败 mid={mid}: {e}")

    return result


def test_pid_download(
    pid: str,
    image_handler: ImageHandler,
    logger: logging.Logger,
) -> Dict[str, Any]:
    """测试 pid 图片下载"""
    result = {
        "type": "pid_download",
        "pid": pid,
        "success": False,
        "error": "",
    }

    url = image_handler.pid_to_url(pid)
    logger.info(f"测试 pid 下载: {pid}")
    logger.info(f"  URL: {url}")

    t0 = time.perf_counter()
    save_path = os.path.join(OUTPUT_DIR, f"test_pid_{pid}.jpg")
    try:
        ok = image_handler.download_image(url, save_path)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if ok:
            file_size = os.path.getsize(save_path)
            result["success"] = True
            result["file_size"] = file_size
            result["elapsed_ms"] = round(elapsed_ms, 1)
            logger.info(f"✅ pid 下载成功: {file_size}B, {elapsed_ms:.0f}ms")
            # 清理测试文件
            try:
                os.remove(save_path)
            except OSError:
                pass
        else:
            result["error"] = "下载返回 False"
            result["elapsed_ms"] = round(elapsed_ms, 1)
            logger.warning(f"❌ pid 下载失败: 返回 False")

    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        result["error"] = str(e)
        result["elapsed_ms"] = round(elapsed_ms, 1)
        logger.error(f"❌ pid 下载异常: {e}")

    return result


def test_fid_resolve(
    fid: str,
    video_handler: VideoHandler,
    logger: logging.Logger,
    customer_id: str = "",
) -> Dict[str, Any]:
    """测试 fid 视频信息获取"""
    result = {
        "type": "fid_resolve",
        "fid": fid,
        "success": False,
        "error": "",
    }

    logger.info(f"测试 fid 视频信息: {fid}")

    t0 = time.perf_counter()
    try:
        info = video_handler.get_video_info(fid, customer_id or None)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        result["elapsed_ms"] = round(elapsed_ms, 1)
        result["cover_url"] = info.get("cover_url", "")
        result["video_url"] = info.get("video_url", "")

        if info.get("cover_url") or info.get("video_url"):
            result["success"] = True
            logger.info(f"✅ fid 信息获取成功:")
            logger.info(f"  封面图: {info['cover_url'][:80] if info['cover_url'] else '无'}")
            logger.info(f"  视频URL: {info['video_url'][:80] if info['video_url'] else '无'}")

            # 尝试下载封面图
            if info.get("cover_url"):
                cover_path = os.path.join(OUTPUT_DIR, f"test_fid_{fid.replace(':', '_')}_cover.jpg")
                cover_ok = video_handler.download_cover(info["cover_url"], cover_path)
                if cover_ok:
                    cover_size = os.path.getsize(cover_path)
                    result["cover_downloaded"] = True
                    result["cover_size"] = cover_size
                    logger.info(f"  封面图下载成功: {cover_size}B")
                    try:
                        os.remove(cover_path)
                    except OSError:
                        pass
                else:
                    result["cover_downloaded"] = False
                    logger.warning(f"  封面图下载失败")
        else:
            result["error"] = "showBatch API 未返回有效信息"
            logger.warning(f"❌ fid 信息获取失败: API 未返回有效数据")

    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        result["error"] = str(e)
        result["elapsed_ms"] = round(elapsed_ms, 1)
        logger.error(f"❌ fid 信息获取异常: {e}")

    return result


def load_mid_list(filepath: str) -> List[Dict[str, str]]:
    """从文件加载 mid 列表，支持 TSV (mid\tuid) 或纯 mid 列表"""
    items = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            item = {"mid": parts[0]}
            if len(parts) > 1:
                item["uid"] = parts[1]
            items.append(item)
    return items


def main():
    parser = argparse.ArgumentParser(
        description="统一媒体反解测试脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # 输入参数（三选一）
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--mid", help="微博博文 mid")
    input_group.add_argument("--pid", help="图片 pid，直接测试图片下载")
    input_group.add_argument("--fid", help="视频 fid，直接测试视频信息获取")
    input_group.add_argument("--input-file", help="批量测试：mid 列表文件（每行 mid 或 mid\\tuid）")

    # 可选参数
    parser.add_argument("--uid", default="", help="微博作者 uid（配合 --mid 使用）")
    parser.add_argument("--customer-id", default="", help="客户ID（配合 --fid 使用）")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    parser.add_argument("--classify", action="store_true",
                        help="mid 反解后继续执行分类（仅 --mid 模式有效）")
    parser.add_argument("--industry", default="", choices=["", "汽车", "奶茶"],
                        help="指定行业（配合 --classify 使用）")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_media_resolve", log_dir=os.path.join(PROJECT_DIR, "logs"))

    logger.info("=" * 60)
    logger.info("统一媒体反解测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ── 加载配置 ──────────────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    resolver_cfg = config.get("mid_resolver", {})
    resolver = MidResolverClient(
        url=resolver_cfg.get("url", "http://terra.biz.weibo.com/mid/media"),
        timeout=resolver_cfg.get("timeout", 30),
        max_retry=resolver_cfg.get("max_retry", 3),
        logger_=logger,
    )

    image_handler = ImageHandler(config["media"]["image"], logger)
    video_handler = VideoHandler(config["media"]["video"], logger)

    results: List[Dict[str, Any]] = []

    # ── 执行测试 ──────────────────────────────────────────────
    if args.mid:
        logger.info(f"模式: mid 反解, mid={args.mid}, uid={args.uid or '未传入'}")
        result = test_mid_resolve(
            args.mid, args.uid, resolver, logger,
            classify=args.classify, config=config if args.classify else None,
        )
        results.append(result)

    elif args.pid:
        logger.info(f"模式: pid 图片下载测试, pid={args.pid}")
        result = test_pid_download(args.pid, image_handler, logger)
        results.append(result)

    elif args.fid:
        logger.info(f"模式: fid 视频信息测试, fid={args.fid}")
        result = test_fid_resolve(args.fid, video_handler, logger, args.customer_id)
        results.append(result)

    elif args.input_file:
        logger.info(f"模式: 批量 mid 反解, 文件={args.input_file}")
        mid_list = load_mid_list(args.input_file)
        logger.info(f"加载 {len(mid_list)} 条 mid")

        for i, item in enumerate(mid_list, 1):
            logger.info(f"\n[{i}/{len(mid_list)}] ─────────────────────────")
            result = test_mid_resolve(
                item["mid"], item.get("uid", ""), resolver, logger,
                classify=args.classify, config=config if args.classify else None,
            )
            results.append(result)

    # ── 汇总 ──────────────────────────────────────────────────
    total = len(results)
    success_count = sum(1 for r in results if r["success"])
    fail_count = total - success_count

    logger.info("\n" + "=" * 60)
    logger.info("测试汇总:")
    logger.info(f"  总数: {total}")
    logger.info(f"  成功: {success_count}")
    logger.info(f"  失败: {fail_count}")
    logger.info("=" * 60)

    # ── 保存结果 ──────────────────────────────────────────────
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(OUTPUT_DIR, f"media_resolve_{run_ts}.json")
    output_data = {
        "run_time": datetime.now().isoformat(),
        "mode": "mid" if args.mid else "pid" if args.pid else "fid" if args.fid else "batch",
        "summary": {
            "total": total,
            "success": success_count,
            "fail": fail_count,
        },
        "results": results,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    logger.info(f"结果已保存: {output_path}")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
