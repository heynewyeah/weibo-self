#!/bin/bash
# 自定义Shell只需执行本文件，避免在网页编辑器内维护多行Python。
set -euo pipefail
RELEASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$RELEASE_DIR"
sha256sum -c SHA256SUMS >/dev/null
ROOT='viewfs://c9/user_ext/ads_fst/intent_behavior_dataworks'
# Runtime stays at its verified location; data can use a separately verified root.
DATA_ROOT="${DATAWORKS_DATA_ROOT:-$ROOT}"
DATA_ROOT="${DATA_ROOT%/}"
ENV_ZIP="$ROOT/runtime/environment_20261016.zip"
ENV_SOURCE='hdfs://wbml-streaming-ns/wbml_data/waic_scheduler/resource/226/202610/1792136858405/intent_behavior_dataworks.zip'
ENV_SHA='7c4446c525d3edb6b2b403a9e207e283ea80935c6e29d26b5bb857e2e9d1cfd4'
hdfs dfs -mkdir -p "$ROOT/runtime"
hdfs dfs -chmod 700 "$ROOT/runtime"
if ! hdfs dfs -test -e "$ENV_ZIP"; then
  hdfs dfs -cp "$ENV_SOURCE" "$ENV_ZIP"
fi
hdfs dfs -chmod 600 "$ENV_ZIP"
hdfs dfs -get "$ENV_ZIP" environment.zip
echo "$ENV_SHA  environment.zip" | sha256sum -c -
unzip -q environment.zip -d environment
bash environment/intent_behavior_dataworks/app/scripts/dataworks_bundle_start.sh --check
export PYTHON_BIN="$RELEASE_DIR/environment/intent_behavior_dataworks/runtime/python/bin/python3"
export PYTHONNOUSERSITE=1 TZ=Asia/Shanghai
case "${1:-worker}" in
  worker)
    export DATAWORKS_LOG_HDFS="$DATA_ROOT/logs"
    export DATAWORKS_STATE_HDFS="$DATA_ROOT/state/retry_state.sqlite3"
    export DATAWORKS_LOG_SYNC_SEC=60
    exec bash app/run_dataworks.sh ;;
  special)
    shift
    export DATAWORKS_LOG_HDFS="$DATA_ROOT/special_888888/logs"
    export DATAWORKS_STATE_HDFS="$DATA_ROOT/special_888888/state/retry_state.sqlite3"
    export DATAWORKS_LOG_SYNC_SEC=60
    exec bash app/run_dataworks.sh --special "$@" ;;
  monitor|special-monitor)
    if [ "$1" = special-monitor ]; then DATA_ROOT="$DATA_ROOT/special_888888"; fi
    shift
    # Bash 4.2 treats an empty array as unbound under set -u.
    if [ -n "${MONITOR_MID:-}" ]; then set -- --mid "$MONITOR_MID" "$@"; fi
    if [ -n "${MONITOR_TASK_ID:-}" ]; then set -- --task-id "$MONITOR_TASK_ID" "$@"; fi
    exec "$PYTHON_BIN" -u app/scripts/dataworks_monitor.py --hdfs-root "$DATA_ROOT" "$@" ;;
  check)
    exec "$PYTHON_BIN" app/scripts/dataworks_runtime_check.py ;;
  *) echo 'Usage: dataworks_cshell_entry.sh [worker|special|monitor|special-monitor|check]' >&2; exit 2 ;;
esac
