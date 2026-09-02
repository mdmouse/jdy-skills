# -*- coding: utf-8 -*-
"""外部数据源：CSV / JSONL / SQLite。让同步的**源端**不必是简道云。

用途很具体：把别的系统导出的东西灌进简道云，并且保住同步的那套保证——
按业务键比对、只写有变化的、ID 映射持久化、写后回读核对。
拿 Excel 导入去做这件事得不到这些（它只认"新增/按 _id 更新"）。

**只做源端，不做目标端。** 往 CSV 里写不需要这套东西，`jdy-excel-bridge`
的导出已经够了；而"两边都是外部文件"跟简道云没关系，不该出现在这个仓库里。

零依赖：csv / json / sqlite3 都在标准库里。

外部行没有简道云的 `_id`，所以用 `业务键` 合成一个稳定的源端 ID
（`file:<业务键>`）——ID 映射靠它认人，业务键一改就等于换了一条记录，
这一点和简道云侧靠 `_id` 的行为不同，必须说清楚。
"""
import csv
import json
import os
import sqlite3

TEXT = lambda col: {"name": col, "label": col, "type": "text"}
SUFFIXES = {".csv": "csv", ".tsv": "csv", ".jsonl": "jsonl", ".ndjson": "jsonl",
            ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite"}


class SourceError(ValueError):
    pass


def kind_of(path):
    kind = SUFFIXES.get(os.path.splitext(path)[1].lower())
    if not kind:
        raise SourceError(
            "认不出这是什么文件：%s。支持 .csv/.tsv、.jsonl/.ndjson、"
            ".db/.sqlite/.sqlite3" % os.path.basename(path))
    return kind


def _rows_csv(path):
    # 带 BOM 的 CSV 是 Excel 导出的常态，utf-8-sig 会把它吃掉；
    # 不处理的话第一列的列名会变成 "﻿客户名称"，然后"表里没有这个字段"。
    delim = "\t" if path.lower().endswith(".tsv") else ","
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=delim)
        if not reader.fieldnames:
            raise SourceError("%s 是空文件或没有表头" % os.path.basename(path))
        cols = [c.strip() for c in reader.fieldnames]
        return cols, [{c.strip(): (v or "") for c, v in row.items() if c is not None}
                      for row in reader]


def _rows_jsonl(path):
    rows, cols = [], []
    with open(path, "r", encoding="utf-8-sig") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise SourceError("%s 第 %d 行不是合法 JSON：%s"
                                  % (os.path.basename(path), i, exc))
            if not isinstance(obj, dict):
                raise SourceError("%s 第 %d 行不是对象——JSONL 每行要是一个 {…}"
                                  % (os.path.basename(path), i))
            rows.append(obj)
            for k in obj:
                if k not in cols:
                    cols.append(k)          # 保持首次出现的顺序，别用 set
    if not rows:
        raise SourceError("%s 一行数据都没有" % os.path.basename(path))
    return cols, rows


def _rows_sqlite(path, table):
    if not table:
        raise SourceError("SQLite 源要指定读哪张表：在表配置里写 source_entry: <表名>")
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)   # 只读打开，不改人家的库
    try:
        conn.row_factory = sqlite3.Row
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
        if table not in names:
            raise SourceError("库里没有表/视图「%s」。有的是：%s"
                              % (table, "、".join(names[:12]) or "（一个都没有）"))
        # 表名不能参数化，所以先在上面的白名单里核过，再拼进去
        cur = conn.execute('SELECT * FROM "%s"' % table.replace('"', '""'))
        cols = [d[0] for d in cur.description]
        rows = [{c: ("" if r[c] is None else r[c]) for c in cols} for r in cur]
    finally:
        conn.close()
    if not rows:
        raise SourceError("表「%s」一行数据都没有" % table)
    return cols, rows


