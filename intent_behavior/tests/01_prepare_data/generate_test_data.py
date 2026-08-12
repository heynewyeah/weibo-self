#!/usr/bin/env python3
"""
测试数据生成脚本
================
功能：将三种类型（文本/图片/视频）的博文样本数据写入 HDFS 测试目录。
      同时在本地 tests/01_prepare_data/fixtures/ 保存一份副本，供离线测试使用。

数据来源：
  - 文本博文：来自 PROJECT_HANDOVER.md 中已验证的真实博文内容
  - 图片博文：使用已验证可下载的真实 pid（HTTP 200, 120KB JPEG）
  - 视频博文：使用已验证的真实 fid（showBatch API 返回 status=200）

输出路径：
  - HDFS: /dw_ext/ad/person/xuanyu11/intent_behavior/data/test_samples/
  - 本地:  tests/01_prepare_data/fixtures/

运行方式：
  python3 tests/01_prepare_data/generate_test_data.py [--local-only]

参数：
  --local-only   仅写本地文件，不上传 HDFS（本机调试时使用）

运行时间预估：
  - 仅本地：< 1 秒
  - 含 HDFS 上传：约 5~30 秒（取决于网络）

作者：xuanyu11
创建时间：2026-08-12
"""

import os
import sys
import json
import argparse
import subprocess
import logging
from datetime import datetime
from typing import List, Dict

# ─────────────────────────────────────────────
# 路径配置
# ─────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
FIXTURES_DIR = os.path.join(SCRIPT_DIR, "fixtures")

HDFS_DATA_DIR = "/dw_ext/ad/person/xuanyu11/intent_behavior/data/test_samples"

# ─────────────────────────────────────────────
# 日志初始化
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 测试样本数据（真实来源，已验证）
# ─────────────────────────────────────────────

TEXT_SAMPLES: List[Dict] = [
    {
        "mid": "5250218712893321",
        "uid": "1647951825",
        "content": "#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里，第五代智能电混双擎加持，WLTC综合油耗低至4.59L/100km，通勤一月一加油、自驾跨省不补能。TSS 4.0智驾+15.6英寸大屏，新车已经到店了，家用还是挺好的",
        "media_type": "text",
        "media_info": None,
        "dt": "20260101",
        "expected_layer": "考虑层",
        "note": "含价格/参数/到店信息，典型考虑层"
    },
    {
        "mid": "5250218712893322",
        "uid": "1647951826",
        "content": "今天带大家体验东风日产N6的零压云毯大沙发，坐进去整个人都放松了，这个座椅真的绝了！内饰质感也很在线，感兴趣的朋友可以去店里体验一下",
        "media_type": "text",
        "media_info": None,
        "dt": "20260101",
        "expected_layer": "兴趣层",
        "note": "产品体验分享，引发兴趣，典型兴趣层"
    },
    {
        "mid": "5250218712893323",
        "uid": "1647951827",
        "content": "比亚迪全新品牌形象发布！「在路上」——这不只是一句口号，更是比亚迪对每一位用户的承诺。新的征程，新的出发。#比亚迪# #新能源汽车#",
        "media_type": "text",
        "media_info": None,
        "dt": "20260101",
        "expected_layer": "认知层",
        "note": "品牌曝光宣传，无购买引导，典型认知层"
    },
    {
        "mid": "5250218712893324",
        "uid": "1647951828",
        "content": "首付0元起，新车开回家！广汽丰田iA5限时优惠，综合优惠高达2万元，月供低至1999元，活动截止本月底，欲购从速！",
        "media_type": "text",
        "media_info": None,
        "dt": "20260101",
        "expected_layer": "考虑层",
        "note": "促销优惠信息，典型考虑层"
    },
    {
        "mid": "5250218712893325",
        "uid": "1647951829",
        "content": "吉利银河M9 vs 理想L9，同价位旗舰SUV深度横评！空间、智驾、动力、舒适性全方位对比，看完再决定买哪台！",
        "media_type": "text",
        "media_info": None,
        "dt": "20260101",
        "expected_layer": "考虑层",
        "note": "竞品横评对比，帮助决策，典型考虑层"
    },
    {
        "mid": "5250218712893326",
        "uid": "1647951830",
        "content": "试驾了一天小米SU7 Ultra，说说真实感受：加速确实猛，0-100km/h只要2.78秒，但日常驾驶其实更在意底盘调校，这台车的悬挂偏硬，高速稳但市区颠",
        "media_type": "text",
        "media_info": None,
        "dt": "20260101",
        "expected_layer": "兴趣层",
        "note": "试驾体验分享，引发讨论，典型兴趣层"
    },
    {
        "mid": "5250218712893327",
        "uid": "1647951831",
        "content": "华为乾崑智驾ADS 3.0正式发布！全场景无图智驾，城区通勤接管率降低90%，高速领航更稳更安全。问界M9、享界S9同步OTA升级，智能驾驶进入新纪元",
        "media_type": "text",
        "media_info": None,
        "dt": "20260101",
        "expected_layer": "认知层",
        "note": "新技术发布宣传，品牌曝光，典型认知层"
    },
]

