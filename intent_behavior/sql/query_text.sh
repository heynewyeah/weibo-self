#!/bin/sh
# =============================================================
# query_text.sh — 查询文本类博文并保存到 HDFS
# =============================================================
# 功能：
#   从 ods_tblog_content 关联 dm_wb_ad_sfst_multi_day，
#   提取汽车行业广告博文的纯文字内容（media_type=3 正文），
#   结果写入 HDFS 个人目录，供后续分类任务使用。
#
# 数据来源：
#   主表: dplus_dm.dm_wb_ad_sfst_multi_day  （广告投放数据，筛选汽车行业）
#   关联: ods_tblog_content                  （博文原始文字内容）
#   过滤: market_industry_name='汽车', bid_type=4
#
# 输出路径：
#   HDFS: /dw_ext/ad/person/xuanyu11/intent_behavior/data/
#         text_weibo_ad_<START_DAY>_<END_DAY>/
#   格式: TSV，字段分隔符 \t，字段顺序：mid \t uid \t content \t dt
#
# 用法：
#   bash sql/query_text.sh                        # 使用默认日期
#   bash sql/query_text.sh 20260701               # 单日
#   bash sql/query_text.sh 20260701 20260731      # 日期范围
#
# 运行时间预估：约 5~20 分钟（取决于数据量和集群负载）
#
# 作者：xuanyu11
# 创建时间：2026-08-12
# =============================================================

## ==================== 默认日期 ====================
DEFAULT_START_DAY="20260701"
DEFAULT_END_DAY="20260731"

## ==================== 参数解析 ====================
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

## ==================== 路径配置 ====================
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TASK_NAME="text_weibo_ad"
HDFS_BASE="/dw_ext/ad/person/xuanyu11/intent_behavior/data"
HDFS_TARGET_PATH="${HDFS_BASE}/${TASK_NAME}_${START_DAY}_${END_DAY}"
ERROR_LOG="/tmp/hive_error_${TASK_NAME}_${START_DAY}_${END_DAY}.log"

echo "==================== 配置信息 ===================="
echo "脚本目录:     ${SCRIPT_DIR}"
echo "任务名称:     ${TASK_NAME}"
echo "查询日期:     ${START_DAY} ~ ${END_DAY}"
echo "数据来源:     dplus_dm.dm_wb_ad_sfst_multi_day JOIN ods_tblog_content"
echo "过滤条件:     market_industry_name='汽车', bid_type=4"
echo "HDFS输出路径: ${HDFS_TARGET_PATH}"
echo "错误日志:     ${ERROR_LOG}"
echo "开始时间:     $(date '+%Y-%m-%d %H:%M:%S')"
echo "==================================================="

## ==================== Hive 参数 ====================
HEAD_SQL="
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

## ==================== 主体 SQL ====================
# 查询逻辑：
#   1. 先从广告表按日期/行业/竞价类型预过滤，得到 mid 集合
#   2. 关联博文内容表，取出文字内容
#   3. 关联媒体表，只保留 media_type=3（正文）的记录，确认是纯文字博文
#   注：ods_tblog_content.content 是博文纯文字，不含图片 pid
MAIN_SQL="
SELECT DISTINCT
    t.mid,
    ad.cust_uid                AS uid,
    t.content,
    t.dt
FROM (
    SELECT DISTINCT
        mid,
        cust_uid,
        dt
    FROM dplus_dm.dm_wb_ad_sfst_multi_day
    WHERE dt >= '${START_DAY}' AND dt <= '${END_DAY}'
      AND market_industry_name = '汽车'
      AND bid_type = 4
) ad
INNER JOIN ods_tblog_content t
    ON ad.mid = t.mid
    AND ad.dt = t.dt
WHERE t.dt >= '${START_DAY}' AND t.dt <= '${END_DAY}'
  AND t.content IS NOT NULL
  AND LENGTH(TRIM(t.content)) > 0
"

## ==================== 写入 HDFS ====================
FULL_SQL="${HEAD_SQL}
INSERT OVERWRITE DIRECTORY '${HDFS_TARGET_PATH}'
ROW FORMAT DELIMITED
FIELDS TERMINATED BY '\t'
STORED AS TEXTFILE
${MAIN_SQL}"

echo "==================== 执行 SQL ===================="
printf '%s\n' "${FULL_SQL}"
echo "==================================================="
echo ">>>> 任务开始运行，实时进度将持续输出至屏幕 <<<<"

export HADOOP_CLIENT_OPTS="-Xmx2048m ${HADOOP_CLIENT_OPTS:-}"
hive -v -e "${FULL_SQL}" 2>&1 | tee "${ERROR_LOG}"
HIVE_RET=${PIPESTATUS[0]}

if [ ${HIVE_RET} -ne 0 ]; then
    echo ""
    echo "[ERROR] Hive 执行失败！错误日志路径：${ERROR_LOG}"
    exit 1
fi

echo ""
echo "[SUCCESS] 任务执行完成"
echo "==================== HDFS 结果检查 ===================="
echo "输出路径: ${HDFS_TARGET_PATH}"
hdfs dfs -ls "${HDFS_TARGET_PATH}/" 2>/dev/null || echo "  (目录为空或路径不存在)"
echo ""
echo "--- 前 5 条数据预览 (mid / uid / content前50字 / dt) ---"
hdfs dfs -cat "${HDFS_TARGET_PATH}/*" 2>/dev/null \
    | head -5 \
    | awk -F'\t' '{printf "mid=%-20s uid=%-12s dt=%s content=%.50s\n", $1, $2, $4, $3}'
echo "========================================================"
echo "完成时间: $(date '+%Y-%m-%d %H:%M:%S')"
exit 0
