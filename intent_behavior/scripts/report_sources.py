"""为报告准备只读输入：HDFS 归档 + 旧服务器审计，不修改 Worker 或业务数据。

由 send_weekly_report.py 调用；入口为 python3 scripts/send_weekly_report.py --dry-run。
输入为 HDFS status.json / tar.gz 与旧 JSONL，输出为临时合并快照和不含正文的来源信息。
只在 logs/report_source 下写缓存，不改原始审计、失败预算、HDFS 或业务库。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_HDFS_ROOT = "viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks"
TIMEZONE = ZoneInfo("Asia/Shanghai")
ARCHIVE_NAME = re.compile(r"^(\d{8}_\d{6})_[a-fA-F0-9]+\.tar\.gz$")
RUN_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.jsonl$")


class ReportSourceError(RuntimeError):
    """输入缺失/不新鲜/不完整时拒绝把它解释成零业务量。"""


@dataclass(frozen=True)
class Archive:
    name: str
    size: int
    modified: datetime
    started: datetime


@dataclass(frozen=True)
class ReportInput:
    audit_dir: Path
    manifest: Path | None = None


class HdfsClient:
    def __init__(self, runner="hdfs", total_timeout=300):
        self.runner = shlex.split(runner)
        if not self.runner:
            raise ReportSourceError("HDFS 客户端命令不能为空")
        self.deadline = time.monotonic() + total_timeout

    def run(self, *args):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReportSourceError("HDFS 报告同步超过总时间限制")
        env = dict(os.environ, TZ="Asia/Shanghai")
        # -ls 的时间没有偏移，固定 Java 时区后才能与归档名比较。
        env["HADOOP_OPTS"] = env.get("HADOOP_OPTS", "") + " -Duser.timezone=Asia/Shanghai"
        try:
            return subprocess.run(self.runner + ["dfs", *args], check=True, capture_output=True,
                                  text=True, timeout=min(90, remaining), env=env).stdout
        except FileNotFoundError as exc:
            raise ReportSourceError("未找到 HDFS 客户端；请设置 HDFS_RUNNER 为服务器 hdfs 的完整路径") from exc
        except subprocess.CalledProcessError as exc:
            # 仅保留可操作的错误摘要，不把整段 Hadoop 输出或潜在凭据发到机器人。
            hints = ("permission denied", "accesscontrolexception", "no such file", "does not exist",
                     "filenotfoundexception", "standbyexception", "unknownhostexception", "no filesystem",
                     "java_home", "kerberos", "gssexception", "connection refused", "name or service not known")
            detail = next((line.strip() for line in (exc.stderr or "").splitlines()
                           if any(hint in line.lower() for hint in hints)), "")
            detail = re.sub(r"(?i)((?:password|secret|access_token|refresh_token|delegation|authorization)\s*[:=]\s*)\S+",
                            r"\1[REDACTED]", detail)[:400]
            suffix = f"；摘要：{detail}" if detail else ""
            raise ReportSourceError(f"HDFS {args[0]} 读取失败（退出码 {exc.returncode}）{suffix}；"
                                    "请检查客户端、网络及 ads_fst 访问权限") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReportSourceError(f"HDFS {args[0]} 读取失败；请检查客户端、网络及 ads_fst 访问权限") from exc

    def status(self, root):
        try:
            value = json.loads(self.run("-cat", root + "/logs/status.json"))
        except ValueError as exc:
            raise ReportSourceError("HDFS status.json 损坏") from exc
        if not isinstance(value, dict):
            raise ReportSourceError("HDFS status.json 格式不正确")
        return value

    def archives(self, root):
        result = []
        for line in self.run("-ls", root + "/logs").splitlines():
            parts = line.split(None, 7)
            if len(parts) != 8 or not parts[0].startswith("-"):
                continue
            name = PurePosixPath(parts[7]).name
            match = ARCHIVE_NAME.fullmatch(name)
            if not match:
                continue
            try:
                result.append(Archive(name, int(parts[4]),
                    datetime.strptime(parts[5] + " " + parts[6], "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE),
                    datetime.strptime(match[1], "%Y%m%d_%H%M%S").replace(tzinfo=TIMEZONE)))
            except ValueError as exc:
                raise ReportSourceError("HDFS 归档列表的日期或大小不正确") from exc
        return result

    def download(self, uri, target):
        self.run("-get", uri, str(target))


def _fresh_status(status, max_age, now):
    try:
        updated = float(status["updated_at"])
        age = now.timestamp() - updated
        if not -300 <= age <= max_age:
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ReportSourceError("DataWorks 心跳缺失或过期，无法把缺少记录认定为零处理量") from exc
    return datetime.fromtimestamp(updated, TIMEZONE)


def _event_time(event):
    value = event.get("event_time")
    if value in (None, ""):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=TIMEZONE)
        return timestamp.astimezone(TIMEZONE)
    except (TypeError, AttributeError, ValueError) as exc:
        raise ReportSourceError("审计记录的 event_time 无效，拒绝生成不完整统计") from exc


def _merge_audit(stream, target):
    """同名审计快照必须互为完整前缀，只保留较长版本，不能反复追加重复计数。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    start = end = None
    temporary = target.with_suffix(".incoming")
    with temporary.open("wb") as output:
        while True:
            raw = stream.readline(16 * 1024 * 1024 + 1)
            if not raw:
                break
            if len(raw) > 16 * 1024 * 1024:
                raise ReportSourceError("单条 JSONL 超过 16MB 安全限制")
            if not raw.strip():
                continue
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise ReportSourceError(f"JSONL 损坏或尚未完整归档：{target.name}，请等待下一次同步") from exc
            if not isinstance(event, dict):
                raise ReportSourceError(f"JSONL 记录不是对象：{target.name}")
            timestamp = _event_time(event)
            if timestamp is not None:
                start = timestamp if start is None else min(start, timestamp)
                end = timestamp if end is None else max(end, timestamp)
            output.write(raw.rstrip(b"\r\n") + b"\n")
    if target.exists():
        common = min(target.stat().st_size, temporary.stat().st_size)
        with target.open("rb") as old, temporary.open("rb") as new:
            while common:
                count = min(common, 1024 * 1024)
                if old.read(count) != new.read(count):
                    raise ReportSourceError(f"同名审计快照内容冲突：{target.name}，未进行重复合并")
                common -= count
        if target.stat().st_size >= temporary.stat().st_size:
            temporary.unlink()
            return start, end
    os.replace(temporary, target)
    return start, end


