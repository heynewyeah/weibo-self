#!/usr/bin/env python3
"""
单条视频博文分类测试
====================
功能：对单条视频博文调用分类接口，验证视频处理链路。
      当前支持两种模式：
        - cover 模式（默认）：使用视频封面图进行多模态分类（方案A，已验证可用）
        - fallback 模式：视频处理未启用时退化为纯文本分类

      视频完整链路（方案B，需 enabled: true）：
        fid → showBatch API → 视频URL → 下载 → 抽帧 → 多模态分类

数据来源：
  tests/01_prepare_data/fixtures/video_samples.jsonl
  （若文件不存在，使用内置 fallback 样本）

输出路径：
  tests/02_single/output/single_video_<timestamp>.json

运行方式：
  # 使用默认样本（第一条，视频处理按 config.yaml 配置）
  python3 tests/02_single/test_single_video.py

  # 指定样本索引
  python3 tests/02_single/test_single_video.py --index 1

  # 使用封面图 URL 直接测试（绕过 fid 查询）
  python3 tests/02_single/test_single_video.py --cover-url "http://wx3.sinaimg.cn/orj480/xxx.jpg"

  # 显示模型原始输出
  python3 tests/02_single/test_single_video.py --verbose

运行时间预估：
  - cover 模式：约 5~20 秒（封面图下载 + vLLM 推理）
  - fallback 文本模式：约 2~10 秒

作者：xuanyu11
创建时间：2026-08-12
"""

import os
import sys
import json
import argparse
import logging
import requests
import tempfile
from datetime import datetime

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
FIXTURES_DIR = os.path.join(PROJECT_DIR, "tests/01_prepare_data/fixtures")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.classifier import BlogClassifier
from src.api_client import VLLMClient
from src.utils import setup_logger

# ── 内置 fallback 样本（已验证 fid 真实存在）──────────────────
FALLBACK_SAMPLES = [
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
        "expected_layer": "认知层",
        "note": "品牌TVC，典型认知层"
    },
]


def extract_video_info(media_info: list) -> dict:
    """从 media_info 中提取视频信息（cover/fid/url）"""
    if not media_info:
        return {}
    for item in media_info:
        if item.get("media_type") == "2":
            customer_info = item.get("customer_info", "")
            try:
                info = json.loads(customer_info)
                if isinstance(info, dict):
                    return info
            except (json.JSONDecodeError, TypeError):
                pass
    return {}


def classify_with_cover(cover_url: str, content: str, mid: str, uid: str,
                         config: dict, logger: logging.Logger) -> dict:
    """
    方案A：使用视频封面图进行多模态分类
    直接下载封面图 → base64 → Qwen3.6 多模态分类
    """
    logger.info(f"  [方案A] 使用封面图分类: {cover_url}")

    # 下载封面图
    tmp_path = os.path.join(tempfile.gettempdir(), f"cover_{mid}.jpg")
    try:
        resp = requests.get(cover_url, timeout=30, stream=True)
        resp.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        file_size = os.path.getsize(tmp_path)
        logger.info(f"  封面图下载成功: {file_size} bytes")
    except Exception as e:
        logger.warning(f"  封面图下载失败: {e}，退化为纯文本分类")
        return {"mode": "cover_fallback_text", "cover_downloaded": False, "error": str(e)}

    # 调用多模态 API
    api_client = VLLMClient(config["api"], logger)
    system_prompt = config["prompts"]["system_prompt"]
    user_prompt = config["prompts"]["user_video_template"].format(content=content)

    model_output = api_client.classify_with_images(system_prompt, user_prompt, [tmp_path])

    # 清理临时文件
    try:
        os.remove(tmp_path)
    except OSError:
        pass

    return {
        "mode": "cover_image",
        "cover_url": cover_url,
        "cover_downloaded": True,
        "model_output": model_output or ""
    }


def load_samples():
    """从 fixtures 加载视频样本，不存在则用 fallback"""
    fixtures_path = os.path.join(FIXTURES_DIR, "video_samples.jsonl")
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


