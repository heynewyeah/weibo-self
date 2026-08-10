#!/bin/bash
# 博文批量分类脚本 v7 — 博文分层（认知层/兴趣层/考虑层）Qwen3.6 thinking 模式兼容
set -uo pipefail
export LC_ALL=C

########################### 配置区域 ###########################
API_URL="http://10.1.126.27:8087/v1/chat/completions"
MODEL="/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"

# 输入输出路径
HDFS_SOURCE="/dw_ext/ad/person/xuanyu11/intent_behavior/data/car_weibo_ad_20260701_20260731/000000_0"
HDFS_OUT_DIR="/dw_ext/ad/person/xuanyu11/intent_behavior/output"
HDFS_OUT_FILE="${HDFS_OUT_DIR}/car_weibo_layer_$(date +%Y%m%d_%H%M%S).tsv"

# 控制参数
LIMIT_ROWS=200          # 0=全量处理；>0 仅处理前N条（调试用）
SLEEP_SEC=0.3
MAX_RETRY=3
BATCH_SIZE=100

# 本地临时文件
LOCAL_TMP_DIR="/dev/shm/intent_behavior_$$"
SHM_TMP="${LOCAL_TMP_DIR}/result.tsv"
FAILED_LOG="${LOCAL_TMP_DIR}/failed_mids.log"

# ========== 博文分层提示词（3层：认知层/兴趣层/考虑层）==========
SYS_PROMPT="你是一个汽车行业博文营销分层分类器。请将博文分类到以下3个营销层级之一。

可选层级：
【认知层】— 主打品牌曝光，高传播高热度，让用户认知品牌，容易形成传播趋势。
  典型内容：精美TVC、功能解读类、知识科普类、生活记录情绪共鸣类、话题承接内容、品牌官方宣传片、新车发布会、品牌联名活动。
  特征：以品牌/产品曝光为核心目的，传播性强，但未必包含深度产品信息或购买引导。

【兴趣层】— 含品牌词，互动率较高，引发讨论、互动，提升用户对产品的兴趣。
  典型内容：KOL对比/评测内容、UGC种草内容、产品功能体验分享、试驾vlog、车型亮点解读、用户讨论帖。
  特征：有具体产品/品牌的信息，能引发用户兴趣和互动讨论，但尚未涉及具体购买决策信息。

【考虑层】— 产品真实测评、竞品横评对比，价格优惠信息，参数分析，帮助用户完成决策。
  典型内容：优惠促销活动内容、竞品横评对比、价格/落地价讨论、参数配置对比、用户购车决策分享、购车攻略、经销商活动。
  特征：包含帮助用户做出购买决策的具体信息，如价格、优惠、对比、参数、购买渠道等。

分类原则：
1. 优先看博文的核心目的：是让用户「知道品牌」→认知层；「产生兴趣」→兴趣层；「辅助决策」→考虑层
2. 如果博文同时涉及多个层级，按最深层级归类（如同时有品牌曝光和价格优惠，归考虑层）
3. 如果博文与汽车行业完全无关，归为【认知层】（兜底，因为广告博文至少有品牌曝光属性）
4. 先进行分析推理
5. 分析完成后，单独一行输出最终结论，格式必须是：最终分类结果：【层级名称】"

USER_TPL="请对以下汽车行业博文进行营销分层分类。

博文内容：
%s

请分析后，最后一行输出：最终分类结果：【层级名称】"

trap 'rm -rf ${LOCAL_TMP_DIR}; echo "[通知] 临时文件已清理"' EXIT
mkdir -p "${LOCAL_TMP_DIR}"
###############################################################

