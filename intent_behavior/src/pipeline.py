#!/usr/bin/env python3
"""
正式分类 Pipeline
=================
核心生产入口，封装单条/批量博文的完整分类流程：

1. mid 反解（通过反解接口拿真实 content / pid / fid）
2. 媒体类型判定（text / image / video / auto）
3. 合并 MySQL 任务上下文（行业 / 品牌词 / 转发信息）
4. 调用分类器
5. 临时文件清理
6. 结果回写（可选，HTTP 接口 POST /api/v1/super-mid/update-level）
7. 结构化计时与错误记录
"""

from __future__ import annotations

import os
import glob
import shutil
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import time

from .classifier import BlogClassifier
from .db_client import MySQLTaskRepository, MidRecord
from .mid_resolver import MidResolverClient, ResolvedBlog


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CACHE_DIR = os.path.join(PROJECT_ROOT, "output", ".cache")
DEFAULT_LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
RESOLVE_FAIL_LOG = os.path.join(DEFAULT_LOG_DIR, "反解失败汇总.txt")
RESOLVE_FAIL_HEADER = "插入时间\tmid\tuid\tcustomer_id\tsuper_task_id\tindustry\t重试次数\t错误信息\n"


@dataclass
class ProcessTimings:
    resolve_ms: float = 0.0
    classify_ms: float = 0.0
    cleanup_ms: float = 0.0
    writeback_ms: float = 0.0
    total_ms: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "resolve_ms": round(self.resolve_ms, 1),
            "classify_ms": round(self.classify_ms, 1),
            "cleanup_ms": round(self.cleanup_ms, 1),
            "writeback_ms": round(self.writeback_ms, 1),
            "total_ms": round(self.total_ms, 1),
        }


@dataclass
class ProcessResult:
    mid: str
    uid: str
    mode: str
    content_preview: str = ""
    pic_ids: List[str] = field(default_factory=list)
    video_fid: str = ""
    video_cover_url: str = ""
    layer: str = "未识别"
    media_type: str = "unknown"
    success: bool = False
    error: str = ""
    error_stage: str = ""
    model_output: str = ""
    industry_name: str = ""
    is_forward: bool = False
    forward_mid: str = ""
    forward_content: str = ""
    forward_status: str = "not_forward"
    hit_mid_tag: str = ""
    resolved: Optional[ResolvedBlog] = None
    timings: ProcessTimings = field(default_factory=ProcessTimings)
    write_back: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mid": self.mid,
            "uid": self.uid,
            "mode": self.mode,
            "content_preview": self.content_preview,
            "pic_ids": self.pic_ids,
            "video_fid": self.video_fid,
            "video_cover_url": self.video_cover_url,
            "layer": self.layer,
            "media_type": self.media_type,
            "success": self.success,
            "error": self.error,
            "error_stage": self.error_stage,
            "model_output": self.model_output,
            "industry_name": self.industry_name,
            "is_forward": self.is_forward,
            "forward_mid": self.forward_mid,
            "forward_content": self.forward_content,
            "forward_status": self.forward_status,
            "hit_mid_tag": self.hit_mid_tag,
            "timings": self.timings.to_dict(),
            "write_back": self.write_back,
        }


