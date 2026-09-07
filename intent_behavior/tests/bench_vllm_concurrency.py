#!/usr/bin/env python3
"""
vLLM 并发能力压测工具
=====================
用途：
  1. 验证当前部署的 vLLM 服务是否支持并发请求（>1 并发时是否仍然全部成功）
  2. 用并发阶梯（1, 2, 4, 8, 16, 32...）找出延迟/成功率开始恶化的临界并发数

原理：
  - 使用与线上 api_client 完全一致的请求体（thinking 关闭、temperature=0、seed=42）
  - 每个并发级别固定发 --per-level 个请求，无重试（让超时/报错直接暴露）
  - 统计：成功率、HTTP 错误、超时、P50/P95/P99 延迟、吞吐（req/s）、输出 tokens/s

结果判读：
  - 若 4 并发耗时明显小于 1 并发耗时的 4 倍、且全部成功 → 服务在做并行推理（支持并发）
  - 随并发升高，吞吐（req/s）先上升后走平 → 平台期开始接近实际容量
  - P95 延迟开始陡增 / 出现超时或 503 → 达到实用极限
  - 若从低并发起就大量超时/报错 → 需先检查服务端（--max-num-seqs、显存、网关限流）

服务端辅助确认（在 vLLM 所在机器执行）：
  curl -s <api_url>/metrics | grep -E "vllm:(num_requests_running|num_requests_waiting|gpu_cache_usage_perc)"
  ps aux | grep vllm        # 查看 --max-num-seqs --tensor-parallel-size 等启动参数

运行方式：
  python3 tests/bench_vllm_concurrency.py
  python3 tests/bench_vllm_concurrency.py --levels "1,2,4,8,16,24,32" --per-level 5
  python3 tests/bench_vllm_concurrency.py --timeout 120 --config config/config.yaml

作者：xuanyu11
版本：v1（2026-09-07）
"""

import os
import sys
import json
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

import yaml
import requests

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

DEFAULT_PROMPT = (
    "这是一条测试博文：全新车型今日正式上市，现场发布了售价和配置，"
    "多家媒体第一时间进行了试驾测评，评论区讨论热烈。"
)


# ── 线程本地 Session（复用 keep-alive 连接）──────────────────
_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_payload(api_cfg: Dict[str, Any], text: str) -> Dict[str, Any]:
    """与 src/api_client.VLLMClient._build_payload 保持一致的请求体。"""
    payload = {
        "model": api_cfg["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是微博博文营销分层分类器。只输出层级名称，"
                    "可选：认知层 / 兴趣层 / 考虑层 / 其他。"
                ),
            },
            {"role": "user", "content": text},
        ],
        "temperature": api_cfg.get("temperature", 0.0),
        "max_tokens": api_cfg.get("max_tokens", 512),
        "top_p": api_cfg.get("top_p", 1.0),
        "top_k": api_cfg.get("top_k", 0),
        "thinking": api_cfg.get("thinking", {"type": "disabled"}),
        "reasoning": api_cfg.get("reasoning", {"effort": "none"}),
    }
    seed = api_cfg.get("seed")
    if seed is not None:
        payload["seed"] = seed
    enable_thinking = api_cfg.get("enable_thinking", False)
    if enable_thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    return payload


def send_one(payload: Dict[str, Any], timeout: float, url: str) -> Dict[str, Any]:
    """发送单个请求（无重试）。"""
    start = time.perf_counter()
    try:
        resp = get_session().post(
            url, json=payload, timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        elapsed = (time.perf_counter() - start) * 1000
        if resp.status_code != 200:
            return {"ok": False, "error": f"http_{resp.status_code}", "latency_ms": elapsed}
        data = resp.json()
        if not data.get("choices"):
            return {"ok": False, "error": "empty_choices", "latency_ms": elapsed}
        content = data["choices"][0].get("message", {}).get("content", "") or ""
        usage = data.get("usage", {}) or {}
        return {
            "ok": True,
            "latency_ms": elapsed,
            "content_chars": len(content),
            "completion_tokens": usage.get("completion_tokens"),
        }
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "timeout", "latency_ms": (time.perf_counter() - start) * 1000}
    except requests.exceptions.RequestException as exc:
        return {
            "ok": False,
            "error": f"request_error: {type(exc).__name__}",
            "latency_ms": (time.perf_counter() - start) * 1000,
        }


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (k - lo) * (sorted_values[hi] - sorted_values[lo])


