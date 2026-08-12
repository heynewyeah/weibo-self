#!/bin/bash
# test_pipeline.sh — 完整链路测试（提取 + 分类）
# 用法: bash tests/test_pipeline.sh
# 说明：根据 config/config.yaml 中 extractor 配置自动提取并分类

cd "$(dirname "$0")/.."

# 准备本地测试数据
cat > data/sample.tsv << 'EOF'
mid001	uid001	#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里
mid002	uid002	比亚迪宋L实拍，外观真的绝了，灯组设计很有未来感	006mX07Rly8ifv3xs5535j30ud0plk1m
EOF

python3 main.py \
  --mode pipeline \
  --config config/config.yaml
