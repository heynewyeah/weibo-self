# 假期值守：错误排查、修复与手动复测

本手册针对当前 MySQL → mid 反解 → 媒体处理 → 模型分类 → HTTP `update-level` 回写链路。以下命令均从服务器仓库根目录执行，以 `cd intent_behavior &&` 开头。执行前先确认服务器已部署预期版本、依赖已安装；排障时记录 `task_id`、`mid`、`run_id`、故障时间和当时的 `level`。

## 先确认影响范围

```bash
cd intent_behavior && sudo systemctl status intent-behavior-worker
cd intent_behavior && sudo journalctl -u intent-behavior-worker -n 100 --no-pager
cd intent_behavior && tail -n 100 logs/mysql_worker.log
```

如果不是 systemd 管理，跳过前两条，按实际启动方式查看进程和日志。空闲时 worker 默认每 30 分钟记录一次心跳；短时间没有新日志，不一定是进程停止。`logs/mysql_worker.log` 或审计文件不存在时，检查 `logging.file_enabled`、`audit.local_enabled` 和 `storage.min_free_mb`；磁盘空间不足时本地写盘会自动停止，仍可查看控制台或 `journalctl`。

从本地 JSONL 审计列出失败或中断记录（只读，需服务器安装 `jq`）：

```bash
cd intent_behavior && jq -r 'select(.event == "mid_processed" and (.status == "failed" or .status == "interrupted")) | [.event_time, .task_id, .mid, .status, .error_stage, .error, .writeback_state] | @tsv' logs/runs/*/*.jsonl
```

定位某个 mid 的完整记录；将示例数字替换为实际 mid：

```bash
cd intent_behavior && jq -c --arg mid '1234567890' 'select(.event == "mid_processed" and .mid == $mid)' logs/runs/*/*.jsonl
```

重点查看 `status`、`error_stage`、`error`、`model_output`、`media_type`、`forward_status`、`writeback_state`、`level`。`status=success` 也可能是业务上归“其他”（`level=6`）；`media_type=image_fallback_text` 或 `video_fallback_text` 表示媒体不可用后按文本处理，需核实结果是否仍可信。不要仅凭 `level=6` 认定模型调用失败。

## 按故障点排查

| 现象或审计字段 | 先查什么 | 处理方向 |
| --- | --- | --- |
| worker 不运行 / 没有待处理记录 | systemd 状态、空闲心跳、任务是否符合 `task_type=1` 及有效状态、任务 `operator_uid`、分表是否有 `level=0` | 运行只读预检；核实 `operator_uid % 20` 对应的分表和任务 ID。`active_task_limit` 默认 50，`fetch_limit_per_task` 默认 100。 |
| `error_stage=resolve` | mid、uid、`mid_resolver.url`、超时或 HTTP 错误 | 先确认反解服务和原始 mid；恢复后单条预演。技术失败保持 `level=0`。 |
| `error_stage=classify`，包含模型超时、HTTP 4xx/5xx、空 `choices`、标签提取失败 | 先跑模型连通性检查；核对 `api.url`、`api.model`、请求参数及模型原始输出 | 400 优先检查请求参数；超时/5xx 看服务状态与耗时；解析失败看 `model_output` 和提示词/标签规则。 |
| `image_fallback_text` / `video_fallback_text` | 反解出的 pid/fid、媒体下载日志、磁盘空间、视频抽帧与封面 | 下载或抽帧恢复后用单条预演复测，并比较媒体类型及层级。 |
| `error_stage=writeback`、接口超时或 `data=0` | 分表中该 `task_id + mid` 的最终 `level`，审计 `writeback_state` | 客户端超时不代表服务端没写入；先只读核对。已是目标 level 时无需再次回写。 |
| `status=interrupted` | 停服/重启时间、当前 `level` | 未完成的 mid 通常保持 `level=0`，恢复服务后可重新处理。 |
| 结果层级与预期不同，但请求成功 | 任务行业、命中品牌、话题、正文、媒体、转发原博正文、`model_output` | 用同一 `task_id + mid` 预演，区分上游数据变化、提示词规则和模型输出变化。 |

