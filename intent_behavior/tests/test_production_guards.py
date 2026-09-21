#!/usr/bin/env python3
"""
生产链路关键保护的离线单元测试。

本文件不连接 MySQL、不调用模型、不发送回写请求，覆盖上线前最容易回归的业务保护：
  - operator_uid 作为 customer_id 的分表路由；
  - hit_mid_tag 精确品牌反解析与缺失时回退；
  - 转发“未发现异常”不被误判为异常；
  - 正常转发按“转发正文 + 原博正文”合并；
  - 回写 data=0 / 网络超时后的只读确认；
  - 失败按规则回写 level=6；
  - 视频超过 300 秒时降级封面。

运行方式：
  cd intent_behavior
  python3 -m unittest tests.test_production_guards -v
"""

from __future__ import annotations

import os
import sys
import unittest
import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch


PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_DIR)

from src.classifier import BlogClassifier
from src.db_client import MidRecord, MySQLTaskRepository, TaskRecord, parse_topic_values
from src.models import BlogItem, ClassifyResult
from src.pipeline import ClassifyPipeline, ProcessResult
from src.utils import extract_forward_status


def minimal_config() -> dict:
    return {
        "api": {"url": "http://unit-test", "model": "unit-test"},
        "media": {
            "image": {},
            "video": {"enabled": True, "max_duration_sec": 300, "max_video_size_mb": 200},
        },
        "classification": {
            "supported_industries": ["汽车"],
            "default_industry": "汽车",
            "pending_level": 0,
            "other_label": "其他",
            "failure_label": "未识别",
            "industry_rules": {
                "汽车": {
                    "layers": ["认知层", "兴趣层", "考虑层", "其他"],
                    "level_mapping": {"认知层": 1, "兴趣层": 2, "考虑层": 3, "其他": 6},
                    "fallback_layer": "其他",
                },
            },
        },
        "prompts": {
            "industries": {
                "汽车": {
                    "system_prompt": "test",
                    "user_text_template": "{content}",
                    "user_image_template": "{content}",
                    "user_video_template": "{content}",
                }
            },
            "forward_review_prompt": "{content}|{forward_content}",
        },
        "mysql": {
            "host": "unit-test",
            "user": "unit-test",
            "password": "unit-test",
            "database": "unit-test",
            "task_customer_id_field": "operator_uid",
        },
        "result_writer": {"url": "http://unit-test"},
        "logging": {"write_legacy_tsv": False},
        "audit": {"enabled": False},
    }


def make_record() -> MidRecord:
    return MidRecord(
        id=79,
        customer_id=2608812381,
        super_task_id=1302305683722469377,
        mid="5281224466635091",
        mid_uid="1027738525",
        mid_uid_name="测试账号",
        mid_text="正文",
        mid_pids="",
        mid_fids="",
        task_industry_name="汽车",
    )


class _FakeCursor:
    def __init__(self, row=None):
        self.row = row

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeConn:
    def __init__(self, row=None):
        self.row = row

    def cursor(self):
        return _FakeCursor(self.row)


