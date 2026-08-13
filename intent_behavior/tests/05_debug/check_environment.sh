#!/bin/bash
# =============================================================
# check_environment.sh — 环境检查与问题排查脚本
# =============================================================
# 功能：
#   快速检查图片/视频分类链路所需的环境条件，帮助定位
#   「模型返回为空」「Permission denied」「图片过大」等问题。
#
# 检查项：
#   1. 当前用户与目录写权限
#   2. vLLM API 连通性（HTTP 200 + choices 结构）
#   3. 图片压缩工具（ImageMagick convert / Python PIL）
#   4. HDFS 读取权限
#   5. curl / jq / bc 等基础命令
#   6. 单条图片分类端到端测试（可选）
#
# 用法：
#   bash tests/05_debug/check_environment.sh
#   bash tests/05_debug/check_environment.sh --test-image   # 同时跑一条图片分类
#
# 作者：xuanyu11
# 创建时间：2026-08-13
# =============================================================

set -uo pipefail
export LC_ALL=C

API_URL="http://10.1.126.27:8087/v1/chat/completions"
MODEL="/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"
TEST_PID="006mX07Rly8ifv3xs5535j30ud0plk1m"
TEST_HDFS_DIR="/dw_ext/ad/person/xuanyu11/intent_behavior/data/image_weibo_ad_20260701_20260701"

RUN_IMAGE_TEST=0
if [ "${1:-}" = "--test-image" ]; then
    RUN_IMAGE_TEST=1
fi

echo "============================================"
echo "  环境检查与问题排查"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  当前用户: $(whoami)"
echo "============================================"
echo ""

PASS=0
FAIL=0

report() {
    local status="$1"
    local msg="$2"
    if [ "${status}" = "PASS" ]; then
        echo "  ✅ ${msg}"
        PASS=$((PASS + 1))
    else
        echo "  ❌ ${msg}"
        FAIL=$((FAIL + 1))
    fi
}

# ── 1. 基础命令检查 ────────────────────────────────────────
echo ">> 基础命令检查"
for cmd in curl jq bc hdfs hive python3; do
    if command -v "${cmd}" &>/dev/null; then
        report "PASS" "${cmd} 已安装"
    else
        report "FAIL" "${cmd} 未安装"
    fi
done
echo ""

# ── 2. 输出目录写权限检查 ──────────────────────────────────
echo ">> 输出目录写权限检查"
for dir in "${PWD}/intent_behavior/output" "/tmp/image_test_output" "/tmp/video_test_output"; do
    mkdir -p "${dir}" 2>/dev/null
    if [ -w "${dir}" ]; then
        report "PASS" "目录可写: ${dir}"
    else
        report "FAIL" "目录不可写: ${dir}"
    fi
done
echo ""

# ── 3. 图片压缩工具检查 ────────────────────────────────────
echo ">> 图片压缩工具检查"
if command -v convert &>/dev/null; then
    report "PASS" "ImageMagick convert 可用: $(convert --version 2>/dev/null | head -1)"
else
    report "FAIL" "ImageMagick convert 未安装（大图片可能无法压缩）"
fi

if python3 -c "from PIL import Image" 2>/dev/null; then
    report "PASS" "Python PIL 可用: $(python3 -c 'import PIL; print(PIL.__version__)')"
else
    report "FAIL" "Python PIL 未安装（大图片可能无法压缩）"
fi
echo ""

# ── 4. HDFS 读取权限检查 ───────────────────────────────────
echo ">> HDFS 读取权限检查"
if hdfs dfs -test -e "${TEST_HDFS_DIR}" 2>/dev/null; then
    report "PASS" "HDFS 目录可访问: ${TEST_HDFS_DIR}"
    echo "   目录内容预览:"
    hdfs dfs -ls "${TEST_HDFS_DIR}" 2>/dev/null | head -5
else
    report "FAIL" "HDFS 目录不可访问: ${TEST_HDFS_DIR}"
fi
echo ""

