# 意图行为项目 - 博文营销分层分类服务

> **最后更新**: 2026-09-03  
> **项目状态**: 生产可用，持续优化中  
> **适用场景**: 微博博文营销分层分类（汽车/奶茶行业）

---

## 一、项目概述

### 1.1 项目目标

通过 AI 模型（Qwen3.6-35B-A3B）对微博博文进行营销分层分类，为"原生内容站"平台提供内容分类能力。

**核心需求**：
- 支持多行业分类（当前：汽车、奶茶）
- 支持纯文本、图文、视频、转发博文场景
- 生产环境通过 MySQL 任务表驱动消费
- 分类结果通过 HTTP 接口回写

### 1.2 两个硬要求

1. **一致性**：同一条博文/图/视频必须给出一致的分类结果（通过 `temperature=0.0` + `seed=42` 保证）
2. **关闭 thinking 模式**：避免输出过长被 `max_tokens` 截断（必须设置 `enable_thinking=false`）

### 1.3 分类层级定义

#### 汽车行业

| 层级 | level | 定义 |
|------|-------|------|
| 认知层 | 1 | 品牌曝光、高传播高热度，让用户认知品牌 |
| 兴趣层 | 2 | 引发讨论、互动，提升用户对产品的兴趣 |
| 考虑层 | 3 | 产品测评、价格优惠、参数分析，帮助用户完成决策 |
| 其他 | 6 | 不符合以上三类目标的内容 |

#### 奶茶行业

| 层级 | level | 定义 |
|------|-------|------|
| 品牌与社交类 | 1 | 传播动力——明星效应、热度事件、情绪认同、联名话题 |
| 口碑体验类 | 2 | 消费后的真实感受和场景关联 |
| 消费决策类 | 3 | 产品吸引力和购买动机，离转化最近 |
| 其他 | 6 | 不符合以上三类目标的内容 |

**特殊状态**：
- `level = 0`：待处理（不是分类结果）
- 非支持行业（如数码）：统一归为 `level=6`（其他）

---

## 二、项目进度

### 2.1 已完成功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 汽车行业分类 | ✅ 完成 | 认知层/兴趣层/考虑层/其他 |
| 奶茶行业分类 | ✅ 完成 | 品牌与社交类/口碑体验类/消费决策类/其他 |
| 多行业支持 | ✅ 完成 | 配置化扩展，新增行业只需修改配置文件 |
| 纯文本分类 | ✅ 完成 | 直接调用模型 |
| 图文分类 | ✅ 完成 | pid → URL → 下载 → base64 → 多模态分类 |
| 视频分类 | ✅ 完成 | 优先抽帧（frame），失败降级封面图（cover） |
| 转发博文处理 | ✅ 完成 | 转发异常判断 → 异常归为 level=6，正常则走分类 |
| mid 反解 | ✅ 完成 | 调用微博内部接口获取真实 content/pid/fid |
| MySQL 任务驱动 | ✅ 完成 | super_mid_task → nature_ad_super_mid_x 路由 |
| HTTP 结果回写 | ✅ 完成 | POST /api/v1/super-mid/update-level |
| 行级锁 | ✅ 完成 | SELECT ... FOR UPDATE，防止多 worker 重复处理 |
| 日志轮转 | ✅ 完成 | RotatingFileHandler，单文件 10MB，保留 5 个历史 |
| 反解失败汇总 | ✅ 完成 | 单文件追加模式，含表头和重试次数统计 |
| 视频时长统计 | ✅ 完成 | 独立脚本，统计视频博文时长分布 |

### 2.2 待优化项

| 项目 | 优先级 | 说明 |
|------|--------|------|
| 回写接口超时问题 | 🔴 高 | 需与接口提供方（王燕威）确认服务状态 |
| 失败状态是否回写 | 🟡 中 | 需与产品确认是否回写 level 表示失败 |
| 多行业测试覆盖 | 🟡 中 | 需补充奶茶行业的完整测试用例 |
| 并发回写支持 | 🟢 低 | 当前 workers>1 时不支持回写，后续可优化 |

---

## 三、踩坑记录

