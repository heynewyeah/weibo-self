# Dataworks 迁移记录（2026-10-16）

## 当前状态（2026-10-20 已配置平台实例轮换）

用户确认旧 worker 已停止，随后要求跳过现有反解失败样本的回写验证，继续迁移并实现随时监控。
正式工作流已保存为“已上线”，Cron 为 `0 0/5 * * * ?`，检查上一个实例未完成=是，提交失败重试=串行。
正式任务的超时kill现为1430分钟（23小时50分）；用户接受按运行时长轮换及数分钟消费空窗，不再要求固定零点。
11:33旧实例已于14:44停止；个人目录迁移后使用修复版启动，15:13节点已恢复4条重试状态并向新目录发布运行心跳。
本次待处理记录反解返回30004，未发生真实回写；不能把运行心跳视为业务回写已经验证。

工作流：原生内容站博文分类，ID `928f2456-7163-4191-87bb-4d63fad55cd0`。
正式自定义Shell：`intent_behavior_worker`，ID `10B7820D-FD00-D14F-33EB-185B78DDA5D0`。
运行用户 `ads_fst`，环境 `ext_hadoop-2.7.3_hive-2.3.7_spark-3.2.0_auron-7.0`，超时kill=1430分钟。
DAG 已移除旧普通 SHELL 和诊断节点，仅保留正式 worker；诊断定义仍可单独运行。

### 平台实例轮换（10月20日）

- 用户要求解决单个实例持续超过24小时的告警；仅重启实例内Python进程不能满足要求，该方案未部署。
- 参考工作流“0922生成训练上传”为大图调度，首节点启动时间9:00、超时kill=0。
  10月20日09:00:56开始、12:44:20完成；它每天新建实例，但没有替换常驻旧实例的配置证据。
- 用户确认采用平台方案：正式自定义Shell的“任务运行设置 → 超时kill时长”保存为1430分钟，
  工作流维持在线、Cron每5分钟、检查上一个实例未完成=是、提交失败重试=串行。
  工作流超时告警仍关闭；节点自动重试仍关闭。超时终止后由Cron创建新实例。
- 保存配置后，旧节点 `F0F56582-56E8-97B9-4F7D-8E5D7F301748` 在17:34:06收到终止信号，
  平台记录结束时间17:34:17、状态失败；本次没有手动点击停止或提交新Worker。
- Cron在17:35:06自动创建工作流实例 `91B91DCE-A0B7-DD08-BBDF-C9D206127668`，参数含
  `cron=1`、`cron_time=20261020173500`。仅1个正式节点实例
  `4BC99D0A-110B-D042-3D42-9BAD2B7CC4E8`，主机10.185.13.61。
- 新实例使用原 `personal_v2` 源码包，Python3.11.17及依赖校验通过；日志确认
  `DATAWORKS_RETRY_STATE_RESTORED rows=4`、`exhausted=4`、HDFS心跳running，归档位于个人项目目录。
- 1430分钟是相对启动时长，不能保证固定零点轮换；实际终止与下一次启动还取决于平台检测、队列及环境准备。
  本次已观察到旧实例结束后Cron自动恢复，未等待新实例完整运行1430分钟再次触发。
  超时kill在平台上可能显示失败，应结合运行时长判断；不能把失败状态一概视为业务分类失败。

### 已完成的实机证据

- MySQL 仍使用 `xuanyu11_r`；工单 51328 新增来源 `10.25.1.%` 和 `10.185.13.%`。
  两个运行池均已连接成功，Linux 用户名与 MySQL 账号独立。
- 2026-10-16 实例 `A41EDB63-7832-881B-FED2-DBF598643ACB` 在 `10.25.1.227` 完成一条真实图文分类，
  `write_back=false`，未修改业务等级。18 个任务待处理查询合计 104.1ms，单次最慢 7.7ms。
- 检查范围内使用现有索引 `idx_customer_super_task_id`，EXPLAIN 最大估计 222 行。
  严格预检仍提示 20 张分表缺少建议的四列索引；未变更数据库结构，也未把此警告改成通过。
