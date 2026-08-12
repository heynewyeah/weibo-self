# 意图行为项目-完整交接文档

> 本文档包含项目全部信息，供AI接手开发使用。所有信息均基于真实表结构和实际查询结果。
> 最后更新：2026-08-12

---

## 一、项目概述

### 1.1 项目目标

通过AI模型（Qwen3.6-35B-A3B）将微博博文分类到3个营销层级（认知层/兴趣层/考虑层），为"原生内容站"平台提供内容分类能力。

### 1.2 两个硬要求

1. **同一条博文/图/视频必须给出一致的分类结果**（需验证一致性）
2. **thinking模式必须关闭**（避免输出过长被max_tokens截断）

### 1.3 后续接口模式

上游通过网络接口传过来 (uid + mid)，量级几百条，返回 (mid + uid + 分类结果)。博文类别有图文+视频，图和视频类的博文处理规则：先转成文字形式，再把结果放到当前模型里进行分类。

---

## 二、模型与服务配置

| 配置项 | 值 |
|--------|-----|
| vLLM服务地址 | `http://10.1.126.27:8087/v1/chat/completions` |
| 分类模型路径 | `/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B` |
| 模型类型 | MoE文本+多模态（支持图片输入，已验证） |
| thinking模式 | `chat_template_kwargs: {"enable_thinking": false}` |
| max_tokens | 512 |
| temperature | 0.0 |
| 分类层级 | 认知层 / 兴趣层 / 考虑层 |
| 行业 | 汽车（当前） |

### API调用示例（纯文字）

```python
payload = {
    "model": "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B",
    "messages": [
        {"role": "system", "content": "你是博文分类器..."},
        {"role": "user", "content": "请分类以下博文..."}
    ],
    "temperature": 0.0,
    "max_tokens": 512,
    "chat_template_kwargs": {"enable_thinking": False}
}
```

### API调用示例（图文多模态）

```python
payload = {
    "model": "/data0/yongsheng/rsync/Qwen3.6-35B/Qwen3.6-35B-A3B",
    "messages": [
        {"role": "system", "content": "你是博文分类器..."},
        {"role": "user", "content": [
            {"type": "text", "text": "请分类以下图文博文..."},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,{base64编码}"}}
        ]}
    ],
    "temperature": 0.0,
    "max_tokens": 512,
    "chat_template_kwargs": {"enable_thinking": False}
}
```

---

## 三、数据表结构（真实来源）

### 3.1 涉及的3张表

| 表名 | 库 | 职责 |
|------|-----|------|
| `dm_wb_ad_sfst_multi_day` | dplus_dm | 广告投放数据，筛选行业博文 |
| `ods_tblog_content` | ods | 微博博文原始内容 |
| `ods_ad_sfst_media_info` | ods | 素材详情（图片pid/视频信息/正文文字） |

### 3.2 dm_wb_ad_sfst_multi_day（项目用到的字段）

| 字段名 | 注释 | 真实示例值 |
|--------|------|-----------|
| mid | 博文id | 5250218712893321 |
| cust_uid | 广告主id | 1647951825 |
| creative_id | 创意id=素材组合 | 60574576 |
| market_industry_name | 运营周报口径行业 | 汽车 |
| bid_type | 竞价类型编码 | 4 |
| dt | 日期分区 | 20260101 |

**注意**：此表没有 `tsid_unique_id` 字段。

### 3.3 ods_tblog_content（项目用到的字段）

| 字段名 | 注释 | 真实示例值 |
|--------|------|-----------|
| mid | 博文ID | 5250218712893321 |
| uid | 用户UID | — |
| content | 博文内容 | 今天带大家体验东风日产N6的零压云毯大沙发... |
| dt | 日期分区 | 20260101 |

**注意**：此表没有独立的图片/视频字段。图片pid和视频信息不在content里，content是纯文字+话题标签+短链接。

### 3.4 ods_ad_sfst_media_info（完整字段）

| 字段名 | 注释 |
|--------|------|
| id | id |
| customer_id | 用户uid（=广告主cust_uid） |
| media_type | 素材类型(1:图片, 2:视频, 3:正文, 4:card标题) |
| media_id | 素材唯一id |
| customer_info | 素材信息（格式见下方） |
| created_at | 创建时间 |
| updated_at | 更新时间 |
| id_md5 | 图片或者视频的md5 |
| media_md5 | 素材唯一标识-MD5 |
| extend_field | 扩展字段(20250519新增) |
| dt | 日期分区 |

