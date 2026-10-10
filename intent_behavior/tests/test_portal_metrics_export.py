import importlib.util
import json
import sys
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_portal_metrics.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("export_portal_metrics", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_summary_matches_report_counting_rules(tmp_path):
    audit_dir = tmp_path / "runs"
    day_dir = audit_dir / "20260919"
    day_dir.mkdir(parents=True)
    events = [
        {"event": "mid_processed", "run_id": "r1", "task_id": "t", "record_id": "1", "mid": "m1", "status": "success", "layer": "L1", "industry": "科技", "media_type": "text", "forward_status": "原创", "timings_ms": {"total_ms": 100}},
        {"event": "mid_processed", "run_id": "r1", "task_id": "t", "record_id": "2", "mid": "m2", "status": "fallback", "error_stage": "model", "timings_ms": {"total_ms": 300}},
        {"event": "mid_processed", "run_id": "r2", "task_id": "t", "record_id": "2", "mid": "m2", "status": "failed"},
    ]
    path = day_dir / "run.jsonl"
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8")
    start = end = date(2026, 9, 19)
    data = MODULE.summarize(MODULE.iter_events(audit_dir, start, end), start, end, audit_dir)
    assert data["totals"]["processed"] == 3
    assert data["totals"]["unique"] == 2
    assert data["totals"]["success"] == 1
    assert data["totals"]["fallback"] == 1
    assert data["totals"]["failed"] == 1
    assert data["totals"]["closure_rate"] == 2 / 3
    assert data["totals"]["avg_ms"] == 200
    assert data["failure_stages"] == [{"name": "model", "value": 1}]


def test_period_ranges_use_natural_week_and_month():
    ranges = MODULE.period_ranges(date(2026, 9, 18))
    assert ranges["day"] == (date(2026, 9, 18), date(2026, 9, 18))
    assert ranges["week"] == (date(2026, 9, 14), date(2026, 9, 18))
    assert ranges["month"] == (date(2026, 9, 1), date(2026, 9, 18))


def test_portal_and_markdown_share_cross_day_counting_rules(tmp_path):
    from generate_weekly_report import build_report

    audit_dir = tmp_path / "runs"
    startup_dir = audit_dir / "20260928"
    startup_dir.mkdir(parents=True)
    events = [
        {"event": "mid_processed", "event_time": timestamp, "status": status, "run_id": "r1"}
        for timestamp, status in (
            ("2026-09-30T12:00:00", "success"),
            ("2026-10-01T12:00:00", "failed"),
            ("2026-10-05T12:00:00", "fallback"),
            ("2026-10-08T16:00:00Z", "success"),  # 北京时间 10.09
            ("2026-10-10T00:00:00", "success"),
        )
    ]
    (startup_dir / "worker.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )
    ranges = MODULE.period_ranges(date(2026, 10, 9))
    for period, count in (("day", 1), ("week", 2), ("month", 3)):
        start, end = ranges[period]
        selected = list(MODULE.iter_events(audit_dir, start, end))
        data = MODULE.summarize(selected, start, end, audit_dir)
        report = build_report(selected, start, end, audit_dir)
        assert data["totals"]["processed"] == count
        assert data["totals"]["success"] == 1
        assert data["trend"][-1]["date"] == "2026-10-09"
        assert data["trend"][-1]["success"] == 1
        assert f"- 处理尝试：{count}\n" in report
        assert "- 2026-10-09：1 条｜success 1" in report
