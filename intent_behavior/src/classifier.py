"""
核心分类器模块

负责协调整个分类流程：
1. 判断博文类型（纯文本/图文/视频）
2. 调用对应的媒体处理器获取媒体内容
3. 调用 vLLM API 进行分类
4. 提取分类结果并返回
"""

import os
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from .api_client import VLLMClient
from .media_handler import ImageHandler, VideoHandler
from .utils import extract_label, validate_input, write_error_record, write_result


@dataclass
class ClassifyResult:
    """分类结果"""
    mid: str
    uid: str
    layer: str              # 认知层 / 兴趣层 / 考虑层 / 未识别
    media_type: str         # text / image / video
    success: bool
    error: str = ""         # 失败原因
    model_output: str = ""  # 模型原始输出（调试用）


class BlogClassifier:
    """博文分类器"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        """
        Args:
            config: 完整配置字典（从config.yaml加载）
            logger: 日志器
        """
        self.config = config
        self.logger = logger

        # 初始化各模块
        self.api_client = VLLMClient(config["api"], logger)

        # 提示词
        self.prompts = config["prompts"]
        self.system_prompt = self.prompts["system_prompt"]

        # 分类配置
        self.valid_labels = config["classification"]["layers"]
        self.fallback_layer = config["classification"].get("fallback_layer", "认知层")

        # 媒体处理器
        self.image_handler = ImageHandler(config["media"]["image"], logger)
        self.video_handler = VideoHandler(config["media"]["video"], logger)

        # 输出配置
        self.error_file = config["logging"].get("error_file", "logs/error_records.tsv")
        self.result_file = config["logging"].get("result_file", "output/result.tsv")

    def detect_media_type(self, content: str,
                          image_pids: List[str] = None,
                          video_media_ids: List[str] = None) -> str:
        """
        判断博文类型

        Args:
            content: 博文文字内容
            image_pids: 图片pid列表（外部传入）
            video_media_ids: 视频media_id列表（外部传入）

        Returns:
            "text" / "image" / "video"
        """
        if video_media_ids:
            return "video"
        if image_pids:
            return "image"

        # 从内容中自动检测
        if content:
            pids = self.image_handler.extract_pids(content)
            if pids:
                return "image"

        return "text"

    def classify_text(self, mid: str, uid: str, content: str) -> ClassifyResult:
        """
        纯文本博文分类

        Args:
            mid: 博文ID
            uid: 用户ID
            content: 博文文字内容

        Returns:
            ClassifyResult 实例
        """
        user_prompt = self.prompts["user_text_template"].format(content=content)

        model_output = self.api_client.classify_text(self.system_prompt, user_prompt)

        if model_output is None:
            return ClassifyResult(
                mid=mid, uid=uid, layer="未识别", media_type="text",
                success=False, error="API调用失败"
            )

        label = extract_label(model_output, self.valid_labels)

        if label is None:
            self.logger.warning(f"标签提取失败 mid={mid}, output={model_output[:100]}")
            return ClassifyResult(
                mid=mid, uid=uid, layer="未识别", media_type="text",
                success=False, error="标签提取失败",
                model_output=model_output
            )

        return ClassifyResult(
            mid=mid, uid=uid, layer=label, media_type="text",
            success=True, model_output=model_output
        )

    def classify_image(self, mid: str, uid: str, content: str,
                       image_pids: List[str]) -> ClassifyResult:
        """
        图文博文分类

        Args:
            mid: 博文ID
            uid: 用户ID
            content: 博文文字内容
            image_pids: 图片pid列表

        Returns:
            ClassifyResult 实例
        """
        # 下载图片
        tmp_dir = f"/tmp/blog_images_{mid}"
        image_paths = self.image_handler.download_images_by_pids(image_pids, tmp_dir)

        if not image_paths:
            self.logger.warning(f"图片全部下载失败 mid={mid}，退化为纯文本分类")
            result = self.classify_text(mid, uid, content)
            result.media_type = "image_fallback_text"
            return result

        # 调用多模态API
        user_prompt = self.prompts["user_image_template"].format(content=content)
        model_output = self.api_client.classify_with_images(
            self.system_prompt, user_prompt, image_paths
        )

        # 清理临时图片
        for path in image_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

        if model_output is None:
            return ClassifyResult(
                mid=mid, uid=uid, layer="未识别", media_type="image",
                success=False, error="API调用失败"
            )

        label = extract_label(model_output, self.valid_labels)

        if label is None:
            self.logger.warning(f"标签提取失败 mid={mid}, output={model_output[:100]}")
            return ClassifyResult(
                mid=mid, uid=uid, layer="未识别", media_type="image",
                success=False, error="标签提取失败",
                model_output=model_output
            )

        return ClassifyResult(
            mid=mid, uid=uid, layer=label, media_type="image",
            success=True, model_output=model_output
        )

    def classify_video(self, mid: str, uid: str, content: str,
                       video_media_ids: List[str]) -> ClassifyResult:
        """
        视频博文分类（预留接口）

        Args:
            mid: 博文ID
            uid: 用户ID
            content: 博文文字内容
            video_media_ids: 视频media_id列表

        Returns:
            ClassifyResult 实例
        """
        if not self.video_handler.enabled:
            self.logger.info(f"视频处理未启用 mid={mid}，退化为纯文本分类")
            result = self.classify_text(mid, uid, content)
            result.media_type = "video_fallback_text"
            return result

        # TODO: 视频处理完整流程
        # 1. 获取视频URL
        # 2. 下载视频
        # 3. 抽帧
        # 4. 调用 classify_with_video_frames

        frame_paths = []
        for media_id in video_media_ids:
            frames = self.video_handler.process_video(media_id)
            frame_paths.extend(frames)

        if not frame_paths:
            self.logger.warning(f"视频抽帧失败 mid={mid}，退化为纯文本分类")
            result = self.classify_text(mid, uid, content)
            result.media_type = "video_fallback_text"
            return result

        user_prompt = self.prompts["user_video_template"].format(content=content)
        model_output = self.api_client.classify_with_video_frames(
            self.system_prompt, user_prompt, frame_paths
        )

        # 清理临时帧
        for path in frame_paths:
            try:
                os.remove(path)
            except OSError:
                pass

        if model_output is None:
            return ClassifyResult(
                mid=mid, uid=uid, layer="未识别", media_type="video",
                success=False, error="API调用失败"
            )

        label = extract_label(model_output, self.valid_labels)

        if label is None:
            return ClassifyResult(
                mid=mid, uid=uid, layer="未识别", media_type="video",
                success=False, error="标签提取失败",
                model_output=model_output
            )

        return ClassifyResult(
            mid=mid, uid=uid, layer=label, media_type="video",
            success=True, model_output=model_output
        )

    def classify(self, mid: str, uid: str, content: str = "",
                 image_pids: List[str] = None,
                 video_media_ids: List[str] = None) -> ClassifyResult:
        """
        统一分类入口：自动判断类型并路由

        Args:
            mid: 博文ID
            uid: 用户ID
            content: 博文文字内容
            image_pids: 图片pid列表（可选）
            video_media_ids: 视频media_id列表（可选）

        Returns:
            ClassifyResult 实例
        """
        # 输入校验
        is_valid, err_msg = validate_input(mid, uid, content)
        if not is_valid:
            return ClassifyResult(
                mid=mid, uid=uid, layer="未识别", media_type="unknown",
                success=False, error=f"输入校验失败: {err_msg}"
            )

        # 判断类型
        media_type = self.detect_media_type(content, image_pids, video_media_ids)
        self.logger.info(f"开始分类 mid={mid} uid={uid} type={media_type}")

        # 路由到对应处理器
        if media_type == "video":
            result = self.classify_video(mid, uid, content, video_media_ids or [])
        elif media_type == "image":
            result = self.classify_image(mid, uid, content, image_pids or [])
        else:
            result = self.classify_text(mid, uid, content)

        # 记录结果
        if result.success:
            self.logger.info(f"分类完成 mid={mid} layer={result.layer}")
            write_result(self.result_file, mid, uid, result.layer, result.media_type)
        else:
            self.logger.warning(f"分类失败 mid={mid} error={result.error}")
            write_error_record(self.error_file, mid, uid, result.media_type,
                               f"{result.error} | output={result.model_output[:200]}")

        return result

    def classify_batch(self, items: List[Dict[str, Any]]) -> List[ClassifyResult]:
        """
        批量分类

        Args:
            items: 待分类列表，每个元素为 dict:
                   {"mid": "", "uid": "", "content": "", "image_pids": [], "video_media_ids": []}

        Returns:
            ClassifyResult 列表
        """
        results = []
        total = len(items)
        batch_size = self.config.get("batch", {}).get("size", 100)
        sleep_sec = self.config.get("batch", {}).get("sleep_sec", 0.3)

        for i, item in enumerate(items, 1):
            result = self.classify(
                mid=item.get("mid", ""),
                uid=item.get("uid", ""),
                content=item.get("content", ""),
                image_pids=item.get("image_pids"),
                video_media_ids=item.get("video_media_ids")
            )
            results.append(result)

            if i % batch_size == 0 or i == total:
                success_count = sum(1 for r in results if r.success)
                fail_count = i - success_count
                self.logger.info(
                    f"进度: {i}/{total} (成功:{success_count} 失败:{fail_count})"
                )

            import time
            time.sleep(sleep_sec)

        # 统计分布
        self.logger.info("=" * 50)
        self.logger.info("分类结果统计:")
        layer_counts = {}
        for r in results:
            layer_counts[r.layer] = layer_counts.get(r.layer, 0) + 1
        for layer, count in sorted(layer_counts.items(), key=lambda x: -x[1]):
            self.logger.info(f"  {layer}: {count} 条")

        return results
