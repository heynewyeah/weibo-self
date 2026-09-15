# 原生内容站 AI 意图分层

> 最后更新：2026-09-14
> 正式持续入口：`python3 worker.py --config config/config.yaml`

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
| 待处理博文 | `nature_ad_super_mid_{operator_uid % 20}` | 查询对应任务的 `level=0` 记录 |
| 转发上下文 | 分表字段 `forward_mid`、`forward_mid_text` | 缺少原博正文时归“其他”；正常时合并转发正文与原博正文 |
| 品牌上下文 | 分表 `hit_mid_tag` + 任务 `brand_tag` JSON | 命中 tag 时精确解析品牌；没有命中时回退任务全部品牌词 |
| 博文真实内容/媒体 | mid 反解接口 | 获取正文、pid、fid、uid 等，不依赖分表中的历史正文 |

历史 Hive/HDFS 输入配置和 `scripts/run_hive.sh` 仅用于早期数据准备/离线实验，不属于当前 MySQL 正式链路。

## 3. 正式处理链路

```text
super_mid_task.operator_uid
  → operator_uid % 20 路由 nature_ad_super_mid_x
  → 查询 level=0
  → MySQL 命名锁 + 再确认 level=0
  → mid 反解
  → 转发审查
      ├─ 原博正文缺失 / 明确异常：level=6（其他）
      └─ 正常：转发正文 + 原博正文综合分类
  → 图片下载 / 视频抽帧（>300 秒或 >200MB 降级封面；仍失败降级文本）
  → AI 分类为 level=1/2/3/6
  → HTTP update-level 回写（服务端只更新 level=0）
  → 超时或 data=0 时只读查询分表确认最终 level
  → 审计、缓存清理
```

`Ctrl+C` 中止当前 mid 时，不写 level=6，未完成记录应保持 `level=0`，下次 worker 会继续处理。

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

# 确认无误后持续运行；Ctrl+C 停止
python3 worker.py --config config/config.yaml
```

默认 worker 行为：持续查询有效任务 → 处理 `level=0` → 回写 → 等待 10 秒 → 下一轮。`--once` 只运行一轮后退出。

更完整的运行说明见 [docs/production_runbook.md](docs/production_runbook.md)。

## 7. 审核与 Case 排查地图

| 场景 | 首先看哪里 | 对应工具 |
| --- | --- | --- |
| 某 task 没被消费 | `super_mid_task`、`operator_uid`、分表路由 | `scripts/production_preflight.py` |
| 某 mid 为什么是某层级/其他 | JSONL 的 `run_id`、`mid_processed` 的 `model_output`/`forward_status` | `scripts/manual_classify_mid.py --task-id <id> --mid <mid>` |
| 回写超时却怀疑已落库 | 分表该 mid 的 `level`、审计 `error_stage=writeback` | `scripts/manual_classify_mid.py`（先不回写预演） |
| 反解/媒体/模型失败 | JSONL 的 `error_stage`、`error`、耗时 | `logs/runs/YYYYMMDD/` |
| 旧版本误把中断写成 6 | 先确认审计和人工判断 | `scripts/manual_classify_mid.py --retry-level-6 --write-back` |
| 回归关键业务保护 | 不连库、不调模型的离线测试 | `python3 -m unittest tests.test_production_guards -v` |
| 模型连通性 | 模型服务状态 | `python3 check_model_service.py` |

MySQL 审计启用后，可按 `task_id`、`mid`、`record_id`、`run_id` 查询 `nature_ad_mid_ai_audit`。清理审计先预览、后低峰执行：

```bash
python3 scripts/cleanup_mysql_audit.py
python3 scripts/cleanup_mysql_audit.py --execute
```

### 每周项目周报

周报优先消费 `logs/runs/YYYYMMDD/*.jsonl`，因此不会将早期手工测试日志混入正式运行指标：

```bash
# 只读生成最近 7 个自然日的统计报告
python3 scripts/generate_weekly_report.py --days 7
```

报告包括处理量、去重 mid、成功/兜底/失败/中断、闭环率、分类/行业/媒体/转发分布、平均/P50/P95 耗时、失败阶段、按天趋势、审计目录和磁盘健康度。

项目配置已包含钉钉机器人和接收人。先手工验证，再在正式运行机安装每周四 10:00 的 cron：

```bash
python3 scripts/send_weekly_report.py --dry-run
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
| 周报统计 | `scripts/generate_weekly_report.py`、`scripts/send_weekly_report.py`、`scripts/install_weekly_report_cron.sh` | JSONL 周报生成、企业机器人单聊发送和周四定时安装 |
| 辅助数据脚本 | `scripts/count_xlsx_mids.py` | Excel 博文映射 mid |
| 历史/弃用 | `run_e2e_pipeline.py`、`scripts/run_hive.sh`、`scripts/batch_classify_3layer.sh`、`sql/query_*.sh`（除查明细 SQL） | 不部署、不绕过 worker；仅保留历史数据准备或人工参考 |

`sql/production_schema_migration.sql` 是新库建表/索引的迁移模板，不会被 Python 自动执行；正式库是否需要执行必须由具备 DDL 权限的 DBA 根据预检结果确认。
