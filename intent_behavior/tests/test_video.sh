#!/bin/bash
# test_video.sh — 视频博文分类测试（单条，当前退化为文本）
# 用法: bash tests/test_video.sh
# 说明：视频处理未启用时，会自动退化为纯文本分类

cd "$(dirname "$0")/.."

python3 main.py \
  --mode single \
  --mid "test_video_001" \
  --uid "test_uid_003" \
  --content "吉利银河M9极寒测试视频，零下30度冰面80km/h爆胎，高管亲自驾车，安全性真的强" \
  --media_ids "fake_media_id_001" \
  --verbose
