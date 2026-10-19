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
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_weekly_report.py"
SPEC = importlib.util.spec_from_file_location("generate_weekly_report", SCRIPT)
weekly_report = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(weekly_report)


class WeeklyReportTests(unittest.TestCase):
    def test_report_lists_failure_files_and_current_exhausted_backlog_without_double_counting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audit_dir = root / "runs"
            audit_dir.mkdir()
            failures = root / "failures"
            failures.mkdir()
            state = failures / "retry_state.sqlite3"
            with sqlite3.connect(state) as conn:
                conn.execute("CREATE TABLE retry_state (status TEXT)")
                conn.executemany("INSERT INTO retry_state VALUES (?)", [("exhausted",), ("exhausted",), ("waiting",)])
            log = str(failures / "record.json")
            event = {"event": "mid_retry_exhausted", "customer_id": 1, "task_id": 2,
                     "record_id": 3, "mid": "m1", "failure_log": log, "raw": "不要发送的原始正文"}
            events = [
                {"event": "mid_processed", "status": "failed", "error_stage": "resolve", "mid": "m1"},
                event, dict(event),
            ]
            day = date(2026, 10, 18)
            report = weekly_report.build_report(events, day, day, audit_dir, "日报")
            self.assertIn("- 处理尝试：1\n", report)
            self.assertIn("- 本窗口新增停止自动重试：1 条", report)
            self.assertIn("- 当前累计预算已耗尽：2 条", report)
            self.assertIn("- 当前等待重试：1 条", report)
            self.assertIn(log, report)
            self.assertIn(str(state), report)
            self.assertNotIn("不要发送的原始正文", report)
            idle_report = weekly_report.build_report([], day, day, audit_dir, "日报")
            self.assertIn("- 本窗口新增停止自动重试：0 条", idle_report)
            self.assertIn("- 当前累计预算已耗尽：2 条", idle_report)

    def test_unreadable_retry_state_does_not_break_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "runs").mkdir()
            (root / "failures").mkdir()
            state = root / "failures" / "retry_state.sqlite3"
            state.write_bytes(b"bad sqlite file")
            report = weekly_report.build_report([], date(2026, 10, 18), date(2026, 10, 18), root / "runs")
            self.assertIn("无法读取重试状态", report)
            self.assertEqual(b"bad sqlite file", state.read_bytes())

    def write_events(self, root, folder, events):
        day_dir = root / folder
        day_dir.mkdir(exist_ok=True)
        (day_dir / "worker.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )

    def test_daily_report_finds_314_successes_in_worker_startup_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = [
                {"event": "run_started", "event_time": "2026-09-28T10:00:00", "run_id": "r1"},
                {"event": "mid_processed", "event_time": "2026-10-08T23:59:59", "run_id": "r1", "status": "failed"},
                {"event": "mid_processed", "event_time": "2026-10-10T00:00:00", "run_id": "r1", "status": "fallback"},
            ] + [
                {"event": "mid_processed", "event_time": "2026-10-09T16:30:51", "run_id": "r1", "status": "success", "mid": str(index)}
                for index in range(314)
            ]
            self.write_events(root, "20260928", events)
            day = date(2026, 10, 9)
            selected = list(weekly_report.iter_events(root, day, day))
            self.assertEqual(314, len(selected))
            self.assertEqual({"2026-10-09"}, {event["_audit_day"] for event in selected})
            report = weekly_report.build_report(selected, day, day, root, "日报")
            self.assertIn("- 处理尝试：314\n", report)
            self.assertIn("- 正常分类 success：314（100.0%）", report)
            self.assertIn("- 运行实例：1\n", report)
            self.assertIn("- 2026-10-09：314 条｜success 314", report)
            startup_day = date(2026, 9, 28)
            startup_events = list(weekly_report.iter_events(root, startup_day, startup_day))
            self.assertEqual(["run_started"], [event["event"] for event in startup_events])

    def test_weekly_and_monthly_reports_use_event_dates_for_totals_and_trends(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_events(root, "20260928", [
                {"event": "mid_processed", "event_time": f"{day}T12:00:00", "status": "success"}
                for day in ("2026-09-30", "2026-10-01", "2026-10-05", "2026-10-09", "2026-10-10")
            ])
            for start, end, name, expected in (
                (date(2026, 10, 5), date(2026, 10, 9), "周汇总", 2),
                (date(2026, 10, 1), date(2026, 10, 9), "月汇总", 3),
            ):
                with self.subTest(name=name):
                    report = weekly_report.build_report(
                        weekly_report.iter_events(root, start, end), start, end, root, name
                    )
                    self.assertIn(f"- 处理尝试：{expected}\n", report)
                    self.assertIn("- 2026-10-09：1 条｜success 1", report)
                    self.assertIn("- 2026-10-05：1 条｜success 1", report)

    def test_timezone_offsets_and_midnight_boundaries_use_shanghai_date(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            timestamps = [
                "2026-10-08T15:59:59Z",   # 北京时间 10.08
                "2026-10-08T16:00:00Z",   # 北京时间 10.09 零点
                "2026-10-09T15:59:59+00:00",
                "2026-10-09T16:00:00+00:00",  # 北京时间 10.10 零点
                "2026-10-09T00:00:00+08:00",
                "2026-10-09T23:59:59",   # 无偏移的历史时间视为北京时间
                "2026-10-10T00:00:00",
            ]
            self.write_events(root, "20260928", [
                {"event": "mid_processed", "event_time": timestamp, "status": "success"}
                for timestamp in timestamps
            ])
            day = date(2026, 10, 9)
            selected = list(weekly_report.iter_events(root, day, day))
            self.assertEqual(timestamps[1:3] + timestamps[4:6], [event["event_time"] for event in selected])
            self.assertEqual({"2026-10-09"}, {event["_audit_day"] for event in selected})

    def test_missing_event_time_falls_back_but_invalid_time_is_not_misdated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_events(root, "20261009", [
                {"event": "mid_processed", "status": "success"},
                {"event": "mid_processed", "event_time": None, "status": "fallback"},
                {"event": "mid_processed", "event_time": "broken", "status": "failed"},
                {"event": "mid_processed", "event_time": 123, "status": "failed"},
            ])
            day = date(2026, 10, 9)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                selected = list(weekly_report.iter_events(root, day, day))
            self.assertEqual(["success", "fallback"], [event["status"] for event in selected])
            self.assertIn("event_time", stderr.getvalue())

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
