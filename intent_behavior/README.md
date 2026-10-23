# 原生内容站 AI 意图分层

> 最后更新：2026-09-16
> 正式持续入口：`python3 worker.py --config config/config.yaml`
> 固定汽车任务 888888 独立入口：`python3 worker_special_task.py --config config/config.yaml`，见 [运行说明](docs/special_task_888888.md)。

本项目消费原生内容站 MySQL 任务和待处理博文，完成 mid 反解、转发处理、媒体理解、AI 分层，并通过 HTTP 接口回写 level。

## 1. 运行边界

- 正式运行环境：`10.194.68.7`。
- 正式库：`clue_collect_common`，连接参数只维护在 [config/config.yaml](config/config.yaml) 的 `mysql` 段；不要把账号或密码复制到脚本、README、命令历史或日志。
- 旧测试库连接信息保留为 `config.yaml` 中的注释，仅为必要时的人工联调参考；正式 `worker.py` 不再使用。
- 正式业务结果只通过 `result_writer.url` 的 HTTP `update-level` 接口回写，不直接 UPDATE `nature_ad_super_mid_*` 的 level。

## 2. 数据从哪里读

| 数据 | 来源/路径 | 用途 |
| --- | --- | --- |
| 有效任务 | `clue_collect_common.super_mid_task` | 读取 `task_id`、`operator_uid`、`industry_tag`、`brand_tag` |
| 待处理博文 | `nature_ad_super_mid_{operator_uid % 20}` | 查询对应任务的 `level=0` 记录；日志中的博文链接取该表 `short_url` |
| 转发上下文 | 分表字段 `forward_mid`、`forward_mid_text` | 缺少原博正文时归“其他”；正常时合并转发正文与原博正文 |
| 品牌上下文 | 分表 `hit_mid_tag` + 任务 `brand_tag` JSON | 命中 tag 时精确解析品牌；没有命中时回退任务全部品牌词 |
| 话题上下文 | 任务 `topic_code` | 动态解析任务话题词，与品牌词、正文和媒体共同用于分层 |
| 博文真实内容/媒体 | mid 反解接口 | 获取正文、pid、fid、uid 等，不依赖分表中的历史正文 |

`data_extractor.py` 中仍保留早期 Hive/HDFS 预演能力，但它不属于当前 MySQL 正式链路，也不能用于生产回写。

“有效任务”是项目原有 MySQL 查询规则，不是本次上线新增的业务规则：`task_type=1`，且 `exec_status!=5`；若 `exec_status=5`，则 `end_time` 在最近 1 天内也会暂时继续被扫描。该规则实现在 `src/db_client.py` 的 `fetch_active_tasks()`；是否要保留“完成后 1 天”的窗口，应由上游任务状态定义确认。

## 3. 正式处理链路

```text
super_mid_task.operator_uid
  → operator_uid % 20 路由 nature_ad_super_mid_x
  → 查询 level=0
  → MySQL 命名锁 + 再确认 level=0
  → mid 反解
  → 粗行业候选路由（如美食 → 奶茶，不在此处提前丢弃）
  → 转发审查
      ├─ 原博正文缺失 / 明确异常：level=6（其他）
      └─ 正常：转发正文 + 原博正文综合分类
  → 图片下载 / 视频抽帧（视频超过 300 秒或 200MB 降级封面；仍失败降级文本）
  → 品牌词 + 话题词 + 正文 + 图片/视频的完整 AI 分类为 level=1/2/3/6
      └─ 反解、媒体或模型技术失败：不生成业务等级，保持 level=0 待重试
  → HTTP update-level 回写（服务端只更新 level=0）
  → 超时或 data=0 时只读查询分表确认最终 level
  → 审计、缓存清理
```

`Ctrl+C` 中止，或反解、图片/视频处理、模型请求、模型结果解析失败时，都不写
`level=6`。这些都是技术失败，不是“其他”分类结果；记录保持 `level=0`，下一轮 worker 或人工命令会重试。

## 4. 写到哪里

