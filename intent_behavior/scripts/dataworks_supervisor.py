#!/usr/bin/env python3
"""前台监督 worker：周期归档 logs 到指定 HDFS；连续归档失败时停止消费并报错。"""
import os
from pathlib import Path
import signal
import sqlite3
import json
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import argparse

PROJECT = Path(__file__).resolve().parents[1]


def local_log_root(entry="worker"):
    if entry not in ("worker", "special"):
        raise ValueError("不支持的 Worker 入口")
    return PROJECT / ("logs/special_888888" if entry == "special" else "logs")


def status_payload(child, archive, failures=0, final=False, entry="worker"):
    now = time.time()
    code = child.poll()
    payload = {"schema": 1, "updated_at": now, "host": socket.gethostname(),
               "pid": child.pid, "state": "running" if code is None else ("stopped" if code == 0 else "failed"),
               "exit_code": code, "archive": archive, "sync_failures": failures,
               "final": final, "worker": {}, "retry_counts": {}, "log_tail": []}
    root = local_log_root(entry)
    if entry == "special":
        payload.update(entry=entry, task_id=888888)
    live = root / "live_worker.json"
    if live.exists():
        payload["worker"] = json.loads(live.read_text())
    db = retry_database(entry)
    if db.exists():
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
            payload["retry_counts"] = dict(conn.execute("SELECT status,COUNT(*) FROM retry_state GROUP BY status"))
    log = root / ("special_worker.log" if entry == "special" else "mysql_worker.log")
    if log.exists():
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - 65536))
            payload["log_tail"] = [line[-1500:] for line in handle.read().decode("utf-8", "replace").splitlines()[-60:]]
    return payload


def publish_status(child, archive, failures=0, final=False, entry="worker"):
    payload = status_payload(child, archive, failures, final, entry)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "status.json"
        path.write_text(json.dumps(payload, ensure_ascii=False))
        subprocess.run(["hdfs", "dfs", "-put", "-f", str(path),
                        os.environ["DATAWORKS_LOG_HDFS"].rstrip("/") + "/status.json"],
                       check=True, timeout=60)
    print("DATAWORKS_HEARTBEAT " + json.dumps({"state": payload["state"],
          "counts": payload["worker"].get("counts", {}), "retry_counts": payload["retry_counts"]}), flush=True)


def retry_database(entry="worker"):
    return local_log_root(entry) / "failures/retry_state.sqlite3"


def sync_retry_state(destination, entry="worker"):
    """用 SQLite backup 获取已提交的一致副本，不能直接复制正在写的数据库。"""
    source = retry_database(entry)
    if not source.exists():
        return
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "retry_state.sqlite3"
        with sqlite3.connect(str(source)) as live, sqlite3.connect(str(backup)) as saved:
            live.backup(saved)
        subprocess.run(["hdfs", "dfs", "-put", "-f", str(backup), destination],
                       check=True, timeout=90)
    print("DATAWORKS_RETRY_STATE_SYNCED", flush=True)


def restore_retry_state(destination, entry="worker"):
    """新容器消费前恢复预算；访问失败或状态损坏时拒绝启动。"""
    parent, name = destination.rsplit("/", 1)
    subprocess.run(["hdfs", "dfs", "-mkdir", "-p", parent], check=True, timeout=60)
    listing = subprocess.run(["hdfs", "dfs", "-ls", parent], check=True,
                             capture_output=True, text=True, timeout=60)
    if not any(line.split()[-1].rstrip("/").endswith("/" + name)
               for line in listing.stdout.splitlines() if line.split()):
        print("DATAWORKS_RETRY_STATE_NEW", flush=True)
        return
    target = retry_database(entry)
    target.parent.mkdir(parents=True, exist_ok=True)
    # /tmp and the scheduler workspace may be different mounts; atomic replace
    # requires the downloaded database to live on the target filesystem.
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        downloaded = Path(tmp) / name
        subprocess.run(["hdfs", "dfs", "-get", destination, str(downloaded)],
                       check=True, timeout=90)
        with sqlite3.connect(str(downloaded)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("HDFS 重试状态损坏，拒绝重置预算")
            rows = conn.execute("SELECT record_key, payload FROM retry_state").fetchall()
            for key, payload in rows:
                # 平台每次分配不同工作目录，不能沿用旧容器的绝对路径。
                path = target.parent / (key + ".json")
                conn.execute("UPDATE retry_state SET failure_log=? WHERE record_key=?", (str(path), key))
                path.write_text(json.dumps(json.loads(payload), ensure_ascii=False, indent=2))
        os.replace(downloaded, target)
        target.chmod(0o600)
    print("DATAWORKS_RETRY_STATE_RESTORED rows=" + str(len(rows)), flush=True)


def snapshot(destination, entry="worker"):
    """每次上传一致的文件快照；不打包媒体或配置；同一实例覆盖同一远端文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "logs.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for source in sorted(local_log_root(entry).rglob("*")):
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
    if os.environ.get("DATAWORKS_STATE_HDFS"):
        sync_retry_state(os.environ["DATAWORKS_STATE_HDFS"], entry)
    print("DATAWORKS_LOG_SYNC_OK " + destination, flush=True)


def main(entry="worker"):
    root = local_log_root(entry)
    destination = os.environ["DATAWORKS_LOG_HDFS"].rstrip("/")
    if entry == "special":
        # 拒绝误用普通任务的 HDFS 根；仅允许特殊任务独立命名空间。
        state = os.environ.get("DATAWORKS_STATE_HDFS", "")
        if (not destination.endswith("/special_888888/logs")
                or state != destination.rsplit("/", 1)[0] + "/state/retry_state.sqlite3"):
            raise ValueError("特殊任务必须设置独立 special_888888 HDFS 日志及重试状态路径")
    interval = max(10, int(os.environ.get("DATAWORKS_LOG_SYNC_SEC", "60")))
    subprocess.run(["hdfs", "dfs", "-mkdir", "-p", destination], check=True, timeout=60)
    if os.environ.get("DATAWORKS_STATE_HDFS"):
        restore_retry_state(os.environ["DATAWORKS_STATE_HDFS"], entry)
    target = destination + "/" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex + ".tar.gz"
    # 启动前先验证真实写权限；失败时不启动会产生业务写入的 worker。
    snapshot(target, entry)
    child_options = {"cwd": PROJECT}
    if entry == "special":
        child_options["env"] = dict(os.environ, DATAWORKS_SPECIAL_LOG_ROOT=str(root))
    command = "worker_special_task.py" if entry == "special" else "worker.py"
    child = subprocess.Popen([sys.executable, "-u", command, "--config", "config/config.yaml"], **child_options)
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
        publish_status(child, target, entry=entry)
        while child.poll() is None:
            try:
                child.wait(timeout=interval)
            except subprocess.TimeoutExpired:
                pass
            try:
                snapshot(target, entry)
                failures = 0
                publish_status(child, target, entry=entry)
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
            snapshot(target, entry)
            publish_status(child, target, failures, final=True, entry=entry)
        except (OSError, subprocess.SubprocessError) as exc:
            print("DATAWORKS_FINAL_LOG_SYNC_FAILED " + type(exc).__name__, flush=True)
            failed = True
    return 1 if failed else child.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", choices=("worker", "special"), default="worker")
    sys.exit(main(parser.parse_args().entry))
