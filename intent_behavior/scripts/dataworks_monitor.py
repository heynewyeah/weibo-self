#!/usr/bin/env python3
"""只读查看 Dataworks 心跳、处理计数、日志与单条失败档案；不连接业务库或调用分类/回写。"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

DEFAULT_ROOT = "viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks"


def assess(status, now=None, stale_seconds=180):
    now = time.time() if now is None else now
    age = max(0, now - float(status["updated_at"]))
    state = status.get("state", "unknown")
    if age > stale_seconds and state == "running":
        health = "心跳过期，请检查 Dataworks 运行历史"
    elif state == "running":
        health = "运行中"
    elif state == "stopped":
        health = "已停止"
    else:
        health = "运行失败"
    return health, round(age, 1)


def read_status(root):
    result = subprocess.run(["hdfs", "dfs", "-cat", root.rstrip("/") + "/logs/status.json"],
                            check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def show(status, tail=20):
    health, age = assess(status)
    worker = status.get("worker", {})
    print("=" * 70)
    title = "Dataworks 特殊任务888888" if status.get("task_id") == 888888 else "Dataworks 原生内容站分类"
    print(title + "：" + health + "；归档心跳距今 " + str(age) + " 秒")
    print("主机=" + str(status.get("host")) + "；PID=" + str(status.get("pid")) +
          "；退出码=" + str(status.get("exit_code")))
    print("本次进程累计=" + json.dumps(worker.get("counts", {}), ensure_ascii=False))
    print("最新轮询=" + json.dumps(worker.get("loop_summary", {}), ensure_ascii=False))
    print("重试状态=" + json.dumps(status.get("retry_counts", {}), ensure_ascii=False))
    print("最近结果=" + json.dumps(worker.get("last_result", {}), ensure_ascii=False))
    print("完整归档=" + str(status.get("archive", "")))
    print("说明：累计数按处理尝试统计；归档约每60秒更新；心跳不能替代真实回写核验。")
    if tail:
        print("最近日志（最多" + str(tail) + "行）：")
        for line in status.get("log_tail", [])[-tail:]:
            print(line)
    print(flush=True)


def read_failure(root, mid, task_id=""):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "retry.sqlite3"
        subprocess.run(["hdfs", "dfs", "-get", root.rstrip("/") + "/state/retry_state.sqlite3", str(path)],
                       check=True, timeout=60)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
            for key, attempts, state, raw in conn.execute("SELECT record_key,attempts,status,payload FROM retry_state"):
                payload = json.loads(raw)
                record = payload.get("current_record", payload.get("record", {}))
                if str(record.get("mid")) != str(mid):
                    continue
                if task_id and str(record.get("super_task_id")) != str(task_id):
                    continue
                print(json.dumps({"record_key": key, "attempts": attempts, "status": state,
                                  "payload": payload}, ensure_ascii=False, indent=2))
                return True
    print("未找到该博文的持久化失败档案；可能尚未失败、已首次成功，或未到同步时间。")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hdfs-root", default=DEFAULT_ROOT)
    parser.add_argument("--tail", type=int, default=20)
    parser.add_argument("--watch", action="store_true", help="持续刷新；Ctrl+C仅退出监控")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--mid", default="", help="查看指定MID完整失败档案")
    parser.add_argument("--task-id", default="")
    args = parser.parse_args()
    if not 0 <= args.tail <= 60 or args.interval < 5:
        parser.error("tail须为0到60，interval至少5秒")
    try:
        if args.mid:
            read_failure(args.hdfs_root, args.mid, args.task_id)
            return
        while True:
            show(read_status(args.hdfs_root), args.tail)
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("已退出查看，worker继续运行。")
    except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as exc:
        raise SystemExit("监控数据读取失败，不能判断worker健康：" + str(exc))


if __name__ == "__main__":
    main()