| 类型 | 路径/目标 | 默认行为 |
| --- | --- | --- |
| 业务分类结果 | `update-level` HTTP 接口 → `nature_ad_super_mid_*` | worker 开启；服务端只改 `level=0` |
| 主运行日志 | `logs/mysql_worker.log` | 按天轮转、保留 30 天 |
| 运行审计 | `logs/runs/YYYYMMDD/<run_id>.jsonl` 和 `_summary.json` | 每个 mid 的结果、耗时、错误、模型输出 |
| MySQL 审计（可选） | `nature_ad_mid_ai_audit` | 新正式库确认表存在且账号有 INSERT 权限后再开启 |
| 媒体缓存 | `output/.cache/` | 单条结束删除；启动时清理超过 24 小时的遗留文件 |
| 旧 TSV | `output/result.tsv` | 默认关闭，仅本地兼容调试 |

## 5. 磁盘空间保护

运行盘空间不足会比“没有日志”更危险。配置中默认：

- `storage.min_free_mb: 5120`：可用空间低于 5GB 时自动停止本地主日志、JSONL、错误汇总和媒体下载；
- 终端日志仍可见；
- 图片/视频会自动降级到文本分类；
- 本地审计的保留期由 `audit.retention_days` 控制，主日志由 `logging.retention_days` 控制。

如必须临时减少写盘，可在 `config.yaml` 中关闭：

```yaml
logging:
  file_enabled: false
  error_file_enabled: false
audit:
  local_enabled: false
```

关闭本地 JSONL 前，应先确认 `audit.mysql_enabled: true` 且正式账号对审计表有 INSERT 权限，或已接入外部日志平台；否则会失去可追溯性。

## 6. 上线与日常运行

首次上线顺序：

```bash
cd intent_behavior
pip install -r requirements.txt

# 仅检查配置、表、索引、重复数据和审计权限；不分类、不回写、不修改数据
python3 scripts/production_preflight.py --strict

# 只执行一轮，观察终端、审计和回写
python3 worker.py --config config/config.yaml --once

# 确认无误后，以 systemd 持续运行（推荐）
# 具体安装、启停、看日志方法见 docs/production_runbook.md
```

默认 worker 行为：持续查询有效任务 → 处理 `level=0` → 回写 → 等待 10 秒 → 下一轮。空轮询不输出逐任务日志或审计汇总，每 30 分钟输出一条空闲心跳（由 `worker.idle_heartbeat_sec` 配置）；有待处理记录时照常输出处理过程与汇总，错误和告警仍立即输出。`--once` 只运行一轮后退出，并打印本轮汇总。

### Dataworks 随时监控

正式消费已由 Dataworks 管理，日常查看无需重新启动服务器 worker。
打开“原生内容站运行监控”（内部任务名 `intent_behavior_dataworks_probe`）→单次提交→运行历史→正常输出，
可查看运行心跳、累计处理尝试、最近轮询、失败阶段、重试状态和最近20行日志。
心跳约60秒更新，超过180秒会标记过期；监控本身不分类、不回写。
完整入口、单条MID查询及归档位置见 [Dataworks 迁移记录](docs/dataworks_migration.md)。

服务器已具备HDFS客户端及 `ads_fst` 访问权限时，从仓库根目录执行（只读，Ctrl+C仅退出查看）：

```bash
cd intent_behavior && python3 scripts/dataworks_monitor.py --hdfs-root viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks --watch --interval 30
```

### 单条失败留存与有限重试

