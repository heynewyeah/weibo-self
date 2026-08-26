#!/bin/bash
# =============================================================
# query_detail.sh — 查询 nature_ad_super_mid_x 明细表
# =============================================================
# 功能：
#   查询分表中的博文明细数据，支持按 mid / customer_id / level 等条件过滤。
#   用于排查某条 mid 反解失败时，确认其在 MySQL 中的原始数据是否正常。
#
# 用法：
#   # 查询分表 1 中 level=0 的前 20 条
#   bash sql/query_detail.sh --shard-index 1
#
#   # 按 mid 精确查询
#   bash sql/query_detail.sh --shard-index 1 --mid 5239377989207686
#
#   # 按 customer_id 过滤
#   bash sql/query_detail.sh --shard-index 1 --customer-id 2608812381
#
#   # 查所有 level（不限 level=0）
#   bash sql/query_detail.sh --shard-index 1 --all-levels
#
#   # 查转发博文
#   bash sql/query_detail.sh --shard-index 1 --forward-only
#
# 数据库：clue_collect_common
# 作者：xuanyu11
# 创建时间：2026-08-26
# =============================================================

set -euo pipefail

# ── 数据库连接 ──
DB_HOST="10.79.104.30"
DB_PORT=3306
DB_USER="clue_collect"
DB_PASS="clue_collect"
DB_NAME="clue_collect_common"
TABLE_PREFIX="nature_ad_super_mid_"

# ── 参数解析 ──
SHARD_INDEX=""
MID=""
CUSTOMER_ID=""
LEVEL=""
ALL_LEVELS=false
FORWARD_ONLY=false
LIMIT=20

while [[ $# -gt 0 ]]; do
    case "$1" in
        --shard-index)  SHARD_INDEX="$2"; shift 2 ;;
        --mid)          MID="$2"; shift 2 ;;
        --customer-id)  CUSTOMER_ID="$2"; shift 2 ;;
        --level)        LEVEL="$2"; shift 2 ;;
        --all-levels)   ALL_LEVELS=true; shift ;;
        --forward-only) FORWARD_ONLY=true; shift ;;
        --limit)        LIMIT="$2"; shift 2 ;;
        -h|--help)
            echo "用法: bash sql/query_detail.sh [选项]"
            echo "  --shard-index N      分表索引（必填），如 1 表示 nature_ad_super_mid_1"
            echo "  --mid MID            按 mid 精确查询"
            echo "  --customer-id ID     按 customer_id 过滤"
            echo "  --level N            按 level 过滤（默认 0）"
            echo "  --all-levels         查所有 level"
            echo "  --forward-only       只查转发博文（forward_mid != 0）"
            echo "  --limit N            限制返回条数（默认 20）"
            exit 0
            ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [ -z "$SHARD_INDEX" ]; then
    echo "[ERROR] 必须指定 --shard-index"
    echo "用法: bash sql/query_detail.sh --shard-index 1 [--mid xxx]"
    exit 1
fi

TABLE_NAME="${TABLE_PREFIX}${SHARD_INDEX}"
MYSQL_CMD="mysql -h${DB_HOST} -P${DB_PORT} -u${DB_USER} -p${DB_PASS} ${DB_NAME} -N -B"

# ── 构造 WHERE 条件 ──
WHERE="1=1"
if [ -n "$MID" ]; then
    WHERE="mid = '${MID}'"
else
    if [ "$ALL_LEVELS" = false ] && [ -z "$LEVEL" ]; then
        WHERE="level = 0"
    elif [ -n "$LEVEL" ]; then
        WHERE="level = ${LEVEL}"
    fi
    if [ -n "$CUSTOMER_ID" ]; then
        WHERE="${WHERE} AND customer_id = ${CUSTOMER_ID}"
    fi
    if [ "$FORWARD_ONLY" = true ]; then
        WHERE="${WHERE} AND forward_mid != '' AND forward_mid != '0' AND forward_mid IS NOT NULL"
    fi
fi

echo "==================== 查询 ${TABLE_NAME} ===================="
echo "WHERE: ${WHERE}"
echo "LIMIT: ${LIMIT}"
echo ""

# 先检查表是否存在
TABLE_EXISTS=$(${MYSQL_CMD} -e "SELECT 1 FROM information_schema.tables WHERE table_schema='${DB_NAME}' AND table_name='${TABLE_NAME}' LIMIT 1;" 2>/dev/null || echo "")
if [ -z "$TABLE_EXISTS" ]; then
    echo "[ERROR] 表 ${TABLE_NAME} 不存在"
    exit 1
fi

${MYSQL_CMD} -e "
SELECT
    id,
    customer_id,
    super_task_id,
    mid,
    mid_uid,
    LEFT(mid_text, 80) AS mid_text_preview,
    LEFT(mid_pids, 60) AS mid_pids_preview,
    LEFT(mid_fids, 60) AS mid_fids_preview,
    forward_mid,
    LEFT(forward_text, 60) AS forward_text_preview,
    level
FROM ${TABLE_NAME}
WHERE ${WHERE}
ORDER BY id ASC
LIMIT ${LIMIT};
" 2>/dev/null \
    | awk -F'\t' '
BEGIN {
    printf "%-8s %-15s %-25s %-20s %-15s %-40s %-30s %-30s %-20s %-30s %-6s\n",
        "id","customer_id","super_task_id","mid","mid_uid","text","pids","fids","forward_mid","forward_text","level"
}
{
    printf "%-8s %-15s %-25s %-20s %-15s %-40s %-30s %-30s %-20s %-30s %-6s\n",
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11
}'

echo ""

# 统计信息
echo "==================== 统计信息 ===================="
${MYSQL_CMD} -e "
SELECT
    level,
    COUNT(*) AS cnt,
    SUM(CASE WHEN forward_mid != '' AND forward_mid != '0' AND forward_mid IS NOT NULL THEN 1 ELSE 0 END) AS forward_cnt
FROM ${TABLE_NAME}
GROUP BY level
ORDER BY level;
" 2>/dev/null \
    | awk -F'\t' 'BEGIN{printf "%-8s %-10s %-12s\n","level","count","forward"} {printf "%-8s %-10s %-12s\n",$1,$2,$3}'

echo ""
echo "查询完成: $(date '+%Y-%m-%d %H:%M:%S')"
