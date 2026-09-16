#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大模型接口平移对比测试（旧：直连 vLLM / 新：KServe 网关）
========================================================
目的：确认新接口能否无缝平移、是否需要调整请求参数。分四部分：

  A. 参数矩阵：对每个接口依次发送 4 种请求体，检查哪些参数被接受：
       1) minimal       仅 model/messages/temperature/max_tokens
       2) prod          与 src/api_client.py 完全一致（thinking/reasoning/top_k/seed/chat_template_kwargs）
       3) prod_no_extra prod 去掉 thinking/reasoning/chat_template_kwargs
       4) prod_think_on prod 且 enable_thinking=True（对比思考开关是否生效）
     并检测返回里是否带思考（reasoning_content / reasoning_tokens）。
  A2. 关思考参数探测：找出每个接口真正能关闭思考的参数（两个网关要求不同）。
  B. 多模态：发送一张内置生成的小图片，确认新接口是否支持 image_url 多模态输入。
  C. 分类一致性：从 MySQL 分表取 N 条真实纯文本博文，用项目真实提示词分别调用两个接口，
     对比抽出的“最终分类结果：【x】”，统计一致率与耗时。
  D. 转发审查一致性：用真实高管转发文本跑 forward_review_prompt，对比“正常/异常”判定。
  E. 结论：给出“是否可直接平移 / 需要调整什么参数”的判定。

运行方式：
  python3 tests/new_request_modal_test/compare_llm_endpoints.py
  python3 tests/new_request_modal_test/compare_llm_endpoints.py --samples 10 --industry 汽车
  python3 tests/new_request_modal_test/compare_llm_endpoints.py --image /path/to/test.jpg     使用真实图片做多模态测试
  python3 tests/new_request_modal_test/compare_llm_endpoints.py --skip-db                     只做接口层测试
  python3 tests/new_request_modal_test/compare_llm_endpoints.py --new-url http://... --new-model ...

作者：xuanyu11
"""

import os
import re
import sys
import json
import time
import base64
import struct
import zlib
import argparse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, PROJECT_DIR)

import requests
import yaml


DEFAULT_NEW_URL = (
    "http://llm-beixian.multimedia.wml.weibo.com/mm-wb-ads/"
    "qwen36-35b-a3b-ads-fst-6aaa4019/v2/models/llm/chat/completions"
)

PING_SYSTEM = "你是模型服务连通性检测器。"
PING_USER = "只回复两个字：正常"

# 数据库不可用时的内置样本（汽车行业纯文本，覆盖不同层级）
BUILTIN_SAMPLES = [
    {
        "mid": "builtin-1",
        "content": "全新蔚来ES9今日正式发布，搭载自研VMC底盘大脑与线控转向，官方公布售价与配置，多家媒体第一时间试驾报道。",
        "industry": "汽车", "brand_terms": "蔚来", "hit_brand": "",
    },
    {
        "mid": "builtin-2",
        "content": "百万公里蔚来ES6拆车实测：底盘用料扎实，电池衰减比预期小，拆解过程发现多处铝合金结构件，整体做工超出同价位水平。",
        "industry": "汽车", "brand_terms": "蔚来", "hit_brand": "",
    },
    {
        "mid": "builtin-3",
        "content": "准备入手蔚来ES9，对比了同价位几款旗舰SUV，销售给了报价和金融方案，纠结选装哪些配置，大家有建议吗？",
        "industry": "汽车", "brand_terms": "蔚来", "hit_brand": "",
    },
    {
        "mid": "builtin-4",
        "content": "今天去试驾了蔚来ES9，线控转向的手感确实特别，低速轻盈高速沉稳，底盘过滤细碎震动很到位，整体驾驶质感满意。",
        "industry": "汽车", "brand_terms": "蔚来", "hit_brand": "",
    },
]

# 真实转发样本（用于转发审查一致性对比；作者为品牌方人员）
BUILTIN_FORWARD_CASES = [
    {
        "mid": "5281823271946996",
        "author": "蔚来马麟",
        "content": "如何全面整合软、硬件，让众多控制系统听从「一个大脑」指挥，实现从分布式向集成式迈进，摆在了研发团队面前。蔚来研发团队是怎么做的？",
        "forward_content": "十余年追寻极致之路，引领行业不断进步，集技术之大成。高度集成+核心自研，蔚来VMC「底盘大脑」赋能蔚来ES9「平流层驾乘体验」。蔚来底盘作为「先行者」，再次定义智能时代底盘新高度。关注@蔚来，转发并在评论区分享蔚来底盘驾乘体验以及对于蔚来ES9的期待，我们将抽取1位朋友，送出NIO Life加电吸管杯。#蔚来##蔚来ES9#",
    },
    {
        "mid": "5280734533390285",
        "author": "蔚来马麟",
        "content": "线控转向的投入使用，是蔚来敢想有为的一个案例。身边开ET9的朋友，对线控转向系统以及这个非常有辨识度的方向盘非常喜欢，属于但用难回的一个配置。ES9也将搭载线控转向系统，大家多体验，一定会满意。",
        "forward_content": "十余年追寻极致之路，引领行业不断进步，集技术为大成，赋能蔚来ES9「平流层智享体验」。蔚来线控转向作为「先行者」，再次定义智能时代转向新高度。关注@蔚来，转发并在评论区分享蔚来线控转向新技术感想与体验，以及对于蔚来ES9的期待，将抽取3位朋友，送出NIO Life加电暖管杯。#蔚来##蔚来ES9##蔚来ES9中国元素#",
    },
]


def make_test_png_b64(size: int = 64, rgb: Tuple[int, int, int] = (220, 30, 30)) -> str:
    """生成一张纯色 PNG（base64），用于多模态通道冒烟测试。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    return base64.b64encode(png).decode("utf-8")