只读检查配置、分表、索引和重复记录：

```bash
cd intent_behavior && python3 scripts/production_preflight.py --strict
```

单独检查模型能否返回文字（**会调用模型接口**，不回写业务数据）：

```bash
cd intent_behavior && python3 check_model_service.py --timeout 30 --retries 0
```

## 手动复测一条错误 case

1. 从审计中确认 `task_id`、`mid`、原始 `level`、错误阶段和预期结果。若需查库，用 `operator_uid % 20` 找到 `nature_ad_super_mid_0` 到 `_19` 中的目标分表，只做按 `customer_id + super_task_id + mid` 精确查询；不要在数据库里直接改业务 `level`。
2. 用 `auto` 模式预演同一条 case。下面数字只是命令示例，执行前替换成要排查的真实值。这条命令**会读 MySQL、调用反解/媒体/模型接口并写本地日志/审计，不会回写业务 level**：

   ```bash
   cd intent_behavior && python3 scripts/manual_classify_mid.py --task-id 123456789 --mid 1234567890 --mode auto
   ```

3. 对照终端输出与新生成的 `logs/runs/YYYYMMDD/<run_id>.jsonl`：检查 `success`、`error_stage`、`layer`、`level`、`media_type`、`model_output`、`writeback_state`。脚本退出码 `0` 是处理成功、`1` 是处理失败；若 case 本来就预期技术失败，退出码 `1` 需要结合 `error_stage` 判断。
4. 只排查强制媒体路径时，可把 `--mode auto` 改成 `--mode image` 或 `--mode video`。目标 mid 没有对应媒体时，强制模式会报分类错误；不要把它当成自动模式的结果。

对于已回写的 `level=1/2/3/6`，仍可以执行不带 `--write-back` 的单条预演，比较新旧结果。对于仍是 `level=0` 且已确认要应用新结果的 case，才执行下面的**真实业务回写**命令；脚本会重新分类并通过 HTTP 接口更新该条记录：

```bash
cd intent_behavior && python3 scripts/manual_classify_mid.py --task-id 123456789 --mid 1234567890 --mode auto --write-back
```

历史版本误将技术失败或人工中止写成 `level=6`，且已核实需要纠正时，可以使用下面的**真实数据库修改和业务回写**命令。它先将该条 `6 → 0`，再重新分类和回写；若后续失败，记录可能停留在 `level=0`。脚本拒绝修改 `level=1/2/3`。

```bash
cd intent_behavior && python3 scripts/manual_classify_mid.py --task-id 123456789 --mid 1234567890 --mode auto --retry-level-6 --write-back
```

执行回写后再次精确查询该条分表记录的最终 `level`，并核对新审计的 `writeback_state`：`applied`、`already_applied` 或 `confirmed_after_transport_error` 表示回写成功。若 worker 同时处理，单条脚本会使用命名锁并复查 `level=0`；如果提示已被处理或非待处理，重新查最终状态，不要强行覆盖。

### 建议人工复测的错误 case

以下场景使用已有故障记录或隔离测试环境中的样本。每条先运行不带 `--write-back` 的单条预演，记录预期与实际；不要为制造故障而停止生产模型服务、改写正式库数据或配置。

| Case | 样本选择 | 预期检查点 |
| --- | --- | --- |
| 反解失败 | 审计中 `error_stage=resolve` 的真实 mid | `success=false`、`error_stage=resolve`；服务恢复后同一 mid 可重测，失败时业务 level 不被改写。 |
| 模型拒绝或超时 | 审计中 `error_stage=classify` 且 `error` 包含模型 HTTP 错误或超时 | `check_model_service.py` 与单条预演能区分服务故障和个别请求参数问题；失败时 `writeback_state` 应是 `skipped_technical_failure`。 |
| 图片或视频降级 | 审计中 `media_type` 为 `image_fallback_text` / `video_fallback_text` 的真实 mid | 核对媒体服务恢复后是否走 `image` / `video_frame` / `video_cover`，并比较层级。 |
| 转发结果异常 | 有 `forward_mid` 和原博正文的真实 mid | 对照 `forward_status`、转发正文、原博正文及 `model_output`；缺少原博正文或明确异常时可业务归“其他”。 |
| 回写响应不确定 | 审计中 `error_stage=writeback` 或 `writeback_state=confirmed_after_transport_error` 的真实 mid | 先查询分表最终 level，再判断是否需要复测；不能只根据客户端超时再次回写。 |
| 已有业务结果复核 | 当前 level 为 1/2/3/6 的争议 mid | 只做不带 `--write-back` 的预演，比较原审计和新结果；脚本不会覆盖既有 1/2/3。 |

