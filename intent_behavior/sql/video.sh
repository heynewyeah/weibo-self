#!/bin/bash
# =============================================================
# video.sh — 视频博文全链路测试（单条 + 批量）
# =============================================================
# 功能：
#   1. 单条模式：指定真实 fid + customer_id + 博文内容，获取视频信息、
#      处理视频（封面图或抽帧），并调用 Qwen3.6 多模态分类
#   2. 批量模式：从 TSV 文件读取多条视频博文（如 query_video.sh 输出），
#      逐条处理、调用 Qwen3.6 分类、记录结果，并生成统计报告
#
# 视频处理模式（--video-mode）：
#   cover（默认）：调用 showBatch API 获取封面图 URL → 下载封面图 → 多模态分类
#                  快速，无需下载视频，已验证可用
#   frame         ：调用 showBatch API 获取视频 URL → 下载视频 → Python/OpenCV 抽帧
#                  → 多模态分类（需服务器已安装 opencv-python-headless）
#
# 数据来源：
#   - 单条：命令行传入的真实 fid + customer_id + 真实博文内容
#   - 批量：HDFS/本地 TSV 文件，字段与 query_video.sh 输出对齐
#           mid \t uid \t content \t media_id \t customer_info(fid/cover JSON) \t dt
#   - 模型服务：vLLM Qwen3.6-35B-A3B
#
# 输出路径：
#   - 单条：终端实时输出
#   - 批量：默认 /tmp/xuanyu11/video_test_output/video_batch_<时间戳>.tsv 和 .json
#           （避免项目目录 output/ 因用户权限不同导致写入失败）
#
# 用法：
#   # 单条模式（默认，cover 模式）
#   bash sql/video.sh
#   bash sql/video.sh --fid "2362904:4826598285967434" --cid "2608812381" --text "吉利银河M9..."
#
#   # 单条模式，使用抽帧
#   bash sql/video.sh --video-mode frame --fid "2362904:4826598285967434" --cid "2608812381"
#
#   # 批量模式（cover 模式）
#   bash sql/video.sh --mode batch \
#     --input /dw_ext/ad/person/xuanyu11/intent_behavior/data/video_weibo_ad_20260701_20260701 \
#     --limit 10
#
#   # 批量模式（frame 模式）
#   bash sql/video.sh --mode batch --video-mode frame \
#     --input /dw_ext/ad/person/xuanyu11/intent_behavior/data/video_weibo_ad_20260701_20260701 \
#     --limit 5 --debug
#
#   # 开启 API 调试日志
#   bash sql/video.sh --mode batch --input ... --debug
#
# 运行时间预估：
#   - 单条 cover：约 10~30 秒
#   - 单条 frame：约 60~180 秒（含视频下载）
#   - 批量：约 N × 单条耗时
#
# 作者：xuanyu11
# 创建时间：2026-08-13
# 更新时间：2026-08-14（新增 frame 模式）
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

# 系统提示词：与 batch_classify_3layer.sh / config.yaml 对齐
SYS_PROMPT="你是一个汽车行业博文营销分层分类器。请将博文分类到以下3个营销层级之一。\\n\\n可选层级：\\n【认知层】— 主打品牌曝光，高传播高热度，让用户认知品牌，容易形成传播趋势。\\n  典型内容：精美TVC、功能解读类、知识科普类、生活记录情绪共鸣类、话题承接内容、品牌官方宣传片、新车发布会、品牌联名活动。\\n  特征：以品牌/产品曝光为核心目的，传播性强，但未必包含深度产品信息或购买引导。\\n\\n【兴趣层】— 含品牌词，互动率较高，引发讨论、互动，提升用户对产品的兴趣。\\n  典型内容：KOL对比/评测内容、UGC种草内容、产品功能体验分享、试驾vlog、车型亮点解读、用户讨论帖。\\n  特征：有具体产品/品牌的信息，能引发用户兴趣和互动讨论，但尚未涉及具体购买决策信息。\\n\\n【考虑层】— 产品真实测评、竞品横评对比，价格优惠信息，参数分析，帮助用户完成决策。\\n  典型内容：优惠促销活动内容、竞品横评对比、价格/落地价讨论、参数配置对比、用户购车决策分享、购车攻略、经销商活动。\\n  特征：包含帮助用户做出购买决策的具体信息，如价格、优惠、对比、参数、购买渠道等。\\n\\n分类原则：\\n1. 优先看博文的核心目的：是让用户「知道品牌」→认知层；「产生兴趣」→兴趣层；「辅助决策」→考虑层\\n2. 如果博文同时涉及多个层级，按最深层级归类（如同时有品牌曝光和价格优惠，归考虑层）\\n3. 如果博文与汽车行业完全无关，归为【认知层】（兜底，因为广告博文至少有品牌曝光属性）\\n4. 先进行分析推理\\n5. 分析完成后，单独一行输出最终结论，格式必须是：最终分类结果：【层级名称】"

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DEFAULT_OUTPUT_DIR="/tmp/xuanyu11/video_test_output"
TMP_DIR="/tmp/xuanyu11/video_test_$$"