### 3.1 thinking 模式导致输出截断

**问题**：模型输出包含思考过程，超出 `max_tokens` 限制导致输出被截断，无法提取有效标签。

**解决**：
```yaml
api:
  thinking:
    type: "disabled"
  reasoning:
    effort: "none"
  enable_thinking: false
```

**注意**：必须同时设置三个参数，缺一不可。

### 3.2 行业判断 fallback 逻辑错误

**问题**：非支持行业（如数码）被错误 fallback 到汽车，导致分类结果错误。

**解决**：
```python
def resolve_industry(self, industry_values: List[str]) -> str:
    for value in industry_values:
        if value in self.supported_industries:
            return value
    # 有行业标签但都不支持 → 返回原始值，后续归为 level=6
    if industry_values:
        return industry_values[0]
    # 无行业标签 → 返回空字符串
    return ""
```

**关键**：非支持行业不应 fallback 到默认行业，而应返回原始值，后续在分类阶段统一归为 level=6。

### 3.3 worker 不走反解链路

**问题**：worker 直接调用分类器，跳过了 mid 反解，导致使用 MySQL 中的旧数据。

**解决**：worker 改为调用 `ClassifyPipeline.process_one()`，走完整链路：
```
mid 反解 → 转发判断 → 分类 → 临时文件清理 → HTTP 回写
```

### 3.4 MySQL 分表无 mid_fids 字段

**问题**：MySQL 分表中没有 `mid_fids` 字段，无法直接筛选视频类博文。

**解决**：从 MySQL 读取所有 mid，通过 mid 反解接口获取 `video.fid`，自动判断是否为视频博文。

### 3.5 SKIP LOCKED 语法不支持

**问题**：MySQL 5.7 不支持 `SELECT ... FOR UPDATE SKIP LOCKED`（MySQL 8.0+ 特性）。

**解决**：改为 `SELECT ... FOR UPDATE`，虽然会阻塞其他 worker，但保证不会重复处理。

### 3.6 回写接口超时

**问题**：回写接口（王燕威服务）频繁超时，3 次重试全部失败。

**排查**：
1. 测试网络连通性：`curl -v http://10.133.6.162:8058/api/v1/super-mid/update-level`
2. 使用独立测试脚本：`python3 tests/test_result_writer.py --timeout 60`
3. 联系接口提供方确认服务状态

---

## 四、项目结构