def resolve(path, table=None):
    """定位这张表真正要读的文件。返回落地路径。

    `source_entry`（这里的 table）在 SQLite 源上一直是有意义的——它选的是哪张表。
    而在 CSV/JSONL 上它**原来被整个忽略**：多张表配同一个 `file:`，
    每张表读到的都是同一个文件的全部内容，配置写得再认真也没用，
    而且一声不吭。同一个字段在两种源上一个管用一个不管用，是最难发现的那种。

    现在两种源上都算数：
      · `file:` 指向**目录**时，`source_entry` 就是文件名（不带后缀也行）；
      · `file:` 指向单个平面文件时，那个文件本身就是这张表——
        平面文件只有一张表，这没有歧义，但**两张表不许指向同一个平面文件**
        （那个由配置校验拦下，见 sync.load_config）。
    """
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        if not os.path.isfile(path):
            raise SourceError("找不到文件：%s" % path)
        return path
    if not table:
        raise SourceError("源指向的是目录（%s），必须用 source_entry 说明读哪个文件"
                          % path)
    names = sorted(n for n in os.listdir(path)
                   if os.path.splitext(n)[1].lower() in SUFFIXES)
    hits = [n for n in names if n == table or os.path.splitext(n)[0] == table]
    if len(hits) > 1:
        # 同名不同后缀（客户.csv 和 客户.jsonl 并存）。按排序取第一个就是**替用户
        # 猜**——猜错了不会报错，只会同步进一份不是他要的数据，而两份数据长得
        # 一模一样，事后根本看不出取的是哪个。宁可让他把后缀写全。
        raise SourceError(
            "目录 %s 里有 %d 个文件都叫「%s」：%s\n"
            "把 source_entry 写成带后缀的完整文件名，别让我替你挑。"
            % (path, len(hits), table, "、".join(hits)))
    if hits:
        return os.path.join(path, hits[0])
    raise SourceError("目录 %s 里没有叫「%s」的文件。有这些：%s"
                      % (path, table, "、".join(names[:12]) or "（一个可读的都没有）"))


def read(path, table=None):
    """读一个外部源，返回 (by_label, rows)。

    by_label 的形状和 `client.field_map()` 的第一个返回值一致，
    这样下游（业务键、取值、差异比对）一行代码都不用改：
    每一列都当成 text，**类型转换留给写入端按目标字段的类型做**
    ——外部文件里什么都是字符串，猜它是数字还是日期只会猜错。
    """
    path = resolve(path, table)
    kind = kind_of(path)
    if kind == "csv":
        cols, rows = _rows_csv(path)
    elif kind == "jsonl":
        cols, rows = _rows_jsonl(path)
    else:
        cols, rows = _rows_sqlite(path, table)
    dupes = [c for i, c in enumerate(cols) if c in cols[:i]]
    if dupes:
        raise SourceError("列名重复：%s —— 按显示名映射时会互相盖掉"
                          % "、".join(sorted(set(dupes))))
    return {c: TEXT(c) for c in cols}, rows


def stamp_ids(rows, by_label, key_label):
    """给外部行合成稳定的源端 ID。

    简道云那边靠 `_id` 认人，外部文件没有——只能用业务键。后果要说清楚：
    **业务键一改，ID 映射就认不出它了**，下次同步会当成新记录再写一条。
    所以外部源的业务键必须选一个真正不变的东西（工号、单号、手机号），
    而不是"客户名称"这种随时会被改的字段。
    """
    if key_label not in by_label:
        raise SourceError("业务键「%s」不在源文件的列里。列有：%s"
                          % (key_label, "、".join(list(by_label)[:12])))
    name = by_label[key_label]["name"]
    seen, out = {}, []
    for i, row in enumerate(rows, 1):
        key = str(row.get(name) or "").strip()
        if not key:
            raise SourceError("第 %d 行的业务键「%s」是空的——外部源靠它认人，不能为空"
                              % (i, key_label))
        if key in seen:
            raise SourceError("业务键「%s」= %s 在第 %d 行和第 %d 行重复——"
                              "外部源靠它认人，重复就没法匹配"
                              % (key_label, key, seen[key], i))
        seen[key] = i
        out.append(dict(row, _id="file:%s" % key))
    return out
