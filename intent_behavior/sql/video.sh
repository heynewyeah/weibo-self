#!/bin/bash
# =============================================================
# video.sh — 视频博文全链路测试（单条 + 批量）
# =============================================================
# 功能：
#   1. 单条模式：指定真实 fid + customer_id + 博文内容，获取视频信息、
#      下载封面图，并用封面图做视频分类（方案A）
#   2. 批量模式：从 TSV 文件读取多条视频博文（如 query_video.sh 输出），
#      逐条获取封面图、调用 Qwen3.6 分类、记录结果，并生成统计报告
#
# 数据来源：
#   - 单条：命令行传入的真实 fid + customer_id + 真实博文内容
#   - 批量：HDFS/本地 TSV 文件，字段与 query_video.sh 输出对齐
#           mid \t uid \t content \t media_id \t customer_info(fid/cover JSON) \t dt
#   - 模型服务：vLLM Qwen3.6-35B-A3B
#
# 输出路径：
#   - 单条：终端实时输出
#   - 批量：默认 /tmp/video_test_output/video_batch_<时间戳>.tsv 和 .json
#           （避免项目目录 output/ 因用户权限不同导致写入失败）
#
# 用法：
#   # 单条模式（默认）
#   bash sql/video.sh
#   bash sql/video.sh --fid "2362904:4826598285967434" --cid "2608812381" --text "吉利银河M9..."
#
#   # 批量模式
#   bash sql/video.sh --mode batch --input /dw_ext/ad/person/xuanyu11/intent_behavior/data/video_weibo_ad_20260701_20260701
#   bash sql/video.sh --mode batch --input /path/to/video_data.tsv --limit 10 --output-dir /path/to/writable/dir
#
#   # 开启 API 调试日志
#   bash sql/video.sh --mode batch --input ... --debug
#
# 运行时间预估：
#   - 单条：约 30~120 秒
#   - 批量：约 N × 30~120 秒（N 为视频个数）
#
# 作者：xuanyu11
# 创建时间：2026-08-13
# =============================================================

set -uo pipefail
export LC_ALL=C

# ==================== 配置 ====================
API_URL="http://10.1.126.27:8087/v1/chat/completions"
MODEL="/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"
SHOWBATCH_API="http://i.iotep.tools.biz.weibo.com/api/v1/video/media/showBatch"

DEFAULT_FID="2362904:4826598285967434"
DEFAULT_CID="2608812381"
DEFAULT_TEXT="吉利银河M9极寒测试，零下40度挑战！看看新能源旗舰SUV在极端低温下的真实表现"

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DEFAULT_OUTPUT_DIR="/tmp/video_test_output"
TMP_DIR="/tmp/video_test_$$"

mkdir -p "${TMP_DIR}"
trap 'rm -rf "${TMP_DIR}"; echo ""; echo "[清理] 临时文件已清理"' EXIT

# ==================== 解析参数 ====================
MODE="single"
INPUT=""
OUTPUT_DIR=""
OUTPUT=""
FID=""
CID=""
TEXT=""
LIMIT=0
DEBUG=0

usage() {
    echo "用法:"
    echo "  单条模式: bash sql/video.sh [--fid FID] [--cid CUSTOMER_ID] [--text '博文内容']"
    echo "  批量模式: bash sql/video.sh --mode batch --input <hdfs路径或本地tsv> [--output-dir <可写目录>] [--output <结果文件>] [--limit N] [--debug]"
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
        --fid)
            FID="$2"
            shift 2
            ;;
        --cid)
            CID="$2"
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

FID="${FID:-${DEFAULT_FID}}"
CID="${CID:-${DEFAULT_CID}}"
TEXT="${TEXT:-${DEFAULT_TEXT}}"
OUTPUT_DIR="${OUTPUT_DIR:-${DEFAULT_OUTPUT_DIR}}"

# ==================== 公共函数 ====================

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
    return 0
}

# 调用 showBatch API 获取视频信息（含封面图 URL）
get_video_cover() {
    local media_id="$1"
    local customer_id="$2"

    local api_resp
    api_resp=$(curl -s --location --request GET \
        "${SHOWBATCH_API}?customer_id=${customer_id}&fids=${media_id}" \
        --header 'User-Agent: BlogClassifier/1.0' \
        --header 'Accept: */*' \
        --header 'Connection: keep-alive' 2>/dev/null)

    local api_status
    api_status=$(echo "${api_resp}" | jq -r '.status // empty')

    if [ "${api_status}" != "200" ]; then
        echo ""
        return 1
    fi

    echo "${api_resp}" | jq -r '.data[0].frontUrl // ""'
}

