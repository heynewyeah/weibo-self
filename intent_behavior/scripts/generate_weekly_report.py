#!/usr/bin/env python3
"""
生成原生内容站 AI 意图分层项目的周运行报告（只读）。

数据来源：
  logs/runs/YYYYMMDD/<run_id>.jsonl
  该文件由 src/audit.py 在每个 mid 处理结束时写入。脚本不连接 MySQL、
  不调用模型、不回写 level，因此可以安全地在正式环境定时运行。

统计内容：
  - 处理量、去重 mid 数、success / fallback / failed / interrupted；
  - 分类层级、行业、媒体、转发状态分布；
  - 耗时均值、P50、P95、最大值；
  - 失败阶段 Top、按天趋势、任务数、运行数；
  - JSONL 审计目录占用和当前磁盘可用空间。

运行方式：
  cd intent_behavior

  # 生成最近 7 个自然日（含今天）的 Markdown 周报并输出到终端
  python3 scripts/generate_weekly_report.py

  # 指定统计窗口，并写到文件
  python3 scripts/generate_weekly_report.py --days 7 \
    --output output/weekly_report.md

  # 截止到指定日期（含该日），便于复盘历史周
  python3 scripts/generate_weekly_report.py --end-date 2026-09-17

说明：
  - 没有 JSONL 审计数据时，脚本会明确显示“暂无正式运行审计数据”，不会把
    旧的手工测试日志误当成正式运行量。
  - fallback 代表反解/模型等业务异常已按规则回写 level=6；它与成功分类分开统计。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_DIR = PROJECT_DIR / "logs" / "runs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成原生内容站 AI 意图分层项目的只读周运行报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--audit-dir",
        default=str(DEFAULT_AUDIT_DIR),
        help="JSONL 审计根目录，默认 logs/runs",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="统计最近多少个自然日（含截止日，默认 7）",
    )
    parser.add_argument(
        "--end-date",
        default="",
        help="统计截止日，格式 YYYY-MM-DD；不传时为今天",
    )
    parser.add_argument(
        "--output",
        default="",
        help="可选：将 Markdown 周报写到指定文件；不传则输出到终端",
    )
    return parser.parse_args()


def parse_end_date(value: str) -> date:
    if not value:
        return datetime.now().date()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit("--end-date 必须是 YYYY-MM-DD，例如 2026-09-17") from exc


def date_range(end_day: date, days: int) -> Tuple[date, date]:
    if days <= 0:
        raise SystemExit("--days 必须为正整数")
    return end_day - timedelta(days=days - 1), end_day


def iter_events(audit_dir: Path, start_day: date, end_day: date) -> Iterable[Dict[str, Any]]:
    if not audit_dir.is_dir():
        return
    for day_dir in sorted(audit_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y%m%d").date()
        except ValueError:
            continue
        if day < start_day or day > end_day:
            continue
        for path in sorted(day_dir.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line_no, raw in enumerate(handle, 1):
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            print(
                                f"[WARN] 跳过损坏 JSONL: {path}:{line_no}",
                                file=sys.stderr,
                            )
                            continue
                        if isinstance(event, dict):
                            event["_audit_day"] = day.isoformat()
                            event["_audit_file"] = str(path)
                            yield event
            except OSError as exc:
                print(f"[WARN] 读取审计文件失败: {path}: {exc}", file=sys.stderr)


def percentile(values: List[float], p: float) -> Optional[float]:
    """线性插值百分位；样本少时仍返回稳定可读的结果。"""
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def safe_counter_percent(count: int, total: int) -> str:
    return f"{count / total * 100:.1f}%" if total else "0.0%"


def fmt_ms(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def fmt_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(max(value, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TB"


def counter_lines(counter: Counter, total: int, empty_text: str = "暂无") -> List[str]:
    if not counter:
        return [f"- {empty_text}"]
    return [
        f"- {name or '未填写'}：{count}（{safe_counter_percent(count, total)}）"
        for name, count in counter.most_common()
    ]


def build_report(
    events: Iterable[Dict[str, Any]],
    start_day: date,
    end_day: date,
    audit_dir: Path,
) -> str:
    result_events: List[Dict[str, Any]] = []
    runs: set[str] = set()
    task_ids: set[str] = set()
    unique_mids: set[Tuple[str, str, str]] = set()
    status_counts: Counter = Counter()
    layer_counts: Counter = Counter()
    industry_counts: Counter = Counter()
    media_counts: Counter = Counter()
    forward_counts: Counter = Counter()
    error_stage_counts: Counter = Counter()
    daily_counts: Dict[str, Counter] = defaultdict(Counter)
    timings: List[float] = []

    for event in events:
        run_id = str(event.get("run_id") or "")
        if run_id:
            runs.add(run_id)
        if event.get("event") != "mid_processed":
            continue

        result_events.append(event)
        day = str(event.get("_audit_day") or event.get("event_time", "")[:10] or "未知日期")
        status = str(event.get("status") or "unknown")
        mid = str(event.get("mid") or "")
        task_id = str(event.get("task_id") or "")
        record_id = str(event.get("record_id") or "")
        status_counts[status] += 1
        daily_counts[day][status] += 1
        if task_id:
            task_ids.add(task_id)
        if mid:
            unique_mids.add((task_id, record_id, mid))
        layer = str(event.get("layer") or "")
        if layer:
            layer_counts[layer] += 1
        industry = str(event.get("industry") or "")
        if industry:
            industry_counts[industry] += 1
        media_type = str(event.get("media_type") or "")
        if media_type:
            media_counts[media_type] += 1
        forward_status = str(event.get("forward_status") or "")
        if forward_status:
            forward_counts[forward_status] += 1
        error_stage = str(event.get("error_stage") or "")
        if error_stage and error_stage != "none":
            error_stage_counts[error_stage] += 1
        total_ms = (event.get("timings_ms") or {}).get("total_ms")
        try:
            if total_ms is not None:
                timings.append(float(total_ms))
        except (TypeError, ValueError):
            pass

    processed = len(result_events)
    success = status_counts["success"]
    fallback = status_counts["fallback"]
    failed = status_counts["failed"]
    interrupted = status_counts["interrupted"]
    completed = success + fallback
    audit_size = directory_size(audit_dir)
    disk = disk_usage(audit_dir)

    lines = [
        f"# 原生内容站 AI 意图分层周报（{start_day.isoformat()} ~ {end_day.isoformat()}）",
        "",
        "## 运行概览",
        f"- 处理尝试：{processed}",
        f"- 去重博文：{len(unique_mids)}",
        f"- 运行实例：{len(runs)}",
        f"- 覆盖任务：{len(task_ids)}",
        f"- 正常分类 success：{success}（{safe_counter_percent(success, processed)}）",
        f"- 失败兜底 fallback（已回写 level=6）：{fallback}（{safe_counter_percent(fallback, processed)}）",
        f"- 未闭环失败 failed：{failed}（{safe_counter_percent(failed, processed)}）",
        f"- 人工中断 interrupted：{interrupted}（{safe_counter_percent(interrupted, processed)}）",
        f"- 处理闭环率（success + fallback）：{safe_counter_percent(completed, processed)}",
        "",
        "## 性能",
        f"- 总耗时样本：{len(timings)}",
        f"- 平均耗时：{fmt_ms(sum(timings) / len(timings) if timings else None)}",
        f"- P50：{fmt_ms(percentile(timings, 0.50))}",
        f"- P95：{fmt_ms(percentile(timings, 0.95))}",
        f"- 最大值：{fmt_ms(max(timings) if timings else None)}",
        "",
        "## 分类与内容分布",
        "### 分类层级",
        *counter_lines(layer_counts, processed),
        "### 行业",
        *counter_lines(industry_counts, processed),
        "### 媒体类型",
        *counter_lines(media_counts, processed),
        "### 转发状态",
        *counter_lines(forward_counts, processed),
        "",
        "## 异常与风险",
    ]

    if error_stage_counts:
        lines.extend(["### 失败阶段"])
        lines.extend(counter_lines(error_stage_counts, processed))
    else:
        lines.append("- 本统计窗口内无结构化错误阶段记录。")
    if not processed:
        lines.extend([
            "- 当前没有可用于周报的 JSONL 审计记录。",
            "- 请确认 `audit.local_enabled: true`，或启用并验证 MySQL 审计表后扩展统计来源。",
        ])

    lines.extend([
        "",
        "## 按天趋势",
    ])
    for offset in range((end_day - start_day).days + 1):
        current = start_day + timedelta(days=offset)
        day_key = current.isoformat()
        counts = daily_counts.get(day_key, Counter())
        total = sum(counts.values())
        lines.append(
            f"- {day_key}：{total} 条"
            f"｜success {counts['success']}"
            f"｜fallback {counts['fallback']}"
            f"｜failed {counts['failed']}"
            f"｜interrupted {counts['interrupted']}"
        )

    lines.extend([
        "",
        "## 审计与存储健康度",
        f"- JSONL 审计目录：`{audit_dir}`",
        f"- JSONL 目录占用：{fmt_bytes(audit_size)}",
        f"- 所在磁盘可用空间：{fmt_bytes(disk.free)} / {fmt_bytes(disk.total)}"
        f"（使用率 {disk.used / disk.total * 100:.1f}%）",
        "",
        "> 口径说明：success 为正常分类并回写；fallback 为处理异常但已按业务规则回写 level=6；"
        "failed 为未闭环失败；interrupted 为人工 Ctrl+C 中止，mid 应保持 level=0。",
    ])
    return "\n".join(lines) + "\n"


def directory_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def disk_usage(path: Path) -> shutil._ntuple_diskusage:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe)


def main() -> None:
    args = parse_args()
    end_day = parse_end_date(args.end_date)
    start_day, end_day = date_range(end_day, args.days)
    audit_dir = Path(args.audit_dir).expanduser().resolve()
    report = build_report(iter_events(audit_dir, start_day, end_day), start_day, end_day, audit_dir)

    if args.output:
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = (PROJECT_DIR / output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(f"周报已写入: {output}")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
