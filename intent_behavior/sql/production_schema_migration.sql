-- 原生内容站 AI 分层：生产上线前数据库迁移
-- ================================================================
-- 适用范围：新正式库或新增分表的迁移模板。
-- 注意：2026-09-09 的执行历史仅对应旧测试环境，不能据此判断 2026-09-14
--       切换后的正式库已具备相同索引、唯一约束或审计表。
--
-- 执行前要求：
-- 1. 先在预发验证，再在低峰期执行生产；
-- 2. 先执行下方“重复数据检查”，确认无重复后再加唯一索引；
-- 3. 所有已存在、或当前有效任务会路由到的 0~19 分表都需要执行相同的索引变更；
-- 4. 由数据库管理员确认在线 DDL 参数与 MySQL 版本。
--
-- 本脚本不会由 Python worker 自动执行。

-- 一、20 分表：生成上线前重复数据检查 SQL
-- 将下面查询结果逐条复制执行。若任一查询返回记录，先按业务规则清理/合并，
-- 再执行 UNIQUE KEY。该写法会覆盖当前库所有实际存在的 _0 ~ _19 分表。
SELECT CONCAT(
  'SELECT customer_id, super_task_id, mid, COUNT(*) AS duplicate_count FROM `',
  table_name,
  '` GROUP BY customer_id, super_task_id, mid HAVING COUNT(*) > 1;'
) AS duplicate_check_sql
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND table_name REGEXP '^nature_ad_super_mid_([0-9]|1[0-9])$'
ORDER BY table_name;

-- 二、20 分表：生成索引 DDL（先由 DBA 审核，再逐条执行）
-- 目的：
-- 1) uk_customer_task_mid 防止同一任务重复写入同一 mid；
-- 2) idx_customer_task_level_id 加速 worker 查询：
--    WHERE customer_id=? AND super_task_id=? AND level=0 ORDER BY id LIMIT ?
--
-- 对已存在同名索引的分表，请跳过对应 ALTER；不要在生产库直接批量自动执行。
SELECT CONCAT(
  'ALTER TABLE `', table_name,
  '` ADD UNIQUE KEY uk_customer_task_mid (customer_id, super_task_id, mid), ',
  'ADD KEY idx_customer_task_level_id (customer_id, super_task_id, level, id);'
) AS shard_index_ddl
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND table_name REGEXP '^nature_ad_super_mid_([0-9]|1[0-9])$'
ORDER BY table_name;

-- 新增分表时必须同步带上以上两个索引，或在投入数据前执行相应 ALTER。
--
-- 三、任务扫描索引
-- worker 按 task_type / exec_status / end_time 扫描有效任务。
ALTER TABLE super_mid_task
  ADD KEY idx_ai_active_task (task_type, exec_status, end_time, id);

-- task_id 是业务主键，确认现有数据没有重复后建议升为唯一索引。
SELECT task_id, COUNT(*) AS duplicate_count
FROM super_mid_task
GROUP BY task_id
HAVING COUNT(*) > 1;

-- 若无重复：
-- ALTER TABLE super_mid_task DROP KEY idx_task_id, ADD UNIQUE KEY uk_task_id (task_id);
-- 旧测试环境曾于 2026-09-09 执行：idx_task_id → uk_task_id。

-- 四、可选：AI 分类审计表
-- 配置 audit.mysql_enabled=true 后，worker 会批量写入此表；
-- JSONL 本地审计始终保留，作为数据库审计不可用时的保底。
CREATE TABLE IF NOT EXISTS nature_ad_mid_ai_audit (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id VARCHAR(64) NOT NULL,
  event_time DATETIME NOT NULL,
  customer_id BIGINT NOT NULL,
  task_id BIGINT NOT NULL,
  record_id BIGINT NOT NULL,
  mid VARCHAR(32) NOT NULL,
  uid VARCHAR(32) NOT NULL DEFAULT '',
  status VARCHAR(32) NOT NULL,
  industry_name VARCHAR(64) NOT NULL DEFAULT '',
  hit_mid_tag VARCHAR(512) NOT NULL DEFAULT '',
  hit_brand_name VARCHAR(256) NOT NULL DEFAULT '',
  is_forward TINYINT NOT NULL DEFAULT 0,
  forward_mid VARCHAR(32) NOT NULL DEFAULT '',
  forward_status VARCHAR(32) NOT NULL DEFAULT '',
  layer_name VARCHAR(64) NOT NULL DEFAULT '',
  media_type VARCHAR(64) NOT NULL DEFAULT '',
  error_stage VARCHAR(32) NOT NULL DEFAULT '',
  error_detail VARCHAR(2000) NOT NULL DEFAULT '',
  model_output MEDIUMTEXT,
  timings_json VARCHAR(2000) NOT NULL DEFAULT '',
  ctime TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_task_mid_time (task_id, mid, event_time),
  KEY idx_record_time (record_id, event_time),
  KEY idx_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='原生内容站 AI 分层运行审计';
-- 旧测试环境曾于 2026-09-09 创建此表。新正式库须先确认表和 INSERT 权限，
-- 再将 config.yaml 的 audit.mysql_enabled 改为 true。

-- 五、审计表保留策略
-- 建议由 cron / XXL 每日低峰执行：
--   python3 scripts/cleanup_mysql_audit.py --execute
-- 脚本按 10000 条分批删除，保留期限由 config.yaml 的
-- audit.mysql_retention_days（默认 90）控制。