def run_test(sample: dict, classifier: BlogClassifier, config: dict,
             verbose: bool, cover_url_override: str,
             logger: logging.Logger) -> dict:
    """执行单条视频分类测试，返回测试结果字典"""
    mid = sample["mid"]
    uid = sample["uid"]
    content = sample["content"]
    media_info = sample.get("media_info") or []
    expected = sample.get("expected_layer", "未知")
    note = sample.get("note", "")

    # 提取视频信息
    video_info = extract_video_info(media_info)
    cover_url = cover_url_override or video_info.get("cover", "")
    fid = video_info.get("fid", "")

    logger.info(f"测试样本: mid={mid}")
    logger.info(f"  内容预览: {content[:80]}")
    logger.info(f"  fid: {fid}")
    logger.info(f"  封面图 URL: {cover_url}")
    logger.info(f"  预期结果: {expected}")
    logger.info(f"  备注: {note}")

    video_enabled = config.get("media", {}).get("video", {}).get("enabled", False)
    logger.info(f"  视频处理启用: {video_enabled}")

    t_start = datetime.now()

    # 判断处理模式
    if cover_url and not video_enabled:
        # 方案A：封面图多模态分类（video.enabled=false 时手动走封面图路径）
        logger.info("  处理模式: 封面图多模态分类（方案A）")
        cover_result = classify_with_cover(cover_url, content, mid, uid, config, logger)

        from src.utils import extract_label
        valid_labels = config["classification"]["layers"]
        model_output = cover_result.get("model_output", "")
        label = extract_label(model_output, valid_labels) if model_output else None

        elapsed_ms = (datetime.now() - t_start).total_seconds() * 1000
        success = label is not None
        actual_layer = label or "未识别"
        actual_media_type = cover_result.get("mode", "video_cover")
        error = cover_result.get("error", "") if not success else ""

    else:
        # 走 classifier 标准流程（video.enabled=true 时走完整视频链路，否则退化文本）
        logger.info("  处理模式: 标准分类器流程")
        result = classifier.classify(mid=mid, uid=uid, content=content)
        elapsed_ms = (datetime.now() - t_start).total_seconds() * 1000
        success = result.success
        actual_layer = result.layer
        actual_media_type = result.media_type
        error = result.error
        model_output = result.model_output

    is_correct = (actual_layer == expected) if expected != "未知" else None

    test_result = {
        "mid": mid,
        "uid": uid,
        "content_preview": content[:100],
        "fid": fid,
        "cover_url": cover_url,
        "expected_layer": expected,
        "actual_layer": actual_layer,
        "is_correct": is_correct,
        "success": success,
        "error": error,
        "elapsed_ms": round(elapsed_ms, 1),
        "media_type": actual_media_type,
        "video_enabled": video_enabled,
        "note": note,
    }

    if verbose:
        test_result["model_output"] = model_output if "model_output" in dir() else ""

    # 打印结果
    status_icon = "✅" if success else "❌"
    match_icon = "✅" if is_correct else ("❓" if is_correct is None else "❌")
    logger.info(f"  {status_icon} 分类结果: {actual_layer}  {match_icon} 预期: {expected}  耗时: {elapsed_ms:.0f}ms")
    logger.info(f"  实际媒体类型: {actual_media_type}")
    if error:
        logger.warning(f"  错误: {error}")

    return test_result


def main():
    parser = argparse.ArgumentParser(
        description="单条视频博文分类测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--index", type=int, default=0,
                        help="使用 fixtures 中第几条样本（从0开始，默认0）")
    parser.add_argument("--mid", default="", help="自定义博文ID")
    parser.add_argument("--uid", default="", help="自定义用户ID")
    parser.add_argument("--content", default="", help="自定义博文内容")
    parser.add_argument("--cover-url", default="", help="封面图 URL（直接指定，绕过 fid 查询）")
    parser.add_argument("--fid", default="", help="视频 fid（用于 showBatch API 查询）")
    parser.add_argument("--verbose", action="store_true", help="显示模型原始输出")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    args = parser.parse_args()

    # ── 初始化 ────────────────────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("test_single_video", log_dir=os.path.join(PROJECT_DIR, "logs"))

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger.info("=" * 60)
    logger.info("单条视频博文分类测试")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"配置文件: {args.config}")

    # ── 加载配置和分类器 ──────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    video_enabled = config.get("media", {}).get("video", {}).get("enabled", False)
    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"thinking 模式: {config['api'].get('enable_thinking', False)}")
    logger.info(f"视频处理启用: {video_enabled}")
    if not video_enabled:
        logger.info("  → 视频未启用，将使用封面图方案A（若有 cover URL）或退化为文本分类")
    classifier = BlogClassifier(config, logger)

    # ── 确定测试样本 ──────────────────────────────────────────
    if args.mid and args.content:
        fid_str = args.fid or ""
        cover_url = args.cover_url or ""
        customer_info = json.dumps({
            "cover": cover_url,
            "fid": fid_str,
            "url": f"https://video.weibo.com/show?fid={fid_str}"
        })
        sample = {
            "mid": args.mid,
            "uid": args.uid or "custom_uid",
            "content": args.content,
            "media_type": "video",
            "media_info": [{"media_type": "2", "customer_info": customer_info}],
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
    test_result = run_test(
        sample, classifier, config,
        args.verbose, args.cover_url, logger
    )

    # ── 保存结果 ──────────────────────────────────────────────
    output_file = os.path.join(OUTPUT_DIR, f"single_video_{run_ts}.json")
    output_data = {
        "test_type": "single_video",
        "run_time": datetime.now().isoformat(),
        "data_source": data_source,
        "config": {
            "model": config["api"]["model"],
            "enable_thinking": config["api"].get("enable_thinking", False),
            "temperature": config["api"].get("temperature", 0.0),
            "video_enabled": video_enabled,
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
