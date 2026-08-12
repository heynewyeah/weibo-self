#!/usr/bin/env python3
"""
批量博文分类测试
================
功能：对多条博文（文本/图片/视频混合）批量调用分类接口，统计分类分布、成功率、耗时。

数据来源：
  优先级（从高到低）：
  1. --input 指定的 JSONL 文件
  2. tests/01_prepare_data/fixtures/all_samples.jsonl
  3. 内置 fallback 样本（13条，覆盖三种类型）

输出路径：
  tests/03_batch/output/batch_<timestamp>.json    — 完整结果（含每条详情）
  tests/03_batch/output/batch_<timestamp>.tsv     — TSV 格式（mid/uid/layer/media_type）
  tests/03_batch/output/batch_<timestamp>_summary.txt — 统计摘要

运行方式：
  # 使用 fixtures 数据（全部样本）
  python3 tests/03_batch/test_batch.py

  # 指定 JSONL 输入文件
  python3 tests/03_batch/test_batch.py --input /path/to/data.jsonl

  # 限制处理条数（调试用）
  python3 tests/03_batch/test_batch.py --limit 5

  # 显示每条模型原始输出
  python3 tests/03_batch/test_batch.py --verbose

运行时间预估：
  - 13条样本（含图片/视频）：约 1~3 分钟
  - 100条纯文本：约 3~10 分钟

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

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
FIXTURES_DIR = os.path.join(PROJECT_DIR, "tests/01_prepare_data/fixtures")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.classifier import BlogClassifier
from src.models import BlogItem
from src.utils import setup_logger, extract_label


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
    """加载 TSV 文件（兼容旧格式）"""
    items = []
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # 检测是否有表头
    has_header = lines and lines[0].strip().startswith("mid")
    data_lines = lines[1:] if has_header else lines

    for line in data_lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        item = {
            "mid": parts[0] if len(parts) > 0 else "",
            "uid": parts[1] if len(parts) > 1 else "",
            "content": parts[2] if len(parts) > 2 else "",
            "media_type": parts[3] if len(parts) > 3 else "text",
            "media_info": None,
        }
        items.append(item)
    return items


def jsonl_item_to_blog_item(item: Dict) -> BlogItem:
    """将 JSONL 数据项转换为 BlogItem"""
    mid = str(item.get("mid", ""))
    uid = str(item.get("uid", ""))
    content = item.get("content", "") or ""
    media_info = item.get("media_info") or []
    media_type_str = item.get("media_type", "text")

    pic_ids = []
    media_ids = []

    if isinstance(media_info, list):
        for m in media_info:
            mt = str(m.get("media_type", ""))
            customer_info = m.get("customer_info", "")
            if mt == "1":
                # 图片：解析 pid 列表
                try:
                    pid_list = json.loads(customer_info)
                    if isinstance(pid_list, list):
                        pic_ids.extend(pid_list)
                except (json.JSONDecodeError, TypeError):
                    pass
            elif mt == "2":
                # 视频：提取 fid
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


def load_data(input_path: Optional[str], limit: Optional[int], logger: logging.Logger):
    """加载测试数据，返回 (items_raw, data_source)"""
    if input_path:
        if not os.path.exists(input_path):
            logger.error(f"输入文件不存在: {input_path}")
            sys.exit(1)
        if input_path.endswith(".jsonl") or input_path.endswith(".json"):
            raw = load_jsonl(input_path)
        else:
            raw = load_tsv(input_path)
        data_source = input_path
    else:
        fixtures_path = os.path.join(FIXTURES_DIR, "all_samples.jsonl")
        if os.path.exists(fixtures_path):
            raw = load_jsonl(fixtures_path)
            data_source = fixtures_path
        else:
            # 内置 fallback
            raw = _get_fallback_samples()
            data_source = "内置 fallback 样本"

    if limit and limit > 0:
        raw = raw[:limit]
        logger.info(f"--limit {limit}，截取前 {len(raw)} 条")

    return raw, data_source


def _get_fallback_samples() -> List[Dict]:
    """内置 fallback 样本（覆盖三种类型）"""
    return [
        {"mid": "5250218712893321", "uid": "1647951825", "content": "#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里，第五代智能电混双擎加持，WLTC综合油耗低至4.59L/100km，通勤一月一加油、自驾跨省不补能。TSS 4.0智驾+15.6英寸大屏，新车已经到店了，家用还是挺好的", "media_type": "text", "media_info": None, "expected_layer": "考虑层"},
        {"mid": "5250218712893322", "uid": "1647951826", "content": "今天带大家体验东风日产N6的零压云毯大沙发，坐进去整个人都放松了，这个座椅真的绝了！内饰质感也很在线，感兴趣的朋友可以去店里体验一下", "media_type": "text", "media_info": None, "expected_layer": "兴趣层"},
        {"mid": "5250218712893323", "uid": "1647951827", "content": "比亚迪全新品牌形象发布！「在路上」——这不只是一句口号，更是比亚迪对每一位用户的承诺。新的征程，新的出发。#比亚迪# #新能源汽车#", "media_type": "text", "media_info": None, "expected_layer": "认知层"},
        {"mid": "5250218712893324", "uid": "1647951828", "content": "首付0元起，新车开回家！广汽丰田iA5限时优惠，综合优惠高达2万元，月供低至1999元，活动截止本月底，欲购从速！", "media_type": "text", "media_info": None, "expected_layer": "考虑层"},
        {"mid": "5250218712893325", "uid": "1647951829", "content": "吉利银河M9 vs 理想L9，同价位旗舰SUV深度横评！空间、智驾、动力、舒适性全方位对比，看完再决定买哪台！", "media_type": "text", "media_info": None, "expected_layer": "考虑层"},
        {"mid": "5250218712893326", "uid": "1647951830", "content": "试驾了一天小米SU7 Ultra，说说真实感受：加速确实猛，0-100km/h只要2.78秒，但日常驾驶其实更在意底盘调校，这台车的悬挂偏硬，高速稳但市区颠", "media_type": "text", "media_info": None, "expected_layer": "兴趣层"},
        {"mid": "5250218712893327", "uid": "1647951831", "content": "华为乾崑智驾ADS 3.0正式发布！全场景无图智驾，城区通勤接管率降低90%，高速领航更稳更安全。问界M9、享界S9同步OTA升级，智能驾驶进入新纪元", "media_type": "text", "media_info": None, "expected_layer": "认知层"},
        {"mid": "5250292767523625", "uid": "1647951825", "content": "比亚迪宋L实拍来了！外观绝了，这个颜色真的太好看了，内饰也很精致，大家觉得怎么样？", "media_type": "image", "media_info": [{"media_type": "1", "customer_info": '["6239bfd1ly1glk3gl3bqfj20gg08843c"]'}], "expected_layer": "兴趣层"},
        {"mid": "5250292767523626", "uid": "1647951826", "content": "威兰达3天免费用车券限时抽取！参与话题活动#威兰达1212宠粉计划#，转发本微博即可参与抽奖，中奖率超高！", "media_type": "image", "media_info": [{"media_type": "1", "customer_info": '["6239bfd1ly1glk49t82q0j20gg0880x6"]'}], "expected_layer": "考虑层"},
        {"mid": "5250292767523627", "uid": "1647951827", "content": "理想L9官方宣传大片，六座旗舰SUV，家的感觉", "media_type": "image", "media_info": [{"media_type": "1", "customer_info": '["62e00111ly1fvco1lvodmj20gg088gol"]'}], "expected_layer": "认知层"},
        {"mid": "5250301234567890", "uid": "1647951825", "content": "吉利银河M9极寒测试，零下40度挑战！看看新能源旗舰SUV在极端低温下的真实表现", "media_type": "video", "media_info": [{"media_type": "2", "customer_info": '{"cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg","fid":"2362904:4666847103221848","url":"https://video.weibo.com/show?fid=2362904:4666847103221848"}'}], "expected_layer": "兴趣层"},
        {"mid": "5250301234567891", "uid": "1647951826", "content": "比亚迪汉EV 2026款正式上市！全新外观设计，续航突破800km，售价18.98万起，限时购车享5000元补贴", "media_type": "video", "media_info": [{"media_type": "2", "customer_info": '{"cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg","fid":"2362904:4826598285967434","url":"https://video.weibo.com/show?fid=2362904:4826598285967434"}'}], "expected_layer": "考虑层"},
        {"mid": "5250301234567892", "uid": "1647951827", "content": "小鹏X9品牌TVC大片，未来已来，智能出行新纪元", "media_type": "video", "media_info": [{"media_type": "2", "customer_info": '{"cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg","fid":"2362904:4826598285967435","url":"https://video.weibo.com/show?fid=2362904:4826598285967435"}'}], "expected_layer": "认知层"},
    ]


def print_progress(current: int, total: int, success: int, fail: int, elapsed_s: float):
    """打印进度条"""
    pct = current / total * 100
    bar_len = 30
    filled = int(bar_len * current / total)
    bar = "█" * filled + "░" * (bar_len - filled)
    eta = (elapsed_s / current * (total - current)) if current > 0 else 0
    print(f"\r  [{bar}] {current}/{total} ({pct:.0f}%)  ✅{success} ❌{fail}  "
          f"已用:{elapsed_s:.0f}s  预计剩余:{eta:.0f}s", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="批量博文分类测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--input", default="", help="输入 JSONL/TSV 文件路径（不指定则用 fixtures）")
    parser.add_argument("--limit", type=int, default=0, help="限制处理条数（0=不限制）")
    parser.add_argument("--verbose", action="store_true", help="在结果文件中保存模型原始输出")
    parser.add_argument("--sleep", type=float, default=0.3, help="每条请求间隔秒数（默认0.3）")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_batch", log_dir=os.path.join(PROJECT_DIR, "logs"))

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("=" * 60)
    logger.info("批量博文分类测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")

    # ── 加载配置和分类器 ──────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"thinking 模式: {config['api'].get('enable_thinking', False)}")
    classifier = BlogClassifier(config, logger)

    # ── 加载数据 ──────────────────────────────────────────────
    raw_items, data_source = load_data(
        args.input or None,
        args.limit or None,
        logger
    )
    logger.info(f"数据来源: {data_source}")
    logger.info(f"待处理条数: {len(raw_items)}")

    # 统计输入类型分布
    type_dist = {}
    for item in raw_items:
        mt = item.get("media_type", "text")
        type_dist[mt] = type_dist.get(mt, 0) + 1
    logger.info(f"输入类型分布: {type_dist}")
    logger.info("=" * 60)

    # ── 批量分类 ──────────────────────────────────────────────
    results = []
    total = len(raw_items)
    success_count = 0
    fail_count = 0
    batch_start = datetime.now()

    for i, raw in enumerate(raw_items, 1):
        item = jsonl_item_to_blog_item(raw)
        expected = raw.get("expected_layer", "未知")

        t0 = datetime.now()
        result = classifier.classify_item(item)
        elapsed_ms = (datetime.now() - t0).total_seconds() * 1000

        if result.success:
            success_count += 1
        else:
            fail_count += 1

        is_correct = (result.layer == expected) if expected != "未知" else None

        record = {
            "index": i,
            "mid": result.mid,
            "uid": result.uid,
            "content_preview": raw.get("content", "")[:80],
            "input_media_type": raw.get("media_type", "text"),
            "actual_media_type": result.media_type,
            "expected_layer": expected,
            "actual_layer": result.layer,
            "is_correct": is_correct,
            "success": result.success,
            "error": result.error,
            "elapsed_ms": round(elapsed_ms, 1),
        }
        if args.verbose:
            record["model_output"] = result.model_output

        results.append(record)

        # 进度显示
        elapsed_s = (datetime.now() - batch_start).total_seconds()
        print_progress(i, total, success_count, fail_count, elapsed_s)

        if args.sleep > 0:
            time.sleep(args.sleep)

    print()  # 换行
    total_elapsed = (datetime.now() - batch_start).total_seconds()

    # ── 统计分析 ──────────────────────────────────────────────
    layer_dist = {}
    correct_count = 0
    has_expected = 0

    for r in results:
        layer_dist[r["actual_layer"]] = layer_dist.get(r["actual_layer"], 0) + 1
        if r["is_correct"] is not None:
            has_expected += 1
            if r["is_correct"]:
                correct_count += 1

    accuracy = correct_count / has_expected if has_expected > 0 else None
    avg_elapsed = sum(r["elapsed_ms"] for r in results) / len(results) if results else 0

    # ── 打印摘要 ──────────────────────────────────────────────
    summary_lines = [
        "=" * 60,
        "批量分类测试结果摘要",
        "=" * 60,
        f"运行时间:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"数据来源:     {data_source}",
        f"总条数:       {total}",
        f"成功:         {success_count}  ({success_count/total*100:.1f}%)",
        f"失败:         {fail_count}  ({fail_count/total*100:.1f}%)",
        f"总耗时:       {total_elapsed:.1f}s",
        f"平均耗时:     {avg_elapsed:.0f}ms/条",
        "",
        "分类结果分布:",
    ]
    for layer, cnt in sorted(layer_dist.items(), key=lambda x: -x[1]):
        pct = cnt / total * 100
        summary_lines.append(f"  {layer}: {cnt} 条 ({pct:.1f}%)")

    if accuracy is not None:
        summary_lines.append("")
        summary_lines.append(f"准确率（有预期标签的 {has_expected} 条）: {accuracy*100:.1f}%")

    summary_lines.append("=" * 60)

    for line in summary_lines:
        logger.info(line)

    # ── 保存结果 ──────────────────────────────────────────────
    # 1. 完整 JSON 结果
    output_json = os.path.join(OUTPUT_DIR, f"batch_{run_ts}.json")
    output_data = {
        "test_type": "batch",
        "run_time": datetime.now().isoformat(),
        "data_source": data_source,
        "config": {
            "model": config["api"]["model"],
            "enable_thinking": config["api"].get("enable_thinking", False),
            "temperature": config["api"].get("temperature", 0.0),
        },
        "summary": {
            "total": total,
            "success": success_count,
            "fail": fail_count,
            "total_elapsed_s": round(total_elapsed, 1),
            "avg_elapsed_ms": round(avg_elapsed, 1),
            "layer_distribution": layer_dist,
            "accuracy": round(accuracy, 4) if accuracy is not None else None,
        },
        "results": results
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    # 2. TSV 格式（mid/uid/layer/media_type/success）
    output_tsv = os.path.join(OUTPUT_DIR, f"batch_{run_ts}.tsv")
    with open(output_tsv, "w", encoding="utf-8") as f:
        f.write("mid\tuid\tlayer\tmedia_type\tsuccess\terror\n")
        for r in results:
            f.write(f"{r['mid']}\t{r['uid']}\t{r['actual_layer']}\t{r['actual_media_type']}\t{r['success']}\t{r['error']}\n")

    # 3. 摘要文本
    output_summary = os.path.join(OUTPUT_DIR, f"batch_{run_ts}_summary.txt")
    with open(output_summary, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines) + "\n")

    logger.info(f"结果文件:")
    logger.info(f"  JSON:    {output_json}")
    logger.info(f"  TSV:     {output_tsv}")
    logger.info(f"  摘要:    {output_summary}")

    # 失败率超过 20% 时非零退出
    if fail_count / total > 0.2:
        logger.warning(f"失败率 {fail_count/total*100:.1f}% 超过 20%，退出码 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
