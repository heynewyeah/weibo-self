# 意图行为项目 - 博文分类服务

> 通过 AI 模型（Qwen3.6-35B-A3B）将微博博文分类到营销层级（认知层/兴趣层/考虑层），支持纯文本、图文、视频博文。

## 项目结构

```
intent_behavior/
├── config/
│   └── config.yaml              # 配置文件（API地址、模型路径、提示词、媒体参数）
├── src/
│   ├── __init__.py
│   ├── api_client.py             # vLLM API 客户端（文本/多模态请求）
│   ├── classifier.py             # 核心分类器（类型判断→路由→分类→记录）
│   ├── data_extractor.py         # 数据提取器（Hive/本地TSV/HDFS）
│   ├── media_handler.py          # 媒体处理器（图片pid→URL→下载 / 视频预留）
│   ├── models.py                 # 数据模型（BlogItem / ClassifyResult）
│   └── utils.py                  # 工具函数（日志、校验、标签提取、结果记录）
├── tests/
│   ├── 01_prepare_data/          # 测试数据准备
│   │   ├── generate_test_data.py # 生成三种类型测试数据，写本地+上传HDFS
│   │   └── fixtures/             # 本地测试数据（自动生成）
│   │       ├── text_samples.jsonl
│   │       ├── image_samples.jsonl
│   │       ├── video_samples.jsonl
│   │       └── all_samples.jsonl
│   ├── 02_single/                # 单条分类测试
│   │   ├── test_single_text.py   # 纯文字博文单条测试
│   │   ├── test_single_image.py  # 图文博文单条测试
│   │   ├── test_single_video.py  # 视频博文单条测试
│   │   └── output/               # 测试结果输出（自动创建）
│   ├── 03_batch/                 # 批量分类测试
│   │   ├── test_batch.py         # 批量测试（含进度/统计/耗时）
│   │   └── output/               # 测试结果输出（自动创建）
│   ├── 04_consistency/           # 一致性测试（硬要求验证）
│   │   ├── test_consistency.py   # 同一条博文重复N次，验证结果一致
│   │   └── output/               # 测试结果输出（自动创建）
│   ├── 05_debug/                 # 调试与环境检查
│   │   └── check_environment.sh  # 环境检查脚本
│   ├── 06_parallel_text/         # 并行批量文本分类测试
│   │   ├── test_parallel_text.py # 从 HDFS 批量读取文本并并发分类
│   │   └── output/               # 测试结果输出（自动创建）
│   ├── 06_parallel_image/        # 并行批量图文分类测试
│   │   ├── test_parallel_image.py # 从 HDFS 批量读取图文并并发分类
│   │   └── output/               # 测试结果输出（自动创建）
│   ├── 06_parallel_video/        # 并行批量视频分类测试
│   │   ├── test_parallel_video.py # 从 HDFS 批量读取视频并并发分类
│   │   └── output/               # 测试结果输出（自动创建）
│   └── run_all_tests.py          # 一键运行所有测试
├── docs/
│   └── upstream_data_spec.md     # 上游数据输入规范（JSONL格式定义）
├── logs/                         # 日志目录（自动创建）
├── output/                       # 生产结果输出（自动创建）
├── main.py                       # 主入口（single/batch/pipeline/server四种模式）
├── requirements.txt              # Python 依赖
└── README.md                     # 本文件
```

---

## 两个硬要求

1. **同一条博文/图/视频必须给出一致的分类结果** → 通过 `temperature=0.0` + `enable_thinking=false` 保障，用 `tests/04_consistency/` 验证
2. **thinking 模式必须关闭** → `config.yaml` 中 `api.enable_thinking: false`，避免输出被 max_tokens 截断

---

## 快速开始

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
# 纯文字博文
python3 tests/02_single/test_single_text.py --verbose

# 图文博文
python3 tests/02_single/test_single_image.py --verbose

# 视频博文
python3 tests/02_single/test_single_video.py --verbose
```

### 4. 批量测试

```bash
# 使用 fixtures 数据（全部样本）
python3 tests/03_batch/test_batch.py

# 指定 JSONL 输入文件（推荐格式）
python3 tests/03_batch/test_batch.py --input /path/to/data.jsonl

# 限制条数（调试用）
python3 tests/03_batch/test_batch.py --limit 5
```

### 5. 一致性测试（验证硬要求）

```bash
# 每种类型各取1条，重复5次
python3 tests/04_consistency/test_consistency.py

# 重复10次，更严格验证
python3 tests/04_consistency/test_consistency.py --repeat 10

# 只测试文本类型
python3 tests/04_consistency/test_consistency.py --type text
```

### 6. 并行批量文本测试（从 HDFS）
```bash
# 从 HDFS 读取 100 条文本博文，10 并发分类
python3 tests/06_parallel_text/test_parallel_text.py \
  --input-hdfs /dw_ext/ad/person/xuanyu11/intent_behavior/data/text_weibo_ad_20260701_20260701 \
  --workers 10 --limit 100
  --workers 10 --limit 100

