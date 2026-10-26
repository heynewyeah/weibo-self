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
