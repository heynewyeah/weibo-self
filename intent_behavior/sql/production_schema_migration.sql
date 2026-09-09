-- 原生内容站 AI 分层：生产上线前数据库迁移
-- ================================================================
-- 执行前要求：
-- 1. 先在预发验证，再在低峰期执行生产；
-- 2. 先执行下方“重复数据检查”，确认无重复后再加唯一索引；
-- 3. 20 张分表都需要执行相同的索引变更（本文件示例列出 _0 / _1）；
-- 4. 由数据库管理员确认在线 DDL 参数与 MySQL 版本。
--
-- 本脚本不会由 Python worker 自动执行。

-- 一、上线前重复数据检查
-- 若以下任一查询返回记录，先按业务规则清理/合并，再执行 UNIQUE KEY。
SELECT customer_id, super_task_id, mid, COUNT(*) AS duplicate_count
FROM nature_ad_super_mid_0
GROUP BY customer_id, super_task_id, mid
HAVING COUNT(*) > 1;

SELECT customer_id, super_task_id, mid, COUNT(*) AS duplicate_count
FROM nature_ad_super_mid_1
GROUP BY customer_id, super_task_id, mid
HAVING COUNT(*) > 1;

-- 二、分表：防止同一任务重复写入同一 mid
-- 以及加速 worker 的：
-- WHERE customer_id=? AND super_task_id=? AND level=0 ORDER BY id LIMIT ?
--
-- 对 nature_ad_super_mid_0：
ALTER TABLE nature_ad_super_mid_0
  ADD UNIQUE KEY uk_customer_task_mid (customer_id, super_task_id, mid),
  ADD KEY idx_customer_task_level_id (customer_id, super_task_id, level, id);

-- 对 nature_ad_super_mid_1：
ALTER TABLE nature_ad_super_mid_1
  ADD UNIQUE KEY uk_customer_task_mid (customer_id, super_task_id, mid),
  ADD KEY idx_customer_task_level_id (customer_id, super_task_id, level, id);

-- 当前生产库仅存在 _0 / _1，已于 2026-09-09 执行上述索引变更。
-- 后续若创建 nature_ad_super_mid_2 ~ nature_ad_super_mid_19，建表时必须带上相同索引，
-- 或在投入数据前执行对应 ALTER。
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
-- 当前生产库已于 2026-09-09 执行：idx_task_id → uk_task_id。

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
-- 当前生产库已于 2026-09-09 创建此表，并在 config.yaml 开启 audit.mysql_enabled。

-- 五、审计表保留策略
-- 建议由 cron / XXL 每日低峰执行：
--   python3 scripts/cleanup_mysql_audit.py --execute
-- 脚本按 10000 条分批删除，保留期限由 config.yaml 的
-- audit.mysql_retention_days（默认 90）控制。
