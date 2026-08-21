# 意图行为项目 - 博文分类服务

> 通过 AI 模型（Qwen3.6-35B-A3B）将微博博文分类到营销层级（认知层/兴趣层/考虑层），支持纯文本、图文、视频博文。生产环境通过 MySQL 分表驱动消费，分类结果通过 HTTP 接口回写。

## 项目结构

```
intent_behavior/
├── config/
│   └── config.yaml              # 配置文件（API、模型、MySQL、HTTP回写、媒体参数）
├── src/
│   ├── __init__.py
│   ├── api_client.py            # vLLM API 客户端（文本/多模态请求）
│   ├── classifier.py            # 核心分类器（媒体类型判断→路由→分类）
│   ├── data_extractor.py        # 数据提取器（Hive/本地TSV/HDFS）
│   ├── db_client.py             # MySQL 分表任务消费与 HTTP 结果回写
│   ├── media_handler.py         # 媒体处理器（图片下载 / 视频封面+抽帧）
│   ├── mid_resolver.py          # 微博 mid 反解客户端（获取真实 content/pid/fid）
│   ├── models.py                # 数据模型（BlogItem / ClassifyResult / MidRecord）
│   ├── pipeline.py              # 正式分类 Pipeline（单条/批量、计时、清理、错误记录）
│   ├── result_writer.py         # HTTP 结果回写客户端（王燕威接口）
│   ├── utils.py                 # 工具函数（日志、校验、标签提取）
│   └── worker.py                # MySQL 分表持续消费 worker
├── tests/
│   ├── 01_prepare_data/         # 测试数据准备
│   ├── 02_single/               # 单条分类测试
│   ├── 03_batch/                # 批量分类测试
│   ├── 04_consistency/          # 一致性测试（硬要求验证）
│   ├── 05_debug/                # 调试与环境检查
│   ├── 06_parallel_text/        # 并行批量文本分类测试
│   ├── 06_parallel_image/       # 并行批量图文分类测试
│   ├── 06_parallel_video/       # 并行批量视频分类测试
│   ├── 07_mysql_worker/         # MySQL 分表消费联调测试
│   └── 08_mid_resolver/         # mid 反解 + 分类联调测试
├── logs/                        # 日志目录（自动创建）
├── output/                      # 生产结果输出（自动创建）
├── main.py                      # 旧主入口（single/batch/pipeline/server）
├── run_classification.py        # 生产环境正式入口（推荐）
├── requirements.txt             # Python 依赖
└── README.md                    # 本文件
```

---

## 两个硬要求

1. **同一条博文/图/视频必须给出一致的分类结果** → 通过 `temperature=0.0` + `enable_thinking=false` + 固定 `seed=42` 保障，用 `tests/04_consistency/` 验证。
2. **thinking 模式必须关闭** → `config.yaml` 中 `api.enable_thinking: false`、`api.thinking.type: disabled`、`api.reasoning.effort: none`，避免输出被 `max_tokens` 截断。

---

## 生产环境主入口（推荐）

生产环境统一使用 [`run_classification.py`](run_classification.py)。

### 1. MySQL 任务驱动模式（完整链路）

自动查询 `super_mid_task` 有效任务 → 按 `customer_id % 20` 路由到 `nature_ad_super_mid_x` → 读取 `level=0` 记录 → mid 反解 → 分类 → HTTP 回写。

```bash
# 查询任务并处理，最多 100 条，自动判断媒体类型，回写结果
python3 run_classification.py \
    --from-tasks \
    --limit 100 \
    --mode auto \
    --write-back

# 限制每个任务最多读取 10 条（用于小批量验证）
python3 run_classification.py \
    --from-tasks \
    --limit 100 \
    --limit-per-task 10 \
    --mode auto \
    --write-back
```

### 2. MySQL 分表直读模式（定向补跑/测试）

不查 `super_mid_task`，直接读取指定分表的 `level=0` 数据。

```bash
python3 run_classification.py \
    --shard-index 1 \
    --customer-id 2608812381 \
    --limit 100 \
    --mode auto \
    --write-back
```

### 3. 单条/文件模式