- 最初 HDFS 可写根为 `viewfs://c9/user_ext/ads_fst/intent_behavior_dataworks`；当前日志和状态已迁到下文的个人项目目录。
  已实测归档上传、下载和解包；真实图文审计在 `validation/dry_run_8f2b876.tar.gz`。
- 当前部署包必须包含 10 月 19 日新增的重试预算和完整失败档案逻辑，不能继续使用旧 `8f2b876` 包。
- 离线保护测试共56项通过，包含心跳过期识别、监控只读查询、完整失败档案、重试预算恢复和生产保护。
- 10月19日正式工作流实例 `18A52017-96A0-43F9-D55B-5B103520C463`，节点实例
  `B1EC3AE4-615F-CDF1-1F59-1A887C96F2CA`，主机 `10.25.1.228`。
  环境包从原预检资源复制到自有HDFS runtime目录并校验成功；Python3.11.17与依赖检查通过。
  观察到 `DATAWORKS_RETRY_STATE_NEW`、`DATAWORKS_RETRY_STATE_SYNCED` 和周期 `DATAWORKS_HEARTBEAT`。
- 只读监控实例 `6C970288-6A3A-CE04-370A-4B8F602AAA2C` 在 `10.185.13.61` 返回代码0。
  实际显示心跳距今31.8秒、轮询34轮、18个任务、累计8次反解失败尝试、4条等待重试，并打印最近20行日志。
  启动脚本已修复容器旧版Bash对 `set -u` 下空数组的兼容问题。
- 重新读取工作流配置确认已上线；查看运行历史时只有11:33:34这一条进行中实例，未见重复worker。
  尚未主动验证强杀后的自动恢复、平台最长运行时长及真实回写成功。

### 个人项目目录迁移（10月19日下午）

- 用户指定项目目录 `/dw_ext/ad/person/xuanyu11/intent_behavior`。以 `ads_fst` 实际创建、写入、重命名、读回均成功；未更改目录权限。
- 用户明确允许短暂停顿后，暂停Cron，停止旧节点 `B1EC3AE4-615F-CDF1-1F59-1A887C96F2CA`（14:44:09结束）。
- 最终迁移实例 `CE04F665-3CBA-DE11-2BC8-2DEB80460C7F`（10.185.13.63，14:45:18–14:46:18）返回0。
  日志逐文件SHA256、tar目录校验、SQLite quick_check通过，重试状态为 `exhausted=4`。
  整个暂存目录原子重命名为项目下 `dataworks/`；旧根及在线预复制副本均保留。
- 首次新实例 `5F01B3CA-F794-ABEB-99DB-51313BCD031D` 启动时发现 `/tmp` 与任务工作目录分属不同挂载，
  `os.replace` 返回 EXDEV。已改为在重试数据库同目录创建临时文件，并加入模拟跨挂载的恢复测试，11项相关测试通过。
- 修复版工作流实例 `BFAB500C-B293-35B0-1BD7-EFCD594276F4`，节点 `F0F56582-56E8-97B9-4F7D-8E5D7F301748`，
  主机10.185.13.62，15:13:11启动。实机确认 `DATAWORKS_RETRY_STATE_RESTORED rows=4`，随后向新目录同步日志和状态，心跳running、exhausted=4。
- 工作流级代码类型改为“无”，正式程序使用已上传依赖包；移除自动遗留的phab初始化节点，当前实例中该无用节点已跳过。
- 新目录只读监控实例 `7BE654B8-FB77-6E8B-A6B6-EBBAFE1275BC`（10.185.13.61，15:18:15–15:19:05）返回0：心跳72.2秒、轮询21轮、18个任务、exhausted=4，归档指向个人目录。
- 完成后重新加载页面确认“已上线”、Cron `0 0/5 * * * ?`、未完成实例检查=是、提交失败串行重试；DAG任务列表仅1条正式Worker。当前15:13工作流实例恢复为进行中。
- 回退时保留当前重试预算：先停当前Worker，把个人目录的最新 `state/retry_state.sqlite3` 同步到旧根，再使用旧入口启动；不要直接拿旧状态覆盖新状态。