#====新增：解析hdfs路径提取起止日期函数====
parse_date_range(){
    local path="$1"
    # 提取 car_weibo_ad_20260101_20260131
    local segment
    segment=$(echo "$path" | grep -oE 'car_weibo_ad_[0-9]{8}_[0-9]{8}')
    local sdate=${segment#*ad_}
    local start_raw=${sdate%_*}
    local end_raw=${sdate#*_}
    # yyyymmdd 转 yyyy‑mm‑dd
    local start_date="${start_raw:0:4}-${start_raw:4:2}-${start_raw:6:2}"
    local end_date="${end_raw:0:4}-${end_raw:4:2}-${end_raw:6:2}"
    echo "${start_date}|${end_date}"
}

# 调用API
call_api() {
    local req_body="$1"
    local retry=0
    local resp=""

    while [ $retry -lt $MAX_RETRY ]; do
        resp=$(curl -s --max-time 60 -X POST "${API_URL}" \
            -H "Content-Type: application/json" \
            -d "${req_body}" 2>/dev/null)

        if echo "${resp}" | jq -e '.choices[0].message.content' >/dev/null 2>&1; then
            echo "${resp}"
            return 0
        fi

        retry=$((retry + 1))
        echo "[警告] API调用失败或返回异常，第${retry}次重试..." >&2
        sleep $((2 ** retry))
    done

    echo "[错误] API调用失败，已达最大重试次数" >&2
    return 1
}

# 从模型输出中提取层级标签（兼容 Qwen3.6 thinking 模式）
extract_label() {
    local full_output="$1"
    local label="未识别"

    # 策略1：查找 "最终分类结果：【xxx】" 格式
    local final_result
    final_result=$(echo "${full_output}" | grep -oP '最终分类结果：【\K[^】]+' | tail -n 1)
    if [ -n "${final_result}" ]; then
        case "${final_result}" in
            "认知层"|"兴趣层"|"考虑层")
                echo "${final_result}"
                return 0
                ;;
        esac
    fi

    # 策略2：查找所有【标签】格式，取最后一个
    local all_brackets
    all_brackets=$(echo "${full_output}" | grep -oP '【\K[^】]+' | tail -n 1)
    if [ -n "${all_brackets}" ]; then
        case "${all_brackets}" in
            "认知层"|"兴趣层"|"考虑层")
                echo "${all_brackets}"
                return 0
                ;;
        esac
    fi

    # 策略3：取全文最后一行，尝试匹配
    local last_line
    last_line=$(echo "${full_output}" | sed '/^$/d' | tail -n 1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

    case "${last_line}" in
        "【认知层】"|"认知层") label="认知层" ;;
        "【兴趣层】"|"兴趣层") label="兴趣层" ;;
        "【考虑层】"|"考虑层") label="考虑层" ;;
        *)
            if echo "${last_line}" | grep -q "认知层"; then label="认知层"
            elif echo "${last_line}" | grep -q "兴趣层"; then label="兴趣层"
            elif echo "${last_line}" | grep -q "考虑层"; then label="考虑层"
            fi
            ;;
    esac

    echo "${label}"
}