# 下载封面图
download_cover() {
    local url="$1"
    local save_path="$2"

    local http_code file_size
    http_code=$(curl -s -o "${save_path}" -w "%{http_code}" --max-time 30 "${url}" 2>/dev/null)
    file_size=$(stat -c %s "${save_path}" 2>/dev/null || echo 0)

    if [ "${http_code}" != "200" ] || [ "${file_size}" -lt 100 ]; then
        echo "封面图下载失败 HTTP=${http_code} size=${file_size}B"
        return 1
    fi
    return 0
}

# 压缩封面图（如果过大）
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

    echo "封面图过大(${file_size_kb}KB)，尝试压缩..."

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

    echo "未安装 ImageMagick/PIL，使用原图"
    cp "${input_path}" "${output_path}"
    return 0
}

# 调用 Qwen3.6 多模态分类
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

    local payload debug_log curl_stderr http_code resp resp_body
    payload="{
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
    }"

    debug_log="${TMP_DIR}/api_debug_${mid}.log"
    curl_stderr="${TMP_DIR}/api_curl_err_${mid}.log"

    if [ "${DEBUG}" -eq 1 ]; then
        echo "=== REQUEST ===" > "${debug_log}"
        echo "mid=${mid}" >> "${debug_log}"
        echo "img_path=${img_path}" >> "${debug_log}"
        echo "img_size_bytes=$(stat -c %s "${img_path}" 2>/dev/null || echo 0)" >> "${debug_log}"
        echo "prompt=${prompt}" >> "${debug_log}"
        echo "" >> "${debug_log}"
        echo "=== CURL RESPONSE ===" >> "${debug_log}"
    fi

    resp=$(curl -s --max-time 180 -X POST "${API_URL}" \
        -H "Content-Type: application/json" \
        -d "${payload}" \
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

# 从 customer_info JSON 中提取 cover URL 和 fid
extract_video_info() {
    local customer_info="$1"
    echo "${customer_info}" | jq -r '[.fid // "", .cover // ""] | @tsv' 2>/dev/null
}

# ==================== 单条模式 ====================
run_single() {
    echo "============================================"
    echo "  视频全链路测试 — 单条模式"
    echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  模型: ${MODEL}"
    echo "  API:  ${API_URL}"
    echo "============================================"
    echo ""

    START_TS=$(date +%s)

    echo ">> [1/3] 调用 showBatch API 获取视频信息..."
    echo "   fid:         ${FID}"
    echo "   customer_id: ${CID}"

    COVER_URL=$(get_video_cover "${FID}" "${CID}")
    if [ -z "${COVER_URL}" ]; then
        echo "   ❌ 未获取到封面图 URL"
        exit 1
    fi
    echo "   ✅ 封面图 URL: ${COVER_URL}"
    echo ""

    local cover_path="${TMP_DIR}/video_cover.jpg"
    local compressed_path="${TMP_DIR}/video_cover_compressed.jpg"

    echo ">> [2/3] 下载封面图..."
    if ! download_cover "${COVER_URL}" "${cover_path}"; then
        echo "   ❌ 封面图下载失败，退出"
        exit 1
    fi
    local file_size
    file_size=$(stat -c %s "${cover_path}" 2>/dev/null || echo 0)
    echo "   ✅ 下载成功 ${file_size} bytes"

    compress_image_if_needed "${cover_path}" "${compressed_path}"
    echo ""

    echo ">> [3/3] 视频博文分类（封面图 + 真实文字）..."
    echo "   博文文字: ${TEXT}"
    local prompt="请对以下汽车行业视频博文进行分类。\\n博文文字：${TEXT}\\n视频封面图已附上，请结合文字和画面综合判断，直接输出：最终分类结果：【层级名称】"
    local classify_result
    classify_result=$(call_qwen_image "single_${FID}" "${compressed_path}" "${prompt}")

    if [ -z "${classify_result}" ]; then
        echo "   ❌ 模型调用失败: ${CALL_QWEN_IMAGE_RESULT}"
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

    if ! check_writable_dir "${OUTPUT_DIR}"; then
        echo "[建议] 使用 --output-dir 指定一个有写权限的目录，例如："
        echo "       bash sql/video.sh --mode batch --input ... --output-dir /tmp/my_output"
        exit 1
    fi

    local input_file="${INPUT}"
    local local_input="${TMP_DIR}/batch_input.tsv"

    echo "============================================"
    echo "  视频全链路测试 — 批量模式"
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
    OUTPUT="${OUTPUT:-${OUTPUT_DIR}/video_batch_${run_ts}.tsv}"
    local output_json="${OUTPUT%.tsv}.json"
    local output_summary="${OUTPUT%.tsv}_summary.txt"

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

    printf "mid\tuid\tcontent_preview\tfid\tlayer\tsuccess\terror\n" > "${OUTPUT}"

    # ── 逐条处理 ──────────────────────────────────────────────
    local lineno=0
    local success_count=0
    local fail_count=0
    local processed=0

    echo "[" > "${output_json}"
    local first_json=1

    while IFS=$'\t' read -r mid uid content media_id customer_info dt; do
        lineno=$((lineno + 1))

        [ -z "${mid}" ] && continue
        if [ "${mid}" = "mid" ]; then
            continue
        fi

        processed=$((processed + 1))
        if [ "${LIMIT}" -gt 0 ] && [ "${processed}" -gt "${LIMIT}" ]; then
            break
        fi

        echo "[${processed}] mid=${mid} uid=${uid} dt=${dt}"

        # 提取 fid 和 cover
        local video_info
        video_info=$(extract_video_info "${customer_info}")
        local vfid vcover
        vfid=$(echo "${video_info}" | awk -F'\t' '{print $1}')
        vcover=$(echo "${video_info}" | awk -F'\t' '{print $2}')

        if [ -z "${vfid}" ]; then
            echo "   ❌ 未解析到 fid"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "" "" "false" "未解析到fid" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            continue
        fi

        echo "   → fid=${vfid}"

        # 如果没有 cover，尝试 showBatch API 获取
        if [ -z "${vcover}" ]; then
            echo "   → customer_info 中无 cover，调用 showBatch API..."
            vcover=$(get_video_cover "${vfid}" "${uid}")
        fi

        if [ -z "${vcover}" ]; then
            echo "   ❌ 未获取到封面图 URL"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "${vfid}" "" "false" "未获取到封面图URL" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            continue
        fi

        local cover_path="${TMP_DIR}/${mid}_${vfid//:/_}_cover.jpg"
        local compressed_path="${TMP_DIR}/${mid}_${vfid//:/_}_cover_compressed.jpg"

        echo "   → 下载封面图..."
        local dl_error
        dl_error=$(download_cover "${vcover}" "${cover_path}" 2>&1)
        local dl_status=$?
        if [ "${dl_status}" -ne 0 ]; then
            echo "   ❌ ${dl_error}"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "${vfid}" "" "false" "${dl_error}" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            rm -f "${cover_path}"
            continue
        fi

        local file_size
        file_size=$(stat -c %s "${cover_path}" 2>/dev/null || echo 0)
        echo "   ✅ 封面图下载成功 ${file_size} bytes"

        compress_image_if_needed "${cover_path}" "${compressed_path}"

        echo "   → 调用模型分类..."
        local prompt="请对以下汽车行业视频博文进行分类。\\n博文文字：${content}\\n视频封面图已附上，请结合文字和画面综合判断，直接输出：最终分类结果：【层级名称】"
        local classify_result
        classify_result=$(call_qwen_image "${mid}" "${compressed_path}" "${prompt}")

        if [ -z "${classify_result}" ]; then
            echo "   ❌ 模型调用失败: ${CALL_QWEN_IMAGE_RESULT}"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "${vfid}" "" "false" "${CALL_QWEN_IMAGE_RESULT}" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            rm -f "${cover_path}" "${compressed_path}"
            continue
        fi

        local layer
        layer=$(echo "${classify_result}" | grep -oE '认知层|兴趣层|考虑层' | tail -1)
        [ -z "${layer}" ] && layer="未识别"

        echo "   ✅ 分类结果: ${layer}"
        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
            "${mid}" "${uid}" "${content:0:50}" "${vfid}" "${layer}" "true" "" >> "${OUTPUT}"
        success_count=$((success_count + 1))

        if [ "${first_json}" -eq 1 ]; then
            first_json=0
        else
            echo "," >> "${output_json}"
        fi
        printf '{"mid":"%s","uid":"%s","content_preview":"%s","fid":"%s","layer":"%s","success":true,"raw_output":"%s"}' \
            "${mid}" "${uid}" "${content:0:50}" "${vfid}" "${layer}" "${classify_result}" >> "${output_json}"

        rm -f "${cover_path}" "${compressed_path}"
    done < "${local_input}"

    echo "]" >> "${output_json}"

    END_TS=$(date +%s)
    ELAPSED=$((END_TS - START_TS))

    # ── 生成摘要 ──────────────────────────────────────────────
    {
        echo "============================================"
        echo "  视频批量测试摘要"
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
            echo "API 调试日志目录: ${TMP_DIR}"
        fi
    } | tee "${output_summary}"

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
