#!/usr/bin/env python3
"""
测试套件一键运行入口
====================
功能：按顺序执行所有测试阶段，汇总结果。

测试阶段：
  Step 1: 生成测试数据（01_prepare_data）
  Step 2: 单条分类测试（02_single）—— 文本/图片/视频各一条
  Step 3: 批量分类测试（03_batch）
  Step 4: 一致性测试（04_consistency）—— 验证硬要求
  Step 5: 并行批量文本测试（06_parallel_text）—— 可选，验证并发稳定性

输出路径：
  tests/run_all_output/run_all_<timestamp>.json   — 汇总结果
  tests/run_all_output/run_all_<timestamp>.txt    — 可读报告

运行方式：
  # 运行全部测试
  python3 tests/run_all_tests.py

  # 跳过数据生成（fixtures 已存在时）
  python3 tests/run_all_tests.py --skip-prepare

  # 只运行指定阶段（逗号分隔）
  python3 tests/run_all_tests.py --only single,consistency

  # 一致性测试重复次数
  python3 tests/run_all_tests.py --consistency-repeat 10

  # 运行并行批量文本测试（默认 20 条，10 并发）
  python3 tests/run_all_tests.py --only parallel

  # 调整并行测试参数
  python3 tests/run_all_tests.py --only parallel \
    --parallel-limit 100 --parallel-workers 12

运行时间预估：全部阶段约 5~15 分钟

作者：xuanyu11
创建时间：2026-08-12
"""

import os
import sys
import json
import subprocess
import argparse
from datetime import datetime
from typing import List, Dict

# ── 路径设置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "run_all_output")

PYTHON = sys.executable


def run_step(name: str, cmd: List[str], cwd: str = None) -> Dict:
    """运行一个测试步骤，返回结果字典"""
    print(f"\n{'='*60}")
    print(f"▶ {name}")
    print(f"  命令: {' '.join(cmd)}")
    print(f"{'='*60}")

    t_start = datetime.now()
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or PROJECT_DIR,
            capture_output=False,   # 实时输出到终端
            timeout=600             # 10分钟超时
        )
        elapsed = (datetime.now() - t_start).total_seconds()
        success = result.returncode == 0

        status = "✅ PASS" if success else "❌ FAIL"
        print(f"\n{status}  {name}  耗时: {elapsed:.1f}s  退出码: {result.returncode}")

        return {
            "name": name,
            "cmd": " ".join(cmd),
            "success": success,
            "returncode": result.returncode,
            "elapsed_s": round(elapsed, 1),
        }

    except subprocess.TimeoutExpired:
        elapsed = (datetime.now() - t_start).total_seconds()
        print(f"\n⏰ TIMEOUT  {name}  超时（{elapsed:.0f}s）")
        return {
            "name": name,
            "cmd": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "elapsed_s": round(elapsed, 1),
            "error": "超时"
        }
    except Exception as e:
        elapsed = (datetime.now() - t_start).total_seconds()
        print(f"\n💥 ERROR  {name}  异常: {e}")
        return {
            "name": name,
            "cmd": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "elapsed_s": round(elapsed, 1),
            "error": str(e)
        }


