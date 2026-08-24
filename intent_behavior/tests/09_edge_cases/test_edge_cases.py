#!/usr/bin/env python3
"""
边界特例 / 异常链路测试
=========================

用途：
1. 把各种边界/异常 mid 写入 MySQL 分表后，用此脚本批量跑分类，验证系统行为。
2. 覆盖维度：反解失败、强制模式不匹配、媒体下载失败、空内容、超长内容、
   结果回写异常等。
3. 输出测试报告，包含预期 vs 实际、失败原因、耗时。

设计原则：
- 特例数据由用户写入 MySQL（保证真实反解/下载/模型链路）。
- 预期行为通过 JSON 文件描述（推荐），或启用 --builtin 使用内置预期模板。
- 不伪造结果：真实调用 mid 反解、分类、HTTP 回写（--write-back 开启时）。

运行示例：
  # 1. 查看内置建议的测试用例 SQL
  python3 tests/09_edge_cases/test_edge_cases.py --print-sql-examples

  # 2. 用内置预期模板跑指定分表中的特例（需先在 MySQL 写入对应 mid）
  python3 tests/09_edge_cases/test_edge_cases.py \
      --shard-index 1 --customer-id 2608812381 --builtin

  # 3. 指定自定义预期文件
  python3 tests/09_edge_cases/test_edge_cases.py \
      --shard-index 1 --customer-id 2608812381 \
      --expectations tests/09_edge_cases/expectations.json

  # 4. 只测指定 mids，并开启回写链路验证
  python3 tests/09_edge_cases/test_edge_cases.py \
      --shard-index 1 --customer-id 2608812381 \
      --mids "5239377989207686,5239377989207687,5239377989207688" \
      --builtin --write-back
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.pipeline import ClassifyPipeline
from src.db_client import MySQLTaskRepository, MidRecord
from src.utils import setup_logger


# ───────────────────────────────────────────────────────────────
# 内置预期模板
# 说明：key 是 mid；用户需要把这些 mid 写入 MySQL 分表，字段按实际情况填充。
# expected_stage 取值：success / resolve_error / classify_error / writeback_error / failure
# expected_error_stage 取值：resolve / classify / writeback / unknown（可选）
# expected_layer / expected_media_type：成功时校验（可选）
# ───────────────────────────────────────────────────────────────
BUILTIN_EXPECTATIONS: Dict[str, Dict[str, Any]] = {
    # 维度1：正常链路
    "EDGE_TEXT_001": {
        "desc": "正常纯文本博文",
        "expected_stage": "success",
        "expected_media_type": "text",
    },
    "EDGE_IMAGE_001": {
        "desc": "正常图文博文",
        "expected_stage": "success",
        "expected_media_type": "image",
    },
    "EDGE_VIDEO_001": {
        "desc": "正常视频博文（cover模式）",
        "expected_stage": "success",
        "expected_media_type": "video_cover",
    },

    # 维度2：mid 反解失败
    "EDGE_INVALID_MID_001": {
        "desc": "无效/删除的 mid，反解失败",
        "expected_stage": "failure",
        "expected_error_stage": "resolve",
    },

    # 维度3：强制模式与反解结果不匹配
    "EDGE_FORCE_IMAGE_NO_PIC_001": {
        "desc": "强制 image 模式但反解结果无图片 pid",
        "expected_stage": "failure",
        "expected_error_stage": "classify",
    },
    "EDGE_FORCE_VIDEO_NO_VIDEO_001": {
        "desc": "强制 video 模式但反解结果无视频 fid",
        "expected_stage": "failure",
        "expected_error_stage": "classify",
    },

    # 维度4：媒体下载失败
    "EDGE_BAD_PID_001": {
        "desc": "反解有图片 pid 但实际下载失败（降级为文本）",
        "expected_stage": "success",
        "expected_media_type": "image_fallback_text",
    },
    "EDGE_BAD_FID_001": {
        "desc": "反解有视频 fid 但封面/视频下载失败（降级为文本）",
        "expected_stage": "success",
        "expected_media_type": "video_fallback_text",
    },

    # 维度5：内容异常
    "EDGE_EMPTY_CONTENT_001": {
        "desc": "内容为空（可能反解成功但文字为空）",
        "expected_stage": "success",
        "expected_media_type": "text",
    },
    "EDGE_LONG_CONTENT_001": {
        "desc": "超长文本（验证模型截断/不报错）",
        "expected_stage": "success",
        "expected_media_type": "text",
    },
    "EDGE_NON_AUTO_001": {
        "desc": "非汽车行业内容（验证兜底到认知层）",
        "expected_stage": "success",
        "expected_layer": "认知层",
        "expected_media_type": "text",
    },

    # 维度6：结果回写链路
    "EDGE_WRITEBACK_001": {
        "desc": "正常回写（需 --write-back 且配置正确 URL）",
        "expected_stage": "success",
        "expected_media_type": "text",
    },
}


# ───────────────────────────────────────────────────────────────
# 示例 SQL（供用户参考写入 MySQL 分表）
# ───────────────────────────────────────────────────────────────
SQL_EXAMPLES = """
-- 请根据实际表名 customer_id 修改后执行
-- 注意：mid_pids / mid_fids 字段在当前链路中仅作参考，真实内容来自 mid 反解接口