无需连接生产服务即可手动执行故障保护用例（模拟 400、模型失败、回写超时、`data=0` 和中断）：

```bash
cd intent_behavior && python3 -m unittest tests.test_production_guards.ProductionGuardTests.test_model_http_400_is_not_retried_and_preserves_response_detail tests.test_production_guards.ProductionGuardTests.test_model_failure_keeps_level_zero_without_writeback tests.test_production_guards.ProductionGuardTests.test_update_timeout_is_success_only_when_confirmed tests.test_production_guards.ProductionGuardTests.test_update_data_zero_is_success_only_when_confirmed tests.test_production_guards.ProductionGuardTests.test_keyboard_interrupt_is_not_fallback_level_six -v
```

## 改配置或代码后的回归顺序

先根据证据找修改点，不要用调大重试次数掩盖持续性错误：

| 证据 | 主要检查位置 |
| --- | --- |
| 模型地址、超时、参数 | `config/config.yaml` 的 `api`；`src/api_client.py` 的请求体、响应解析和重试 |
| 反解响应或字段异常 | `config/config.yaml` 的 `mid_resolver`；`src/mid_resolver.py` |
| 图片/视频降级、下载和抽帧 | `config/config.yaml` 的 `media`；`src/media_handler.py`；`src/classifier.py` |
| 层级、行业、品牌、话题或转发判定错误 | `config/config.yaml` 的 `classification` / `prompts`；`src/classifier.py`；`src/db_client.py` 的任务字段映射 |
| 回写 HTTP 错误或落库状态不明 | `config/config.yaml` 的 `result_writer`；`src/result_writer.py`；`src/db_client.py` 的 `update_level_result()` |
| 任务没进入消费队列 | `src/db_client.py` 的任务筛选与分表查询；`src/worker.py` 的轮询和锁 |

1. 保存故障证据和预演结果，确认修改属于配置、上游数据还是项目代码。服务地址变更更新 `config/config.yaml` 的 `api.url`，并同步相关测试默认值与文档；提示词/标签变更先核实行业及原始模型输出。
2. 对模型请求体或接口地址的修改，先跑离线参数测试，再跑真实接口测试；对分类/回写逻辑修改，跑生产保护测试。离线测试不调用外部服务；带 `--live` 的测试会真实调用模型。

   ```bash
   cd intent_behavior && python3 -m unittest tests.new_request_modal_test.test_api_client_endpoints -v
   cd intent_behavior && python3 -m unittest tests.test_production_guards -v
   cd intent_behavior && python3 tests/new_request_modal_test/test_api_client_endpoints.py --live
   ```

3. 用本节的单条预演命令重测原错误 case，确认输出符合预期。按仓库约定只提交本次相关文件并推送；服务器部署到该提交后按既有方式重启服务，再检查 worker 状态、日志与新审计。正式持续消费只使用 `worker.py`；`worker.py --once` 会处理一轮所有有效任务下的待处理记录并**真实回写**，不能当成单 case 测试。

内置模型业务样例可用 `python3 tests/new_request_modal_test/test_model_cases.py --only '关键词'` 运行；它会真实调用模型、写本地测试报告，但不读写业务分表。`tests/09_edge_cases/test_edge_cases.py` 依赖预先准备的特例数据库记录和预期文件，不要直接在正式库批量造假 mid 运行。

详细生产启停和日志保留规则见 [production_runbook.md](production_runbook.md)。
