#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入预检：Excel 表头映射 + 逐格类型校验 + 值预检。只读，不写任何数据。

为什么预检是必需的而不是加分项：简道云 batch_create 几乎不校验，
脏值静默存 null 并返回 success——API 里根本没有"失败行"这个概念。
不在写入前拦住，就没有第二次机会发现。
"""
import argparse
import json
import os
import re
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("把 Excel 导入简道云", "导入报错")
from jdy_client import (ATTACHMENT_TYPES, cli_main, pad, LOOKUP_TYPES, NOT_WRITABLE_TYPES,
                        READ_ONLY_TYPES, JdyClient,
                        JdyError, describe_targets, resolve_app, resolve_entry, encode_value, infer_lookup_target,
                        lookup_exists, is_data_id, display_value, parse_tz, print_targets,
                        EXPORT_ID_COLUMN, EXPORT_SYSTEM_COLUMNS)
from xlsx import XlsxError, read_table

# 表头常见装饰：*必填、（单位：元）、[必填]、末尾冒号
_DECOR = re.compile(r"[\*＊]|[（(\[【][^）)\]】]*[）)\]】]|[:：]\s*$")
_SPACE = re.compile(r"[\s　]+")


def normalize(text):
    """归一化表头用于模糊匹配：去装饰、去空格、全角转半角。"""
    if text is None:
        return ""
    s = str(text)
    s = "".join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in s)
    return _SPACE.sub("", _DECOR.sub("", s)).lower()


def match_headers(headers, by_label):
    """表头 → 字段。精确名 > 归一化名 > 单向包含。

    返回 (mapping, unmatched, ambiguous)。

    候选多于一个时**不自动选**，而是记进 ambiguous 交用户裁决——
    「这列有 2 个候选」和「这列没匹配上」对用户是完全不同的指令，
    混为一谈会把选择题变成死路。
    """
    norm_fields = {}
    for label, w in by_label.items():
        norm_fields.setdefault(normalize(label), []).append(w)

    mapping, unmatched, ambiguous, used = {}, [], {}, set()
    for h in headers:
        if h in by_label:                                   # 1. 精确
            mapping[h] = {"field": by_label[h], "how": "精确匹配"}
            used.add(by_label[h]["name"])
            continue
        nh = normalize(h)
        cands = [w for w in norm_fields.get(nh, []) if w["name"] not in used]
        if len(cands) == 1:                                 # 2. 归一化后唯一
            mapping[h] = {"field": cands[0], "how": "归一化匹配（去空格/装饰/全角）"}
            used.add(cands[0]["name"])
            continue
        if len(cands) > 1:
            ambiguous[h] = [w["label"] for w in cands]
            continue
        if nh:                                              # 3. 包含关系
            loose = [w for label, w in by_label.items()
                     if w["name"] not in used and
                     (nh in normalize(label) or normalize(label) in nh)]
            if len(loose) == 1:
                mapping[h] = {"field": loose[0], "how": "模糊匹配（包含关系）⚠️ 请确认"}
                used.add(loose[0]["name"])
                continue
            if len(loose) > 1:
                ambiguous[h] = [w["label"] for w in loose]
                continue
        unmatched.append(h)
    return mapping, unmatched, ambiguous


ID_COLUMN = EXPORT_ID_COLUMN     # 导出的定位列：有值即更新，为空即新增


CHOICE_TYPES = ("checkboxgroup", "combocheck", "combo", "radiogroup")


def sample_rows(client, app_id, entry_id, sample=200):
    """抽一批既有数据当索引用。**只拉这一次**，成员索引和选项索引共用。"""
    try:
        return client.fetch_all(app_id, entry_id, limit=sample, page_size=100)
    except JdyError:
        return []


def build_option_index(rows, choice_fields):
    """从既有数据里收集每个选择型字段**出现过**的选项。

    为什么需要：实测写一个**不存在的选项**，接口照样原样存下
    （2026-08-31，checkboxgroup/combocheck 都是），而 `widget/list` 只返回
    name/label/type，**不给选项列表**——和"不返回 lookup 指向哪张表"同一个缺口。
    于是 Excel 里一个错别字就会静默变成一个界面上根本没有的选项。

    这个索引是**启发式**，不是权威：它只覆盖历史数据里出现过的值，
    所以只用来提醒，绝不据此扣下整行——真要新增一个选项是完全合法的。
    """
    index = {}
    for row in rows:
        for w in choice_fields:
            v = row.get(w["name"])
            for item in (v if isinstance(v, list) else [v]):
                if isinstance(item, str) and item.strip():
                    index.setdefault(w["name"], set()).add(item.strip())
    return {k: sorted(v) for k, v in index.items()}


def build_user_index(rows, user_fields):
    """从表内既有数据反查「姓名 → username」。

    简道云没有可用的通讯录查询接口，只能拿本表历史数据当索引。
    因此它只能覆盖出现过的人，是**辅助**而非权威——覆盖不到的必须由用户提供 username。
    重名会被记成多个候选，交由用户裁决，绝不自动挑一个。
    """
    index = {}
    if not user_fields:
        return index
    for row in rows:
        for w in user_fields:
            v = row.get(w["name"])
            for item in (v if isinstance(v, list) else [v]):
                if isinstance(item, dict) and item.get("name") and item.get("username"):
                    index.setdefault(item["name"], set()).add(item["username"])
    return {k: sorted(v) for k, v in index.items()}


def row_label(rec, limit=2):
    """给一行取个人认得出的标识：按表头顺序取前几个非空业务值。

    只报「第 6 行有问题」是不够的——用户脑子里是名字不是行号，Agent 就会
    自己去文件里数行来补充说明，然后数错（实测里它把第 6 行说成了第 7 行的人，
    差点让用户对着错的记录拍板）。行号旁边必须带上这行是谁。
    """
    out = []
    for key, value in rec.items():
        if key in EXPORT_SYSTEM_COLUMNS:
            continue
        text = str(value).strip() if value is not None else ""
        if text:
            out.append(text[:16])
        if len(out) >= limit:
            break
    return " / ".join(out) or "（整行为空）"


def values_match(stored, values, by_label):
    """Excel 这一行的值，和库里那条记录完全一致吗？

    导出全表、只改两行、再整份导回，是最自然的用法。若不判断，
    另外 21 行也会各发一次更新——每次更新都是一次静默丢字段的机会，
    收益却为零。没改就别写。

    比较用**显示值**：Excel 里是人看到的字符串，库里是原始结构
    （user 是对象、phone 是 {verified, phone}），只有转成显示值才可比。
    """
    for label, new in values.items():
        w = by_label.get(label)
        if w is None:
            return False                      # 字段对不上，宁可写一次
        if w["type"] in ATTACHMENT_TYPES:
            if _same_files(stored.get(w["name"]), new):
                continue
            return False
        old = display_value(stored.get(w["name"]), w["type"])
        if str("" if old is None else old) != str("" if new is None else new):
            return False
    return True


def _same_files(stored, cell):
    """附件列：库里存的和 Excel 里写的是不是同一批文件。**按文件名比。**

    两边根本不是一种东西：库里是 `[{name,size,mime,url}]`，Excel 里是本地
    路径 `附件/合同A.pdf`。直接比显示值永远不相等——于是**每一次导回都会把
    所有带附件的行判成"改过了"**，把文件重新上传一遍、把记录重写一遍。
    白耗上传凭证，把操作日志刷满，而每次重写都是一次静默丢字段的机会。
    「导出→改两行→整份导回」正是本技能推荐的用法。

    只比文件名，不比内容——本地文件换了内容、名字没变时会被当成没改。
    那是有意的取舍：读回来的附件 url 带过期戳、要下载才拿得到内容，
    为了判断"改没改"而把整表的附件都下载一遍，代价远大于收益。
    要强制重传就把文件改个名。
    """
    have = sorted(os.path.basename(f.get("name") or "")
                  for f in (stored or []) if isinstance(f, dict))
    if isinstance(cell, (list, tuple)):
        items = list(cell)
    else:
        items = [p for p in str(cell or "").split("|")]
    want = sorted(os.path.basename(str(p).strip()) for p in items if str(p).strip())
    return have == want


def unknown_options(value, widget, option_index):
    """这一格里有哪些选项是历史数据里没见过的。见 build_option_index 的说明。"""
    known = option_index.get(widget["name"])
    if not known:
        return []                       # 没有样本就无从判断，别瞎报
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [p.strip() for p in re.split(r"[,，、;；]", str(value))]
    return [i for i in items if i and i not in known]


def attachment_paths(value, base_dir):
    """附件单元格 → 本地文件路径列表。多个文件用 `|` 分隔（导出侧就是这么写的）。

    相对路径按 **Excel 所在目录**解析，不按当前工作目录——同一份表格换个目录跑
    就找不到文件，那种失败很难看出原因。
    """
    out = []
    for part in str(value).split("|"):
        part = part.strip()
        if not part:
            continue
        path = os.path.expanduser(part)
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(base_dir, path))
        out.append(path)
    return out


def check_cells(records, mapping, user_index, tz=None, lookup_check=None,
                id_check=None, option_index=None, base_dir=None):
    """逐格校验。返回 (issues, clean_rows, held_rows)。

    lookup_check(widget, data_id) → True/False/None：
    关联数据字段写入时接口**不校验引用是否存在**，写个不存在的 ID 照样入库、
    照样回读"一致"——所以连回读比对都兜不住，只能在这里查。
    返回 None 表示推断不出目标表、无法校验，如实记为警告而不是放行。

    有问题的行**整行扣下**，不做"剔掉坏字段照写"——那等于亲手制造一条缺字段的记录，
    而且用户修好再导会变成重复行。扣下来，修完只补这几行，才是完整的修复循环。
    """
    issues, clean, held, warnings = [], [], [], []
    option_index = option_index or {}
    for i, rec in enumerate(records):
        row_out, row_num, row_issues = {}, i + 2, []        # +2：表头占第 1 行
        who = row_label(rec)

        # _id 决定这行是"改"还是"增"。它不是表单字段，不参与映射。
        data_id = str(rec.get(ID_COLUMN) or "").strip()
        if data_id:
            if not is_data_id(data_id):
                row_issues.append({"row": row_num, "column": ID_COLUMN, "value": data_id[:60],
                                   "who": who, "kind": "bad_data_id",
                                   "detail": "不是合法的记录 ID（应为 24 位十六进制）。"
                                             "留空表示新增，别填别的东西"})
            elif id_check and id_check(data_id) is False:
                row_issues.append({"row": row_num, "column": ID_COLUMN, "value": data_id,
                                   "who": who, "kind": "data_id_missing",
                                   "detail": "目标表里没有这条记录——可能已被删除，"
                                             "或这个 _id 来自别的表单。**不会当成新增写进去**"})

        for header, m in mapping.items():
            w = m["field"]
            value = rec.get(header)
            if value is None or value == "":
                continue
            if w["type"] in NOT_WRITABLE_TYPES or w["type"] in READ_ONLY_TYPES:
                continue                                     # 已在列级别报过，不逐行刷屏
            if w["type"] == "user":
                cands = user_index.get(str(value).strip())
                if cands and len(cands) == 1:
                    row_out[w["label"]] = cands[0]
                    continue
                if cands:
                    row_issues.append({"row": row_num, "column": header, "value": value,
                                       "kind": "user_ambiguous",
                                       "detail": "「%s」对应多个成员：%s —— 需指定 username"
                                                 % (value, "、".join(cands))})
                    continue
            if w["type"] in ATTACHMENT_TYPES:
                # 附件列填的是**本地文件路径**。导之前就把"文件在不在"查掉——
                # 留到上传时才发现，前面的行已经写进去了，只能补第二遍。
                paths = attachment_paths(value, base_dir or ".")
                missing = [p for p in paths if not os.path.isfile(p)]
                if missing:
                    row_issues.append({
                        "row": row_num, "column": header, "who": who,
                        "value": "、".join(os.path.basename(p) for p in missing),
                        "kind": "file_missing",
                        "detail": "这些文件找不到：%s。附件列填的是本地文件路径，"
                                  "相对路径按 Excel 所在目录算" % "、".join(missing[:3])})
                    continue
                row_out[w["label"]] = paths          # 交给导入阶段上传
                continue
            if w["type"] in CHOICE_TYPES and option_index:
                unknown = unknown_options(value, w, option_index)
                if unknown:
                    # **只提醒不扣行**：新增一个选项是完全合法的，而这个索引
                    # 只覆盖历史数据里出现过的值。但错别字也长这样，
                    # 而写错的选项会被接口原样存下——所以必须说一声。
                    warnings.append({"row": row_num, "column": header, "who": who,
                                     "value": "、".join(unknown), "kind": "unknown_option",
                                     "detail": "「%s」在这一列的历史数据里没出现过。"
                                               "接口**不校验选项**，写错了会原样存进去、"
                                               "在界面上显示成一个不存在的选项。"
                                               "确认是新增选项就忽略这条。"
                                               % "、".join(unknown)})
            if w["type"] in LOOKUP_TYPES and lookup_check:
                try:
                    encoded = encode_value(w, value, tz=tz)
                except ValueError as exc:
                    row_issues.append({"row": row_num, "column": header,
                                       "value": str(value)[:60], "kind": "bad_value",
                                       "detail": str(exc)})
                    continue
                exists = lookup_check(w, encoded)
                if exists is False:
                    row_issues.append({
                        "row": row_num, "column": header, "value": str(value)[:60],
                        "kind": "lookup_missing",
                        "detail": "关联的目标记录不存在。接口不会拦——写进去会变成"
                                  "一个指向虚无的引用，且回读比对也发现不了"})
                    continue
                if exists is None:
                    issues.append({"row": row_num, "column": header,
                                   "value": str(value)[:60], "kind": "lookup_unverified",
                                   "detail": "推断不出该关联字段指向哪张表，**无法校验引用是否存在**"})
                row_out[w["label"]] = value
                continue
            try:
                encode_value(w, value, tz=tz)
            except Exception as exc:
                kind = "user_unresolved" if w["type"] == "user" else "bad_value"
                row_issues.append({"row": row_num, "column": header, "value": str(value)[:60],
                                   "kind": kind, "detail": str(exc)})
                continue
            row_out[w["label"]] = value
        issues.extend(row_issues)
        if row_issues:
            held.append({"row": row_num, "who": who, "data": row_out,
                         "data_id": data_id or None, "issues": row_issues})
        else:
            clean.append({"values": row_out, "data_id": data_id or None,
                          "row": row_num, "who": who})
    return issues, clean, held, warnings


def main():
    ap = argparse.ArgumentParser(description="简道云导入预检（只读）")
    ap.add_argument("excel", nargs="?", help="待导入的 xlsx 文件")
    ap.add_argument("--app", help="应用 ID；不确定就先 --list")
    ap.add_argument("--entry", help="表单 ID；配合 --app --list 可列出")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="列出应用；配合 --app 则列出该应用的表单与行数")
    ap.add_argument("--sheet", help="工作表名，默认第一个")
    ap.add_argument("--header-row", type=int, default=1, help="表头所在行号，默认 1")
    ap.add_argument("--plan", help="把预检结果存成导入计划 JSON，供 import_data.py 使用")
    ap.add_argument("--tz", default="+08:00",
                    help="Excel 里时间所属的时区，默认 +08:00（北京时间）。"
                         "可填 utc / -04:00 / local。**不要用 local 除非确认机器时区就是数据时区**")
    args = ap.parse_args()

    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2
    try:
        if args.app:
            args.app = resolve_app(client, args.app)
        if getattr(args, "entry", None) and args.app:
            args.entry = resolve_entry(client, args.app, args.entry)
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    if args.do_list or not (args.app and args.entry and args.excel):
        items = describe_targets(client, args.app)
        print_targets(items, "应用：" if not args.app else "该应用下的表单：")
        print("\n用法：preflight.py <excel> --app <app_id> --entry <entry_id>")
        return 0
    try:
        headers, records = read_table(args.excel, args.sheet, args.header_row - 1)
    except XlsxError as exc:
        sys.stderr.write("读取 Excel 失败：%s\n" % exc)
        return 2

    by_label, _ = client.field_map(args.app, args.entry)
    mapping, unmatched, ambiguous = match_headers(headers, by_label)
    user_fields = [m["field"] for m in mapping.values() if m["field"]["type"] == "user"]
    choice_fields = [m["field"] for m in mapping.values()
                     if m["field"]["type"] in CHOICE_TYPES]
    # 一次采样喂两个索引：成员姓名→username，以及选择型字段出现过的选项
    samples = sample_rows(client, args.app, args.entry) if (user_fields or choice_fields) else []
    user_index = build_user_index(samples, user_fields)
    option_index = build_option_index(samples, choice_fields)
    try:
        tz = parse_tz(args.tz)
    except ValueError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    # 关联数据字段：先推断各自指向哪张表，才能校验引用存在性
    lookup_targets, lookup_cache = {}, {}
    for m in mapping.values():
        w = m["field"]
        if w["type"] in LOOKUP_TYPES and w["name"] not in lookup_targets:
            lookup_targets[w["name"]] = infer_lookup_target(client, args.app, args.entry, w)

    def lookup_check(widget, data_id):
        target = lookup_targets.get(widget["name"])
        if target is None:
            return None
        key = (target, data_id)
        if key not in lookup_cache:
            lookup_cache[key] = lookup_exists(client, args.app, target, data_id)
        return lookup_cache[key]

    mapping.pop(ID_COLUMN, None)          # _id 是定位列，不是要写的字段
    ambiguous.pop(ID_COLUMN, None)
    sys_cols = [h for h in headers if h in EXPORT_SYSTEM_COLUMNS and h not in mapping]
    unmatched = [h for h in unmatched if h not in EXPORT_SYSTEM_COLUMNS]
    has_id_col = ID_COLUMN in headers

    # 只取**文件里出现过的**那些 _id。原来无条件全表拉：一份 5 行的 Excel
    # 对着 50 万行的表做预检，要拉 5000 页——而且只用得上其中 5 行。
    # fetch_rows_by_id 的代价跟着这批 id 走，表多大都有上界。
    existing = None
    wanted_ids = [str(r.get(ID_COLUMN) or "").strip() for r in records]
    wanted_ids = [i for i in wanted_ids if i]
    if has_id_col and wanted_ids:
        try:
            existing = client.fetch_rows_by_id(args.app, args.entry, wanted_ids)
        except JdyError:
            existing = None           # 拉不到就不校验，别把预检卡死

    def id_check(data_id):
        return None if existing is None else (data_id in existing)

    def unchanged(data_id, values):
        if existing is None or data_id not in existing:
            return False
        return values_match(existing[data_id], values, by_label)

    issues, clean_rows, held_rows, warnings = check_cells(
        records, mapping, user_index, tz=tz, lookup_check=lookup_check,
        id_check=id_check, option_index=option_index,
        base_dir=os.path.dirname(os.path.abspath(args.excel)))

    blocked = [(h, m["field"]) for h, m in mapping.items()
               if m["field"]["type"] in NOT_WRITABLE_TYPES or m["field"]["type"] in READ_ONLY_TYPES]
    fuzzy = [(h, m) for h, m in mapping.items() if "模糊" in m["how"]]
    unmapped_fields = [l for l, w in by_label.items()
                       if w["name"] not in {m["field"]["name"] for m in mapping.values()}
                       and w["type"] not in NOT_WRITABLE_TYPES and w["type"] not in READ_ONLY_TYPES]

    print("=" * 70)
    print("导入预检：%s → %s" % (os.path.basename(args.excel), args.entry))
    print("%d 列表头，%d 行数据　源时区 %s" % (len(headers), len(records), args.tz))
    print("=" * 70)

    print("\n【字段映射】")
    if has_id_col:
        print("  %s → 记录定位列（不写入）：有值的行走**更新**，留空的行走**新增**"
              % pad(ID_COLUMN, 20))
    for h in sys_cols:
        if h != ID_COLUMN:
            print("  %s → 导出附带的系统列，导入时忽略（正常，不是问题）" % pad(h, 20))
    for h in headers:
        if h in sys_cols:
            continue                      # 系统列已单独说明，不当成"无匹配字段"报错
        m = mapping.get(h)
        if m:
            print("  %s → %s (%s，%s)" % (pad(h, 20), pad(m["field"]["label"], 20), m["field"]["type"], m["how"]))
        elif h in ambiguous:
            print("  %s → ❓ 有 %d 个候选，请指定：%s"
                  % (h, len(ambiguous[h]), "、".join(ambiguous[h])))
        else:
            print("  %s → ❌ 无匹配字段（该列会被简道云静默忽略）" % pad(h, 20))

    if blocked:
        print("\n【❌ 这些列导不进去】——写了也会静默留空，不是报错")
        for h, w in blocked:
            why = "接口不支持写入该类型" if w["type"] in NOT_WRITABLE_TYPES else "由系统生成"
            print("  %s %s：%s" % (pad(h, 20), w["type"], why))

    if fuzzy:
        print("\n【⚠️ 模糊匹配，请人工确认】")
        for h, m in fuzzy:
            print("  %s → %s" % (pad(h, 20), m["field"]["label"]))

    if ambiguous:
        print("\n【❓ 需要你指定映射到哪个字段】——候选不止一个，不敢替你选")
        for h, cands in ambiguous.items():
            print("  %s 候选：%s" % (pad(h, 20), "、".join(cands)))
    if unmatched:
        print("\n【Excel 有但表单没有的列】共 %d 个：%s" % (len(unmatched), "、".join(unmatched)))
    if unmapped_fields:
        print("\n【表单有但 Excel 没提供的字段】共 %d 个：%s%s"
              % (len(unmapped_fields), "、".join(unmapped_fields[:10]),
                 " …" if len(unmapped_fields) > 10 else ""))

    if issues:
        print("\n【数据问题】共 %d 处" % len(issues))
        by_kind = {}
        for it in issues:
            by_kind.setdefault(it["kind"], []).append(it)
        for kind, items in by_kind.items():
            print("\n  · %s（%d 处）" % (kind, len(items)))
            for it in items[:8]:
                who = ("（%s）" % it["who"]) if it.get("who") else ""
                print("      第 %d 行%s「%s」= %r：%s"
                      % (it["row"], who, it["column"], it["value"], it["detail"]))
            if len(items) > 8:
                print("      … 另有 %d 处" % (len(items) - 8))
    else:
        print("\n【数据问题】无")

    if ambiguous:
        verdict = "需先确认字段映射再导入"
    elif issues:
        verdict = "建议先修数据再导入"
    elif blocked:
        verdict = "可以导入，但有列会留空"
    else:
        verdict = "可以导入"
    creates = [r for r in clean_rows if not r["data_id"]]
    same = [r for r in clean_rows if r["data_id"] and unchanged(r["data_id"], r["values"])]
    same_ids = {id(r) for r in same}
    updates = [r for r in clean_rows if r["data_id"] and id(r) not in same_ids]

    if warnings:
        print("\n【提醒】共 %d 处——不扣行，但请确认" % len(warnings))
        for wn in warnings[:8]:
            print("  · 第 %d 行「%s」%s" % (wn["row"], wn["column"], wn["detail"]))
        if len(warnings) > 8:
            print("  … 另有 %d 处" % (len(warnings) - 8))

    print("\n" + "-" * 70)
    print("结论：%s" % verdict)
    if updates:
        print("  更新已有记录    %d 行（按 %s 列定位，不会产生重复行）"
              % (len(updates), ID_COLUMN))
        for u in updates[:10]:        # 让用户确认写入，就得说清改的是哪几条
            print("        第 %d 行  %s" % (u["row"], u.get("who") or ""))
        if len(updates) > 10:
            print("        …另有 %d 行" % (len(updates) - 10))
    if same:
        print("  无变化跳过      %d 行——值与库里一致，不重复写"
              % len(same))
    print("  新增记录        %d 行（预计 %.1f 秒）"
          % (len(creates), client.estimate_seconds(
              len(creates), "/app/entry/data/batch_create", 100)))
    for c in creates[:10]:
        print("        第 %d 行  %s" % (c["row"], c.get("who") or ""))
    if len(creates) > 10:
        print("        …另有 %d 行" % (len(creates) - 10))
    if has_id_col and not updates and creates:
        print("  ⚠️ 表里有 %s 列但**全为空**——这 %d 行会当成新增。"
              "如果本意是改回原记录，请确认 %s 没被清掉"
              % (ID_COLUMN, len(creates), ID_COLUMN))
    if held_rows:
        print("  扣下待修        %d 行 —— 修好后重跑预检再导，避免重复写入"
              % len(held_rows))
        for h in held_rows[:10]:
            print("        第 %d 行  %s" % (h["row"], h.get("who") or ""))

    if args.plan:
        with open(args.plan, "w", encoding="utf-8") as fh:
            json.dump({"app_id": args.app, "entry_id": args.entry, "source": args.excel,
                       "tz": args.tz,
                       "mapping": {h: {"label": m["field"]["label"], "type": m["field"]["type"],
                                       "how": m["how"]} for h, m in mapping.items()},
                       "blocked_columns": [h for h, _ in blocked],
                       "ambiguous_columns": ambiguous,
                       "issues": issues,
                       "warnings": warnings,
                       # creates 和 updates 同一个形状：都带 row 与 who。
                       # 原来 creates 只写 values，行号和"这行是谁"在计划里就丢了，
                       # 于是导入阶段报「数字：无法解析为数字」时**指不出是哪一行**——
                       # 200 行的文件里这句话等于没说。而 row_label() 的存在
                       # 本来就是为了"行号旁边必须带上这行是谁"。
                       "creates": [{"values": r["values"], "row": r["row"],
                                    "who": r.get("who")} for r in creates],
                       "updates": [{"data_id": r["data_id"], "values": r["values"],
                                    "row": r["row"], "who": r.get("who")}
                                   for r in updates],
                       "held_rows": held_rows}, fh, ensure_ascii=False, indent=2)
        print("导入计划已保存：%s" % args.plan)
        print("确认无误后执行：python3 scripts/import_data.py %s --execute" % args.plan)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
