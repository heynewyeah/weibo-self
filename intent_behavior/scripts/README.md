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
| `generate_weekly_report.py` | 从 JSONL 运行审计生成只读周报（成功/失败/耗时/趋势/存储） | `python3 scripts/generate_weekly_report.py --days 7` |
| `send_weekly_report.py` | 通过已发布的企业机器人向个人单聊发送周报 | `python3 scripts/send_weekly_report.py` |
| `install_weekly_report_cron.sh` | 安装/更新每周四 10:00 的周报 cron | `bash scripts/install_weekly_report_cron.sh` |
| `batch_classify_3layer.sh` | 历史 Hive 批量分类辅助脚本；不用于 MySQL 正式回写 | 见脚本文件头 |
| `run_hive.sh` | 历史 Hive 数据准备辅助脚本；不用于 MySQL 正式回写 | 见脚本文件头 |

## 钉钉企业机器人周报

项目周报的推荐能力是钉钉“企业机器人”，而不是互动卡片或服务窗：它支持主动向个人单聊发送 Markdown 周报。

企业机器人需在钉钉开发者后台完成以下步骤后才能启用发送：

1. 创建应用；
2. 在“消息推送”中开启机器人，接收模式选 **Stream**；
3. 申请 **企业内机器人发送消息权限**；
4. 将应用可见范围设置为项目负责人；
5. 调试并发布应用。

机器人已配置为 Stream 且已发布。运行机器必须已安装并登录 `dws`，然后可先验证：

```bash
python3 scripts/send_weekly_report.py --dry-run
python3 scripts/send_weekly_report.py
```

再在正式运行机器安装每周四 10:00（北京时间）任务：

```bash
bash scripts/install_weekly_report_cron.sh \
  --project-dir /data0/xuanyu11/intent_behavior-git/weibo-self/intent_behavior \
  --python /usr/bin/python3 \
  --dws-runner /usr/local/bin/dws
```

## 约束

- 新增测试应放在 `tests/`，并在文件头写明用途和运行方式。
- 新增运维/数据辅助脚本应放在本目录，并在文件头写明用途、输入输出和运行方式。
- 不要使用这里的历史脚本绕开 `worker.py` 直接消费 MySQL 或批量回写。
