#!/usr/bin/env python3
"""前台监督 worker：周期归档 logs 到指定 HDFS；连续归档失败时停止消费并报错。"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]


def snapshot(destination):
    """每次上传一致的文件快照；不打包媒体或配置；同一实例覆盖同一远端文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "logs.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for source in sorted((PROJECT / "logs").rglob("*")):
                if source.is_file() and not source.is_symlink():
                    # 固定当前长度；流式读取，避免长期运行的大日志占满内存。
                    info = tarfile.TarInfo(source.relative_to(PROJECT).as_posix())
                    stat = source.stat()
                    info.size = stat.st_size
                    info.mtime = int(stat.st_mtime)
                    with source.open("rb") as handle:
                        tar.addfile(info, handle)
        subprocess.run(["hdfs", "dfs", "-put", "-f", str(archive), destination],
                       check=True, timeout=90)


def main():
    destination = os.environ["DATAWORKS_LOG_HDFS"].rstrip("/")
    interval = max(10, int(os.environ.get("DATAWORKS_LOG_SYNC_SEC", "60")))
    subprocess.run(["hdfs", "dfs", "-mkdir", "-p", destination], check=True, timeout=60)
    target = destination + "/" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex + ".tar.gz"
    # 启动前先验证真实写权限；失败时不启动会产生业务写入的 worker。
    snapshot(target)
    child = subprocess.Popen([sys.executable, "-u", "worker.py", "--config", "config/config.yaml"], cwd=PROJECT)
    stopped = False

    def stop(signum, _frame):
        nonlocal stopped
        stopped = True
        if child.poll() is None:
            child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    failures, failed = 0, False
    print("DATAWORKS_LOG_ARCHIVE " + target, flush=True)
    try:
        while child.poll() is None:
            try:
                child.wait(timeout=interval)
            except subprocess.TimeoutExpired:
                pass
            try:
                snapshot(target)
                failures = 0
            except (OSError, subprocess.SubprocessError) as exc:
                failures += 1
                print("DATAWORKS_LOG_SYNC_FAILED " + type(exc).__name__, flush=True)
                if failures >= 3 or stopped:
                    failed = True
                    break
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=90)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                failed = True
        try:
            snapshot(target)
        except (OSError, subprocess.SubprocessError) as exc:
            print("DATAWORKS_FINAL_LOG_SYNC_FAILED " + type(exc).__name__, flush=True)
            failed = True
    return 1 if failed else child.returncode


if __name__ == "__main__":
    sys.exit(main())