### 3.5 customer_info 四种格式（真实查询结果）

| media_type | 含义 | customer_info 格式 | 真实示例 |
|-----------|------|-------------------|----------|
| 1 | 图片 | JSON数组，元素为pid字符串 | `["6239bfd1ly1glk3gl3bqfj20gg08843c"]` |
| 2 | 视频 | JSON对象 | `{"cover":"http://wx3.sinaimg.cn/orj480/xxx.jpg","fid":"2362904:4666847103221848","orientation":"vertical","source":3,"url":"https://video.weibo.com/show?fid=2362904:4666847103221848"}` |
| 3 | 正文 | 纯文字 | `首付0元起，新车开回家！广汽丰田iA5...` |
| 4 | card标题 | 纯文字 | `威兰达3天免费用车券限时抽取` |

---

## 四、表关联关系

```
dm_wb_ad_sfst_multi_day          ods_tblog_content
┌──────────────────────┐        ┌──────────────────┐
│ mid                  │───────→│ mid              │
│ cust_uid             │        │ content (博文文字) │
│ creative_id          │        └──────────────────┘
│ market_industry_name │
│ bid_type             │
└──────┬───────────────┘
       │
       │ cust_uid = customer_id
       ▼
ods_ad_sfst_media_info
┌──────────────────────────────────┐
│ customer_id (= cust_uid)         │
│ media_type (1图片/2视频/3正文/4标题)│
│ media_id                         │
│ customer_info (素材内容)          │
└──────────────────────────────────┘
```

### 关联粒度问题

通过 `cust_uid = customer_id` 关联会返回该广告主的**所有素材**，而非某条博文对应的素材。

实际查询结果示例（customer_id=1647951825）：

| dm表 mid | dm表 cust_uid | dm表 creative_id | media表 media_type | media表 media_id | media表 customer_info |
|----------|---------------|------------------|--------------------|------------------|----------------------|
| 5250218712893321 | 1647951825 | 60574576 | 3(正文) | 2203182000000522189 | 参与话题活动#威兰达1212宠粉计划#... |
| 5250218712893321 | 1647951825 | 60574576 | 4(标题) | 2204182000000522190 | 威兰达3天免费用车券限时抽取 |
| 5250218712893321 | 1647951825 | 60574576 | 1(图片) | 2201182000000522191 | `["6239bfd1ly1glk3gl3bqfj20gg08843c"]` |
| 5250218712893321 | 1647951825 | 60574576 | 1(图片) | 2201182000000522192 | `["6239bfd1ly1glk3jvoyqkj20gg08878t"]` |
| 5250218712893321 | 1647951825 | 60574576 | 1(图片) | 2201182000000522193 | `["6239bfd1ly1glk49t82q0j20gg0880x6"]` |

**待确认**：`creative_id=60574576`（整数）与 `media_id`（长字符串）之间的精确对应关系。当前无法直接匹配。

**当前可行方案**：按 customer_id 拉取全部素材，按 media_type 分类处理。对于博文分类来说，同一广告主的素材类型通常一致。

---

## 五、完整数据提取SQL

### Step 1：提取汽车行业广告博文 + 博文文字内容

```sql
SELECT DISTINCT
    ad.mid,
    ad.cust_uid,
    ad.creative_id,
    t.content,
    t.dt
FROM dplus_dm.dm_wb_ad_sfst_multi_day ad
INNER JOIN ods_tblog_content t
    ON ad.mid = t.mid
WHERE ad.dt >= '20260101' AND ad.dt <= '20260131'
    AND ad.market_industry_name = '汽车'
    AND ad.bid_type = 4
    AND ad.social_bhv_from_imp_day > 0
    AND ad.c2s_imp_cnt > 0
```

**结果结构**：mid, cust_uid, creative_id, content, dt
**1月数据量**：5874条

### Step 2：根据 cust_uid 获取素材信息

```sql
SELECT
    m.id,
    m.customer_id,
    m.media_type,
    m.media_id,
    m.customer_info
FROM ods_ad_sfst_media_info m
WHERE m.customer_id = '${cust_uid}'
    AND m.dt = '${dt}'
```

---

## 六、各类型博文分类处理链路

### 6.1 纯文字博文（media_type=3 或 ods_tblog_content.content）

```
博文文字内容 → Qwen3.6 分类（关闭thinking）→ 认知层/兴趣层/考虑层
```

