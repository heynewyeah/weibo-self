#!/usr/bin/env python3
"""
单条视频博文分类测试
====================
功能：对单条视频博文调用分类接口，验证视频处理链路。
      支持两种视频处理模式：
        - cover 模式（默认）：使用视频封面图进行多模态分类（快速，无需下载视频）
        - frame 模式：下载视频 → OpenCV 抽帧 → 多模态分类（需 opencv-python-headless）

数据来源：
  /dw_ext/ad/person/xuanyu11/intent_behavior/data/video_weibo_ad_20260701_20260701/000000_0
  （由 sql/query_video.sh 生成，字段：mid\tuid\tcontent\tmedia_id\tcustomer_info\tdt）
  若文件不存在，使用内置 fallback 样本

输出路径：
  tests/02_single/output/single_video_<timestamp>.json

运行方式：
  # cover 模式（默认）
  python3 tests/02_single/test_single_video.py

  # frame 模式（需 opencv-python-headless）
  python3 tests/02_single/test_single_video.py --video-mode frame

  # 指定样本索引
  python3 tests/02_single/test_single_video.py --index 1

  # 使用封面图 URL 直接测试（绕过 fid 查询）
  python3 tests/02_single/test_single_video.py --cover-url "http://wx3.sinaimg.cn/orj480/xxx.jpg"

  # 显示模型原始输出
  python3 tests/02_single/test_single_video.py --verbose

运行时间预估：
  - cover 模式：约 10~30 秒（封面图下载 + vLLM 推理）
  - frame 模式：约 60~180 秒（视频下载 + 抽帧 + vLLM 推理）

作者：xuanyu11
创建时间：2026-08-12
更新时间：2026-08-14（新增 frame 模式）
"""

import os
import sys
import json
import argparse
import logging
import tempfile
from datetime import datetime

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
# 数据来源：HDFS 落地路径（由 sql/query_video.sh 生成）
HDFS_DATA_PATH = "/dw_ext/ad/person/xuanyu11/intent_behavior/data/video_weibo_ad_20260701_20260701/000000_0"
FIXTURES_DIR = os.path.join(PROJECT_DIR, "tests/01_prepare_data/tmp_fixtures")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

from src.api_client import VLLMClient
from src.media_handler import VideoHandler
from src.utils import setup_logger, extract_label

# ── 内置 fallback 样本（已验证 fid 真实存在）──────────────────
FALLBACK_SAMPLES = [
    {
        "mid": "5250301234567890",
        "uid": "2608812381",
        "content": "吉利银河M9极寒测试，零下40度挑战！看看新能源旗舰SUV在极端低温下的真实表现",
        "media_type": "video",
        "fid": "2362904:4666847103221848",
        "cover": "http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg",
        "expected_layer": "兴趣层",
        "note": "产品测试视频，引发兴趣，典型兴趣层"
    },
    {
        "mid": "5250301234567891",
        "uid": "2608812381",
        "content": "比亚迪汉EV 2026款正式上市！全新外观设计，续航突破800km，售价18.98万起，限时购车享5000元补贴",
        "media_type": "video",
        "fid": "2362904:4826598285967434",
        "cover": "http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg",
        "expected_layer": "考虑层",
        "note": "新车上市+价格信息，典型考虑层"
    },
    {
        "mid": "5250301234567892",
        "uid": "2608812381",
        "content": "小鹏X9品牌TVC大片，未来已来，智能出行新纪元",
        "media_type": "video",
        "fid": "2362904:4826598285967435",
        "cover": "http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg",
        "expected_layer": "认知层",
        "note": "品牌TVC，典型认知层"
    },
]


