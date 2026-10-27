# scripts 目录说明

这里仅放运维、自检和一次性数据辅助脚本；不放正式持续消费入口，也不放单元测试。

## 正式入口

生产持续消费只能使用项目根目录的：

```bash
python3 worker.py --config config/config.yaml
```

详见 [`docs/production_runbook.md`](../docs/production_runbook.md)。

## 当前脚本

| 脚本 | 用途 | 示例 |
| --- | --- | --- |
| `production_preflight.py` | 上线前只读检查路由字段、分表、重复数据和索引 | `python3 scripts/production_preflight.py --strict` |
| `cleanup_mysql_audit.py` | 预览/分批清理过期 MySQL 运行审计 | `python3 scripts/cleanup_mysql_audit.py --execute` |
| `manual_classify_mid.py` | 指定一个 task_id + mid 预演、受控分类或回写 | `python3 scripts/manual_classify_mid.py --task-id <id> --mid <mid>` |
| `count_xlsx_mids.py` | 查询 Excel 博文对应的 mid 并输出映射文件 | `python3 scripts/count_xlsx_mids.py --help` |
| `generate_weekly_report.py` | 从 JSONL 运行审计生成指定日期范围的只读报告 | `python3 scripts/generate_weekly_report.py --start-date 2026-09-19 --end-date 2026-09-19 --report-name 日报` |
| `send_weekly_report.py` | 按日历规则发送 T-1 日报、周五汇总和月末汇总 | `python3 scripts/send_weekly_report.py` |
| `report_sources.py` | 只读同步 HDFS 审计、与旧日志去重合并、校验来源与窗口覆盖 | 由 `send_weekly_report.py` 调用，无独立发送入口 |
| `install_weekly_report_cron.sh` | 安装/更新每天 10:00（含周末）的报告 cron | `bash scripts/install_weekly_report_cron.sh` |

## 钉钉企业机器人报告

项目报告使用钉钉“企业机器人”主动向个人单聊发送 Markdown。每天固定发 T-1 日报；周五另发当周汇总；月末最后一天另发当月汇总。各汇总与日报分开发送。

正式报告默认先读取 `viewfs://c9/dw_ext/ad/person/xuanyu11/intent_behavior/dataworks/logs/` 的心跳及实例归档，并与旧服务器审计去重合并。安装和运行都必须使用 Linux 用户 `ads_fst`，不能在 `xuanyu11` 下安装另一份 cron；`HDFS_RUNNER` 可指定完整客户端路径。`--dry-run` 同样会读取 HDFS、写 UID 隔离的本地下载缓存，但不发消息、不触发业务处理。误装后可在错误用户下执行 `bash scripts/install_weekly_report_cron.sh --remove`，仅备份并移除该用户的本项目定时块；不要删除其他定时任务或 chmod 777 缓存。

数据源过期/损坏/缺失时，不回退到陈旧本地审计，不发送零值统计；正式任务只发一条数据异常提示并非零退出。`--source local --dry-run` 仅用于人工排查。实现及缓存边界见 `report_sources.py` 和项目 README 的“钉钉定时项目报告”。

cron 不自动加载登录 shell 的 Java 环境。安装器校验 `--java-home`（未传时使用当前 `JAVA_HOME`）中的 `bin/java`，并把 `JAVA_HOME` 固定到本报告命令；路径缺失或 Java 无法运行时不修改原 cron。服务器已确认的 Java 目录为 `/usr/local/jdk1.8.0_202`。环境修复后重新安装报告定时块即可，无需重启 Worker。

企业机器人需在钉钉开发者后台完成以下步骤后才能启用发送：

1. 创建应用；
2. 在“消息推送”中开启机器人，接收模式选 **Stream**；
3. 申请 **企业内机器人发送消息权限**；
4. 将应用可见范围设置为项目负责人；
5. 调试并发布应用。

机器人已配置为 Stream 且已发布。运行机器必须已安装并登录 `dws`，然后可先验证：

```bash
python3 scripts/send_weekly_report.py --dry-run
python3 scripts/send_weekly_report.py --run-date 2026-07-31 --dry-run
python3 scripts/send_weekly_report.py
```

再在正式运行机器安装每天 10:00（北京时间，含周末）任务：

```bash
bash scripts/install_weekly_report_cron.sh \
  --project-dir /data0/xuanyu11/intent_behavior-git/weibo-self/intent_behavior \
  --python /usr/bin/python3 \
  --hdfs-runner "$(command -v hdfs)" \
  --java-home "$JAVA_HOME" \
  --dws-runner /usr/local/bin/dws
```

## 约束

- 新增测试应放在 `tests/`，并在文件头写明用途和运行方式。
- 新增运维/数据辅助脚本应放在本目录，并在文件头写明用途、输入输出和运行方式。
- 不要使用这里的历史脚本绕开 `worker.py` 直接消费 MySQL 或批量回写。
