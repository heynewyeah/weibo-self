# 意图行为项目 - 多行业博文分类服务

> 基于 AI 模型对微博博文进行营销分层分类，当前支持 **汽车** 与 **奶茶** 两个行业，支持纯文本、图文、视频、转发博文场景；生产环境通过 `super_mid_task -> nature_ad_super_mid_x` 驱动消费，分类结果通过 HTTP 接口回写。

## 1. 当前能力

### 已支持行业

- **汽车**
  - `认知层` -> `1`
  - `兴趣层` -> `2`
  - `考虑层` -> `3`
  - `其他` -> `6`

- **奶茶**
  - `品牌与社交类` -> `1`
  - `口碑体验类` -> `2`
  - `消费决策类` -> `3`
  - `其他` -> `6`

### 待处理状态

- `level = 0` 表示待处理，不是分类结果。

### 转发博文处理

- 当前记录中的 [`mid`](src/models.py:42) 是**转发博文 mid**。
- [`forward_mid`](src/models.py:48) 是**被转发的原博文 mid**。
- 当 [`forward_mid`](src/models.py:48) 为 `0` / 空时，按原博文逻辑处理。
- 当 [`forward_mid`](src/models.py:48) 非 `0` 时，先做“转发异常判断”：
  - 非理智
  - 拉踩
  - 抹黑
  - 与原博文信息明显不一致
- 若判定为异常，输出 `其他=6`。
- 若转发关系解析失败或模型输出无法解析，输出**失败态**。

---

## 2. 项目结构

```text
intent_behavior/
├── config/
│   └── config.yaml
├── src/
│   ├── api_client.py
│   ├── classifier.py
│   ├── db_client.py
│   ├── media_handler.py
│   ├── mid_resolver.py
│   ├── models.py
│   ├── pipeline.py
│   ├── result_writer.py
│   ├── utils.py
│   └── worker.py
├── tests/
│   ├── 07_mysql_worker/
│   ├── 08_mid_resolver/
│   ├── 09_edge_cases/
│   └── 10_multi_industry/
├── output/
├── logs/
├── run_classification.py
└── README.md
```

---

## 3. 核心链路

### 任务驱动模式

1. 查询 [`super_mid_task`](src/db_client.py:46)
2. 解析 [`industry_tag`](src/db_client.py:46) / [`brand_tag`](src/db_client.py:46) 的 JSON value
3. 根据 `customer_id % 20` 路由到 `nature_ad_super_mid_x`
4. 读取 `level = 0` 的分表记录
5. 结合 [`forward_mid`](src/db_client.py:67) / [`forward_text`](src/db_client.py:67) 判断是否转发
6. 调用 [`mid_resolver`](src/mid_resolver.py:103) 反解真实内容
7. 按行业 prompt 调用 [`classifier`](src/classifier.py:30) 分类
8. 将层级映射为数值并通过 [`result_writer`](src/result_writer.py:15) 回写

### 当前任务筛选条件

[`fetch_active_tasks()`](src/db_client.py:172) 当前条件：

- `task_type = 1`
- 且满足：
  - `exec_status != 5`
  - 或 `exec_status = 5 and end_time < now() - 1 day`

---

## 4. 配置说明

核心配置位于 [`config/config.yaml`](config/config.yaml)。

### 分类配置

- [`classification.supported_industries`](config/config.yaml)
- [`classification.industry_rules`](config/config.yaml)
- [`classification.other_label`](config/config.yaml)
- [`classification.pending_level`](config/config.yaml)

### Prompt 配置

- [`prompts.industries.汽车`](config/config.yaml)
- [`prompts.industries.奶茶`](config/config.yaml)
- [`prompts.forward_review_prompt`](config/config.yaml)

### MySQL 字段配置

- [`mysql.task_industry_tag_field`](config/config.yaml)
- [`mysql.task_brand_tag_field`](config/config.yaml)
- [`mysql.task_end_time_field`](config/config.yaml)
- [`mysql.shard_forward_mid_field`](config/config.yaml)
- [`mysql.shard_forward_text_field`](config/config.yaml)

