#!/usr/bin/env bash
# =============================================================================
# 安装/更新“每天 10:00（北京时间，含周末）发送钉钉项目报告”的 cron 任务。
#
# 前置条件：
#   1. 项目 config/config.yaml 已配置 notifications.dingtalk_weekly_report；
#   2. 当前机器已安装并登录 dws，能以项目机器人发送消息；
#   3. 先手工验证：
#        python3 scripts/send_weekly_report.py --dry-run
#        python3 scripts/send_weekly_report.py
#
# 用法：
#   cd intent_behavior
#
#   # 默认使用当前项目目录、python3 和 PATH 中的 dws
#   bash scripts/install_weekly_report_cron.sh
#
#   # 指定远端项目路径、Python 和 dws 路径
#   bash scripts/install_weekly_report_cron.sh \
#     --project-dir /data0/xuanyu11/intent_behavior-git/weibo-self/intent_behavior \
#     --python /usr/bin/python3 \
#     --dws-runner /usr/local/bin/dws
#
# 行为：
#   - 添加或更新一条带唯一标记的 cron，不影响用户其他 cron；
#   - 使用 TZ=Asia/Shanghai，固定为每天 10:00（含周末）；
#   - 输出追加到 logs/weekly_report_cron.log；
#   - 不保存密码或 AppSecret。
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="python3"
DWS_RUNNER="dws"
MARKER="# intent_behavior_weekly_dingtalk_report"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --dws-runner) DWS_RUNNER="$2"; shift 2 ;;
    -h|--help)
      sed -n '1,31p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$PROJECT_DIR/scripts/send_weekly_report.py" ]]; then
  echo "未找到 $PROJECT_DIR/scripts/send_weekly_report.py" >&2
  exit 2
fi

mkdir -p "$PROJECT_DIR/logs"
CRON_TIMEZONE="CRON_TZ=Asia/Shanghai $MARKER"
CRON_LINE="0 10 * * * cd '$PROJECT_DIR' && DWS_RUNNER='$DWS_RUNNER' '$PYTHON_BIN' scripts/send_weekly_report.py >> logs/weekly_report_cron.log 2>&1 $MARKER"

EXISTING="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf '%s\n' "$EXISTING" | grep -Fv "$MARKER" || true)"
{
  printf '%s\n' "$FILTERED"
  printf '%s\n' "$CRON_TIMEZONE"
  printf '%s\n' "$CRON_LINE"
} | crontab -

echo "已安装/更新 cron：每天 10:00（Asia/Shanghai，含周末）发送钉钉报告。"
crontab -l | grep -F "$MARKER"
