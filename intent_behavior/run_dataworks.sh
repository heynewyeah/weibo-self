#!/usr/bin/env bash
# Dataworks SHELL 启动适配：定位项目目录后，前台运行唯一生产入口 worker.py。
# 输入：config/config.yaml；可用 PYTHON_BIN 指定已安装依赖的 Python 可执行文件。
# 输出：worker 日志和审计；执行后会真实分类并通过 HTTP 回写业务结果。
# 从服务器仓库根目录运行：cd intent_behavior && bash run_dataworks.sh
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

export TZ=Asia/Shanghai
export PYTHONUNBUFFERED=1
export DATAWORKS_MAX_LOOP_ERRORS="${DATAWORKS_MAX_LOOP_ERRORS:-3}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
export PYTHONNOUSERSITE=1
ENTRY=worker.py
SUPERVISOR_ENTRY=worker
if [ "${1:-}" = --special ]; then
  shift
  ENTRY=worker_special_task.py
  SUPERVISOR_ENTRY=special
  export DATAWORKS_SPECIAL_LOG_ROOT="$PROJECT_DIR/logs/special_888888"
fi
case "${1:-}" in
  --check)
    exec "$PYTHON_BIN" scripts/dataworks_runtime_check.py ;;
  --preflight)
    exec "$PYTHON_BIN" scripts/production_preflight.py --strict ;;
  --once)
    exec "$PYTHON_BIN" -u "$ENTRY" --config config/config.yaml "$@" ;;
  --dry-run)
    if [ "$SUPERVISOR_ENTRY" != special ]; then
      echo '--dry-run 仅用于 --special 特殊入口' >&2; exit 2
    fi
    exec "$PYTHON_BIN" -u "$ENTRY" --config config/config.yaml "$@" ;;
  "") ;;
  *) echo 'Usage: bash run_dataworks.sh [--special] [--check|--preflight|--once|--dry-run [--limit N]]' >&2; exit 2 ;;
esac

"$PYTHON_BIN" scripts/dataworks_runtime_check.py
# Dataworks 临时目录会被清理：由前台监督进程定期保存日志并转发终止信号。
if [ -n "${DATAWORKS_LOG_HDFS:-}" ]; then
  if [ "$SUPERVISOR_ENTRY" = special ]; then
    exec "$PYTHON_BIN" -u scripts/dataworks_supervisor.py --entry special
  fi
  exec "$PYTHON_BIN" -u scripts/dataworks_supervisor.py
fi
exec "$PYTHON_BIN" -u "$ENTRY" --config config/config.yaml
