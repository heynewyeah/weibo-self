# 固定汽车任务 888888 独立运行

普通任务继续使用原来的 `worker.py`。特殊任务使用 `worker_special_task.py`，两者可以分开启动、停止、查看日志。当前仅支持服务器运行；DataWorks 适配另行实施。

## 处理范围

- 不查询 `super_mid_task`，不需要任务表的 `operator_uid`、品牌词或行业配置。
- 固定扫描 `nature_ad_super_mid_0` 至 `_19`，只选择 `super_task_id=888888 AND level=0`。
- 固定汽车行业；品牌词和任务话题词为空，保留明细中的发博账号、转发信息、mid、uid 等上下文。
- 复用普通任务的汽车提示词、反解、多模态分类和 HTTP 回写。请求中的 `customer_id` 来自每条明细，`task_id` 固定为 888888；不直接 UPDATE 业务表。
- 特殊任务不使用 `customer_id % 20` 路由或过滤。扫描时记录实际来源分表，处理前的 level 检查及回写后确认均查原表的 `id + super_task_id + mid`；不查询任务表，也不凭 customer_id 猜表。明细的有效 `customer_id` 仅按现有 HTTP 接口要求原样传递，不伪造客户 ID。
- 每次特殊任务回写后必须查回实际来源表确认目标 level，即使 HTTP 返回 `data=1` 也不例外；来源表没有更新就明确报错，不扩大测试范围。
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

## 首轮测试库预演结果（2026-10-26，按当时的取模保护规则）

在 `10.194.68.7` 的 `ads_fst` 用户下，继续使用 `/tmp/special-888888-preview.bnFqas` 隔离副本。测试库连接配置仅在进程内覆盖；关闭 HTTP 回写客户端和 MySQL 审计，不修改原项目及正式配置，不启动长期消费者。

### 数据范围核对

| 分表 | 任务 888888 的 level=0 条数 | 路由核对 |
| --- | ---: | --- |
| `nature_ad_super_mid_0` | 32 | `customer_id=2608812382`，取模为 2，不等于实际表号 0；被保护逻辑拦截，不分类、不回写 |
| `nature_ad_super_mid_1` | 34 | `customer_id=2608812381`，取模为 1，可正常预演 |
| `_2` 至 `_19` | 不适用 | 测试库不存在这些分表；这不代表这些表的待处理数量为 0 |

合计发现 66 条待分类记录，34 条符合当前路由保护，32 条需要确认测试数据的放置方式。未移动数据、未修改 customer_id，也未放宽生产路由保护。

### 真实模型预演

先预演 1 条，再预演前 5 条；两轮包含重复的首条，合计 5 条不同样本，不是 6 条。以下为第二轮结果，全部成功、汽车行业、品牌词为空，审计 `writeback_state=not_requested`：

| 明细 id | mid | 实际媒体处理方式 | 模型分层 | 查库最终 level |
| ---: | --- | --- | --- | ---: |
| 2344 | 5280999766230642 | image | 1（认知层） | 0 |
| 2345 | 5280845579159937 | text | 2（兴趣层） | 0 |
| 2349 | 5326869998339938 | video_frame | 2（兴趣层） | 0 |
| 2350 | 5326868404506835 | video_cover | 2（兴趣层） | 0 |
| 2351 | 5326680019175634 | video_frame | 1（认知层） | 0 |

- id=2350 的视频下载超过现有 200MB 上限，沿用原链路降级到封面，最终分类成功；该条总耗时约 46.6 秒。这是媒体降级，不是技术失败强行写 level=6。
- 预演后再次查库：`_0` 仍为 32 条 level=0，`_1` 仍为 34 条 level=0；以上 5 条逐条核对均保持 level=0。
- 本地离线回归 50 项通过；服务器隔离副本新增测试 14 项通过。预演成功只证明真实服务链路可运行，尚不代表分类准确率经过人工标注评估，也不证明真实回写成功。
- 服务器审计目录：`/tmp/special-888888-preview.bnFqas/logs/test_20261026/special_888888/runs/20261026/`，第二轮审计文件 `20261026_100553_2166171_73ea7866e2e2.jsonl`。

