#!/bin/bash
# =============================================================
# query_task-查询有效任务.sh — 历史人工查询脚本（不作为正式入口）
# =============================================================
# 状态：
#   历史兼容。正式排障优先使用 scripts/production_preflight.py、
#   scripts/manual_classify_mid.py 和 README 的 case 排查路径。
#   本脚本不读取 config.yaml，且不应保存数据库密码；仅保留 SQL 查询逻辑作人工参考。
#
# 功能：
#   查询 super_mid_task 中的有效任务，支持按 mid 反查任务信息。
#   用于排查某条 mid 反解失败时，确认其对应的任务数据是否正常。
#
# 用法：
#   # 查询所有有效任务（默认 limit 50）
#   bash sql/query_task-查询有效任务.sh
#
#   # 按 task_id 查询
#   bash sql/query_task-查询有效任务.sh --task-id 1296499607471128577
#
#   # 按 customer_id 查询
#   bash sql/query_task-查询有效任务.sh --customer-id 2608812381
#
#   # 按 mid 反查（先查明细表拿到 super_task_id，再查任务表）
#   bash sql/query_task-查询有效任务.sh --mid 5239377989207686 --shard-index 1
#
# 数据库：clue_collect_common（连接需通过环境变量提供）
# 作者：xuanyu11
# 创建时间：2026-08-26
# =============================================================

set -euo pipefail

# ── 数据库连接（禁止在脚本中固化密码） ─────────────────────────
# 示例：
#   export MYSQL_HOST='...'
#   export MYSQL_PORT='9671'
#   export MYSQL_USER='...'
#   export MYSQL_PASSWORD='...'
#   bash sql/query_task-查询有效任务.sh --task-id <task_id>
DB_HOST="${MYSQL_HOST:-}"
DB_PORT="${MYSQL_PORT:-9671}"
DB_USER="${MYSQL_USER:-}"
DB_PASS="${MYSQL_PASSWORD:-}"
DB_NAME="${MYSQL_DATABASE:-clue_collect_common}"
TASK_TABLE="super_mid_task"

# ── 参数解析 ──
TASK_ID=""
CUSTOMER_ID=""
MID=""
SHARD_INDEX=""
LIMIT=50

while [[ $# -gt 0 ]]; do
    case "$1" in
        --task-id)    TASK_ID="$2"; shift 2 ;;
        --customer-id) CUSTOMER_ID="$2"; shift 2 ;;
        --mid)        MID="$2"; shift 2 ;;
        --shard-index) SHARD_INDEX="$2"; shift 2 ;;
        --limit)      LIMIT="$2"; shift 2 ;;
        -h|--help)
            echo "用法: bash sql/query_task-查询有效任务.sh [选项]"
            echo "  --task-id ID         按 task_id 查询"
            echo "  --customer-id ID     按 customer_id 查询"
            echo "  --mid MID            按 mid 反查（需配合 --shard-index）"
            echo "  --shard-index N      分表索引（配合 --mid 使用）"
            echo "  --limit N            限制返回条数（默认 50）"
            exit 0
            ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

if [[ -z "$DB_HOST" || -z "$DB_USER" || -z "$DB_PASS" ]]; then
    echo "[ERROR] 请通过 MYSQL_HOST、MYSQL_USER、MYSQL_PASSWORD 提供连接信息；本历史脚本不再内置账号密码。"
    exit 2
fi

MYSQL_CMD=(mysql "-h${DB_HOST}" "-P${DB_PORT}" "-u${DB_USER}" "-p${DB_PASS}" "${DB_NAME}" -N -B)

# ── 按 mid 反查 ──
if [ -n "$MID" ]; then
    if [ -z "$SHARD_INDEX" ]; then
        echo "[ERROR] --mid 需要配合 --shard-index 使用"
        exit 1
    fi
    DETAIL_TABLE="nature_ad_super_mid_${SHARD_INDEX}"
    echo "==================== 步骤1: 从明细表查 mid 对应的 super_task_id ===================="
    echo "SELECT id, customer_id, super_task_id, mid, mid_uid, level, forward_mid FROM ${DETAIL_TABLE} WHERE mid = '${MID}' LIMIT 5;"
    DETAIL_RESULT=$("${MYSQL_CMD[@]}" -e "SELECT id, customer_id, super_task_id, mid, mid_uid, level, forward_mid FROM ${DETAIL_TABLE} WHERE mid = '${MID}' LIMIT 5;" 2>/dev/null || echo "")
    if [ -z "$DETAIL_RESULT" ]; then
        echo "[WARN] 在 ${DETAIL_TABLE} 中未找到 mid=${MID}"
        exit 0
    fi
    echo "$DETAIL_RESULT" | awk -F'\t' 'BEGIN{printf "%-8s %-15s %-25s %-20s %-15s %-6s %-20s\n","id","customer_id","super_task_id","mid","mid_uid","level","forward_mid"} {printf "%-8s %-15s %-25s %-20s %-15s %-6s %-20s\n",$1,$2,$3,$4,$5,$6,$7}'

    SUPER_TASK_ID=$(echo "$DETAIL_RESULT" | head -1 | awk -F'\t' '{print $3}')
    if [ -n "$SUPER_TASK_ID" ]; then
        echo ""
        echo "==================== 步骤2: 从任务表查 super_task_id=${SUPER_TASK_ID} ===================="
        "${MYSQL_CMD[@]}" -e "SELECT id, task_id, operator_uid, task_type, exec_status, industry_tag, brand_tag, end_time FROM ${TASK_TABLE} WHERE task_id = ${SUPER_TASK_ID} LIMIT 5;" 2>/dev/null \
            | awk -F'\t' 'BEGIN{printf "%-8s %-25s %-15s %-10s %-12s %-40s %-40s %-20s\n","id","task_id","operator_uid","task_type","exec_status","industry_tag","brand_tag","end_time"} {printf "%-8s %-25s %-15s %-10s %-12s %-40s %-40s %-20s\n",$1,$2,$3,$4,$5,$6,$7,$8}'
    fi
    exit 0
fi

# ── 构造 WHERE 条件 ──
# 与 src/db_client.py fetch_active_tasks() 保持一致：
#   task_type=1 AND (exec_status != 5 OR (exec_status=5 AND end_time > now()-1天))
WHERE="task_type = 1 AND (exec_status != 5 OR (exec_status = 5 AND end_time > DATE_SUB(NOW(), INTERVAL 1 DAY)))"
if [ -n "$TASK_ID" ]; then
    WHERE="task_id = ${TASK_ID}"
elif [ -n "$CUSTOMER_ID" ]; then
    WHERE="${WHERE} AND operator_uid = ${CUSTOMER_ID}"
fi

echo "==================== 查询 super_mid_task ===================="
echo "WHERE: ${WHERE}"
echo "LIMIT: ${LIMIT}"
echo ""

"${MYSQL_CMD[@]}" -e "SELECT id, task_id, operator_uid, task_type, exec_status, industry_tag, brand_tag, end_time FROM ${TASK_TABLE} WHERE ${WHERE} ORDER BY id ASC LIMIT ${LIMIT};" 2>/dev/null \
    | awk -F'\t' 'BEGIN{printf "%-8s %-25s %-15s %-10s %-12s %-40s %-40s %-20s\n","id","task_id","operator_uid","task_type","exec_status","industry_tag","brand_tag","end_time"} {printf "%-8s %-25s %-15s %-10s %-12s %-40s %-40s %-20s\n",$1,$2,$3,$4,$5,$6,$7,$8}'

echo ""
echo "查询完成: $(date '+%Y-%m-%d %H:%M:%S')"