- 技术失败仍保持业务 `level=0`，不写成成功或 `level=6`。首次处理失败后**最多额外重试 3 次，总共最多 4 次**；`worker.max_retries` 可设为 0～3。默认间隔为 60 / 300 / 1800 秒（`worker.retry_delays_sec`），到期后由轮询执行，不阻塞其他博文。
- 正式 worker 将反解、模型 API、回写客户端的内层 `max_retry` 统一设为 1，避免多层重试相乘。每次完整链路可包含不同业务阶段的请求，但各阶段不再自行重复重试。手工排障脚本仍使用自身的配置，不受自动预算控制。
- 达到上限后停止自动消费该记录，冷却/已隔离记录不生成反复处理的日志或审计；分页越过这些记录，避免前 100 条失败占满查询结果，阻塞后面的正常数据。
- 第一次失败就保存完整任务、分表原始行、每次尝试输入、错误与异常栈、可获得的完整反解响应、反解结果、模型输出、耗时及回写状态/响应。JSON 档案不做正文、错误或模型输出截断；不复制媒体二进制文件，也不保存数据库密码或配置密钥。
- 默认档案：`logs/failures/<记录标识的 SHA256>.json`；每条一份，最多 4 次自动尝试的历史合并其中，不逐轮制造新文件。状态与完整详情的原子备份：`logs/failures/retry_state.sqlite3`。JSON 文件和 SQLite 文件权限为 0600。目录随 `audit.dir` 的父目录定位：例如 `logs/runs` 对应 `logs/failures`。
- 预算在接口调用前写盘，进程崩溃也不会重置预算；崩溃时尚未返回的结果不能凭空补齐，但已保存输入与预占次数。Ctrl+C/SIGTERM 正常走中断路径时不消耗重试次数。首次即成功的记录不会生成失败档案。重试后成功的历史档案保留，状态改为 `resolved`。
- 预算跨同一持久目录中的进程重启、跨天保留，不自动清零、不随 JSONL 的 30 天清理删除。**不要删除 `logs/failures` 来清理普通日志**，否则会丢失重试预算。多主机/临时 DataWorks 工作目录不能共享本地预算；当前 Dataworks 入口通过 `DATAWORKS_STATE_HDFS` 单独备份 SQLite，新容器消费前恢复预算及完整失败档案。每60秒及正常退出同步；主机崩溃可能丢失最后一个同步周期内的预算，普通日志 tar 包不能替代状态恢复。
- 无法写入重试预算或完整档案时，停止 worker；JSON 导出失败时详情仍保存在 SQLite。基础设施查询异常连续 3 轮退出，避免环境故障无限刷日志。
- 日/周/月机器人报告增加失败档案目录、本窗口新增停止数、当前累计预算耗尽/等待数和样例文件位置；**不发送原始博文或模型输出**。状态读取为只读，状态损坏不会导致整份报告失败。历史审计不回改，预算从部署此版本后的首次处理开始计数。

上线时先在原运行窗口按 Ctrl+C 停止旧 worker（若由调度平台管理，应停止旧实例并通过原入口重启），再更新代码。必须重启 worker 才能启用新预算；日报 cron 无需重装。不要在旧实例仍运行时另起一份：旧版本不遵守新预算。

```bash
# 离线测试：不连接 MySQL、不调用模型、不回写、不发送钉钉
python3 -m unittest tests.test_worker_retry tests.test_worker_idle_logging \
  tests.test_production_guards tests.test_weekly_report tests.test_dingtalk_report_schedule -v

# 正式持续消费：会调用业务接口/模型并真实回写；在停止旧实例后执行
python3 worker.py --config config/config.yaml

# 报告只预览，不发送消息
python3 scripts/send_weekly_report.py --period daily --dry-run
```

更完整的运行说明见 [docs/production_runbook.md](docs/production_runbook.md)；假期值守、错误定位和单条复测见 [docs/holiday_troubleshooting.md](docs/holiday_troubleshooting.md)。

## 7. 审核与 Case 排查地图

