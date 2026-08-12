#!/bin/bash
# test_batch.sh — 批量分类测试
# 用法: bash tests/test_batch.sh
# 输入文件格式: mid \t uid \t content \t [pids] \t [media_ids]

cd "$(dirname "$0")/.."

# 生成测试数据（含纯文本 + 图文）
cat > /tmp/test_batch_input.tsv << 'EOF'
5250218712893321	uid001	#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里，第五代智能电混双擎加持，WLTC综合油耗低至4.59L/100km
5249978236931005	uid002	【行业首次！-30℃冰面80km/h爆胎测试，竟是吉利高管亲自驾车？】在零下30度的极寒冰面上，一辆吉利银河M9以80km/h的速度疾驰过雪地弯道
5250292767523625	uid003	26年开始新能源购置税减免缩水1.5万，让新能源车购车成本波动，不少人开始持币观望。沃尔沃的出现打破了这一僵局
test_img_001	uid004	比亚迪宋L实拍，外观真的绝了	006mX07Rly8ifv3xs5535j30ud0plk1m
EOF

python3 main.py \
  --mode batch \
  --input /tmp/test_batch_input.tsv