```
intent_behavior/
├── config/
│   └── config.yaml                    # 核心配置文件（API、分类规则、MySQL、回写接口）
├── src/
│   ├── __init__.py
│   ├── api_client.py                  # vLLM API 客户端（文本+多模态请求，重试机制）
│   ├── classifier.py                  # 核心分类器（行业感知、转发判断、自动路由）
│   ├── data_extractor.py              # 数据提取器（Hive/本地/HDFS）
│   ├── db_client.py                   # MySQL 任务仓储（任务查询、分表路由、行级锁）
│   ├── media_handler.py               # 媒体处理器（图片下载、视频抽帧/封面）
│   ├── mid_resolver.py                # mid 反解客户端（获取真实 content/pid/fid）
│   ├── models.py                      # 数据模型（BlogItem、ClassifyResult、MidRecord）
│   ├── pipeline.py                    # 正式分类 Pipeline（完整链路、计时、错误记录）
│   ├── result_writer.py               # HTTP 结果回写客户端（POST 接口）
│   ├── utils.py                       # 工具函数（日志、校验、标签提取）
│   └── worker.py                      # MySQL 分表持续消费 worker
├── tests/
│   ├── 01_prepare_data/               # 测试数据生成（从 Hive 提取 3 类博文）
│   │   ├── generate_test_data.py
│   │   └── tmp_fixtures/              # 生成的测试数据（JSONL/TSV）
│   ├── 02_single/                     # 单条分类测试（文本/图片/视频）
│   │   ├── test_single_text.py
│   │   ├── test_single_image.py
│   │   └── test_single_video.py
│   ├── 03_batch/                      # 批量分类测试
│   │   └── test_batch.py
│   ├── 04_consistency/                # 一致性测试（同一条多次调用）
│   │   └── test_consistency.py
│   ├── 05_debug/                      # 调试工具
│   │   └── check_environment.sh
│   ├── 06_parallel_text/              # 并行批量文本测试
│   ├── 06_parallel_image/             # 并行批量图文测试
│   ├── 06_parallel_video/             # 并行批量视频测试
│   ├── 07_mysql_worker/               # MySQL 分表联调测试
│   │   ├── test_super_mid_task.py     # 任务表查询测试
│   │   ├── test_nature_ad_super_mid.py # 分表查询测试
│   │   ├── test_parallel_mysql_level_zero.py # 并行处理测试
│   │   └── test_worker_once.py        # worker 单轮测试
│   ├── 08_mid_resolver/               # mid 反解测试
│   │   ├── test_media_resolve.py      # 反解接口测试
│   │   ├── test_resolve_and_classify_image.py
│   │   └── test_resolve_and_classify_video.py
│   ├── 09_edge_cases/                 # 边界用例测试
│   │   └── test_edge_cases.py         # 超短内容、纯表情、纯话题等
│   ├── 10_multi_industry/             # 多行业统一测试
│   │   └── test_multi_industry.py     # 汽车/奶茶行业测试
│   ├── run_all_tests.py               # 单元测试套件一键运行
│   ├── run_e2e_pipeline.py            # 端到端流水线（持续轮询 MySQL）
│   ├── test_result_writer.py          # 回写接口单元测试
│   └── test_video_duration.py         # 视频时长统计脚本
├── sql/
│   ├── query_task-查询有效任务.sh      # 查询 super_mid_task 任务表
│   ├── query_detail-明细表查询.sql     # 查询 nature_ad_super_mid_x 分表
│   ├── query_text.sh                  # 查询文本类博文
│   ├── query_image.sh                 # 查询图片类博文
│   └── query_video.sh                 # 查询视频类博文
├── docs/
│   ├── qwen_model_usage_guide.md      # Qwen3.6-35B-A3B 模型使用说明
│   ├── mysql_worker_spec.md           # MySQL worker 规格说明
│   └── upstream_data_spec.md          # 上游数据规范
├── logs/                              # 日志目录（自动轮转）
│   ├── classify.log                   # 主日志（RotatingFileHandler）
│   ├── classify.log.1                 # 历史日志
│   ├── YYYYMMDD_error.log             # 每日错误日志
│   └── 反解失败汇总.txt                # 反解失败汇总（单文件追加）
├── output/                            # 输出目录
│   ├── result.tsv                     # 分类结果（TSV 格式）
│   └── run_classification_*.json      # 运行结果（JSON 格式）
├── run_classification.py              # 生产入口（推荐）
├── worker.py                          # worker 入口（持续轮询）
├── main.py                            # 旧入口（single/batch/server）
├── requirements.txt                   # Python 依赖
├── README.md                          # 本文档
└── PROJECT_HANDOVER.md                # 完整交接文档（历史版本）
```

---

## 五、核心链路

### 5.1 任务驱动模式（生产环境）

```
super_mid_task (任务表)
    ↓
解析 industry_tag / brand_tag (JSON)
    ↓
根据 customer_id % 20 路由到 nature_ad_super_mid_x (分表)
    ↓
读取 level=0 的记录
    ↓
mid 反解 → 获取真实 content/pid/fid
    ↓
转发判断 → 异常归为 level=6，正常继续
    ↓
行业判断 → 非支持行业归为 level=6，支持行业继续
    ↓
分类 → 调用 Qwen3.6-35B-A3B 模型
    ↓
HTTP 回写 → POST /api/v1/super-mid/update-level
```

### 5.2 任务筛选条件

```sql
SELECT * FROM super_mid_task
WHERE task_type = 1
  AND (
    exec_status != 5
    OR (exec_status = 5 AND end_time > DATE_SUB(NOW(), INTERVAL 1 DAY))
  )
```

**含义**：
- `task_type = 1`：有效任务类型
- `exec_status != 5`：未完成的任务
- `exec_status = 5 AND end_time > now() - 1 day`：最近 1 天内完成的任务（防止重复处理）

