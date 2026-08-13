#!/bin/bash
# =============================================================
# image.sh — 图片博文全链路测试（单条 + 批量）
# =============================================================
# 功能：
#   1. 单条模式：指定真实 pid + 博文内容，下载图片并做图文分类
#   2. 批量模式：从 TSV 文件读取多条图文博文（如 query_image.sh 输出），
#      逐条下载图片、调用 Qwen3.6 分类、记录结果，并生成统计报告
#
# 数据来源：
#   - 单条：命令行传入的真实 pid + 真实博文内容
#   - 批量：HDFS/本地 TSV 文件，字段与 query_image.sh 输出对齐
#           mid \t uid \t content \t media_id \t customer_info(pid JSON 数组) \t dt
#   - 模型服务：vLLM Qwen3.6-35B-A3B
#
# 输出路径：
#   - 单条：终端实时输出
#   - 批量：默认 output/image_batch_<时间戳>.tsv 和 .json
#
# 用法：
#   # 单条模式（默认）
#   bash sql/image.sh
#   bash sql/image.sh --pid "006mX07Rly8ifv3xs5535j30ud0plk1m" --text "比亚迪宋L实拍..."
#
#   # 批量模式
#   bash sql/image.sh --mode batch --input /dw_ext/ad/person/xuanyu11/intent_behavior/data/image_weibo_ad_20260701_20260731
#   bash sql/image.sh --mode batch --input /path/to/image_data.tsv --limit 10 --output /path/to/result.tsv
#
# 运行时间预估：
#   - 单条：约 10~30 秒
#   - 批量：约 N × 10~30 秒（N 为图片张数）
#
# 作者：xuanyu11
# 创建时间：2026-08-13
# =============================================================

set -uo pipefail
export LC_ALL=C

# ==================== 配置 ====================
API_URL="http://10.1.126.27:8087/v1/chat/completions"
MODEL="/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"

DEFAULT_PID="006mX07Rly8ifv3xs5535j30ud0plk1m"
DEFAULT_TEXT="比亚迪宋L实拍来了！外观绝了，这个颜色真的太好看了，内饰也很精致，大家觉得怎么样？"
IMG_URL_PATTERN="https://wx2.sinaimg.cn/mw690/{pid}.jpg"

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
OUTPUT_DIR="${PROJECT_DIR}/output"
TMP_DIR="/tmp/image_test_$$"

mkdir -p "${TMP_DIR}" "${OUTPUT_DIR}"
trap 'rm -rf "${TMP_DIR}"; echo ""; echo "[清理] 临时文件已清理"' EXIT

# ==================== 解析参数 ====================
MODE="single"
INPUT=""
OUTPUT=""
PID=""
TEXT=""
LIMIT=0

usage() {
    echo "用法:"
    echo "  单条模式: bash sql/image.sh [--pid PID] [--text '博文内容']"
    echo "  批量模式: bash sql/image.sh --mode batch --input <hdfs路径或本地tsv> [--output <结果文件>] [--limit N]"
    exit 1
}

# 简单解析 --key value 形式参数
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)
            MODE="$2"
            shift 2
            ;;
        --input)
            INPUT="$2"
            shift 2
            ;;
        --output)
            OUTPUT="$2"
            shift 2
            ;;
        --pid)
            PID="$2"
            shift 2
            ;;
        --text)
            TEXT="$2"
            shift 2
            ;;
        --limit)
            LIMIT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "未知参数: $1"
            usage
            ;;
    esac
done

PID="${PID:-${DEFAULT_PID}}"
TEXT="${TEXT:-${DEFAULT_TEXT}}"

# ==================== 公共函数 ====================

