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
