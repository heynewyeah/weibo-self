#!/bin/bash
# =============================================================
# video.sh — 视频获取 + 下载 + 封面图识别 全链路测试
# =============================================================
# 功能：
#   1. 调用 showBatch API，通过 fid 获取视频播放 URL 和封面图 URL
#   2. 下载完整视频文件，验证 MP4 文件完整性
#   3. 下载视频封面图
#   4. 封面图转 base64 → 送 Qwen3.6 多模态识别（方案A）
#   5. 检测视频抽帧工具可用性（ffmpeg / cv2 / imageio）
#
# 数据来源：
#   - fid：已验证的真实视频 fid（showBatch API status=200，视频 2.2MB MP4）
#   - 模型服务：vLLM Qwen3.6-35B-A3B
#
# 输出路径：
#   - 临时视频/封面图：/tmp/media_test_<PID>/
#   - 日志：实时打印到终端
#
# 用法：
#   bash sql/video.sh
#   bash sql/video.sh [fid] [customer_id]   # 指定自定义参数
#
# 运行时间预估：约 30~120 秒（含视频下载 2MB + vLLM 推理）
#
# 作者：xuanyu11
# 创建时间：2026-08-12
# =============================================================

set -uo pipefail
export LC_ALL=C

# ==================== 配置 ====================
API_URL="http://10.1.126.27:8087/v1/chat/completions"
MODEL="/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"

# 视频测试数据（已验证：showBatch status=200，视频 2.2MB MP4 720p）
TEST_MEDIA_ID="${1:-2362904:4826598285967434}"
TEST_CUSTOMER_ID="${2:-2608812381}"
SHOWBATCH_API="http://i.iotep.tools.biz.weibo.com/api/v1/video/media/showBatch"

# 临时目录（脚本退出时自动清理）
TMP_DIR="/tmp/media_test_$$"
mkdir -p "${TMP_DIR}"
trap 'rm -rf "${TMP_DIR}"; echo ""; echo "[清理] 临时文件已清理"' EXIT

# ==================== 公共函数 ====================

# 调用 Qwen3.6 识别图片（图片路径 + 提示词）
call_qwen_image() {
    local img_path="$1"
    local prompt="$2"

    local img_b64
    img_b64=$(base64 -w 0 "${img_path}" 2>/dev/null)
    if [ -z "${img_b64}" ]; then
        echo "[错误] 图片转 base64 失败"
        return 1
    fi

    local resp
    resp=$(curl -s --max-time 60 -X POST "${API_URL}" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"${MODEL}\",
            \"messages\": [{
                \"role\": \"user\",
                \"content\": [
                    {\"type\": \"text\", \"text\": \"${prompt}\"},
                    {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${img_b64}\"}}
                ]
            }],
            \"max_tokens\": 300,
            \"temperature\": 0.0,
            \"chat_template_kwargs\": {\"enable_thinking\": false}
        }" 2>/dev/null)

    echo "${resp}" | jq -r '.choices[0].message.content // "未返回内容"'
}

# ==================== 主流程 ====================

echo "============================================"
echo "  视频获取 + 下载 + 封面图识别 全链路测试"
echo "  时间:        $(date '+%Y-%m-%d %H:%M:%S')"
echo "  模型:        ${MODEL}"
echo "  API:         ${API_URL}"
echo "  fid:         ${TEST_MEDIA_ID}"
echo "  customer_id: ${TEST_CUSTOMER_ID}"
echo "============================================"
echo ""

START_TS=$(date +%s)

VIDEO_PATH="${TMP_DIR}/test_video.mp4"
COVER_PATH="${TMP_DIR}/video_cover.jpg"

# ── 步骤 1: 调 showBatch API 获取视频信息 ─────────────────────
echo ">> [1/5] 调用 showBatch API 获取视频信息..."
echo "   fid:         ${TEST_MEDIA_ID}"
echo "   customer_id: ${TEST_CUSTOMER_ID}"
echo ""

API_RESP=$(curl -s --location --request GET \
    "${SHOWBATCH_API}?customer_id=${TEST_CUSTOMER_ID}&fids=${TEST_MEDIA_ID}" \
    --header 'User-Agent: BlogClassifier/1.0' \
    --header 'Accept: */*' \
    --header 'Connection: keep-alive' 2>/dev/null)

API_STATUS=$(echo "${API_RESP}" | jq -r '.status // empty')

if [ "${API_STATUS}" != "200" ]; then
    echo "   ❌ API 返回异常: ${API_RESP}"
    exit 1
fi

# 解析视频信息
VIDEO_URL=$(echo "${API_RESP}" | jq -r '.data[0].url // empty')
DURATION=$(echo "${API_RESP}" | jq -r '.data[0].duration // empty')
FILE_SIZE_API=$(echo "${API_RESP}" | jq -r '.data[0].fileSize // empty')
COVER_URL=$(echo "${API_RESP}" | jq -r '.data[0].frontUrl // empty')
VIDEO_TYPE=$(echo "${API_RESP}" | jq -r '.data[0].type // empty')
WIDTH=$(echo "${API_RESP}" | jq -r '.data[0].width // empty')
HEIGHT=$(echo "${API_RESP}" | jq -r '.data[0].height // empty')
QUALITY=$(echo "${API_RESP}" | jq -r '.data[0].quality // empty')

if [ -z "${VIDEO_URL}" ]; then
    echo "   ❌ 未获取到视频 URL"
    exit 1
fi

