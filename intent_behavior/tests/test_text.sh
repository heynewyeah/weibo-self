#!/bin/bash
# test_text.sh — 纯文本博文分类测试
# 用法: bash tests/test_text.sh

cd "$(dirname "$0")/.."

python3 main.py \
  --mode single \
  --mid "5250218712893321" \
  --uid "test_uid_001" \
  --content "#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里，第五代智能电混双擎加持，WLTC综合油耗低至4.59L/100km，通勤一月一加油、自驾跨省不补能。TSS 4.0智驾+15.6英寸大屏，新车已经到店了，家用还是挺好的" \
  --verbose