# 本地 JSONL 测试
python3 tests/06_parallel_text/test_parallel_text.py \
  --input tests/01_prepare_data/fixtures/text_samples.jsonl \
  --workers 10 --limit 50
```

### 7. 并行批量图文测试（从 HDFS）

```bash
# 从 HDFS 读取 100 条图文博文，10 并发分类
python3 tests/06_parallel_image/test_parallel_image.py \
  --input-hdfs /dw_ext/ad/person/xuanyu11/intent_behavior/data/image_weibo_ad_20260701_20260701 \
  --workers 10 --limit 100

# 本地 JSONL 测试
python3 tests/06_parallel_image/test_parallel_image.py \
  --input tests/01_prepare_data/fixtures/image_samples.jsonl \
  --workers 10 --limit 20
```

### 8. 并行批量视频测试（从 HDFS）

```bash
# 从 HDFS 读取 20 条视频博文，10 并发 cover 模式分类
python3 tests/06_parallel_video/test_parallel_video.py \
  --input-hdfs /dw_ext/ad/person/xuanyu11/intent_behavior/data/video_weibo_ad_20260701_20260701/000000_0 \
  --workers 10 --limit 20

# frame 模式（下载视频 + OpenCV 抽帧，并发数建议降低）
python3 tests/06_parallel_video/test_parallel_video.py \
  --input-hdfs /dw_ext/ad/person/xuanyu11/intent_behavior/data/video_weibo_ad_20260701_20260701/000000_0 \
  --video-mode frame --workers 3 --limit 10

# 本地 JSONL 测试
python3 tests/06_parallel_video/test_parallel_video.py \
  --input tests/01_prepare_data/fixtures/video_samples.jsonl \
  --workers 10 --limit 10
```

---

## main.py 使用方式
```bash
# 单条文本分类
python3 main.py --mode single \
  --mid "5250218712893321" \
  --uid "1647951825" \
  --content "#14万级全新威兰达到店# ..." \
  --verbose

# 单条图文分类
python3 main.py --mode single \
  --mid "5250292767523625" \
  --uid "1647951825" \
  --content "比亚迪宋L实拍" \
  --pids "6239bfd1ly1glk3gl3bqfj20gg08843c"

# 批量处理（JSONL，推荐）
python3 main.py --mode batch --input data.jsonl

# 批量处理（TSV，兼容旧版）
python3 main.py --mode batch --input data.tsv

# 完整链路（从 Hive/HDFS 提取 + 分类）
python3 main.py --mode pipeline --config config/config.yaml
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

### TSV 格式（兼容旧版）

```
mid	uid	content	[pids逗号分隔]	[media_ids逗号分隔]
```

---

## 输出说明

### 结果文件 `output/result.tsv`

```
mid	uid	layer	media_type	confidence
5250218712893321	1647951825	考虑层	text	
```

### 错误记录 `logs/error_records.tsv`

```
mid	uid	error_type	error_detail
5250218712893321	1647951825	text	标签提取失败 | output=...
```

### 日志文件 `logs/classify_YYYYMMDD.log`

完整运行日志，含进度、警告、错误。

---

## 配置说明

所有参数集中在 `config/config.yaml`，修改配置不需要改代码：

| 配置段 | 说明 |
|--------|------|
| `api` | vLLM服务地址、模型路径、超时、重试、**enable_thinking（必须false）** |
| `classification` | 行业、分类层级、兜底策略 |
| `prompts` | 系统提示词、各类博文用户提示词模板 |
| `media.image` | 图片pid转URL规则、下载超时、最大图片数 |
| `media.video` | 视频showBatch API、抽帧参数（当前disabled） |
| `logging` | 日志级别、目录、错误文件、结果文件 |
| `batch` | 批处理大小、间隔、并发数 |
| `extractor` | 数据源类型（hive/local/hdfs）及各源配置 |

---

## 数据路径

## 数据路径

| 路径 | 说明 |
|------|------|
| `/dw_ext/ad/person/xuanyu11/intent_behavior/data/` | HDFS 数据目录 |
| `/dw_ext/ad/person/xuanyu11/intent_behavior/data/test_samples/` | HDFS 测试数据 |
| `/dw_ext/ad/person/xuanyu11/intent_behavior/output/` | HDFS 结果目录 |
| `tests/01_prepare_data/fixtures/` | 本地测试数据 |

---

1. 在 `config.yaml` 中设置 `media.video.enabled: true`
2. 在 `src/media_handler.py` 的 `VideoHandler` 中实现：
   - `download_video()` — 视频下载
   - `extract_frames()` — ffmpeg/cv2 抽帧
3. `classifier.py` 中的 `classify_video()` 已预留完整流程，无需改动

---

## 后续扩展为 API 服务

`main.py` 已预留 `--mode server` 入口，后续可用 FastAPI 实现：

```python
@app.post("/api/v1/classify")
async def classify(items: List[ClassifyRequest]):
    results = classifier.classify_batch(items)
    return [{"mid": r.mid, "layer": r.layer, "success": r.success} for r in results]
```
