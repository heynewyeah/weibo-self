#!/bin/bash
# test_media.sh — 图片+视频 获取/下载/识别 全链路测试
# 用法: bash test_media.sh [image|video|all]

set -uo pipefail
export LC_ALL=C

# ==================== 配置 ====================
API_URL="http://10.1.126.27:8087/v1/chat/completions"
MODEL="/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"

# 图片测试数据
TEST_PID="006mX07Rly8ifv3xs5535j30ud0plk1m"
IMG_URL_PATTERN="https://wx2.sinaimg.cn/mw690/{pid}.jpg"

# 视频测试数据
TEST_MEDIA_ID="2362904:4826598285967434"
TEST_CUSTOMER_ID="2608812381"
SHOWBATCH_API="http://i.iotep.tools.biz.weibo.com/api/v1/video/media/showBatch"

# 临时文件
TMP_DIR="/tmp/media_test_$$"
mkdir -p "${TMP_DIR}"

# 清理
trap 'rm -rf ${TMP_DIR}; echo ""; echo "[清理] 临时文件已清理"' EXIT

# ==================== 公共函数 ====================

# 调用Qwen3.6识别图片
call_qwen_image() {
    local img_path="$1"
    local prompt="$2"

    local img_b64
    img_b64=$(base64 -w 0 "${img_path}" 2>/dev/null)
    if [ -z "${img_b64}" ]; then
        echo "[错误] 图片转base64失败"
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

# ==================== 图片测试 ====================
test_image() {
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║          图片获取+下载+识别测试           ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""

    local pid="${TEST_PID}"
    local img_url="${IMG_URL_PATTERN/\{pid\}/${pid}}"
    local save_path="${TMP_DIR}/${pid}.jpg"

    # 步骤1: pid转URL
    echo ">> [1/4] pid转URL"
    echo "   pid:  ${pid}"
    echo "   URL:  ${img_url}"
    echo ""

    # 步骤2: 下载图片
    echo ">> [2/4] 下载图片..."
    local http_code file_size
    http_code=$(curl -s -o "${save_path}" -w "%{http_code}" --max-time 30 "${img_url}" 2>/dev/null)
    file_size=$(stat -c %s "${save_path}" 2>/dev/null || echo 0)

    if [ "${http_code}" != "200" ] || [ "${file_size}" -lt 100 ]; then
        echo "   ❌ 下载失败 HTTP=${http_code} size=${file_size}B"
        return 1
    fi
    echo "   ✅ 下载成功 HTTP=${http_code} 大小=$((file_size / 1024))KB"
    echo "   文件类型: $(file "${save_path}")"
    echo ""

    # 步骤3: 图片转base64 + 送Qwen3.6识别
    echo ">> [3/4] 送Qwen3.6识别图片内容..."
    local result
    result=$(call_qwen_image "${save_path}" \
        "请简短描述这张图片的内容，重点关注：1.是否包含汽车/品牌信息 2.是否有价格/优惠信息 3.内容类型(宣传/测评/促销等)。100字以内。")

    echo "   模型返回:"
    echo "   ┌──────────────────────────────────────────"
    echo "   │ ${result}"
    echo "   └──────────────────────────────────────────"
    echo ""

    # 步骤4: 模拟博文分类（图片+文字组合）
    echo ">> [4/4] 模拟图文博文分类..."
    local blog_text="比亚迪宋L实拍，外观真的绝了，灯组设计很有未来感"
    local img_b64
    img_b64=$(base64 -w 0 "${save_path}" 2>/dev/null)

    local classify_resp
    classify_resp=$(curl -s --max-time 60 -X POST "${API_URL}" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"${MODEL}\",
            \"messages\": [
                {\"role\": \"system\", \"content\": \"你是一个汽车行业博文营销分层分类器。将博文分类到以下3个层级之一：【认知层】品牌曝光传播；【兴趣层】引发讨论互动；【考虑层】辅助购买决策。直接输出：最终分类结果：【层级名称】\"},
                {\"role\": \"user\", \"content\": [
                    {\"type\": \"text\", \"text\": \"请对以下汽车行业图文博文进行分类。\\n博文文字：${blog_text}\\n请结合文字和图片综合判断，直接输出：最终分类结果：【层级名称】\"},
                    {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${img_b64}\"}}
                ]}
            ],
            \"max_tokens\": 512,
            \"temperature\": 0.0,
            \"chat_template_kwargs\": {\"enable_thinking\": false}
        }" 2>/dev/null)

    local classify_result
    classify_result=$(echo "${classify_resp}" | jq -r '.choices[0].message.content // "未返回内容"')
    local finish_reason
    finish_reason=$(echo "${classify_resp}" | jq -r '.choices[0].finish_reason // ""')

    echo "   博文文字: ${blog_text}"
    echo "   finish_reason: ${finish_reason}"
    echo "   分类结果:"
    echo "   ┌──────────────────────────────────────────"
    echo "   │ ${classify_result}"
    echo "   └──────────────────────────────────────────"
    echo ""

    echo "✅ 图片全链路测试完成"
}

# ==================== 视频测试 ====================
test_video() {
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║          视频获取+下载测试                ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""

    local media_id="${TEST_MEDIA_ID}"
    local customer_id="${TEST_CUSTOMER_ID}"
    local video_path="${TMP_DIR}/test_video.mp4"
    local cover_path="${TMP_DIR}/video_cover.jpg"

    # 步骤1: 调showBatch API
    echo ">> [1/5] 调用showBatch API获取视频信息..."
    echo "   media_id:    ${media_id}"
    echo "   customer_id: ${customer_id}"
    echo ""

    local api_resp
    api_resp=$(curl -s --location --request GET \
        "${SHOWBATCH_API}?customer_id=${customer_id}&fids=${media_id}" \
        --header 'User-Agent: BlogClassifier/1.0' \
        --header 'Accept: */*' \
        --header 'Connection: keep-alive' 2>/dev/null)

    local api_status
    api_status=$(echo "${api_resp}" | jq -r '.status // empty')

    if [ "${api_status}" != "200" ]; then
        echo "   ❌ API返回异常: ${api_resp}"
        return 1
    fi

    # 解析视频信息
    local video_url duration file_size cover_url video_type width height quality
    video_url=$(echo "${api_resp}" | jq -r '.data[0].url // empty')
    duration=$(echo "${api_resp}" | jq -r '.data[0].duration // empty')
    file_size=$(echo "${api_resp}" | jq -r '.data[0].fileSize // empty')
    cover_url=$(echo "${api_resp}" | jq -r '.data[0].frontUrl // empty')
    video_type=$(echo "${api_resp}" | jq -r '.data[0].type // empty')
    width=$(echo "${api_resp}" | jq -r '.data[0].width // empty')
    height=$(echo "${api_resp}" | jq -r '.data[0].height // empty')
    quality=$(echo "${api_resp}" | jq -r '.data[0].quality // empty')

    if [ -z "${video_url}" ]; then
        echo "   ❌ 未获取到视频URL"
        return 1
    fi

    echo "   ✅ 视频信息获取成功"
    echo "   ┌──────────────────────────────────────────"
    echo "   │ 时长:     ${duration}s"
    echo "   │ 分辨率:   ${width}x${height} (${quality})"
    echo "   │ 格式:     ${video_type}"
    echo "   │ 预估大小: $((file_size / 1024))KB"
    echo "   │ 封面图:   ${cover_url}"
    echo "   │ 视频URL:  ${video_url:0:80}..."
    echo "   └──────────────────────────────────────────"
    echo ""

    # 步骤2: 下载视频
    echo ">> [2/5] 下载完整视频..."
    local start_time end_time cost_time http_code
    start_time=$(date +%s)

    http_code=$(curl -s -o "${video_path}" -w "%{http_code}" \
        --max-time 120 "${video_url}" 2>/dev/null)

    end_time=$(date +%s)
    cost_time=$((end_time - start_time))

    if [ "${http_code}" != "200" ] && [ "${http_code}" != "206" ]; then
        echo "   ❌ 下载失败 HTTP=${http_code}"
        return 1
    fi

    local actual_size
    actual_size=$(stat -c %s "${video_path}" 2>/dev/null || echo 0)

    echo "   ✅ 下载成功"
    echo "   HTTP状态码:  ${http_code}"
    echo "   实际大小:    $((actual_size / 1024))KB (${actual_size} bytes)"
    echo "   耗时:        ${cost_time}s"
    echo "   文件类型:    $(file "${video_path}")"
    echo ""

    # 步骤3: 验证MP4文件头
    echo ">> [3/5] 验证视频文件完整性..."
    local file_header
    file_header=$(xxd -l 12 "${video_path}" 2>/dev/null | head -1)
    echo "   文件头: ${file_header}"

    if echo "${file_header}" | grep -qi "iso\|mp4\|ftyp"; then
        echo "   ✅ 合法MP4文件"
    else
        echo "   ⚠️ 文件头可能非标准MP4"
    fi
    echo ""

    # 步骤4: 下载封面图
    echo ">> [4/5] 下载视频封面图..."
    if [ -n "${cover_url}" ]; then
        local cover_http_code cover_size
        cover_http_code=$(curl -s -o "${cover_path}" -w "%{http_code}" --max-time 30 "${cover_url}" 2>/dev/null)
        cover_size=$(stat -c %s "${cover_path}" 2>/dev/null || echo 0)

        if [ "${cover_http_code}" = "200" ] && [ "${cover_size}" -gt 100 ]; then
            echo "   ✅ 封面图下载成功 大小=$((cover_size / 1024))KB"
            echo "   文件类型: $(file "${cover_path}")"
        else
            echo "   ⚠️ 封面图下载失败 HTTP=${cover_http_code} size=${cover_size}B"
            cover_path=""
        fi
    else
        echo "   ⚠️ 无封面图URL"
        cover_path=""
    fi
    echo ""

    # 步骤5: 封面图送Qwen3.6识别
    echo ">> [5/5] 封面图送Qwen3.6识别..."
    if [ -n "${cover_path}" ] && [ -f "${cover_path}" ]; then
        local cover_result
        cover_result=$(call_qwen_image "${cover_path}" \
            "这是汽车行业视频博文的封面图。请简短描述内容，重点关注：1.是否包含汽车/品牌信息 2.是否有价格/优惠信息 3.视频可能的内容类型(宣传/测评/促销等)。100字以内。")

        echo "   模型返回:"
        echo "   ┌──────────────────────────────────────────"
        echo "   │ ${cover_result}"
        echo "   └──────────────────────────────────────────"
    else
        echo "   ⏭️ 无封面图，跳过识别"
    fi
    echo ""

    # 抽帧能力检测
    echo ">> [附加] 检测视频抽帧能力..."
    if command -v ffmpeg &> /dev/null; then
        echo "   ✅ ffmpeg命令行可用: $(ffmpeg -version 2>/dev/null | head -1)"
    else
        echo "   ❌ ffmpeg命令行不可用"
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

    echo "✅ 视频全链路测试完成（下载+封面识别已通过，抽帧待安装工具）"
}

# ==================== 主流程 ====================
MODE="${1:-all}"

echo "============================================"
echo "  媒体获取+下载+识别 全链路测试"
echo "  模式: ${MODE}"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"

case "${MODE}" in
    image)
        test_image
        ;;
    video)
        test_video
        ;;
    all)
        test_image
        echo ""
        echo "============================================"
        test_video
        ;;
    *)
        echo "用法: bash test_media.sh [image|video|all]"
        exit 1
        ;;
esac

echo ""
echo "============================================"
echo "  全部测试完成！"
echo "============================================"