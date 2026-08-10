#!/bin/bash
set -uo pipefail
export LC_ALL=C

########################### 配置区域 ###########################
API_URL="http://10.1.126.27:8087/v1/chat/completions"
MODEL="/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"

# 针对 Qwen3.6 thinking 模式的提示词
# 要求模型在 thinking 后明确输出标签
SYS_PROMPT="你是一个博文内容分类器。请将汽车行业博文分类到以下6个类别之一。

可选标签：
【品牌官方博文】— 品牌官方口吻，直接宣传产品/价格/活动
【KOL产品测评/种草】— 第三方博主，'体验/测评/推荐/试驾/带大家看'，引用媒体评价
【用户UGC晒单/使用心得】— 普通用户真实用车感受，'提车/用车/油耗/保养'
【品类话题/超话内容】— 行业趋势/多车对比/通用话题，不聚焦单一品牌
【泛娱乐中植入品牌】— 娱乐/生活内容中顺带出现品牌，品牌非主角
【无关内容】— 与汽车完全无关

要求：
1. 先进行分析推理
2. 分析完成后，单独一行输出最终结论，格式必须是：最终分类结果：【标签名称】"

USER_TPL="请对以下汽车行业博文进行分类。

博文内容：
%s

请分析后，最后一行输出：最终分类结果：【标签名称】"

MAX_RETRY=3
SLEEP_SEC=0.5
###############################################################

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

# 从模型输出中提取标签（兼容 Qwen3.6 thinking 模式）
extract_label() {
    local full_output="$1"
    local label="未识别"

    # 策略1：查找 "最终分类结果：【xxx】" 格式
    local final_result
    final_result=$(echo "${full_output}" | grep -oP '最终分类结果：【\K[^】]+' | tail -n 1)
    if [ -n "${final_result}" ]; then
        case "${final_result}" in
            "品牌官方博文"|"KOL产品测评/种草"|"用户UGC晒单/使用心得"|\
            "品类话题/超话内容"|"泛娱乐中植入品牌"|"无关内容")
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
            "品牌官方博文"|"KOL产品测评/种草"|"用户UGC晒单/使用心得"|\
            "品类话题/超话内容"|"泛娱乐中植入品牌"|"无关内容")
                echo "${all_brackets}"
                return 0
                ;;
        esac
    fi

    # 策略3：取全文最后一行，尝试匹配
    local last_line
    last_line=$(echo "${full_output}" | sed '/^$/d' | tail -n 1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

    case "${last_line}" in
        "【品牌官方博文】"|"品牌官方博文") label="品牌官方博文" ;;
        "【KOL产品测评/种草】"|"KOL产品测评/种草") label="KOL产品测评/种草" ;;
        "【用户UGC晒单/使用心得】"|"用户UGC晒单/使用心得") label="用户UGC晒单/使用心得" ;;
        "【品类话题/超话内容】"|"品类话题/超话内容") label="品类话题/超话内容" ;;
        "【泛娱乐中植入品牌】"|"泛娱乐中植入品牌") label="泛娱乐中植入品牌" ;;
        "【无关内容】"|"无关内容") label="无关内容" ;;
        *)
            # 兜底：最后一行中包含标签名
            if echo "${last_line}" | grep -q "品牌官方博文"; then label="品牌官方博文"
            elif echo "${last_line}" | grep -q "KOL产品测评/种草"; then label="KOL产品测评/种草"
            elif echo "${last_line}" | grep -q "用户UGC晒单/使用心得"; then label="用户UGC晒单/使用心得"
            elif echo "${last_line}" | grep -q "品类话题/超话内容"; then label="品类话题/超话内容"
            elif echo "${last_line}" | grep -q "泛娱乐中植入品牌"; then label="泛娱乐中植入品牌"
            elif echo "${last_line}" | grep -q "无关内容"; then label="无关内容"
            fi
            ;;
    esac

    echo "${label}"
}

# ==================== 主流程 ====================
echo "=========================================="
echo "  博文单条分类工具 v5 (Qwen3.6兼容版)"
echo "=========================================="
echo ""

read -p "请输入博文mid: " mid
[ -z "${mid}" ] && echo "[错误] mid不能为空" && exit 1

echo "粘贴博文正文，粘贴完成后，单独一行输入 EOF 结束输入"
content=""
while IFS= read -r line; do
    [ "${line}" = "EOF" ] && break
    if [ -z "${content}" ]; then
        content="${line}"
    else
        content="${content}\\n${line}"
    fi
done

[ -z "${content}" ] && echo "[错误] 博文内容不能为空" && exit 1

echo ""
echo "[处理中] mid:${mid}，正在调用模型..."

# 安全转义 + 构建请求
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
        max_tokens: 2048
    }')

resp=$(call_api "${req_body}")
[ $? -ne 0 ] && echo "[错误] 模型调用失败" && exit 1

full_output=$(echo "${resp}" | jq -r '.choices[0].message.content // ""')
label=$(extract_label "${full_output}")

echo ""
echo "======================"
echo "博文MID: ${mid}"
echo "----------------------"
# 显示完整输出（不截断），方便排查
output_lines=$(echo "${full_output}" | wc -l)
echo "完整模型返回内容（共 ${output_lines} 行）："
echo "${full_output}"
echo "----------------------"
echo "提取分类标签：【${label}】"
echo "======================"