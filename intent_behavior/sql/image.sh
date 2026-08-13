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
#   - 批量：默认 /tmp/image_test_output/image_batch_<时间戳>.tsv 和 .json
#           （避免项目目录 output/ 因用户权限不同导致写入失败）
#
# 用法：
#   # 单条模式（默认）
#   bash sql/image.sh
#   bash sql/image.sh --pid "006mX07Rly8ifv3xs5535j30ud0plk1m" --text "比亚迪宋L实拍..."
#
#   # 批量模式
#   bash sql/image.sh --mode batch --input /dw_ext/ad/person/xuanyu11/intent_behavior/data/image_weibo_ad_20260701_20260701
#   bash sql/image.sh --mode batch --input /path/to/image_data.tsv --limit 10 --output-dir /path/to/writable/dir
#
#   # 开启 API 调试日志（保存每次请求/响应原始内容）
#   bash sql/image.sh --mode batch --input ... --debug
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

# 系统提示词：与 batch_classify_3layer.sh / config.yaml 对齐
SYS_PROMPT="你是一个汽车行业博文营销分层分类器。请将博文分类到以下3个营销层级之一。\\n\\n可选层级：\\n【认知层】— 主打品牌曝光，高传播高热度，让用户认知品牌，容易形成传播趋势。\\n  典型内容：精美TVC、功能解读类、知识科普类、生活记录情绪共鸣类、话题承接内容、品牌官方宣传片、新车发布会、品牌联名活动。\\n  特征：以品牌/产品曝光为核心目的，传播性强，但未必包含深度产品信息或购买引导。\\n\\n【兴趣层】— 含品牌词，互动率较高，引发讨论、互动，提升用户对产品的兴趣。\\n  典型内容：KOL对比/评测内容、UGC种草内容、产品功能体验分享、试驾vlog、车型亮点解读、用户讨论帖。\\n  特征：有具体产品/品牌的信息，能引发用户兴趣和互动讨论，但尚未涉及具体购买决策信息。\\n\\n【考虑层】— 产品真实测评、竞品横评对比，价格优惠信息，参数分析，帮助用户完成决策。\\n  典型内容：优惠促销活动内容、竞品横评对比、价格/落地价讨论、参数配置对比、用户购车决策分享、购车攻略、经销商活动。\\n  特征：包含帮助用户做出购买决策的具体信息，如价格、优惠、对比、参数、购买渠道等。\\n\\n分类原则：\\n1. 优先看博文的核心目的：是让用户「知道品牌」→认知层；「产生兴趣」→兴趣层；「辅助决策」→考虑层\\n2. 如果博文同时涉及多个层级，按最深层级归类（如同时有品牌曝光和价格优惠，归考虑层）\\n3. 如果博文与汽车行业完全无关，归为【认知层】（兜底，因为广告博文至少有品牌曝光属性）\\n4. 先进行分析推理\\n5. 分析完成后，单独一行输出最终结论，格式必须是：最终分类结果：【层级名称】"

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# 默认输出到 /tmp，避免跨 Linux 用户权限问题
DEFAULT_OUTPUT_DIR="/tmp/image_test_output"
TMP_DIR="/tmp/image_test_$$"

mkdir -p "${TMP_DIR}"
trap 'rm -rf "${TMP_DIR}"; echo ""; echo "[清理] 临时文件已清理"' EXIT

# ==================== 解析参数 ====================
MODE="single"
INPUT=""
OUTPUT_DIR=""
OUTPUT=""
PID=""
TEXT=""
LIMIT=0
DEBUG=0

usage() {
    echo "用法:"
    echo "  单条模式: bash sql/image.sh [--pid PID] [--text '博文内容']"
    echo "  批量模式: bash sql/image.sh --mode batch --input <hdfs路径或本地tsv> [--output-dir <可写目录>] [--output <结果文件>] [--limit N] [--debug]"
    exit 1
}

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
        --output-dir)
            OUTPUT_DIR="$2"
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
        --debug)
            DEBUG=1
            shift
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
OUTPUT_DIR="${OUTPUT_DIR:-${DEFAULT_OUTPUT_DIR}}"

# ==================== 公共函数 ====================