| 场景 | 首先看哪里 | 对应工具 |
| --- | --- | --- |
| 某 task 没被消费 | `super_mid_task`、`operator_uid`、分表路由 | `scripts/production_preflight.py` |
| 某 mid 为什么是某层级/其他 | JSONL 的 `run_id`、`mid_processed` 的 `model_output`/`forward_status` | `scripts/manual_classify_mid.py --task-id <id> --mid <mid>` |
| 回写超时却怀疑已落库 | 分表该 mid 的 `level`、审计 `error_stage=writeback` | `scripts/manual_classify_mid.py`（先不回写预演） |
| 反解/媒体/模型失败 | `logs/failures/*.json` 完整档案及 JSONL 的 `error_stage`；分表保持 `level=0` | 最多自动重试 3 次；达到上限后人工排查，可用 `scripts/manual_classify_mid.py` 受控复测 |
| 旧版本误把中断写成 6 | 先确认审计和人工判断 | `scripts/manual_classify_mid.py --retry-level-6 --write-back` |
| 回归关键业务保护 | 不连库、不调模型的离线测试 | `python3 -m unittest tests.test_production_guards -v` |
| 模型连通性 | 模型服务状态（当前为 KServe 网关） | `python3 check_model_service.py` |
| 模型接口参数回归 | 请求体是否符合网关要求、思考是否关闭 | `python3 -m unittest tests.new_request_modal_test.test_api_client_endpoints -v`（加 `--live` 走真实接口） |

MySQL 审计启用后，可按 `task_id`、`mid`、`record_id`、`run_id` 查询 `nature_ad_mid_ai_audit`。清理审计先预览、后低峰执行：

```bash
python3 scripts/cleanup_mysql_audit.py
python3 scripts/cleanup_mysql_audit.py --execute
```

### 钉钉定时项目报告

报告优先消费 `logs/runs/YYYYMMDD/*.jsonl`，因此不会将早期手工测试日志混入正式运行指标。定时规则为：

- 每天发送 T-1 日报；
- 每周五额外、单独发送当周周一至周五的周汇总；
- 每月最后一天额外、单独发送当月 1 日至当天的月汇总。

统计时扫描所有保留的审计日期目录，以每条记录的 `event_time`（Asia/Shanghai）决定是否进入窗口及归属哪一天。长期 worker 的目录是启动日期，跨天记录仍可能写在旧目录，不再按目录日期过滤。无时区的历史时间按北京时间解释；缺少时间的旧记录兼容使用目录日期，时间格式损坏的记录告警并跳过。门户 JSON 聚合复用相同读取逻辑。

更新报告脚本后，下一次 cron 会自动使用新逻辑，无需重启 worker 或重装 cron；无需迁移旧审计文件或重新分类。补发某天日报时，`--run-date` 应为报告日期的下一天，并使用 `--period daily` 避免额外发送周/月汇总。例如补发 2026-10-09：

```bash
# 只预览，不发送
python3 scripts/send_weekly_report.py --period daily --run-date 2026-10-10 --dry-run

# 真实发送一条日报；重复执行会重复发送
DWS_RUNNER=/usr/home/ads_fst/.local/bin/dws \
  python3 scripts/send_weekly_report.py --period daily --run-date 2026-10-10
```

可以按明确日期范围手工生成报告：

```bash
python3 scripts/generate_weekly_report.py \
  --start-date 2026-09-19 --end-date 2026-09-19 --report-name 日报
```

报告包括处理量、去重 mid、成功/兜底/失败/中断、闭环率、分类/行业/媒体/转发分布、平均/P50/P95 耗时、失败阶段、按天趋势、审计目录和磁盘健康度。

项目配置已包含钉钉机器人和接收人。先手工验证自动调度结果，再在正式运行机安装每天 10:00（北京时间，含周末）的 cron：

```bash
python3 scripts/send_weekly_report.py --dry-run
python3 scripts/send_weekly_report.py --run-date 2026-07-31 --dry-run
python3 scripts/send_weekly_report.py

bash scripts/install_weekly_report_cron.sh \
  --project-dir /data0/xuanyu11/intent_behavior-git/weibo-self/intent_behavior \
  --python /usr/bin/python3 \
  --dws-runner /usr/local/bin/dws
```

## 8. 脚本分级

