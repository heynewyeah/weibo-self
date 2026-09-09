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
| `count_xlsx_mids.py` | 查询 Excel 博文对应的 mid 并输出映射文件 | `python3 scripts/count_xlsx_mids.py --help` |
| `batch_classify_3layer.sh` | 历史 Hive 批量分类辅助脚本；不用于 MySQL 正式回写 | 见脚本文件头 |
| `run_hive.sh` | 历史 Hive 数据准备辅助脚本；不用于 MySQL 正式回写 | 见脚本文件头 |

## 约束

- 新增测试应放在 `tests/`，并在文件头写明用途和运行方式。
- 新增运维/数据辅助脚本应放在本目录，并在文件头写明用途、输入输出和运行方式。
- 不要使用这里的历史脚本绕开 `worker.py` 直接消费 MySQL 或批量回写。