# 检查目录是否可写
check_writable_dir() {
    local dir="$1"
    if [ ! -d "${dir}" ]; then
        mkdir -p "${dir}" 2>/dev/null || {
            echo "[错误] 无法创建输出目录: ${dir}"
            return 1
        }
    fi
    if [ ! -w "${dir}" ]; then
        echo "[错误] 输出目录无写权限: ${dir}"
        return 1
    fi
    }
    
    # 从模型原始输出中提取层级标签
    # 优先匹配中文三层；匹配不到时尝试英文/拼音关键词；仍失败返回"未识别"
    extract_layer() {
        local raw="$1"
        local layer
    
        layer=$(echo "${raw}" | grep -oE '认知层|兴趣层|考虑层' | tail -1)
        if [ -n "${layer}" ]; then
            echo "${layer}"
            return 0
        fi
    
        layer=$(echo "${raw}" | grep -oiE 'awareness|cognition|interest|consideration|认知|兴趣|考虑' | tail -1)
        case "${layer}" in
            [Aa]wareness|[Cc]ognition|认知)
                echo "认知层"
                ;;
            [Ii]nterest|兴趣)
                echo "兴趣层"
                ;;
            [Cc]onsideration|考虑)
                echo "考虑层"
                ;;
            *)
                echo "未识别"
                ;;
        esac
    }
    
    # 压缩图片（如果图片过大）
    # 策略：优先用 ImageMagick convert，否则用 Python PIL，否则返回原图路径
    compress_image_if_needed() {
    local input_path="$1"
    local output_path="$2"
    local max_size_kb=500
    local max_pixels=1024

    local file_size_kb
    file_size_kb=$(stat -c %s "${input_path}" 2>/dev/null | awk '{print int($1/1024)}')

    if [ "${file_size_kb}" -le "${max_size_kb}" ]; then
        cp "${input_path}" "${output_path}"
        echo "无需压缩"
        return 0
    fi

    echo "图片过大(${file_size_kb}KB)，尝试压缩..."

    if command -v convert &>/dev/null; then
        convert "${input_path}" -resize "${max_pixels}x${max_pixels}>" -quality 85 "${output_path}" 2>/dev/null && {
            local new_size
            new_size=$(stat -c %s "${output_path}" 2>/dev/null | awk '{print int($1/1024)}')
            echo "已压缩至 ${new_size}KB (ImageMagick)"
            return 0
        }
    fi

    if python3 -c "from PIL import Image" 2>/dev/null; then
        python3 - <<PYEOF
from PIL import Image
import sys
img = Image.open("${input_path}")
img.thumbnail((${max_pixels}, ${max_pixels}))
img.save("${output_path}", quality=85, optimize=True)
PYEOF
        if [ -f "${output_path}" ]; then
            local new_size
            new_size=$(stat -c %s "${output_path}" 2>/dev/null | awk '{print int($1/1024)}')
            echo "已压缩至 ${new_size}KB (PIL)"
            return 0
        fi
    fi

    echo "未安装 ImageMagick/PIL，使用原图（可能请求过大导致 API 失败）"
    cp "${input_path}" "${output_path}"
    return 0
}

