#!/usr/bin/env python3
"""构建小型源码发布包；Python/依赖环境单独存放于受限 HDFS 目录。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = [PROJECT / name for name in ("worker.py", "run_dataworks.sh", "config/config.yaml", "check_model_service.py")]
    paths += list((PROJECT / "src").glob("*.py"))
    paths += list((PROJECT / "scripts").glob("*.py"))
    paths += [PROJECT / "scripts/dataworks_bundle_start.sh", PROJECT / "scripts/dataworks_cshell_entry.sh"]
    entries = {"app/" + path.relative_to(PROJECT).as_posix(): path.read_bytes() for path in paths}
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip()
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}
    entries["manifest.json"] = json.dumps({"base_commit": revision, "source_hashes": hashes,
                                            "contains_config": True}, indent=2).encode()
    entries["SHA256SUMS"] = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n"
                                     for name, data in sorted(entries.items())).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr("intent_behavior_release/" + name, data)
    print(json.dumps({"revision": revision, "bytes": args.output.stat().st_size,
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
