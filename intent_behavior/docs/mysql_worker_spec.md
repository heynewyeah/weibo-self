# MySQL 分表 Worker 设计与运行说明（历史说明）

> 正式运行请以 [production_runbook.md](production_runbook.md) 为准。本文件保留开发阶段背景，以下旧描述不再代表生产实现。

## 1. 背景

根据 [`nature_ad_super_mid 分表业务数据库开发说明文档`](../nature_ad_super_mid%20分表业务数据库开发说明文档)，当前项目已经从原先的 HDFS / TSV 输入扩展为 **MySQL 分表持续消费** 模式：

- 主表：`super_mid_task`
- 业务分表：`nature_ad_super_mid_0 ~ nature_ad_super_mid_19`
- 分片规则：`customer_id % 20`
- 待处理判定：`level = 0`
- 结果回写：调用 HTTP `update-level`，由服务端仅对 `level=0` 的记录更新 `level` 与 `level_time`

现有分类核心能力可直接复用：
- [`src/classifier.py`](../src/classifier.py)
- [`src/api_client.py`](../src/api_client.py)
- [`src/media_handler.py`](../src/media_handler.py)

新增部分主要是 **MySQL 数据访问层 + 持续轮询 worker**。

---

## 2. 新增模块

### 2.1 [`src/db_client.py`](../src/db_client.py)

职责：
- 连接 MySQL
- 查询有效任务
- 按 `customer_id % 20` 路由分表
- 读取 `level=0` 的待分类记录
- 映射为 [`BlogItem`](../src/models.py)
- 将分类结果回写为 `level`

当前 level 映射：

| 分类结果 | level |
|---|---:|
| 认知层 | 1 |
| 兴趣层 | 2 |
| 考虑层 | 3 |
| 其他 / 失败兜底 | 6 |

### 2.2 [`src/worker.py`](../src/worker.py)

职责：
- 单轮执行 [`run_once()`](../src/worker.py)
- 持续轮询 [`run_forever()`](../src/worker.py)
- 串行执行分类并回写结果

### 2.3 [`worker.py`](../worker.py)

独立启动入口：

```bash
python3 worker.py --config config/config.yaml --once
python3 worker.py --config config/config.yaml
```

### 2.4 [`main.py`](../main.py)

本地预演入口的兼容别名；正式 MySQL 持续回写不要通过它启动。

---

## 3. 配置项

已在 [`config/config.yaml`](../config/config.yaml) 中新增：

```yaml
mysql:
  host: "10.79.104.30"
  port: 3306
  user: "clue_collect"
  password: "clue_collect"
  database: "clue_collect_common"
  charset: "utf8mb4"
  task_table: "super_mid_task"
  shard_table_prefix: "nature_ad_super_mid_"
  active_task_type: 1
  inactive_exec_status: 5
  task_customer_id_field: "operator_uid"

worker:
  poll_interval_sec: 10
  active_task_limit: 50
  fetch_limit_per_task: 100
  max_loops: 0
```

说明：
- `active_task_type=1`：来自开发文档中的 `task_type = 1`
- `inactive_exec_status=5`：来自开发文档中的 `exec_status != 5`
- `fetch_limit_per_task`：每个有效任务单轮最多读取多少条待处理 mid
- `max_loops=0`：无限轮询；如设为正整数则轮询指定次数后退出

---

## 4. 当前处理逻辑

1. 查询 `super_mid_task`
2. 过滤：`task_type = 1` 且任务仍在有效窗口
3. 获取任务中的 `operator_uid`（即 `customer_id`）
4. 计算分表：`nature_ad_super_mid_{customer_id % 20}`
5. 查询该分表中：
   - `customer_id = ?`
   - `super_task_id = task.task_id`
   - `level = 0`
6. 将记录映射为：
   - `mid -> BlogItem.mid`
   - `mid_uid -> BlogItem.uid`
   - `mid_text -> BlogItem.content`
   - `mid_pids -> BlogItem.pic_ids`
   - `mid_fids -> BlogItem.media_ids`
7. 使用 MySQL 命名锁互斥，重新确认记录仍为 `level=0`
8. 调用完整 Pipeline（反解、转发审查、图文视频、分类）
9. HTTP 回写，并在超时/data=0 时只读确认最终 level

---

## 5. 当前版本的边界与后续优化

### 已完成
- 基础分表路由
- level=0 待处理拉取
- 文本 / 图片 / 视频字段映射
- 分类回写
- 独立 worker 入口

### 当前边界

1. **失败处理**
   - 反解、模型、媒体等可确认的处理失败会按约定回写 `level=6`；
   - 审计中保留真实 `error_stage/error`，`level=6` 不等于模型正常分类；
   - HTTP 回写失败不补写 6，因为原请求可能已在服务端落库，必须先读库确认。

2. **并发消费**
   - 当前单进程串行；多实例时使用 MySQL 命名锁与 `level=0` 二次确认防重；
   - 上线前仍需执行唯一约束与组合索引迁移。

3. **任务路由**
   - 只使用 `super_mid_task.operator_uid`，不支持 `test_customer_id` 兜底。

4. **失败重试**
   - 当前业务约定将可确认失败终态写为 6，不再自动重试；
   - 需要复盘/人工重跑时依据 JSONL/MySQL 审计定位后受控处理。

---

## 6. 依赖

已在 [`requirements.txt`](../requirements.txt) 增加：

```txt
pymysql>=1.1.0
```

安装：

```bash
pip install -r requirements.txt
```

---

## 7. 建议联调顺序

### 第一步：验证 MySQL 连接

```bash
python3 worker.py --config config/config.yaml --once
```

### 第二步：验证 [`nature_ad_super_mid_x`](../tests/07_mysql_worker/test_nature_ad_super_mid.py) 分表测试样例

推荐直接执行：

```bash
python3 tests/07_mysql_worker/test_nature_ad_super_mid.py --table nature_ad_super_mid_1 --limit 10
```

重点确认：
- `mid_text`
- `mid_pids`
- `mid_fids`
- `level`
- `BlogItem` 字段映射是否正确

如需直接跑分类验证：

```bash
python3 tests/07_mysql_worker/test_nature_ad_super_mid.py --table nature_ad_super_mid_1 --run-classify --limit 3
```

### 第三步：按需验证 [`super_mid_task`](../tests/07_mysql_worker/test_super_mid_task.py) 主表读取与路由

```bash
python3 tests/07_mysql_worker/test_super_mid_task.py --limit 10
```

用途：
- 验证主表结构读取是否正常
- 验证测试注入的 `customer_id` 是否能正确路由到分表

### 第四步：插入测试数据后执行单轮

观察：
- 日志是否正确打印路由表名
- 是否读取到待处理记录
- 是否成功回写 `level` / `level_time`
- 失败时是否正确回写 `transfer_score_error_code` / `transfer_score_error_detail`

---

## 8. 下一步建议

下一轮开发建议按优先级继续补这 4 件事：

1. 增加并发 worker 版本
2. 将 `level` 回写之外，再补一份独立 AI 分类结果表，降低对业务字段的侵入
3. 明确 `super_mid_task -> customer_id` 的正式映射来源，去掉测试注入逻辑
4. 为 [`tests/07_mysql_worker/test_nature_ad_super_mid.py`](../tests/07_mysql_worker/test_nature_ad_super_mid.py) 增加断言型自动化测试
