# 固定汽车任务 888888 独立运行

普通任务继续使用原来的 `worker.py`。特殊任务使用 `worker_special_task.py`，两者可以分开启动、停止、查看日志。当前仅支持服务器运行；DataWorks 适配另行实施。

## 处理范围

- 不查询 `super_mid_task`，不需要任务表的 `operator_uid`、品牌词或行业配置。
- 固定扫描 `nature_ad_super_mid_0` 至 `_19`，只选择 `super_task_id=888888 AND level=0`。
- 固定汽车行业；品牌词和任务话题词为空，保留明细中的发博账号、转发信息、mid、uid 等上下文。
- 复用普通任务的汽车提示词、反解、多模态分类和 HTTP 回写。请求中的 `customer_id` 来自每条明细，`task_id` 固定为 888888；不直接 UPDATE 业务表。
- 明细必须有有效 `customer_id`，并符合 `customer_id % 20` 的分表规则；异常记录会明确报错并跳过，不会猜测客户 ID。
- 记录锁、回写前 `level=0` 二次检查、最多额外重试三次、30 分钟空闲心跳沿用普通 Worker。冷却/重试耗尽的记录不会阻塞后续分页。

## 命令

以下命令均从服务器仓库根目录执行。配置继续共用 `config/config.yaml` 的连接与模型服务信息，无需复制账号密码；程序会在内存中生成隔离配置，不修改原配置文件。

离线单元测试（不连接数据库、模型或回写接口）：

```bash
cd intent_behavior && python3 -m unittest tests.test_special_task_worker tests.test_production_guards tests.test_worker_retry tests.test_worker_idle_logging -v
```

首次预演一条（会查询真实数据库并调用真实反解/模型，**不回写业务结果、不写 MySQL 审计，也不消耗正式重试预算**；始终只跑一轮）：

```bash
cd intent_behavior && python3 worker_special_task.py --config config/config.yaml --dry-run --limit 1
```

正式单轮一条（**真实分类并调用 HTTP 回写**）：

```bash
cd intent_behavior && python3 worker_special_task.py --config config/config.yaml --once --limit 1
```

正式持续执行（**持续查询、分类和真实回写**；Ctrl+C 停止）：

```bash
cd intent_behavior && python3 worker_special_task.py --config config/config.yaml
```

`--limit` 是每轮全部分表合计的条数上限，`0` 表示不额外限制。默认仍按 `worker.fetch_limit_per_task` 每表最多取 100 条；特殊入口不受普通任务数量上限 `active_task_limit` 影响。特殊入口强制使用独立单实例锁，包括 `--once` / `--dry-run`，避免同机误开两份。

## 日志与排障

| 数据 | 默认位置 |
| --- | --- |
| 主日志（按日轮转） | `logs/special_888888/special_worker.log` |
| 分类审计 | `logs/special_888888/runs/YYYYMMDD/*.jsonl` |
| 重试状态与完整失败档案 | `logs/special_888888/failures/` |
| 错误补充日志 | `logs/special_888888/YYYYMMDD_error.log` |
| 单实例锁 | `logs/special_888888/worker.lock` |
| 媒体临时缓存 | `output/.cache_special_888888/` |

若基础配置使用自定义 `logging.dir`，以上日志根目录相应变为 `<logging.dir>/special_888888/`。预演审计记录 `write_back=false`；正式回写后可核对 `writeback_state` 和明细最终 `level`。技术失败仍保持 `level=0`，耗尽预算后查本入口独立的失败档案。

只读跟踪特殊任务日志（Ctrl+C 退出查看）：

```bash
cd intent_behavior && tail -f logs/special_888888/special_worker.log
```

## 首次验证记录（2026-10-23）

- 本地：特殊任务、普通生产保护、重试和空闲日志共 50 项离线测试通过。
- 10.194.68.7 / ads_fst：在独立临时副本运行新增 14 项测试，通过；随后使用原服务器配置执行 `--dry-run --limit 1`。
- 实际扫描 20 个分表，匹配 `super_task_id=888888 AND level=0` 的记录为 0；尚未验证真实样本的模型结果或真实回写。测试未修改原服务器项目文件，也未启动持续消费者。

## 周一继续测试的交接（2026-10-26）

- 特殊任务功能已提交：`80bc219`；独立入口、独立日志和缓存，普通 Worker 不变，尚未迁移 DataWorks。代码尚未 push，服务器原项目也尚未部署本次代码。
- 用户提供的测试库：`10.79.104.30:3306`，数据库 `clue_collect_common`，账号 `clue_collect`，其中有测试任务。密码不记录在文档、Git 或提醒中，执行时通过安全输入或已有本地凭据获取。
- 2026-10-23 到此暂停，不继续访问测试库、不做真实回写；2026-10-26（周一）继续。
- 届时先准备独立测试配置，不修改正式 `config/config.yaml`；确认其连接测试库，并核对 20 个分表中任务 `888888` 的 `level=0` 样本及明细 `customer_id`。
- 先以 `--dry-run --limit 1` 验证真实反解、汽车分类结果及空品牌词场景；这会调用真实服务，但不会回写分类结果。
- **仅切换 MySQL 地址不等于切换回写环境。** 当前功能沿用 HTTP 回写，真实回写前必须另行确认测试回写接口确实写入测试库；未确认时只做预演。
- 测试回写环境确认后，再执行单轮一条并查库核对，最后才扩大样本；不自动启动长期消费者，不变更普通任务或 DataWorks 调度。