# 调用 Qwen3.6 多模态分类
# 参数：图片路径、提示词
# 返回：模型输出文本（失败时返回空字符串）
call_qwen_image() {
    local img_path="$1"
    local prompt="$2"

    local img_b64
    img_b64=$(base64 -w 0 "${img_path}" 2>/dev/null)
    if [ -z "${img_b64}" ]; then
        echo ""
        return 1
    fi

    local resp
    resp=$(curl -s --max-time 120 -X POST "${API_URL}" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"${MODEL}\",
            \"messages\": [
                {\"role\": \"system\", \"content\": \"你是一个汽车行业博文营销分层分类器。将博文分类到以下3个层级之一：【认知层】品牌曝光传播；【兴趣层】引发讨论互动；【考虑层】辅助购买决策。直接输出：最终分类结果：【层级名称】\"},
                {\"role\": \"user\", \"content\": [
                    {\"type\": \"text\", \"text\": \"${prompt}\"},
                    {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${img_b64}\"}}
                ]}
            ],
            \"max_tokens\": 512,
            \"temperature\": 0.0,
            \"chat_template_kwargs\": {\"enable_thinking\": false}
        }" 2>/dev/null)

    echo "${resp}" | jq -r '.choices[0].message.content // ""'
}

# 下载图片
# 参数：pid、保存路径
# 返回：0 成功，1 失败
download_image() {
    local pid="$1"
    local save_path="$2"
    local img_url="${IMG_URL_PATTERN/\{pid\}/${pid}}"

    local http_code file_size
    http_code=$(curl -s -o "${save_path}" -w "%{http_code}" --max-time 30 "${img_url}" 2>/dev/null)
    file_size=$(stat -c %s "${save_path}" 2>/dev/null || echo 0)

    if [ "${http_code}" != "200" ] || [ "${file_size}" -lt 100 ]; then
        echo "下载失败 HTTP=${http_code} size=${file_size}B URL=${img_url}"
        return 1
    fi
    return 0
}

# 从 JSON 数组字符串中提取 pid 列表
# 例如：["pid1","pid2"] → pid1 pid2
extract_pids() {
    local customer_info="$1"
    echo "${customer_info}" | tr -d '[]"' | tr ',' '\n' | sed '/^$/d'
}

# ==================== 单条模式 ====================
run_single() {
    echo "============================================"
    echo "  图片全链路测试 — 单条模式"
    echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  模型: ${MODEL}"
    echo "  API:  ${API_URL}"
    echo "============================================"
    echo ""

    START_TS=$(date +%s)

    echo ">> [1/3] pid 转 URL"
    echo "   pid:  ${PID}"
    echo "   URL:  ${IMG_URL_PATTERN/\{pid\}/${PID}}"
    echo ""

    local save_path="${TMP_DIR}/${PID}.jpg"

    echo ">> [2/3] 下载图片..."
    if ! download_image "${PID}" "${save_path}"; then
        echo "   ❌ 图片下载失败，退出"
        exit 1
    fi
    local file_size
    file_size=$(stat -c %s "${save_path}" 2>/dev/null || echo 0)
    echo "   ✅ 下载成功 大小=$((file_size / 1024))KB 类型=$(file "${save_path}" | cut -d: -f2)"
    echo ""

    echo ">> [3/3] 图文博文分类..."
    echo "   博文文字: ${TEXT}"
    local prompt="请对以下汽车行业图文博文进行分类。\\n博文文字：${TEXT}\\n请结合文字和图片综合判断，直接输出：最终分类结果：【层级名称】"
    local classify_result
    classify_result=$(call_qwen_image "${save_path}" "${prompt}")

    if [ -z "${classify_result}" ]; then
        echo "   ❌ 模型返回为空，可能 API 调用失败"
        exit 1
    fi

    echo "   分类结果:"
    echo "   ┌──────────────────────────────────────────"
    echo "   │ ${classify_result}"
    echo "   └──────────────────────────────────────────"

    END_TS=$(date +%s)
    echo ""
    echo "============================================"
    echo "  ✅ 单条测试完成"
    echo "  总耗时: $((END_TS - START_TS))s"
    echo "  时间:   $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================"
}

# ==================== 批量模式 ====================
run_batch() {
    if [ -z "${INPUT}" ]; then
        echo "[错误] 批量模式必须指定 --input"
        usage
    fi

    local input_file="${INPUT}"
    local local_input="${TMP_DIR}/batch_input.tsv"

    echo "============================================"
    echo "  图片全链路测试 — 批量模式"
    echo "  时间:     $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  输入:     ${input_file}"
    echo "  模型:     ${MODEL}"
    echo "============================================"
    echo ""

    START_TS=$(date +%s)

    # ── 获取输入数据 ──────────────────────────────────────────
    if [[ "${input_file}" == hdfs://* ]]; then
        input_file="${input_file#hdfs://}"
    fi

    if hdfs dfs -test -d "${input_file}" 2>/dev/null; then
        echo ">> 从 HDFS 目录读取数据: ${input_file}"
        hdfs dfs -cat "${input_file}/part-*" > "${local_input}" 2>/dev/null || {
            echo "[错误] 无法读取 HDFS 目录: ${input_file}"
            exit 1
        }
    elif hdfs dfs -test -e "${input_file}" 2>/dev/null; then
        echo ">> 从 HDFS 文件读取数据: ${input_file}"
        hdfs dfs -cat "${input_file}" > "${local_input}" 2>/dev/null || {
            echo "[错误] 无法读取 HDFS 文件: ${input_file}"
            exit 1
        }
    elif [ -f "${input_file}" ]; then
        echo ">> 从本地文件读取数据: ${input_file}"
        cp "${input_file}" "${local_input}"
    else
        echo "[错误] 输入路径不存在: ${input_file}"
        exit 1
    fi

    local total_lines
    total_lines=$(wc -l < "${local_input}" | tr -d ' ')
    echo "   读取行数: ${total_lines}"

    # ── 准备输出文件 ──────────────────────────────────────────
    local run_ts
    run_ts=$(date '+%Y%m%d_%H%M%S')
    OUTPUT="${OUTPUT:-${OUTPUT_DIR}/image_batch_${run_ts}.tsv}"
    local output_json="${OUTPUT%.tsv}.json"
    local output_summary="${OUTPUT%.tsv}_summary.txt"

    echo "   结果文件: ${OUTPUT}"
    echo "   摘要文件: ${output_summary}"
    echo ""

    # 表头
    printf "mid\tuid\tcontent_preview\tpid\tlayer\tsuccess\terror\n" > "${OUTPUT}"

    # ── 逐条处理 ──────────────────────────────────────────────
    local lineno=0
    local success_count=0
    local fail_count=0
    local processed=0

    # JSON 数组初始化
    echo "[" > "${output_json}"
    local first_json=1

    while IFS=$'\t' read -r mid uid content media_id customer_info dt; do
        lineno=$((lineno + 1))

        # 跳过空行和表头
        [ -z "${mid}" ] && continue
        if [ "${mid}" = "mid" ]; then
            continue
        fi

        processed=$((processed + 1))
        if [ "${LIMIT}" -gt 0 ] && [ "${processed}" -gt "${LIMIT}" ]; then
            break
        fi

        echo "[${processed}] mid=${mid} uid=${uid} dt=${dt}"

        # 提取第一个 pid
        local first_pid
        first_pid=$(echo "${customer_info}" | tr -d '[]"' | tr ',' '\n' | sed '/^$/d' | head -1)

        if [ -z "${first_pid}" ]; then
            echo "   ❌ 未解析到 pid"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "" "" "false" "未解析到pid" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            continue
        fi

        local img_path="${TMP_DIR}/${mid}_${first_pid}.jpg"

        echo "   → 下载图片 pid=${first_pid}"
        local dl_error
        dl_error=$(download_image "${first_pid}" "${img_path}" 2>&1)
        local dl_status=$?
        if [ "${dl_status}" -ne 0 ]; then
            echo "   ❌ ${dl_error}"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "${first_pid}" "" "false" "${dl_error}" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            rm -f "${img_path}"
            continue
        fi

        local file_size
        file_size=$(stat -c %s "${img_path}" 2>/dev/null || echo 0)
        echo "   ✅ 图片下载成功 ${file_size} bytes"

        echo "   → 调用模型分类..."
        local prompt="请对以下汽车行业图文博文进行分类。\\n博文文字：${content}\\n请结合文字和图片综合判断，直接输出：最终分类结果：【层级名称】"
        local classify_result
        classify_result=$(call_qwen_image "${img_path}" "${prompt}")

        if [ -z "${classify_result}" ]; then
            echo "   ❌ 模型返回为空"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "${first_pid}" "" "false" "模型返回为空" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            rm -f "${img_path}"
            continue
        fi

        # 提取层级标签
        local layer
        layer=$(echo "${classify_result}" | grep -oE '认知层|兴趣层|考虑层' | tail -1)
        [ -z "${layer}" ] && layer="未识别"

        echo "   ✅ 分类结果: ${layer}"
        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
            "${mid}" "${uid}" "${content:0:50}" "${first_pid}" "${layer}" "true" "" >> "${OUTPUT}"
        success_count=$((success_count + 1))

        # 写入 JSON
        if [ "${first_json}" -eq 1 ]; then
            first_json=0
        else
            echo "," >> "${output_json}"
        fi
        printf '{"mid":"%s","uid":"%s","content_preview":"%s","pid":"%s","layer":"%s","success":true,"raw_output":"%s"}' \
            "${mid}" "${uid}" "${content:0:50}" "${first_pid}" "${layer}" "${classify_result}" >> "${output_json}"

        rm -f "${img_path}"
    done < "${local_input}"

    echo "]" >> "${output_json}"

    END_TS=$(date +%s)
    ELAPSED=$((END_TS - START_TS))

    # ── 生成摘要 ──────────────────────────────────────────────
    {
        echo "============================================"
        echo "  图片批量测试摘要"
        echo "============================================"
        echo "输入路径:     ${INPUT}"
        echo "结果文件:     ${OUTPUT}"
        echo "JSON文件:     ${output_json}"
        echo "开始时间:     $(date -d "@${START_TS}" '+%Y-%m-%d %H:%M:%S')"
        echo "结束时间:     $(date -d "@${END_TS}" '+%Y-%m-%d %H:%M:%S')"
        echo "总耗时:       ${ELAPSED}s"
        echo "处理条数:     ${processed}"
        echo "成功:         ${success_count}"
        echo "失败:         ${fail_count}"
        echo "成功率:       $([ "${processed}" -gt 0 ] && echo "scale=1; ${success_count}*100/${processed}" | bc || echo "N/A")%"
        echo "============================================"
    } | tee "${output_summary}"

    # 失败率超过 20% 时非零退出
    if [ "${processed}" -gt 0 ] && [ $((fail_count * 100 / processed)) -gt 20 ]; then
        echo "[警告] 失败率超过 20%，退出码 1"
        exit 1
    fi
}

# ==================== 主流程 ====================

case "${MODE}" in
    single)
        run_single
        ;;
    batch)
        run_batch
        ;;
    *)
        echo "未知模式: ${MODE}"
        usage
        ;;
esac