### 5.3 转发博文处理逻辑

```
forward_mid == 0 或空 → 按原博文处理
forward_mid != 0 → 转发异常判断
    ↓
模型判断：非理智/拉踩/抹黑/不一致
    ↓
异常 → level=6（其他）
正常 → 继续分类
```

---

## 六、配置说明

### 6.1 核心配置（config/config.yaml）

```yaml
# vLLM API 配置
api:
  url: "http://10.1.126.27:8087/v1/chat/completions"
  model: "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B"
  max_tokens: 512
  temperature: 0.0
  seed: 42
  thinking:
    type: "disabled"
  reasoning:
    effort: "none"
  enable_thinking: false

# 分类配置
classification:
  supported_industries:
    - "汽车"
    - "奶茶"
  default_industry: "汽车"
  pending_level: 0
  other_label: "其他"
  failure_label: "未识别"
  industry_rules:
    汽车:
      layers: ["认知层", "兴趣层", "考虑层", "其他"]
      level_mapping:
        "认知层": 1
        "兴趣层": 2
        "考虑层": 3
        "其他": 6
    奶茶:
      layers: ["品牌与社交类", "口碑体验类", "消费决策类", "其他"]
      level_mapping:
        "品牌与社交类": 1
        "口碑体验类": 2
        "消费决策类": 3
        "其他": 6

# MySQL 配置
mysql:
  host: "10.79.104.30"
  port: 3306
  user: "clue_collect"
  database: "clue_collect_common"
  task_table: "super_mid_task"
  shard_table_prefix: "nature_ad_super_mid_"

# 回写接口配置
result_writer:
  url: "http://10.133.6.162:8058/api/v1/super-mid/update-level"
  timeout: 30
  max_retry: 3
```

### 6.2 新增行业

只需在 `config.yaml` 中添加行业规则：

```yaml
classification:
  supported_industries:
    - "汽车"
    - "奶茶"
    - "新行业"  # 新增
  industry_rules:
    新行业:
      layers: ["层级1", "层级2", "层级3", "其他"]
      level_mapping:
        "层级1": 1
        "层级2": 2
        "层级3": 3
        "其他": 6
      fallback_layer: "其他"
      keyword_map:
        "层级1": ["keyword1", "关键词1"]
        "层级2": ["keyword2", "关键词2"]
        "层级3": ["keyword3", "关键词3"]
        "其他": ["other", "其他"]

prompts:
  industries:
    新行业:
      system_prompt: |
        你是一个新行业博文分类器...
      user_text_template: |
        请对以下新行业博文进行分类...
      user_image_template: |
        请对以下新行业图文博文进行分类...
      user_video_template: |
        请对以下新行业视频博文进行分类...
```

---

## 七、使用方法

### 7.1 生产环境（推荐）

```bash
# 从任务表驱动执行（持续轮询）
python3 run_classification.py --from-tasks --limit 100 --mode auto --write-back

# 直接从分表读取执行
python3 run_classification.py --shard-index 1 --customer-id 2608812381 --limit 100 --mode auto --write-back

# 单条调试
python3 run_classification.py --mid 5239345868702306 --uid 7008866503
```

### 7.2 持续轮询模式

```bash
# 持续轮询 MySQL 任务表，直到没有待处理任务
python3 tests/run_e2e_pipeline.py

# 限制最大轮数
python3 tests/run_e2e_pipeline.py --max-rounds 5

# 限制每轮处理的任务数
python3 tests/run_e2e_pipeline.py --max-tasks-per-round 10
```

### 7.3 测试脚本

```bash
# 单元测试套件
python3 tests/run_all_tests.py

# 回写接口测试
python3 tests/test_result_writer.py --mid 5333296278144730 --customer-id 2608812381 --task-id 1301222511089811457 --level 6

# 视频时长统计
python3 tests/test_video_duration.py --from-mysql --shard-index 1 --limit 20

# 多行业测试
python3 tests/10_multi_industry/test_multi_industry.py --mode batch-from-tasks --industry 汽车 --limit 20
python3 tests/10_multi_industry/test_multi_industry.py --mode batch-from-tasks --industry 奶茶 --limit 20
```

