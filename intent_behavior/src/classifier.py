"""
核心分类器模块

负责协调整个分类流程：
1. 判断博文类型（纯文本/图文/视频）
2. 调用对应的媒体处理器获取媒体内容
3. 调用 vLLM API 进行分类
4. 提取分类结果并返回
5. 支持行业感知与转发异常判断
6. 超短内容 / 纯表情 / 纯话题标签 → 归为"其他"
"""

import os
import re
import time
import logging
from typing import Dict, Any, Optional, List, Tuple

from .models import BlogItem, ClassifyResult, MediaType


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CACHE_DIR = os.path.join(PROJECT_ROOT, "output", ".cache")
from .api_client import VLLMClient
from .media_handler import ImageHandler, VideoHandler
from .utils import (
    extract_label,
    extract_forward_status,
    validate_input,
    write_error_record,
    write_result,
)


logger = logging.getLogger(__name__)


class BlogClassifier:
    """博文分类器"""

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

        self.api_client = VLLMClient(config["api"], self.logger)
        self.prompts = config["prompts"]

        classification_cfg = config["classification"]
        self.supported_industries = classification_cfg.get("supported_industries", [])
        self.default_industry = classification_cfg.get("default_industry", "汽车")
        self.other_label = classification_cfg.get("other_label", "其他")
        self.failure_label = classification_cfg.get("failure_label", "未识别")
        self.industry_rules = classification_cfg.get("industry_rules", {})

        self.image_handler = ImageHandler(config["media"]["image"], self.logger)
        self.video_handler = VideoHandler(config["media"]["video"], self.logger)

        self.error_file = config["logging"].get("error_file", "logs/error_records.tsv")
        self.result_file = config["logging"].get("result_file", "output/result.tsv")

    def _resolve_industry(self, item: BlogItem) -> str:
        """
        解析行业名称。
        - 如果 item.industry_name 在支持列表中，直接返回
        - 否则返回 default_industry（兜底）
        - 如果 item.industry_name 非空但不在支持列表中，返回空字符串（表示不支持）
        """
        if item.industry_name and item.industry_name in self.supported_industries:
            return item.industry_name
        if item.industry_name and item.industry_name not in self.supported_industries:
            # 明确指定了行业但不在支持列表中 → 不支持
            return ""
        return self.default_industry

    def _get_industry_rule(self, industry_name: str) -> Dict[str, Any]:
        return self.industry_rules.get(industry_name, self.industry_rules.get(self.default_industry, {}))

    def _get_valid_labels(self, industry_name: str) -> List[str]:
        rule = self._get_industry_rule(industry_name)
        return list(rule.get("layers", []))

    def _get_keyword_map(self, industry_name: str) -> Dict[str, List[str]]:
        rule = self._get_industry_rule(industry_name)
        return dict(rule.get("keyword_map", {}))

    def _get_fallback_layer(self, industry_name: str) -> str:
        rule = self._get_industry_rule(industry_name)
        return rule.get("fallback_layer", self.other_label)

    def _format_brand_terms(self, item: BlogItem) -> str:
        return "、".join(item.brand_values) if item.brand_values else "无"

    def _build_prompt_pack(self, item: BlogItem, media_type: str) -> Tuple[str, str, str]:
        industry_name = self._resolve_industry(item)
        industry_prompts = self.prompts["industries"][industry_name]
        brand_terms = self._format_brand_terms(item)
        content = item.content or ""

        if media_type == MediaType.IMAGE:
            user_prompt = industry_prompts["user_image_template"].format(
                industry=industry_name,
                brand_terms=brand_terms,
                content=content,
            )
        elif media_type == MediaType.VIDEO:
            user_prompt = industry_prompts["user_video_template"].format(
                industry=industry_name,
                brand_terms=brand_terms,
                content=content,
            )
        else:
            user_prompt = industry_prompts["user_text_template"].format(
                industry=industry_name,
                brand_terms=brand_terms,
                content=content,
            )
        return industry_name, industry_prompts["system_prompt"], user_prompt

    def _check_forward_abnormal(self, item: BlogItem) -> Tuple[bool, str, str]:
        """
        转发异常判断：
        - 返回 (is_abnormal, status, model_output)
        - status: normal / abnormal / empty_forward / not_forward
        - 当转发原博文内容为空时，返回 empty_forward，上层归为 level=6
        """
        if not item.has_forward():
            return False, "not_forward", ""

        if not item.forward_content or not item.forward_content.strip():
            return True, "empty_forward", "被转发原博文内容为空，归为其他(level=6)"

        industry_name = self._resolve_industry(item)
        brand_terms = self._format_brand_terms(item)
        prompt = self.prompts["forward_review_prompt"].format(
            industry=industry_name,
            brand_terms=brand_terms,
            content=item.content or "",
            forward_content=item.forward_content or "",
        )
        model_output = self.api_client.classify_text("你是一个转发博文关系审查器。", prompt)
        if model_output is None:
            return False, "failed", "转发异常判断模型调用失败"

        forward_status = extract_forward_status(model_output)
        if forward_status is None:
            return False, "failed", f"转发异常判断输出无法解析: {model_output[:200]}"

        if forward_status == "异常":
            return True, "abnormal", model_output
        return False, "normal", model_output

    def _is_trivial_content(self, content: str) -> bool:
        """
        判断内容是否为"无意义内容"：
        - 有效文字 < 6 字（去除表情、话题标签、空白后）
        - 纯表情
        - 纯话题标签（如 #xxx# #yyy#）
        """
        if not content:
            return True

        # 去除话题标签 #xxx#
        text = re.sub(r'#[^#]+#', '', content)
        # 去除微博表情 [xxx]
        text = re.sub(r'\[[^\]]+\]', '', text)
        # 去除 Unicode emoji
        text = re.sub(
            r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF'
            r'\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF'
            r'\U00002702-\U000027B0\U0000FE00-\U0000FE0F'
            r'\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F'
            r'\U0001FA70-\U0001FAFF\U00002600-\U000026FF]+',
            '', text,
        )
        # 去除空白
        text = text.strip()

        if len(text) < 6:
            return True
        return False

    def detect_media_type(self, item: BlogItem) -> str:
        if item.media_ids:
            return MediaType.VIDEO
        if item.pic_ids:
            return MediaType.IMAGE

        if item.content:
            pids = self.image_handler.extract_pids(item.content)
            if pids:
                item.pic_ids = pids
                return MediaType.IMAGE

        return MediaType.TEXT

    def classify_item(self, item: BlogItem) -> ClassifyResult:
        is_valid, err_msg = validate_input(item.mid, item.uid, item.content)
        if not is_valid:
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.failure_label,
                media_type=MediaType.UNKNOWN,
                success=False,
                error=f"输入校验失败: {err_msg}",
                industry_name=item.industry_name or "",
                is_forward=item.has_forward(),
                forward_mid=item.forward_mid,
                forward_status="failed" if item.has_forward() else "not_forward",
            )

        media_type = self.detect_media_type(item)
        industry_name = self._resolve_industry(item)

        # 超短内容 / 纯表情 / 纯话题 → 归为"其他"（有效业务结果）
        if self._is_trivial_content(item.content):
            self.logger.info(
                f"内容过短或无意义 mid={item.mid}，归为其他"
            )
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.other_label,
                media_type=media_type,
                success=True,
                error="",
                model_output="内容过短或无意义（<6字/纯表情/纯话题），归为其他",
                industry_name=industry_name or item.industry_name or "",
                is_forward=item.has_forward(),
                forward_mid=item.forward_mid,
                forward_status="not_forward",
            )

        # 行业不支持时，直接返回"其他"作为有效业务结果
        if not industry_name:
            self.logger.warning(
                f"行业不支持 mid={item.mid} industry={item.industry_name}，归为其他"
            )
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.other_label,
                media_type=media_type,
                success=True,
                error="",
                model_output=f"行业不支持: {item.industry_name}",
                industry_name=item.industry_name or "",
                is_forward=item.has_forward(),
                forward_mid=item.forward_mid,
                forward_status="not_forward",
            )

        self.logger.info(
            f"开始分类 mid={item.mid} uid={item.uid} industry={industry_name} type={media_type} forward={item.has_forward()}"
        )

        try:
            is_abnormal, forward_status, forward_review_output = self._check_forward_abnormal(item)
            if forward_status == "failed":
                result = ClassifyResult(
                    mid=item.mid,
                    uid=item.uid,
                    layer=self.failure_label,
                    media_type=media_type,
                    success=False,
                    error=forward_review_output,
                    model_output=forward_review_output,
                    industry_name=industry_name,
                    is_forward=item.has_forward(),
                    forward_mid=item.forward_mid,
                    forward_status="failed",
                )
                self._persist_result(result)
                return result

            if is_abnormal:
                result = ClassifyResult(
                    mid=item.mid,
                    uid=item.uid,
                    layer=self.other_label,
                    media_type=media_type,
                    success=True,
                    model_output=forward_review_output,
                    industry_name=industry_name,
                    is_forward=True,
                    forward_mid=item.forward_mid,
                    forward_status="abnormal",
                )
                self._persist_result(result)
                return result

            if media_type == MediaType.VIDEO:
                result = self._classify_video(item)
            elif media_type == MediaType.IMAGE:
                result = self._classify_image(item)
            else:
                result = self._classify_text(item)

            result.industry_name = industry_name
            result.is_forward = item.has_forward()
            result.forward_mid = item.forward_mid
            result.forward_status = forward_status

        except Exception as e:
            self.logger.exception(f"分类异常 mid={item.mid}: {e}")
            result = ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.failure_label,
                media_type=media_type,
                success=False,
                error=f"分类异常: {str(e)}",
                industry_name=industry_name,
                is_forward=item.has_forward(),
                forward_mid=item.forward_mid,
                forward_status="failed" if item.has_forward() else "not_forward",
            )

        self._persist_result(result)
        return result

    def classify(self, mid: str, uid: str, content: str = "",
                 image_pids: List[str] = None,
                 video_media_ids: List[str] = None) -> ClassifyResult:
        item = BlogItem(
            mid=mid,
            uid=uid,
            content=content or "",
            pic_ids=image_pids or [],
            media_ids=video_media_ids or [],
            industry_name=self.default_industry,
        )
        return self.classify_item(item)

    def _classify_text(self, item: BlogItem) -> ClassifyResult:
        industry_name, system_prompt, user_prompt = self._build_prompt_pack(item, MediaType.TEXT)
        valid_labels = self._get_valid_labels(industry_name)
        keyword_map = self._get_keyword_map(industry_name)

        model_output = self.api_client.classify_text(system_prompt, user_prompt)
        if model_output is None:
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.failure_label,
                media_type=MediaType.TEXT,
                success=False,
                error="API调用失败",
                industry_name=industry_name,
            )

        label = extract_label(model_output, valid_labels, keyword_map=keyword_map)
        if label is None:
            self.logger.warning(f"标签提取失败 mid={item.mid}, output={model_output[:100]}")
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.failure_label,
                media_type=MediaType.TEXT,
                success=False,
                error="标签提取失败",
                model_output=model_output,
                industry_name=industry_name,
            )

        return ClassifyResult(
            mid=item.mid,
            uid=item.uid,
            layer=label,
            media_type=MediaType.TEXT,
            success=True,
            model_output=model_output,
            industry_name=industry_name,
        )

    def _classify_image(self, item: BlogItem) -> ClassifyResult:
        pids = item.pic_ids or []
        if not pids and item.content:
            pids = self.image_handler.extract_pids(item.content)

        if not pids:
            self.logger.warning(f"未解析到图片pid mid={item.mid}，退化为纯文本分类")
            result = self._classify_text(item)
            result.media_type = "image_fallback_text"
            return result

        tmp_dir = os.path.join(DEFAULT_CACHE_DIR, "blog_images", item.mid)
        image_paths = self.image_handler.download_images_by_pids(pids, tmp_dir)

        if not image_paths:
            self.logger.warning(f"图片全部下载失败 mid={item.mid}，退化为纯文本分类")
            result = self._classify_text(item)
            result.media_type = "image_fallback_text"
            return result

        industry_name, system_prompt, user_prompt = self._build_prompt_pack(item, MediaType.IMAGE)
        valid_labels = self._get_valid_labels(industry_name)
        keyword_map = self._get_keyword_map(industry_name)
        model_output = self.api_client.classify_with_images(system_prompt, user_prompt, image_paths)

        self._cleanup_files(image_paths, tmp_dir)

        if model_output is None:
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.failure_label,
                media_type=MediaType.IMAGE,
                success=False,
                error="API调用失败",
                industry_name=industry_name,
            )

        label = extract_label(model_output, valid_labels, keyword_map=keyword_map)
        if label is None:
            self.logger.warning(f"标签提取失败 mid={item.mid}, output={model_output[:100]}")
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.failure_label,
                media_type=MediaType.IMAGE,
                success=False,
                error="标签提取失败",
                model_output=model_output,
                industry_name=industry_name,
            )

        return ClassifyResult(
            mid=item.mid,
            uid=item.uid,
            layer=label,
            media_type=MediaType.IMAGE,
            success=True,
            model_output=model_output,
            industry_name=industry_name,
        )

    def _classify_video(self, item: BlogItem) -> ClassifyResult:
        if not self.video_handler.enabled:
            self.logger.info(f"视频处理未启用 mid={item.mid}，退化为纯文本分类")
            result = self._classify_text(item)
            result.media_type = "video_fallback_text"
            return result

        # 策略：先尝试 frame（抽帧），失败则降级为 cover（封面图）
        image_paths = []
        used_mode = "frame"
        self.logger.info(f"视频处理: 先尝试 frame 模式, mid={item.mid}")

        for media_id in (item.media_ids or []):
            paths = self.video_handler.process_video(media_id, mode="frame")
            image_paths.extend(paths)

        if not image_paths:
            self.logger.warning(
                f"frame 模式获取失败 mid={item.mid}，降级为 cover 模式"
            )
            for media_id in (item.media_ids or []):
                paths = self.video_handler.process_video(media_id, mode="cover")
                image_paths.extend(paths)
            used_mode = "cover"

        if not image_paths:
            self.logger.warning(
                f"视频图片获取失败(frame+cover均失败) mid={item.mid}，退化为纯文本分类"
            )
            result = self._classify_text(item)
            result.media_type = "video_fallback_text"
            return result

        industry_name, system_prompt, user_prompt = self._build_prompt_pack(item, MediaType.VIDEO)
        valid_labels = self._get_valid_labels(industry_name)
        keyword_map = self._get_keyword_map(industry_name)
        model_output = self.api_client.classify_with_video_frames(system_prompt, user_prompt, image_paths)

        self._cleanup_files(image_paths)

        if model_output is None:
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.failure_label,
                media_type=MediaType.VIDEO,
                success=False,
                error="API调用失败",
                industry_name=industry_name,
            )

        label = extract_label(model_output, valid_labels, keyword_map=keyword_map)
        if label is None:
            return ClassifyResult(
                mid=item.mid,
                uid=item.uid,
                layer=self.failure_label,
                media_type=MediaType.VIDEO,
                success=False,
                error="标签提取失败",
                model_output=model_output,
                industry_name=industry_name,
            )

        actual_media_type = f"video_{used_mode}"
        return ClassifyResult(
            mid=item.mid,
            uid=item.uid,
            layer=label,
            media_type=actual_media_type,
            success=True,
            model_output=model_output,
            industry_name=industry_name,
        )

    def _persist_result(self, result: ClassifyResult):
        if result.success:
            self.logger.info(
                f"分类完成 mid={result.mid} industry={result.industry_name} layer={result.layer} "
                f"forward_status={result.forward_status}"
            )
            write_result(self.result_file, result.mid, result.uid, result.layer, result.media_type)
        else:
            self.logger.warning(f"分类失败 mid={result.mid} error={result.error}")
            write_error_record(
                self.error_file,
                result.mid,
                result.uid,
                result.media_type,
                f"{result.error} | output={result.model_output[:200]}",
            )

    @staticmethod
    def _cleanup_files(paths: List[str], dir_path: Optional[str] = None):
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
                self.logger.info(f"进度: {i}/{total} (成功:{success_count} 失败:{fail_count})")

            if sleep_sec > 0:
                time.sleep(sleep_sec)

        self.logger.info("=" * 50)
        self.logger.info("分类结果统计:")
        layer_counts = {}
        for r in results:
            layer_counts[r.layer] = layer_counts.get(r.layer, 0) + 1
        for layer, count in sorted(layer_counts.items(), key=lambda x: -x[1]):
            self.logger.info(f"  {layer}: {count} 条")

        return results
