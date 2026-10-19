#!/bin/bash
# 在 Dataworks 的实际运行身份下检查个人目录；不停止 worker、不修改已有数据或权限。
set -uo pipefail
echo HDFS_IDENTITY
id
hdfs getconf -confKey fs.defaultFS
for parent in /dw_ext/ad/person/xuanyu11; do
  echo "HDFS_PARENT $parent"
  hdfs dfs -ls -d "$parent" 2>&1
done
if [ "$#" -eq 0 ]; then
  set -- viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior
fi
for target do
  echo "HDFS_CANDIDATE $target"
  hdfs dfs -ls -d "$target" 2>&1
  if ! hdfs dfs -test -d "$target"; then
    echo HDFS_CANDIDATE_MISSING_OR_INACCESSIBLE
    continue
  fi
  hdfs dfs -getfacl "$target" 2>&1
  # Legacy Hadoop clients may not implement -test -w; verify actual operations.
  probe="$target/.dataworks_permission_probe_$(date +%Y%m%d_%H%M%S)_$$"
  marker="ads_fst Dataworks write/read/rename permission probe"
  if hdfs dfs -mkdir "$probe" && printf '%s\n' "$marker" | hdfs dfs -put - "$probe/check.txt"; then
    if hdfs dfs -mv "$probe/check.txt" "$probe/verified.txt" && hdfs dfs -cat "$probe/verified.txt"; then
      echo "HDFS_PERMISSION_WRITE_READ_RENAME_OK $target"
      echo "HDFS_PROBE_RETAINED $probe/verified.txt"
    fi
  fi
done
echo HDFS_PROBE_FINISHED