def _copy_legacy(source, destination):
    intervals = []
    count = 0
    for path in sorted(source.glob("*/*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            datetime.strptime(path.parent.name, "%Y%m%d")
        except ValueError:
            continue
        if not RUN_NAME.fullmatch(path.name):
            continue
        with path.open("rb") as stream:
            interval = _merge_audit(stream, destination / path.parent.name / path.name)
        if all(interval):
            intervals.append(interval)
        count += 1
    return count, intervals


def _read_archive(path, destination):
    total = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or "\\" in member.name:
                    raise ReportSourceError("HDFS 日志归档含有不安全路径")
                if len(name.parts) != 4 or name.parts[:2] != ("logs", "runs"):
                    continue
                if not name.name.endswith(".jsonl"):
                    continue
                if not member.isfile() or not RUN_NAME.fullmatch(name.name):
                    raise ReportSourceError("HDFS 审计成员不是常规 JSONL 文件")
                try:
                    datetime.strptime(name.parts[2], "%Y%m%d")
                except ValueError as exc:
                    raise ReportSourceError("HDFS 审计目录日期无效") from exc
                total += member.size
                if member.size < 0 or total > 4 * 1024**3:
                    raise ReportSourceError("单份归档的审计数据超过 4GB 安全限制")
                with archive.extractfile(member) as stream:
                    _merge_audit(stream, destination / name.parts[2] / name.name)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise ReportSourceError("无法完整读取 HDFS 日志归档；不使用旧缓存伪造正常报告") from exc


def _check_window(intervals, start_day, end_day, now):
    start = datetime.combine(start_day, datetime.min.time(), TIMEZONE)
    end = min(datetime.combine(end_day + timedelta(days=1), datetime.min.time(), TIMEZONE), now)
    relevant = [(left, right) for left, right in intervals if right >= start and left <= end]
    # 容忍正常平台轮换/同步延迟，但不能拿今天的归档把昨天无输入算成 0。
    grace = timedelta(minutes=15)
    if (not relevant or min(item[0] for item in relevant) > start + grace
            or max(item[1] for item in relevant) < end - grace):
        raise ReportSourceError(f"统计窗口 {start_day} ~ {end_day} 的首尾审计覆盖不足，无法确认零处理量")


@contextmanager
def prepare_report_inputs(source, start_day, end_day, *, legacy_dir=None, cache_dir=None,
                          hdfs_root=DEFAULT_HDFS_ROOT, runner="hdfs", max_age=900, client=None):
    legacy_dir = Path(legacy_dir) if legacy_dir is not None else PROJECT / "logs/runs"
    if source == "local":
        yield ReportInput(legacy_dir)
        return
    if source != "dataworks_hdfs":
        raise ReportSourceError("报告 source 必须为 local 或 dataworks_hdfs")
    hdfs_root = hdfs_root.rstrip("/")
    parsed = urlsplit(hdfs_root)
    if parsed.scheme not in ("viewfs", "hdfs") or not parsed.netloc or ".." in PurePosixPath(parsed.path).parts:
        raise ReportSourceError("无效 HDFS 根目录")
    cache_dir = Path(cache_dir) if cache_dir is not None else PROJECT / "logs/report_source"
    # 缓存目录不能放在另一个用户创建的 0700 父目录内；使用同级 UID 隔离目录。
    # 切换账号无需 chmod/chown 旧缓存，也不会沿用别人的锁或下载内容。
    cache_dir = cache_dir.with_name(cache_dir.name + f"_uid_{os.geteuid()}")
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = client or HdfsClient(runner)
    with (cache_dir / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReportSourceError("另一份报告正在同步 HDFS，请稍后重试") from exc
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=cache_dir) as temporary:
            snapshot = Path(temporary)
            audit_dir = snapshot / "runs"
            audit_dir.mkdir()
            now = datetime.now(TIMEZONE)
            status = client.status(hdfs_root)
            heartbeat = _fresh_status(status, int(max_age), now)
            entries = client.archives(hdfs_root)
            current = str(status.get("archive", ""))
            log_root = hdfs_root + "/logs"
            if not any(current == log_root + "/" + entry.name for entry in entries):
                raise ReportSourceError("心跳引用的当前实例归档不存在，无法验证报告来源")
            legacy_count, intervals = _copy_legacy(legacy_dir, audit_dir)
            selected = [entry for entry in entries if
                        entry.started.date() <= end_day + timedelta(days=1)
                        and entry.modified.date() >= start_day - timedelta(days=1)]
            if not selected and not legacy_count:
                raise ReportSourceError("HDFS 中没有统计窗口对应的日志归档")
            archives = cache_dir / "archives"
            archives.mkdir(exist_ok=True, mode=0o700)
            used = set()
            for entry in selected:
                uri = log_root + "/" + entry.name
                identity = hashlib.sha256(f"{uri}:{entry.size}:{entry.modified.isoformat()}".encode()).hexdigest()
                cached = archives / (identity + ".tar.gz")
                used.add(cached.name)
                # 当前实例归档每 60 秒覆盖；-ls 只有分钟精度，不能仅凭大小/时间复用。
                if not cached.exists() or uri == current:
                    download = snapshot / "download.tar.gz"
                    if download.exists():
                        download.unlink()
                    client.download(uri, download)
                    _read_archive(download, audit_dir)
                    os.replace(download, cached)
                    cached.chmod(0o600)
                else:
                    try:
                        _read_archive(cached, audit_dir)
                    except ReportSourceError:
                        cached.unlink()  # 仅删除本模块可重新下载的损坏缓存。
                        raise
                os.utime(cached, None)
                intervals.append((entry.started, entry.modified + timedelta(minutes=1)))
            _fresh_status(status, int(max_age), datetime.now(TIMEZONE))
            _check_window(intervals, start_day, end_day, now)
            if not list(audit_dir.glob("*/*.jsonl")):
                raise ReportSourceError("归档中没有 JSONL 审计文件，不能生成零值报告")
            retry_counts = status.get("retry_counts", {})
            if not isinstance(retry_counts, dict):
                raise ReportSourceError("HDFS 心跳中的重试计数字段损坏")
            try:
                retry_counts = {key: int(retry_counts.get(key, 0)) for key in ("waiting", "exhausted", "resolved", "inflight")}
                if any(value < 0 for value in retry_counts.values()):
                    raise ValueError()
            except (TypeError, ValueError, OverflowError) as exc:
                raise ReportSourceError("HDFS 心跳中的重试计数无效") from exc
            info = {"source": source, "hdfs_log_root": log_root,
                    "status_uri": log_root + "/status.json",
                    "failure_state_uri": hdfs_root + "/state/retry_state.sqlite3",
                    "synced_at": datetime.now(TIMEZONE).isoformat(), "heartbeat_at": heartbeat.isoformat(),
                    "archive_count": len(selected), "legacy_file_count": legacy_count,
                    "retry_counts": retry_counts, "worker_state": str(status.get("state", "unknown"))}
            manifest = snapshot / "source.json"
            manifest.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
            # 仅清理本模块 45 天未使用的可重建下载缓存，绝不清理 HDFS/原始审计。
            for cached in archives.glob("*.tar.gz"):
                if (not cached.is_symlink() and re.fullmatch(r"[0-9a-f]{64}\.tar\.gz", cached.name)
                        and cached.name not in used and cached.stat().st_mtime < time.time() - 45 * 86400):
                    cached.unlink()
            yield ReportInput(audit_dir, manifest)