**状态**：✅ 已验证，20条测试通过（19成功1失败，失败原因为thinking截断，已关闭）

### 6.2 图片博文（media_type=1）

```
customer_info = ["pid1", "pid2"]
  → 解析JSON数组取出pid
  → pid转URL：https://wx2.sinaimg.cn/mw690/{pid}.jpg
  → curl下载图片 → base64编码
  → 配合博文文字 → Qwen3.6 多模态分类
  → 认知层/兴趣层/考虑层
```

**状态**：✅ 链路已验证

**真实pid示例**：
- `6239bfd1ly1glk3gl3bqfj20gg08843c`（来自media_info查询结果）
- `6239bfd1ly1glk3jvoyqkj20gg08878t`
- `6239bfd1ly1glk49t82q0j20gg0880x6`
- `62e00111ly1fvco1lvodmj20gg088gol`（同事给的示例，下载成功120KB）
- `006mX07Rly8ig0vpy0ph2j30t808241m`（用户提供的真实pid）

**图片下载验证结果**：
```
HTTP:200 SIZE:120242
文件类型: JPEG image data, 690x581
```

### 6.3 视频博文（media_type=2）

```
customer_info = {"cover":"url", "fid":"xxx", "url":"xxx"}
```

**方案A（当前可用）——封面图识别**：

```
cover URL → curl下载图片 → base64 → Qwen3.6多模态识别
  → 配合博文文字 → 分类
  → 认知层/兴趣层/考虑层
```

**方案B（后续实现）——视频抽帧识别**：

```
fid → 调showBatch API获取视频下载URL
  → GET http://i.iotep.tools.biz.weibo.com/api/v1/video/media/showBatch?customer_id={cust_uid}&fids={fid}
  → 下载视频（已验证：HTTP 206，2.2MB，MP4格式）
  → 抽帧（需安装opencv-python-headless或imageio-ffmpeg）
  → 帧图片base64 → Qwen3.6多模态识别
```

**showBatch API返回示例**（真实结果）：
```json
{
  "status": 200,
  "data": [{
    "fid": "2362904:4826598285967434",
    "duration": 23.218,
    "url": "https://ad.us.sinaimg.cn/o0/xxx.mp4?label=mp4_720p&...",
    "frontUrl": "http://wx3.sinaimg.cn/orj480/xxx.jpg",
    "type": "video/mp4",
    "width": 1280,
    "height": 720,
    "fileSize": 2297506,
    "quality": "720p"
  }]
}
```

**视频下载验证结果**：
```
HTTP: 206, 文件大小: 2MB, 格式: ISO Media MP4
```

**状态**：方案A可直接使用；方案B需安装抽帧工具（ffmpeg命令行不可用，Python包有ffmpeg 1.4）

---

## 七、文件路径汇总

### 7.1 数据路径

| 路径 | 说明 |
|------|------|
| `/dw_ext/ad/person/xuanyu11/intent_behavior/data/car_weibo_ad_20260101_20260131/000000_0` | 1月博文数据（5874条，TSV格式：mid\tcontent\tdt） |
| `/dw_ext/ad/person/xuanyu11/intent_behavior/data/` | 测试数据输出目录 |
| `/dw_ext/ad/person/xuanyu11/intent_behavior/output/` | 分类结果输出目录 |

### 7.2 脚本路径

| 路径 | 说明 |
|------|------|
| `/data0/xuanyu11/intent_behavior/scripts/classify_single_v2.sh` | Bash版单条分类脚本（已验证可用） |
| `/data0/xuanyu11/intent_behavior/scripts/batch_classify_v2.sh` | Bash版6类分类批量脚本（旧版，需改为3层） |

### 7.3 HDFS路径

| 路径 | 说明 |
|------|------|
| `/dw_ext/ad/person/xuanyu11/intent_behavior/data/` | 数据目录 |
| `/dw_ext/ad/person/xuanyu11/intent_behavior/output/` | 结果目录 |

---

## 八、分类提示词（3层）