# 调用 Qwen3.6 多模态分类
# 参数：mid(用于日志)、图片路径、提示词
# 返回：模型输出文本（失败时返回空字符串）
# 全局变量 DEBUG 控制是否保存原始响应
CALL_QWEN_IMAGE_RESULT=""
call_qwen_image() {
    local mid="$1"
    local img_path="$2"
    local prompt="$3"
    CALL_QWEN_IMAGE_RESULT=""

    local img_b64
    img_b64=$(base64 -w 0 "${img_path}" 2>/dev/null)
    if [ -z "${img_b64}" ]; then
        CALL_QWEN_IMAGE_RESULT="图片转base64失败"
        echo ""
        return 1
    fi

        local payload payload_file debug_log curl_stderr http_code resp
        payload="{
            \"model\": \"${MODEL}\",
            \"messages\": [
                {\"role\": \"system\", \"content\": \"${SYS_PROMPT}\"},
                {\"role\": \"user\", \"content\": [
                    {\"type\": \"text\", \"text\": \"${prompt}\"},
                    {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${img_b64}\"}}
                ]}
            ],
            \"max_tokens\": 512,
            \"top_p\": 1.0,
            \"top_k\": 0,
            \"seed\": 42,
            \"thinking\": {\"type\": \"disabled\"},
            \"reasoning\": {\"effort\": \"none\"},
            \"temperature\": 0.0,
            \"chat_template_kwargs\": {\"enable_thinking\": false}
        }"
        payload_file="${TMP_DIR}/payload_${mid}.json"
        printf '%s' "${payload}" > "${payload_file}"
    
        debug_log="${TMP_DIR}/api_debug_${mid}.log"
        curl_stderr="${TMP_DIR}/api_curl_err_${mid}.log"
    # 保存请求体（调试用）
    if [ "${DEBUG}" -eq 1 ]; then
        echo "=== REQUEST ===" > "${debug_log}"
        echo "mid=${mid}" >> "${debug_log}"
        echo "img_path=${img_path}" >> "${debug_log}"
        echo "img_size_bytes=$(stat -c %s "${img_path}" 2>/dev/null || echo 0)" >> "${debug_log}"
                echo "prompt=${prompt}" >> "${debug_log}"
                echo "payload_file=${payload_file}" >> "${debug_log}"
                echo "" >> "${debug_log}"
                echo "=== CURL RESPONSE ===" >> "${debug_log}"
            fi
        
            resp=$(curl -s --max-time 180 -X POST "${API_URL}" \
                -H "Content-Type: application/json" \
                --data @"${payload_file}" \
                -w "\nHTTP_CODE:%{http_code}" \
                2>"${curl_stderr}")
    http_code=$(echo "${resp}" | grep -oE 'HTTP_CODE:[0-9]+' | cut -d: -f2)
    resp_body=$(echo "${resp}" | sed '/HTTP_CODE:/d')

        if [ "${DEBUG}" -eq 1 ]; then
            echo "http_code=${http_code}" >> "${debug_log}"
            echo "${resp_body}" >> "${debug_log}"
            echo "curl_stderr:" >> "${debug_log}"
            cat "${curl_stderr}" >> "${debug_log}" 2>/dev/null || true
            echo "Debug日志: ${debug_log}"
            if [ -n "${PERSIST_DEBUG_DIR:-}" ]; then
                mkdir -p "${PERSIST_DEBUG_DIR}"
                cp "${debug_log}" "${PERSIST_DEBUG_DIR}/api_debug_${mid}.log"
            fi
        fi
    if [ -s "${curl_stderr}" ]; then
        CALL_QWEN_IMAGE_RESULT="curl错误:$(cat "${curl_stderr}" | head -1)"
        echo ""
        return 1
    fi

    if [ "${http_code}" != "200" ]; then
        CALL_QWEN_IMAGE_RESULT="HTTP${http_code}"
        echo ""
        return 1
    fi

    if [ -z "${resp_body}" ]; then
        CALL_QWEN_IMAGE_RESULT="响应体为空"
        echo ""
        return 1
    fi

    # 检查 choices 是否存在
    local choices_len
    choices_len=$(echo "${resp_body}" | jq '.choices | length' 2>/dev/null || echo 0)
    if [ "${choices_len}" -eq 0 ]; then
        CALL_QWEN_IMAGE_RESULT="API无choices:${resp_body:0:200}"
        echo ""
        return 1
    fi

    local content
    content=$(echo "${resp_body}" | jq -r '.choices[0].message.content // ""')
    if [ -z "${content}" ]; then
        CALL_QWEN_IMAGE_RESULT="模型content为空"
        echo ""
        return 1
    fi

    CALL_QWEN_IMAGE_RESULT="OK"
    echo "${content}"
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
    local prompt="请对以下汽车行业图文博文进行营销分层分类。\\n\\n博文文字内容：\\n${TEXT}\\n\\n博文配图已附上，请结合文字和图片综合判断。\\n\\n请分析后，最后一行输出：最终分类结果：【层级名称】"
    local classify_result
    classify_result=$(call_qwen_image "single_${PID}" "${save_path}" "${prompt}")

    if [ -z "${classify_result}" ]; then
        echo "   ❌ 模型调用失败: ${CALL_QWEN_IMAGE_RESULT}"
        exit 1
    fi

    local layer
    layer=$(extract_layer "${classify_result}")

    echo "   模型原始输出:"
    echo "   ┌──────────────────────────────────────────"
    echo "   │ ${classify_result}"
    echo "   └──────────────────────────────────────────"
    echo "   提取标签: ${layer}"

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

    # 检查输出目录可写性
    if ! check_writable_dir "${OUTPUT_DIR}"; then
        echo "[建议] 使用 --output-dir 指定一个有写权限的目录，例如："
        echo "       bash sql/image.sh --mode batch --input ... --output-dir /tmp/my_output"
        exit 1
    fi

    local input_file="${INPUT}"
    local local_input="${TMP_DIR}/batch_input.tsv"

    echo "============================================"
    echo "  图片全链路测试 — 批量模式"
    echo "  时间:       $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  输入:       ${input_file}"
    echo "  输出目录:   ${OUTPUT_DIR}"
    echo "  模型:       ${MODEL}"
    echo "============================================"
    echo ""

    START_TS=$(date +%s)

    # ── 获取输入数据 ──────────────────────────────────────────
    if [[ "${input_file}" == hdfs://* ]]; then
        input_file="${input_file#hdfs://}"
    fi

    if hdfs dfs -test -d "${input_file}" 2>/dev/null; then
        echo ">> 从 HDFS 目录读取数据: ${input_file}"
        hdfs dfs -cat "${input_file}/*" > "${local_input}" 2>/dev/null || {
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
        PERSIST_DEBUG_DIR="${OUTPUT_DIR}/debug_${run_ts}"
    # 检查输出文件是否可写
    if ! touch "${OUTPUT}" 2>/dev/null; then
        echo "[错误] 无法写入结果文件: ${OUTPUT}"
        exit 1
    fi
    if ! touch "${output_json}" 2>/dev/null || ! touch "${output_summary}" 2>/dev/null; then
        echo "[错误] 无法写入 JSON/摘要文件到 ${OUTPUT_DIR}"
        exit 1
    fi

    echo "   结果文件: ${OUTPUT}"
    echo "   摘要文件: ${output_summary}"
    echo ""
    # 表头
    printf "mid\tuid\tcontent_preview\tpid\tlayer\traw_output\tsuccess\terror\n" > "${OUTPUT}"

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
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "" "" "" "false" "未解析到pid" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            continue
        fi

        local img_path="${TMP_DIR}/${mid}_${first_pid}.jpg"
        local compressed_path="${TMP_DIR}/${mid}_${first_pid}_compressed.jpg"

        echo "   → 下载图片 pid=${first_pid}"
        local dl_error
        dl_error=$(download_image "${first_pid}" "${img_path}" 2>&1)
        local dl_status=$?
                if [ "${dl_status}" -ne 0 ]; then
                    echo "   ❌ ${dl_error}"
                    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                        "${mid}" "${uid}" "${content:0:50}" "${first_pid}" "" "" "false" "${dl_error}" >> "${OUTPUT}"
                    fail_count=$((fail_count + 1))
                    rm -f "${img_path}"
                    continue
                fi
        local file_size
        file_size=$(stat -c %s "${img_path}" 2>/dev/null || echo 0)
        echo "   ✅ 图片下载成功 ${file_size} bytes"

        # 图片压缩
        echo "   → 检查图片大小..."
        compress_image_if_needed "${img_path}" "${compressed_path}"

        echo "   → 调用模型分类..."
        local prompt="请对以下汽车行业图文博文进行营销分层分类。\\n\\n博文文字内容：\\n${content}\\n\\n博文配图已附上，请结合文字和图片综合判断。\\n\\n请分析后，最后一行输出：最终分类结果：【层级名称】"
        local classify_result
        classify_result=$(call_qwen_image "${mid}" "${compressed_path}" "${prompt}")
        if [ -z "${classify_result}" ]; then
            echo "   ❌ 模型调用失败: ${CALL_QWEN_IMAGE_RESULT}"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "${first_pid}" "" "" "false" "${CALL_QWEN_IMAGE_RESULT}" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            rm -f "${img_path}" "${compressed_path}"
            continue
        fi

        # 提取层级标签
        local layer
        layer=$(extract_layer "${classify_result}")

        if [ "${layer}" = "未识别" ]; then
            echo "   ⚠️  未识别，原始输出: ${classify_result}"
        else
            echo "   ✅ 分类结果: ${layer}"
        fi
        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
            "${mid}" "${uid}" "${content:0:50}" "${first_pid}" "${layer}" "${classify_result}" "true" "" >> "${OUTPUT}"
        success_count=$((success_count + 1))

        # 写入 JSON
        if [ "${first_json}" -eq 1 ]; then
            first_json=0
        else
            echo "," >> "${output_json}"
        fi
        printf '{"mid":"%s","uid":"%s","content_preview":"%s","pid":"%s","layer":"%s","success":true,"raw_output":"%s"}' \
            "${mid}" "${uid}" "${content:0:50}" "${first_pid}" "${layer}" "${classify_result}" >> "${output_json}"

        rm -f "${img_path}" "${compressed_path}"
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
        if [ "${DEBUG}" -eq 1 ]; then
            echo "API 调试日志目录: ${PERSIST_DEBUG_DIR}"
        fi
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
