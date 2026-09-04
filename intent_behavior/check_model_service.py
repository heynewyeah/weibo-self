#!/usr/bin/env python3
"""
模型服务连通性检查脚本
======================
用于手动确认 vLLM 推理服务（Qwen3.6-35B-A3B）是否存活、能否正常返回。

与 src/api_client.py 使用完全相同的请求体结构（thinking 关闭、seed、temperature 等），
默认只发一次纯文本请求并统计耗时，可用来区分：
  - 服务不可达（连接拒绝 / DNS 失败）
  - 服务无响应（请求超时，可能卡死或过载）
  - 服务异常（HTTP 4xx/5xx）
  - 服务正常（正常返回模型输出）

运行方式：
  # 默认：一次纯文本 ping（超时 30s）
  python3 check_model_service.py

  # 指定超时与重试次数（0=只试一次）
  python3 check_model_service.py --timeout 15 --retries 0

  # 附带一张本地图片，走与生产一致的多模态通道
  python3 check_model_service.py --image /path/to/test.jpg

  # 完全按 config 里的超时/重试（模拟一次生产请求）
  python3 check_model_service.py --full

退出码：0 = 服务正常；1 = 服务异常/不可达/无响应。
"""

import argparse
import base64
import os
import sys
import time

import requests
import yaml


def load_api_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("api", {})


def build_payload(api_cfg: dict, system_prompt: str, user_content) -> dict:
    """与 src/api_client.py 的 _build_payload 保持一致。"""
    payload = {
        "model": api_cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": api_cfg.get("temperature", 0.0),
        "max_tokens": api_cfg.get("max_tokens", 128),
        "top_p": api_cfg.get("top_p", 1.0),
        "top_k": api_cfg.get("top_k", 0),
        "thinking": api_cfg.get("thinking", {"type": "disabled"}),
        "reasoning": api_cfg.get("reasoning", {"effort": "none"}),
    }
    if api_cfg.get("seed") is not None:
        payload["seed"] = api_cfg["seed"]
    if api_cfg.get("enable_thinking") is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": api_cfg["enable_thinking"]}
    return payload


def build_text_content() -> tuple:
    system_prompt = "你是模型服务连通性检测器。"
    user_prompt = "模型服务连通性测试，请只回复两个字：正常"
    return system_prompt, user_prompt


def build_image_content(image_path: str, user_prompt: str) -> list:
    """图片走与 classify_with_images 相同的 content 数组结构。"""
    content = [{"type": "text", "text": user_prompt}]
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")
    content.append({
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
    })
    return content


def main():
    parser = argparse.ArgumentParser(
        description="模型服务连通性检查（vLLM / Qwen3.6-35B-A3B）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--timeout", type=int, default=0,
                        help="单次请求超时秒数（0=使用 config 中的 api.timeout）")
    parser.add_argument("--retries", type=int, default=-1,
                        help="重试次数（-1=使用 config 中的 api.max_retry；0=不重试）")
    parser.add_argument("--image", default="", help="附带一张本地图片，走多模态通道测试")
    parser.add_argument("--full", action="store_true",
                        help="完全按 config 的超时/重试/输出长度模拟一次生产请求")
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="输出长度上限（0=使用 config 中的 max_tokens；纯文本 ping 建议设小值）")
    args = parser.parse_args()

    project_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(project_dir, args.config) if not os.path.isabs(args.config) else args.config
    api_cfg = load_api_config(config_path)

    timeout = api_cfg.get("timeout", 60)
    retries = int(api_cfg.get("max_retry", 3))
    max_tokens = int(api_cfg.get("max_tokens", 128))
    backoff = float(api_cfg.get("retry_backoff_base", 2))

    if args.full:
        # 保持 config 原值（模拟一次生产请求）
        pass
    else:
        # 快速 ping：默认短超时、小输出、只试一次
        timeout = args.timeout if args.timeout > 0 else 30
        retries = args.retries if args.retries >= 0 else 1
        max_tokens = args.max_tokens if args.max_tokens > 0 else 16

    url = api_cfg["url"]
    model = api_cfg["model"]

    print("=" * 70)
    print("模型服务连通性检查")
    print("=" * 70)
    print(f"接口:   {url}")
    print(f"模型:   {model}")
    print(f"超时:   {timeout}s   重试次数: {retries}   输出上限: {max_tokens} tokens")
    mode = "多模态（含图片）" if args.image else "纯文本"
    print(f"通道:   {mode}")
    print("=" * 70)

    system_prompt, user_prompt = build_text_content()
    user_content = user_prompt
    if args.image:
        if not os.path.exists(args.image):
            print(f"❌ 图片不存在: {args.image}")
            sys.exit(1)
        user_content = build_image_content(
            args.image, "图片通道连通性测试，请用一句话描述这张图片。"
        )

    payload = build_payload(api_cfg, system_prompt, user_content)
    payload["max_tokens"] = max_tokens

    attempts = max(1, retries)
    last_error = ""
    for attempt in range(1, attempts + 1):
        print(f"\n[{attempt}/{attempts}] 发送请求 ..." if attempts > 1 else "\n发送请求 ...")
        start = time.time()
        try:
            resp = requests.post(
                url,
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
            elapsed = time.time() - start
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                snippet = resp.text[:300].replace("\n", " ")
                print(f"❌ 服务返回异常状态: HTTP {resp.status_code}（耗时 {elapsed:.1f}s）")
                if snippet:
                    print(f"   响应内容: {snippet}")
            else:
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    last_error = "响应结构异常（无 choices）"
                    print(f"❌ 响应结构异常: 缺少 choices 字段（耗时 {elapsed:.1f}s）")
                    print(f"   响应内容: {str(data)[:300]}")
                else:
                    msg = choices[0].get("message", {})
                    content = msg.get("content", "")
                    finish = choices[0].get("finish_reason", "")
                    usage = data.get("usage", {})
                    print(f"✅ 服务正常   耗时 {elapsed:.1f}s   finish_reason={finish}")
                    print(f"   模型返回: {content[:200]}")
                    if usage:
                        print(f"   token 用量: prompt={usage.get('prompt_tokens', '-')} "
                              f"completion={usage.get('completion_tokens', '-')}")
                    print("=" * 70)
                    sys.exit(0)

        except requests.exceptions.ConnectionError as e:
            last_error = f"连接失败: {e}"
            print(f"❌ 无法连接服务（{time.time() - start:.1f}s）：{e}")
        except requests.exceptions.Timeout:
            last_error = f"请求超时（>{timeout}s）"
            print(f"❌ 请求超时，超过 {timeout}s 服务无响应")
        except requests.exceptions.RequestException as e:
            last_error = f"请求异常: {e}"
            print(f"❌ 请求异常: {e}")
        except Exception as e:
            last_error = f"未知异常: {e}"
            print(f"❌ 未知异常: {e}")

        if attempt < attempts:
            sleep_s = backoff ** attempt
            print(f"   等待 {sleep_s:.0f}s 后重试 ...")
            time.sleep(sleep_s)

    print("\n" + "=" * 70)
    print(f"❌ 检查未通过：{last_error}")
    print("=" * 70)
    sys.exit(1)


if __name__ == "__main__":
    main()