```
你是一个汽车行业博文营销分层分类器。请将博文分类到以下3个营销层级之一。

【认知层】— 主打品牌曝光，高传播高热度，让用户认知品牌，容易形成传播趋势。
  典型内容：精美TVC、功能解读类、知识科普类、生活记录情绪共鸣类、话题承接内容、品牌官方宣传片、新车发布会、品牌联名活动。
  特征：以品牌/产品曝光为核心目的，传播性强，但未必包含深度产品信息或购买引导。

【兴趣层】— 含品牌词，互动率较高的，引发讨论、互动，提升用户对产品的兴趣。
  典型内容：KOL对比/评测内容、UGC种草内容、产品功能体验分享、试驾vlog、车型亮点解读、用户讨论帖。
  特征：有具体产品/品牌的信息，能引发用户兴趣和互动讨论，但尚未涉及具体购买决策信息。

【考虑层】— 产品真实测评、竞品横评对比，价格优惠信息，参数分析，帮助用户完成决策。
  典型内容：优惠促销活动内容、竞品横评对比、价格/落地价讨论、参数配置对比、用户购车决策分享、购车攻略、经销商活动。
  特征：包含帮助用户做出购买决策的具体信息，如价格、优惠、对比、参数、购买渠道等。

分类原则：
1. 优先看博文的核心目的：让用户「知道品牌」→认知层；「产生兴趣」→兴趣层；「辅助决策」→考虑层
2. 如果博文同时涉及多个层级，按最深层级归类
3. 如果博文与汽车行业完全无关，归为【认知层】
4. 直接输出分类结果，不要输出分析过程
5. 输出格式必须是：最终分类结果：【层级名称】
```

**注意**：3层定义需与产品（雷莉）最终确认。雷莉发了图片定义，待对齐。

---

## 九、上游数据输入规范

### 9.1 传输方式

| 项目 | 说明 |
|------|------|
| 数据格式 | JSONL（每行一个JSON对象） |
| 编码 | UTF-8 |
| 批量大小 | 建议100-500条/批 |

### 9.2 输入字段

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `mid` | string | ✅ | 博文ID |
| `uid` | string | ✅ | 用户ID（广告主cust_uid） |
| `content` | string | ✅ | 博文文字内容（可能为空） |
| `media_type` | string | ❌ | text/image/video（不传则自动判断） |
| `media_info` | array | ❌ | 素材信息列表 |

### 9.3 输入示例

纯文字：
```json
{"mid":"5250218712893321","uid":"1647951825","content":"今天带大家体验东风日产N6...","media_type":"text","media_info":null}
```

图文：
```json
{"mid":"5250292767523625","uid":"1647951825","content":"比亚迪宋L实拍","media_type":"image","media_info":[{"media_type":"1","customer_info":"[\"6239bfd1ly1glk3gl3bqfj20gg08843c\",\"6239bfd1ly1glk3jvoyqkj20gg08878t\"]"}]}
```

视频：
```json
{"mid":"5250301234567890","uid":"1647951825","content":"吉利银河M9极寒测试","media_type":"video","media_info":[{"media_type":"2","customer_info":"{\"cover\":\"http://wx3.sinaimg.cn/orj480/xxx.jpg\",\"fid\":\"2362904:4666847103221848\",\"url\":\"https://video.weibo.com/show?fid=xxx\"}"}]}
```

### 9.4 返回格式

```json
{"mid":"5250218712893321","uid":"1647951825","layer":"考虑层","media_type":"text","success":true,"error":""}
```

### 9.5 边界情况

| 场景 | 处理方式 |
|------|----------|
| content为空 + 有图片 | 按image类型处理 |
| content为空 + 有视频 | 按video类型处理，用封面图 |
| content为空 + 无media_info | 返回error: "内容为空" |
| media_info中pid无效 | 跳过该图片，退化为纯文字分类 |
| media_info中cover无效 | 跳过封面，退化为纯文字分类 |
| media_type不传 | 自动判断：有media_info→image/video，否则text |

---

## 十、已验证结果汇总

| 验证项 | 结果 | 日期 |
|--------|------|------|
| Qwen3.6支持图片输入 | ✅ 支持，能正确识别图片内容 | 08-07 |
| 真实pid下载图片 | ✅ HTTP 200, 120KB, JPEG | 08-11 |
| showBatch API获取视频URL | ✅ status=200, 返回完整JSON | 08-07 |
| 视频下载 | ✅ HTTP 206, 2.2MB, MP4 | 08-07 |
| 纯文字分类20条测试 | ✅ 19成功1失败（thinking截断，已关闭） | 08-05 |
| 分类分布（20条） | 认知层13/兴趣层3/考虑层3/未识别1 | 08-05 |
| thinking关闭后输出 | ✅ finish_reason=stop，输出正常 | 08-05 |

---

## 十一、已知问题与待确认