### 回写接口与下一步

Git 历史确认：2026-09-16 的 `0d97a73` 将回写地址从 `http://10.133.6.162:8058/api/v1/super-mid/update-level` 换成 `http://terra.biz.weibo.com/api/v1/super-mid/update-level`；新增特殊入口的 `80bc219` 没有修改这个地址。测试仅在进程内禁用回写，没有修改配置文件中的地址。

当时尚未确认 HTTP 服务是否把任务 888888 写入本测试库，因此仅做预演。随后用户明确：888888 不遵循 customer_id 取模分表规则，并授权向现有地址进行测试回写；只有测试通过后才切换真实库。下节记录这次规则修正，不能将上表的旧保护结论当作当前规则。

## 特殊任务按实际来源表定位（2026-10-26）

- 仅修改特殊入口：`SpecialMidRecord.source_shard` 来自扫描循环的固定表号，不从数据库字符串或 customer_id 构造任意表名。
- 分表扫描仍固定 0～19，且只选择任务 888888 的 level=0。level 二次检查、回写后的幂等/超时确认都查实际来源表；普通 Worker 的取模规则不变。
- 特殊任务重试键包含来源分表，避免不同表中相同明细 id/博文共用重试预算；普通记录的历史重试键保持不变。
- HTTP 地址、payload 字段保持原协议；每次成功响应都额外查原表验证。如果接口仍按 customer_id 定位，导致原表没有更新，则认定测试未通过，不自动切换真实库。
- 本地回归 56 项通过，服务器隔离副本专项测试 20 项通过。真实测试回写结果另行记录，不将单元测试通过当作回写成功。

### 本次真实回写未通过：未切换真实库

2026-10-26 10:23，在测试库对原来被拦截的 `_0` 表样本完成预演，然后执行单轮、单条真实回写：

- 实际来源 `nature_ad_super_mid_0`，明细 id=222，mid=`5326869998339938`，明细 customer_id=`2608812382`，任务=888888；视频抽帧分类成功，目标 level=2。
- 使用原地址 `http://terra.biz.weibo.com/api/v1/super-mid/update-level`。请求关键字段为 `customer_id=2608812382, task_id=888888, mid="5326869998339938", level=2`，另按原协议带 update_time；没有改字段、伪造 customer_id 或直接执行 UPDATE。
- 回写响应为 `{"data":0,"message":"成功","code":0,"payload":null,"header":null}`。其中 message 不是落库证明；data=0 且查询实际来源表仍为 level=0，因此按回写失败处理。
- 随后独立只读查库，id=222 仍为 level=0；`_1` 表中同 mid 的 id=2349 也仍为 level=0。两个表合计仍为 66 条 level=0。客户端来源表定位修改已生效，20 表扫描现在能取到全部 66 条测试待处理记录（`_0` 32 条、`_1` 34 条），不再因为 customer_id 取模过滤 `_0`。
- 回写前只读检查正式配置连接的 20 表，未找到同一 task_id+mid；这个检查不证明 HTTP 服务连接哪一个库。现有证据不能区分“服务端仍按取模定位”和“HTTP 服务未连接该测试库”等原因，需要服务端确认实际连接与定位逻辑。
- 仅发起一次真实回写，未自动重复、未扩大测试批次、未切换生产数据、未启动长期消费者。失败预算保留在隔离副本 `logs/source_routing_20261026/special_888888/failures/`，不影响正式 Worker。
- 失败审计文件：隔离副本 `logs/source_routing_20261026/special_888888/runs/20261026/20261026_102310_2175247_13a71bdb4199.jsonl`；预演文件为同目录 `20261026_102304_2175247_6d451f886161.jsonl`。

上述回写失败只描述“旧测试库样本 + 当前接口”这一组合，不能据此认定服务端特殊任务路由有问题。随后用户明确当前接口应写新库，最新核对与下一步以下节为准。

