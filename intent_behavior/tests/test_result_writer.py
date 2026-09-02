#!/usr/bin/env python3
"""
回写接口单元测试
================
单独测试 LevelUpdateClient 的 HTTP 回写功能，用于排查回写失败问题。

用法：
  # 测试回写接口连通性（使用配置中的 URL）
  python3 tests/test_result_writer.py

  # 指定参数测试
  python3 tests/test_result_writer.py --mid 5333296278144730 --customer-id 2608812381 --task-id 1301222511089811457 --level 6

  # 自定义 URL
  python3 tests/test_result_writer.py --url http://10.133.6.162:8058/api/v1/super-mid/update-level

  # 增加超时时间
  python3 tests/test_result_writer.py --timeout 60

  # 增加重试次数
  python3 tests/test_result_writer.py --max-retry 5
"""

import os
import sys
import argparse
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_DIR)

import yaml
from src.result_writer import LevelUpdateClient


def main():
    parser = argparse.ArgumentParser(description="回写接口单元测试")
    parser.add_argument("--config", default=os.path.join(PROJECT_DIR, "config/config.yaml"))
    parser.add_argument("--url", default="", help="回写接口 URL（默认从配置读取）")
    parser.add_argument("--mid", default="5333296278144730", help="测试 mid")
    parser.add_argument("--customer-id", type=int, default=2608812381, help="customer_id")
    parser.add_argument("--task-id", type=int, default=1301222511089811457, help="task_id")
    parser.add_argument("--level", type=int, default=6, help="level 值")
    parser.add_argument("--timeout", type=int, default=0, help="超时时间（秒，0=使用配置）")
    parser.add_argument("--max-retry", type=int, default=0, help="最大重试次数（0=使用配置）")
    args = parser.parse_args()

    # 加载配置
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    writer_cfg = config.get("result_writer", {})
    url = args.url or writer_cfg.get("url", "")
    timeout = args.timeout or writer_cfg.get("timeout", 30)
    max_retry = args.max_retry or writer_cfg.get("max_retry", 3)
    retry_backoff_base = writer_cfg.get("retry_backoff_base", 2.0)

    if not url:
        print("[ERROR] 未配置回写接口 URL，请在 config.yaml 中配置 result_writer.url 或使用 --url 参数")
        sys.exit(1)

    print("=" * 70)
    print("回写接口单元测试")
    print("=" * 70)
    print(f"URL:          {url}")
    print(f"mid:          {args.mid}")
    print(f"customer_id:  {args.customer_id}")
    print(f"task_id:      {args.task_id}")
    print(f"level:        {args.level}")
    print(f"timeout:      {timeout}s")
    print(f"max_retry:    {max_retry}")
    print(f"retry_backoff: {retry_backoff_base}")
    print("=" * 70)

    # 创建客户端
    client = LevelUpdateClient(
        url=url,
        timeout=timeout,
        max_retry=max_retry,
        retry_backoff_base=retry_backoff_base,
    )

    # 执行回写
    update_time = datetime.now().isoformat()
    print(f"\n开始回写... (update_time={update_time})")
    t_start = time.perf_counter()

    try:
        result = client.update_level(
            customer_id=args.customer_id,
            task_id=args.task_id,
            mid=args.mid,
            level=args.level,
            update_time=update_time,
        )
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        print(f"\n✅ 回写成功！耗时: {elapsed_ms:.0f}ms")
        print(f"响应: {result}")
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        print(f"\n❌ 回写失败！耗时: {elapsed_ms:.0f}ms")
        print(f"错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