class ProductionGuardTests(unittest.TestCase):
    def test_food_routes_to_milk_tea_without_text_only_rejection(self):
        cfg = minimal_config()
        cfg["classification"]["supported_industries"] = ["汽车", "奶茶"]
        cfg["classification"]["infer_refine_map"] = {"美食": "奶茶"}
        classifier = BlogClassifier(cfg)
        classifier.api_client.classify_text = MagicMock(return_value="否")

        industry, note = classifier._maybe_refine_industry(
            BlogItem(mid="m", uid="u", content="泛情绪文案", industry_name="美食"),
            "美食",
        )

        self.assertEqual("奶茶", industry)
        self.assertIn("品牌词、话题词、正文和媒体动态判断", note)
        classifier.api_client.classify_text.assert_not_called()

    def test_food_video_reaches_full_video_classifier(self):
        cfg = minimal_config()
        cfg["classification"]["supported_industries"] = ["汽车", "奶茶"]
        cfg["classification"]["infer_refine_map"] = {"美食": "奶茶"}
        cfg["classification"]["industry_rules"]["奶茶"] = {
            "layers": ["品牌与社交类", "口碑体验类", "消费决策类", "其他"],
        }
        cfg["prompts"]["industries"]["奶茶"] = cfg["prompts"]["industries"]["汽车"]
        classifier = BlogClassifier(cfg)
        classifier._classify_video = MagicMock(return_value=ClassifyResult(
            mid="5345366745549609",
            uid="7876703713",
            layer="品牌与社交类",
            media_type="video_frame",
            success=True,
            industry_name="奶茶",
        ))
        item = BlogItem(
            mid="5345366745549609",
            uid="7876703713",
            content="#动态品牌话题#",
            media_ids=["1034:5345251599253522"],
            industry_name="美食",
            brand_values=["动态茶饮品牌"],
            topic_values=["动态品牌话题"],
        )

        result = classifier.classify_item(item)

        classifier._classify_video.assert_called_once()
        self.assertEqual("奶茶", result.industry_name)
        self.assertEqual("品牌与社交类", result.layer)

    def test_milk_tea_prompt_uses_dynamic_brand_topic_and_content(self):
        cfg = minimal_config()
        cfg["classification"]["supported_industries"] = ["汽车", "奶茶"]
        cfg["classification"]["industry_rules"]["奶茶"] = {
            "layers": ["品牌与社交类", "口碑体验类", "消费决策类", "其他"],
        }
        cfg["prompts"]["industries"]["奶茶"] = {
            "system_prompt": "dynamic",
            "user_text_template": "品牌={brand_terms}|话题={topic_terms}|正文={content}",
            "user_image_template": "品牌={brand_terms}|话题={topic_terms}|正文={content}",
            "user_video_template": "品牌={brand_terms}|话题={topic_terms}|正文={content}",
        }
        classifier = BlogClassifier(cfg)
        item = BlogItem(
            mid="m",
            uid="u",
            content="赞美和情绪认同内容",
            industry_name="奶茶",
            brand_values=["动态品牌"],
            topic_values=["动态话题"],
        )

        _, _, prompt = classifier._build_prompt_pack(item, "text")

        self.assertIn("品牌=动态品牌", prompt)
        self.assertIn("话题=动态话题", prompt)
        self.assertIn("正文=赞美和情绪认同内容", prompt)

    def test_topic_code_parser_is_dynamic_and_deduplicated(self):
        self.assertEqual(
            ["霸王茶姬抹茶", "代言人林一"],
            parse_topic_values("霸王茶姬抹茶,#代言人林一#,霸王茶姬抹茶"),
        )

    def test_result_log_is_compact_and_ends_with_level_code(self):
        cfg = minimal_config()
        test_logger = logging.getLogger("test.compact_result_log")
        pipeline = ClassifyPipeline(cfg, test_logger)
        result = ProcessResult(
            mid="5345366745549609",
            uid="7876703713",
            mode="auto",
            task_id=1307727027356303361,
            customer_id=7419005545,
            content_preview="第一行\n第二行",
            forward_content="原博第一行\n原博第二行",
            pic_ids=[],
            video_fid="1034:5345251599253522",
            video_cover_url="https://wx3.sinaimg.cn/cover.jpg",
            layer="其他",
            level_code=6,
            media_type="video",
            success=True,
            industry_name="奶茶",
            source_industry_name="美食",
            industry_refine_note="进入完整多模态分层",
            hit_brand_name="霸王茶姬",
            brand_terms=["霸王茶姬"],
            topic_terms=["霸王茶姬抹茶系列"],
            write_back=True,
            writeback_state="applied",
            writeback_response={
                "data": 1, "message": "成功", "code": 0, "payload": None, "header": None,
            },
        )

        with self.assertLogs(test_logger, level="INFO") as captured:
            pipeline._log_result(result)

        output = "\n".join(captured.output)
        self.assertNotIn("mid=5345366745549609", output)
        self.assertNotIn("uid=7876703713", output)
        self.assertIn("task_id=1307727027356303361", output)
        self.assertIn("customer_id=7419005545", output)
        self.assertIn("品牌词=霸王茶姬", output)
        self.assertIn("话题词=霸王茶姬抹茶系列", output)
        self.assertIn("行业路由: 美食 → 奶茶", output)
        self.assertIn("正文预览: 第一行 第二行", output)
        self.assertIn("原博文mid(forward_mid): 无", output)
        self.assertIn("原博文内容: 原博第一行 原博第二行", output)
        self.assertIn("转发判定: not_forward", output)
        self.assertIn("图片 pid: []", output)
        self.assertIn("视频 fid: 1034:5345251599253522", output)
        self.assertIn("视频封面: https://wx3.sinaimg.cn/cover.jpg", output)
        self.assertIn("分类结果: level=6（其他）", output)
        self.assertIn("回写响应: resp={'data': 1, 'message': '成功', 'code': 0", output)
        self.assertTrue(captured.output[-1].endswith("└─ ✅ 处理成功 | level=6（其他）"))

    def test_operator_uid_routes_shard_and_parses_brand_tag(self):
        repo = MySQLTaskRepository(minimal_config()["mysql"], app_config=minimal_config())
        row = {
            "id": 1,
            "task_id": 99,
            "operator_uid": 2608812381,
            "industry_tag": '{"1042001":"汽车"}',
            "brand_tag": '{"1042015:carSubBrand_x":"蔚来"}',
            "topic_code": "蔚来发布会,乐道新车",
        }
        task = repo._row_to_task_record(row)
        self.assertIsNotNone(task)
        self.assertEqual(2608812381, task.customer_id)
        self.assertEqual("nature_ad_super_mid_1", task.shard_table)
        self.assertEqual("蔚来", task.resolve_brand_by_tag("1042015:carSubBrand_x"))
        self.assertEqual(["蔚来发布会", "乐道新车"], task.topic_values)

    def test_hit_brand_exact_match_and_missing_fallback(self):
        task = TaskRecord(
            id=1,
            task_id=99,
            customer_id=21,
            task_type=1,
            exec_status=1,
            industry_name="汽车",
            brand_values=["蔚来", "乐道"],
            brand_tag_map={"tag:nio": "蔚来"},
        )
        repo = MySQLTaskRepository(minimal_config()["mysql"], app_config=minimal_config())
        exact = repo._row_to_mid_record(
            {
                "id": 1, "customer_id": 21, "super_task_id": 99, "mid": "m1",
                "mid_uid": "u1", "hit_mid_tag": "tag:nio",
            },
            task,
        )
        missing = repo._row_to_mid_record(
            {
                "id": 2, "customer_id": 21, "super_task_id": 99, "mid": "m2",
                "mid_uid": "u2", "hit_mid_tag": "",
            },
            task,
        )
        self.assertEqual("蔚来", exact.hit_brand_name)
        self.assertEqual("", missing.hit_brand_name)
        self.assertEqual(["蔚来", "乐道"], missing.task_brand_values)

    def test_forward_normal_phrase_is_not_abnormal(self):
        self.assertEqual("正常", extract_forward_status("转发判定：未发现异常"))
        self.assertEqual("正常", extract_forward_status("转发判定：【正常】"))
        self.assertIsNone(extract_forward_status("这里只是提到异常两个字"))

    def test_normal_forward_combines_repost_and_original(self):
        item = BlogItem(
            mid="m",
            uid="u",
            content="转发者的观点",
            forward_mid="f",
            forward_content="原博正文内容",
        )
        composed = BlogClassifier._compose_forward_content(item)
        self.assertIn("【转发者正文】", composed)
        self.assertIn("转发者的观点", composed)
        self.assertIn("【被转发原博文正文】", composed)
        self.assertIn("原博正文内容", composed)

    def test_update_data_zero_is_success_only_when_confirmed(self):
        cfg = minimal_config()
        repo = MySQLTaskRepository(cfg["mysql"], app_config=cfg)
        repo.writer = MagicMock()
        repo.writer.update_level.return_value = {"code": 0, "data": 0}
        record = make_record()
        result = ClassifyResult(
            mid=record.mid, uid=record.mid_uid, layer="认知层",
            media_type="text", success=True, industry_name="汽车",
        )
        with patch.object(repo, "confirm_level", return_value=True):
            state = repo.update_level_result(None, record, result)
        self.assertEqual("already_applied", state["state"])

        with patch.object(repo, "confirm_level", return_value=False):
            with self.assertRaises(RuntimeError):
                repo.update_level_result(None, record, result)

    def test_update_timeout_is_success_only_when_confirmed(self):
        cfg = minimal_config()
        repo = MySQLTaskRepository(cfg["mysql"], app_config=cfg)
        repo.writer = MagicMock()
        repo.writer.update_level.side_effect = RuntimeError("timeout")
        record = make_record()
        result = ClassifyResult(
            mid=record.mid, uid=record.mid_uid, layer="其他",
            media_type="text", success=True, industry_name="汽车",
        )
        with patch.object(repo, "confirm_level", return_value=True):
            state = repo.update_level_result(None, record, result)
        self.assertEqual("confirmed_after_transport_error", state["state"])

    def test_failure_is_closed_with_level_six_when_writeback_enabled(self):
        pipeline = ClassifyPipeline(minimal_config())
        pipeline.repo = MagicMock()
        result = ProcessResult(
            mid="m", uid="u", mode="auto", success=False,
            error_stage="classify", error="模型不可用", industry_name="汽车",
        )
        pipeline._write_failure_fallback_level(result, make_record(), write_back=True)
        self.assertTrue(result.fallback_level_written)
        self.assertTrue(result.success)
        self.assertEqual("其他", result.layer)
        pipeline.repo.update_level_result.assert_called_once()

    def test_local_preview_does_not_write_mysql_audit(self):
        from src.audit import RunAudit

        config = minimal_config()
        config["audit"] = {"enabled": False, "mysql_enabled": True}
        audit = RunAudit(config)
        audit._queue_db_result = MagicMock()
        audit.record_result(
            ProcessResult(mid="m", uid="u", mode="auto", success=True),
            record=None,
        )
        audit._queue_db_result.assert_not_called()

    def test_keyboard_interrupt_is_not_fallback_level_six(self):
        pipeline = ClassifyPipeline(minimal_config())
        pipeline.repo = MagicMock()
        pipeline.resolver = MagicMock()
        pipeline.resolver.resolve.return_value = MagicMock(
            uid="u",
            content="测试正文",
            pic_ids=[],
            video_fid="",
            video_cover_url="",
            has_image=lambda: False,
            has_video=lambda: False,
            to_blog_item=lambda: BlogItem(mid="m", uid="u", content="测试正文"),
        )
        pipeline.classifier = MagicMock()
        pipeline.classifier.other_label = "其他"
        pipeline.classifier.classify_item.side_effect = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            pipeline.process_one("m", uid="u", write_back=True, record=make_record())

        # Ctrl+C 不能调用 level=6 兜底回写。
        pipeline.repo.update_level_result.assert_not_called()

    def test_mysql_audit_flushes_each_record_by_default(self):
        from src.audit import RunAudit

        config = minimal_config()
        config["audit"] = {"enabled": False, "mysql_enabled": True}
        audit = RunAudit(config)
        audit.flush_db = MagicMock()
        audit.record_result(
            ProcessResult(mid="m", uid="u", mode="auto", success=True),
            record=make_record(),
        )
        audit.flush_db.assert_called_once()

    def test_video_over_300_seconds_skips_frames(self):
        from src.media_handler import VideoHandler

        handler = VideoHandler(minimal_config()["media"]["video"])
        with patch.object(handler, "get_video_url", return_value="http://video"), \
             patch.object(handler, "download_video", return_value=True), \
             patch.object(handler, "get_video_duration", return_value=301), \
             patch("src.media_handler.os.remove"), \
             patch.object(handler, "extract_frames") as extract_frames:
            paths = handler.process_video_frames("fid:1")
        self.assertEqual([], paths)
        extract_frames.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
