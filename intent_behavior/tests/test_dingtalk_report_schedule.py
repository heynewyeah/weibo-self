#!/usr/bin/env python3
"""
钉钉报告日历调度的离线测试。

不连接钉钉、不读取业务库，只验证 T-1 日报、周五汇总、月末汇总
的触发条件和日期边界。

运行方式：
  cd intent_behavior
  python3 -m unittest tests.test_dingtalk_report_schedule -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "send_weekly_report.py"
SPEC = importlib.util.spec_from_file_location("send_weekly_report", SCRIPT)
report_sender = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = report_sender
SPEC.loader.exec_module(report_sender)


class DingtalkReportScheduleTests(unittest.TestCase):
    def test_explicit_run_date_is_stable_across_timezones(self):
        parsed = report_sender.parse_run_date("2026-09-20", "Asia/Shanghai")
        self.assertEqual(date(2026, 9, 20), parsed)

    def test_invalid_configured_timezone_is_rejected(self):
        with self.assertRaises(SystemExit):
            report_sender.parse_run_date("", "Invalid/Timezone")

    def test_normal_day_only_sends_t_minus_one_daily_report(self):
        reports = report_sender.scheduled_reports(date(2026, 9, 21))

        self.assertEqual(["daily"], [item.period for item in reports])
        self.assertEqual(date(2026, 9, 20), reports[0].start_day)
        self.assertEqual(date(2026, 9, 20), reports[0].end_day)

    def test_friday_adds_current_week_as_separate_report(self):
        reports = report_sender.scheduled_reports(date(2026, 9, 18))

        self.assertEqual(["daily", "weekly"], [item.period for item in reports])
        self.assertEqual(date(2026, 9, 14), reports[1].start_day)
        self.assertEqual(date(2026, 9, 18), reports[1].end_day)

    def test_month_end_adds_current_month_as_separate_report(self):
        reports = report_sender.scheduled_reports(date(2026, 9, 30))

        self.assertEqual(["daily", "monthly"], [item.period for item in reports])
        self.assertEqual(date(2026, 9, 1), reports[1].start_day)
        self.assertEqual(date(2026, 9, 30), reports[1].end_day)

    def test_friday_month_end_sends_three_independent_reports(self):
        reports = report_sender.scheduled_reports(date(2026, 7, 31))

        self.assertEqual(
            ["daily", "weekly", "monthly"],
            [item.period for item in reports],
        )
        self.assertEqual(
            (date(2026, 7, 30), date(2026, 7, 30)),
            (reports[0].start_day, reports[0].end_day),
        )
        self.assertEqual(
            (date(2026, 7, 27), date(2026, 7, 31)),
            (reports[1].start_day, reports[1].end_day),
        )
        self.assertEqual(
            (date(2026, 7, 1), date(2026, 7, 31)),
            (reports[2].start_day, reports[2].end_day),
        )

    def test_manual_period_can_be_previewed_on_any_day(self):
        weekly = report_sender.scheduled_reports(date(2026, 9, 23), "weekly")
        monthly = report_sender.scheduled_reports(date(2026, 9, 23), "monthly")

        self.assertEqual(
            (date(2026, 9, 21), date(2026, 9, 23)),
            (weekly[0].start_day, weekly[0].end_day),
        )
        self.assertEqual(
            (date(2026, 9, 1), date(2026, 9, 23)),
            (monthly[0].start_day, monthly[0].end_day),
        )


if __name__ == "__main__":
    unittest.main()