def main():
    parser = argparse.ArgumentParser(
        description="测试套件一键运行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--skip-prepare", action="store_true",
                        help="跳过数据生成步骤（fixtures 已存在时使用）")
    parser.add_argument("--only", default="",
                        help="只运行指定阶段，逗号分隔：prepare,single,batch,consistency,parallel")
    parser.add_argument("--consistency-repeat", type=int, default=5,
                        help="一致性测试重复次数（默认5）")
    parser.add_argument("--batch-limit", type=int, default=0,
                        help="批量测试条数限制（0=不限制）")
    parser.add_argument("--parallel-limit", type=int, default=20,
                        help="并行文本测试条数限制（默认20）")
    parser.add_argument("--parallel-workers", type=int, default=10,
                        help="并行文本测试并发数（默认10）")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"),
                        help="配置文件路径")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 确定要运行的阶段
    only_stages = set(args.only.split(",")) if args.only else {"prepare", "single", "batch", "consistency"}

    print("=" * 60)
    print("意图行为项目 - 测试套件")
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"运行阶段: {', '.join(sorted(only_stages))}")
    print(f"配置文件: {args.config}")
    print("=" * 60)

    all_results = []
    total_start = datetime.now()

    # ── Step 1: 生成测试数据 ──────────────────────────────────
    if "prepare" in only_stages and not args.skip_prepare:
        result = run_step(
            "Step 1: 生成测试数据",
            [PYTHON, "tests/01_prepare_data/generate_test_data.py", "--local-only"]
        )
        all_results.append(result)
    elif "prepare" in only_stages and args.skip_prepare:
        print("\n⏭  跳过 Step 1: 生成测试数据（--skip-prepare）")

    # ── Step 2: 单条测试 ──────────────────────────────────────
    if "single" in only_stages:
        for test_name, script in [
            ("Step 2a: 单条文本测试", "tests/02_single/test_single_text.py"),
            ("Step 2b: 单条图片测试", "tests/02_single/test_single_image.py"),
            ("Step 2c: 单条视频测试", "tests/02_single/test_single_video.py"),
        ]:
            result = run_step(test_name, [PYTHON, script, "--config", args.config])
            all_results.append(result)

    # ── Step 3: 批量测试 ──────────────────────────────────────
    if "batch" in only_stages:
        batch_cmd = [PYTHON, "tests/03_batch/test_batch.py", "--config", args.config]
        if args.batch_limit > 0:
            batch_cmd += ["--limit", str(args.batch_limit)]
        result = run_step("Step 3: 批量分类测试", batch_cmd)
        all_results.append(result)

    # ── Step 4: 一致性测试 ────────────────────────────────────
    if "consistency" in only_stages:
        result = run_step(
            "Step 4: 一致性测试",
            [PYTHON, "tests/04_consistency/test_consistency.py",
             "--repeat", str(args.consistency_repeat),
             "--config", args.config]
        )
        all_results.append(result)

    # ── Step 5: 并行批量文本测试 ──────────────────────────────
    if "parallel" in only_stages:
        parallel_cmd = [
            PYTHON, "tests/06_parallel_text/test_parallel_text.py",
            "--config", args.config,
            "--workers", str(args.parallel_workers),
            "--limit", str(args.parallel_limit),
        ]
        result = run_step("Step 5: 并行批量文本测试", parallel_cmd)
        all_results.append(result)

    # ── 汇总报告 ──────────────────────────────────────────────
    total_elapsed = (datetime.now() - total_start).total_seconds()
    pass_count = sum(1 for r in all_results if r["success"])
    fail_count = len(all_results) - pass_count
    overall_pass = fail_count == 0

    report_lines = [
        "",
        "=" * 60,
        "测试套件汇总报告",
        "=" * 60,
        f"运行时间:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"总耗时:    {total_elapsed:.1f}s",
        f"总步骤:    {len(all_results)}",
        f"通过:      {pass_count}",
        f"失败:      {fail_count}",
        f"整体结果:  {'✅ 全部通过' if overall_pass else '❌ 存在失败'}",
        "",
        "各步骤详情:",
    ]

    for r in all_results:
        status = "✅ PASS" if r["success"] else "❌ FAIL"
        report_lines.append(f"  {status}  {r['name']}  ({r['elapsed_s']}s)")
        if not r["success"] and r.get("error"):
            report_lines.append(f"         错误: {r['error']}")

    report_lines.append("=" * 60)

    for line in report_lines:
        print(line)

    # ── 保存结果 ──────────────────────────────────────────────
    output_json = os.path.join(OUTPUT_DIR, f"run_all_{run_ts}.json")
    output_txt = os.path.join(OUTPUT_DIR, f"run_all_{run_ts}.txt")

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump({
            "run_time": datetime.now().isoformat(),
            "total_elapsed_s": round(total_elapsed, 1),
            "overall_pass": overall_pass,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "steps": all_results,
        }, f, ensure_ascii=False, indent=2)

    with open(output_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")

    print(f"\n结果文件:")
    print(f"  JSON: {output_json}")
    print(f"  报告: {output_txt}")

    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
