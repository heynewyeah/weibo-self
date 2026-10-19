#!/bin/bash
# 默认在线预复制；--promote 仅在旧实例已结束后使用，将整份副本原子发布到dataworks子目录。
set -euo pipefail
SOURCE='viewfs://c9/user_ext/ads_fst/intent_behavior_dataworks'
TARGET='viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior'
STAGING="$TARGET/.dataworks_migration_$(date +%Y%m%d_%H%M%S)_$$"
LOCAL_TMP="$(mktemp -d)"
case "${1:-}" in
  '') ;;
  --promote)
    if hdfs dfs -test -e "$TARGET/dataworks"; then
      echo 'MIGRATION_TARGET_EXISTS: refusing to overwrite a prior cutover' >&2
      exit 2
    fi ;;
  *) echo 'Usage: dataworks_hdfs_precopy.sh [--promote]' >&2; exit 2 ;;
esac
echo "MIGRATION_STAGING $STAGING"
hdfs dfs -test -e "$SOURCE/state/retry_state.sqlite3"
hdfs dfs -mkdir "$STAGING"
for category in logs state; do
  hdfs dfs -mkdir "$STAGING/$category"
  hdfs dfs -ls "$SOURCE/$category" > "$LOCAL_TMP/$category.list"
  awk '$1 ~ /^-/ {print $NF}' "$LOCAL_TMP/$category.list" > "$LOCAL_TMP/$category.files"
  while IFS= read -r remote; do
    name="${remote##*/}"
    case "$category/$name" in
      logs/*.tar.gz|logs/status.json|state/retry_state.sqlite3) ;;
      *) echo "MIGRATION_SKIP_TEMP_OR_UNKNOWN $category/$name"; continue ;;
    esac
    # 下载固定版本后再上传，不对在线源文件做移动/重命名/删除。
    hdfs dfs -get "$remote" "$LOCAL_TMP/source_$name"
    expected="$(sha256sum "$LOCAL_TMP/source_$name" | awk '{print $1}')"
    hdfs dfs -put "$LOCAL_TMP/source_$name" "$STAGING/$category/$name"
    hdfs dfs -get "$STAGING/$category/$name" "$LOCAL_TMP/verify_$name"
    actual="$(sha256sum "$LOCAL_TMP/verify_$name" | awk '{print $1}')"
    test "$expected" = "$actual"
    if [ "$category/$name" = state/retry_state.sqlite3 ]; then
      python -c 'import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); check=c.execute("PRAGMA quick_check").fetchone()[0]; assert check=="ok",check; counts=dict(c.execute("SELECT status,COUNT(*) FROM retry_state GROUP BY status")); print("MIGRATION_RETRY_INTEGRITY_OK "+json.dumps(counts)); c.close()' "$LOCAL_TMP/verify_$name"
    elif [ "$category" = logs ] && [ "$name" != status.json ]; then
      tar -tzf "$LOCAL_TMP/verify_$name" >/dev/null
    fi
    echo "MIGRATION_VERIFIED $category/$name $actual"
  done < "$LOCAL_TMP/$category.files"
done
echo "MIGRATION_PRECOPY_COMPLETE $STAGING"
if [ "${1:-}" = --promote ]; then
  hdfs dfs -mv "$STAGING" "$TARGET/dataworks"
  echo "MIGRATION_PROMOTED $TARGET/dataworks"
else
  echo 'SOURCE_UNCHANGED: current worker still writes its original destination; cutover is not complete.'
fi
