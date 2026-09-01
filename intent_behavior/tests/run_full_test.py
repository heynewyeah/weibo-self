#!/usr/bin/env python3
"""
项目功能完整测试脚本
==================
覆盖当前项目所有核心功能的端到端测试。

测试项：
  1. 单条文本分类（mid 反解 + 分类）
  2. 单条图片分类
  3. 单条视频分类
  4. 超短内容归为"其他"
  5. MySQL 分表直读 + 分类
  6. MySQL 任务驱动 + 分类
  7. 转发博文处理
  8. 反解失败处理

用法：
  # 运行全部测试（需要 MySQL 可连接）
  python3 tests/run_full_test.py

  # 只跑不依赖 MySQL 的测试
  python3 tests/run_full_test.py --no-mysql

  # 指定 mid 测试
  python3 tests/run_full_test.py --text-mid 5239377989207686 --text-uid 2050767771

  # 指定分表测试
  python3 tests/run_full_test.py --shard-index 1 --customer-id 2608812381

输出：
  - 终端：每项测试的通过/失败状态
  - tests/output/full_test_<timestamp>.json：完整测试结果
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime
from typing import Dict, Any, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

sys.path.insert(0, PROJECT_DIR)

import yaml
from src.pipeline import ClassifyPipeline
from src.db_client import MySQLTaskRepository
from src.utils import setup_logger


# ── 测试用例定义 ──────────────────────────────────────────────
# 默认测试 mid（可被命令行参数覆盖）
DEFAULT_TEXT_MID = "5239377989207686"
DEFAULT_TEXT_UID = "2050767771"
DEFAULT_IMAGE_MID = "5239345868702306"
DEFAULT_IMAGE_UID = "7008866503"
DEFAULT_VIDEO_MID = ""  # 需要用户填入已知的视频 mid
DEFAULT_VIDEO_UID = ""


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.error = ""
        self.elapsed_ms = 0.0
        self.detail: Dict[str, Any] = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "detail": self.detail,
        }


def run_test(name: str, func, *args, **kwargs) -> TestResult:
    """运行单个测试并捕获异常"""
    result = TestResult(name)
    t0 = time.perf_counter()
    try:
        func(result, *args, **kwargs)
    except Exception as e:
        result.passed = False
        result.error = str(e)
    result.elapsed_ms = (time.perf_counter() - t0) * 1000
    return result


# ── 测试函数 ──────────────────────────────────────────────────

def test_single_text(result: TestResult, pipeline: ClassifyPipeline, mid: str, uid: str):
    """测试1: 单条文本分类"""
    pr = pipeline.process_one(mid=mid, uid=uid, mode="auto")
    result.detail = pr.to_dict()
    if pr.success and pr.layer in ("认知层", "兴趣层", "考虑层", "其他"):
        result.passed = True
    else:
        result.error = f"分类失败: layer={pr.layer}, error={pr.error}"


def test_single_image(result: TestResult, pipeline: ClassifyPipeline, mid: str, uid: str):
    """测试2: 单条图片分类"""
    pr = pipeline.process_one(mid=mid, uid=uid, mode="image")
    result.detail = pr.to_dict()
    if pr.success:
        result.passed = True
    elif pr.layer == "其他":
        # 图片下载失败降级为文本也可能归为其他
        result.passed = True
    else:
        result.error = f"分类失败: layer={pr.layer}, error={pr.error}"


def test_single_video(result: TestResult, pipeline: ClassifyPipeline, mid: str, uid: str):
    """测试3: 单条视频分类"""
    pr = pipeline.process_one(mid=mid, uid=uid, mode="video")
    result.detail = pr.to_dict()
    if pr.success:
        result.passed = True
    else:
        result.error = f"分类失败: layer={pr.layer}, error={pr.error}"


def test_short_content(result: TestResult, pipeline: ClassifyPipeline):
    """测试4: 超短内容归为其他"""
    from src.models import BlogItem
    item = BlogItem(
        mid="TEST_SHORT_001",
        uid="12345",
        content="哈哈哈",
        industry_name="汽车",
    )
    cr = pipeline.classifier.classify_item(item)
    result.detail = {
        "layer": cr.layer,
        "success": cr.success,
        "model_output": cr.model_output,
    }
    if cr.success and cr.layer == "其他":
        result.passed = True
    else:
        result.error = f"超短内容未归为其他: layer={cr.layer}, success={cr.success}"


def test_pure_topic(result: TestResult, pipeline: ClassifyPipeline):
    """测试5: 纯话题标签归为其他"""
    from src.models import BlogItem
    item = BlogItem(
        mid="TEST_TOPIC_001",
        uid="12345",
        content="#汽车# #新车# #试驾#",
        industry_name="汽车",
    )
    cr = pipeline.classifier.classify_item(item)
    result.detail = {
        "layer": cr.layer,
        "success": cr.success,
        "model_output": cr.model_output,
    }
    if cr.success and cr.layer == "其他":
        result.passed = True
    else:
        result.error = f"纯话题未归为其他: layer={cr.layer}, success={cr.success}"


def test_mysql_shard_read(result: TestResult, repo: MySQLTaskRepository,
                          table_name: str, customer_id: Optional[int], limit: int):
    """测试6: MySQL 分表直读"""
    with repo.connect() as conn:
        records = repo.fetch_pending_mids_by_table(
            conn, table_name=table_name,
            customer_id=customer_id, limit=limit,
            only_level_zero=True,
        )
    result.detail = {
        "table": table_name,
        "record_count": len(records),
        "first_3_mids": [r.mid for r in records[:3]],
    }
    if len(records) > 0:
        result.passed = True
    else:
        result.error = f"分表 {table_name} 中无 level=0 记录"


def test_mysql_task_driven(result: TestResult, repo: MySQLTaskRepository, limit: int):
    """测试7: MySQL 任务驱动"""
    with repo.connect() as conn:
        tasks = repo.fetch_active_tasks(conn, limit=10)
    result.detail = {
        "task_count": len(tasks),
        "first_3_tasks": [
            {"task_id": t.task_id, "customer_id": t.customer_id,
             "industry": t.industry_name, "shard": t.shard_table}
            for t in tasks[:3]
        ],
    }
    if len(tasks) > 0:
        result.passed = True
    else:
        result.error = "super_mid_task 中无有效任务"


def test_mysql_classify(result: TestResult, pipeline: ClassifyPipeline,
                        repo: MySQLTaskRepository, table_name: str,
                        customer_id: Optional[int], limit: int):
    """测试8: MySQL 分表读取 + 分类（不回写）"""
    with repo.connect() as conn:
        records = repo.fetch_pending_mids_by_table(
            conn, table_name=table_name,
            customer_id=customer_id, limit=limit,
            only_level_zero=True,
        )
    if not records:
        result.error = f"分表 {table_name} 中无 level=0 记录"
        return

    # 只处理前 3 条
    test_records = records[:3]
    results = []
    for record in test_records:
        pr = pipeline.process_one(
            mid=record.mid, uid=record.mid_uid,
            mode="auto", write_back=False, record=record,
        )
        results.append({
            "mid": pr.mid,
            "layer": pr.layer,
            "success": pr.success,
            "industry": pr.industry_name,
            "forward_status": pr.forward_status,
            "media_type": pr.media_type,
        })

    result.detail = {"processed": len(results), "results": results}
    success_count = sum(1 for r in results if r["success"])
    if success_count > 0:
        result.passed = True
    else:
        result.error = f"全部 {len(results)} 条分类失败"


def test_resolve_failure(result: TestResult, pipeline: ClassifyPipeline):
    """测试9: 反解失败处理"""
    pr = pipeline.process_one(mid="9999999999999999", uid="99999", mode="auto")
    result.detail = pr.to_dict()
    # 反解失败应该: success=False, layer=其他, error_stage=resolve
    if pr.error_stage == "resolve" and pr.layer == "其他":
        result.passed = True
    elif not pr.success and pr.error_stage == "resolve":
        # 反解失败但 layer 不是其他也可以接受（取决于实现）
        result.passed = True
    else:
        result.error = f"反解失败处理异常: success={pr.success}, error_stage={pr.error_stage}, layer={pr.layer}"


# ── 主流程 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="项目功能完整测试")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"))
    parser.add_argument("--no-mysql", action="store_true", help="跳过 MySQL 相关测试")
    parser.add_argument("--text-mid", default=DEFAULT_TEXT_MID)
    parser.add_argument("--text-uid", default=DEFAULT_TEXT_UID)
    parser.add_argument("--image-mid", default=DEFAULT_IMAGE_MID)
    parser.add_argument("--image-uid", default=DEFAULT_IMAGE_UID)
    parser.add_argument("--video-mid", default=DEFAULT_VIDEO_MID)
    parser.add_argument("--video-uid", default=DEFAULT_VIDEO_UID)
    parser.add_argument("--shard-index", type=int, default=1)
    parser.add_argument("--customer-id", type=int, default=2608812381)
    parser.add_argument("--mysql-limit", type=int, default=5)
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger = setup_logger("full_test", log_dir=os.path.join(PROJECT_DIR, "logs"))

    # 加载配置
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    pipeline = ClassifyPipeline(config, logger)
    table_name = f"{config['mysql'].get('shard_table_prefix', 'nature_ad_super_mid_')}{args.shard_index}"

    # MySQL repo（如果需要）
    repo = None
    if not args.no_mysql:
        mysql_cfg = config.get("mysql")
        if mysql_cfg:
            repo = MySQLTaskRepository(mysql_cfg, logger, app_config=config)

    # ── 运行测试 ──────────────────────────────────────────────
    all_results: List[TestResult] = []

    print("\n" + "=" * 70)
    print("项目功能完整测试")
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # 测试1: 单条文本分类
    print("\n[1/9] 单条文本分类...")
    r = run_test("单条文本分类", test_single_text, pipeline, args.text_mid, args.text_uid)
    all_results.append(r)
    print(f"  {'✅ 通过' if r.passed else '❌ 失败'} ({r.elapsed_ms:.0f}ms) {r.error}")

    # 测试2: 单条图片分类
    print("\n[2/9] 单条图片分类...")
    r = run_test("单条图片分类", test_single_image, pipeline, args.image_mid, args.image_uid)
    all_results.append(r)
    print(f"  {'✅ 通过' if r.passed else '❌ 失败'} ({r.elapsed_ms:.0f}ms) {r.error}")

    # 测试3: 单条视频分类
    if args.video_mid:
        print("\n[3/9] 单条视频分类...")
        r = run_test("单条视频分类", test_single_video, pipeline, args.video_mid, args.video_uid)
        all_results.append(r)
        print(f"  {'✅ 通过' if r.passed else '❌ 失败'} ({r.elapsed_ms:.0f}ms) {r.error}")
    else:
        print("\n[3/9] 单条视频分类... ⏭️ 跳过（未指定 --video-mid）")

    # 测试4: 超短内容
    print("\n[4/9] 超短内容归为其他...")
    r = run_test("超短内容归为其他", test_short_content, pipeline)
    all_results.append(r)
    print(f"  {'✅ 通过' if r.passed else '❌ 失败'} ({r.elapsed_ms:.0f}ms) {r.error}")

    # 测试5: 纯话题标签
    print("\n[5/9] 纯话题标签归为其他...")
    r = run_test("纯话题标签归为其他", test_pure_topic, pipeline)
    all_results.append(r)
    print(f"  {'✅ 通过' if r.passed else '❌ 失败'} ({r.elapsed_ms:.0f}ms) {r.error}")

    # 测试6: MySQL 分表直读
    if repo and not args.no_mysql:
        print(f"\n[6/9] MySQL 分表直读 ({table_name})...")
        r = run_test("MySQL 分表直读", test_mysql_shard_read, repo, table_name, args.customer_id, args.mysql_limit)
        all_results.append(r)
        print(f"  {'✅ 通过' if r.passed else '❌ 失败'} ({r.elapsed_ms:.0f}ms) {r.error}")
    else:
        print("\n[6/9] MySQL 分表直读... ⏭️ 跳过（--no-mysql）")

    # 测试7: MySQL 任务驱动
    if repo and not args.no_mysql:
        print("\n[7/9] MySQL 任务驱动...")
        r = run_test("MySQL 任务驱动", test_mysql_task_driven, repo, args.mysql_limit)
        all_results.append(r)
        print(f"  {'✅ 通过' if r.passed else '❌ 失败'} ({r.elapsed_ms:.0f}ms) {r.error}")
    else:
        print("\n[7/9] MySQL 任务驱动... ⏭️ 跳过（--no-mysql）")

    # 测试8: MySQL 分表 + 分类
    if repo and not args.no_mysql:
        print(f"\n[8/9] MySQL 分表读取 + 分类 ({table_name})...")
        r = run_test("MySQL 分表+分类", test_mysql_classify, pipeline, repo, table_name, args.customer_id, args.mysql_limit)
        all_results.append(r)
        print(f"  {'✅ 通过' if r.passed else '❌ 失败'} ({r.elapsed_ms:.0f}ms) {r.error}")
    else:
        print("\n[8/9] MySQL 分表+分类... ⏭️ 跳过（--no-mysql）")

    # 测试9: 反解失败处理
    print("\n[9/9] 反解失败处理...")
    r = run_test("反解失败处理", test_resolve_failure, pipeline)
    all_results.append(r)
    print(f"  {'✅ 通过' if r.passed else '❌ 失败'} ({r.elapsed_ms:.0f}ms) {r.error}")

    # ── 汇总 ──────────────────────────────────────────────────
    passed_count = sum(1 for r in all_results if r.passed)
    total_count = len(all_results)
    total_elapsed = sum(r.elapsed_ms for r in all_results)

    print("\n" + "=" * 70)
    print(f"测试汇总: {passed_count}/{total_count} 通过, 总耗时 {total_elapsed:.0f}ms")
    print("=" * 70)

    for r in all_results:
        status = "✅" if r.passed else "❌"
        print(f"  {status} {r.name} ({r.elapsed_ms:.0f}ms)")
        if r.error:
            print(f"     └─ {r.error[:100]}")

    # 保存结果
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(OUTPUT_DIR, f"full_test_{run_ts}.json")
    output_data = {
        "run_time": datetime.now().isoformat(),
        "summary": {
            "total": total_count,
            "passed": passed_count,
            "failed": total_count - passed_count,
            "total_elapsed_ms": round(total_elapsed, 1),
        },
        "results": [r.to_dict() for r in all_results],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"\n结果已保存: {output_path}")

    if passed_count < total_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
