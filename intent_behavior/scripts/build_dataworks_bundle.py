#!/usr/bin/env python3
"""在任意开发机打包 Linux Python、预下载 wheel 和项目；不执行打包进去的 Linux 程序。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
PYTHON_SHA256 = "3b8e5a248bcc351265ffe68f5e457668a2e09c99a47f81f658988c7d3d107522"
EXPECTED = {"PyYAML", "requests", "PyMySQL", "opencv-python-headless", "numpy", "tzdata"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    archive = args.assets / "python-linux.tar.gz"
    if sha(archive.read_bytes()) != PYTHON_SHA256:
        raise SystemExit("Python 运行包哈希不匹配，拒绝打包")
    entries = {"runtime/python-linux.tar.gz": archive.read_bytes()}
    lock, packages = [], set()
    for wheel in sorted((args.assets / "wheels").glob("*.whl")):
        data = wheel.read_bytes()
        with zipfile.ZipFile(wheel) as z:
            metadata = z.read(next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))).decode()
        fields = dict(line.split(": ", 1) for line in metadata.splitlines() if line.startswith(("Name: ", "Version: ")))
        packages.add(fields["Name"].lower().replace("_", "-"))
        lock.append(f'{fields["Name"]}=={fields["Version"]} --hash=sha256:{sha(data)}')
        entries["wheels/" + wheel.name] = data
    missing = {p.lower() for p in EXPECTED} - packages
    if missing:
        raise SystemExit("缺少依赖: " + ", ".join(sorted(missing)))
    locked = (PROJECT / "requirements.dataworks.lock").read_bytes()
    if locked != ("\n".join(lock) + "\n").encode():
        raise SystemExit("wheel 集合或哈希与 requirements.dataworks.lock 不匹配，拒绝打包")
    entries["requirements.dataworks.lock"] = locked
    # 明确白名单：不打包媒体、日志、导出数据、Git 或开发机虚拟环境。
    paths = [PROJECT / n for n in ("worker.py", "worker_special_task.py", "run_dataworks.sh", "config/config.yaml", "check_model_service.py")]
    paths += list((PROJECT / "src").glob("*.py"))
    paths += list((PROJECT / "scripts").glob("*.py"))
    paths += [PROJECT / "scripts/dataworks_bundle_start.sh", PROJECT / "scripts/dataworks_cshell_entry.sh"]
    for path in paths:
        entries["app/" + path.relative_to(PROJECT).as_posix()] = path.read_bytes()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip()
    entries["manifest.json"] = json.dumps({"base_commit": revision,
        "source_hashes": {n: sha(d) for n, d in entries.items() if n.startswith("app/")},
        "python_release": "astral/python-build-standalone 20261013 CPython 3.11.17 Linux x86_64 glibc>=2.17",
        "contains_config": True}, indent=2).encode()
    entries["SHA256SUMS"] = "".join(f"{sha(d)}  {n}\n" for n, d in sorted(entries.items())).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in entries.items():
            bundle.writestr("intent_behavior_dataworks/" + name, data)
    print(json.dumps({"bundle": str(args.output), "bytes": args.output.stat().st_size,
                      "sha256": sha(args.output.read_bytes()), "wheels": len(lock)}))


if __name__ == "__main__":
    main()
