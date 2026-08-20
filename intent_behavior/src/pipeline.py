#!/usr/bin/env python3
"""
正式分类 Pipeline
=================
核心生产入口，封装单条/批量博文的完整分类流程：

1. mid 反解（通过反解接口拿真实 content / pid / fid）
2. 媒体类型判定（text / image / video / auto）
3. 调用分类器
4. 临时文件清理
5. 结果回写（可选）
6. 结构化计时与错误记录

设计原则：
- 与 tests/08_mid_resolver 测试脚本使用同一套反解 + 分类逻辑
- 不伪造数据：所有输出字段必须来自真实反解结果或模型真实返回
- 失败明确：任何阶段失败都会记录错误日志，不会冒充成功
- 资源清理：图片/视频下载的临时文件在分类后必须清理
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
from .models import ClassifyResult


# 项目根目录与默认缓存目录
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CACHE_DIR = os.path.join(PROJECT_ROOT, "output", ".cache")
DEFAULT_LOG_DIR = os.path.join(PROJECT_ROOT, "logs")


@dataclass
class ProcessTimings:
    """各阶段耗时（毫秒）"""

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
    """单条博文处理结果"""

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

        # 初始化反解客户端
        resolver_cfg = config.get("mid_resolver", {})
        self.resolver = MidResolverClient(
            url=resolver_cfg.get("url", "http://terra.biz.weibo.com/mid/media"),
            timeout=resolver_cfg.get("timeout", 30),
            max_retry=resolver_cfg.get("max_retry", 3),
            logger_=self.logger,
        )

        # 初始化分类器
        self.classifier = BlogClassifier(config, self.logger)

        # 初始化 MySQL 仓储（需要时才用）
        self.repo: Optional[MySQLTaskRepository] = None
        mysql_cfg = config.get("mysql")
        if mysql_cfg:
            self.repo = MySQLTaskRepository(mysql_cfg, self.logger)

    def process_one(
        self,
        mid: str,
        uid: Optional[str] = None,
        mode: str = "auto",
        write_back: bool = False,
        record: Optional[MidRecord] = None,
    ) -> ProcessResult:
        """
        处理单条博文。

        Args:
            mid: 博文 mid
            uid: 作者 uid（可选，建议传入）
            mode: 处理模式
                  - "auto": 根据反解结果自动判断 video > image > text
                  - "text": 强制按文本分类
                  - "image": 强制按图片分类（反解无图则失败）
                  - "video": 强制按视频分类（反解无视频则失败）
            write_back: 是否回写 MySQL（需传入 record）
            record: MySQL 分表记录（write_back=True 时使用）

        Returns:
            ProcessResult
        """
        t_total_start = time.perf_counter()
        result = ProcessResult(mid=mid, uid=uid or "", mode=mode, write_back=write_back)

        try:
            # ── 阶段 1：mid 反解 ─────────────────────────────────
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
                raise

            result.timings.resolve_ms = (time.perf_counter() - t_resolve_start) * 1000
            result.resolved = resolved
            result.uid = resolved.uid or uid or ""
            result.content_preview = (resolved.content or "")[:200]
            result.pic_ids = list(resolved.pic_ids)
            result.video_fid = resolved.video_fid
            result.video_cover_url = resolved.video_cover_url

            # ── 阶段 2：构造 BlogItem 并分类 ──────────────────────
            t_classify_start = time.perf_counter()
            try:
                item = resolved.to_blog_item()

                # 根据 mode 强制覆盖媒体字段
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
                # mode == "auto" 时不做强制覆盖，由 classifier.detect_media_type 判断

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

            if not classify_result.success:
                result.error_stage = "classify"
                raise RuntimeError(classify_result.error or "分类器返回失败")

            # ── 阶段 3：结果回写（可选）───────────────────────────
            if write_back and record is not None and self.repo is not None:
                t_writeback_start = time.perf_counter()
                try:
                    with self.repo.connect() as conn:
                        self.repo.update_level_result(conn, record, classify_result)
                except Exception as e:
                    result.error = f"回写失败: {str(e)}"
                    result.error_stage = "writeback"
                    result.success = False
                    raise
                result.timings.writeback_ms = (time.perf_counter() - t_writeback_start) * 1000

        except Exception:
            # 确保 success=False，error_stage 如果不为空保留
            result.success = False
            if not result.error:
                result.error = traceback.format_exc()
            if not result.error_stage:
                result.error_stage = "unknown"

        finally:
            # ── 阶段 4：清理临时文件 ──────────────────────────────
            t_cleanup_start = time.perf_counter()
            self._cleanup_temp_files(mid, result.video_fid)
            result.timings.cleanup_ms = (time.perf_counter() - t_cleanup_start) * 1000

            result.timings.total_ms = (time.perf_counter() - t_total_start) * 1000

            # 失败时写错误日志
            if not result.success:
                self._write_error_log(result)

            # 输出结构化信息
            self._log_result(result)

        return result

    def process_batch(
        self,
        inputs: List[Dict[str, Any]],
        mode: str = "auto",
        write_back: bool = False,
        workers: int = 1,
    ) -> List[ProcessResult]:
        """
        批量处理博文。

        Args:
            inputs: 输入列表，每个元素是 dict，必须包含 "mid"，可选 "uid"
            mode: 处理模式
            write_back: 是否回写（当前仅支持单线程 + 传入完整 record）
            workers: 并发数（>1 时 write_back 必须为 False）

        Returns:
            ProcessResult 列表
        """
        if workers > 1 and write_back:
            raise ValueError("并发模式暂不支持 MySQL 回写，请 workers=1 或 write_back=False")

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
            # 并发模式：每个线程独立 Pipeline 实例
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _worker(inp: Dict[str, Any]) -> ProcessResult:
                # 每个线程独立实例，避免共享连接/状态问题
                pipeline = ClassifyPipeline(self.config, self.logger, self.error_log_dir)
                return pipeline.process_one(
                    mid=inp["mid"],
                    uid=inp.get("uid"),
                    mode=mode,
                    write_back=False,
                )

            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_input = {
                    executor.submit(_worker, inp): inp for inp in inputs
                }
                completed = 0
                for future in as_completed(future_to_input):
                    results.append(future.result())
                    completed += 1
                    if completed % max(1, total // 10) == 0 or completed == total:
                        self.logger.info(f"批量进度: {completed}/{total}")

        # 按输入顺序排序
        mid_order = {inp["mid"]: idx for idx, inp in enumerate(inputs)}
        results.sort(key=lambda r: mid_order.get(r.mid, 0))
        return results

    def _cleanup_temp_files(self, mid: str, video_fid: str = ""):
        """清理与本次处理相关的临时文件"""
        cleaned = []

        # 1. 清理图片临时目录
        img_dir = os.path.join(DEFAULT_CACHE_DIR, "blog_images", mid)
        if os.path.exists(img_dir):
            try:
                shutil.rmtree(img_dir)
                cleaned.append(img_dir)
            except Exception as e:
                self.logger.warning(f"清理图片目录失败 {img_dir}: {e}")

        # 2. 清理视频相关文件
        if video_fid:
            safe_fid = video_fid.replace(":", "_")

            # cover 模式封面图
            cover_path = os.path.join(DEFAULT_CACHE_DIR, "video_covers", f"cover_{safe_fid}.jpg")
            if os.path.exists(cover_path):
                try:
                    os.remove(cover_path)
                    cleaned.append(cover_path)
                except Exception as e:
                    self.logger.warning(f"清理视频封面失败 {cover_path}: {e}")

            # frame 模式视频文件和帧目录
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

        # 3. 兜底：删除可能遗漏的以 mid 命名的缓存文件
        for pattern in [
            os.path.join(DEFAULT_CACHE_DIR, "**", f"*{mid}*"),
        ]:
            for path in glob.glob(pattern, recursive=True):
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                        cleaned.append(path)
                    except Exception:
                        pass
                elif os.path.isdir(path) and mid in os.path.basename(path):
                    try:
                        shutil.rmtree(path)
                        cleaned.append(path)
                    except Exception:
                        pass

        if cleaned:
            self.logger.debug(f"清理临时文件: {cleaned}")

    def _write_error_log(self, result: ProcessResult):
        """把失败记录写入 [日期]_error.log"""
        os.makedirs(self.error_log_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        error_log_path = os.path.join(self.error_log_dir, f"{date_str}_error.log")

        line = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t"
            f"{result.mid}\t"
            f"{result.uid}\t"
            f"{result.mode}\t"
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

    def _log_result(self, result: ProcessResult):
        """输出结构化处理信息"""
        t = result.timings
        status = "✅ 成功" if result.success else "❌ 失败"

        lines = [
            "=" * 60,
            f"处理结果 [{status}] mid={result.mid} uid={result.uid}",
            f"  模式: {result.mode}",
            f"  媒体类型: {result.media_type}",
            f"  分类层级: {result.layer}",
            f"  正文预览: {result.content_preview[:120]}",
            f"  图片 pid: {result.pic_ids}",
            f"  视频 fid: {result.video_fid}",
            f"  视频封面: {result.video_cover_url}",
            f"  耗时: 反解={t.resolve_ms:.0f}ms 分类={t.classify_ms:.0f}ms "
            f"清理={t.cleanup_ms:.0f}ms 回写={t.writeback_ms:.0f}ms 总计={t.total_ms:.0f}ms",
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
