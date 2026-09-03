# Qwen3.6-35B-A3B 模型使用说明

> 本文档整理了当前项目中 Qwen3.6-35B-A3B 模型的完整使用方式，方便在其他项目中复用。

---

## 一、模型基本信息

| 项目 | 值 |
|------|-----|
| 模型名称 | Qwen3.6-35B-A3B |
| 模型路径 | `/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B` |
| 模型类型 | MoE（混合专家），支持文本 + 多模态（图片） |
| 推理框架 | vLLM |
| API 地址 | `http://10.1.126.27:8087/v1/chat/completions` |
| API 协议 | OpenAI 兼容格式（`/v1/chat/completions`） |

---

## 二、关键参数配置

```yaml
api:
  url: "http://10.1.126.27:8087/v1/chat/completions"
  model: "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"
  max_tokens: 512              # 分类任务只需 128~512
  temperature: 0.0             # 保证确定性/一致性（必须为 0）
  top_p: 1.0
  top_k: 0
  seed: 42                     # 固定种子，提升可复现性
  thinking:
    type: "disabled"           # 关闭思考模式（必须）
  reasoning:
    effort: "none"             # 关闭推理（必须）
  enable_thinking: false       # 双重关闭 thinking（必须）
  timeout: 60                  # 单次请求超时（秒）
  max_retry: 3                 # 最大重试次数
  retry_backoff_base: 2        # 重试间隔底数（指数退避）
```

### ⚠️ 必须注意的参数

| 参数 | 值 | 原因 |
|------|-----|------|
| `temperature` | `0.0` | 保证同一条输入多次调用结果一致 |
| `seed` | `42`（或任意固定值） | 配合 temperature=0 保证可复现性 |
| `thinking.type` | `"disabled"` | 关闭思考模式，避免输出被 max_tokens 截断 |
| `reasoning.effort` | `"none"` | 关闭推理，直接输出结果 |
| `enable_thinking` | `false` | 通过 `chat_template_kwargs` 双重关闭 |

**如果不关闭 thinking 模式**，模型会先输出一段思考过程，可能超出 `max_tokens` 限制导致输出被截断，无法提取有效标签。

---

## 三、纯文本调用示例

### 3.1 Python 示例

```python
import requests

url = "http://10.1.126.27:8087/v1/chat/completions"

payload = {
    "model": "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B",
    "messages": [
        {"role": "system", "content": "你是一个文本分类器。"},
        {"role": "user", "content": "请对以下内容进行分类：今天天气真好"}
    ],
    "temperature": 0.0,
    "max_tokens": 512,
    "top_p": 1.0,
    "top_k": 0,
    "seed": 42,
    "thinking": {"type": "disabled"},
    "reasoning": {"effort": "none"},
    "chat_template_kwargs": {"enable_thinking": False}
}

resp = requests.post(url, json=payload, timeout=60, headers={"Content-Type": "application/json"})
resp.raise_for_status()
data = resp.json()

# 提取模型输出
output = data["choices"][0]["message"]["content"]
print(output)
```

### 3.2 curl 示例

```bash
curl -X POST "http://10.1.126.27:8087/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B",
    "messages": [
      {"role": "system", "content": "你是一个文本分类器。"},
      {"role": "user", "content": "请对以下内容进行分类：今天天气真好"}
    ],
    "temperature": 0.0,
    "max_tokens": 512,
    "top_p": 1.0,
    "top_k": 0,
    "seed": 42,
    "thinking": {"type": "disabled"},
    "reasoning": {"effort": "none"},
    "chat_template_kwargs": {"enable_thinking": false}
  }'
```

---

## 四、多模态（图文）调用示例

### 4.1 Python 示例

```python
import requests
import base64

url = "http://10.1.126.27:8087/v1/chat/completions"

# 读取图片并转 base64
with open("image.jpg", "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode("utf-8")

# 构建 content 数组：先文字，后图片
content = [
    {"type": "text", "text": "请对以下图文内容进行分类：这是一篇汽车评测"},
    {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
    }
]

payload = {
    "model": "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B",
    "messages": [
        {"role": "system", "content": "你是一个图文分类器。"},
        {"role": "user", "content": content}
    ],
    "temperature": 0.0,
    "max_tokens": 512,
    "top_p": 1.0,
    "top_k": 0,
    "seed": 42,
    "thinking": {"type": "disabled"},
    "reasoning": {"effort": "none"},
    "chat_template_kwargs": {"enable_thinking": False}
}

resp = requests.post(url, json=payload, timeout=60, headers={"Content-Type": "application/json"})
resp.raise_for_status()
data = resp.json()

output = data["choices"][0]["message"]["content"]
print(output)
```

