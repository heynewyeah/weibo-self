#!/usr/bin/env bash
# =============================================================================
# 安装/更新“每天 10:00（北京时间，含周末）发送钉钉项目报告”的 cron 任务。
# 发送脚本每天发 T-1 日报，周五另发当周汇总，月末另发当月汇总。
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
#   # 必须先 su - ads_fst，脚本会核对当前 Linux 用户。
#   bash scripts/install_weekly_report_cron.sh
#   # 若误装进其他用户 crontab，仅备份并移除当前用户的本项目定时块：
#   bash scripts/install_weekly_report_cron.sh --remove
#
#   # 指定远端项目路径、Python 和 dws 路径
#   bash scripts/install_weekly_report_cron.sh \
#     --project-dir /data0/xuanyu11/intent_behavior-git/weibo-self/intent_behavior \
#     --python /usr/bin/python3 \
#     --hdfs-runner /path/to/hadoop/bin/hdfs \
#     --dws-runner /usr/local/bin/dws
#
# 行为：
#   - 添加或更新一个带唯一起止标记的 cron 配置块，不影响其他 cron；
#   - 使用 TZ=Asia/Shanghai，固定为每天 10:00（含周末）；
#   - 输出追加到 logs/weekly_report_cron.log；
#   - 不保存密码或 AppSecret。
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="python3"
DWS_RUNNER="dws"
HDFS_RUNNER="${HDFS_RUNNER:-hdfs}"
EXPECTED_USER="ads_fst"
REMOVE_ONLY=false
BLOCK_BEGIN="# BEGIN intent_behavior_dingtalk_report"
BLOCK_END="# END intent_behavior_dingtalk_report"
LEGACY_MARKER="# intent_behavior_weekly_dingtalk_report"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --dws-runner) DWS_RUNNER="$2"; shift 2 ;;
    --hdfs-runner) HDFS_RUNNER="$2"; shift 2 ;;
    --expected-user) EXPECTED_USER="$2"; shift 2 ;;
    --remove) REMOVE_ONLY=true; shift ;;
    -h|--help)
      sed -n '1,35p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 2
      ;;
  esac
done

CURRENT_USER="$(id -un)"
if [[ "$REMOVE_ONLY" == false && "$CURRENT_USER" != "$EXPECTED_USER" ]]; then
  echo "必须以 ${EXPECTED_USER} 安装报告 cron，当前是 ${CURRENT_USER}。未修改 cron；请先 su - ${EXPECTED_USER}。" >&2
  exit 2
fi

if [[ ! -f "$PROJECT_DIR/scripts/send_weekly_report.py" ]]; then
  echo "未找到 $PROJECT_DIR/scripts/send_weekly_report.py" >&2
  exit 2
fi

EXISTING="$(crontab -l 2>/dev/null || true)"
FILTERED="$(
  printf '%s\n' "$EXISTING" \
    | sed "/^${BLOCK_BEGIN}$/,/^${BLOCK_END}$/d" \
    | grep -Fv "$LEGACY_MARKER" \
    || true
)"
if [[ "$REMOVE_ONLY" == true ]]; then
  BACKUP_FILE="$(mktemp "${TMPDIR:-/tmp}/intent-behavior-report-cron.XXXXXX")"
  chmod 600 "$BACKUP_FILE"
  printf '%s\n' "$EXISTING" > "$BACKUP_FILE"
  printf '%s\n' "$FILTERED" | crontab -
  echo "已仅移除当前用户 $CURRENT_USER 的本项目报告定时块，其他任务保留。"
  echo "原 crontab 可从备份恢复：$BACKUP_FILE"
  exit 0
fi

mkdir -p "$PROJECT_DIR/logs"
if ! HDFS_EXECUTABLE="$(command -v "$HDFS_RUNNER")"; then
  echo "未找到 HDFS 客户端；请先确认 ads_fst 的访问环境，或传 --hdfs-runner 完整路径。未修改 cron。" >&2
  exit 2
fi
HDFS_RUNNER="$HDFS_EXECUTABLE"
CRON_TIMEZONE="CRON_TZ=Asia/Shanghai"
CRON_LINE="0 10 * * * cd '$PROJECT_DIR' && HDFS_RUNNER='$HDFS_RUNNER' DWS_RUNNER='$DWS_RUNNER' '$PYTHON_BIN' scripts/send_weekly_report.py >> logs/weekly_report_cron.log 2>&1"

{
  printf '%s\n' "$FILTERED"
  printf '%s\n' "$BLOCK_BEGIN"
  printf '%s\n' "$CRON_TIMEZONE"
  printf '%s\n' "$CRON_LINE"
  printf '%s\n' "$BLOCK_END"
} | crontab -

echo "已安装/更新 cron：每天 10:00（Asia/Shanghai）发 T-1 日报，周五/月末额外发汇总。"
crontab -l | sed -n "/^${BLOCK_BEGIN}$/,/^${BLOCK_END}$/p"