mkdir -p "${TMP_DIR}"
trap 'rm -rf "${TMP_DIR}"; echo ""; echo "[清理] 临时文件已清理"' EXIT

# ==================== 解析参数 ====================
MODE="single"
VIDEO_MODE="cover"   # cover | frame
INPUT=""
OUTPUT_DIR=""
OUTPUT=""
FID=""
CID=""
TEXT=""
LIMIT=0
DEBUG=0
FRAMES=3             # frame 模式下抽帧数量

usage() {
    echo "用法:"
    echo "  单条模式: bash sql/video.sh [--video-mode cover|frame] [--fid FID] [--cid CUSTOMER_ID] [--text '博文内容']"
    echo "  批量模式: bash sql/video.sh --mode batch [--video-mode cover|frame] --input <hdfs路径或本地tsv>"
    echo "            [--output-dir <可写目录>] [--output <结果文件>] [--limit N] [--frames N] [--debug]"
    echo ""
    echo "  --video-mode cover  使用封面图（默认，快速）"
    echo "  --video-mode frame  下载视频并用 OpenCV 抽帧（需 opencv-python-headless）"
    echo "  --frames N          frame 模式下抽取帧数（默认 3）"
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)
            MODE="$2"
            shift 2
            ;;
        --video-mode)
            VIDEO_MODE="$2"
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
        --frames)
            FRAMES="$2"
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

# 校验 video-mode
if [ "${VIDEO_MODE}" != "cover" ] && [ "${VIDEO_MODE}" != "frame" ]; then
    echo "[错误] --video-mode 只支持 cover 或 frame，当前: ${VIDEO_MODE}"
    exit 1
fi

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

# 从模型原始输出中提取层级标签
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