def run_level(
    payload: Dict[str, Any],
    url: str,
    workers: int,
    total_requests: int,
    timeout: float,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    level_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(send_one, payload, timeout, url) for _ in range(total_requests)]
        for future in as_completed(futures):
            results.append(future.result())
    level_wall = time.perf_counter() - level_start

    ok_items = [r for r in results if r["ok"]]
    fail_items = [r for r in results if not r["ok"]]
    latencies = [r["latency_ms"] for r in ok_items]
    ok_tokens = [r["completion_tokens"] for r in ok_items if r.get("completion_tokens")]
    content_chars = [r["content_chars"] for r in ok_items if r.get("content_chars")]

    error_buckets: Dict[str, int] = {}
    for r in fail_items:
        error_buckets[r["error"]] = error_buckets.get(r["error"], 0) + 1

    ok_count = len(ok_items)
    total_count = len(results)
    return {
        "workers": workers,
        "requests": total_count,
        "wall_sec": round(level_wall, 2),
        "ok": ok_count,
        "fail": total_count - ok_count,
        "success_rate": round(ok_count / total_count * 100, 1) if total_count else 0.0,
        "p50_ms": round(percentile(latencies, 50), 0) if latencies else None,
        "p95_ms": round(percentile(latencies, 95), 0) if latencies else None,
        "p99_ms": round(percentile(latencies, 99), 0) if latencies else None,
        "avg_ms": round(sum(latencies) / len(latencies), 0) if latencies else None,
        "req_per_sec": round(ok_count / level_wall, 2) if level_wall > 0 else 0.0,
        "out_tokens_per_sec": round(sum(ok_tokens) / level_wall, 1) if ok_tokens and level_wall > 0 else None,
        "content_chars_per_req": round(sum(content_chars) / len(content_chars), 0) if content_chars else None,
        "errors": error_buckets,
    }


