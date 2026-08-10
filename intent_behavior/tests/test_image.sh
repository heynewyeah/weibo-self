#!/bin/bash
# test_image.sh — 图文博文分类测试
# 用法: bash tests/test_image.sh

cd "$(dirname "$0")/.."

python3 main.py \
  --mode single \
  --mid "test_image_001" \
  --uid "test_uid_002" \
  --content "比亚迪宋L实拍，外观真的绝了，灯组设计很有未来感" \
  --pids "006mX07Rly8ifv3xs5535j30ud0plk1m" \
  --verbose