| 序号 | 事项 | 严重程度 | 说明 |
|------|------|----------|------|
| 1 | creative_id 与 media_id 的精确关联 | 🟡 中 | creative_id=60574576(整数)，media_id=2201182000000522191(字符串)，无法直接匹配 |
| 2 | 3层分类定义最终确认 | 🔴 高 | 雷莉发了图片定义，待对齐 |
| 3 | 视频抽帧工具 | 🟡 中 | ffmpeg命令行不可用，需用Python(cv2/imageio) |
| 4 | 黄金数据集 | 🔴 高 | 需人工标注100条，准确率无法量化 |
| 5 | 兜底策略"无关内容归认知层" | 🟡 中 | 可能污染数据，建议增加"无关内容"第4标签 |
| 6 | 只有1月数据 | 🟢 低 | 春节因素可能导致分布偏差，建议补充2-3月数据 |

---

## 十二、项目代码结构（已开发部分）

```
intent_behavior/
├── config/
│   └── config.yaml          # 配置文件（API、提示词、媒体参数、日志）
├── src/
│   ├── __init__.py
│   ├── api_client.py         # vLLM客户端（文本+多模态请求，重试机制）
│   ├── media_handler.py      # 媒体处理（图片完整实现 / 视频预留接口）
│   ├── classifier.py         # 核心分类器（自动路由text/image/video）
│   └── utils.py              # 工具（日志、校验、标签提取、结果/错误记录）
├── tests/
│   ├── 01_test_data/         # 测试数据生成（从Hive提取3类博文）
│   ├── 02_single/            # 单条分类测试
│   ├── 03_batch/             # 批量分类测试
│   └── 04_consistency/       # 一致性测试（同一条多次调用）
├── docs/
│   └── upstream_data_spec.md # 上游数据规范
├── main.py                   # 主入口（single/batch/server三种模式）
├── requirements.txt
└── README.md
```

### 关键设计原则

1. **配置与代码分离**：所有参数在config.yaml
2. **模块解耦**：API调用、媒体处理、分类逻辑、工具函数各自独立
3. **视频预留**：VideoHandler和classify_video()已预留完整流程
4. **错误可追溯**：错误TSV+日志文件双重记录
5. **自动路由**：classify()自动判断text/image/video

---

## 十三、标签提取逻辑

从模型输出中提取分类标签，3策略：

```python
# 策略1：查找 "最终分类结果：【xxx】" 格式（取最后一个匹配）
matches = re.findall(r'最终分类结果：【([^】]+)】', output)
# 策略2：查找所有【xxx】，取最后一个有效标签
bracket_matches = re.findall(r'【([^】]+)】', output)
# 策略3：取最后一行文本，模糊匹配
if label in last_line: return label
```

有效标签：`认知层` / `兴趣层` / `考虑层`

---

## 十四、关键人员

| 角色 | 姓名 | 职责 |
|------|------|------|
| 产品 | 雷莉 | 需求定义、原生内容站产品设计、优先级决策 |
| 算法/开发 | 金昡宇 | 博文分类模型、脚本开发、数据处理 |
| 前端 | 待定 | 原生内容站页面开发 |
| 后端 | 待定 | API对接、UGC打捞任务接入 |

---

## 十五、相关钉钉文档

| 文档 | 链接 |
|------|------|
| 需求整合文档 | https://alidocs.dingtalk.com/i/nodes/bva6QBXJwOY340Z3iM09NqK4Jn4qY5Pr |
| 博文分类体系梳理 | https://alidocs.dingtalk.com/i/nodes/GZLxjv9VGwmz6jGzU6b1Xb2DV6EDybno |
| 原生内容站PRD | https://alidocs.dingtalk.com/i/nodes/l6Pm2Db8Dazj6DQjfeOnmvLeWxLq0Ee4 |
| 意图行为库 | https://alidocs.dingtalk.com/i/nodes/Exel2BLV5P60aog0cPwvOkGLWgk9rpMq |
| 进度文档 | https://alidocs.dingtalk.com/i/nodes/P0MALyR8kwXL6jnLUYnj3DLB83bzYmDO |
| 全景文档 | https://alidocs.dingtalk.com/i/nodes/LeBq413JA2X4QrO4S3j42wlm8DOnGvpb |
| 开发日志 | https://alidocs.dingtalk.com/i/nodes/yQod3RxJKM0b6pQbTomKGNjzWkb4Mw9r |
| 数据链路文档 | https://alidocs.dingtalk.com/i/nodes/MyQA2dXW7zKynwoySZOQnekb8zlwrZgb |
