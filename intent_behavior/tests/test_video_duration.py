#!/usr/bin/env python3
"""
视频博文时长统计脚本
==================
通过 mid 反解获取视频 fid，再调用 showBatch API 获取视频时长。

数据来源：
  1. mid 反解接口 → 获取 video.fid
  2. showBatch API → 获取 duration（秒）

用法：
  # 指定 mid 列表（逗号分隔）
  python3 tests/test_video_duration.py --mids 5333296278144730,5239345868702306

  # 从 MySQL 分表读取视频类博文（mid_fids 非空）
  python3 tests/test_video_duration.py --from-mysql --shard-index 1 --limit 20

  # 从文件读取 mid 列表（每行一个 mid 或 mid\\tuid）
  python3 tests/test_video_duration.py --input-file mids.txt

  # 指定 uid（提高反解命中率）
  python3 tests/test_video_duration.py --mids 5333296278144730 --uid 3038801145

输出：
  - 终端：每条视频的 mid / fid / 时长 / 封面图
  - tests/output/video_duration_<timestamp>.json：完整结果
  - tests/output/video_duration_<timestamp>.tsv：TSV 格式

作者：xuanyu11
创建时间：2026-09-03
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

import yaml
import requests
from src.mid_resolver import MidResolverClient
from src.utils import setup_logger


def get_video_duration_from_showbatch(
    showbatch_api: str,
    fid: str,
    customer_id: str,
    timeout: int = 30,
    logger: logging.Logger = None,
) -> Dict[str, Any]:
    """
    通过 showBatch API 获取视频信息（含 duration）。

    Returns:
        dict: {duration, cover_url, video_url, file_size, quality, width, height}
    """
    result = {
        "duration": None,
        "cover_url": "",
        "video_url": "",
        "file_size": 0,
        "quality": "",
        "width": 0,
        "height": 0,
    }

    try:
        resp = requests.get(
            showbatch_api,
            params={"customer_id": customer_id, "fids": fid},
            headers={
                "User-Agent": "BlogClassifier/1.0",
                "Accept": "*/*",
                "Connection": "keep-alive",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == 200 and data.get("data"):
            item = data["data"][0]
            result["duration"] = item.get("duration")
            result["cover_url"] = item.get("frontUrl") or item.get("cover") or ""
            result["video_url"] = item.get("url") or item.get("mp4Url") or ""
            result["file_size"] = item.get("fileSize", 0)
            result["quality"] = item.get("quality", "")
            result["width"] = item.get("width", 0)
            result["height"] = item.get("height", 0)
            if logger:
                logger.debug(f"showBatch 成功: fid={fid} duration={result['duration']}s")
        else:
            if logger:
                logger.warning(f"showBatch 返回异常: fid={fid}, resp={data}")

    except Exception as e:
        if logger:
            logger.warning(f"showBatch 调用异常: fid={fid} - {e}")

    return result


def format_duration(seconds: Optional[float]) -> str:
    """将秒数格式化为 mm:ss 或 hh:mm:ss"""
    if seconds is None:
        return "N/A"
    s = int(seconds)
    if s < 3600:
        return f"{s // 60:02d}:{s % 60:02d}"
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def main():
    parser = argparse.ArgumentParser(
        description="视频博文时长统计",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # 输入源（三选一）
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--mids", default="", help="mid 列表，逗号分隔")
    input_group.add_argument("--input-file", default="", help="mid 列表文件（每行一个 mid 或 mid\\tuid）")
    input_group.add_argument("--from-mysql", action="store_true", help="从 MySQL 分表读取视频类博文")

    # 可选参数
    parser.add_argument("--uid", default="", help="指定 uid（提高反解命中率）")
    parser.add_argument("--shard-index", type=int, default=1, help="分表索引（--from-mysql 时使用）")
    parser.add_argument("--customer-id", type=int, default=None, help="customer_id 过滤（--from-mysql 时使用）")
    parser.add_argument("--limit", type=int, default=20, help="限制条数（--from-mysql 时使用）")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"))
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("video_duration", log_dir=os.path.join(PROJECT_DIR, "logs"))

    # 加载配置
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    resolver_cfg = config.get("mid_resolver", {})
    video_cfg = config.get("media", {}).get("video", {})
    showbatch_api = video_cfg.get("showbatch_api", "")
    default_customer_id = video_cfg.get("default_customer_id", "")

    resolver = MidResolverClient(
        url=resolver_cfg.get("url", "http://terra.biz.weibo.com/mid/media"),
        timeout=resolver_cfg.get("timeout", 30),
        max_retry=resolver_cfg.get("max_retry", 3),
        logger_=logger,
    )

    # ── 构建 mid 列表 ─────────────────────────────────────────
    mid_list: List[Dict[str, str]] = []

    if args.mids:
        for mid in args.mids.split(","):
            mid = mid.strip()
            if mid:
                mid_list.append({"mid": mid, "uid": args.uid})

    elif args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                item = {"mid": parts[0].strip()}
                if len(parts) > 1:
                    item["uid"] = parts[1].strip()
                else:
                    item["uid"] = args.uid
                mid_list.append(item)

    elif args.from_mysql:
        from src.db_client import MySQLTaskRepository
        mysql_cfg = config.get("mysql", {})
        repo = MySQLTaskRepository(mysql_cfg, logger, app_config=config)
        table_name = f"{mysql_cfg.get('shard_table_prefix', 'nature_ad_super_mid_')}{args.shard_index}"

        with repo.connect() as conn:
            # 查 mid_fids 非空的记录（视频类博文）
            sql = f"""
            SELECT mid, mid_uid, mid_fids
            FROM {table_name}
            WHERE mid_fids IS NOT NULL AND mid_fids != ''
            """
            params = []
            if args.customer_id is not None:
                sql += " AND customer_id = %s"
                params.append(args.customer_id)
            sql += f" ORDER BY id ASC LIMIT %s"
            params.append(args.limit)

            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall() or []

        for row in rows:
            mid_list.append({
                "mid": str(row.get("mid", "")),
                "uid": str(row.get("mid_uid", "")),
            })
        logger.info(f"从 {table_name} 读取到 {len(mid_list)} 条视频类博文")

    if not mid_list:
        logger.warning("没有待处理的 mid")
        sys.exit(0)

    # ── 处理每条 mid ──────────────────────────────────────────
    results: List[Dict[str, Any]] = []

    print("\n" + "=" * 90)
    print(f"{'mid':<22} {'fid':<30} {'时长':>10} {'分辨率':>12} {'大小':>10} {'画质':>8}")
    print("-" * 90)

    for i, item in enumerate(mid_list, 1):
        mid = item["mid"]
        uid = item.get("uid", "")

        logger.info(f"[{i}/{len(mid_list)}] 处理 mid={mid}")

        # Step 1: mid 反解获取 fid
        try:
            resolved = resolver.resolve(mid=mid, uid=uid or None)
            fid = resolved.video_fid
        except Exception as e:
            logger.warning(f"  反解失败 mid={mid}: {e}")
            results.append({
                "mid": mid,
                "uid": uid,
                "fid": "",
                "duration": None,
                "duration_str": "反解失败",
                "cover_url": "",
                "video_url": "",
                "file_size": 0,
                "quality": "",
                "width": 0,
                "height": 0,
                "error": str(e),
            })
            print(f"{mid:<22} {'反解失败':<30} {'N/A':>10} {'N/A':>12} {'N/A':>10} {'N/A':>8}")
            continue

        if not fid:
            logger.warning(f"  非视频博文 mid={mid}")
            results.append({
                "mid": mid,
                "uid": uid,
                "fid": "",
                "duration": None,
                "duration_str": "非视频",
                "cover_url": resolved.video_cover_url,
                "video_url": "",
                "file_size": 0,
                "quality": "",
                "width": 0,
                "height": 0,
                "error": "非视频博文",
            })
            print(f"{mid:<22} {'非视频':<30} {'N/A':>10} {'N/A':>12} {'N/A':>10} {'N/A':>8}")
            continue

        # Step 2: showBatch API 获取时长
        video_info = get_video_duration_from_showbatch(
            showbatch_api, fid, default_customer_id, logger=logger,
        )

        duration = video_info["duration"]
        duration_str = format_duration(duration)
        resolution = f"{video_info['width']}x{video_info['height']}" if video_info['width'] else "N/A"
        file_size_str = f"{video_info['file_size'] // 1024}KB" if video_info['file_size'] else "N/A"
        quality = video_info['quality'] or "N/A"

        results.append({
            "mid": mid,
            "uid": uid,
            "fid": fid,
            "duration": duration,
            "duration_str": duration_str,
            "cover_url": video_info["cover_url"],
            "video_url": video_info["video_url"],
            "file_size": video_info["file_size"],
            "quality": video_info["quality"],
            "width": video_info["width"],
            "height": video_info["height"],
            "error": "",
        })

        print(f"{mid:<22} {fid:<30} {duration_str:>10} {resolution:>12} {file_size_str:>10} {quality:>8}")

    # ── 汇总统计 ──────────────────────────────────────────────
    valid_durations = [r["duration"] for r in results if r["duration"] is not None]

    print("\n" + "=" * 90)
    print("汇总统计:")
    print(f"  总 mid 数:     {len(results)}")
    print(f"  视频博文数:    {len(valid_durations)}")
    print(f"  非视频/失败:   {len(results) - len(valid_durations)}")
    if valid_durations:
        avg_duration = sum(valid_durations) / len(valid_durations)
        max_duration = max(valid_durations)
        min_duration = min(valid_durations)
        print(f"  平均时长:      {format_duration(avg_duration)} ({avg_duration:.1f}s)")
        print(f"  最长时长:      {format_duration(max_duration)} ({max_duration:.1f}s)")
        print(f"  最短时长:      {format_duration(min_duration)} ({min_duration:.1f}s)")

        # 时长分布
        buckets = {"<15s": 0, "15-30s": 0, "30-60s": 0, "1-3min": 0, "3-5min": 0, ">5min": 0}
        for d in valid_durations:
            if d < 15:
                buckets["<15s"] += 1
            elif d < 30:
                buckets["15-30s"] += 1
            elif d < 60:
                buckets["30-60s"] += 1
            elif d < 180:
                buckets["1-3min"] += 1
            elif d < 300:
                buckets["3-5min"] += 1
            else:
                buckets[">5min"] += 1
        print("\n  时长分布:")
        for bucket, count in buckets.items():
            bar = "█" * count
            print(f"    {bucket:>8}: {count:>3} {bar}")

    print("=" * 90)

    # ── 保存结果 ──────────────────────────────────────────────
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_json = os.path.join(OUTPUT_DIR, f"video_duration_{run_ts}.json")
    output_tsv = os.path.join(OUTPUT_DIR, f"video_duration_{run_ts}.tsv")

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump({
            "run_time": datetime.now().isoformat(),
            "total": len(results),
            "video_count": len(valid_durations),
            "results": results,
        }, f, ensure_ascii=False, indent=2)

    with open(output_tsv, "w", encoding="utf-8") as f:
        f.write("mid\tuid\tfid\tduration_s\tduration_str\tresolution\tfile_size\tquality\tcover_url\terror\n")
        for r in results:
            resolution = f"{r['width']}x{r['height']}" if r['width'] else ""
            f.write(
                f"{r['mid']}\t{r['uid']}\t{r['fid']}\t"
                f"{r['duration'] or ''}\t{r['duration_str']}\t"
                f"{resolution}\t{r['file_size']}\t{r['quality']}\t"
                f"{r['cover_url']}\t{r['error']}\n"
            )

    logger.info(f"结果已保存:")
    logger.info(f"  JSON: {output_json}")
    logger.info(f"  TSV:  {output_tsv}")


if __name__ == "__main__":
    main()