## 新接口与新库配对核对（2026-10-26 11:23）

用户明确当前 `terra.biz.weibo.com` 回写接口对应新库，而不是 `10.79.104.30` 测试库。因此验证方式改为“从新库读取 888888 → 分类 → 当前接口回写 → 查询同一新库来源表”，不再用旧测试库样本验证新接口能否更新旧库。

- 在服务器 `ads_fst` 用户下读取实际项目 `config/config.yaml`：当前默认 MySQL 已是 `s9671i.hebe.grid.sina.com.cn:9671/clue_collect_common`，账号 `xuanyu11_r`；回写地址仍为 `http://terra.biz.weibo.com/api/v1/super-mid/update-level`。原配置本来就指向新库，无需把公共配置再改一次。
- 实际连库成功，完整检查新库 20 个分表：所有 level 中均无 `super_task_id=888888` 记录；之前的测试 mid=`5326869998339938` 在该任务下也不存在。不是仅 level=0 为零，也没有跳过缺失分表。
- 这与“旧测试数据不在新接口目标库”相符，可解释此前 data=0。数据库查询本身不能直接证明 HTTP 服务的实际后端连接；本次未检查服务端源码或配置，仍需新库正向样本完成端到端证明。
- 使用服务器已同步的实际项目代码及默认新库/新接口配置执行 `--dry-run --limit 1`；只把日志目录临时隔离到 `/tmp/special-888888-preview.bnFqas/logs/new_database_20261026/`，没有覆盖 MySQL 配置，没有发起额外回写。11:24:55 预演启动后正常结束，退出码 0，汇总 `shards=20, pending=0, success=0, fail=0, dry_run=true`。
- 缺少新库 888888 样本，不能把空轮成功当作真实回写验证通过。未复制或创建生产数据、未启动持续消费者、未修改普通 Worker 或 DataWorks。

下一步：在新库准备至少一条真实的 `super_task_id=888888 AND level=0` 明细，或由用户提供该批数据的正确目标库配置，再执行单条分类/HTTP 回写/原表查库验证。只有确实落库后，才扩大真实处理范围。

## 测试库与两个回写接口的对照验证（2026-10-26 14:33）

用户授权两个接口都测试后，在 `10.194.68.7` 的 `ads_fst` 用户下，使用服务器实际仓库中的特殊 Worker 完成真实反解、汽车分类、HTTP 回写及来源表确认。测试数据库和接口只在进程内覆盖；没有修改正式配置。每个案例使用独立日志和重试状态，避免前次失败预算影响对照结果。

测试库为 `10.79.104.30:3306/clue_collect_common`。下表全部为任务 888888：

| 接口 | 来源表 / id / mid | 目标 level | HTTP 响应 | 原表 level 前后 | 结果 |
| --- | --- | ---: | --- | --- | --- |
| 新域名 `terra.biz.weibo.com` | `_1` / 2344 / 5280999766230642 | 1 | code=0，data=0，message=成功 | 0 → 0 | 未更新测试库，记为回写失败 |
| 旧地址 `10.133.6.162:8058` | `_1` / 2344 / 5280999766230642 | 1 | code=0，data=1，message=成功 | 0 → 1 | 成功，审计 applied |
| 旧地址 `10.133.6.162:8058` | `_0` / 222 / 5326869998339938 | 2 | code=0，data=1，message=成功 | 0 → 2 | 成功，审计 applied |

两个接口路径均为 `/api/v1/super-mid/update-level`。共发起三次 HTTP 回写尝试，更新两条不同的测试记录；没有直接 SQL UPDATE、重置 level 或重复批量消费。第二次独立连接数据库核对，id=2344 仍为 level=1、id=222 仍为 level=2。