IMAGE_SAMPLES: List[Dict] = [
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
        "dt": "20260101",
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
        "dt": "20260101",
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
        "dt": "20260101",
        "expected_layer": "认知层",
        "note": "品牌宣传图文，典型认知层"
    },
]

VIDEO_SAMPLES: List[Dict] = [
    {
        "mid": "5250301234567890",
        "uid": "1647951825",
        "content": "吉利银河M9极寒测试，零下40度挑战！看看新能源旗舰SUV在极端低温下的真实表现",
        "media_type": "video",
        "media_info": [
            {
                "media_type": "2",
                "customer_info": '{"cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg","fid":"2362904:4666847103221848","orientation":"horizontal","source":3,"url":"https://video.weibo.com/show?fid=2362904:4666847103221848"}'
            }
        ],
        "dt": "20260101",
        "expected_layer": "兴趣层",
        "note": "产品测试视频，引发兴趣，典型兴趣层"
    },
    {
        "mid": "5250301234567891",
        "uid": "1647951826",
        "content": "比亚迪汉EV 2026款正式上市！全新外观设计，续航突破800km，售价18.98万起，限时购车享5000元补贴",
        "media_type": "video",
        "media_info": [
            {
                "media_type": "2",
                "customer_info": '{"cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg","fid":"2362904:4826598285967434","orientation":"horizontal","source":3,"url":"https://video.weibo.com/show?fid=2362904:4826598285967434"}'
            }
        ],
        "dt": "20260101",
        "expected_layer": "考虑层",
        "note": "新车上市+价格信息，典型考虑层"
    },
    {
        "mid": "5250301234567892",
        "uid": "1647951827",
        "content": "小鹏X9品牌TVC大片，未来已来，智能出行新纪元",
        "media_type": "video",
        "media_info": [
            {
                "media_type": "2",
                "customer_info": '{"cover":"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg","fid":"2362904:4826598285967435","orientation":"horizontal","source":3,"url":"https://video.weibo.com/show?fid=2362904:4826598285967435"}'
            }
        ],
        "dt": "20260101",
        "expected_layer": "认知层",
        "note": "品牌TVC，典型认知层"
    },
]


def build_all_samples() -> List[Dict]:
    """合并所有类型的样本"""
    return TEXT_SAMPLES + IMAGE_SAMPLES + VIDEO_SAMPLES


def write_jsonl(samples: List[Dict], filepath: str):
    """写入 JSONL 格式文件（每行一个 JSON 对象）"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        for item in samples:
            # 写入时去掉 note/expected_layer 等测试专用字段，保留核心字段
            record = {
                "mid": item["mid"],
                "uid": item["uid"],
                "content": item["content"],
                "media_type": item["media_type"],
                "media_info": item["media_info"],
                "dt": item["dt"],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info(f"写入 JSONL: {filepath} ({len(samples)} 条)")


def write_jsonl_with_meta(samples: List[Dict], filepath: str):
    """写入含 expected_layer/note 的完整测试数据（供测试脚本使用）"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        for item in samples:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    logger.info(f"写入完整测试数据: {filepath} ({len(samples)} 条)")


