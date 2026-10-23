#!/usr/bin/env python3
"""特殊汽车任务 888888：独立扫描 20 个分表的 level=0，默认持续分类并回写。

--dry-run 自动只跑一轮，调用真实反解/模型但不回写；可用 --limit 1 控制首次预演。
--once 只跑一轮，会真实回写；--limit 是每轮总量上限，默认每表最多 100 条。
日志/审计/重试位于 logs/special_888888/，不依赖 super_mid_task。
"""

import argparse
import json
import signal

from worker import interrupt_on_termination, load_config
from src.special_task_worker import SpecialTaskWorker, build_special_config
from src.utils import setup_logger


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--once", action="store_true", help="仅执行一轮，会真实回写")
    parser.add_argument("--dry-run", action="store_true", help="仅预演一轮，调用模型但不回写")
    parser.add_argument("--limit", type=int, default=0, help="每轮最多处理条数，0=不额外限量")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit 不能为负数")
    signal.signal(signal.SIGTERM, interrupt_on_termination)
    config = load_config(args.config)
    isolated = build_special_config(config, args.dry_run)
    logging_cfg = isolated["logging"]
    logger = setup_logger(
        name="special_worker", log_dir=logging_cfg["dir"],
        level=logging_cfg.get("level", "INFO"), retention_days=int(logging_cfg.get("retention_days", 30)),
        console_enabled=bool(logging_cfg.get("console_enabled", True)),
        file_enabled=bool(logging_cfg.get("file_enabled", True)), storage_config=isolated.get("storage", {}),
    )
    logger.info("特殊入口: task_id=888888 level=0 行业=汽车 扫描20分表 日志=%s", logging_cfg["dir"])
    worker = SpecialTaskWorker(config, logger, dry_run=args.dry_run, limit=args.limit)
    lock_fd = None
    try:
        if args.once or args.dry_run:
            lock_fd = worker._acquire_instance_lock()
            summary = worker.run_once()
            print(json.dumps(summary, ensure_ascii=False))
            return 1 if summary["fail"] else 0
        worker.run_forever()
        return 0
    except KeyboardInterrupt:
        logger.warning("特殊 Worker 已中止，未完成记录保持 level=0")
        return 130
    except Exception:
        logger.exception("特殊 Worker 执行失败")
        return 1
    finally:
        worker.pipeline.audit.finalize({"special_task_id": 888888, "dry_run": args.dry_run})
        if lock_fd is not None:
            lock_fd.close()


if __name__ == "__main__":
    raise SystemExit(main())