---

## 5. 生产入口

推荐使用 [`run_classification.py`](run_classification.py)。

### 5.1 从任务表驱动执行

```bash
python3 run_classification.py \
  --from-tasks \
  --limit 100 \
  --mode auto \
  --write-back
```

### 5.2 直接从分表读取执行

```bash
python3 run_classification.py \
  --shard-index 1 \
  --customer-id 2608812381 \
  --limit 100 \
  --mode auto \
  --write-back
```

### 5.3 单条调试

```bash
python3 run_classification.py --mid 5239345868702306 --uid 7008866503
```

---

## 6. 统一测试脚本

为减少脚本分散，建议统一使用 [`tests/10_multi_industry/test_multi_industry.py`](tests/10_multi_industry/test_multi_industry.py) 通过参数切换不同测试目标。

### 6.1 预览任务

```bash
python3 tests/10_multi_industry/test_multi_industry.py \
  --mode preview-tasks \
  --limit 10
```

### 6.2 预览分表记录

```bash
python3 tests/10_multi_industry/test_multi_industry.py \
  --mode preview-records \
  --shard-index 1 \
  --customer-id 2608812381 \
  --limit 10
```

### 6.3 单条分类（从分表取）

```bash
python3 tests/10_multi_industry/test_multi_industry.py \
  --mode classify \
  --shard-index 1 \
  --customer-id 2608812381 \
  --mid 5239377989207686
```

### 6.4 任务驱动批量分类

```bash
python3 tests/10_multi_industry/test_multi_industry.py \
  --mode batch-from-tasks \
  --limit 20
```

### 6.5 分表批量分类并回写

```bash
python3 tests/10_multi_industry/test_multi_industry.py \
  --mode batch-from-shard \
  --shard-index 1 \
  --customer-id 2608812381 \
  --limit 20 \
  --write-back
```

### 6.6 专测转发场景

```bash
python3 tests/10_multi_industry/test_multi_industry.py \
  --mode forward-check \
  --shard-index 1 \
  --customer-id 2608812381 \
  --limit 20
```

### 6.7 专测某行业

```bash
python3 tests/10_multi_industry/test_multi_industry.py \
  --mode batch-from-tasks \
  --industry 汽车 \
  --limit 20

python3 tests/10_multi_industry/test_multi_industry.py \
  --mode batch-from-tasks \
  --industry 奶茶 \
  --limit 20
```

---

## 7. 输出文件

### 生产输出

- `output/run_classification_<timestamp>.json`
- `output/run_classification_<timestamp>_summary.txt`
- `logs/YYYYMMDD_error.log`

### 测试输出

推荐统一输出到：
- `tests/10_multi_industry/output/*.json`
- `tests/10_multi_industry/output/*.tsv`
- `tests/10_multi_industry/output/*_summary.txt`

---

## 8. 注意事项

1. [`industry_tag`](src/db_client.py:172) 当前按你的口径只会有一个有效 value，但代码已兼容 JSON map。
2. [`brand_tag`](src/db_client.py:172) 只取 JSON 的 value 列表，不使用 key。
3. `other=6` 是**正常业务分类结果**，不是失败态。
4. 转发异常判定失败、反解失败、模型输出无法解析，才进入失败态。
5. `workers > 1` 时仍不建议开启 `--write-back`。

---

## 9. 后续建议

下一步建议优先补齐：
- 统一多行业测试脚本 [`tests/10_multi_industry/test_multi_industry.py`](tests/10_multi_industry/test_multi_industry.py)
- 对 [`run_classification.py`](run_classification.py) 和 [`src/worker.py`](src/worker.py) 做多行业结果摘要增强
- 更新 [`tests/09_edge_cases/test_edge_cases.py`](tests/09_edge_cases/test_edge_cases.py) 以支持汽车/奶茶/转发异常场景
