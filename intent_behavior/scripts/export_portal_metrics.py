#!/usr/bin/env python3
"""以只读 JSON 输出门户所需的日、周、月聚合指标。

默认截止日为 Asia/Shanghai 的 T-1。脚本只读取 JSONL 审计与文件系统元数据，
不连接 MySQL、不调用模型，也不输出原始微博内容。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo

from generate_weekly_report import DEFAULT_AUDIT_DIR, directory_size, iter_events, percentile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="输出项目管理门户使用的只读聚合 JSON")
    parser.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument("--period", choices=("all", "day", "week", "month"), default="all")
    parser.add_argument("--end-date", default="", help="截止日 YYYY-MM-DD；默认北京时间 T-1")
    return parser.parse_args()


def default_end_day() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)


def period_ranges(end_day: date) -> Dict[str, Tuple[date, date]]:
    return {
        "day": (end_day, end_day),
        "week": (end_day - timedelta(days=end_day.weekday()), end_day),
        "month": (end_day.replace(day=1), end_day),
    }


def _distribution(counter: Counter) -> List[Dict[str, Any]]:
    return [{"name": str(name or "未填写"), "value": int(value)} for name, value in counter.most_common()]


def summarize(events: Iterable[Dict[str, Any]], start_day: date, end_day: date, audit_dir: Path) -> Dict[str, Any]:
    processed = 0
    unique_mids = set()
    status_counts: Counter = Counter()
    distributions = {key: Counter() for key in ("category", "industry", "media_type", "repost_status")}
    failure_stages: Counter = Counter()
    daily: Dict[str, Counter] = defaultdict(Counter)
    timings: List[float] = []
    run_ids = set()

    for event in events:
        run_id = str(event.get("run_id") or "")
        if run_id:
            run_ids.add(run_id)
        if event.get("event") != "mid_processed":
            continue
        processed += 1
        status = str(event.get("status") or "unknown")
        status_counts[status] += 1
        day = str(event.get("_audit_day") or event.get("event_time", "")[:10] or "未知日期")
        daily[day][status] += 1
        mid = str(event.get("mid") or "")
        if mid:
            unique_mids.add((str(event.get("task_id") or ""), str(event.get("record_id") or ""), mid))
        for source, target in (("layer", "category"), ("industry", "industry"), ("media_type", "media_type"), ("forward_status", "repost_status")):
            value = str(event.get(source) or "")
            if value:
                distributions[target][value] += 1
        failure = str(event.get("error_stage") or "")
        if failure and failure != "none":
            failure_stages[failure] += 1
        try:
            total_ms = (event.get("timings_ms") or {}).get("total_ms")
            if total_ms is not None:
                timings.append(float(total_ms))
        except (TypeError, ValueError):
            pass

    success = status_counts["success"]
    fallback = status_counts["fallback"]
    totals = {
        "processed": processed,
        "unique": len(unique_mids),
        "success": success,
        "fallback": fallback,
        "failed": status_counts["failed"],
        "interrupted": status_counts["interrupted"],
        "closure_rate": (success + fallback) / processed if processed else 0.0,
        "avg_ms": sum(timings) / len(timings) if timings else None,
        "p50_ms": percentile(timings, 0.50),
        "p95_ms": percentile(timings, 0.95),
    }
    trend = []
    current = start_day
    while current <= end_day:
        counts = daily[current.isoformat()]
        trend.append({"date": current.isoformat(), "success": counts["success"], "fallback": counts["fallback"], "failed": counts["failed"], "interrupted": counts["interrupted"]})
        current += timedelta(days=1)
    disk = shutil.disk_usage(_existing_parent(audit_dir))
    latest_mtime = _latest_mtime(audit_dir)
    label = start_day.isoformat() if start_day == end_day else f"{start_day.isoformat()} ~ {end_day.isoformat()}"
    return {
        "period_label": label,
        "totals": totals,
        "trend": trend,
        "distributions": {key: _distribution(value) for key, value in distributions.items()},
        "failure_stages": _distribution(failure_stages),
        "operations": {
            "worker": f"{len(run_ids)} 个运行实例",
            "audit": latest_mtime.isoformat() if latest_mtime else "未发现审计文件",
            "disk": f"使用率 {disk.used / disk.total * 100:.1f}% · 审计 {directory_size(audit_dir)} bytes",
        },
    }


def _existing_parent(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _latest_mtime(path: Path):
    if not path.is_dir():
        return None
    values = []
    for item in path.rglob("*.jsonl"):
        try:
            values.append(item.stat().st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(max(values), tz=ZoneInfo("Asia/Shanghai")) if values else None


def main() -> None:
    args = parse_args()
    try:
        end_day = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else default_end_day()
    except ValueError as exc:
        raise SystemExit("--end-date 必须为 YYYY-MM-DD") from exc
    audit_dir = Path(args.audit_dir).expanduser().resolve()
    ranges = period_ranges(end_day)
    selected = ranges if args.period == "all" else {args.period: ranges[args.period]}
    periods = {name: summarize(iter_events(audit_dir, start, end), start, end, audit_dir) for name, (start, end) in selected.items()}
    json.dump({"schema_version": 1, "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "timezone": "Asia/Shanghai", "periods": periods}, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    print()


if __name__ == "__main__":
    main()
