-- =============================================================
-- query_detail-明细表查询.sql
-- =============================================================
-- 这是一个 SQL 记事本，包含各种常用查询场景的完整 SQL。
-- 直接复制需要的 SQL 到 MySQL 客户端执行即可。
--
-- 数据库：clue_collect_common
-- 分表：nature_ad_super_mid_0 ~ nature_ad_super_mid_19
-- 作者：xuanyu11
-- 创建时间：2026-08-27
-- =============================================================


-- =============================================================
-- 场景1：查询分表 1 中 level=0（待处理）的前 20 条
-- =============================================================
SELECT
    id,
    customer_id,
    super_task_id,
    mid,
    mid_uid,
    mid_text,
    mid_pids,
    mid_fids,
    forward_mid,
    forward_text,
    hit_mid_gat,
    level
FROM nature_ad_super_mid_1
WHERE level = 0
ORDER BY id ASC
LIMIT 20;


-- =============================================================
-- 场景2：按 mid 精确查询某条记录
-- =============================================================
SELECT
    id,
    customer_id,
    super_task_id,
    mid,
    mid_uid,
    mid_text,
    mid_pids,
    mid_fids,
    forward_mid,
    forward_text,
    hit_mid_gat,
    level
FROM nature_ad_super_mid_1
WHERE mid = '5239377989207686';


-- =============================================================
-- 场景3：按 customer_id 过滤
-- =============================================================
SELECT
    id,
    customer_id,
    super_task_id,
    mid,
    mid_uid,
    mid_text,
    mid_pids,
    mid_fids,
    forward_mid,
    forward_text,
    hit_mid_gat,
    level
FROM nature_ad_super_mid_1
WHERE customer_id = 2608812381
  AND level = 0
ORDER BY id ASC
LIMIT 20;


-- =============================================================
-- 场景4：查所有 level（不限 level=0）
-- =============================================================
SELECT
    id,
    customer_id,
    super_task_id,
    mid,
    mid_uid,
    forward_mid,
    forward_mid_text,
    hit_mid_tag,
    level
FROM nature_ad_super_mid_1
ORDER BY id ASC
LIMIT 20;


-- =============================================================
-- 场景5：只查转发博文（forward_mid 非空且非 0）
-- =============================================================
SELECT
    id,
    customer_id,
    super_task_id,
    mid,
    mid_uid,
    mid_text,
    forward_mid,
    forward_text,
    hit_mid_gat,
    level
FROM nature_ad_super_mid_1
WHERE forward_mid IS NOT NULL
  AND forward_mid != ''
  AND forward_mid != '0'
ORDER BY id ASC
LIMIT 20;


-- =============================================================
-- 场景6：按 level 统计各层级数量 + 转发数量
-- =============================================================
SELECT
    level,
    COUNT(*) AS cnt,
    SUM(CASE
        WHEN forward_mid IS NOT NULL
         AND forward_mid != ''
         AND forward_mid != '0'
        THEN 1 ELSE 0
    END) AS forward_cnt
FROM nature_ad_super_mid_1
GROUP BY level
ORDER BY level;


-- =============================================================
-- 场景7：查某个 super_task_id 下的所有记录
-- =============================================================
SELECT
    id,
    customer_id,
    super_task_id,
    mid,
    mid_uid,
    mid_text,
    forward_mid,
    forward_text,
    hit_mid_gat,
    level
FROM nature_ad_super_mid_1
WHERE super_task_id = 1296499607471128577
ORDER BY id ASC
LIMIT 50;


-- =============================================================
-- 场景8：查反解可能失败的记录（mid_text 为空或很短）
-- =============================================================
SELECT
    id,
    customer_id,
    super_task_id,
    mid,
    mid_uid,
    mid_text,
    mid_pids,
    mid_fids,
    forward_mid,
    forward_text,
    hit_mid_gat,
    level
FROM nature_ad_super_mid_1
WHERE (mid_text IS NULL OR mid_text = '' OR LENGTH(mid_text) < 5)
  AND level = 0
ORDER BY id ASC
LIMIT 20;


-- =============================================================
-- 场景9：查 hit_mid_gat 非空的记录
-- =============================================================
SELECT
    id,
    customer_id,
    super_task_id,
    mid,
    mid_uid,
    mid_text,
    forward_mid,
    forward_text,
    hit_mid_gat,
    level
FROM nature_ad_super_mid_1
WHERE hit_mid_gat IS NOT NULL
  AND hit_mid_gat != ''
ORDER BY id ASC
LIMIT 20;


-- =============================================================
-- 场景10：跨分表查询（查所有分表中某个 mid）
-- 注意：需要手动 UNION ALL 所有分表，或逐个执行
-- =============================================================
SELECT 'nature_ad_super_mid_0' AS table_name, id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_0 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_1', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_1 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_2', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_2 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_3', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_3 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_4', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_4 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_5', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_5 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_6', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_6 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_7', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_7 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_8', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_8 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_9', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_9 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_10', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_10 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_11', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_11 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_12', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_12 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_13', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_13 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_14', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_14 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_15', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_15 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_16', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_16 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_17', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_17 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_18', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_18 WHERE mid = '5239377989207686'
UNION ALL
SELECT 'nature_ad_super_mid_19', id, customer_id, super_task_id, mid, mid_uid, level, forward_mid, hit_mid_gat FROM nature_ad_super_mid_19 WHERE mid = '5239377989207686';
