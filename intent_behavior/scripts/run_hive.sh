#!/bin/sh

##默认日期
DEFAULT_START_DAY="20260701"
DEFAULT_END_DAY="20260731"

##参数解析
if [ $# -ge 2 ]; then
 START_DAY="$1"
 END_DAY="$2"
 echo "传入参数: START_DAY=${START_DAY}, END_DAY=${END_DAY}"
elif [ $# -eq 1 ]; then
 START_DAY="$1"
 END_DAY="$1"
 echo "单日期参数: START_DAY=${START_DAY}, END_DAY=${END_DAY}"
else
 START_DAY="${DEFAULT_START_DAY}"
 END_DAY="${DEFAULT_END_DAY}"
 echo "使用默认日期: START_DAY=${START_DAY}, END_DAY=${END_DAY}"
fi

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TASK_NAME="car_weibo_ad"
HDFS_TARGET_PATH="/dw_ext/ad/person/xuanyu11/intent_behavior/data/${TASK_NAME}_${START_DAY}_${END_DAY}"
ERROR_LOG="/tmp/hive_error_${TASK_NAME}_${START_DAY}_${END_DAY}.log"

echo "====================配置信息 ===================="
echo "脚本目录: ${SCRIPT_DIR}"
echo "HDFS输出路径: ${HDFS_TARGET_PATH}"
echo "查询日期: ${START_DAY} ~ ${END_DAY}"
echo "错误日志: ${ERROR_LOG}"
echo "=================================================="

## Hive主体SQL（子查询预过滤 + dt关联优化）
hive_sql_main="
SELECT DISTINCT
 t.mid,
 t.content,
 t.dt
FROM (
    SELECT DISTINCT mid, dt
    FROM dplus_dm.dm_wb_ad_sfst_multi_day
    WHERE dt >= '${START_DAY}' AND dt <= '${END_DAY}'
      AND market_industry_name = '汽车'
      AND bid_type =4
) ad
INNER JOIN ods_tblog_content t
ON ad.mid = t.mid AND ad.dt = t.dt
WHERE t.dt >= '${START_DAY}' AND t.dt <= '${END_DAY}'
"

## Hive2.3.7 纯兼容参数，无任何不存在配置项
head_tmp_sql="
SET hive.metastore.client.socket.timeout=600;
SET hive.stats.fetch.column.stats=false;
SET hive.stats.fetch.partition.stats=false;
SET hive.exec.dynamic.partition=true;
SET hive.exec.dynamic.partition.mode=nonstrict;
SET hive.exec.max.dynamic.partitions=10000;
SET hive.exec.max.dynamic.partitions.pernode=5000;
SET mapreduce.job.reduces=50;
SET mapreduce.map.memory.mb=4096;
SET mapreduce.map.java.opts=-Xmx3096M;
SET mapreduce.reduce.memory.mb=4096;
SET mapreduce.reduce.java.opts=-Xmx3096M;
SET hive.merge.mapfiles=true;
SET hive.merge.mapredfiles=true;
SET hive.merge.size.per.task=256000000;
SET hive.merge.smallfiles.avgsize=64000000;
SET hive.input.format=org.apache.hadoop.hive.ql.io.CombineHiveInputFormat;
SET hive.cli.print.header=true;
SET hive.exec.compress.output=false;
SET mapred.output.compress=false;
"

tmp_sql="${head_tmp_sql}
INSERT OVERWRITE DIRECTORY '${HDFS_TARGET_PATH}'
ROW FORMAT DELIMITED
FIELDS TERMINATED BY '\t'
STORED AS TEXTFILE
${hive_sql_main}"

echo "====================执行 SQL ===================="
printf '%s\n' "$tmp_sql"
echo "=================================================="
echo ">>>> 任务开始运行，实时进度将持续输出至屏幕 <<<<"

export HADOOP_CLIENT_OPTS="-Xmx2048m $HADOOP_CLIENT_OPTS"
hive -v -e "$tmp_sql" 2>&1 | tee "${ERROR_LOG}"
hive_ret=${PIPESTATUS[0]}

if [ ${hive_ret} -ne 0 ]; then
 echo -e "\n[ERROR] Hive执行失败！错误日志路径：${ERROR_LOG}"
 exit 1
else
 echo -e "\n[SUCCESS] 任务执行完成"
 echo "====================HDFS结果检查 ===================="
 hdfs dfs -ls ${HDFS_TARGET_PATH}/part-*
 echo "输出路径：${HDFS_TARGET_PATH}"
 echo "=================================================="
 exit 0
fi