```bash
# 单条
python3 run_classification.py --mid 5239345868702306 --uid 7008866503

# 批量文件（JSONL/TSV）
python3 run_classification.py --input-file data/input.jsonl --workers 5
```

### 输出

- 终端：每条 mid 的处理结果和耗时
- `logs/YYYYMMDD_error.log`：失败记录
- `output/run_classification_<timestamp>.json`：完整结果
- `output/run_classification_<timestamp>_summary.txt`：摘要

---

## 快速开始（测试链路）

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 生成测试数据

```bash
# 仅写本地（本机调试）
python3 tests/01_prepare_data/generate_test_data.py --local-only

# 写本地 + 上传 HDFS（服务器环境）
python3 tests/01_prepare_data/generate_test_data.py
```

### 3. 单条测试

```bash
python3 tests/02_single/test_single_text.py --verbose
python3 tests/02_single/test_single_image.py --verbose
python3 tests/02_single/test_single_video.py --verbose
```

### 4. 批量测试

```bash
python3 tests/03_batch/test_batch.py --limit 5
```

### 5. 一致性测试

```bash
python3 tests/04_consistency/test_consistency.py --repeat 10
```

### 6. 并行批量测试

```bash
python3 tests/06_parallel_text/test_parallel_text.py \
    --input tests/01_prepare_data/tmp_fixtures/text_samples.jsonl \
    --workers 10 --limit 50

python3 tests/06_parallel_image/test_parallel_image.py \
    --input tests/01_prepare_data/tmp_fixtures/image_samples.jsonl \
    --workers 10 --limit 20

python3 tests/06_parallel_video/test_parallel_video.py \
    --input tests/01_prepare_data/tmp_fixtures/video_samples.jsonl \
    --workers 3 --limit 10
```

### 7. MySQL 分表联调测试

```bash
# 验证 super_mid_task 任务路由
python3 tests/07_mysql_worker/test_super_mid_task.py --limit 10

# 直接读取分表并分类回写
python3 tests/07_mysql_worker/test_nature_ad_super_mid.py \
    --table nature_ad_super_mid_1 --run-classify --write-back --limit 3

# 并发读取分表 level=0 并回写
python3 tests/07_mysql_worker/test_parallel_mysql_level_zero.py \
    --shard-index 1 --run-classify --write-back --limit 100

# worker 单轮执行
python3 tests/07_mysql_worker/test_worker_once.py
```

### 8. mid 反解 + 分类测试

```bash
# 图片反解+分类
python3 tests/08_mid_resolver/test_resolve_and_classify_image.py \
    --mid 5239345868702306 --uid 7008866503

# 视频反解+分类
python3 tests/08_mid_resolver/test_resolve_and_classify_video.py \
    --mid 5239345868702306 --uid 7008866503
```

---

## 核心数据流

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ super_mid_task │ --> │ nature_ad_super_ │ --> │ run_classification │
│  (有效任务)     │     │ mid_{customer_id%20}│     │   / worker        │
└──────────────┘     └──────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                                              ┌─────────────────┐
                                              │  mid 反解接口    │
                                              │  terra.biz...   │
                                              └─────────────────┘
                                                        │
                    ┌─────────────────────────────────────┼─────────────────────────────────────┐
                    ▼                                     ▼                                     ▼
            ┌───────────────┐                    ┌───────────────┐                    ┌───────────────┐
            │     text      │                    │     image     │                    │     video     │
            │  纯文本分类    │                    │  图文分类      │                    │  视频分类      │
            └───────┬───────┘                    └───────┬───────┘                    └───────┬───────┘
                    │                                     │                                     │
                    └─────────────────────────────────────┼─────────────────────────────────────┘
                                                          ▼
                                                ┌─────────────────┐
                                                │  HTTP 结果回写   │
                                                │ /api/v1/super-  │
                                                │ mid/update-level│
                                                └─────────────────┘