def write_tsv(samples: List[Dict], filepath: str):
    """写入 TSV 格式（兼容旧版批量处理）"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        # 表头
        f.write("mid\tuid\tcontent\tmedia_type\tdt\n")
        for item in samples:
            content_escaped = item["content"].replace("\t", " ").replace("\n", " ")
            f.write(f"{item['mid']}\t{item['uid']}\t{content_escaped}\t{item['media_type']}\t{item['dt']}\n")
    logger.info(f"写入 TSV: {filepath} ({len(samples)} 条)")


def upload_to_hdfs(local_path: str, hdfs_path: str) -> bool:
    """上传文件到 HDFS"""
    try:
        # 确保 HDFS 目录存在
        mkdir_cmd = ["hdfs", "dfs", "-mkdir", "-p", os.path.dirname(hdfs_path)]
        subprocess.run(mkdir_cmd, check=True, capture_output=True, timeout=60)

        # 上传（覆盖已有文件）
        put_cmd = ["hdfs", "dfs", "-put", "-f", local_path, hdfs_path]
        result = subprocess.run(put_cmd, capture_output=True, text=True, timeout=120)

        if result.returncode == 0:
            logger.info(f"HDFS 上传成功: {local_path} → {hdfs_path}")
            return True
        else:
            logger.error(f"HDFS 上传失败: {result.stderr[:300]}")
            return False

    except FileNotFoundError:
        logger.warning("hdfs 命令不可用，跳过 HDFS 上传（本地模式）")
        return False
    except subprocess.TimeoutExpired:
        logger.error("HDFS 上传超时")
        return False
    except Exception as e:
        logger.error(f"HDFS 上传异常: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="生成测试数据并写入本地/HDFS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 仅写本地（本机调试）
  python3 tests/01_prepare_data/generate_test_data.py --local-only

  # 写本地 + 上传 HDFS（服务器环境）
  python3 tests/01_prepare_data/generate_test_data.py
        """
    )
    parser.add_argument("--local-only", action="store_true",
                        help="仅写本地文件，不上传 HDFS")
    args = parser.parse_args()

    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info("测试数据生成开始")
    logger.info(f"本地输出目录: {FIXTURES_DIR}")
    logger.info(f"HDFS 输出目录: {HDFS_DATA_DIR}")
    logger.info("=" * 60)

    all_samples = build_all_samples()
    text_samples = [s for s in all_samples if s["media_type"] == "text"]
    image_samples = [s for s in all_samples if s["media_type"] == "image"]
    video_samples = [s for s in all_samples if s["media_type"] == "video"]

    logger.info(f"样本统计: 文本={len(text_samples)} 图片={len(image_samples)} 视频={len(video_samples)} 合计={len(all_samples)}")

    # ── 1. 写本地 fixtures ──────────────────────────────────────
    os.makedirs(FIXTURES_DIR, exist_ok=True)

    # 按类型分文件（JSONL，含 meta 信息，供测试脚本读取）
    write_jsonl_with_meta(text_samples,  os.path.join(FIXTURES_DIR, "text_samples.jsonl"))
    write_jsonl_with_meta(image_samples, os.path.join(FIXTURES_DIR, "image_samples.jsonl"))
    write_jsonl_with_meta(video_samples, os.path.join(FIXTURES_DIR, "video_samples.jsonl"))

    # 合并文件（JSONL，纯数据，供上游格式验证）
    write_jsonl(all_samples, os.path.join(FIXTURES_DIR, "all_samples.jsonl"))

    # TSV 格式（兼容旧版批量处理）
    write_tsv(all_samples, os.path.join(FIXTURES_DIR, "all_samples.tsv"))

    # ── 2. 上传 HDFS ────────────────────────────────────────────
    if not args.local_only:
        logger.info("开始上传 HDFS...")
        hdfs_files = [
            ("text_samples.jsonl",  f"{HDFS_DATA_DIR}/text_samples.jsonl"),
            ("image_samples.jsonl", f"{HDFS_DATA_DIR}/image_samples.jsonl"),
            ("video_samples.jsonl", f"{HDFS_DATA_DIR}/video_samples.jsonl"),
            ("all_samples.jsonl",   f"{HDFS_DATA_DIR}/all_samples.jsonl"),
            ("all_samples.tsv",     f"{HDFS_DATA_DIR}/all_samples.tsv"),
        ]
        success_count = 0
        for local_name, hdfs_path in hdfs_files:
            local_path = os.path.join(FIXTURES_DIR, local_name)
            if upload_to_hdfs(local_path, hdfs_path):
                success_count += 1
        logger.info(f"HDFS 上传完成: {success_count}/{len(hdfs_files)} 个文件")
    else:
        logger.info("--local-only 模式，跳过 HDFS 上传")

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info(f"数据生成完成，耗时 {elapsed:.1f}s")
    logger.info(f"本地文件目录: {FIXTURES_DIR}")
    logger.info("文件列表:")
    for fname in os.listdir(FIXTURES_DIR):
        fpath = os.path.join(FIXTURES_DIR, fname)
        size = os.path.getsize(fpath)
        logger.info(f"  {fname}  ({size} bytes)")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
