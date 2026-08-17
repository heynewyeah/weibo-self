"""
核心分类器模块

负责协调整个分类流程：
1. 判断博文类型（纯文本/图文/视频）
2. 调用对应的媒体处理器获取媒体内容
3. 调用 vLLM API 进行分类
4. 提取分类结果并返回
"""

import os
import time
import logging
from typing import Dict, Any, Optional, List

from .models import BlogItem, ClassifyResult, MediaType


# 项目根目录与默认缓存目录
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CACHE_DIR = os.path.join(PROJECT_ROOT, "output", ".cache")
from .api_client import VLLMClient
from .media_handler import ImageHandler, VideoHandler
from .utils import extract_label, validate_input, write_error_record, write_result


logger = logging.getLogger(__name__)


class BlogClassifier:
    """博文分类器"""

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        """
        Args:
            config: 完整配置字典（从config.yaml加载）
            logger: 日志器
        """
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

        # 初始化各模块
        self.api_client = VLLMClient(config["api"], self.logger)

        # 提示词
        self.prompts = config["prompts"]
        self.system_prompt = self.prompts["system_prompt"]

        # 分类配置
        self.valid_labels = config["classification"]["layers"]
        self.fallback_layer = config["classification"].get("fallback_layer", "认知层")

        # 媒体处理器
        self.image_handler = ImageHandler(config["media"]["image"], self.logger)
        self.video_handler = VideoHandler(config["media"]["video"], self.logger)

        # 输出配置
        self.error_file = config["logging"].get("error_file", "logs/error_records.tsv")
        self.result_file = config["logging"].get("result_file", "output/result.tsv")

    def detect_media_type(self, item: BlogItem) -> str:
        """
        判断博文类型

        优先级：video > image > text
        """
        if item.media_ids:
            return MediaType.VIDEO
        if item.pic_ids:
            return MediaType.IMAGE

        # 从内容中自动检测图片 pid
        if item.content:
            pids = self.image_handler.extract_pids(item.content)
            if pids:
                item.pic_ids = pids  # 回填，避免重复解析
                return MediaType.IMAGE

        return MediaType.TEXT

    def classify_item(self, item: BlogItem) -> ClassifyResult:
        """
        对 BlogItem 进行分类（推荐使用）

        Args:
            item: BlogItem 实例

        Returns:
            ClassifyResult 实例
        """
        # 输入校验
        is_valid, err_msg = validate_input(item.mid, item.uid, item.content)
        if not is_valid:
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer="未识别",
                media_type=MediaType.UNKNOWN,
                success=False,
                error=f"输入校验失败: {err_msg}"
            )

        # 判断类型
        media_type = self.detect_media_type(item)
        self.logger.info(f"开始分类 mid={item.mid} uid={item.uid} type={media_type}")

        try:
            if media_type == MediaType.VIDEO:
                result = self._classify_video(item)
            elif media_type == MediaType.IMAGE:
                result = self._classify_image(item)
            else:
                result = self._classify_text(item)
        except Exception as e:
            self.logger.exception(f"分类异常 mid={item.mid}: {e}")
            result = ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer="未识别",
                media_type=media_type,
                success=False,
                error=f"分类异常: {str(e)}"
            )

        # 记录结果
        self._persist_result(result)
        return result

    def classify(self, mid: str, uid: str, content: str = "",
                 image_pids: List[str] = None,
                 video_media_ids: List[str] = None) -> ClassifyResult:
        """
        统一分类入口（兼容旧版直接传参方式）

        Args:
            mid: 博文ID
            uid: 用户ID
            content: 博文文字内容
            image_pids: 图片pid列表（可选）
            video_media_ids: 视频media_id列表（可选）

        Returns:
            ClassifyResult 实例
        """
        item = BlogItem(
            mid=mid,
            uid=uid,
            content=content or "",
            pic_ids=image_pids or [],
            media_ids=video_media_ids or []
        )
        return self.classify_item(item)

    def _classify_text(self, item: BlogItem) -> ClassifyResult:
        """纯文本博文分类"""
        user_prompt = self.prompts["user_text_template"].format(content=item.content)

        model_output = self.api_client.classify_text(self.system_prompt, user_prompt)

        if model_output is None:
            return ClassifyResult(
                mid=item.mid, uid=item.uid, layer="未识别", media_type=MediaType.TEXT,
                success=False, error="API调用失败"
            )

        label = extract_label(model_output, self.valid_labels)

        if label is None:
            self.logger.warning(f"标签提取失败 mid={item.mid}, output={model_output[:100]}")
            return ClassifyResult(
                mid=item.mid, uid=item.uid, layer="未识别", media_type=MediaType.TEXT,
                success=False, error="标签提取失败",
                model_output=model_output
            )

        return ClassifyResult(
            mid=item.mid, uid=item.uid, layer=label, media_type=MediaType.TEXT,
            success=True, model_output=model_output
        )

    def _classify_image(self, item: BlogItem) -> ClassifyResult:
        """图文博文分类"""
        pids = item.pic_ids or []
        if not pids and item.content:
            pids = self.image_handler.extract_pids(item.content)

        if not pids:
            # 没有图片，退化为纯文本
            self.logger.warning(f"未解析到图片pid mid={item.mid}，退化为纯文本分类")
            result = self._classify_text(item)
            result.media_type = "image_fallback_text"
            return result

        # 下载图片
        tmp_dir = os.path.join(DEFAULT_CACHE_DIR, "blog_images", item.mid)
        image_paths = self.image_handler.download_images_by_pids(pids, tmp_dir)

        if not image_paths:
            self.logger.warning(f"图片全部下载失败 mid={item.mid}，退化为纯文本分类")
            result = self._classify_text(item)
            result.media_type = "image_fallback_text"
            return result

        # 调用多模态API
        user_prompt = self.prompts["user_image_template"].format(content=item.content)
        model_output = self.api_client.classify_with_images(
            self.system_prompt, user_prompt, image_paths
        )

        # 清理临时图片
        self._cleanup_files(image_paths, tmp_dir)

        if model_output is None:
            return ClassifyResult(
                mid=item.mid, uid=item.uid, layer="未识别", media_type=MediaType.IMAGE,
                success=False, error="API调用失败"
            )

        label = extract_label(model_output, self.valid_labels)

        if label is None:
            self.logger.warning(f"标签提取失败 mid={item.mid}, output={model_output[:100]}")
            return ClassifyResult(
                mid=item.mid, uid=item.uid, layer="未识别", media_type=MediaType.IMAGE,
                success=False, error="标签提取失败",
                model_output=model_output
            )

        return ClassifyResult(
            mid=item.mid, uid=item.uid, layer=label, media_type=MediaType.IMAGE,
            success=True, model_output=model_output
        )

    def _classify_video(self, item: BlogItem) -> ClassifyResult:
        """
        视频博文分类

        支持两种模式（由 config.media.video.video_mode 控制）：
          - cover：使用封面图进行多模态分类（快速，无需下载视频）
          - frame：下载视频并用 OpenCV 抽帧进行多模态分类

        若视频处理未启用或图片获取失败，退化为纯文本分类。
        """
        if not self.video_handler.enabled:
            self.logger.info(f"视频处理未启用 mid={item.mid}，退化为纯文本分类")
            result = self._classify_text(item)
            result.media_type = "video_fallback_text"
            return result

        video_mode = self.video_handler.video_mode
        self.logger.info(f"视频处理模式: {video_mode}, mid={item.mid}")

        image_paths = []
        for media_id in (item.media_ids or []):
            paths = self.video_handler.process_video(media_id)
            image_paths.extend(paths)

        if not image_paths:
            self.logger.warning(
                f"视频图片获取失败(mode={video_mode}) mid={item.mid}，退化为纯文本分类"
            )
            result = self._classify_text(item)
            result.media_type = "video_fallback_text"
            return result

        # 多模态分类（封面图或抽帧图片，接口相同）
        user_prompt = self.prompts["user_video_template"].format(content=item.content)
        model_output = self.api_client.classify_with_video_frames(
            self.system_prompt, user_prompt, image_paths
        )

        # 清理临时图片
        self._cleanup_files(image_paths)

        if model_output is None:
            return ClassifyResult(
                mid=item.mid, uid=item.uid, layer="未识别", media_type=MediaType.VIDEO,
                success=False, error="API调用失败"
            )

        label = extract_label(model_output, self.valid_labels)

        if label is None:
            return ClassifyResult(
                mid=item.mid, uid=item.uid, layer="未识别", media_type=MediaType.VIDEO,
                success=False, error="标签提取失败",
                model_output=model_output
            )

        actual_media_type = f"video_{video_mode}"
        return ClassifyResult(
            mid=item.mid, uid=item.uid, layer=label, media_type=actual_media_type,
            success=True, model_output=model_output
        )

    def _persist_result(self, result: ClassifyResult):
        """持久化分类结果（成功/失败分别记录）"""
        if result.success:
            self.logger.info(f"分类完成 mid={result.mid} layer={result.layer}")
            write_result(self.result_file, result.mid, result.uid,
                         result.layer, result.media_type)
        else:
            self.logger.warning(f"分类失败 mid={result.mid} error={result.error}")
            write_error_record(self.error_file, result.mid, result.uid,
                               result.media_type,
                               f"{result.error} | output={result.model_output[:200]}")

    @staticmethod
    def _cleanup_files(paths: List[str], dir_path: Optional[str] = None):
        """清理临时文件"""
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass
        if dir_path:
            try:
                os.rmdir(dir_path)
            except OSError:
                pass

    def classify_batch(self, items: List[BlogItem]) -> List[ClassifyResult]:
        """
        批量分类

        Args:
            items: BlogItem 列表

        Returns:
            ClassifyResult 列表
        """
        results = []
        total = len(items)
        batch_size = self.config.get("batch", {}).get("size", 100)
        sleep_sec = self.config.get("batch", {}).get("sleep_sec", 0.3)

        for i, item in enumerate(items, 1):
            result = self.classify_item(item)
            results.append(result)

            if i % batch_size == 0 or i == total:
                success_count = sum(1 for r in results if r.success)
                fail_count = i - success_count
                self.logger.info(
                    f"进度: {i}/{total} (成功:{success_count} 失败:{fail_count})"
                )

            if sleep_sec > 0:
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