- `_0` 样本的明细 customer_id 仍为 2608812382，取模为 2，但旧接口成功更新了实际 `_0` 表。因此不能将此前失败归因于这条特殊样本的取模位置；本次对照证明旧接口能够更新所提供的测试库，而新域名没有更新同一测试目标，与此前接口和数据库环境未配对的解释一致。
- 测试结束时，`_0` 共 32 条（31 条 level=0、1 条 level=2），`_1` 共 36 条（35 条 level=0、1 条 level=1）。相较上午，`_1` 已新增两条记录，本次测试没有创建这些记录。剩余待分类为 66 条。
- 再次完整查询正式配置连接的新库 20 表，任务 888888 在所有 level 中仍为零条。正式配置保持新库 `s9671i.hebe.grid.sina.com.cn:9671` 和新域名；没有正向样本，尚不能证明正式环境的真实回写成功。
- 本次验证通过的是两条测试样本的完整链路，不等于分类准确率评估或全量测试通过。未启动长期 Worker，未修改普通任务或 DataWorks。
- 服务器测试日志和审计根目录：`/tmp/special-888888-paired-c7l_3z86/`，三个案例分别在 `new_source1`、`old_source1`、`old_source0` 下的 `special_888888/`。临时目录可能被系统清理，关键响应与查库结果已记录在本节。

后续正式验证仍按单条执行：正式库出现任务 888888 的 level=0 样本后，先预演，再真实回写并核对来源表。当前无需再次切换公共配置。

## 独立 DataWorks 发布（2026-10-26）

特殊任务使用独立工作流和自定义Shell节点 `intent_behavior_special_888888`，不加入普通工作流，
不更新普通 Worker 的已部署源码包、Shell、调度或重试状态。正式配置保持上文的新库和新域名。
测试库两条样本已完成分类、HTTP回写和独立查库；这不等于新库已有真实回写样本。

### 发布与目录隔离

- 源码发布包和完整环境包均包含 `worker_special_task.py`、`src/special_task_worker.py`。
- `dataworks_cshell_entry.sh special` 通过 `run_dataworks.sh --special` 启动监督器 `--entry special`，
  监督器只启动 `worker_special_task.py`；普通 `worker` 模式默认值保持原样。
- 特殊任务本地日志为 `logs/special_888888/special_worker.log`，心跳为同目录 `live_worker.json`，
  审计为 `runs/`，重试及完整失败档案为 `failures/`；媒体缓存为 `output/.cache_special_888888/`。
- 设置 `DATAWORKS_DATA_ROOT` 为原个人项目的 `.../intent_behavior/dataworks` 后，特殊模式自动追加
  `/special_888888`。独立HDFS根为 `viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks/special_888888`，
  其下 `logs/status.json`、`logs/*.tar.gz`、`state/retry_state.sqlite3` 均独立。
  特殊监督器拒绝普通 HDFS 路径，消费前恢复独立重试状态，归档失败的保护沿用普通入口。
- `special-monitor` 只读取特殊HDFS根，不连接业务数据库、不分类、不回写。普通 `monitor` 的默认根不变。

在服务器仓库根目录构建源码包（仅本地打包，不访问业务服务、不上传）：

```bash
cd intent_behavior && python3 scripts/build_dataworks_release.py --prefix intent_behavior_special_888888_v1 --output output/dataworks/intent_behavior_special_888888_20261026.zip
```

将该ZIP上传到**新特殊任务节点**的依赖包，Python环境继续复用已验证的HDFS runtime包。
发布包包含业务配置，只用于已授权的本项目DataWorks任务，不提交ZIP到Git。
网页正式Shell（会持续分类并真实HTTP回写）：

```bash
#!/bin/bash
set -euo pipefail
export DATAWORKS_DATA_ROOT='viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks'
exec bash intent_behavior_special_888888_v1/app/scripts/dataworks_cshell_entry.sh special
```

环境检查时把末尾 `special` 改为 `check`（不分类、不回写）；单条预演可用
`special --dry-run --limit 1`（调用真实反解/模型，不回写，不恢复或上传正式重试预算）。
正式常驻入口不附加日期参数。新工作流建议Cron `0 0/5 * * * ?`、检查未完成实例=是、
提交失败重试=串行；节点超时kill=1430分钟、节点自动重试关闭，沿用已验证的平台轮换方式。
同一特殊工作流仅保留一个正式消费节点，监控独立单次执行。