# ── 5. vLLM API 连通性检查 ─────────────────────────────────
echo ">> vLLM API 连通性检查"
echo "   URL: ${API_URL}"

PING_RESP=$(curl -s --max-time 10 -X POST "${API_URL}" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"max_tokens\":5}" \
    -w "\nHTTP_CODE:%{http_code}" 2>/dev/null)

HTTP_CODE=$(echo "${PING_RESP}" | grep -oE 'HTTP_CODE:[0-9]+' | cut -d: -f2)
RESP_BODY=$(echo "${PING_RESP}" | sed '/HTTP_CODE:/d')

if [ "${HTTP_CODE}" = "200" ]; then
    CHOICES=$(echo "${RESP_BODY}" | jq '.choices | length' 2>/dev/null || echo 0)
    if [ "${CHOICES}" -gt 0 ]; then
        report "PASS" "API 连通正常 (HTTP 200, choices=${CHOICES})"
    else
        report "FAIL" "API 返回 200 但无 choices 结构: ${RESP_BODY:0:200}"
    fi
else
    report "FAIL" "API 连通失败 (HTTP ${HTTP_CODE}): ${RESP_BODY:0:200}"
fi
echo ""

# ── 6. 单条图片分类端到端测试 ──────────────────────────────
if [ "${RUN_IMAGE_TEST}" -eq 1 ]; then
    echo ">> 单条图片分类端到端测试"
    TMP_IMG_DIR="/tmp/env_test_$$"
    mkdir -p "${TMP_IMG_DIR}"

    IMG_URL="https://wx2.sinaimg.cn/mw690/${TEST_PID}.jpg"
    IMG_PATH="${TMP_IMG_DIR}/test.jpg"

    HTTP_CODE=$(curl -s -o "${IMG_PATH}" -w "%{http_code}" --max-time 30 "${IMG_URL}" 2>/dev/null)
    if [ "${HTTP_CODE}" = "200" ]; then
        report "PASS" "图片下载成功 (${TEST_PID})"

        IMG_B64=$(base64 -w 0 "${IMG_PATH}" 2>/dev/null)
        TEST_RESP=$(curl -s --max-time 120 -X POST "${API_URL}" \
            -H "Content-Type: application/json" \
            -d "{
                \"model\": \"${MODEL}\",
                \"messages\": [
                    {\"role\": \"system\", \"content\": \"你是一个汽车行业博文营销分层分类器。将博文分类到以下3个层级之一：【认知层】品牌曝光传播；【兴趣层】引发讨论互动；【考虑层】辅助购买决策。直接输出：最终分类结果：【层级名称】\"},
                    {\"role\": \"user\", \"content\": [
                        {\"type\": \"text\", \"text\": \"请对以下汽车行业图文博文进行分类。\\n博文文字：比亚迪宋L实拍来了！外观绝了\\n请结合文字和图片综合判断，直接输出：最终分类结果：【层级名称】\"},
                        {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,${IMG_B64}\"}}
                    ]}
                ],
                \"max_tokens\": 512,
                \"temperature\": 0.0,
                \"chat_template_kwargs\": {\"enable_thinking\": false}
            }" 2>/dev/null)

        CLASSIFY_RESULT=$(echo "${TEST_RESP}" | jq -r '.choices[0].message.content // ""')
        if [ -n "${CLASSIFY_RESULT}" ]; then
            report "PASS" "图片分类成功: ${CLASSIFY_RESULT}"
        else
            report "FAIL" "图片分类失败，API返回: ${TEST_RESP:0:300}"
        fi
    else
        report "FAIL" "图片下载失败 HTTP=${HTTP_CODE}"
    fi

    rm -rf "${TMP_IMG_DIR}"
    echo ""
fi

# ── 总结 ───────────────────────────────────────────────────
echo "============================================"
echo "  环境检查完成"
echo "  ✅ 通过: ${PASS}"
echo "  ❌ 失败: ${FAIL}"
echo "============================================"

if [ "${FAIL}" -gt 0 ]; then
    exit 1
fi