INSERT INTO nature_ad_super_mid_1
  (customer_id, super_task_id, mid, mid_uid, mid_text, mid_pids, mid_fids, level)
VALUES
  -- 正常文本
  (2608812381, 1296499607471128577, 'EDGE_TEXT_001', '2050767771',
   '今天去看了新车发布会，设计很有科技感', '', '', 0),

  -- 正常图文（mid_pids 可以写真实的或占位，真实分类用反解结果）
  (2608812381, 1296499607471128577, 'EDGE_IMAGE_001', '2050767771',
   '这款 SUV 的空间真大', '006mX07Rly8ifv3xs5535j30ud0plk1m', '', 0),

  -- 正常视频（cover模式）
  (2608812381, 1296499607471128577, 'EDGE_VIDEO_001', '2050767771',
   '试驾体验分享', '', '2362904:4826598285967434', 0),

  -- 无效 mid（用明显不存在的 mid）
  (2608812381, 1296499607471128577, 'EDGE_INVALID_MID_001', '2050767771',
   '这条微博不存在', '', '', 0),

  -- 强制 image 模式但无图
  (2608812381, 1296499607471128577, 'EDGE_FORCE_IMAGE_NO_PIC_001', '2050767771',
   '只有文字没有图', '', '', 0),

  -- 强制 video 模式但无视频
  (2608812381, 1296499607471128577, 'EDGE_FORCE_VIDEO_NO_VIDEO_001', '2050767771',
   '只有文字没有视频', '', '', 0),

  -- 图片下载失败（pid 故意写错）
  (2608812381, 1296499607471128577, 'EDGE_BAD_PID_001', '2050767771',
   '图片_pid_无效', 'INVALID_PID_00000000000000000000', '', 0),

  -- 视频下载失败（fid 故意写错）
  (2608812381, 1296499607471128577, 'EDGE_BAD_FID_001', '2050767771',
   '视频_fid_无效', '', '2362904:INVALID_FID_0000000000', 0),

  -- 空内容
  (2608812381, 1296499607471128577, 'EDGE_EMPTY_CONTENT_001', '2050767771',
   '', '', '', 0),

  -- 超长内容（程序生成）
  (2608812381, 1296499607471128577, 'EDGE_LONG_CONTENT_001', '2050767771',
   REPEAT('这车真不错 ', 2000), '', '', 0),

  -- 非汽车行业内容
  (2608812381, 1296499607471128577, 'EDGE_NON_AUTO_001', '2050767771',
   '今天天气很好，适合去公园散步', '', '', 0),

  -- 回写链路验证
  (2608812381, 1296499607471128577, 'EDGE_WRITEBACK_001', '2050767771',
   '验证回写接口', '', '', 0);
