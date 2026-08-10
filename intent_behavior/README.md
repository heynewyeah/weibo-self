# 意图行为项目 - 博文分类服务

> 通过 AI 模型将博文分类到营销层级（认知层/兴趣层/考虑层），支持纯文本、图文、视频博文。

## 项目结构

```
intent_behavior/
├── config/
│   └── config.yaml          # 配置文件（API地址、模型路径、提示词、媒体参数）
├── src/
│   ├── __init__.py
│   ├── api_client.py         # vLLM API 客户端（文本/多模态请求）
│   ├── media_handler.py      # 媒体处理器（图片pid→URL→下载 / 视频预留）
│   ├── classifier.py         # 核心分类器（类型判断→路由→分类→记录）
│   └── utils.py              # 工具函数（日志、校验、标签提取、结果记录）
├── tests/
│   ├── test_text.sh          # 纯文本分类测试
│   ├── test_image.sh         # 图文分类测试
│   └── test_batch.sh         # 批量分类测试
├── logs/                     # 日志目录（自动创建）
├── output/                   # 结果输出（自动创建）
├── main.py                   # 主入口（CLI / 批量 / API服务预留）
├── requirements.txt          # Python 依赖
└── README.md                 # 本文件
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 单条文本分类测试

```bash
python3 main.py \
  --mode single \
  --mid "5250218712893321" \
  --uid "test_uid" \
  --content "#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里..." \
  --verbose
```

### 3. 单条图文分类测试

```bash
python3 main.py \
  --mode single \
  --mid "test_001" \
  --uid "test_uid" \
  --content "比亚迪宋L实拍，外观绝了" \
  --pids "006mX07Rly8ifv3xs5535j30ud0plk1m" \
  --verbose
```

### 4. 批量处理

输入TSV格式：`mid \t uid \t content \t [pids逗号分隔] \t [media_ids逗号分隔]`

```bash
python3 main.py --mode batch --input data.tsv
```

### 5. 一键测试

```bash
bash tests/test_text.sh    # 文本测试
bash tests/test_image.sh   # 图片测试
bash tests/test_batch.sh   # 批量测试
```

## 配置说明

所有参数集中在 `config/config.yaml`，修改配置不需要改代码：

| 配置段 | 说明 |
|--------|------|
| `api` | vLLM服务地址、模型路径、超时、重试 |
| `classification` | 行业、分类层级、兜底策略 |
| `prompts` | 系统提示词、各类博文用户提示词模板 |
| `media.image` | 图片pid转URL规则、下载超时、最大图片数 |
| `media.video` | 视频hive表、showBatch API、抽帧参数（当前disabled） |
| `logging` | 日志级别、目录、错误文件、结果文件 |
| `batch` | 批处理大小、间隔、并发数 |

## 输出说明

### 结果文件 `output/result.tsv`

```
mid	uid	layer	media_type	confidence
5250218712893321	uid001	考虑层	text	
```

### 错误记录 `logs/error_records.tsv`

```
mid	uid	error_type	error_detail
5250218712893321	uid001	text	标签提取失败 | output=...
```

### 日志文件 `logs/classify_YYYYMMDD.log`

完整运行日志，含进度、警告、错误。

## 扩展视频功能

1. 在 `config.yaml` 中设置 `media.video.enabled: true`
2. 在 `src/media_handler.py` 的 `VideoHandler` 中实现：
   - `download_video()` — 视频下载
   - `extract_frames()` — ffmpeg/cv2 抽帧
3. `classifier.py` 中的 `classify_video()` 已预留完整流程，无需改动

## 后续扩展为API服务

`main.py` 已预留 `--mode server` 入口，后续可用 FastAPI 实现：

```python
# 示例（未实现）
@app.post("/api/classify")
async def classify(req: ClassifyRequest):
    result = classifier.classify(req.mid, req.uid, req.content, ...)
    return {"mid": result.mid, "layer": result.layer}
```