### 4.2 多张图片

```python
content = [
    {"type": "text", "text": "请对以下图文内容进行分类"},
]

for img_path in ["img1.jpg", "img2.jpg", "img3.jpg"]:
    with open(img_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")
    content.append({
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
    })
```

> **建议**：单次请求最多 3 张图片，过多会增加延迟和 token 消耗。

---

## 五、API 响应格式

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "最终分类结果：【认知层】"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 150,
    "completion_tokens": 10,
    "total_tokens": 160
  }
}
```

### 提取输出

```python
output = data["choices"][0]["message"]["content"]
finish_reason = data["choices"][0]["finish_reason"]

# finish_reason 说明：
# "stop"   → 正常结束
# "length" → 被 max_tokens 截断（需要增大 max_tokens 或检查 thinking 是否关闭）
```

---

## 六、重试机制

当前项目使用指数退避重试：

```python
import time
import requests

def call_model(payload, max_retry=3, retry_backoff_base=2, timeout=60):
    url = "http://10.1.126.27:8087/v1/chat/completions"
    
    for attempt in range(1, max_retry + 1):
        try:
            resp = requests.post(
                url, json=payload, timeout=timeout,
                headers={"Content-Type": "application/json"}
            )
            resp.raise_for_status()
            data = resp.json()
            
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"]
            
        except requests.exceptions.Timeout:
            print(f"请求超时，第 {attempt}/{max_retry} 次重试")
        except requests.exceptions.RequestException as e:
            print(f"请求异常: {e}，第 {attempt}/{max_retry} 次重试")
        
        if attempt < max_retry:
            time.sleep(retry_backoff_base ** attempt)  # 2s, 4s, 8s...
    
    return None  # 全部重试失败
```

---

## 七、常见问题

### Q1: 输出被截断（finish_reason=length）

**原因**：thinking 模式未关闭，模型先输出思考过程，超出 max_tokens。

**解决**：确保以下三个参数都设置正确：
```json
{
  "thinking": {"type": "disabled"},
  "reasoning": {"effort": "none"},
  "chat_template_kwargs": {"enable_thinking": false}
}
```

### Q2: 同一条输入多次调用结果不一致

**原因**：temperature 不为 0 或 seed 未固定。

**解决**：
```json
{
  "temperature": 0.0,
  "seed": 42
}
```

### Q3: 图片输入报错

**原因**：图片格式不支持或 base64 编码错误。

**解决**：
- 确保图片是 JPEG/PNG 格式
- base64 编码后加上 `data:image/jpeg;base64,` 前缀
- 单张图片建议不超过 1MB

### Q4: 请求超时

**原因**：模型推理时间超过 timeout 设置。

**解决**：
- 增大 `timeout`（建议 60~120 秒）
- 减少 `max_tokens`
- 减少图片数量

---

## 八、最小可运行示例

```python
#!/usr/bin/env python3
"""Qwen3.6-35B-A3B 最小调用示例"""

import requests

URL = "http://10.1.126.27:8087/v1/chat/completions"
MODEL = "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"

def classify(text: str) -> str:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你是一个文本分类器。"},
            {"role": "user", "content": text}
        ],
        "temperature": 0.0,
        "max_tokens": 512,
        "top_p": 1.0,
        "top_k": 0,
        "seed": 42,
        "thinking": {"type": "disabled"},
        "reasoning": {"effort": "none"},
        "chat_template_kwargs": {"enable_thinking": False}
    }
    
    resp = requests.post(URL, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]

if __name__ == "__main__":
    result = classify("今天带大家体验东风日产N6的零压云毯大沙发")
    print(f"分类结果: {result}")
```

---

## 九、依赖

```bash
pip install requests
```

无需安装 vLLM 或其他推理框架，模型已通过 vLLM 服务部署，直接通过 HTTP API 调用。