```

---

## 输入数据格式

### JSONL 格式（推荐，上游数据规范）

每行一个 JSON 对象，详见 [`docs/upstream_data_spec.md`](docs/upstream_data_spec.md)。

```jsonl
{"mid":"5250218712893321","uid":"1647951825","content":"博文文字...","media_type":"text","media_info":null}
{"mid":"5250292767523625","uid":"1647951825","content":"图文博文...","media_type":"image","media_info":[{"media_type":"1","customer_info":"[\"pid1\",\"pid2\"]"}]}
{"mid":"5250301234567890","uid":"1647951825","content":"视频博文...","media_type":"video","media_info":[{"media_type":"2","customer_info":"{\"cover\":\"http://...\",\"fid\":\"2362904:xxx\"}"}]}
```

### MySQL 分表字段

| 表 | 说明 |
|---|---|
| `super_mid_task` | 任务主表，含 `task_id`、`customer_id`、`task_type`、`exec_status` |
| `nature_ad_super_mid_{customer_id % 20}` | 分表，含 `mid`、`mid_uid`、`mid_text`、`mid_pids`、`mid_fids`、`level`、`super_task_id` |

> 分表中的 `mid_pids`/`mid_fids` 仅作为参考，实际分类时会通过 mid 反解接口获取真实内容、图片 pid、视频 fid/cover。

---

## 输出说明

### 生产结果

运行 `run_classification.py` 后生成：

- `output/run_classification_<timestamp>.json`：完整结果，含每条 mid 的分类、耗时、媒体类型、错误信息。
- `output/run_classification_<timestamp>_summary.txt`：摘要，含成功率、分布、P95 耗时等。
- `logs/YYYYMMDD_error.log`：失败记录，格式为 `时间\tmid\tuid\tmode\terror_stage\terror\tpic_ids\tvideo_fid\tcover\tcontent_preview`。

### 旧结果文件

- `output/result.tsv`：`main.py` 旧入口输出，当前生产环境不再使用。

---

## 配置说明

所有参数集中在 `config/config.yaml`，修改配置不需要改代码：

| 配置段 | 说明 |
|--------|------|
| `api` | vLLM 服务地址、模型路径、超时、重试、**enable_thinking（必须 false）** |
| `classification` | 行业、分类层级、兜底策略 |
| `prompts` | 系统提示词、各类博文用户提示词模板 |
| `media.image` | 图片 pid 转 URL 规则、下载超时、最大图片数 |
| `media.video` | 视频 showBatch API、cover/frame 模式、抽帧参数 |
| `mysql` | MySQL 连接、任务表/分表前缀、任务匹配字段 |
| `worker` | 轮询间隔、每任务读取条数、最大轮询次数 |
| `mid_resolver` | 微博 mid 反解接口地址、超时、重试 |
| `result_writer` | HTTP 结果回写接口地址、超时、重试（王燕威接口） |
| `logging` | 日志级别、目录 |
| `batch` | 批处理大小、间隔、并发数 |
| `extractor` | 数据源类型（hive/local/hdfs）及各源配置 |

---

## 数据路径

| 路径 | 说明 |
|------|------|
| `/dw_ext/ad/person/xuanyu11/intent_behavior/data/` | HDFS 数据目录 |
| `/dw_ext/ad/person/xuanyu11/intent_behavior/data/test_samples/` | HDFS 测试数据 |
| `/dw_ext/ad/person/xuanyu11/intent_behavior/output/` | HDFS 结果目录 |
| `tests/01_prepare_data/tmp_fixtures/` | 本地测试数据 |

---

## 注意事项

1. **HTTP 回写依赖 `result_writer.url`**：如果未配置，`--write-back` 会报错。
2. **MySQL 模式下必须传入完整 `app_config`**：`run_classification.py` 已统一处理，自定义脚本创建 `MySQLTaskRepository` 时请使用 `MySQLTaskRepository(mysql_cfg, logger, app_config=config)`。
3. **并发与回写互斥**：`run_classification.py` 中 `workers > 1` 时不支持 `--write-back`。
4. **视频 frame 模式需要 opencv-python-headless**。

---

## 后续扩展

- 将 `run_classification.py` 的 `--from-tasks` 模式扩展为持续轮询服务，可参考 `src/worker.py` 的 `run_forever()`。
- `main.py` 的 `--mode server` 可作为 FastAPI 服务雏形，但当前生产推荐以 MySQL 驱动消费为主。
