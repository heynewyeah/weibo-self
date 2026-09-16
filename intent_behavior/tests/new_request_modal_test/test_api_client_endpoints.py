#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型接口参数与连通性单元测试
============================
覆盖两件事：
  1. 离线单测（默认执行）：校验 src/api_client.py 的请求体构造是否符合当前网关要求
     - KServe 网关（当前启用）：thinking 关思考；reasoning / chat_template_kwargs 不下发
     - 旧直连 vLLM（备份方案）：reasoning 字典 + chat_template_kwargs 正常下发
     - 配置为 null 的参数必须完全不出现在请求体里
     - extra_params 透传、值为 None 时跳过
  2. 真实接口联调（--live）：用配置里的地址实际发请求，断言
     - 请求能返回 200 且含 choices
     - 生产请求体下思考已关闭（无 reasoning_content / reasoning_tokens）
     - 多模态 image_url 通道可用
     - （可选 --check-legacy）配置里注释掉的旧直连地址仍然可用

运行方式：
  python3 -m unittest tests.new_request_modal_test.test_api_client_endpoints -v      # 只跑离线单测
  python3 tests/new_request_modal_test/test_api_client_endpoints.py --live            # 离线单测 + 真实接口联调
  python3 tests/new_request_modal_test/test_api_client_endpoints.py --live --check-legacy

