#!/bin/bash
# =============================================================
# image.sh — 图片获取 + 下载 + 多模态识别 全链路测试
# =============================================================
# 功能：
#   1. 通过 pid 构造图片 URL
#   2. curl 下载图片到本地临时目录
#   3. 图片转 base64 → 送 Qwen3.6 多模态识别图片内容
#   4. 模拟图文博文分类（图片 + 文字 → 认知层/兴趣层/考虑层）
#
# 数据来源：
#   - 图片 pid：已验证可下载的真实 pid（HTTP 200, 120KB JPEG）
#   - 模型服务：vLLM Qwen3.6-35B-A3B
#
# 输出路径：
#   - 临时图片：/tmp/media_test_<PID>/
#   - 日志：实时打印到终端
#
# 用法：
#   bash sql/image.sh
#   bash sql/image.sh [pid]   # 指定自定义 pid
#
# 运行时间预估：约 10~30 秒（含图片下载 + vLLM 推理）
#
# 作者：xuanyu11
# 创建时间：2026-08-12
# =============================================================

set -uo pipefail
export LC_ALL=C

# ==================== 配置 ====================
API_URL="http://10.1.126.27:8087/v1/chat/completions"
MODEL="/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"

# 图片测试数据（已验证：HTTP 200, 120KB, JPEG 690x581）
TEST_PID="${1:-006mX07Rly8ifv3xs5535j30ud0plk1m}"
IMG_URL_PATTERN="https://wx2.sinaimg.cn/mw690/{pid}.jpg"

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
echo "  图片获取 + 下载 + 识别 全链路测试"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  模型: ${MODEL}"
echo "  API:  ${API_URL}"
echo "============================================"
echo ""

START_TS=$(date +%s)

# ── 步骤 1: pid 转 URL ────────────────────────────────────────
echo ">> [1/4] pid 转 URL"
PID="${TEST_PID}"
IMG_URL="${IMG_URL_PATTERN/\{pid\}/${PID}}"
SAVE_PATH="${TMP_DIR}/${PID}.jpg"

echo "   pid:  ${PID}"
echo "   URL:  ${IMG_URL}"
echo ""

# ── 步骤 2: 下载图片 ──────────────────────────────────────────
echo ">> [2/4] 下载图片..."
HTTP_CODE=$(curl -s -o "${SAVE_PATH}" -w "%{http_code}" --max-time 30 "${IMG_URL}" 2>/dev/null)
FILE_SIZE=$(stat -c %s "${SAVE_PATH}" 2>/dev/null || echo 0)

if [ "${HTTP_CODE}" != "200" ] || [ "${FILE_SIZE}" -lt 100 ]; then
    echo "   ❌ 下载失败 HTTP=${HTTP_CODE} size=${FILE_SIZE}B"
    exit 1
fi
echo "   ✅ 下载成功"
echo "   HTTP 状态码: ${HTTP_CODE}"
echo "   文件大小:    $((FILE_SIZE / 1024))KB (${FILE_SIZE} bytes)"
echo "   文件类型:    $(file "${SAVE_PATH}")"
echo ""

# ── 步骤 3: 图片内容识别 ──────────────────────────────────────
echo ">> [3/4] 送 Qwen3.6 识别图片内容..."
RECOGNIZE_RESULT=$(call_qwen_image "${SAVE_PATH}" \
    "请简短描述这张图片的内容，重点关注：1.是否包含汽车/品牌信息 2.是否有价格/优惠信息 3.内容类型(宣传/测评/促销等)。100字以内。")

echo "   模型返回:"
echo "   ┌──────────────────────────────────────────"
echo "   │ ${RECOGNIZE_RESULT}"
echo "   └──────────────────────────────────────────"
echo ""

# ── 步骤 4: 模拟图文博文分类 ──────────────────────────────────
echo ">> [4/4] 模拟图文博文分类（图片 + 文字 → 营销层级）..."
BLOG_TEXT="比亚迪宋L实拍，外观真的绝了，灯组设计很有未来感"
IMG_B64=$(base64 -w 0 "${SAVE_PATH}" 2>/dev/null)

CLASSIFY_RESP=$(curl -s --max-time 60 -X POST "${API_URL}" \
    -H "Content-Type: application/json" \
    -d "{
        \"model\": \"${MODEL}\",
        \"messages\": [
            {\"role\": \"system\", \"content\": \"你是一个汽车行业博文营销分层分类器。将博文分类到以下3个层级之一：【认知层】品牌曝光传播；【兴趣层】引发讨论互动；【考虑层】辅助购买决策。直接输出：最终分类结果：【层级名称】\"},
            {\"role\": \"user\", \"content\": [
                {\"type\": \"text\", \"text\": \"请对以下汽车行业图文博文进行分类。\\n博文文字：${BLOG_TEXT}\\n请结合文字和图片综合判断，直接输出：最终分类结果：【层级名称】\"},
                {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${IMG_B64}\"}}
            ]}
        ],
        \"max_tokens\": 512,
        \"temperature\": 0.0,
        \"chat_template_kwargs\": {\"enable_thinking\": false}
    }" 2>/dev/null)

CLASSIFY_RESULT=$(echo "${CLASSIFY_RESP}" | jq -r '.choices[0].message.content // "未返回内容"')
FINISH_REASON=$(echo "${CLASSIFY_RESP}" | jq -r '.choices[0].finish_reason // ""')

echo "   博文文字:    ${BLOG_TEXT}"
echo "   finish_reason: ${FINISH_REASON}"
echo "   分类结果:"
echo "   ┌──────────────────────────────────────────"
echo "   │ ${CLASSIFY_RESULT}"
echo "   └──────────────────────────────────────────"
echo ""

END_TS=$(date +%s)
ELAPSED=$((END_TS - START_TS))

echo "============================================"
echo "  ✅ 图片全链路测试完成"
echo "  总耗时: ${ELAPSED}s"
echo "  时间:   $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"