服务器具备HDFS客户端及权限时，只读持续监控（Ctrl+C只退出查看）：

```bash
cd intent_behavior && python3 scripts/dataworks_monitor.py --hdfs-root viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks/special_888888 --watch --interval 30
```

### 实际部署与实机核验（2026-10-26 15:46）

- 新工作流：`原生内容站特殊任务888888`，ID `fd119871-a55a-48cf-90e4-21939621abb0`。
  [工作流与运行历史](http://dataworks.sina.com.cn/#/workflow/detail?projID=fd119871-a55a-48cf-90e4-21939621abb0&projName=原生内容站特殊任务888888&tab=runHistory)。
- 工作流只有一个消费节点 `intent_behavior_special_888888`，中文名称 `原生内容站特殊汽车任务888888`；
  运行用户 `ads_fst`，运行环境 `ext_hadoop-2.7.3_hive-2.3.7_spark-3.2.0_auron-7.0(推荐)`。
- 已保存并刷新确认工作流**已上线**：Cron `0 0/5 * * * ?`、检查未完成实例=是、
  调度提交失败重试=串行、代码类型=无、全局参数 `{}`。节点超时kill=1430分钟，节点自动重试关闭。
  该设置沿用普通任务的平台轮换方式；本次没有等待满1430分钟，不能将配置核对当作新节点超时轮换已实测。
- 上传包 `intent_behavior_special_888888_20261026.zip`，大小127984字节，代码基线 `192dada4be4dde6a76063099f574bfc3714cfc0a`。
  SHA256：`cd2b0191c1b73b50ce1a74eb38e11179d6a9cf15fb11cd194966861ff53eff4e`。
  依赖资源：`hdfs://wbml-streaming-ns/wbml_data/waic_scheduler/resource/226/202610/1793000170609/intent_behavior_special_888888_20261026.zip`。
  使用上面的正式Shell，仅新特殊节点引用此包；普通Worker仍引用原发布包，未修改普通工作流。
- 首个工作流实例 `4C7BD018-6631-B044-70E6-4DA8CCDB2DE7`，节点实例
  `AB0B833F-DB1D-9DD8-84CA-D61D8E1992BC`，2026-10-26 15:38:13手动提交，运行主机 `10.185.13.63`。
  15:46核对运行历史仍为共1条、状态进行中，跨过15:45调度点没有新增重叠实例。
- 容器内Python3.11.17及离线依赖校验通过，`DATAWORKS_RUNTIME.ok=true`、`video_roundtrip=true`。
  启动日志确认入口为888888，已获取独立 `logs/special_888888/worker.lock`，每轮间隔10秒。
- 只读查看独立 `live_worker.json`：状态running，30轮完成，每轮 `tasks=1, shards=20, pending=0`，
  累计成功、降级、失败、跳过均为0，`dry_run=false`。这证明正式库扫描正常，不证明新的真实分类或回写发生。
- 标准输出首先出现 `DATAWORKS_RETRY_STATE_NEW`，随后多次 `DATAWORKS_RETRY_STATE_SYNCED`、
  `DATAWORKS_LOG_SYNC_OK` 和running心跳。日志归档位于独立HDFS根下
  `logs/20261026_153852_910311800fab4d6d8443bc25243560fa.tar.gz`，重试状态存入独立 `state/retry_state.sqlite3`。
  本地 `logs/` 只产生 `special_888888/`；没有恢复普通Worker的历史重试预算。
- 本地74项离线回归通过，覆盖特殊任务、发布包、监督器、监控、重试、生产保护及普通入口兼容；
  Bash语法和Git差异格式检查通过。本次未重复处理已完成的测试库样本、未创建或修改生产样本。

平台任务已启动，无需在旧服务器再启动第二个特殊Worker。查看使用上面的只读监控命令；
源代码提交未自动push，平台使用的是本次已上传且校验过的发布包。