# 调用 showBatch API 获取视频信息（封面图 URL + 视频 URL）
# 输出格式：cover_url\tvideo_url（tab 分隔）
get_video_info() {
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
        echo -e "\t"
        return 1
    fi

    local cover_url video_url
    cover_url=$(echo "${api_resp}" | jq -r '.data[0].frontUrl // .data[0].cover // ""')
    video_url=$(echo "${api_resp}" | jq -r '.data[0].url // .data[0].mp4Url // ""')
    printf '%s\t%s\n' "${cover_url}" "${video_url}"
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

# 下载视频
download_video() {
    local url="$1"
    local save_path="$2"

    local http_code file_size
    http_code=$(curl -s -o "${save_path}" -w "%{http_code}" --max-time 300 "${url}" 2>/dev/null)
    file_size=$(stat -c %s "${save_path}" 2>/dev/null || echo 0)

    if [ "${http_code}" != "200" ] || [ "${file_size}" -lt 1024 ]; then
        echo "视频下载失败 HTTP=${http_code} size=${file_size}B"
        return 1
    fi
    echo "视频下载成功 ${file_size} bytes"
    return 0
}

# 使用 Python/OpenCV 从视频中均匀抽帧
# 参数：视频路径、输出目录、帧数
# 返回：帧图片路径列表（每行一个路径），失败返回空
extract_frames_opencv() {
    local video_path="$1"
    local output_dir="$2"
    local num_frames="${3:-3}"

    mkdir -p "${output_dir}"

    python3 - <<PYEOF 2>/dev/null
import cv2, os, sys

video_path = "${video_path}"
output_dir = "${output_dir}"
num_frames = ${num_frames}

cap = cv2.VideoCapture(video_path)
if not cap.isOpened():
    sys.exit(1)

total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
if total <= 0:
    cap.release()
    sys.exit(1)

actual_n = min(num_frames, total)
if actual_n == 1:
    indices = [total // 2]
else:
    step = total / actual_n
    indices = [int(step * i + step / 2) for i in range(actual_n)]

saved = []
basename = os.path.splitext(os.path.basename(video_path))[0]
for idx, frame_no in enumerate(indices):
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
    ret, frame = cap.read()
    if not ret:
        continue
    out_path = os.path.join(output_dir, f"{basename}_frame{idx:02d}.jpg")
    if cv2.imwrite(out_path, frame):
        saved.append(out_path)

cap.release()
for p in saved:
    print(p)
PYEOF
}

# 压缩图片（如果过大）
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

# 调用 Qwen3.6 多模态分类（支持多张图片）
# 参数：mid、图片路径列表（空格分隔）、提示词
# 返回：模型输出文本（失败时返回空字符串）
CALL_QWEN_IMAGE_RESULT=""
call_qwen_multimodal() {
    local mid="$1"
    local prompt="$2"
    shift 2
    local img_paths=("$@")   # 剩余参数为图片路径列表
    CALL_QWEN_IMAGE_RESULT=""

    if [ ${#img_paths[@]} -eq 0 ]; then
        CALL_QWEN_IMAGE_RESULT="无图片路径"
        echo ""
        return 1
    fi

    # 构建 content 数组（文字 + 多张图片）
    local content_items
    content_items="{\"type\": \"text\", \"text\": \"${prompt}\"}"

    for img_path in "${img_paths[@]}"; do
        local img_b64
        img_b64=$(base64 -w 0 "${img_path}" 2>/dev/null)
        if [ -z "${img_b64}" ]; then
            echo "   [警告] 图片转base64失败: ${img_path}" >&2
            continue
        fi
        content_items="${content_items}, {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${img_b64}\"}}"
    done

    local payload payload_file debug_log curl_stderr http_code resp resp_body
    payload="{
        \"model\": \"${MODEL}\",
        \"messages\": [
            {\"role\": \"system\", \"content\": \"${SYS_PROMPT}\"},
            {\"role\": \"user\", \"content\": [${content_items}]}
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
    if [ "${DEBUG}" -eq 1 ]; then
        echo "=== REQUEST ===" > "${debug_log}"
        echo "mid=${mid}" >> "${debug_log}"
        echo "img_count=${#img_paths[@]}" >> "${debug_log}"
        echo "prompt=${prompt}" >> "${debug_log}"
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
        CALL_QWEN_IMAGE_RESULT="curl错误:$(head -1 "${curl_stderr}")"
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
extract_video_info_from_json() {
    local customer_info="$1"
    echo "${customer_info}" | jq -r '[.fid // "", .cover // ""] | @tsv' 2>/dev/null
}

# ==================== 核心处理函数 ====================

# 处理单条视频：根据 VIDEO_MODE 获取图片（封面图或抽帧），返回图片路径数组
# 参数：fid、customer_id、mid（用于临时文件命名）
# 输出：图片路径列表（每行一个），失败输出空
process_video_to_images() {
    local fid="$1"
    local cid="$2"
    local mid="$3"

    local video_info cover_url video_url
    video_info=$(get_video_info "${fid}" "${cid}")
    cover_url=$(echo "${video_info}" | awk -F'\t' '{print $1}')
    video_url=$(echo "${video_info}" | awk -F'\t' '{print $2}')

    if [ "${VIDEO_MODE}" = "frame" ]; then
        # 方案B：下载视频 → OpenCV 抽帧
        if [ -z "${video_url}" ]; then
            echo "   ❌ 未获取到视频 URL（fid=${fid}）" >&2
            return 1
        fi
        echo "   → 视频 URL: ${video_url}" >&2

        local safe_fid="${fid//:/_}"
        local video_path="${TMP_DIR}/${mid}_${safe_fid}.mp4"
        local frames_dir="${TMP_DIR}/${mid}_${safe_fid}_frames"

        echo "   → 下载视频..." >&2
        local dl_msg
        dl_msg=$(download_video "${video_url}" "${video_path}" 2>&1)
        if [ $? -ne 0 ]; then
            echo "   ❌ ${dl_msg}" >&2
            return 1
        fi
        echo "   ✅ ${dl_msg}" >&2

        echo "   → OpenCV 抽帧（${FRAMES}帧）..." >&2
        local frame_paths
        frame_paths=$(extract_frames_opencv "${video_path}" "${frames_dir}" "${FRAMES}")
        local frame_count
        frame_count=$(echo "${frame_paths}" | grep -c '.' 2>/dev/null || echo 0)

        if [ -z "${frame_paths}" ] || [ "${frame_count}" -eq 0 ]; then
            echo "   ❌ 抽帧失败（OpenCV 未安装或视频损坏）" >&2
            rm -f "${video_path}"
            return 1
        fi
        echo "   ✅ 抽帧成功: ${frame_count} 帧" >&2
        rm -f "${video_path}"
        echo "${frame_paths}"

    else
        # 方案A（默认）：使用封面图
        if [ -z "${cover_url}" ]; then
            echo "   ❌ 未获取到封面图 URL（fid=${fid}）" >&2
            return 1
        fi
        echo "   → 封面图 URL: ${cover_url}" >&2

        local safe_fid="${fid//:/_}"
        local cover_path="${TMP_DIR}/${mid}_${safe_fid}_cover.jpg"
        local compressed_path="${TMP_DIR}/${mid}_${safe_fid}_cover_compressed.jpg"

        echo "   → 下载封面图..." >&2
        if ! download_cover "${cover_url}" "${cover_path}"; then
            echo "   ❌ 封面图下载失败" >&2
            return 1
        fi
        local file_size
        file_size=$(stat -c %s "${cover_path}" 2>/dev/null || echo 0)
        echo "   ✅ 封面图下载成功 ${file_size} bytes" >&2

        compress_image_if_needed "${cover_path}" "${compressed_path}" >&2
        echo "${compressed_path}"
    fi
}

# ==================== 单条模式 ====================
run_single() {
    echo "============================================"
    echo "  视频全链路测试 — 单条模式"
    echo "  时间:       $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  模型:       ${MODEL}"
    echo "  API:        ${API_URL}"
    echo "  视频模式:   ${VIDEO_MODE}"
    echo "============================================"
    echo ""

    START_TS=$(date +%s)

    echo ">> [1/3] 获取视频信息..."
    echo "   fid:         ${FID}"
    echo "   customer_id: ${CID}"

    local img_paths_str
    img_paths_str=$(process_video_to_images "${FID}" "${CID}" "single")
    if [ -z "${img_paths_str}" ]; then
        echo "   ❌ 视频处理失败，退出"
        exit 1
    fi

    # 将路径字符串转为数组
    local img_paths_arr=()
    while IFS= read -r line; do
        [ -n "${line}" ] && img_paths_arr+=("${line}")
    done <<< "${img_paths_str}"

    echo ""
    echo ">> [2/3] 图片准备完成: ${#img_paths_arr[@]} 张"
    for p in "${img_paths_arr[@]}"; do
        echo "   - ${p}"
    done
    echo ""

    echo ">> [3/3] 视频博文分类..."
    echo "   博文文字: ${TEXT}"
    local prompt="请对以下汽车行业视频博文进行营销分层分类。\\n\\n博文文字内容：\\n${TEXT}\\n\\n视频画面已附上（${VIDEO_MODE}模式），请结合文字和画面综合判断。\\n\\n请分析后，最后一行输出：最终分类结果：【层级名称】"

    local classify_result
    classify_result=$(call_qwen_multimodal "single_${FID}" "${prompt}" "${img_paths_arr[@]}")

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
    echo "  视频模式: ${VIDEO_MODE}"
    echo "  总耗时:   $((END_TS - START_TS))s"
    echo "  时间:     $(date '+%Y-%m-%d %H:%M:%S')"
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
    echo "  视频模式:   ${VIDEO_MODE}"
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
    OUTPUT="${OUTPUT:-${OUTPUT_DIR}/video_batch_${VIDEO_MODE}_${run_ts}.tsv}"
    local output_json="${OUTPUT%.tsv}.json"
    local output_summary="${OUTPUT%.tsv}_summary.txt"
    PERSIST_DEBUG_DIR="${OUTPUT_DIR}/debug_${run_ts}"

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

    printf "mid\tuid\tcontent_preview\tfid\tvideo_mode\tlayer\traw_output\tsuccess\terror\n" > "${OUTPUT}"

    # ── 逐条处理 ──────────────────────────────────────────
    local lineno=0
    local success_count=0
    local fail_count=0
    local processed=0

    echo "[" > "${output_json}"
    local first_json=1

    while IFS=$'\t' read -r mid uid content media_id customer_info dt; do
        lineno=$((lineno + 1))

        [ -z "${mid}" ] && continue
        [ "${mid}" = "mid" ] && continue

        processed=$((processed + 1))
        if [ "${LIMIT}" -gt 0 ] && [ "${processed}" -gt "${LIMIT}" ]; then
            break
        fi

        echo "[${processed}] mid=${mid} uid=${uid} dt=${dt}"

        # 提取 fid（从 customer_info JSON）
        local video_info_json
        video_info_json=$(extract_video_info_from_json "${customer_info}")
        local vfid
        vfid=$(echo "${video_info_json}" | awk -F'\t' '{print $1}')

        if [ -z "${vfid}" ]; then
            echo "   ❌ 未解析到 fid"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "" "${VIDEO_MODE}" "" "" "false" "未解析到fid" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            continue
        fi

        echo "   → fid=${vfid}"

        # 获取图片（封面图或抽帧）
        local img_paths_str
        img_paths_str=$(process_video_to_images "${vfid}" "${uid}" "${mid}")
        if [ -z "${img_paths_str}" ]; then
            echo "   ❌ 视频处理失败"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "${vfid}" "${VIDEO_MODE}" "" "" "false" "视频处理失败" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            continue
        fi

        local img_paths_arr=()
        while IFS= read -r line; do
            [ -n "${line}" ] && img_paths_arr+=("${line}")
        done <<< "${img_paths_str}"

        echo "   → 调用模型分类（${#img_paths_arr[@]}张图片）..."
        local prompt="请对以下汽车行业视频博文进行营销分层分类。\\n\\n博文文字内容：\\n${content}\\n\\n视频画面已附上（${VIDEO_MODE}模式），请结合文字和画面综合判断。\\n\\n请分析后，最后一行输出：最终分类结果：【层级名称】"

        local classify_result
        classify_result=$(call_qwen_multimodal "${mid}" "${prompt}" "${img_paths_arr[@]}")

        # 清理临时图片
        for p in "${img_paths_arr[@]}"; do
            rm -f "${p}"
        done

        if [ -z "${classify_result}" ]; then
            echo "   ❌ 模型调用失败: ${CALL_QWEN_IMAGE_RESULT}"
            printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
                "${mid}" "${uid}" "${content:0:50}" "${vfid}" "${VIDEO_MODE}" "" "" "false" "${CALL_QWEN_IMAGE_RESULT}" >> "${OUTPUT}"
            fail_count=$((fail_count + 1))
            continue
        fi

        local layer
        layer=$(extract_layer "${classify_result}")

        if [ "${layer}" = "未识别" ]; then
            echo "   ⚠️  未识别，原始输出: ${classify_result}"
        else
            echo "   ✅ 分类结果: ${layer}"
        fi

        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
            "${mid}" "${uid}" "${content:0:50}" "${vfid}" "${VIDEO_MODE}" "${layer}" "${classify_result}" "true" "" >> "${OUTPUT}"
        success_count=$((success_count + 1))

        if [ "${first_json}" -eq 1 ]; then
            first_json=0
        else
            echo "," >> "${output_json}"
        fi
        printf '{"mid":"%s","uid":"%s","content_preview":"%s","fid":"%s","video_mode":"%s","layer":"%s","success":true}' \
            "${mid}" "${uid}" "${content:0:50}" "${vfid}" "${VIDEO_MODE}" "${layer}" >> "${output_json}"

    done < "${local_input}"

    echo "]" >> "${output_json}"

    END_TS=$(date +%s)
    ELAPSED=$((END_TS - START_TS))

    # ── 生成摘要 ──────────────────────────────────────────
    {
        echo "============================================"
        echo "  视频批量测试摘要"
        echo "============================================"
        echo "输入路径:     ${INPUT}"
        echo "视频模式:     ${VIDEO_MODE}"
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
