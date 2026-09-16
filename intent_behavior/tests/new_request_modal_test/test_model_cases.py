#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新模型具体样例测试（是否请求成功 / 返回结果 / 与原模型是否一致）
================================================================
按“用例”驱动：
  - cases/*.json 里的每条样例包含输入（博文/转发内容）与预期结果（层级 或 转发判定）
  - 逐条调用新模型接口，断言：请求成功、输出可解析、结果与预期一致
  - 可选 --legacy-url 与旧模型接口逐条对比，判断是否“与原模型结果一致”
  - 可选 --task-id 直接用 MySQL 里已被旧模型回写过 level 的真实博文做对比样本

运行方式：
  # 内置样例（cases/sample_cases.json）
  python3 tests/new_request_modal_test/test_model_cases.py

  # 指定用例文件 / 只要某几条
  python3 tests/new_request_modal_test/test_model_cases.py --cases tests/new_request_modal_test/cases/sample_cases.json
  python3 tests/new_request_modal_test/test_model_cases.py --only 转发

  # 与旧模型接口逐条对比（原模型结果一致性）
  python3 tests/new_request_modal_test/test_model_cases.py \
      --legacy-url http://<旧直连地址>:8087/v1/chat/completions \
      --legacy-model /data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B

  # 用 MySQL 真实数据（以库中 level 作为“原模型结果”），需要能连库的机器
  python3 tests/new_request_modal_test/test_model_cases.py --task-id 1302305683722469377 --limit 10

退出码：0 = 全部用例通过；1 = 有失败。

作者：xuanyu11
"""

import os
import re
import sys
import json
import time
import argparse
from datetime import datetime
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import yaml

try:
    from .compare_llm_endpoints import build_payload_style, post_chat, parse_reply, extract_layer
except ImportError:  # 直接以脚本方式运行
    from compare_llm_endpoints import build_payload_style, post_chat, parse_reply, extract_layer


DEFAULT_CASES = os.path.join(SCRIPT_DIR, "cases", "sample_cases.json")
VERDICT_RE = re.compile(r"转发判定：【([^】]+)】")


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_cases(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"用例文件格式错误（应为 JSON 数组）: {path}")
    return data


def build_prompt(config: Dict[str, Any], case: Dict[str, Any]) -> str:
    """按用例类型拼提示词（与生产一致）。"""
    industry = case.get("industry") or config["classification"].get("default_industry", "汽车")
    brand_terms = case.get("brand_terms") or "无"
    ctype = case.get("type", "classify")
    if ctype == "forward_review":
        tpl = config["prompts"]["forward_review_prompt"]
        return tpl.format(industry=industry, brand_terms=brand_terms,
                          author_name=case.get("author_name") or "未知",
                          content=case.get("content") or "",
                          forward_content=case.get("forward_content") or "")
    tpl = config["prompts"]["industries"][industry]["user_text_template"]
    return tpl.format(industry=industry, brand_terms=brand_terms,
                      author_name=case.get("author_name") or "未知",
                      content=case.get("content") or "")


def system_prompt_for(config: Dict[str, Any], case: Dict[str, Any]) -> str:
    if case.get("type", "classify") == "forward_review":
        return "你是一个转发博文关系审查器。"
    industry = case.get("industry") or config["classification"].get("default_industry", "汽车")
    return config["prompts"]["industries"][industry]["system_prompt"]


def parse_answer(case: Dict[str, Any], content: str) -> str:
    """按用例类型抽取结论（层级 或 转发判定）。"""
    if case.get("type", "classify") == "forward_review":
        found = VERDICT_RE.findall(content or "")
        return found[-1].strip() if found else ""
    return extract_layer(content or "")


def expected_of(case: Dict[str, Any]) -> str:
    return str(case.get("expected_verdict") if case.get("type") == "forward_review"
               else case.get("expected_layer") or "").strip()


def call_endpoint(config: Dict[str, Any], url: str, model: str, style: str,
                  case: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    api_cfg = dict(config["api"])
    api_cfg["url"] = url
    api_cfg["model"] = model
    payload = build_payload_style(style, model, system_prompt_for(config, case),
                                  build_prompt(config, case), api_cfg)
    res = post_chat(url, payload, timeout)
    parsed = parse_reply(res)
    return {
        "ok": res["ok"],
        "status": res["status"],
        "latency_ms": res["latency_ms"],
        "answer": parse_answer(case, parsed["content"] or ""),
        "thinking": bool((parsed.get("reasoning") or "").strip())
                    or bool(parsed.get("reasoning_tokens")),
        "raw": (parsed["content"] or "")[:200],
        "error": "" if res["ok"] else res["text"][:200],
    }


# ── MySQL 真实样本 ───────────────────────────────────────────────
def level_to_layer(config: Dict[str, Any], industry: str, level: int) -> str:
    rules = config["classification"].get("industry_rules", {}).get(industry, {})
    for layer, code in (rules.get("level_mapping") or {}).items():
        if int(code) == int(level):
            return layer
    return ""


def load_cases_from_mysql(config: Dict[str, Any], task_id: int, limit: int) -> List[Dict[str, Any]]:
    """用分表中已被旧模型回写过 level 的真实博文作为对比样本（只读）。"""
    from src.db_client import MySQLTaskRepository

    mysql_cfg = config["mysql"]
    repo = MySQLTaskRepository(mysql_cfg, app_config=config)
    cases: List[Dict[str, Any]] = []
    with repo.connect() as conn:
        task = repo.fetch_task_by_id(conn, task_id)
        if task is None:
            raise RuntimeError(f"未找到 task_id={task_id}")
        records = repo.fetch_pending_mids(conn, task, limit=limit * 5,
                                          only_level_zero=False, for_update=False)
        for rec in records:
            if rec.level == 0:
                continue  # 只对比已被旧模型回写过的记录
            if rec.mid_pids or rec.mid_fids:
                continue  # 媒体类型需要反解，这里只取纯文本
            if len((rec.mid_text or "").strip()) < 20:
                continue
            industry = rec.task_industry_name or config["classification"].get("default_industry", "汽车")
            expected = level_to_layer(config, industry, rec.level) or f"level={rec.level}"
            cases.append({
                "name": f"mysql-{rec.mid}",
                "type": "classify",
                "industry": industry,
                "brand_terms": rec.hit_brand_name or "、".join(rec.task_brand_values) or "无",
                "author_name": rec.mid_uid_name or "未知",
                "content": rec.mid_text,
                "expected_layer": expected,
                "source": f"mysql:{task_id}:level={rec.level}",
            })
            if len(cases) >= limit:
                break
    return cases


def main():
    parser = argparse.ArgumentParser(description="新模型具体样例测试")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"))
    parser.add_argument("--cases", default=DEFAULT_CASES, help="用例 JSON 文件")
    parser.add_argument("--only", default="", help="只跑名称包含该关键字的用例")
    parser.add_argument("--timeout", type=int, default=0, help="超时秒数（0=取 config）")
    parser.add_argument("--new-url", default="", help="新接口 URL（默认取 config.api.url）")
    parser.add_argument("--new-model", default="", help="新接口 model（默认取 config.api.model）")
    parser.add_argument("--legacy-url", default="", help="旧接口 URL，填了才做“与原模型一致”对比")
    parser.add_argument("--legacy-model", default="", help="旧接口 model")
    parser.add_argument("--task-id", type=int, default=0, help="从 MySQL 拉真实样本的任务 id")
    parser.add_argument("--limit", type=int, default=10, help="MySQL 样本条数 / 用例上限")
    parser.add_argument("--report", default="", help="结果 JSON 输出路径")
    args = parser.parse_args()

    config = load_config(args.config)
    api_cfg = config["api"]
    new_url = args.new_url or api_cfg["url"]
    new_model = args.new_model or api_cfg["model"]
    legacy_model = args.legacy_model or api_cfg["model"]
    timeout = args.timeout or api_cfg.get("timeout", 60)

    if args.task_id:
        cases = load_cases_from_mysql(config, args.task_id, args.limit)
        print(f"从 MySQL 取到可对比样本: {len(cases)} 条（仅含已被旧模型回写 level 的纯文本记录）")
    else:
        cases = load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if args.only in c.get("name", "")]
    if not cases:
        print("没有可执行的用例")
        sys.exit(1)

    print("=" * 100)
    print("新模型具体样例测试")
    print("=" * 100)
    print(f"新接口:   {new_url}")
    print(f"新模型:   {new_model}")
    if args.legacy_url:
        print(f"旧接口:   {args.legacy_url}  (model={legacy_model})")
    print(f"用例数:   {len(cases)}")

    rows: List[Dict[str, Any]] = []
    passed = 0
    for i, case in enumerate(cases, 1):
        new_res = call_endpoint(config, new_url, new_model, "new", case, timeout)
        expected = expected_of(case)
        ok = new_res["ok"] and bool(new_res["answer"]) and (not expected or new_res["answer"] == expected)
        if ok:
            passed += 1

        row = {
            "name": case.get("name", f"case-{i}"),
            "type": case.get("type", "classify"),
            "expected": expected,
            "new_answer": new_res["answer"],
            "new_ok": new_res["ok"],
            "new_status": new_res["status"],
            "new_ms": new_res["latency_ms"],
            "new_thinking": new_res["thinking"],
            "new_raw": new_res["raw"],
            "pass": ok,
            "source": case.get("source", ""),
        }
        if not new_res["ok"]:
            row["new_error"] = new_res["error"]

        if args.legacy_url:
            legacy_res = call_endpoint(config, args.legacy_url, legacy_model, "legacy", case, timeout)
            row["legacy_answer"] = legacy_res["answer"]
            row["legacy_ok"] = legacy_res["ok"]
            row["legacy_ms"] = legacy_res["latency_ms"]
            row["legacy_same_as_new"] = bool(legacy_res["answer"]) and legacy_res["answer"] == new_res["answer"]
            if not legacy_res["ok"]:
                row["legacy_error"] = legacy_res["error"]

        rows.append(row)
        flag = "PASS" if ok else "FAIL"
        extra = ""
        if args.legacy_url:
            extra = f" 旧模型={row.get('legacy_answer') or '-'}" \
                    f" 一致={row.get('legacy_same_as_new')}"
        print(f"[{i}/{len(cases)}] [{flag}] {row['name']}  预期={expected or '-'} "
              f"新模型={new_res['answer'] or '-'} ({new_res['latency_ms']}ms){extra}")
        if not ok and not new_res["ok"]:
            print(f"        请求失败: {new_res['error']}")

    print("\n" + "-" * 100)
    print(f"通过: {passed}/{len(rows)}")
    if args.legacy_url:
        same = sum(1 for r in rows if r.get("legacy_same_as_new"))
        print(f"与旧模型结果一致: {same}/{len(rows)}")
    failed = [r["name"] for r in rows if not r["pass"]]
    if failed:
        print("未通过用例: " + ", ".join(failed))

    report_path = args.report or os.path.join(
        PROJECT_DIR, "output", f"model_cases_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"run_time": datetime.now().isoformat(), "new_url": new_url,
                   "legacy_url": args.legacy_url, "results": rows},
                  f, ensure_ascii=False, indent=2)
    print(f"详细结果已写入: {report_path}")
    sys.exit(0 if passed == len(rows) else 1)


if __name__ == "__main__":
    main()
