#!/usr/bin/env python3
"""
意图行为项目 - 博文分类服务主入口（兼容层）

注意：
- 本文件是本地预演入口的兼容别名，实际逻辑委托给 run_classification.py。
- MySQL 正式持续回写唯一入口是 worker.py，不要通过本文件回写。

推荐直接使用预演入口：
  python3 run_classification.py --mid <mid> --uid <uid> --mode auto
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 直接复用新的统一入口，确保 main.py 和 run_classification.py 行为一致
from run_classification import main as _new_main


if __name__ == "__main__":
    _new_main()
