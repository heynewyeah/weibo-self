# 上游数据输入规范 v1.0

> 文档版本：v1.0  
> 最后更新：2026-08-12  
> 维护人：金昡宇（算法/开发）  
> 对接人：上游数据提供方

---

## 一、背景与目标

本服务接收上游批量传入的微博博文数据，对每条博文进行营销层级分类，返回分类结果。

**分类层级**：认知层 / 兴趣层 / 考虑层  
**当前行业**：汽车  
**博文类型**：纯文字 / 图文 / 视频

---

## 二、推荐数据格式：JSONL

### 2.1 为什么选择 JSONL

| 对比项 | JSONL | TSV | CSV |
|--------|-------|-----|-----|
| 字段扩展性 | ✅ 随时增减字段，无需改解析逻辑 | ❌ 列顺序固定，增列需双方同步 | ❌ 同 TSV |
| 嵌套结构 | ✅ 原生支持（media_info 是数组/对象） | ❌ 需要额外序列化 | ❌ 同 TSV |
| 空值处理 | ✅ null 语义明确 | ❌ 空字符串与缺失难区分 | ❌ 同 TSV |
| 流式处理 | ✅ 逐行读取，内存友好 | ✅ 同 | ✅ 同 |
| 可读性 | ✅ 字段名自描述 | ❌ 需对照文档 | ❌ 同 TSV |
| 鲁棒性 | ✅ 单行解析失败不影响其他行 | ⚠️ 列数不对会整体错位 | ⚠️ 同 TSV |

**结论：强烈推荐 JSONL 格式。**

### 2.2 文件规范

| 项目 | 规范 |
|------|------|
| 文件格式 | JSONL（每行一个 JSON 对象，行末 `\n`） |
| 编码 | UTF-8（无 BOM） |
| 换行符 | `\n`（Unix 风格） |
| 批量大小 | 建议 100~500 条/批，单批不超过 1000 条 |
| 文件大小 | 建议单文件不超过 50MB |

---

## 三、字段规范

### 3.1 必填字段

| 字段名 | 类型 | 说明 | 示例 |
|--------|------|------|------|
| `mid` | string | 博文 ID（微博唯一标识） | `"5250218712893321"` |
| `uid` | string | 用户 ID（广告主 cust_uid） | `"1647951825"` |
| `content` | string | 博文文字内容（可为空字符串，但字段必须存在） | `"今天带大家体验..."` |

### 3.2 可选字段

| 字段名 | 类型 | 说明 | 默认行为 |
|--------|------|------|----------|
| `media_type` | string | 博文类型：`text` / `image` / `video` | 不传则自动判断：有 media_info → image/video，否则 text |
| `media_info` | array\|null | 素材信息列表（见 3.3） | null 表示纯文字博文 |
| `dt` | string | 日期分区，格式 `YYYYMMDD` | `""` |

### 3.3 media_info 结构

`media_info` 是一个数组，每个元素代表一条素材记录：

```json
{
  "media_type": "1",          // 素材类型：1=图片, 2=视频, 3=正文, 4=card标题
  "customer_info": "..."      // 素材内容（格式见下方）
}
```

**各 media_type 的 customer_info 格式：**

| media_type | 含义 | customer_info 格式 |
|-----------|------|-------------------|
| `"1"` | 图片 | JSON 数组字符串，元素为 pid | `'["pid1","pid2"]'` |
| `"2"` | 视频 | JSON 对象字符串，含 cover/fid/url | `'{"cover":"http://...","fid":"xxx","url":"https://..."}'` |
| `"3"` | 正文 | 纯文字字符串 | `"首付0元起，新车开回家！..."` |
| `"4"` | card标题 | 纯文字字符串 | `"威兰达3天免费用车券限时抽取"` |

---

## 四、完整示例

### 4.1 纯文字博文

```json
{"mid":"5250218712893321","uid":"1647951825","content":"#14万级全新威兰达到店# 14万级家用SUV，全新威兰达实测续航1500公里，第五代智能电混双擎加持，WLTC综合油耗低至4.59L/100km，通勤一月一加油、自驾跨省不补能。TSS 4.0智驾+15.6英寸大屏，新车已经到店了，家用还是挺好的","media_type":"text","media_info":null,"dt":"20260101"}
```

### 4.2 图文博文

```json
{"mid":"5250292767523625","uid":"1647951825","content":"比亚迪宋L实拍来了！外观绝了，这个颜色真的太好看了，内饰也很精致，大家觉得怎么样？","media_type":"image","media_info":[{"media_type":"1","customer_info":"[\"6239bfd1ly1glk3gl3bqfj20gg08843c\",\"6239bfd1ly1glk3jvoyqkj20gg08878t\"]"}],"dt":"20260101"}
```

### 4.3 视频博文

```json
{"mid":"5250301234567890","uid":"1647951825","content":"吉利银河M9极寒测试，零下40度挑战！看看新能源旗舰SUV在极端低温下的真实表现","media_type":"video","media_info":[{"media_type":"2","customer_info":"{\"cover\":\"http://wx3.sinaimg.cn/orj480/006mX07Rly8ig0vpy0ph2j30t808241m.jpg\",\"fid\":\"2362904:4666847103221848\",\"url\":\"https://video.weibo.com/show?fid=2362904:4666847103221848\"}"}],"dt":"20260101"}
```

### 4.4 100条批量文件示例（前3行）