### 7.4 SQL 查询

```bash
# 查询任务表
bash sql/query_task-查询有效任务.sh

# 查询分表
# 直接复制 sql/query_detail-明细表查询.sql 中的 SQL 到 MySQL 客户端执行
```

---

## 八、输出文件

### 8.1 生产输出

| 文件 | 说明 |
|------|------|
| `output/run_classification_<timestamp>.json` | 完整结果（JSON 格式） |
| `output/run_classification_<timestamp>_summary.txt` | 运行摘要 |
| `output/result.tsv` | 分类结果（TSV 格式） |

### 8.2 日志文件

| 文件 | 说明 |
|------|------|
| `logs/classify.log` | 主日志（RotatingFileHandler，单文件 10MB，保留 5 个历史） |
| `logs/classify.log.1` ~ `classify.log.5` | 历史日志 |
| `logs/YYYYMMDD_error.log` | 每日错误日志 |
| `logs/反解失败汇总.txt` | 反解失败汇总（单文件追加，含表头和重试次数） |

### 8.3 测试输出

| 目录 | 说明 |
|------|------|
| `tests/02_single/output/` | 单条测试结果 |
| `tests/03_batch/output/` | 批量测试结果 |
| `tests/04_consistency/output/` | 一致性测试结果 |
| `tests/10_multi_industry/output/` | 多行业测试结果 |
| `tests/output/` | 通用测试输出（视频时长统计等） |

---

## 九、依赖安装

```bash
pip install -r requirements.txt
```

**核心依赖**：
- `requests`：HTTP 请求
- `pymysql`：MySQL 连接
- `pyyaml`：配置文件解析
- `opencv-python-headless`：视频抽帧（可选，仅 frame 模式需要）

---

## 十、关键人员

| 角色 | 姓名 | 职责 |
|------|------|------|
| 产品 | 雷莉 | 需求定义、原生内容站产品设计、优先级决策 |
| 算法/开发 | 金昡宇 | 博文分类模型、脚本开发、数据处理 |
| 接口提供方 | 王燕威 | 回写接口（POST /api/v1/super-mid/update-level） |

---

## 十一、相关文档

| 文档 | 说明 |
|------|------|
| [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) | 完整交接文档（历史版本，2026-08-12） |
| [docs/qwen_model_usage_guide.md](docs/qwen_model_usage_guide.md) | Qwen3.6-35B-A3B 模型使用说明 |
| [docs/mysql_worker_spec.md](docs/mysql_worker_spec.md) | MySQL worker 规格说明 |
| [docs/upstream_data_spec.md](docs/upstream_data_spec.md) | 上游数据规范 |
| [sql/query_detail-明细表查询.sql](sql/query_detail-明细表查询.sql) | MySQL 查询脚本集合 |

---

## 十二、迁移指南

### 12.1 迁移到其他 AI 对话

本文档已包含项目完整信息，可直接迁移到其他 AI 对话。关键信息：

1. **项目结构**：见第四节
2. **核心链路**：见第五节
3. **配置说明**：见第六节
4. **踩坑记录**：见第三节
5. **使用方法**：见第七节

### 12.2 关键文件

| 文件 | 说明 |
|------|------|
| `config/config.yaml` | 核心配置（API、分类规则、MySQL、回写接口） |
| `src/pipeline.py` | 正式分类 Pipeline（完整链路） |
| `src/classifier.py` | 核心分类器（行业感知、转发判断） |
| `src/db_client.py` | MySQL 任务仓储（任务查询、分表路由） |
| `src/mid_resolver.py` | mid 反解客户端 |
| `src/result_writer.py` | HTTP 结果回写客户端 |

### 12.3 注意事项

1. **thinking 模式必须关闭**：否则输出会被截断
2. **非支持行业归为 level=6**：不要 fallback 到默认行业
3. **MySQL 分表无 mid_fids 字段**：需通过 mid 反解获取视频 fid
4. **回写接口可能超时**：需与接口提供方确认服务状态

---

**文档版本**: v2.0  
**最后更新**: 2026-09-03  
**维护者**: 金昡宇