"""


# ───────────────────────────────────────────────────────────────
# 工具函数
# ───────────────────────────────────────────────────────────────
def load_config(config_path: str) -> dict:
    import yaml
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_expectations(path: str) -> Dict[str, Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_stage(result: Dict[str, Any]) -> str:
    """把 ProcessResult 转换为预期阶段字符串。"""
    if result.get("success"):
        return "success"
    error_stage = result.get("error_stage", "")
    if error_stage == "resolve":
        return "resolve_error"
    if error_stage == "classify":
        return "classify_error"
    if error_stage == "writeback":
        return "writeback_error"
    return "failure"


def evaluate_case(mid: str, expected: Dict[str, Any], actual: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """对比预期与实际，返回 (是否通过, 差异说明列表)。"""
    diffs: List[str] = []

    expected_stage = expected.get("expected_stage", "success")
    actual_stage = normalize_stage(actual)

    if expected_stage != actual_stage:
        diffs.append(
            f"阶段不匹配: 预期 {expected_stage}, 实际 {actual_stage} "
            f"(success={actual.get('success')}, error_stage={actual.get('error_stage')})"
        )

    if expected_stage == "success":
        if "expected_layer" in expected and actual.get("layer") != expected["expected_layer"]:
            diffs.append(
                f"层级不匹配: 预期 {expected['expected_layer']}, 实际 {actual.get('layer')}"
            )
        if "expected_media_type" in expected and actual.get("media_type") != expected["expected_media_type"]:
            diffs.append(
                f"媒体类型不匹配: 预期 {expected['expected_media_type']}, 实际 {actual.get('media_type')}"
            )
    else:
        if "expected_error_stage" in expected and actual.get("error_stage") != expected["expected_error_stage"]:
            diffs.append(
                f"错误阶段不匹配: 预期 {expected['expected_error_stage']}, 实际 {actual.get('error_stage')}"
            )

    return len(diffs) == 0, diffs


def fetch_records(
    repo: MySQLTaskRepository,
    table_name: str,
    customer_id: Optional[int],
    mids: Optional[List[str]],
    logger: logging.Logger,
) -> List[MidRecord]:
    """从分表读取记录，支持按 mids 过滤。"""
    with repo.connect() as conn:
        if not repo.table_exists(conn, table_name):
            logger.error(f"分表不存在: {table_name}")
            return []

        if mids:
            # 按 mids 批量查询
            placeholders = ",".join(["%s"] * len(mids))
            sql = f"""
            SELECT *
            FROM {table_name}
            WHERE mid IN ({placeholders})
            """
            params: List[Any] = list(mids)
            if customer_id is not None:
                sql += " AND customer_id = %s"
                params.append(customer_id)
            sql += " ORDER BY id ASC"
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall() or []
        else:
            # 读取全部（受 limit 在外层控制）
            rows = repo.fetch_pending_mids_by_table(
                conn,
                table_name=table_name,
                customer_id=customer_id,
                limit=10000,
                only_level_zero=False,
            )
            rows = [r.raw for r in rows]

    records = [repo._row_to_mid_record(row) for row in rows]
    logger.info(f"从 {table_name} 读取到 {len(records)} 条记录")
    return records


# ───────────────────────────────────────────────────────────────
# 主流程
# ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="边界特例 / 异常链路测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--shard-index", type=int, default=1, help="分表索引")
    parser.add_argument("--customer-id", type=int, default=None, help="customer_id 过滤")
    parser.add_argument("--mids", default="", help="逗号分隔的 mid 列表（空则读取分表全部）")
    parser.add_argument("--expectations", default="", help="预期行为 JSON 文件路径")
    parser.add_argument("--builtin", action="store_true", help="使用内置预期模板")
    parser.add_argument("--write-back", action="store_true", help="测试 HTTP 结果回写链路")
    parser.add_argument("--mode", default="auto", choices=["auto", "text", "image", "video"],
                        help="处理模式")
    parser.add_argument("--limit", type=int, default=0, help="最多处理条数（0=不限制）")
    parser.add_argument("--output-dir", default=os.path.join(os.path.dirname(__file__), "output"),
                        help="测试报告输出目录")
    parser.add_argument("--print-sql-examples", action="store_true",
                        help="打印示例 SQL 并退出")
    args = parser.parse_args()

    if args.print_sql_examples:
        print(SQL_EXAMPLES)
        sys.exit(0)

    if not args.expectations and not args.builtin:
        print("错误：必须指定 --expectations 或 --builtin")
        parser.print_help()
        sys.exit(1)

    config = load_config(os.path.join(PROJECT_ROOT, args.config))
    logger = setup_logger(
        name="test_edge_cases",
        log_dir=os.path.join(PROJECT_ROOT, "logs"),
        level=config.get("logging", {}).get("level", "INFO"),
    )

    # 加载预期
    if args.builtin:
        expectations = dict(BUILTIN_EXPECTATIONS)
        if args.expectations:
            expectations.update(load_expectations(args.expectations))
    else:
        expectations = load_expectations(args.expectations)

    # 确定要查询的 mids
    target_mids: Optional[List[str]] = None
    if args.mids:
        target_mids = [m.strip() for m in args.mids.split(",") if m.strip()]

    mysql_cfg = config.get("mysql", {})
    table_name = f"{mysql_cfg.get('shard_table_prefix', 'nature_ad_super_mid_')}{args.shard_index}"
    repo = MySQLTaskRepository(mysql_cfg, logger, app_config=config)

    records = fetch_records(repo, table_name, args.customer_id, target_mids, logger)

    # 如果用户指定了 mids，只保留有预期的；否则只处理有预期的记录
    records = [r for r in records if r.mid in expectations]
    if args.limit > 0:
        records = records[:args.limit]

    if not records:
        logger.warning("没有匹配到预期模板的记录，请检查 MySQL 中的 mid 是否与预期文件一致")
        sys.exit(0)

    logger.info(f"开始边界测试，共 {len(records)} 条记录，模式={args.mode}，回写={args.write_back}")

    pipeline = ClassifyPipeline(config, logger)
    results: List[Dict[str, Any]] = []
    start_time = time.perf_counter()

    for record in records:
        expected = expectations[record.mid]
        t0 = time.perf_counter()
        process_result = pipeline.process_one(
            mid=record.mid,
            uid=record.mid_uid,
            mode=args.mode,
            write_back=args.write_back,
            record=record,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        actual = process_result.to_dict()

        passed, diffs = evaluate_case(record.mid, expected, actual)

        case_result = {
            "mid": record.mid,
            "uid": record.mid_uid,
            "customer_id": record.customer_id,
            "super_task_id": record.super_task_id,
            "desc": expected.get("desc", ""),
            "expected": expected,
            "actual": actual,
            "elapsed_ms": round(elapsed_ms, 1),
            "passed": passed,
            "diffs": diffs,
        }
        results.append(case_result)

        status = "✅ 通过" if passed else "❌ 未通过"
        logger.info(
            f"[{status}] mid={record.mid} stage={normalize_stage(actual)} "
            f"layer={actual.get('layer')} media_type={actual.get('media_type')} "
            f"elapsed={elapsed_ms:.0f}ms"
        )
        if diffs:
            for d in diffs:
                logger.warning(f"  差异: {d}")

    total_elapsed_s = time.perf_counter() - start_time

    # 摘要
    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    failed_count = total - passed_count

    summary_lines = [
        "=" * 70,
        "边界特例测试摘要",
        "=" * 70,
        f"运行时间:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"分表:         {table_name}",
        f"处理模式:     {args.mode}",
        f"回写测试:     {'是' if args.write_back else '否'}",
        f"总用例数:     {total}",
        f"通过:         {passed_count}  ({passed_count/total*100:.1f}%)",
        f"失败:         {failed_count}  ({failed_count/total*100:.1f}%)",
        f"总耗时:       {total_elapsed_s:.1f}s",
        "=" * 70,
    ]
    for line in summary_lines:
        logger.info(line)

    # 保存报告
    os.makedirs(args.output_dir, exist_ok=True)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_json = os.path.join(args.output_dir, f"edge_case_report_{run_ts}.json")
    output_tsv = os.path.join(args.output_dir, f"edge_case_report_{run_ts}.tsv")

    report = {
        "test_type": "edge_case",
        "run_time": datetime.now().isoformat(),
        "table": table_name,
        "mode": args.mode,
        "write_back": args.write_back,
        "summary": {
            "total": total,
            "passed": passed_count,
            "failed": failed_count,
            "total_elapsed_s": round(total_elapsed_s, 1),
        },
        "results": results,
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    with open(output_tsv, "w", encoding="utf-8") as f:
        f.write("mid\tuid\tdesc\tpassed\texpected_stage\tactual_stage\tlayer\tmedia_type\terror_stage\terror\telapsed_ms\tdiffs\n")
        for r in results:
            actual = r["actual"]
            diffs_str = " | ".join(r["diffs"]) if r["diffs"] else ""
            f.write(
                f"{r['mid']}\t{r['uid']}\t{r['desc']}\t{r['passed']}\t"
                f"{r['expected'].get('expected_stage', '')}\t{normalize_stage(actual)}\t"
                f"{actual.get('layer', '')}\t{actual.get('media_type', '')}\t"
                f"{actual.get('error_stage', '')}\t{actual.get('error', '')[:200]}\t"
                f"{r['elapsed_ms']}\t{diffs_str}\n"
            )

    logger.info(f"报告已保存: {output_json}")
    logger.info(f"TSV 已保存: {output_tsv}")

    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