```
{"mid":"5250218712893321","uid":"1647951825","content":"...","media_type":"text","media_info":null,"dt":"20260101"}
{"mid":"5250292767523625","uid":"1647951826","content":"...","media_type":"image","media_info":[{"media_type":"1","customer_info":"[\"pid1\"]"}],"dt":"20260101"}
{"mid":"5250301234567890","uid":"1647951827","content":"...","media_type":"video","media_info":[{"media_type":"2","customer_info":"{\"cover\":\"...\",\"fid\":\"...\"}"}],"dt":"20260101"}
```

---

## 五、返回格式

服务处理完成后，返回 JSONL 格式，每行对应一条输入记录：

```json
{"mid":"5250218712893321","uid":"1647951825","layer":"考虑层","media_type":"text","success":true,"error":""}
{"mid":"5250292767523625","uid":"1647951826","layer":"兴趣层","media_type":"image","success":true,"error":""}
{"mid":"5250301234567890","uid":"1647951827","layer":"兴趣层","media_type":"video_fallback_text","success":true,"error":""}
```

### 返回字段说明

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `mid` | string | 博文 ID（原样返回） |
| `uid` | string | 用户 ID（原样返回） |
| `layer` | string | 分类结果：`认知层` / `兴趣层` / `考虑层` / `未识别` |
| `media_type` | string | 实际处理类型（见下方说明） |
| `success` | bool | 是否分类成功 |
| `error` | string | 失败原因（成功时为空字符串） |

**media_type 返回值说明：**

| 返回值 | 含义 |
|--------|------|
| `text` | 纯文字分类 |
| `image` | 图文多模态分类 |
| `video` | 视频多模态分类（视频功能启用时） |
| `image_fallback_text` | 图片下载失败，退化为纯文字分类 |
| `video_fallback_text` | 视频处理失败/未启用，退化为纯文字分类 |

---

## 六、边界情况处理

| 场景 | 处理方式 | 返回示例 |
|------|----------|----------|
| `content` 为空 + 有图片 | 按 image 类型处理（仅看图） | `"layer":"认知层","media_type":"image"` |
| `content` 为空 + 有视频 | 按 video 类型处理（用封面图） | `"layer":"认知层","media_type":"video"` |
| `content` 为空 + 无 media_info | 返回错误 | `"success":false,"error":"内容为空"` |
| `media_info` 中 pid 无效/下载失败 | 跳过该图片，退化为纯文字分类 | `"media_type":"image_fallback_text"` |
| `media_info` 中 cover URL 无效 | 退化为纯文字分类 | `"media_type":"video_fallback_text"` |
| `media_type` 字段不传 | 自动判断：有 media_info → image/video，否则 text | — |
| `mid` 或 `uid` 为空 | 返回错误 | `"success":false,"error":"输入校验失败: mid为空"` |
| JSON 解析失败（单行） | 跳过该行，记录错误日志 | — |

---

## 七、数据质量要求

### 7.1 上游必须保证

1. **mid 唯一性**：同一批次内 mid 不重复（重复会导致结果覆盖）
2. **content 编码**：UTF-8，不含控制字符（`\x00`~`\x1f`，换行符 `\n` 除外）
3. **pid 格式**：微博图片 pid，字母数字组合，长度 20~40 字符
4. **fid 格式**：`数字:数字` 格式，如 `2362904:4666847103221848`
5. **cover URL**：可访问的 HTTP/HTTPS URL，建议使用 `wx*.sinaimg.cn` 域名

### 7.2 建议

- `content` 字段保留原始文字，不要做截断或清洗（模型需要完整上下文）
- `media_info` 中图片建议提供 1~3 张（超过 3 张只取前 3 张）
- 视频博文建议同时提供 `cover` URL（封面图），作为视频处理失败时的兜底

---

## 八、接口调用方式（后续扩展）

当前阶段通过文件传输（JSONL 文件）。后续扩展为 HTTP API 时：

```
POST /api/v1/classify
Content-Type: application/json

{
  "items": [
    {"mid": "...", "uid": "...", "content": "...", "media_type": "text", "media_info": null},
    ...
  ]
}
```

响应：

```json
{
  "code": 0,
  "message": "success",
  "data": [
    {"mid": "...", "uid": "...", "layer": "考虑层", "success": true, "error": ""},
    ...
  ]
}
```

---

## 九、常见问题

**Q：media_type 字段必须传吗？**  
A：不必须。不传时服务会自动判断：有 media_info 且含图片 pid → image；有 media_info 且含视频 fid → video；否则 text。但建议传，可以减少误判。

**Q：一条博文有多张图片，怎么传？**  
A：在 `media_info` 中传一条 `media_type=1` 的记录，`customer_info` 是包含多个 pid 的 JSON 数组字符串：`'["pid1","pid2","pid3"]'`。

**Q：视频博文没有封面图 URL 怎么办？**  
A：只传 `fid`，服务会尝试通过 showBatch API 获取视频信息。若 API 不可用，退化为纯文字分类。

**Q：content 字段可以传 HTML 或 Markdown 吗？**  
A：建议传纯文字。HTML 标签和 Markdown 语法会被模型当作内容处理，可能影响分类准确性。

**Q：同一条博文重复传会怎样？**  
A：服务会正常处理并返回结果，不做去重。由于 temperature=0.0，同一条博文的分类结果是确定性的，重复传结果相同。

---

## 十、版本历史

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v1.0 | 2026-08-12 | 初始版本，定义 JSONL 格式、字段规范、边界处理 |
