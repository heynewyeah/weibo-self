# Dataworks 迁移记录（2026-10-16）

## 当前状态（2026-10-19 切换验证中）

用户已确认旧 worker 停止，并授权继续切换。Dataworks 工作流目前仍未上线，
先验证少量真实 HTTP 回写，再启动前台常驻 worker。不能将“验证完成”视为生产已切换。

工作流：原生内容站博文分类，ID `928f2456-7163-4191-87bb-4d63fad55cd0`。
正式自定义Shell：`intent_behavior_worker`，ID `10B7820D-FD00-D14F-33EB-185B78DDA5D0`。
运行用户 `ads_fst`，环境 `ext_hadoop-2.7.3_hive-2.3.7_spark-3.2.0_auron-7.0`，超时kill=0。
DAG 已移除旧普通 SHELL 和诊断节点，仅保留正式 worker；诊断定义仍可单独运行。

### 已完成的实机证据

- MySQL 仍使用 `xuanyu11_r`；工单 51328 新增来源 `10.25.1.%` 和 `10.185.13.%`。
  两个运行池均已连接成功，Linux 用户名与 MySQL 账号独立。
- 2026-10-16 实例 `A41EDB63-7832-881B-FED2-DBF598643ACB` 在 `10.25.1.227` 完成一条真实图文分类，
  `write_back=false`，未修改业务等级。18 个任务待处理查询合计 104.1ms，单次最慢 7.7ms。
- 检查范围内使用现有索引 `idx_customer_super_task_id`，EXPLAIN 最大估计 222 行。
  严格预检仍提示 20 张分表缺少建议的四列索引；未变更数据库结构，也未把此警告改成通过。
- HDFS 可写根为 `viewfs://c9/user_ext/ads_fst/intent_behavior_dataworks`。
  已实测归档上传、下载和解包；真实图文审计在 `validation/dry_run_8f2b876.tar.gz`。
- 当前部署包必须包含 10 月 19 日新增的重试预算和完整失败档案逻辑，不能继续使用旧 `8f2b876` 包。
- 离线保护测试共 52 项通过；包含重建目录后恢复 exhausted 预算、路径重定位、状态访问失败时拒绝启动。

### 正式 Shell

```bash
#!/bin/bash
set -euo pipefail
export DATAWORKS_LOG_HDFS='viewfs://c9/user_ext/ads_fst/intent_behavior_dataworks/logs'
export DATAWORKS_STATE_HDFS='viewfs://c9/user_ext/ads_fst/intent_behavior_dataworks/state/retry_state.sqlite3'
export DATAWORKS_LOG_SYNC_SEC=60
exec bash intent_behavior_dataworks/app/scripts/dataworks_bundle_start.sh
```

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

正式脚本必须在数据库授权、HDFS 归档权限和完整业务验证通过后填入已验证的 `DATAWORKS_LOG_HDFS`，
再调用不带参数的 `dataworks_bundle_start.sh`。不得用未经验证的目录占位直接上线。
每轮结束等待 10 秒沿用配置；Cron 的 5 分钟只是尝试创建实例的间隔，不提供 10 秒故障恢复保证。
工作流 Cron `0 0/5 * * * ?`，检查上一个实例未完成=是，超时kill=0，调度提交失败重试=串行。
这些设置仍需验证“失败后下一次触发”和平台最大运行时长，不保证可连续运行数周。
切换时工作流中只保留一条正式消费路径，诊断节点不要接入正式 DAG；Cron 工作流只上线工作流，节点保持下线。
原普通 SHELL 的 `YYYYMMDD HH` 运行参数须清空；新入口对意外参数会报错。

## 审计、故障和切换

`DATAWORKS_LOG_HDFS` 启用前台监督器：启动前验证 HDFS 真实写权限；默认每 60 秒将 logs 快照覆盖到实例专属归档；
退出时再次归档。连续 3 次归档失败会停止 worker 并非零退出。归档用 UUID 区分实例；审计 run_id 也增加随机后缀。
强制 kill 或主机宕机最多会丢失最近一次成功同步后的日志；同步期间单条未写完 JSONL 可能只有下次同步才完整。
不能把这个方案描述为零丢失。长时间运行的大日志采用流式打包，但仍需观察归档时长和磁盘使用。
报告程序仍在旧服务器上，切换前需把 HDFS 归档中的 `logs/runs` 合并到报告输入，避免报告漏算；不自动迁移钉钉登录或发送消息。

容器 SIGTERM 进入现有中断清理/审计流程，未完成博文保持 level=0；Dataworks 启动器默认连续 3 轮查询异常后非零退出。
这不把单条模型技术失败当成业务 level=6。单条技术失败仍保留原有重试及审计逻辑。

切换顺序：数据库预检通过 → 合成模型/媒体验证通过 → HDFS 审计验证通过 →
确认旧进程已退出 → 目标少量真实分类及 HTTP 回写验证 → 仅上线一条正式路径 →
验证停止/重启、Cron 跳过未结束实例和崩溃后恢复。验证失败保留旧服务器方案，不同时长期运行两个 worker。