### 正式 Shell

```bash
#!/bin/bash
set -euo pipefail
export DATAWORKS_DATA_ROOT='viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks'
exec bash intent_behavior_personal_v2/app/scripts/dataworks_cshell_entry.sh worker
```

正式节点当前源码包 `intent_behavior_personal_v2_20261019.zip`，基于提交 `9f9f489`；
SHA256 `9516e50edb168a2faf6adc6066d7cca9ebcf7b24f1d4029640bc6f99ec5f9e6e`。
启动器使用新源码，但Python离线环境从自有HDFS旧服务根 `runtime/environment_20261016.zip` 下载；
环境SHA256 `7c4446c525d3edb6b2b403a9e207e283ea80935c6e29d26b5bb857e2e9d1cfd4`。
旧8f2b876依赖引用仍显示在页面，实际文件不存在且未使用；当前启动成功不依赖该文件。
后续不要删除原预检环境资源或自有runtime副本来清理日志。

### 随时查看监控

[打开独立运行监控任务](http://dataworks.sina.com.cn/#/machine-learning/cshell/task/detail?defID=1BC74F3E-5DD3-E49A-2588-6C5955B59203&projectID=62607430-a984-423a-b94d-214393878a7f&type=cshell)。
中文名“原生内容站运行监控”，内部名保留 `intent_behavior_dataworks_probe`，位于默认公共项目，未接入正式DAG。
操作：单次提交（日期无需调整）→运行历史→最新实例→正常输出，滚动到日志末尾。
每次提交读取当前HDFS状态；以前已完成的监控实例是历史快照，不会随worker更新。

可查看心跳、主机/PID、累计处理尝试、最近轮询、等待/耗尽重试数、最近错误和日志尾部。
心跳目标60秒更新（加归档耗时），180秒未更新标记过期；累计数是尝试次数，不是唯一博文数。
提交弹窗“自定义环境变量”添加 `MONITOR_MID=<博文MID>`，可查看完整失败档案；
可另加 `MONITOR_TASK_ID=<任务ID>`。不传MID时显示运行概况。
监控不连接业务MySQL、不调用模型、不回写；启动器会准备离线环境并检查运行环境。

监控节点使用兼容修复提交 `198562f` 的包 `intent_behavior_release_monitor_v2_20261019.zip`，
ZIP目录为 `intent_behavior_release_v2`（避免与旧包解压目录冲突），SHA256为
`6e601e485b3f43e4647bfdeec5c2fc17b447162ae56530679aab0802aca53c66`。
网页Shell为：

```bash
#!/bin/bash
set -euo pipefail
exec bash intent_behavior_release_v2/app/scripts/dataworks_cshell_entry.sh monitor --hdfs-root viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks
```

当前日志和重试状态根目录 `viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks`：

- `logs/status.json`：最新状态，周期覆盖。
- `logs/20261019_151407_4cc9d94d2de94dacba843adb701dbb41.tar.gz`：15:13实例完整日志归档，周期覆盖。
- `logs/20261019_113416_fa0df2c2730c49d4ada5897b6cc4bf32.tar.gz`：迁移保留的旧实例日志归档。
- `state/retry_state.sqlite3`：一致的重试状态副本，新容器消费前恢复。

离线环境仍从旧服务目录 `viewfs://c9/user_ext/ads_fst/intent_behavior_dataworks/runtime/environment_20261016.zip` 下载。
这次仅迁移日志和状态，个人项目原有 `data/`、`output/` 未修改。

服务器具备HDFS客户端和 `ads_fst` 权限、仓库已更新时，新的执行命令（只读，Ctrl+C只退出查看）：

```bash
cd intent_behavior && python3 scripts/dataworks_monitor.py --hdfs-root viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks --watch --interval 30
```

按MID查询（只读，把MID替换为目标值）：

```bash
cd intent_behavior && python3 scripts/dataworks_monitor.py --hdfs-root viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks --mid 5354439580059623
```

已有日报/周报/月报的cron仍读旧服务器本地审计目录，尚未接入HDFS新归档；不要用它判断迁移后完整处理量。
当前上线的是持续消费、实时查看和失败档案查询；报告发送链路未迁移，未发送任何消息。

重试预算通过 SQLite backup 保存一致副本；新容器启动前从 HDFS 恢复，重建失败档案 JSON 并修正旧容器绝对路径。
周期同步及正常退出会保存预算。主机崩溃或强制 kill 仍可能丢失最后一个同步周期内的预算，不能声称跨崩溃绝对只重试 3 次。
以下保留历史诊断和部署说明；其中早期 1045 和 HDFS 路径失败已由上述证据解决。

## 目标平台实测

- 自定义Shell实际调到主机 `10.25.1.228`、`10.185.13.63`，不是普通 SHELL 的 docker_ceph 路径。
- 原生 PATH 只有 Python 2.7.5，没有 python3/pip/conda；Linux x86_64，glibc 2.17。
- 依赖 ZIP 自动下载并解压到当前任务目录。当前目录可写，但平台明确提示任务结束后自动清理。
- Python 官方独立运行包 + 10 个 wheel 完全离线安装成功；Python 3.11.17、
  PyYAML、requests、PyMySQL、NumPy、opencv-python-headless、tzdata 均已加载；
  合成视频编码再解码通过，实例 `432A4C94-C5AC-3949-5AA9-B2DE92669B17`。
- 模型服务合成文本 ping 返回“正常”，约 0.1 秒；实例
  `9B2E6751-FB71-3CE2-0847-4AD1A98AB875`。
- 同一实例数据库预检失败：`1045 Access denied for user 'xuanyu11_r'@'10.25.1.228'`。
  这可能是账号来源限制、密码或认证配置；不能仅根据 1045 确认是哪一种。
  `ads_fst` 是 Linux 运行用户，MySQL 登录用户名来自 `config/config.yaml`，不能直接替换为 Linux 用户名。
- HDFS 默认 `viewfs://c9`；创建 `/user/ads_fst` 被拒绝，错误在 `/user` 无写权限。
- 当前任务上传资源目录属于 `hadoop_admin`，`ads_fst` 在其下创建日志目录也失败；未获得可用持久化目录。
- 平台“调度IP段”页面列出了 `10.25.1.%`、`10.185.13.%` 等网段；已观察到任务落在这两个网段。
  账号来源授权应由数据库及平台负责人按实际运行池确认，不能只授权某次执行的单个 IP。
- 工作流级 Git 配置未自动提供给自定义Shell：实际日志为 `Phab code url is empty, skip download phab code`。
  离线包包含当前项目，不依赖工作流级 Git 拉取。
- 已询问平台 AI；它无法确认账号可写存储路径、常驻运行时长和故障自动恢复保证。

## 离线环境

生产链路直接依赖四个包：PyYAML、requests、PyMySQL、opencv-python-headless。
离线锁文件另外包含 NumPy、tzdata、certifi、charset-normalizer、idna、urllib3。
`openpyxl` 只用于 Excel 辅助脚本，不是分类 worker 依赖；不需要本地模型框架。

Python 来源为 Astral 官方 python-build-standalone 20261013 发布的 CPython 3.11.17，
文件 `cpython-3.11.17+20261013-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz`。
SHA256：`3b8e5a248bcc351265ffe68f5e457668a2e09c99a47f81f658988c7d3d107522`。
下载后必须校验官方 SHA256SUMS；不要复制 macOS 虚拟环境到 Linux。
10 个 Linux wheel 的版本和哈希在 `requirements.dataworks.lock`，安装时强制哈希匹配且禁止联网解析。

在服务器仓库根目录，准备 `intent_behavior/output/dataworks/assets/python-linux.tar.gz`
和 `assets/wheels/*.whl` 后构建（仅打包，不连接业务系统）：

```bash
cd intent_behavior && python3 scripts/build_dataworks_bundle.py --assets output/dataworks/assets --output output/dataworks/intent_behavior_dataworks.zip
```

构建器只包含源代码、业务配置和白名单脚本；不包含原有日志、媒体、Git、开发机环境。
包内 `manifest.json` 记录基础 commit 和源文件哈希；`SHA256SUMS` 覆盖全部载荷。
ZIP 含业务配置，仅上传到本项目 Dataworks 节点，不提交 Git 或公开分发。

## 自定义Shell 字段

“依赖包”上传构建好的 ZIP；不包含模型训练；不上传临时文件；`python-hdfs-path` 留空。
脚本以 Bash 调用，Python 从包内解压并安装锁定的 wheel；不需要填写原普通 SHELL 节点的代码地址/运行参数。
初始验证脚本（不分类、不回写）：

```bash
#!/bin/bash
set -euo pipefail
bash intent_behavior_dataworks/app/scripts/dataworks_bundle_start.sh --check
bash intent_behavior_dataworks/app/scripts/dataworks_bundle_start.sh --preflight
```

正式入口以上方当前配置为准，已内置验证过的HDFS目录。以下段落记录原始部署约束。
每轮结束等待 10 秒沿用配置；Cron 的 5 分钟只是尝试创建实例的间隔，不提供 10 秒故障恢复保证。
工作流 Cron `0 0/5 * * * ?`，检查上一个实例未完成=是，任务超时kill=1430分钟，调度提交失败重试=串行。
这些设置仍需验证“失败后下一次触发”和平台最大运行时长，不保证可连续运行数周。
切换时工作流中只保留一条正式消费路径，诊断节点不要接入正式 DAG；Cron 工作流只上线工作流，节点保持下线。
原普通 SHELL 的 `YYYYMMDD HH` 运行参数须清空；新入口对意外参数会报错。

## 审计、故障和切换

`DATAWORKS_LOG_HDFS` 启用前台监督器：启动前验证 HDFS 真实写权限；默认每 60 秒将 logs 快照覆盖到实例专属归档；
退出时再次归档。连续 3 次归档失败会停止 worker 并非零退出。归档用 UUID 区分实例；审计 run_id 也增加随机后缀。
强制 kill 或主机宕机最多会丢失最近一次成功同步后的日志；同步期间单条未写完 JSONL 可能只有下次同步才完整。
不能把这个方案描述为零丢失。长时间运行的大日志采用流式打包，但仍需观察归档时长和磁盘使用。
报告发送程序仍在旧服务器上，但新版本 `send_weekly_report.py` 已通过 `report_sources.py` 接入个人 HDFS 归档：发送前同步并校验心跳、窗口首尾覆盖及 JSONL 完整性，与旧服务器审计按同名完整前缀去重；不再只统计旧本地目录。失败时只提示数据异常，不生成零值报表。需要在发送服务器部署该版本并验证 HDFS 客户端和 ads_fst 的权限；本地测试不等于服务器已更新。不自动迁移钉钉登录、不重启 DataWorks Worker、不自动补发消息。

容器 SIGTERM 进入现有中断清理/审计流程，未完成博文保持 level=0；Dataworks 启动器默认连续 3 轮查询异常后非零退出。
这不把单条模型技术失败当成业务 level=6。单条技术失败仍保留原有重试及审计逻辑。

切换顺序：数据库预检通过 → 合成模型/媒体验证通过 → HDFS 审计验证通过 →
确认旧进程已退出 → 目标少量真实分类及 HTTP 回写验证（本次用户要求跳过现有失败样本）→ 仅上线一条正式路径 →
验证停止/重启、Cron 跳过未结束实例和崩溃后恢复。验证失败保留旧服务器方案，不同时长期运行两个 worker。
