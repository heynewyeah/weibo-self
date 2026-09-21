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
from .audit import RunAudit
from .utils import local_file_writes_allowed


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CACHE_DIR = os.path.join(PROJECT_ROOT, "output", ".cache")
DEFAULT_LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
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
    task_id: Optional[int] = None
    customer_id: Optional[int] = None
    short_url: str = ""
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
    source_industry_name: str = ""
    industry_refine_note: str = ""
    is_forward: bool = False
    forward_mid: str = ""
    forward_content: str = ""
    forward_status: str = "not_forward"
    hit_mid_tag: str = ""
    hit_brand_name: str = ""
    brand_terms: List[str] = field(default_factory=list)
    topic_terms: List[str] = field(default_factory=list)
    resolved: Optional[ResolvedBlog] = None
    timings: ProcessTimings = field(default_factory=ProcessTimings)
    write_back: bool = False
    level_code: Optional[int] = None
    writeback_state: str = "not_requested"
    writeback_response: Dict[str, Any] = field(default_factory=dict)
    # 兼容历史审计字段。新逻辑不再将反解/媒体/模型等技术失败回写为 level=6；
    # 技术失败保持 level=0，等待下一轮或人工重试。
    fallback_level_written: bool = False
    # 人工 Ctrl+C 中止不是业务分类失败，不能回写 level=6。
    interrupted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mid": self.mid,
            "uid": self.uid,
            "mode": self.mode,
            "task_id": self.task_id,
            "customer_id": self.customer_id,
            "short_url": self.short_url,
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
            "source_industry_name": self.source_industry_name,
            "industry_refine_note": self.industry_refine_note,
            "is_forward": self.is_forward,
            "forward_mid": self.forward_mid,
            "forward_content": self.forward_content,
            "forward_status": self.forward_status,
            "hit_mid_tag": self.hit_mid_tag,
            "hit_brand_name": self.hit_brand_name,
            "brand_terms": self.brand_terms,
            "topic_terms": self.topic_terms,
            "timings": self.timings.to_dict(),
            "write_back": self.write_back,
            "level_code": self.level_code,
            "writeback_state": self.writeback_state,
            "writeback_response": self.writeback_response,
            "fallback_level_written": self.fallback_level_written,
            "interrupted": self.interrupted,
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
        logging_cfg = config.get("logging", {})
        self.error_file_enabled = bool(logging_cfg.get("error_file_enabled", True))
        self.resolve_failure_file_enabled = bool(
            logging_cfg.get("resolve_failure_file_enabled", False)
        )
        self.storage_cfg = config.get("storage", {})

        resolver_cfg = config.get("mid_resolver", {})
        self.resolver = MidResolverClient(
            url=resolver_cfg.get("url", "http://terra.biz.weibo.com/mid/media"),
            timeout=resolver_cfg.get("timeout", 30),
            max_retry=resolver_cfg.get("max_retry", 3),
            logger_=self.logger,
        )

        self.classifier = BlogClassifier(config, self.logger)
        self.audit = RunAudit(config, self.logger)
        self._cleanup_expired_cache()

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
            result.task_id = record.super_task_id
            result.customer_id = record.customer_id
            result.short_url = record.short_url or ""
            result.source_industry_name = record.task_industry_name or ""
            result.hit_mid_tag = record.hit_mid_tag or ""
            result.hit_brand_name = record.hit_brand_name or ""
            result.brand_terms = (
                [record.hit_brand_name]
                if record.hit_brand_name else list(record.task_brand_values)
            )
            result.topic_terms = list(record.task_topic_values)
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
                # 反解失败属于技术失败，保持 level=0 等待重试。
                self._write_resolve_fail_log(result, record)
                result.layer = self.classifier.other_label
                result.success = False
                result.industry_name = record.task_industry_name if record else ""
                result.forward_status = "not_forward"
                result.timings.resolve_ms = (time.perf_counter() - t_resolve_start) * 1000
                result.timings.total_ms = (time.perf_counter() - t_total_start) * 1000
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
                    # 优先使用 hit_mid_tag 反解析出的“命中品牌词”；无命中时兼容回退到任务品牌词列表
                    item.brand_values = list(result.brand_terms)
                    item.topic_values = list(result.topic_terms)
                    item.forward_mid = str(record.forward_mid or "")
                    item.forward_content = record.forward_text or ""
                    item.extra.update({
                        "row_id": record.id,
                        "customer_id": record.customer_id,
                        "super_task_id": record.super_task_id,
                        "author_name": record.mid_uid_name,
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
            result.industry_refine_note = classify_result.industry_refine_note
            result.is_forward = classify_result.is_forward
            result.forward_mid = classify_result.forward_mid
            result.forward_status = classify_result.forward_status

            if not classify_result.success:
                result.error_stage = "classify"
                raise RuntimeError(classify_result.error or "分类器返回失败")

            if record is not None and self.repo is not None:
                result.level_code = self.repo.get_level_code(
                    classify_result.industry_name or record.task_industry_name,
                    classify_result.layer,
                )

            if write_back and record is not None and self.repo is not None:
                t_writeback_start = time.perf_counter()
                result.writeback_state = "pending"
                try:
                    writeback_result = self.repo.update_level_result(None, record, classify_result)
                    result.level_code = int(writeback_result["level"])
                    result.writeback_state = str(writeback_result.get("state", "applied"))
                    result.writeback_response = dict(writeback_result.get("response") or {})
                except Exception as e:
                    result.writeback_state = "failed"
                    result.error = f"回写失败: {str(e)}"
                    result.error_stage = "writeback"
                    result.success = False
                    raise
                result.timings.writeback_ms = (time.perf_counter() - t_writeback_start) * 1000

        except KeyboardInterrupt:
            # Ctrl+C 可能发生在反解、下载、模型调用或回写等待期间。
            # 这是人工终止，不是“内容分类失败”，必须保持 level=0 以便下次 worker 继续处理。
            result.success = False
            result.interrupted = True
            result.error_stage = "interrupted"
            result.error = "运行被人工中止（Ctrl+C）；未将此 mid 回写为 level=6"
            self.logger.warning("当前 mid 被人工中止，保留 level=0 等待下次处理: %s", mid)
            raise
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

            # 技术链路失败不是业务分类结果，绝不能伪造为 level=6。
            # 只有 classifier 成功返回 1/2/3/6 时才会进入 update_level_result。
            if not result.success and result.writeback_state == "not_requested" and write_back:
                result.writeback_state = (
                    "skipped_interrupted" if result.interrupted else "skipped_technical_failure"
                )

            if result.error_stage:
                self._write_error_log(result)

            self._log_result(result)
            self.audit.record_result(result, record)

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

    def _cleanup_expired_cache(self):
        """启动时清理过期缓存，异常中断遗留的媒体文件不会无限堆积。"""
        retention_hours = float(self.config.get("media", {}).get("cache_retention_hours", 24))
        if retention_hours <= 0 or not os.path.isdir(DEFAULT_CACHE_DIR):
            return
        cutoff = time.time() - retention_hours * 3600
        removed = 0
        for root, dirs, files in os.walk(DEFAULT_CACHE_DIR, topdown=False):
            for filename in files:
                path = os.path.join(root, filename)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
            for dirname in dirs:
                path = os.path.join(root, dirname)
                try:
                    if not os.listdir(path):
                        os.rmdir(path)
                except OSError:
                    pass
        if removed:
            self.logger.info("启动清理过期媒体缓存: %s 个文件", removed)

    def _write_error_log(self, result: ProcessResult):
        if not self.error_file_enabled:
            return
        os.makedirs(self.error_log_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        error_log_path = os.path.join(self.error_log_dir, f"{date_str}_error.log")
        if not local_file_writes_allowed({"storage": self.storage_cfg}, error_log_path):
            self.logger.warning("磁盘空间不足，跳过每日错误文件写入: %s", error_log_path)
            return

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
        if not self.resolve_failure_file_enabled:
            return
        os.makedirs(self.error_log_dir, exist_ok=True)
        resolve_fail_log = os.path.join(self.error_log_dir, "反解失败汇总.txt")
        if not local_file_writes_allowed({"storage": self.storage_cfg}, resolve_fail_log):
            self.logger.warning("磁盘空间不足，跳过反解失败汇总文件写入: %s", resolve_fail_log)
            return

        # 计算重试次数：检查文件中该 mid 已出现多少次
        retry_count = 1
        try:
            if os.path.exists(resolve_fail_log):
                with open(resolve_fail_log, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.split("\t")
                        if len(parts) >= 2 and parts[1] == result.mid:
                            retry_count += 1
        except Exception:
            pass

        # 如果文件不存在或为空，先写表头
        need_header = (
            not os.path.exists(resolve_fail_log)
            or os.path.getsize(resolve_fail_log) == 0
        )

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
            with open(resolve_fail_log, "a", encoding="utf-8") as f:
                if need_header:
                    f.write(RESOLVE_FAIL_HEADER)
                f.write(line)
        except Exception as e:
            self.logger.error(f"写入反解失败汇总失败: {e}")

    def _log_result(self, result: ProcessResult):
        t = result.timings
        source_industry = result.source_industry_name or result.industry_name or "无"
        effective_industry = result.industry_name or source_industry
        brand = "、".join(result.brand_terms) if result.brand_terms else "无"
        topics = "、".join(result.topic_terms) if result.topic_terms else "无"
        forward_text = "是" if result.is_forward else "否"
        content_preview = " ".join((result.content_preview or "").split())[:200] or "无"
        # 原博内容是用户明确要求的排障上下文，不做字符截断；
        # 仅折叠换行，保持每个 mid 的结果块易读。
        forward_content = " ".join((result.forward_content or "").split()) or "无"
        forward_mid = result.forward_mid or "无"
        video_fid = result.video_fid or "无"
        video_cover_url = result.video_cover_url or "无"
        if not result.success and result.error_stage != "writeback":
            level_text = "未生成（技术失败，不是业务等级）"
        elif result.level_code is not None:
            level_text = f"level={result.level_code}（{result.layer}）"
        else:
            level_text = f"层级={result.layer}"
        writeback_labels = {
            "applied": "成功",
            "already_applied": "已是目标值",
            "confirmed_after_transport_error": "响应异常，已确认落库",
            "not_requested": "未执行（预览）",
            "pending": "执行中",
            "failed": "失败",
            "skipped_technical_failure": "未回写（技术失败，保留 level=0 待重试）",
            "skipped_interrupted": "未回写（人工中止，保留 level=0）",
        }

        lines = [
            f"  │ 任务: task_id={result.task_id or '无'} | customer_id={result.customer_id or '无'}",
            f"  │ 博文URL: {result.short_url or '无'}",
            f"  │ 输入: 行业={source_industry} | 品牌词={brand} | 话题词={topics} | "
            f"hit_mid_tag={result.hit_mid_tag or '无'} | 媒体={result.media_type}",
        ]
        if result.industry_refine_note:
            lines.append(
                f"  │ 行业路由: {source_industry} → {effective_industry} | {result.industry_refine_note}"
            )
        lines.extend([
            f"  │ 正文预览: {content_preview}",
            f"  │ 是否转发: {forward_text}",
            f"  │ 原博文mid(forward_mid): {forward_mid}",
            f"  │ 原博文内容: {forward_content}",
            f"  │ 转发判定: {result.forward_status}",
            f"  │ 图片 pid: {result.pic_ids}",
            f"  │ 视频 fid: {video_fid}",
            f"  │ 视频封面: {video_cover_url}",
            f"  │ 分类结果: {level_text}",
            f"  │ 回写状态: {writeback_labels.get(result.writeback_state, result.writeback_state)}",
        ])
        if result.writeback_response:
            lines.append(f"  │ 回写响应: resp={result.writeback_response!r}")
        if result.error_stage:
            lines.append(f"  │ 异常: 阶段={result.error_stage} | {result.error[:300]}")
        lines.append(
            f"  │ 耗时: 反解={t.resolve_ms:.0f}ms 分类={t.classify_ms:.0f}ms "
            f"清理={t.cleanup_ms:.0f}ms 回写={t.writeback_ms:.0f}ms 总计={t.total_ms:.0f}ms"
        )

        if result.fallback_level_written:
            lines.append(f"  └─ ⚠️ 处理完成（异常兜底） | {level_text}")
        elif result.success:
            lines.append(f"  └─ ✅ 处理成功 | {level_text}")
        elif result.error_stage == "writeback":
            lines.append("  └─ ❌ 处理失败 | 回写结果未确认，请核查数据库")
        else:
            lines.append("  └─ ❌ 处理失败 | 未回写，level=0 待重试")

        for line in lines:
            if result.success and not result.fallback_level_written:
                self.logger.info(line)
            else:
                self.logger.warning(line)
