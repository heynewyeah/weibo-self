#!/usr/bin/env python3
"""
意图行为项目 - 博文分类服务主入口（兼容层）

注意：
- 新的完整分类入口已迁移到 run_classification.py
- 本文件保留兼容性，实际逻辑已委托给 run_classification.main()

推荐使用新入口：
  python3 run_classification.py --mid <mid> --uid <uid> --mode auto
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 直接复用新的统一入口，确保 main.py 和 run_classification.py 行为一致
from run_classification import main as _new_main


if __name__ == "__main__":
    _new_main()
