#!/usr/bin/env python3
"""
nature_ad_super_mid_x 测试脚本。

用途：
1. 在测试阶段即使 [`super_mid_task`](intent_behavior/tests/07_mysql_worker/test_nature_ad_super_mid.py:1) 没有可用实际任务，
   也可以直接验证 [`nature_ad_super_mid_x`](intent_behavior/tests/07_mysql_worker/test_nature_ad_super_mid.py:1) 分表中的测试数据。
2. 直接读取指定分表，检查文本 / 图片 / 视频测试样例是否存在。
3. 将分表记录映射为 [`BlogItem`](intent_behavior/src/models.py:28)，验证字段适配是否正确。
4. 可选调用 [`BlogClassifier.classify_item()`](intent_behavior/src/classifier.py:81) 对单条或多条分表样例进行分类验证。

运行示例：
  python3 tests/07_mysql_worker/test_nature_ad_super_mid.py --table nature_ad_super_mid_1 --limit 10
  python3 tests/07_mysql_worker/test_nature_ad_super_mid.py --table nature_ad_super_mid_1 --run-classify --limit 3
"""

import argparse
import os
import sys
import yaml

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.classifier import BlogClassifier
from src.db_client import MySQLTaskRepository
from src.utils import setup_logger


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_rows(repo, conn, table_name: str, limit: int):
    sql = f"""
    SELECT *
    FROM {table_name}
    ORDER BY id ASC
    LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        return cur.fetchall() or []


def main():
    parser = argparse.ArgumentParser(description="nature_ad_super_mid_x 测试脚本")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--table", default="nature_ad_super_mid_1", help="要验证的分表名")
    parser.add_argument("--limit", type=int, default=10, help="最多读取多少条记录")
    parser.add_argument("--run-classify", action="store_true", help="是否对读取结果直接执行分类")
    args = parser.parse_args()

    config = load_config(os.path.join(PROJECT_ROOT, args.config))
    logger = setup_logger(
        name="test_nature_ad_super_mid",
        log_dir=os.path.join(PROJECT_ROOT, config.get("logging", {}).get("dir", "logs")),
        level=config.get("logging", {}).get("level", "INFO"),
    )

    repo = MySQLTaskRepository(config.get("mysql", {}), logger)
    classifier = BlogClassifier(config, logger) if args.run_classify else None

    with repo.connect() as conn:
        if not repo.table_exists(conn, args.table):
            print(f"分表不存在: {args.table}")
            return

        rows = fetch_rows(repo, conn, args.table, args.limit)
        print("=" * 100)
        print(f"分表: {args.table}")
        print(f"记录数: {len(rows)}")
        print("=" * 100)

        for idx, row in enumerate(rows, 1):
            record = repo._row_to_mid_record(row)
            item = record.to_blog_item()
            print(
                f"[{idx}] row_id={record.id} customer_id={record.customer_id} "
                f"super_task_id={record.super_task_id} mid={record.mid} uid={record.mid_uid}"
            )
            print(
                f"    text_len={len(record.mid_text or '')} "
                f"pic_count={len(item.pic_ids)} video_count={len(item.media_ids)} level={record.level}"
            )
            print(f"    text_preview={(record.mid_text or '')[:80]}")
            if item.pic_ids:
                print(f"    pic_ids={item.pic_ids}")
            if item.media_ids:
                print(f"    media_ids={item.media_ids}")

            if classifier is not None:
                result = classifier.classify_item(item)
                print(
                    f"    classify => success={result.success} layer={result.layer} media_type={result.media_type}"
                )
                if result.error:
                    print(f"    error => {result.error}")
            print("-" * 100)


if __name__ == "__main__":
    main()