def main():
    parser = argparse.ArgumentParser(
        description="vLLM 并发能力压测工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径（默认 config/config.yaml）")
    parser.add_argument("--levels", default="1,2,4,8,16,24,32",
                        help="并发阶梯，逗号分隔（默认 1,2,4,8,16,24,32）")
    parser.add_argument("--per-level", type=int, default=5,
                        help="每个并发级别发送的请求数（默认 5）")
    parser.add_argument("--timeout", type=int, default=0,
                        help="单请求超时秒数（默认取配置 api.timeout，建议 >=120）")
    parser.add_argument("--text", default=DEFAULT_PROMPT,
                        help="测试用博文文本（默认内置测试文案）")
    parser.add_argument("--json", action="store_true",
                        help="结果额外写入 output/concurrency_bench_<ts>.json")
    args = parser.parse_args()

    levels = [int(x.strip()) for x in args.levels.split(",") if x.strip()]
    if not levels or any(x <= 0 for x in levels):
        print("--levels 需要是正整数列表")
        sys.exit(2)
    if args.per_level <= 0:
        print("--per-level 需要是正整数")
        sys.exit(2)

    config = load_config(args.config)
    api_cfg = config.get("api", {})
    if not api_cfg.get("url") or not api_cfg.get("model"):
        print(f"配置文件缺少 api.url / api.model: {args.config}")
        sys.exit(2)

    url = api_cfg["url"]
    timeout = args.timeout if args.timeout > 0 else int(api_cfg.get("timeout", 60))
    payload = build_payload(api_cfg, args.text)

    print("=" * 100)
    print("vLLM 并发能力压测")
    print(f"运行时间:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"API:        {url}")
    print(f"model:      {api_cfg['model']}")
    print(f"并发阶梯:   {levels}")
    print(f"每级请求数: {args.per_level}")
    print(f"请求超时:   {timeout}s（无重试）")
    print(f"请求体:     temperature={payload.get('temperature')} seed={payload.get('seed')} "
          f"max_tokens={payload.get('max_tokens')} thinking=disabled")
    print("=" * 100)

    # 先探测服务是否可用
    try:
        probe = send_one(payload, timeout, url)
        if not probe["ok"]:
            print(f"服务探测失败: {probe.get('error')}，请先确认 API 可达、模型已加载")
            sys.exit(2)
        print(f"服务探测 OK：单请求 {probe['latency_ms']:.0f}ms")
    except Exception as exc:
        print(f"服务探测异常: {exc}")
        sys.exit(2)

    print()
    header = (
        f"{'并发':>5} | {'请求':>4} | {'成功':>4} | {'失败':>4} | {'成功率':>7} | "
        f"{'P50ms':>7} | {'P95ms':>7} | {'P99ms':>7} | {'req/s':>6} | "
        f"{'tok/s':>8} | 错误明细"
    )
    print(header)
    print("-" * 100)

    all_levels: List[Dict[str, Any]] = []
    baseline_req_per_sec: Optional[float] = None
    try:
        for idx, workers in enumerate(levels, 1):
            level_result = run_level(payload, url, workers, args.per_level, timeout)
            all_levels.append(level_result)

            err_desc = "; ".join(f"{k}:{v}" for k, v in level_result["errors"].items()) or "-"
            if idx == 1:
                baseline_req_per_sec = level_result["req_per_sec"]
            speedup = (
                f"{level_result['req_per_sec'] / baseline_req_per_sec:.1f}x"
                if baseline_req_per_sec and baseline_req_per_sec > 0 else "-"
            )
            print(
                f"{level_result['workers']:>5} | {level_result['requests']:>4} | "
                f"{level_result['ok']:>4} | {level_result['fail']:>4} | "
                f"{level_result['success_rate']:>6.1f}% | "
                f"{level_result['p50_ms'] if level_result['p50_ms'] is not None else '-':>7} | "
                f"{level_result['p95_ms'] if level_result['p95_ms'] is not None else '-':>7} | "
                f"{level_result['p99_ms'] if level_result['p99_ms'] is not None else '-':>7} | "
                f"{level_result['req_per_sec']:>6.2f} | "
                f"{level_result['out_tokens_per_sec'] if level_result['out_tokens_per_sec'] is not None else '-':>8} | "
                f"{err_desc}（吞吐 {speedup}）"
            )
            if workers != levels[-1]:
                time.sleep(2)  # 级别之间略作停顿，避免上一级的排队请求叠加
    except KeyboardInterrupt:
        print("\n收到中断信号，停止压测")

    print("-" * 100)
    print("判读建议：")
    print("  1. 吞吐倍数（相对 1 并发）在 4~8 并发后仍持续增长 → 服务具备明显并行推理能力")
    print("  2. req/s 走平、P95 开始陡增的位置 → 接近服务实用容量，可当作建议并发上限")
    print("  3. 出现 timeout/http_5xx 的并发点 → 硬上限（已超出服务排队/负载能力）")

    if args.json:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(
            OUTPUT_DIR, f"concurrency_bench_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        record = {
            "time": datetime.now().isoformat(),
            "url": url,
            "model": api_cfg["model"],
            "timeout_sec": timeout,
            "levels": levels,
            "per_level": args.per_level,
            "results": all_levels,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"\n完整结果已写入: {out_path}")

    sys.exit(0)


if __name__ == "__main__":
    main()