| 分类 | 文件 | 说明 |
| --- | --- | --- |
| 正式入口 | `worker.py` | 唯一持续 MySQL 消费和回写入口 |
| 受控排障 | `run_single_task.py`、`scripts/manual_classify_mid.py` | 默认不回写；需要明确加 `--write-back` |
| 上线检查 | `scripts/production_preflight.py` | 只读检查 |
| 审计维护 | `scripts/cleanup_mysql_audit.py` | 默认预览，`--execute` 才删除 |
| 本地预演 | `run_classification.py`、`main.py` | `main.py` 是兼容别名，不用于正式回写 |
| 关键回归测试 | `tests/test_production_guards.py` | 当前正式链路的离线保护测试 |
| 模型接口测试 | `tests/new_request_modal_test/`（`test_model_cases.py`、`test_api_client_endpoints.py`、`compare_llm_endpoints.py`） | 具体样例断言、与原模型一致性对比、请求体参数单测、接口参数矩阵；见目录内 README |
| 钉钉报告 | `scripts/generate_weekly_report.py`、`scripts/send_weekly_report.py`、`scripts/install_weekly_report_cron.sh` | JSONL 日/周/月报生成、企业机器人单聊发送和每天定时安装 |
| 辅助数据脚本 | `scripts/count_xlsx_mids.py` | Excel 博文映射 mid |
| SQL 排查工具 | `sql/query_detail-明细表查询.sql`、`sql/query_detail-明细表查询.sh`、`sql/query_task-查询有效任务.sh` | 只读查询任务/分表明细；不部署、不绕过 worker |

`sql/production_schema_migration.sql` 是新库建表/索引的迁移模板，不会被 Python 自动执行；正式库是否需要执行必须由具备 DDL 权限的 DBA 根据预检结果确认。

## 9. 模型服务与接口参数

当前启用的模型服务（2026-09 起切换到 llm-beixian 网关注入）：

| 项 | 值 |
| --- | --- |
| 接口地址 | `config/config.yaml` 的 `api.url`：`http://llm-beixian.multimedia.wml.weibo.com/mm-wb-ads/qwen36-35b-a3b-ads-fst-6ab34420/v2/models/llm/chat/completions` |
| 协议 | KServe v2（`.../v2/models/llm` 提供模型元信息，chat 走末尾 `/chat/completions`） |
| 服务端模型名 | `qwen36-35b-a3b-fp8`（请求体里的 `model` 字段会被网关覆盖，填旧路径也能用） |
| 关闭思考 | `thinking: {type: "disabled"}`（顶层 `enable_thinking: false` 也可） |
| 网关不支持 | `reasoning` 字典、`chat_template_kwargs` 字典（返回 400，只接受 int/bool/string） |
| 多模态 | 支持 `image_url`（图文、视频抽帧与旧接口相同链路） |
| 备份方案 | 旧直连 vLLM（`:8087`）的 url/model 已在 `config.yaml` 注释保留；切换时同时放开 `reasoning: {effort: "none"}` 和 `enable_thinking: false` |

[src/api_client.py](src/api_client.py) 按“配置为 `null` 就不下发该参数”的方式组织请求体，因此两套网关可以共存配置、按需切换，不需要改代码。若网关新增参数，可用 `api.extra_params` 透传（值为 `null` 时跳过）。

模型接口回归：

```bash
# 具体样例测试（内置 6 条：4 条汽车分类 + 2 条高管转发审查）
python3 tests/new_request_modal_test/test_model_cases.py
python3 tests/new_request_modal_test/test_model_cases.py --task-id <task_id> --limit 10   # 用 MySQL 已回写记录对比“原模型结果”

# 请求体参数 / 连通性
python3 -m unittest tests.new_request_modal_test.test_api_client_endpoints -v
python3 tests/new_request_modal_test/test_api_client_endpoints.py --live --check-legacy

# 完整对比：参数矩阵 + 模型元信息 + 分类/转发一致性
python3 tests/new_request_modal_test/compare_llm_endpoints.py --samples 10
python3 check_model_service.py                                  # 单次连通性 ping
```

测试目录说明见 [tests/new_request_modal_test/README.md](tests/new_request_modal_test/README.md)。