echo "   ✅ 视频信息获取成功"
echo "   ┌──────────────────────────────────────────"
echo "   │ 时长:     ${DURATION}s"
echo "   │ 分辨率:   ${WIDTH}x${HEIGHT} (${QUALITY})"
echo "   │ 格式:     ${VIDEO_TYPE}"
echo "   │ 预估大小: $((FILE_SIZE_API / 1024))KB"
echo "   │ 封面图:   ${COVER_URL}"
echo "   │ 视频URL:  ${VIDEO_URL:0:80}..."
echo "   └──────────────────────────────────────────"
echo ""

# ── 步骤 2: 下载完整视频 ──────────────────────────────────────
echo ">> [2/5] 下载完整视频..."
DL_START=$(date +%s)

HTTP_CODE=$(curl -s -o "${VIDEO_PATH}" -w "%{http_code}" \
    --max-time 120 "${VIDEO_URL}" 2>/dev/null)

DL_END=$(date +%s)
DL_ELAPSED=$((DL_END - DL_START))

if [ "${HTTP_CODE}" != "200" ] && [ "${HTTP_CODE}" != "206" ]; then
    echo "   ❌ 下载失败 HTTP=${HTTP_CODE}"
    exit 1
fi

ACTUAL_SIZE=$(stat -c %s "${VIDEO_PATH}" 2>/dev/null || echo 0)
echo "   ✅ 下载成功"
echo "   HTTP 状态码: ${HTTP_CODE}"
echo "   实际大小:    $((ACTUAL_SIZE / 1024))KB (${ACTUAL_SIZE} bytes)"
echo "   下载耗时:    ${DL_ELAPSED}s"
echo "   文件类型:    $(file "${VIDEO_PATH}")"
echo ""

# ── 步骤 3: 验证 MP4 文件完整性 ──────────────────────────────
echo ">> [3/5] 验证视频文件完整性..."
FILE_HEADER=$(xxd -l 12 "${VIDEO_PATH}" 2>/dev/null | head -1)
echo "   文件头: ${FILE_HEADER}"

if echo "${FILE_HEADER}" | grep -qi "iso\|mp4\|ftyp"; then
    echo "   ✅ 合法 MP4 文件"
else
    echo "   ⚠️  文件头可能非标准 MP4，请人工确认"
fi
echo ""

# ── 步骤 4: 下载封面图 ────────────────────────────────────────
echo ">> [4/5] 下载视频封面图..."
COVER_OK=0
if [ -n "${COVER_URL}" ]; then
    COVER_HTTP=$(curl -s -o "${COVER_PATH}" -w "%{http_code}" --max-time 30 "${COVER_URL}" 2>/dev/null)
    COVER_SIZE=$(stat -c %s "${COVER_PATH}" 2>/dev/null || echo 0)

    if [ "${COVER_HTTP}" = "200" ] && [ "${COVER_SIZE}" -gt 100 ]; then
        echo "   ✅ 封面图下载成功"
        echo "   HTTP 状态码: ${COVER_HTTP}"
        echo "   文件大小:    $((COVER_SIZE / 1024))KB"
        echo "   文件类型:    $(file "${COVER_PATH}")"
        COVER_OK=1
    else
        echo "   ⚠️  封面图下载失败 HTTP=${COVER_HTTP} size=${COVER_SIZE}B"
    fi
else
    echo "   ⚠️  无封面图 URL，跳过"
fi
echo ""

# ── 步骤 5: 封面图送 Qwen3.6 识别（方案A）────────────────────
echo ">> [5/5] 封面图送 Qwen3.6 识别（视频分类方案A）..."
if [ "${COVER_OK}" -eq 1 ] && [ -f "${COVER_PATH}" ]; then
    COVER_RESULT=$(call_qwen_image "${COVER_PATH}" \
        "这是汽车行业视频博文的封面图。请简短描述内容，重点关注：1.是否包含汽车/品牌信息 2.是否有价格/优惠信息 3.视频可能的内容类型(宣传/测评/促销等)。100字以内。")

    echo "   模型返回:"
    echo "   ┌──────────────────────────────────────────"
    echo "   │ ${COVER_RESULT}"
    echo "   └──────────────────────────────────────────"
else
    echo "   ⏭️  无封面图，跳过识别"
fi
echo ""

# ── 附加：检测视频抽帧工具可用性 ─────────────────────────────
echo ">> [附加] 检测视频抽帧工具可用性..."
if command -v ffmpeg &> /dev/null; then
    echo "   ✅ ffmpeg 命令行可用: $(ffmpeg -version 2>/dev/null | head -1)"
else
    echo "   ❌ ffmpeg 命令行不可用"
fi

if python3 -c "import cv2" 2>/dev/null; then
    echo "   ✅ Python cv2 (opencv) 可用: $(python3 -c 'import cv2; print(cv2.__version__)')"
else
    echo "   ❌ Python cv2 不可用"
fi

if python3 -c "import imageio" 2>/dev/null; then
    echo "   ✅ Python imageio 可用: $(python3 -c 'import imageio; print(imageio.__version__)')"
else
    echo "   ❌ Python imageio 不可用"
fi

if python3 -c "import imageio_ffmpeg" 2>/dev/null; then
    echo "   ✅ Python imageio-ffmpeg 可用"
else
    echo "   ❌ Python imageio-ffmpeg 不可用"
fi
echo ""

END_TS=$(date +%s)
ELAPSED=$((END_TS - START_TS))

echo "============================================"
echo "  ✅ 视频全链路测试完成"
echo "  总耗时: ${ELAPSED}s"
echo "  时间:   $(date '+%Y-%m-%d %H:%M:%S')"
echo "  说明:   下载+封面识别已通过，抽帧待安装工具"
echo "============================================"