def build_payload(
    model: str,
    system_prompt: str,
    user_content,
    api_cfg: Dict[str, Any],
    max_tokens: Optional[int] = None,
    enable_thinking: Optional[bool] = None,
    include_extra: bool = True,
) -> Dict[str, Any]:
    """与 src/api_client.py::_build_payload 保持一致的请求体。"""
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": api_cfg.get("temperature", 0.0),
        "max_tokens": max_tokens if max_tokens is not None else api_cfg.get("max_tokens", 512),
        "top_p": api_cfg.get("top_p", 1.0),
    }
    if include_extra:
        payload["top_k"] = api_cfg.get("top_k", 0)
        payload["thinking"] = api_cfg.get("thinking", {"type": "disabled"})
        payload["reasoning"] = api_cfg.get("reasoning", {"effort": "none"})
        if api_cfg.get("seed") is not None:
            payload["seed"] = api_cfg["seed"]
        thinking = api_cfg.get("enable_thinking", False) if enable_thinking is None else enable_thinking
        payload["chat_template_kwargs"] = {"enable_thinking": thinking}
    return payload


def build_payload_style(style: str, model: str, system_prompt: str, user_content,
                        api_cfg: Dict[str, Any], max_tokens: Optional[int] = None) -> Dict[str, Any]:
    """
    按接口风格构建可用请求体：
      - legacy：旧接口（Qwen3.6 / vLLM），靠 chat_template_kwargs 关思考，可带 reasoning 字典
      - new   ：新接口（KServe 网关），用 thinking 关思考，不能带 reasoning 字典/chat_template_kwargs
    """
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": api_cfg.get("temperature", 0.0),
        "max_tokens": max_tokens if max_tokens is not None else api_cfg.get("max_tokens", 512),
        "top_p": api_cfg.get("top_p", 1.0),
        "top_k": api_cfg.get("top_k", 0),
    }
    if api_cfg.get("seed") is not None:
        payload["seed"] = api_cfg["seed"]
    if style == "legacy":
        # 旧直连 vLLM 必须靠 reasoning + chat_template_kwargs 关思考；
        # 即使当前 config 已切到网关（这两项为 null），测试时也要兜底成旧接口需要的值
        payload["thinking"] = api_cfg.get("thinking") or {"type": "disabled"}
        payload["reasoning"] = api_cfg.get("reasoning") or {"effort": "none"}
        enable_thinking = api_cfg.get("enable_thinking")
        payload["chat_template_kwargs"] = {
            "enable_thinking": False if enable_thinking is None else enable_thinking
        }
    else:
        payload["thinking"] = {"type": "disabled"}
    return payload


