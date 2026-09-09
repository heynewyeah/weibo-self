#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统计《汽车博文分类标注版.xlsx》中的博文在 MySQL 里对应的 mid。

原理：
1. 读取 xlsx 的 Sheet1，逐行取“原始URL”（形如 http://weibo.com/{uid}/{code}）。
2. 把 URL 里的 weibo code 按微博 mid 编码规则解码成十进制 mid。
3. 到 MySQL（clue_collect_common）的 nature_ad_super_mid_{0..19} 分表中按 mid 精确匹配；
   若解码出的 mid 未命中，再用 (mid_uid + code) 与 short_url 做兜底匹配。
4. 输出逐条 TSV（含命中 mid 与库内信息）+ 控制台汇总（总数/命中/未命中/去重 mid/level 分布）。

用法：
  python3 scripts/count_xlsx_mids.py
  python3 scripts/count_xlsx_mids.py --xlsx 汽车博文分类标注版.xlsx
  python3 scripts/count_xlsx_mids.py --task-id 1302305683722469377 --output output/xlsx_mid_map.tsv

依赖：pyyaml pymysql openpyxl（如缺失请先 pip install 对应包）
"""

import os
import re
import sys
import argparse
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))


# ── weibo mid 编码/解码 ─────────────────────────────────────────
ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def code_to_mid(code: str) -> str:
    """
    微博 URL 中的短码（如 QyrZlCxRT）转十进制 mid。
    规则：除最左侧一组外，每 4 个字符一组表示 7 位十进制块。
    """
    code = str(code).strip()
    if not code:
        return ""
    result = []
    head_len = len(code) % 4
    start = 0
    if head_len:
        head = code[:head_len]
        val = 0
        for ch in head:
            val = val * 62 + ALPHABET.index(ch)
        result.append(str(val))
        start = head_len
    for i in range(start, len(code), 4):
        part = code[i:i + 4]
        val = 0
        for ch in part:
            val = val * 62 + ALPHABET.index(ch)
        result.append(str(val).rjust(7, "0"))
    return "".join(result)


URL_RE = re.compile(r"weibo\.com/(\d+)/([0-9A-Za-z]+)")


def parse_weibo_url(url) -> tuple:
    """从 URL 提取 (mid_uid, code)；解析失败返回 (None, None)。"""
    if not url:
        return None, None
    mt = URL_RE.search(str(url).strip())
    if not mt:
        return None, None
    return mt.group(1), mt.group(2)


# ── xlsx 读取 ────────────────────────────────────────────────────
def load_xlsx_rows(xlsx_path: str, sheet_name: str = "Sheet1"):
    """读取标注表，返回 [{seq, category, author, summary, url, mid_marked}, ...]"""
    try:
        import openpyxl
    except ImportError:
        sys.exit("缺少 openpyxl，请先执行: pip install openpyxl")

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    if sheet_name not in wb.sheetnames:
        sys.exit(f"xlsx 中不存在工作表: {sheet_name}（现有: {wb.sheetnames}）")
    ws = wb[sheet_name]

    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        # 列：序号(0) 分类(1) 发博用户(2) 内容摘要(3) 原始URL(4) ... mid(7)
        seq = row[0] if len(row) > 0 else None
        if seq is None or str(seq).strip() == "":
            continue
        rows.append({
            "seq": seq,
            "category": row[1] if len(row) > 1 else "",
            "author": row[2] if len(row) > 2 else "",
            "summary": row[3] if len(row) > 3 else "",
            "url": row[4] if len(row) > 4 else "",
            "mid_marked": row[7] if len(row) > 7 else "",
        })
    wb.close()
    return rows


# ── MySQL 查询 ───────────────────────────────────────────────────
def connect_mysql(mysql_cfg):
    try:
        import pymysql
    except ImportError:
        sys.exit("缺少 pymysql，请先执行: pip install pymysql")
    return pymysql.connect(
        host=mysql_cfg["host"],
        port=int(mysql_cfg.get("port", 3306)),
        user=mysql_cfg["user"],
        password=mysql_cfg["password"],
        database=mysql_cfg["database"],
        charset=mysql_cfg.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )


def list_shard_tables(conn, database: str, prefix: str = "nature_ad_super_mid_"):
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name LIKE %s ORDER BY table_name",
        (database, prefix + "%"),
    )
    return [r["table_name"] for r in cur.fetchall() or []]


def fetch_rows_by_mids(conn, table: str, mids, task_id=None):
    """按 mid 列表查询指定分表（mids 分批，避免 IN 过长）。"""
    if not mids:
        return []
    cur = conn.cursor()
    result = []
    placeholders = ",".join(["%s"] * len(mids))
    sql = (
        f"SELECT id, customer_id, super_task_id, mid, mid_uid, short_url, level, level_time "
        f"FROM {table} WHERE mid IN ({placeholders})"
    )
    params = [str(x) for x in mids]
    if task_id:
        sql += " AND super_task_id=%s"
        params.append(int(task_id))
    sql += " ORDER BY id ASC"
    cur.execute(sql, params)
    result.extend(cur.fetchall() or [])
    return result


def fetch_rows_by_codes(conn, table: str, codes, task_id=None):
    """兜底：按 short_url 尾部短码匹配。"""
    if not codes:
        return []
    cur = conn.cursor()
    result = []
    for code in codes:
        sql = (
            f"SELECT id, customer_id, super_task_id, mid, mid_uid, short_url, level, level_time "
            f"FROM {table} WHERE short_url LIKE %s"
        )
        params = ["%" + code]
        if task_id:
            sql += " AND super_task_id=%s"
            params.append(int(task_id))
        sql += " ORDER BY id ASC"
        cur.execute(sql, params)
        result.extend(cur.fetchall() or [])
    return result


# ── 输出 ─────────────────────────────────────────────────────────
TSV_HEADER = [
    "序号", "分类", "发博用户", "内容摘要", "原始URL",
    "Excel标注mid", "库mid", "匹配状态", "库表", "库id",
    "customer_id", "super_task_id", "level", "level_time", "同mid库内行数",
]


def main():
    parser = argparse.ArgumentParser(
        description="统计《汽车博文分类标注版.xlsx》中博文对应的 mid（查询 MySQL）"
    )
    parser.add_argument(
        "--xlsx",
        default=os.path.join(PROJECT_DIR, "汽车博文分类标注版.xlsx"),
        help="标注表路径（默认 intent_behavior/汽车博文分类标注版.xlsx）",
    )
    parser.add_argument("--sheet", default="Sheet1", help="工作表名（默认 Sheet1）")
    parser.add_argument(
        "--config",
        default=os.path.join(PROJECT_DIR, "config/config.yaml"),
        help="MySQL 配置文件路径",
    )
    parser.add_argument(
        "--task-id",
        type=int,
        default=0,
        help="可选：只匹配指定 super_task_id 下的记录",
    )
    parser.add_argument(
        "--output",
        default=os.path.join(PROJECT_DIR, "output/xlsx_mid_map.tsv"),
        help="输出 TSV 路径（默认 output/xlsx_mid_map.tsv）",
    )
    args = parser.parse_args()

    if not os.path.exists(args.xlsx):
        sys.exit(f"xlsx 文件不存在: {args.xlsx}")
    if not os.path.exists(args.config):
        sys.exit(f"配置文件不存在: {args.config}")

    try:
        import yaml
    except ImportError:
        sys.exit("缺少 pyyaml，请先执行: pip install pyyaml")

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    mysql_cfg = config["mysql"]

    # 1. 读 xlsx，解析 URL -> (uid, code) 并解码出 mid
    xlsx_rows = load_xlsx_rows(args.xlsx, args.sheet)
    print("=" * 80)
    print(f"xlsx 文件:   {args.xlsx}")
    print(f"工作表:      {args.sheet}")
    print(f"博文条数:    {len(xlsx_rows)}")
    if args.task_id:
        print(f"限定任务:    super_task_id={args.task_id}")
    print("=" * 80)

    for row in xlsx_rows:
        uid, code = parse_weibo_url(row["url"])
        row["uid"] = uid
        row["code"] = code
        row["mid_decoded"] = code_to_mid(code) if code else ""

    mid_set = sorted({r["mid_decoded"] for r in xlsx_rows if r["mid_decoded"]})

    # 2. 查询分表
    conn = connect_mysql(mysql_cfg)
    tables = list_shard_tables(conn, mysql_cfg["database"])
    if not tables:
        sys.exit("MySQL 中未找到 nature_ad_super_mid_* 分表")
    print(f"发现分表:    {len(tables)} 张 -> {', '.join(tables)}")

    mid_index = {}      # mid -> [row, ...]
    code_index = {}     # code -> [row, ...]
    for table in tables:
        for r in fetch_rows_by_mids(conn, table, mid_set, task_id=args.task_id or None):
            mid_index.setdefault(str(r["mid"]), []).append(r)
        # 兜底：short_url LIKE %code（只查解码未命中的 code，减少开销）
        missing_codes = {
            r["code"] for r in xlsx_rows
            if r["code"] and not mid_index.get(r["mid_decoded"])
        }
        if missing_codes:
            for r in fetch_rows_by_codes(conn, table, missing_codes, task_id=args.task_id or None):
                code_index.setdefault(r["short_url"][-10:].strip("/"), []).append(r)
    conn.close()

    # 3. 逐条匹配
    out_rows = []
    for row in xlsx_rows:
        hit = None
        hit_count = 0
        status = "未命中"
        if row["mid_decoded"] and mid_index.get(row["mid_decoded"]):
            hit = mid_index[row["mid_decoded"]][0]
            hit_count = len(mid_index[row["mid_decoded"]])
        elif row["code"]:
            for key, rows in code_index.items():
                if key.endswith(row["code"]) and rows:
                    hit = rows[0]
                    hit_count = len(rows)
                    break

        if hit is None:
            reason = "URL无法解析" if not row["code"] else "库中不存在"
            out_rows.append([
                row["seq"], row["category"], row["author"], row["summary"],
                row["url"], row["mid_marked"], "", status + "(" + reason + ")",
                "", "", "", "", "", "", "",
            ])
            continue

        status = "命中"
        out_rows.append([
            row["seq"], row["category"], row["author"], row["summary"],
            row["url"], row["mid_marked"], str(hit["mid"]), status,
            "nature_ad_super_mid_" + str(int(hit["customer_id"]) % 20),
            hit["id"], hit["customer_id"], hit["super_task_id"],
            hit["level"], hit["level_time"], hit_count,
        ])
        row["db_mid"] = str(hit["mid"])
        row["dup_count"] = hit_count

    # 4. 写 TSV
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\t".join(TSV_HEADER) + "\n")
        for r in out_rows:
            f.write("\t".join("" if v is None else str(v) for v in r) + "\n")

    # 5. 汇总统计
    total = len(xlsx_rows)
    matched = sum(1 for r in out_rows if r[7] == "命中")
    missing = total - matched
    distinct_mids = {r[6] for r in out_rows if r[7] == "命中"}

    print()
    print("=" * 80)
    print("统计结果")
    print("=" * 80)
    print(f"xlsx 总条数:     {total}")
    print(f"命中 mid:        {matched} ({matched / total * 100:.1f}%)" if total else "命中 mid: 0")
    print(f"未命中:          {missing}")
    print(f"命中去重 mid 数: {len(distinct_mids)}")

    multi_hits = sum(1 for r in xlsx_rows if r.get("dup_count", 0) > 1)
    if multi_hits:
        print(f"其中同 mid 在库内有多行的博文: {multi_hits} 条（可用 --task-id 限定具体任务）")

    hit_levels = Counter(r[12] for r in out_rows if r[7] == "命中")
    if hit_levels:
        print("命中记录 level 分布:")
        for level, cnt in sorted(hit_levels.items(), key=lambda kv: str(kv[0])):
            tag = " (未回写/待处理)" if str(level) == "0" else ""
            print(f"  level={level}: {cnt} 条{tag}")

    marked = [r for r in xlsx_rows if str(r["mid_marked"] or "").strip()]
    if marked:
        same = sum(
            1 for r in marked
            if str(r["mid_marked"]).strip() == str(r.get("db_mid", "")).strip()
        )
        print(f"Excel 已填 mid 列: {len(marked)} 条，与库一致 {same} 条")

    if missing:
        print("\n未命中明细:")
        for r in out_rows:
            if r[7] != "命中":
                print(f"  序号={r[0]} url={r[4]} -> {r[7]}")

    print(f"\n详细结果已写入: {args.output}")
    sys.exit(0 if missing == 0 else 1)


if __name__ == "__main__":
    main()
