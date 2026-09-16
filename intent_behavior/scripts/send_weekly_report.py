#!/usr/bin/env python3
"""
向钉钉个人单聊发送意图项目周报。

前置条件：
  1. 企业内部应用机器人已创建、配置为 Stream 模式并发布；
  2. 已申请“企业内机器人发送消息”权限；
  3. 当前执行机器已安装并登录 dws，且能使用该机器人；
  4. config/config.yaml 已配置 notifications.dingtalk_weekly_report。

配置优先级：
  - RobotCode、接收人、标题、统计天数：优先读取 config.yaml；
  - 同名环境变量可覆盖配置，便于临时换机器人或调试；
  - DWS_RUNNER 可指定 dws 可执行文件或包装器命令。

环境变量（可选覆盖）：
  DINGTALK_CHAT_ROBOT_CODE    RobotCode
  DINGTALK_RECIPIENT_USER_IDS 接收人 userId，多个以英文逗号分隔
  DINGTALK_REPORT_TITLE       Markdown 标题
  DINGTALK_REPORT_DAYS        统计天数
  DWS_RUNNER                  dws 可执行文件或包装器命令，默认 dws

运行方式：
  cd intent_behavior

  # 按 config.yaml 的 notifications.dingtalk_weekly_report 发送
  python3 scripts/send_weekly_report.py

  # 仅生成预览，不实际发送
  python3 scripts/send_weekly_report.py --dry-run

定时执行（每天 10:00，北京时间，含周末）：
  0 10 * * * cd /data0/xuanyu11/intent_behavior-git/weibo-self/intent_behavior && \
    /usr/bin/python3 scripts/send_weekly_report.py >> logs/weekly_report_cron.log 2>&1

说明：
  - 本脚本只发送，不创建机器人；
  - 发送前先生成最近 7 个自然日的 JSONL 审计报告。
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


PROJECT_DIR = Path(__file__).resolve().parents[1]
GENERATE_SCRIPT = PROJECT_DIR / "scripts" / "generate_weekly_report.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过钉钉企业机器人向个人单聊发送项目周报",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        default=str(PROJECT_DIR / "config" / "config.yaml"),
        help="项目配置文件路径",
    )
    parser.add_argument("--days", type=int, default=0, help="统计天数（0=读取配置）")
    parser.add_argument("--title", default="", help="消息标题（为空=读取配置）")
    parser.add_argument("--dry-run", action="store_true", help="只输出周报，不发送钉钉消息")
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
        weekly = _parse_weekly_report_config_without_pyyaml(content)
    else:
        config = yaml.safe_load(content) or {}
        weekly = config.get("notifications", {}).get("dingtalk_weekly_report", {})

    if not isinstance(weekly, dict):
        raise SystemExit("notifications.dingtalk_weekly_report 必须是对象")
    return weekly


def _parse_weekly_report_config_without_pyyaml(content: str) -> Dict[str, Any]:
    """
    仅解析本项目 notifications.dingtalk_weekly_report 的简单 YAML 子树。

    worker 本身仍推荐安装 requirements.txt 中的 PyYAML。本兜底仅保证独立周报
    发送脚本在最小 Python 环境下可运行，不把整个 config.yaml 解析器重复实现一遍。
    """
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


def main() -> None:
    args = parse_args()
    config = load_dingtalk_config(args.config)
    if not bool(config.get("enabled", False)):
        raise SystemExit("钉钉周报已在 config.yaml 中关闭，未发送任何消息。")

    robot_code = env_or_config("DINGTALK_CHAT_ROBOT_CODE", config.get("robot_code", ""))
    recipients = recipient_ids(config)
    title = args.title.strip() or env_or_config(
        "DINGTALK_REPORT_TITLE", config.get("title", "原生内容站项目周报")
    )
    days = args.days or int(
        env_or_config("DINGTALK_REPORT_DAYS", config.get("report_days", 7))
    )
    if not robot_code:
        raise SystemExit("缺少 RobotCode：请在 config.yaml 或 DINGTALK_CHAT_ROBOT_CODE 配置。")
    if not recipients:
        raise SystemExit(
            "缺少接收人：请在 config.yaml 或 DINGTALK_RECIPIENT_USER_IDS 配置。"
        )
    if days <= 0:
        raise SystemExit("--days / report_days 必须为正整数。")

    runner = shlex.split(os.getenv("DWS_RUNNER", "dws").strip() or "dws")
    report = subprocess.run(
        [sys.executable, str(GENERATE_SCRIPT), "--days", str(days)],
        cwd=str(PROJECT_DIR),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if args.dry_run:
        print(report, end="")
        return

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
    print("钉钉周报发送成功。")


if __name__ == "__main__":
    main()