def post_chat(url: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            url, json=payload, timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        try:
            body: Optional[Dict[str, Any]] = resp.json()
        except Exception:
            body = None
        return {
            "ok": resp.status_code == 200 and isinstance(body, dict) and bool(body.get("choices")),
            "status": resp.status_code,
            "latency_ms": latency_ms,
            "body": body,
            "text": resp.text[:500],
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "body": None,
            "text": f"{type(exc).__name__}: {exc}"[:500],
        }


def parse_reply(res: Dict[str, Any]) -> Dict[str, Any]:
    """从响应里提取 content / reasoning / usage 等信息。"""
    out = {"content": "", "reasoning": "", "model": "", "finish_reason": "", "reasoning_tokens": None}
    body = res.get("body") or {}
    out["model"] = body.get("model", "")
    usage = body.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    if isinstance(details, dict):
        out["reasoning_tokens"] = details.get("reasoning_tokens")
    choices = body.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        out["content"] = msg.get("content") or ""
        out["reasoning"] = msg.get("reasoning_content") or ""
        out["finish_reason"] = choices[0].get("finish_reason") or ""
    return out


def has_thinking(parsed: Dict[str, Any]) -> bool:
    if (parsed.get("reasoning") or "").strip():
        return True
    if parsed.get("reasoning_tokens"):
        return True
    content = parsed.get("content") or ""
    return "thinking process" in content.lower()


def run_param_matrix(name: str, url: str, model: str, api_cfg: Dict[str, Any], timeout: int) -> List[Dict]:
    variants = [
        ("minimal", dict(include_extra=False, enable_thinking=None)),
        ("prod", dict(include_extra=True, enable_thinking=None)),
        ("prod_no_extra", dict(include_extra=False, enable_thinking=None)),
        ("prod_think_on", dict(include_extra=True, enable_thinking=True)),
    ]
    results = []
    for label, kwargs in variants:
        payload = build_payload(model, PING_SYSTEM, PING_USER, api_cfg, max_tokens=32, **kwargs)
        res = post_chat(url, payload, timeout)
        parsed = parse_reply(res)
        results.append({
            "endpoint": name, "variant": label, "status": res["status"], "ok": res["ok"],
            "latency_ms": res["latency_ms"], "model": parsed["model"],
            "finish_reason": parsed["finish_reason"],
            "thinking": has_thinking(parsed),
            "reasoning_tokens": parsed["reasoning_tokens"],
            "content": (parsed["content"] or "").strip()[:60],
            "error": "" if res["ok"] else res["text"][:200],
        })
    return results


def run_thinking_probe(name: str, url: str, model: str, api_cfg: Dict[str, Any],
                       timeout: int) -> List[Dict]:
    """探测“关闭思考”到底该用哪个参数（两个接口要求不同）。"""
    candidates = [
        ("thinking_dict", {"thinking": {"type": "disabled"}}),
        ("reasoning_dict", {"reasoning": {"effort": "none"}}),
        ("ctk_dict", {"chat_template_kwargs": {"enable_thinking": False}}),
        ("enable_thinking_top", {"enable_thinking": False}),
        ("recommended", {"thinking": {"type": "disabled"},
                         "top_k": api_cfg.get("top_k", 0),
                         "seed": api_cfg.get("seed", 42)}),
        ("current_prod", {"thinking": {"type": "disabled"},
                          "reasoning": {"effort": "none"},
                          "top_k": api_cfg.get("top_k", 0),
                          "seed": api_cfg.get("seed", 42),
                          "chat_template_kwargs": {"enable_thinking": False}}),
    ]
    results = []
    for label, extra in candidates:
        payload = build_payload(model, PING_SYSTEM, PING_USER, api_cfg,
                                max_tokens=32, include_extra=False)
        payload.update(extra)
        res = post_chat(url, payload, timeout)
        parsed = parse_reply(res)
        results.append({
            "endpoint": name, "candidate": label, "status": res["status"], "ok": res["ok"],
            "thinking": has_thinking(parsed), "latency_ms": res["latency_ms"],
            "content": (parsed["content"] or "").strip()[:30],
            "error": "" if res["ok"] else res["text"][:150],
        })
    return results


def run_multimodal(name: str, url: str, model: str, api_cfg: Dict[str, Any],
                   timeout: int, image_path: str = "", style: str = "new") -> Dict[str, Any]:
    if image_path:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
        mime = "image/jpeg"
    else:
        img_b64 = make_test_png_b64()
        mime = "image/png"

    content = [
        {"type": "text", "text": "这张图片主要是什么颜色？只回答颜色。"},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
    ]
    payload = build_payload_style(style, model, "你是图像识别助手。", content, api_cfg, max_tokens=32)
    res = post_chat(url, payload, timeout)
    parsed = parse_reply(res)
    return {
        "endpoint": name, "status": res["status"], "ok": res["ok"],
        "latency_ms": res["latency_ms"],
        "content": (parsed["content"] or "").strip()[:60],
        "error": "" if res["ok"] else res["text"][:200],
    }


LAYER_RE = re.compile(r"最终分类结果：【([^】]+)】")


def extract_layer(text: str) -> str:
    if not text:
        return ""
    m = LAYER_RE.findall(text)
    if m:
        return m[-1].strip()
    bracket = re.findall(r"【([^】]+)】", text)
    return bracket[-1].strip() if bracket else ""


def load_samples(config: Dict[str, Any], task_id: int, limit: int) -> List[Dict[str, str]]:
    """从 MySQL 分表取真实纯文本博文（只读）。"""
    from src.db_client import MySQLTaskRepository

    mysql_cfg = config.get("mysql", {})
    customer_id = int(mysql_cfg.get("test_customer_id", 0) or 0)
    shard_table = f"nature_ad_super_mid_{customer_id % 20}"

    repo = MySQLTaskRepository(mysql_cfg, app_config=config)
    samples: List[Dict[str, str]] = []
    with repo.connect() as conn:
        if task_id:
            task = repo.fetch_task_by_id(conn, task_id)
            records = repo.fetch_pending_mids(conn, task, limit=limit * 5,
                                              only_level_zero=False, for_update=False) if task else []
        else:
            records = repo.fetch_pending_mids_by_table(conn, shard_table, customer_id=customer_id,
                                                       limit=limit * 5, only_level_zero=False,
                                                       task=None)
        for rec in records:
            if rec.mid_pids or rec.mid_fids:
                continue
            if len((rec.mid_text or "").strip()) < 20:
                continue
            samples.append({
                "mid": rec.mid,
                "content": rec.mid_text,
                "industry": rec.task_industry_name or "",
                "brand_terms": "、".join(rec.task_brand_values) if rec.task_brand_values else "无",
                "hit_brand": rec.hit_brand_name or "",
                "author_name": rec.mid_uid_name or "",
            })
            if len(samples) >= limit:
                break
    return samples


def run_consistency(config: Dict[str, Any], old_url: str, old_model: str,
                    new_url: str, new_model: str, samples: List[Dict[str, str]],
                    industry: str, timeout: int,
                    old_style: str = "legacy", new_style: str = "new") -> List[Dict[str, Any]]:
    prompts = config["prompts"]["industries"][industry]
    system_prompt = prompts["system_prompt"]
    user_tpl = prompts["user_text_template"]
    api_cfg = config["api"]

    rows = []
    for s in samples:
        ind = s["industry"] or industry
        brand_terms = s["hit_brand"] or s["brand_terms"] or "无"
        try:
            user_prompt = user_tpl.format(industry=ind, brand_terms=brand_terms,
                                          author_name=s.get("author_name") or "未知",
                                          content=s["content"])
        except Exception:
            user_prompt = (user_tpl.replace("{industry}", ind)
                           .replace("{brand_terms}", brand_terms)
                           .replace("{author_name}", s.get("author_name") or "未知")
                           .replace("{content}", s["content"]))

        row = {"mid": s["mid"], "content_preview": s["content"][:30]}
        targets = (
            ("old", old_url, old_model, old_style),
            ("new", new_url, new_model, new_style),
        )
        for name, url, model, style in targets:
            payload = build_payload_style(style, model, system_prompt, user_prompt, api_cfg)
            res = post_chat(url, payload, timeout)
            parsed = parse_reply(res)
            row[f"{name}_ok"] = res["ok"]
            row[f"{name}_status"] = res["status"]
            row[f"{name}_layer"] = extract_layer(parsed["content"])
            row[f"{name}_ms"] = res["latency_ms"]
            row[f"{name}_thinking"] = has_thinking(parsed)
            if not res["ok"]:
                row[f"{name}_error"] = res["text"][:150]
        row["match"] = bool(row["old_layer"]) and row["old_layer"] == row["new_layer"]
        rows.append(row)
    return rows


def fetch_model_meta(url: str, timeout: int) -> Dict[str, Any]:
    """查询 KServe 网关的模型元数据（url 去掉 /chat/completions）。"""
    meta_url = url[:-len("/chat/completions")] if url.endswith("/chat/completions") else url
    try:
        resp = requests.get(meta_url, timeout=timeout)
        body = resp.json() if resp.status_code == 200 else None
        return {"url": meta_url, "status": resp.status_code, "meta": body,
                "error": "" if body else resp.text[:200]}
    except Exception as exc:
        return {"url": meta_url, "status": None, "meta": None,
                "error": f"{type(exc).__name__}: {exc}"[:200]}


def parse_backup_endpoint(raw_text: str) -> Tuple[str, str]:
    """从 config.yaml 注释里解析旧直连 vLLM 的 url/model（备份方案）。"""
    m_url = re.search(r'#.*备份.*url: "([^"]+)"', raw_text)
    m_model = re.search(r'#.*备份.*model: "([^"]+)"', raw_text)
    return (m_url.group(1) if m_url else ""), (m_model.group(1) if m_model else "")


def run_forward_review(config: Dict[str, Any], old_url: str, old_model: str,
                       new_url: str, new_model: str, timeout: int,
                       cases: List[Dict[str, str]], old_style: str = "legacy",
                       new_style: str = "new") -> List[Dict[str, Any]]:
    """用项目真实转发审查提示词，对比两个接口的“正常/异常”判定。"""
    tpl = config["prompts"]["forward_review_prompt"]
    api_cfg = config["api"]
    verdict_re = re.compile(r"转发判定：【([^】]+)】")
    rows = []
    for case in cases:
        prompt = tpl.format(industry="汽车", brand_terms="蔚来",
                            author_name=case["author"],
                            content=case["content"],
                            forward_content=case["forward_content"])
        row: Dict[str, Any] = {"mid": case["mid"], "author": case["author"]}
        for name, url, model, style in (("old", old_url, old_model, old_style),
                                        ("new", new_url, new_model, new_style)):
            payload = build_payload_style(style, model, "你是一个转发博文关系审查器。",
                                          prompt, api_cfg, max_tokens=256)
            res = post_chat(url, payload, timeout)
            parsed = parse_reply(res)
            found = verdict_re.findall(parsed["content"] or "")
            row[f"{name}_ok"] = res["ok"]
            row[f"{name}_status"] = res["status"]
            row[f"{name}_verdict"] = found[-1] if found else ""
            row[f"{name}_ms"] = res["latency_ms"]
            if not res["ok"]:
                row[f"{name}_error"] = res["text"][:150]
        row["match"] = bool(row["old_verdict"]) and row["old_verdict"] == row["new_verdict"]
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description="新旧大模型接口平移对比测试")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"))
    parser.add_argument("--old-url", default="", help="旧接口 URL（默认取 config.api.url）")
    parser.add_argument("--old-model", default="", help="旧接口 model（默认取 config.api.model）")
    parser.add_argument("--new-url", default=DEFAULT_NEW_URL, help="新接口 URL")
    parser.add_argument("--new-model", default="", help="新接口 model（默认沿用旧 model 值）")
    parser.add_argument("--timeout", type=int, default=0, help="超时秒数（0=取 config.api.timeout）")
    parser.add_argument("--samples", type=int, default=5, help="分类一致性对比条数（0=跳过）")
    parser.add_argument("--task-id", type=int, default=0, help="取样本的任务 id（0=任意）")
    parser.add_argument("--industry", default="汽车", help="分类一致性所用行业提示词")
    parser.add_argument("--image", default="", help="多模态测试用的真实图片路径（默认内置生成图）")
    parser.add_argument("--old-style", default="legacy", choices=["legacy", "new"],
                        help="旧接口使用的请求体风格（默认 legacy＝chat_template_kwargs 关思考）")
    parser.add_argument("--new-style", default="new", choices=["legacy", "new"],
                        help="新接口使用的请求体风格（默认 new＝thinking 关思考）")
    parser.add_argument("--skip-db", action="store_true", help="跳过分类一致性（不连数据库）")
    parser.add_argument("--output", default="", help="结果 JSON 输出路径")
    args = parser.parse_args()

    config_path = args.config if os.path.isabs(args.config) else os.path.join(PROJECT_DIR, args.config)
    with open(config_path, "r", encoding="utf-8") as f:
        raw_config_text = f.read()
    config = yaml.safe_load(raw_config_text)
    api_cfg = config["api"]

    backup_url, backup_model = parse_backup_endpoint(raw_config_text)
    old_url = args.old_url or backup_url or api_cfg["url"]
    old_model = args.old_model or backup_model or api_cfg["model"]
    new_model = args.new_model or api_cfg["model"]
    timeout = args.timeout or api_cfg.get("timeout", 60)

    endpoints = [("old", old_url, old_model, args.old_style),
                 ("new", args.new_url, new_model, args.new_style)]
    report: Dict[str, Any] = {
        "run_time": datetime.now().isoformat(),
        "endpoints": {n: {"url": u, "model": m, "style": s} for n, u, m, s in endpoints},
    }

    print("=" * 90)
    print("新旧大模型接口平移对比测试")
    print("=" * 90)
    for n, u, m, s in endpoints:
        print(f"[{n}] {u}\n      model={m}  style={s}")
    if not args.old_url and backup_url:
        print("[说明] 旧接口取的是 config.yaml 中注释保留的备份地址（旧直连 vLLM）")
    elif not args.old_url and not backup_url:
        print("[说明] 未在 config.yaml 找到备份地址，旧接口沿用 config.api.url")
    print(f"超时: {timeout}s  分类对比样本: {'跳过' if args.skip_db or args.samples <= 0 else args.samples} 条")

    print("\n" + "-" * 90)
    print("0. 新接口模型元信息（KServe /v2/models/llm）")
    print("-" * 90)
    model_meta = fetch_model_meta(args.new_url, timeout)
    report["model_meta"] = model_meta
    if model_meta.get("meta"):
        meta = model_meta["meta"]
        print(f"模型名: {meta.get('name')}  版本: {meta.get('versions')}  平台: {meta.get('platform')}")
        print("输出项: " + ", ".join(o.get("name", "") for o in meta.get("outputs", [])))
    else:
        print(f"元信息获取失败: status={model_meta['status']} {model_meta['error']}")

    print("\n" + "-" * 90)
    print("A. 参数矩阵（minimal / prod / prod_no_extra / prod_think_on）")
    print("-" * 90)
    matrix = []
    for n, u, m, _s in endpoints:
        matrix.extend(run_param_matrix(n, u, m, api_cfg, timeout))
    report["param_matrix"] = matrix
    print(f"{'endpoint':<10}{'variant':<15}{'status':<8}{'ms':<7}{'model':<14}{'think':<7}{'rsn_tok':<9}content")
    for r in matrix:
        print(f"{r['endpoint']:<10}{r['variant']:<15}{str(r['status']):<8}{r['latency_ms']:<7}"
              f"{(r['model'] or '-')[:13]:<14}{str(r['thinking']):<7}{str(r['reasoning_tokens']):<9}"
              f"{(r['content'] or r['error'])[:40]}")

    print("\n" + "-" * 90)
    print("A2. 关闭思考参数探测（两个接口要求可能不同）")
    print("-" * 90)
    probes = []
    for n, u, m, _s in endpoints:
        probes.extend(run_thinking_probe(n, u, m, api_cfg, timeout))
    report["thinking_probe"] = probes
    print(f"{'endpoint':<10}{'candidate':<22}{'status':<8}{'ms':<7}{'think':<7}content")
    for r in probes:
        print(f"{r['endpoint']:<10}{r['candidate']:<22}{str(r['status']):<8}{r['latency_ms']:<7}"
              f"{str(r['thinking']):<7}{(r['content'] or r['error'])[:40]}")

    print("\n" + "-" * 90)
    print("B. 多模态（image_url）冒烟测试")
    print("-" * 90)
    mm = []
    for n, u, m, s in endpoints:
        r = run_multimodal(n, u, m, api_cfg, timeout, args.image, style=s)
        mm.append(r)
        print(f"[{n}] status={r['status']} ms={r['latency_ms']} ok={r['ok']} "
              f"content={r['content'] or r['error']}")
    report["multimodal"] = mm

    rows: List[Dict[str, Any]] = []
    if not args.skip_db and args.samples > 0:
        print("\n" + "-" * 90)
        print("C. 分类一致性（真实博文，同一提示词分别请求两个接口）")
        print("-" * 90)
        try:
            samples = load_samples(config, args.task_id, args.samples)
        except Exception as exc:
            samples = []
            print(f"取样本失败（数据库不可用？）：{exc}")
            print("改用内置样本继续分类一致性对比")
        if not samples:
            samples = [dict(s) for s in BUILTIN_SAMPLES[:args.samples]]
            print(f"使用内置样本: {len(samples)} 条")
        else:
            print(f"样本数: {len(samples)}")
        if samples:
            rows = run_consistency(config, old_url, old_model, args.new_url, new_model,
                                   samples, args.industry, timeout,
                                   old_style=args.old_style, new_style=args.new_style)
            print(f"{'mid':<20}{'old层':<10}{'new层':<10}{'一致':<7}{'old_ms':<8}{'new_ms':<8}content")
            for r in rows:
                print(f"{r['mid']:<20}{r['old_layer'] or '-':<10}{r['new_layer'] or '-':<10}"
                      f"{str(r['match']):<7}{r['old_ms']:<8}{r['new_ms']:<8}{r['content_preview']}")
    report["consistency"] = rows

    print("\n" + "-" * 90)
    print("D. 转发审查一致性（真实高管转发文本 + 项目真实提示词）")
    print("-" * 90)
    fwd_rows = run_forward_review(config, old_url, old_model, args.new_url, new_model,
                                  timeout, BUILTIN_FORWARD_CASES,
                                  old_style=args.old_style, new_style=args.new_style)
    print(f"{'mid':<20}{'作者':<10}{'old判定':<10}{'new判定':<10}{'一致':<7}{'old_ms':<8}{'new_ms':<8}")
    for r in fwd_rows:
        print(f"{r['mid']:<20}{r['author']:<10}{r['old_verdict'] or '-':<10}{r['new_verdict'] or '-':<10}"
              f"{str(r['match']):<7}{r['old_ms']:<8}{r['new_ms']:<8}")
    report["forward_review"] = fwd_rows

    print("\n" + "=" * 90)
    print("E. 结论")
    print("=" * 90)
    new_prod = next((r for r in matrix if r["endpoint"] == "new" and r["variant"] == "prod"), None)
    new_min = next((r for r in matrix if r["endpoint"] == "new" and r["variant"] == "minimal"), None)
    new_mm = next((r for r in mm if r["endpoint"] == "new"), None)
    old_mm = next((r for r in mm if r["endpoint"] == "old"), None)
    conclusions = []
    if new_prod:
        if new_prod["ok"]:
            conclusions.append("新接口接受生产请求体（thinking/reasoning/top_k/seed/chat_template_kwargs 未报错）")
        else:
            conclusions.append(f"新接口拒绝生产请求体: status={new_prod['status']} {new_prod['error'][:120]}")
        if new_prod["thinking"]:
            conclusions.append("生产参数下新接口仍返回思考内容 -> 需要调整关闭思考的参数")
        else:
            conclusions.append("生产参数下新接口未返回思考内容（思考已关闭）")
    if new_min and new_min["thinking"]:
        conclusions.append("不加关闭参数时新接口默认开启思考（与旧接口一致）")

    def off_ok(endpoint: str) -> List[str]:
        return [r["candidate"] for r in probes
                if r["endpoint"] == endpoint and r["ok"] and not r["thinking"]]

    def rejected(endpoint: str) -> List[str]:
        return [f"{r['candidate']}({r['status']})" for r in probes
                if r["endpoint"] == endpoint and not r["ok"]]

    old_off, new_off = off_ok("old"), off_ok("new")
    conclusions.append(f"旧接口可关闭思考的参数: {', '.join(old_off) or '无'}")
    conclusions.append(f"新接口可关闭思考的参数: {', '.join(new_off) or '无'}")
    if rejected("new"):
        conclusions.append(f"新接口直接报错的参数: {', '.join(rejected('new'))}")
    if rejected("old"):
        conclusions.append(f"旧接口直接报错的参数: {', '.join(rejected('old'))}")
    if "ctk_dict" in old_off and "thinking_dict" in new_off:
        conclusions.append("两接口关闭思考的参数不同 -> 无法用同一份请求体直接平移，需要按接口调整参数"
                           "（旧: chat_template_kwargs / 新: thinking，且新接口不要下发 reasoning 字典）")
    if new_mm:
        if new_mm["ok"]:
            conclusions.append("新接口支持 image_url 多模态输入")
        else:
            conclusions.append(f"新接口多模态失败: status={new_mm['status']} {new_mm['error'][:120]}"
                               "（若为 4xx，需确认新模型是否为纯文本部署）")
    if old_mm and old_mm["ok"]:
        conclusions.append("旧接口多模态正常（对照组）")
    if rows:
        matched = sum(1 for r in rows if r["match"])
        conclusions.append(f"分类一致率: {matched}/{len(rows)}")
    if fwd_rows:
        fwd_matched = sum(1 for r in fwd_rows if r["match"])
        fwd_normal = sum(1 for r in fwd_rows if r["new_verdict"] == "正常")
        conclusions.append(f"转发审查一致率: {fwd_matched}/{len(fwd_rows)}；"
                           f"新接口判定正常的条数: {fwd_normal}/{len(fwd_rows)}")
    for c in conclusions:
        print(" - " + c)
    report["conclusions"] = conclusions

    output = args.output or os.path.join(
        PROJECT_DIR, "output", f"llm_endpoint_compare_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n完整结果已写入: {output}")


if __name__ == "__main__":
    main()