# ==================== 主流程 ====================
#====新增：记录开始时间戳====
START_SEC=$(date +%s)
DATE_RANGE=$(parse_date_range "${HDFS_SOURCE}")
START_DT=${DATE_RANGE%|*}
END_DT=${DATE_RANGE#*|}

echo "======================================"
echo "【博文批量分类工具 v7 — 博文分层（3层）】"
echo "数据源：${HDFS_SOURCE}"
echo "输出HDFS：${HDFS_OUT_FILE}"
echo "分类目标：认知层 / 兴趣层 / 考虑层"
echo "数据时间段：${START_DT} ~ ${END_DT}"
[ ${LIMIT_ROWS} -gt 0 ] && echo "采样前 ${LIMIT_ROWS} 条"
echo "======================================"

> "${SHM_TMP}"
> "${FAILED_LOG}"

echo "[步骤1] 预加载HDFS数据到内存..."
if [ ${LIMIT_ROWS} -gt 0 ]; then
    RAW_DATA=$(hdfs dfs -cat "${HDFS_SOURCE}" 2>/dev/null | head -n ${LIMIT_ROWS})
    TOTAL_ROWS=${LIMIT_ROWS}
else
    RAW_DATA=$(hdfs dfs -cat "${HDFS_SOURCE}" 2>/dev/null)
    TOTAL_ROWS=$(echo "${RAW_DATA}" | wc -l)
fi

echo "[步骤1完成] 加载时间段 ${START_DT} ~ ${END_DT} 数据，共 ${TOTAL_ROWS} 条"
echo ""
echo "[步骤2] 开始逐条分类打标..."

count=0
success_count=0
fail_count=0

# ==================== 主流程中的 while 循环 ====================

while IFS=$'\t' read -r mid content dt; do
    [[ -z "${mid}" ]] && continue

    # ★ 修复：去除博文内容中的换行符和回车符
    content=$(echo "${content}" | tr '\n\r' '  ' | sed 's/  */ /g')

    count=$((count + 1))
    [ $((count % BATCH_SIZE)) -eq 0 ] || [ ${count} -eq 1 ] && \
        echo "[进度] 已处理 ${count}/${TOTAL_ROWS} 条 (成功:${success_count} 失败:${fail_count})"

    # 安全转义（用于API请求）
    safe_content=$(echo "${content}" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/ /g')
    user_prompt=$(printf "${USER_TPL}" "${safe_content}")

    req_body=$(jq -n \
        --arg mod "${MODEL}" \
        --arg sys "${SYS_PROMPT}" \
        --arg usr "${user_prompt}" \
        '{
            model: $mod,
            messages: [
                {"role":"system", "content":$sys},
                {"role":"user", "content":$usr}
            ],
            temperature: 0.0,
            max_tokens: 512,
            chat_template_kwargs: {enable_thinking: false}
        }')

    resp=$(call_api "${req_body}")
    if [ $? -ne 0 ]; then
        # ★ 修复：用 printf 代替 echo -e
        printf '%s\t%s\t%s\tAPI_ERROR\n' "${mid}" "${content}" "${dt}" >> "${SHM_TMP}"
        printf '%s\tAPI_ERROR\n' "${mid}" >> "${FAILED_LOG}"
        fail_count=$((fail_count + 1))
        sleep ${SLEEP_SEC}
        continue
    fi

    full_output=$(echo "${resp}" | jq -r '.choices[0].message.content // ""')
    label=$(extract_label "${full_output}")

    if [ "${label}" = "未识别" ]; then
        echo "[警告] mid=${mid} 标签提取失败" >&2
        # ★ 修复：用 printf 代替 echo -e
        printf '%s\t%s\t%s\t未识别\n' "${mid}" "${content}" "${dt}" >> "${SHM_TMP}"
        printf '%s\t未识别\t%s\n' "${mid}" "${full_output}" >> "${FAILED_LOG}"
        fail_count=$((fail_count + 1))
    else
        # ★ 修复：用 printf 代替 echo -e
        printf '%s\t%s\t%s\t%s\n' "${mid}" "${content}" "${dt}" "${label}" >> "${SHM_TMP}"
        success_count=$((success_count + 1))
    fi

    sleep ${SLEEP_SEC}
done <<< "${RAW_DATA}"

echo ""
echo "[步骤2完成] 分类处理结束"
echo "  数据时间段：${START_DT} ~ ${END_DT}"
echo "  总计处理: ${count} 条"
echo "  成功: ${success_count} 条"
echo "  失败/未识别: ${fail_count} 条"
echo ""

echo "[统计] 分层标签分布："
cut -f4 "${SHM_TMP}" | sort | uniq -c | sort -rn

echo ""
echo "======================================"
echo "[步骤3] 上传结果到HDFS..."
hdfs dfs -mkdir -p "${HDFS_OUT_DIR}"
hdfs dfs -put -f "${SHM_TMP}" "${HDFS_OUT_FILE}"

#====新增：计算总运行时间====
END_SEC=$(date +%s)
DURATION_SEC=$(( END_SEC - START_SEC ))
DURATION_H=$(( DURATION_SEC / 3600 ))
DURATION_M=$(( (DURATION_SEC % 3600)/60 ))
DURATION_S=$(( DURATION_SEC % 60 ))

if [ $? -eq 0 ]; then
    echo "✅ 全部流程执行完成！！"
    echo "数据时间段：${START_DT} ~ ${END_DT}，共处理 ${count} 条"
    echo "总运行耗时：${DURATION_H}时${DURATION_M}分${DURATION_S}秒"
    echo "HDFS结果文件：${HDFS_OUT_FILE}"
    echo ""
    echo "校验命令："
    echo "  hdfs dfs -cat ${HDFS_OUT_FILE} | head -5"
    echo "  hdfs dfs -cat ${HDFS_OUT_FILE} | wc -l"
    echo ""
    echo "失败记录（如有）：${FAILED_LOG}"
else
    echo "❌ HDFS上传失败，结果保留在本地：${SHM_TMP}"
    echo "总运行耗时：${DURATION_H}时${DURATION_M}分${DURATION_S}秒"
    exit 1
fi
echo "======================================"