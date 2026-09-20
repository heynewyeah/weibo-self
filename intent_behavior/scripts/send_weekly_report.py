#!/usr/bin/env python3
"""
按日历规则向钉钉个人单聊发送意图项目报告。

自动调度规则：
  - 每天发送 T-1 日报；
  - 每周五另外发送当周周一至周五的周汇总；
  - 每月最后一天另外发送当月 1 日至当天的月汇总。

周汇总和月汇总与日报是独立钉钉消息。如果周五同时是月末，
当天会依次发送日报、周汇总和月汇总三条消息。

前置条件：
  1. 企业内部应用机器人已创建、配置为 Stream 模式并发布；
  2. 已申请“企业内机器人发送消息”权限；
  3. 当前执行机器已安装并登录 dws，且能使用该机器人；
  4. config/config.yaml 已配置 notifications.dingtalk_weekly_report。

运行方式：
  cd intent_behavior

  # 按当天日历规则预览，不发送
  python3 scripts/send_weekly_report.py --dry-run

  # 预览指定日期会触发的报告，便于回归验证
  python3 scripts/send_weekly_report.py --run-date 2026-07-31 --dry-run

  # 手工预览某一类汇总（不受星期或月末条件限制）
  python3 scripts/send_weekly_report.py --period weekly --dry-run

定时执行（每天 10:00，北京时间，含周末）：
  0 10 * * * cd /data0/xuanyu11/intent_behavior-git/weibo-self/intent_behavior && \
    /usr/bin/python3 scripts/send_weekly_report.py >> logs/weekly_report_cron.log 2>&1

说明：
  - 本脚本只生成和发送报告，不创建机器人；
  - 文件名和配置节名保留 weekly_report 仅为兼容现有部署。
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_DIR = Path(__file__).resolve().parents[1]
GENERATE_SCRIPT = PROJECT_DIR / "scripts" / "generate_weekly_report.py"


@dataclass(frozen=True)
class ReportSpec:
    period: str
    report_name: str
    start_day: date
    end_day: date


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过钉钉企业机器人按日历规则发送项目报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        default=str(PROJECT_DIR / "config" / "config.yaml"),
        help="项目配置文件路径",
    )
    parser.add_argument(
        "--run-date",
        default="",
        help="调度日期，格式 YYYY-MM-DD；默认为配置时区的当天",
    )
    parser.add_argument(
        "--period",
        choices=("auto", "daily", "weekly", "monthly"),
        default="auto",
        help="auto=按日历规则；其他值用于手工生成指定类型",
    )
    parser.add_argument("--title", default="", help="手工覆盖消息标题")
    parser.add_argument("--dry-run", action="store_true", help="只输出报告，不发送钉钉消息")
    return parser.parse_args()


def load_dingtalk_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise SystemExit(f"读取配置失败: {path}: {exc}") from exc

    try:
        import yaml  # type: ignore
    except ImportError:
        report_config = _parse_weekly_report_config_without_pyyaml(content)
    else:
        config = yaml.safe_load(content) or {}
        report_config = config.get("notifications", {}).get("dingtalk_weekly_report", {})

    if not isinstance(report_config, dict):
        raise SystemExit("notifications.dingtalk_weekly_report 必须是对象")
    return report_config


def _parse_weekly_report_config_without_pyyaml(content: str) -> Dict[str, Any]:
    """仅解析本项目 notifications.dingtalk_weekly_report 的简单 YAML 子树。"""
    section_indent: int | None = None
    weekly_indent: int | None = None
    data: Dict[str, Any] = {}
    recipients: list[str] = []
    in_recipients = False

    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if stripped == "notifications:":
            section_indent = indent
            weekly_indent = None
            in_recipients = False
            continue
        if section_indent is None:
            continue
        if indent <= section_indent:
            break
        if stripped == "dingtalk_weekly_report:":
            weekly_indent = indent
            in_recipients = False
            continue
        if weekly_indent is None:
            continue
        if indent <= weekly_indent:
            break
        if stripped.startswith("- ") and in_recipients:
            recipients.append(stripped[2:].strip().strip("'\""))
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key, value = key.strip(), value.strip()
        in_recipients = key == "recipient_user_ids" and not value
        if in_recipients:
            continue
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if value.lower() in {"true", "false"}:
            data[key] = value.lower() == "true"
        elif value.isdigit():
            data[key] = int(value)
        else:
            data[key] = value

    if recipients:
        data["recipient_user_ids"] = recipients
    return data


def parse_run_date(value: str, timezone_name: str = "Asia/Shanghai") -> date:
    if not value:
        try:
            return datetime.now(ZoneInfo(timezone_name)).date()
        except ZoneInfoNotFoundError as exc:
            raise SystemExit(f"无效时区: {timezone_name}") from exc
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit("--run-date 必须是 YYYY-MM-DD，例如 2026-09-20") from exc


def is_month_last_day(day: date) -> bool:
    return (day + timedelta(days=1)).month != day.month


def scheduled_reports(run_day: date, period: str = "auto") -> List[ReportSpec]:
    daily_day = run_day - timedelta(days=1)
    daily = ReportSpec("daily", "日报", daily_day, daily_day)
    week_start = run_day - timedelta(days=run_day.weekday())
    weekly = ReportSpec("weekly", "周汇总", week_start, run_day)
    monthly = ReportSpec("monthly", "月汇总", run_day.replace(day=1), run_day)

    if period == "daily":
        return [daily]
    if period == "weekly":
        return [weekly]
    if period == "monthly":
        return [monthly]

    reports = [daily]
    if run_day.weekday() == 4:
        reports.append(weekly)
    if is_month_last_day(run_day):
        reports.append(monthly)
    return reports


def env_or_config(name: str, fallback: object) -> str:
    return os.getenv(name, "").strip() or str(fallback or "").strip()


def recipient_ids(config: dict) -> str:
    override = os.getenv("DINGTALK_RECIPIENT_USER_IDS", "").strip()
    if override:
        return override
    recipients = config.get("recipient_user_ids", [])
    if isinstance(recipients, str):
        return recipients.strip()
    if isinstance(recipients, list):
        return ",".join(str(item).strip() for item in recipients if str(item).strip())
    return ""


def report_title(config: dict, spec: ReportSpec, override: str = "") -> str:
    if override.strip():
        return override.strip()
    config_keys = {
        "daily": ("DINGTALK_DAILY_REPORT_TITLE", "daily_title", "原生内容站项目日报"),
        "weekly": ("DINGTALK_WEEKLY_REPORT_TITLE", "weekly_title", "原生内容站项目周汇总"),
        "monthly": ("DINGTALK_MONTHLY_REPORT_TITLE", "monthly_title", "原生内容站项目月汇总"),
    }
    env_name, config_key, default = config_keys[spec.period]
    return env_or_config(env_name, config.get(config_key, default))


def render_report(spec: ReportSpec) -> str:
    return subprocess.run(
        [
            sys.executable,
            str(GENERATE_SCRIPT),
            "--start-date",
            spec.start_day.isoformat(),
            "--end-date",
            spec.end_day.isoformat(),
            "--report-name",
            spec.report_name,
        ],
        cwd=str(PROJECT_DIR),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def send_report(
    runner: List[str],
    robot_code: str,
    recipients: str,
    title: str,
    report: str,
) -> None:
    command = runner + [
        "chat",
        "message",
        "send-by-bot",
        "--robot-code",
        robot_code,
        "--users",
        recipients,
        "--title",
        title,
        "--text",
        report,
        "--format",
        "json",
    ]
    subprocess.run(command, cwd=str(PROJECT_DIR), check=True)


def main() -> None:
    args = parse_args()
    config = load_dingtalk_config(args.config)
    if not bool(config.get("enabled", False)):
        raise SystemExit("钉钉报告已在 config.yaml 中关闭，未发送任何消息。")

    robot_code = env_or_config("DINGTALK_CHAT_ROBOT_CODE", config.get("robot_code", ""))
    recipients = recipient_ids(config)
    if not robot_code:
        raise SystemExit("缺少 RobotCode：请在 config.yaml 或 DINGTALK_CHAT_ROBOT_CODE 配置。")
    if not recipients:
        raise SystemExit(
            "缺少接收人：请在 config.yaml 或 DINGTALK_RECIPIENT_USER_IDS 配置。"
        )

    timezone_name = env_or_config(
        "DINGTALK_REPORT_TIMEZONE", config.get("timezone", "Asia/Shanghai")
    )
    run_day = parse_run_date(args.run_date, timezone_name)
    reports = scheduled_reports(run_day, args.period)
    runner = shlex.split(os.getenv("DWS_RUNNER", "dws").strip() or "dws")

    for index, spec in enumerate(reports):
        title = report_title(config, spec, args.title)
        report = render_report(spec)
        if args.dry_run:
            if index:
                print()
            print(f"=== {title} | {spec.start_day} ~ {spec.end_day} ===")
            print(report, end="")
            continue
        send_report(runner, robot_code, recipients, title, report)
        print(f"钉钉{spec.report_name}发送成功：{spec.start_day} ~ {spec.end_day}")


if __name__ == "__main__":
    main()