class ClassifyPipeline:
    """正式分类 Pipeline"""

    def __init__(
        self,
        config: Dict[str, Any],
        logger: Optional[logging.Logger] = None,
        error_log_dir: str = DEFAULT_LOG_DIR,
    ):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.error_log_dir = error_log_dir

        resolver_cfg = config.get("mid_resolver", {})
        self.resolver = MidResolverClient(
            url=resolver_cfg.get("url", "http://terra.biz.weibo.com/mid/media"),
            timeout=resolver_cfg.get("timeout", 30),
            max_retry=resolver_cfg.get("max_retry", 3),
            logger_=self.logger,
        )

        self.classifier = BlogClassifier(config, self.logger)

        self.repo: Optional[MySQLTaskRepository] = None
        mysql_cfg = config.get("mysql")
        if mysql_cfg:
            self.repo = MySQLTaskRepository(mysql_cfg, self.logger, app_config=config)

    def process_one(
        self,
        mid: str,
        uid: Optional[str] = None,
        mode: str = "auto",
        write_back: bool = False,
        record: Optional[MidRecord] = None,
    ) -> ProcessResult:
        t_total_start = time.perf_counter()
        result = ProcessResult(mid=mid, uid=uid or "", mode=mode, write_back=write_back)

        # 从 record 中提取 hit_mid_tag 和转发信息
        if record is not None:
            result.hit_mid_tag = record.hit_mid_tag or ""
            result.forward_mid = record.forward_mid or ""
            result.forward_content = record.forward_text or ""

        try:
            t_resolve_start = time.perf_counter()
            try:
                resolved = self.resolver.resolve(
                    mid=mid,
                    uid=uid or None,
                    parse_component=self.config.get("mid_resolver", {}).get("parse_component", 1),
                )
            except Exception as e:
                result.error = f"反解失败: {str(e)}"
                result.error_stage = "resolve"
                # 反解失败 → 写入反解失败汇总 + 归为其他(level=6) + 标记失败
                self._write_resolve_fail_log(result, record)
                result.layer = self.classifier.other_label
                result.success = False
                result.industry_name = record.task_industry_name if record else ""
                result.forward_status = "not_forward"
                # 尝试回写 level=6
                if write_back and record is not None and self.repo is not None:
                    try:
                        from .models import ClassifyResult as CR
                        fake_result = CR(
                            mid=mid, uid=uid or "", layer=self.classifier.other_label,
                            media_type="unknown", success=True,
                            industry_name=result.industry_name,
                        )
                        self.repo.update_level_result(None, record, fake_result)
                    except Exception as wb_e:
                        self.logger.warning(f"反解失败回写 level=6 也失败 mid={mid}: {wb_e}")
                result.timings.resolve_ms = (time.perf_counter() - t_resolve_start) * 1000
                result.timings.total_ms = (time.perf_counter() - t_total_start) * 1000
                self._log_result(result)
                return result

            result.timings.resolve_ms = (time.perf_counter() - t_resolve_start) * 1000
            result.resolved = resolved
            result.uid = resolved.uid or uid or ""
            result.content_preview = (resolved.content or "")[:200]
            result.pic_ids = list(resolved.pic_ids)
            result.video_fid = resolved.video_fid
            result.video_cover_url = resolved.video_cover_url

            t_classify_start = time.perf_counter()
            try:
                item = resolved.to_blog_item()

                if record is not None:
                    item.industry_name = record.task_industry_name
                    item.brand_values = list(record.task_brand_values)
                    item.forward_mid = str(record.forward_mid or "")
                    item.forward_content = record.forward_text or ""
                    item.extra.update({
                        "row_id": record.id,
                        "customer_id": record.customer_id,
                        "super_task_id": record.super_task_id,
                        "source": "mysql_shard",
                        "has_forward": record.has_forward(),
                    })

                if mode == "text":
                    item.pic_ids = []
                    item.media_ids = []
                elif mode == "image":
                    item.media_ids = []
                    if not resolved.has_image():
                        raise RuntimeError("强制 image 模式但反解结果无图片 pid")
                elif mode == "video":
                    item.pic_ids = []
                    if not resolved.has_video():
                        raise RuntimeError("强制 video 模式但反解结果无视频 fid")

                classify_result = self.classifier.classify_item(item)

            except Exception as e:
                result.error = f"分类失败: {str(e)}"
                result.error_stage = "classify"
                raise

            result.timings.classify_ms = (time.perf_counter() - t_classify_start) * 1000
            result.layer = classify_result.layer
            result.media_type = classify_result.media_type
            result.success = classify_result.success
            result.error = classify_result.error
            result.model_output = classify_result.model_output
            result.industry_name = classify_result.industry_name
            result.is_forward = classify_result.is_forward
            result.forward_mid = classify_result.forward_mid
            result.forward_status = classify_result.forward_status

            if not classify_result.success:
                result.error_stage = "classify"
                raise RuntimeError(classify_result.error or "分类器返回失败")

            if write_back and record is not None and self.repo is not None:
                t_writeback_start = time.perf_counter()
                try:
                    self.repo.update_level_result(None, record, classify_result)
                except Exception as e:
                    result.error = f"回写失败: {str(e)}"
                    result.error_stage = "writeback"
                    result.success = False
                    raise
                result.timings.writeback_ms = (time.perf_counter() - t_writeback_start) * 1000

        except Exception:
            result.success = False
            if not result.error:
                result.error = traceback.format_exc()
            if not result.error_stage:
                result.error_stage = "unknown"

        finally:
            t_cleanup_start = time.perf_counter()
            self._cleanup_temp_files(mid, result.video_fid)
            result.timings.cleanup_ms = (time.perf_counter() - t_cleanup_start) * 1000
            result.timings.total_ms = (time.perf_counter() - t_total_start) * 1000

            if not result.success:
                self._write_error_log(result)

            self._log_result(result)

        return result

    def process_batch(
        self,
        inputs: List[Dict[str, Any]],
        mode: str = "auto",
        write_back: bool = False,
        workers: int = 1,
    ) -> List[ProcessResult]:
        if workers > 1 and write_back:
            raise ValueError("并发模式暂不支持结果回写，请 workers=1 或 write_back=False")

        results: List[ProcessResult] = []
        total = len(inputs)

        if workers <= 1:
            for i, inp in enumerate(inputs, 1):
                self.logger.info(f"[{i}/{total}] 开始处理 mid={inp.get('mid')}")
                res = self.process_one(
                    mid=inp["mid"],
                    uid=inp.get("uid"),
                    mode=mode,
                    write_back=write_back,
                    record=inp.get("record"),
                )
                results.append(res)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _worker(inp: Dict[str, Any]) -> ProcessResult:
                pipeline = ClassifyPipeline(self.config, self.logger, self.error_log_dir)
                return pipeline.process_one(
                    mid=inp["mid"],
                    uid=inp.get("uid"),
                    mode=mode,
                    write_back=False,
                    record=inp.get("record"),
                )

            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_input = {executor.submit(_worker, inp): inp for inp in inputs}
                completed = 0
                for future in as_completed(future_to_input):
                    results.append(future.result())
                    completed += 1
                    if completed % max(1, total // 10) == 0 or completed == total:
                        self.logger.info(f"批量进度: {completed}/{total}")

        mid_order = {inp["mid"]: idx for idx, inp in enumerate(inputs)}
        results.sort(key=lambda r: mid_order.get(r.mid, 0))
        return results

    def _cleanup_temp_files(self, mid: str, video_fid: str = ""):
        """只清理当次任务产生的临时文件，不做 glob 兜底。"""
        cleaned = []

        # 1. 图片临时目录（以 mid 命名的子目录）
        img_dir = os.path.join(DEFAULT_CACHE_DIR, "blog_images", mid)
        if os.path.exists(img_dir):
            try:
                shutil.rmtree(img_dir)
                cleaned.append(img_dir)
            except Exception as e:
                self.logger.warning(f"清理图片目录失败 {img_dir}: {e}")

        # 2. 视频相关文件（以 fid 命名的文件）
        if video_fid:
            safe_fid = video_fid.replace(":", "_")

            cover_path = os.path.join(DEFAULT_CACHE_DIR, "video_covers", f"cover_{safe_fid}.jpg")
            if os.path.exists(cover_path):
                try:
                    os.remove(cover_path)
                    cleaned.append(cover_path)
                except Exception as e:
                    self.logger.warning(f"清理视频封面失败 {cover_path}: {e}")

            video_path = os.path.join(DEFAULT_CACHE_DIR, "video_frames", f"video_{safe_fid}.mp4")
            if os.path.exists(video_path):
                try:
                    os.remove(video_path)
                    cleaned.append(video_path)
                except Exception as e:
                    self.logger.warning(f"清理视频文件失败 {video_path}: {e}")

            frames_dir = os.path.join(DEFAULT_CACHE_DIR, "video_frames", f"frames_{safe_fid}")
            if os.path.exists(frames_dir):
                try:
                    shutil.rmtree(frames_dir)
                    cleaned.append(frames_dir)
                except Exception as e:
                    self.logger.warning(f"清理视频帧目录失败 {frames_dir}: {e}")

        if cleaned:
            self.logger.debug(f"清理临时文件: {cleaned}")

    def _write_error_log(self, result: ProcessResult):
        os.makedirs(self.error_log_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        error_log_path = os.path.join(self.error_log_dir, f"{date_str}_error.log")

        line = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t"
            f"{result.mid}\t"
            f"{result.uid}\t"
            f"{result.mode}\t"
            f"{result.industry_name}\t"
            f"{result.forward_status}\t"
            f"{result.error_stage}\t"
            f"{result.error.replace(chr(9), ' ').replace(chr(10), ' ')}\t"
            f"{','.join(result.pic_ids)}\t"
            f"{result.video_fid}\t"
            f"{result.video_cover_url}\t"
            f"{result.content_preview[:100]}\n"
        )

        try:
            with open(error_log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            self.logger.error(f"写入错误日志失败: {e}")

    def _write_resolve_fail_log(self, result: ProcessResult, record: Optional[MidRecord] = None):
        """
        反解失败时写入反解失败汇总.txt。
        单文件追加模式，首次写入时自动添加表头。
        表头：插入时间 / mid / uid / customer_id / super_task_id / industry / 重试次数 / 错误信息
        """
        os.makedirs(self.error_log_dir, exist_ok=True)

        # 计算重试次数：检查文件中该 mid 已出现多少次
        retry_count = 1
        try:
            if os.path.exists(RESOLVE_FAIL_LOG):
                with open(RESOLVE_FAIL_LOG, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.split("\t")
                        if len(parts) >= 2 and parts[1] == result.mid:
                            retry_count += 1
        except Exception:
            pass

        # 如果文件不存在或为空，先写表头
        need_header = not os.path.exists(RESOLVE_FAIL_LOG) or os.path.getsize(RESOLVE_FAIL_LOG) == 0

        line = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t"
            f"{result.mid}\t"
            f"{result.uid}\t"
            f"{record.customer_id if record else ''}\t"
            f"{record.super_task_id if record else ''}\t"
            f"{record.task_industry_name if record else ''}\t"
            f"{retry_count}\t"
            f"{result.error.replace(chr(9), ' ').replace(chr(10), ' ')}\n"
        )
        try:
            with open(RESOLVE_FAIL_LOG, "a", encoding="utf-8") as f:
                if need_header:
                    f.write(RESOLVE_FAIL_HEADER)
                f.write(line)
        except Exception as e:
            self.logger.error(f"写入反解失败汇总失败: {e}")

    def _log_result(self, result: ProcessResult):
        t = result.timings
        status = "✅ 成功" if result.success else "❌ 失败"

        lines = [
            "=" * 60,
            f"处理结果 [{status}] mid={result.mid} uid={result.uid}",
            f"  模式: {result.mode}",
            f"  行业: {result.industry_name}",
            f"  hit_mid_tag: {result.hit_mid_tag}",
            f"  是否转发: {result.is_forward}",
            f"  原博文mid(forward_mid): {result.forward_mid}",
            f"  原博文内容: {(result.forward_content or '')[:120]}",
            f"  转发判定: {result.forward_status}",
            f"  媒体类型: {result.media_type}",
            f"  分类层级: {result.layer}",
            f"  正文预览: {result.content_preview[:120]}",
            f"  图片 pid: {result.pic_ids}",
            f"  视频 fid: {result.video_fid}",
            f"  视频封面: {result.video_cover_url}",
            f"  耗时: 反解={t.resolve_ms:.0f}ms 分类={t.classify_ms:.0f}ms 清理={t.cleanup_ms:.0f}ms 回写={t.writeback_ms:.0f}ms 总计={t.total_ms:.0f}ms",
        ]
        if not result.success:
            lines.append(f"  失败阶段: {result.error_stage}")
            lines.append(f"  错误信息: {result.error[:300]}")
        lines.append("=" * 60)

        for line in lines:
            if result.success:
                self.logger.info(line)
            else:
                self.logger.warning(line)
