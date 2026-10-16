#!/usr/bin/env bash
# Dataworks SHELL 启动适配：定位项目目录后，前台运行唯一生产入口 worker.py。
# 输入：config/config.yaml；可用 PYTHON_BIN 指定已安装依赖的 Python 可执行文件。
# 输出：worker 日志和审计；执行后会真实分类并通过 HTTP 回写业务结果。
# 从服务器仓库根目录运行：cd intent_behavior && bash run_dataworks.sh
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

export TZ=Asia/Shanghai
export PYTHONUNBUFFERED=1

# 不使用 --once、nohup 或后台运行，保证 Dataworks 跟踪实际 worker 进程。
exec "${PYTHON_BIN:-python3}" -u worker.py --config config/config.yaml
