#!/usr/bin/env python3
"""
报告统计脚本的离线测试。

本测试不连接 MySQL、不调用模型、不发送钉钉消息，只用临时 JSONL 审计样本验证：
  - success / fallback / failed / interrupted 的统计；
  - 分类层级、失败阶段和耗时百分位；
  - 无审计数据时的明确提示。

运行方式：
  cd intent_behavior
  python3 -m unittest tests.test_weekly_report -v
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_weekly_report.py"
SPEC = importlib.util.spec_from_file_location("generate_weekly_report", SCRIPT)
weekly_report = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(weekly_report)


class WeeklyReportTests(unittest.TestCase):
    def test_report_aggregates_structured_audit_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            day_dir = root / "20260917"
            day_dir.mkdir()
            events = [
                {"event": "run_started", "run_id": "r1"},
                {
                    "event": "mid_processed",
                    "run_id": "r1",
                    "status": "success",
                    "task_id": 1,
                    "record_id": 11,
                    "mid": "m1",
                    "layer": "认知层",
                    "industry": "汽车",
                    "media_type": "text",
                    "forward_status": "not_forward",
                    "error_stage": "",
                    "timings_ms": {"total_ms": 100},
                },
                {
                    "event": "mid_processed",
                    "run_id": "r1",
                    "status": "fallback",
                    "task_id": 1,
                    "record_id": 12,
                    "mid": "m2",
                    "layer": "其他",
                    "industry": "汽车",
                    "media_type": "video_cover",
                    "forward_status": "empty_forward",
                    "error_stage": "resolve",
                    "timings_ms": {"total_ms": 300},
                },
                {
                    "event": "mid_processed",
                    "run_id": "r1",
                    "status": "failed",
                    "task_id": 2,
                    "record_id": 13,
                    "mid": "m3",
                    "layer": "未识别",
                    "industry": "奶茶",
                    "media_type": "image",
                    "forward_status": "failed",
                    "error_stage": "writeback",
                    "timings_ms": {"total_ms": 500},
                },
                {
                    "event": "mid_processed",
                    "run_id": "r1",
                    "status": "interrupted",
                    "task_id": 2,
                    "record_id": 14,
                    "mid": "m4",
                    "layer": "未识别",
                    "industry": "奶茶",
                    "media_type": "text",
                    "forward_status": "not_forward",
                    "error_stage": "interrupted",
                    "timings_ms": {"total_ms": 700},
                },
            ]
            (day_dir / "r1.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in events),
                encoding="utf-8",
            )
            report = weekly_report.build_report(
                weekly_report.iter_events(root, date(2026, 9, 17), date(2026, 9, 17)),
                date(2026, 9, 17),
                date(2026, 9, 17),
                root,
                report_name="日报",
            )
            self.assertIn("# 原生内容站 AI 意图分层日报（2026-09-17）", report)
            self.assertIn("- 处理尝试：4", report)
            self.assertIn("- 正常分类 success：1（25.0%）", report)
            self.assertIn("- 失败兜底 fallback（已回写 level=6）：1（25.0%）", report)
            self.assertIn("- 未闭环失败 failed：1（25.0%）", report)
            self.assertIn("- 人工中断 interrupted：1（25.0%）", report)
            self.assertIn("- P50：400ms", report)
            self.assertIn("- P95：670ms", report)
            self.assertIn("- resolve：1（25.0%）", report)
            self.assertIn("- writeback：1（25.0%）", report)

    def test_report_clearly_marks_missing_audit_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = weekly_report.build_report(
                weekly_report.iter_events(root, date(2026, 9, 17), date(2026, 9, 17)),
                date(2026, 9, 17),
                date(2026, 9, 17),
                root,
            )
            self.assertIn("- 当前没有可用于本报告的 JSONL 审计记录。", report)

    def test_explicit_date_range_takes_precedence_over_days(self):
        start_day, end_day = weekly_report.explicit_date_range(
            "2026-09-01", date(2026, 9, 30), 7
        )
        self.assertEqual(date(2026, 9, 1), start_day)
        self.assertEqual(date(2026, 9, 30), end_day)


if __name__ == "__main__":
    unittest.main()
