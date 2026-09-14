# 原生内容站 AI 分层生产运行手册

最后更新：2026-09-14

## 唯一正式入口

```bash
cd intent_behavior
python3 worker.py --config config/config.yaml
```

`worker.py` 是唯一允许持续 MySQL 回写的生产入口。

- `run_single_task.py`：单任务联调/排障，默认不回写；需显式传 `--write-back`。
- `scripts/manual_classify_mid.py`：指定单个 task_id + mid 的人工排障/受控回写脚本；默认不回写。
- `run_classification.py`：本地文件、单条、只读预演；MySQL 模式禁止回写。
- `run_e2e_pipeline.py`：历史兼容入口，已弃用，不部署。
- `tests/`：测试脚本；`scripts/`：运维、自检与数据辅助脚本。

## 生产处理链路

```text
super_mid_task.operator_uid（即 customer_id）
  → customer_id % 20 路由分表
  → 短事务读取 level=0
  → MySQL 命名锁互斥 + 再确认 level=0
  → mid 反解 / 图文视频处理 / 转发审查 / AI 分层
  → POST update-level
  → 服务端仅将 level=0 更新为 1/2/3/6
  → 超时或 data=0 时只读确认分表最终 level
  → 结构化审计落盘
```

## 上线前步骤

1. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

2. 正式库已切换到 `config/config.yaml` 的 MySQL 配置。旧测试库仅作为配置注释保留。不要根据旧环境的迁移历史推断新库已具备索引、唯一约束或 MySQL 审计表。

   [production_schema_migration.sql](../sql/production_schema_migration.sql) 是新库的 DDL 模板，不会由 worker 自动执行。当前账号可能只有读取权限；是否执行、何时执行必须由具备 DDL 权限的 DBA 根据预检结果决定。

3. 运行只读预检：

   ```bash
   python3 scripts/production_preflight.py --strict
   ```

4. 确认下游：

   - mid 反解接口可用；
   - 模型接口通过 `check_model_service.py` 验证；
   - `update-level` 接口返回 `code=0`，并遵守“只更新 level=0”的约定。

5. 先用单任务无回写预演：

   ```bash
   python3 run_single_task.py --task-id <task_id> --limit 10
   ```

6. 小批量正式回写（一轮完成即退出）：

   ```bash
   python3 worker.py --config config/config.yaml --once
   ```

7. 确认审计、日志和回写结果正常后，启动持续消费：

   ```bash
   python3 worker.py --config config/config.yaml
   ```

   默认每 10 秒查询一轮有效任务和 `level=0` 数据，直到人工按 `Ctrl+C` 停止。

## 人工中止与指定 mid 复测

- `Ctrl+C` 发生在某条处理中时，该条不视为业务分类失败，保持 `level=0`；下次启动 worker 会重新处理。
- 已完成并成功回写的前序 mid 不受影响。
- 每条审计立即写入 MySQL；被打断的当前 mid 写入本地 JSONL 的 `interrupted` 记录。
- 若旧版本曾把人工中止错误回写为 `level=6`，只能由人工用下方命令受控恢复；脚本只允许 `6 → 0`，绝不修改 `1/2/3`：

  ```bash
  # 先只预演
  python3 scripts/manual_classify_mid.py --task-id <task_id> --mid <mid>

  # 确认后恢复 level=6 为 0，再分类并回写
  python3 scripts/manual_classify_mid.py \
    --task-id <task_id> --mid <mid> --retry-level-6 --write-back
  ```

## 路由与业务规则

- `super_mid_task.operator_uid` 是广告主 `customer_id`。
- 分表：`nature_ad_super_mid_{operator_uid % 20}`。
- `level=0` 为待处理；1/2/3 为客户可见层级；6 为“其他”，不对客户展示。
- 非支持行业、原博正文缺失的转发、反解等失败都可回写 6；但运行审计会保留真实失败原因，不能仅以 level=6 判断业务分类成功。
- 转发：
  - 原博正文缺失 → 其他；
  - 模型明确判“异常” → 其他；
  - 正常 → 用“转发正文 + 原博正文”综合分类。
- 品牌词：
  - `hit_mid_tag` 能在任务 `brand_tag` JSON 中命中时，仅使用该命中品牌；
  - 缺失/无法反解析时，回退任务全部品牌词；
  - 不把“未命中品牌 tag”直接视作内容无关。

## 视频与缓存保护

- 视频先尝试抽帧；
- 抽帧文件超过 200 MB 或时长超过 300 秒，自动降级封面；
- frame 与 cover 都不可用时降级文本；
- 单条完成后删除当次图片、视频和帧；
- worker 启动时删除超过 24 小时的缓存。

## 日志、审计与保留

| 类型 | 路径 | 用途 | 默认保留 |
| --- | --- | --- | --- |
| 主运行日志 | `logs/<logger>.log` | 人工查看进度与告警 | 30 天，按天轮转 |
| 运行审计 | `logs/runs/YYYYMMDD/<run_id>.jsonl` | 每个 mid 的 task、结果、耗时、错误、模型输出 | 30 天，按天目录清理 |
| 运行汇总 | `logs/runs/YYYYMMDD/<run_id>_summary.json` | 任务级/运行级汇总 | 30 天 |
| MySQL 审计 | `nature_ad_mid_ai_audit` | 按 task/mid 查询历史 | 90 天，定时分批清理 |

MySQL 审计默认关闭，避免新正式库尚未创建审计表或当前账号无 INSERT 权限时干扰主链路。完成下列条件后，才将 `config.yaml` 的 `audit.mysql_enabled` 改为 `true`：

1. `nature_ad_mid_ai_audit` 已存在；
2. 当前正式账号具备该表的 INSERT 权限；
3. `python3 scripts/production_preflight.py --strict` 通过。

启用后，由 cron / XXL 每日低峰执行：

```bash
python3 scripts/cleanup_mysql_audit.py --execute
```

默认先运行不带 `--execute` 的命令可只读预览过期记录数。

## 本地磁盘保护与日志开关

- `storage.min_free_mb` 默认是 5120；可用空间低于阈值时，程序自动停止本地主日志、JSONL、错误汇总和媒体下载，控制台仍保留。
- 低磁盘时，图片/视频自动降级文本分类；已有临时媒体在单条结束和启动过期清理中删除。
- 若人工关闭本地落盘，将 `logging.file_enabled`、`logging.error_file_enabled`、`audit.local_enabled` 设为 `false`。此操作前必须确认 MySQL 审计已启用且可写，或已有外部日志平台，否则无法追溯 case。

完整的数据读写和排障入口见项目根目录 [README.md](../README.md)。

## 排障

- 回写超时：不要立刻认定失败。worker 会查询分表确认是否已从 level=0 写为目标 level。
- `data=0`：同样查询最终 level。目标 level 已存在则是幂等成功；否则算失败。
- 反解/模型失败：查对应 `run_id` 的 JSONL 中 `error_stage`、`error` 和 `model_output`。
- 回写失败：不应手工反复重跑整个任务。先定位审计记录，再用 `run_single_task.py --write-back` 做受控复测。