def load_samples_from_hdfs_data(data_path: str) -> list:
    """
    从 HDFS 落地文件加载视频样本
    字段格式：mid\tuid\tcontent\tmedia_id\tcustomer_info\tdt
    """
    samples = []
    if not os.path.exists(data_path):
        return samples

    with open(data_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            mid, uid, content = parts[0], parts[1], parts[2]
            customer_info_str = parts[4] if len(parts) > 4 else ""
            dt = parts[5] if len(parts) > 5 else ""

            # 解析 customer_info JSON
            fid, cover = "", ""
            try:
                info = json.loads(customer_info_str)
                if isinstance(info, dict):
                    fid = info.get("fid", "")
                    cover = info.get("cover", "")
            except (json.JSONDecodeError, TypeError):
                pass

            if not fid:
                continue

            samples.append({
                "mid": mid,
                "uid": uid,
                "content": content,
                "media_type": "video",
                "fid": fid,
                "cover": cover,
                "dt": dt,
                "expected_layer": "未知",
                "note": f"来自 HDFS 数据 dt={dt}"
            })

    return samples


def load_samples_from_fixtures(fixtures_dir: str) -> list:
    """从 fixtures 目录加载视频样本"""
    fixtures_path = os.path.join(fixtures_dir, "video_samples.jsonl")
    if not os.path.exists(fixtures_path):
        return []

    samples = []
    with open(fixtures_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                item = json.loads(line)
                # 兼容旧格式（media_info 嵌套）
                if "media_info" in item and not item.get("fid"):
                    for mi in item.get("media_info", []):
                        if mi.get("media_type") == "2":
                            try:
                                ci = json.loads(mi.get("customer_info", "{}"))
                                item["fid"] = ci.get("fid", "")
                                item["cover"] = ci.get("cover", "")
                            except Exception:
                                pass
                samples.append(item)
    return samples


def load_samples():
    """按优先级加载样本：HDFS数据 > fixtures > fallback"""
    # 优先从 HDFS 落地数据加载
    samples = load_samples_from_hdfs_data(HDFS_DATA_PATH)
    if samples:
        return samples, f"HDFS数据: {HDFS_DATA_PATH}"

    # 其次从 fixtures 加载
    samples = load_samples_from_fixtures(FIXTURES_DIR)
    if samples:
        return samples, f"fixtures: {FIXTURES_DIR}/video_samples.jsonl"

    # 最后使用内置 fallback
    return FALLBACK_SAMPLES, "内置 fallback 样本"


def classify_with_cover(cover_url: str, content: str, mid: str,
                        config: dict, logger: logging.Logger) -> dict:
    """
    方案A（cover）：使用视频封面图进行多模态分类
    直接下载封面图 → base64 → Qwen3.6 多模态分类
    """
    import requests

    logger.info(f"  [cover 模式] 封面图: {cover_url}")

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

    api_client = VLLMClient(config["api"], logger)
    system_prompt = config["prompts"]["system_prompt"]
    user_prompt = config["prompts"]["user_video_template"].format(content=content)

    model_output = api_client.classify_with_images(system_prompt, user_prompt, [tmp_path])

    try:
        os.remove(tmp_path)
    except OSError:
        pass

    return {
        "mode": "cover",
        "cover_url": cover_url,
        "cover_downloaded": True,
        "model_output": model_output or ""
    }


def classify_with_frames(fid: str, customer_id: str, content: str, mid: str,
                         config: dict, logger: logging.Logger,
                         num_frames: int = 3) -> dict:
    """
    方案B（frame）：下载视频 → OpenCV 抽帧 → 多模态分类

    Args:
        fid: 视频 fid
        customer_id: 客户ID
        content: 博文文字内容
        mid: 博文ID（用于临时文件命名）
        config: 配置字典
        logger: 日志器
        num_frames: 抽取帧数

    Returns:
        包含 mode/frame_paths/model_output 等字段的字典
    """
    logger.info(f"  [frame 模式] fid={fid} 抽帧数={num_frames}")

    video_config = config.get("media", {}).get("video", {})
    video_config["enabled"] = True
    video_config["video_mode"] = "frame"
    video_config["extract_frames_count"] = num_frames

    handler = VideoHandler(video_config, logger)

    tmp_dir = os.path.join(tempfile.gettempdir(), f"video_frames_{mid}")
    frame_paths = handler.process_video_frames(fid, customer_id, tmp_dir)

    if not frame_paths:
        return {
            "mode": "frame",
            "frame_paths": [],
            "model_output": "",
            "error": "视频下载或抽帧失败（检查 fid 是否有效，或 opencv-python-headless 是否已安装）"
        }

    logger.info(f"  抽帧成功: {len(frame_paths)} 帧")
    for p in frame_paths:
        logger.info(f"    - {p}")

    api_client = VLLMClient(config["api"], logger)
    system_prompt = config["prompts"]["system_prompt"]
    user_prompt = config["prompts"]["user_video_template"].format(content=content)

    model_output = api_client.classify_with_video_frames(system_prompt, user_prompt, frame_paths)

    # 清理临时帧
    for p in frame_paths:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass

    return {
        "mode": "frame",
        "frame_count": len(frame_paths),
        "model_output": model_output or ""
    }


def run_test(sample: dict, config: dict, video_mode: str,
             verbose: bool, cover_url_override: str,
             num_frames: int, logger: logging.Logger) -> dict:
    """执行单条视频分类测试，返回测试结果字典"""
    mid = sample["mid"]
    uid = sample["uid"]
    content = sample["content"]
    fid = sample.get("fid", "")
    cover_url = cover_url_override or sample.get("cover", "")
    expected = sample.get("expected_layer", "未知")
    note = sample.get("note", "")

    logger.info(f"测试样本: mid={mid}")
    logger.info(f"  内容预览: {content[:80]}")
    logger.info(f"  fid: {fid}")
    logger.info(f"  封面图 URL: {cover_url}")
    logger.info(f"  视频模式: {video_mode}")
    logger.info(f"  预期结果: {expected}")
    logger.info(f"  备注: {note}")

    t_start = datetime.now()
    valid_labels = config["classification"]["layers"]

    if video_mode == "frame":
        # 方案B：抽帧模式
        if not fid:
            logger.warning("  frame 模式需要 fid，但未提供，退化为 cover 模式")
            video_mode = "cover"
        else:
            result_info = classify_with_frames(
                fid, uid, content, mid, config, logger, num_frames
            )
            model_output = result_info.get("model_output", "")
            label = extract_label(model_output, valid_labels) if model_output else None
            elapsed_ms = (datetime.now() - t_start).total_seconds() * 1000
            success = label is not None
            actual_layer = label or "未识别"
            error = result_info.get("error", "") if not success else ""
            actual_media_type = f"video_frame"

    if video_mode == "cover":
        # 方案A：封面图模式
        if not cover_url:
            # 尝试通过 showBatch API 获取封面图
            if fid:
                logger.info("  尝试通过 showBatch API 获取封面图...")
                video_config = config.get("media", {}).get("video", {})
                handler = VideoHandler(video_config, logger)
                cover_url = handler.get_cover_url(fid, uid) or ""

        if not cover_url:
            logger.warning("  无封面图 URL，退化为纯文本分类")
            from src.classifier import BlogClassifier
            classifier = BlogClassifier(config, logger)
            result = classifier.classify(mid=mid, uid=uid, content=content)
            elapsed_ms = (datetime.now() - t_start).total_seconds() * 1000
            success = result.success
            actual_layer = result.layer
            actual_media_type = result.media_type
            error = result.error
            model_output = result.model_output
        else:
            result_info = classify_with_cover(cover_url, content, mid, config, logger)
            model_output = result_info.get("model_output", "")
            label = extract_label(model_output, valid_labels) if model_output else None
            elapsed_ms = (datetime.now() - t_start).total_seconds() * 1000
            success = label is not None
            actual_layer = label or "未识别"
            error = result_info.get("error", "") if not success else ""
            actual_media_type = "video_cover"

    is_correct = (actual_layer == expected) if expected != "未知" else None

    test_result = {
        "mid": mid,
        "uid": uid,
        "content_preview": content[:100],
        "fid": fid,
        "cover_url": cover_url,
        "video_mode": video_mode,
        "expected_layer": expected,
        "actual_layer": actual_layer,
        "is_correct": is_correct,
        "success": success,
        "error": error,
        "elapsed_ms": round(elapsed_ms, 1),
        "media_type": actual_media_type,
        "note": note,
    }

    if verbose:
        test_result["model_output"] = model_output

    # 打印结果
    status_icon = "✅" if success else "❌"
    match_icon = "✅" if is_correct else ("❓" if is_correct is None else "❌")
    logger.info(
        f"  {status_icon} 分类结果: {actual_layer}  "
        f"{match_icon} 预期: {expected}  耗时: {elapsed_ms:.0f}ms"
    )
    logger.info(f"  实际媒体类型: {actual_media_type}")
    if error:
        logger.warning(f"  错误: {error}")
    if verbose and model_output:
        logger.info(f"  模型原始输出:\n{model_output}")

    return test_result


def main():
    parser = argparse.ArgumentParser(
        description="单条视频博文分类测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--index", type=int, default=0,
                        help="使用数据中第几条样本（从0开始，默认0）")
    parser.add_argument("--mid", default="", help="自定义博文ID")
    parser.add_argument("--uid", default="", help="自定义用户ID")
    parser.add_argument("--content", default="", help="自定义博文内容")
    parser.add_argument("--cover-url", default="", help="封面图 URL（直接指定，绕过 fid 查询）")
    parser.add_argument("--fid", default="", help="视频 fid（用于 showBatch API 查询）")
    parser.add_argument("--video-mode", default="cover", choices=["cover", "frame"],
                        help="视频处理模式：cover（封面图，默认）或 frame（OpenCV抽帧）")
    parser.add_argument("--frames", type=int, default=3,
                        help="frame 模式下抽取帧数（默认 3）")
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
    logger.info(f"视频模式: {args.video_mode}")
    if args.video_mode == "frame":
        logger.info(f"抽帧数量: {args.frames}")

    # ── 加载配置 ──────────────────────────────────────────────
    import yaml
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger.info(f"模型: {config['api']['model']}")
    logger.info(f"API: {config['api']['url']}")

    # ── 确定测试样本 ──────────────────────────────────────────
    if args.mid and args.content:
        sample = {
            "mid": args.mid,
            "uid": args.uid or "custom_uid",
            "content": args.content,
            "media_type": "video",
            "fid": args.fid or "",
            "cover": args.cover_url or "",
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
        sample=sample,
        config=config,
        video_mode=args.video_mode,
        verbose=args.verbose,
        cover_url_override=args.cover_url,
        num_frames=args.frames,
        logger=logger
    )

    # ── 保存结果 ──────────────────────────────────────────────
    output_file = os.path.join(OUTPUT_DIR, f"single_video_{run_ts}.json")
    output_data = {
        "test_type": "single_video",
        "run_time": datetime.now().isoformat(),
        "data_source": data_source,
        "config": {
            "model": config["api"]["model"],
            "temperature": config["api"].get("temperature", 0.0),
            "max_tokens": config["api"].get("max_tokens", 512),
            "video_mode": args.video_mode,
            "frames": args.frames if args.video_mode == "frame" else None,
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
