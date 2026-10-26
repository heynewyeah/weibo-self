#!/usr/bin/env bash
# 依赖包自动解压后：bash intent_behavior_dataworks/app/scripts/dataworks_bundle_start.sh --check
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_DIR="$(cd -- "$APP_DIR/.." && pwd)"
cd "$BUNDLE_DIR"
sha256sum --check --quiet SHA256SUMS
if [ ! -x runtime/python/bin/python3 ]; then
  tar -xzf runtime/python-linux.tar.gz -C runtime
fi
export PYTHON_BIN="$BUNDLE_DIR/runtime/python/bin/python3"
export PYTHONNOUSERSITE=1
unset PYTHONHOME PYTHONPATH
if [ ! -f runtime/.dependencies_ready ]; then
  "$PYTHON_BIN" -m pip install --disable-pip-version-check --no-index --require-hashes \
    --find-links "$BUNDLE_DIR/wheels" -r requirements.dataworks.lock
  touch runtime/.dependencies_ready
fi
if { [ "$#" -eq 0 ] || { [ "$#" -eq 1 ] && [ "$1" = --special ]; }; } && [ -z "${DATAWORKS_LOG_HDFS:-}" ]; then
  echo '正式持续运行必须设置 DATAWORKS_LOG_HDFS，以免任务目录清理后丢失审计。' >&2
  exit 2
fi
exec bash "$APP_DIR/run_dataworks.sh" "$@"