作者：xuanyu11
"""

import os
import sys
import json
import base64
import struct
import zlib
import time
import logging
import argparse
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_DIR)

import yaml
import requests

from src.api_client import VLLMClient


DEFAULT_CONFIG = os.path.join(PROJECT_DIR, "config/config.yaml")


def gateway_config() -> dict:
    """当前 KServe 网关风格配置。"""
    return {
        "url": "http://unit-test/v2/models/llm/chat/completions",
        "model": "qwen36-35b-a3b-fp8",
        "max_tokens": 512,
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 0,
        "seed": 42,
        "thinking": {"type": "disabled"},
        "reasoning": None,
        "enable_thinking": None,
        "timeout": 60,
        "max_retry": 1,
        "retry_backoff_base": 2,
    }


def legacy_config() -> dict:
    """旧直连 vLLM（备份方案）风格配置。"""
    cfg = gateway_config()
    cfg.update({
        "url": "http://unit-test/v1/chat/completions",
        "model": "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B",
        "reasoning": {"effort": "none"},
        "enable_thinking": False,
    })
    return cfg


def make_png(path: str, size: int = 64, rgb=(220, 30, 30)) -> str:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    return path


class TestPayloadConstruction(unittest.TestCase):
    """离线校验请求体构造（不联网）。"""

    def setUp(self):
        self.client = VLLMClient(gateway_config(), logging.getLogger("test"))

    def test_gateway_style_payload(self):
        """KServe 网关：只下发 thinking，不带 reasoning / chat_template_kwargs。"""
        payload = self.client._build_payload("sys", "user")
        self.assertEqual(payload["model"], "qwen36-35b-a3b-fp8")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["top_p"], 1.0)
        self.assertEqual(payload["top_k"], 0)
        self.assertEqual(payload["seed"], 42)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("chat_template_kwargs", payload)
        self.assertEqual(payload["messages"][0]["role"], "system")

    def test_legacy_style_payload(self):
        """旧直连 vLLM：reasoning 字典 + chat_template_kwargs 都要下发。"""
        client = VLLMClient(legacy_config(), logging.getLogger("test"))
        payload = client._build_payload("sys", "user")
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_none_params_are_omitted(self):
        """配置为 null 的参数必须完全不出现在请求体里。"""
        cfg = gateway_config()
        cfg.update({"thinking": None, "reasoning": None, "enable_thinking": None})
        client = VLLMClient(cfg, logging.getLogger("test"))
        payload = client._build_payload("sys", "user")
        for key in ("thinking", "reasoning", "chat_template_kwargs"):
            self.assertNotIn(key, payload)

    def test_seed_optional(self):
        cfg = gateway_config()
        cfg["seed"] = None
        client = VLLMClient(cfg, logging.getLogger("test"))
        self.assertNotIn("seed", client._build_payload("sys", "user"))

    def test_extra_params_merged_and_none_skipped(self):
        cfg = gateway_config()
        cfg["extra_params"] = {"chat_template_kwargs": None, "think": False}
        client = VLLMClient(cfg, logging.getLogger("test"))
        payload = client._build_payload("sys", "user")
        self.assertEqual(payload["think"], False)
        self.assertNotIn("chat_template_kwargs", payload)

    def test_multimodal_content_list_passthrough(self):
        content = [
            {"type": "text", "text": "这是什么颜色？"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        payload = self.client._build_payload("sys", content)
        self.assertIsInstance(payload["messages"][1]["content"], list)
        self.assertEqual(payload["messages"][1]["content"][1]["type"], "image_url")

    def test_classify_text_parses_gateway_response(self):
        """网关返回 reasoning_content 时，只取 content。"""
        fake = MagicMock()
        fake.status_code = 200
        fake.raise_for_status.return_value = None
        fake.json.return_value = {
            "model": "qwen36-35b-a3b-fp8",
            "choices": [{"message": {"role": "assistant", "content": "最终分类结果：【认知层】",
                                     "reasoning_content": ""}}],
        }
        with patch("src.api_client.requests.post", return_value=fake) as mock_post:
            out = self.client.classify_text("sys", "user")
            self.assertEqual(out, "最终分类结果：【认知层】")
            sent = mock_post.call_args.kwargs["json"]
            self.assertNotIn("reasoning", sent)
            self.assertNotIn("chat_template_kwargs", sent)

    def test_retry_then_fail_returns_none(self):
        cfg = gateway_config()
        cfg["max_retry"] = 2
        cfg["retry_backoff_base"] = 1
        client = VLLMClient(cfg, logging.getLogger("test"))
        with patch("src.api_client.requests.post", side_effect=requests.exceptions.Timeout):
            self.assertIsNone(client.classify_text("sys", "user"))


# ── 真实接口联调（--live）────────────────────────────────────────
def live_checks(config_path: str, check_legacy: bool = False) -> bool:
    with open(config_path, "r", encoding="utf-8") as f:
        raw_text = f.read()
    config = yaml.safe_load(raw_text)
    api_cfg = config["api"]
    logger = logging.getLogger("live")
    logging.basicConfig(level=logging.WARNING)
    client = VLLMClient(api_cfg, logger)

    results = []

    def record(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    print("\n真实接口联调：")
    print(f"  url = {client.url}")

    # 1) 生产请求体不含网关拒绝的参数
    payload = client._build_payload("你是检测器。", "只回复两个字：正常")
    bad = [k for k in ("reasoning", "chat_template_kwargs") if k in payload]
    record("生产请求体不含 reasoning / chat_template_kwargs", not bad,
           f"仍然包含: {bad}" if bad else "")

    # 2) 实际调用 + 思考是否关闭
    t0 = time.perf_counter()
    try:
        resp = requests.post(client.url, json=payload,
                             timeout=client.timeout, headers={"Content-Type": "application/json"})
        ms = int((time.perf_counter() - t0) * 1000)
        body = resp.json() if resp.status_code == 200 else None
    except Exception as exc:
        body, ms = None, int((time.perf_counter() - t0) * 1000)
        record("接口可达且返回 choices", False, f"{type(exc).__name__}: {exc}")
        body = None
    if body is not None:
        record("接口返回 200 且含 choices", bool(body.get("choices")),
               f"{ms}ms model={body.get('model')}")
        msg = (body.get("choices") or [{}])[0].get("message") or {}
        detail = body.get("usage", {}).get("completion_tokens_details") or {}
        thinking = bool((msg.get("reasoning_content") or "").strip()) or bool(detail.get("reasoning_tokens"))
        record("生产请求体下思考已关闭", not thinking,
               "仍返回思考内容，需要调整关闭参数" if thinking else "无 reasoning_content")
        record("返回内容非空", bool((msg.get("content") or "").strip()),
               repr((msg.get("content") or "")[:20]))

    # 3) 多模态通道
    tmp_png = os.path.join(tempfile.gettempdir(), "api_client_endpoint_test.png")
    make_png(tmp_png)
    try:
        out = client.classify_with_images("你是图像识别助手。", "这张图片主要是什么颜色？只回答颜色。", [tmp_png])
        record("多模态 image_url 可用", bool(out), repr((out or "")[:30]))
    except Exception as exc:
        record("多模态 image_url 可用", False, f"{type(exc).__name__}: {exc}")

    # 4) 可选：配置里注释掉的旧直连地址
    if check_legacy:
        import re
        m_url = re.search(r'#.*旧直连.*url: "([^"]+)"', raw_text)
        m_model = re.search(r'#.*旧直连.*model: "([^"]+)"', raw_text)
        if not m_url:
            record("旧直连地址可用（备份方案）", False, "配置里未找到注释的备份 url")
        else:
            legacy_cfg = dict(api_cfg)
            legacy_cfg["url"] = m_url.group(1)
            if m_model:
                legacy_cfg["model"] = m_model.group(1)
            legacy_cfg.update({"reasoning": {"effort": "none"}, "enable_thinking": False})
            legacy_client = VLLMClient(legacy_cfg, logger)
            lp = legacy_client._build_payload("你是检测器。", "只回复两个字：正常")
            lp["max_tokens"] = 32
            try:
                r = requests.post(legacy_cfg["url"], json=lp, timeout=30)
                ok = r.status_code == 200 and bool(r.json().get("choices"))
                record("旧直连地址可用（备份方案）", ok, f"status={r.status_code}")
            except Exception as exc:
                record("旧直连地址可用（备份方案）", False, f"{type(exc).__name__}: {exc}")

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n联调结果: {len(results) - len(failed)}/{len(results)} 通过")
    return not failed


def main():
    parser = argparse.ArgumentParser(description="模型接口参数与连通性单元测试")
    parser.add_argument("--live", action="store_true", help="执行真实接口联调")
    parser.add_argument("--check-legacy", action="store_true", help="顺带检查配置里备份的旧直连地址")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    args, remaining = parser.parse_known_args()

    print("=" * 80)
    print("离线单元测试（请求体构造）")
    print("=" * 80)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPayloadConstruction)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    ok = result.wasSuccessful()

    if args.live:
        ok = live_checks(args.config, args.check_legacy) and ok

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
