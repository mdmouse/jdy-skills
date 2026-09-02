#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按条件导出简道云数据到 xlsx。只读。

列名用显示名（人能看懂），并附 `_id` 列供后续回写定位。
关联字段导出的是数据 ID，会额外标注——因为它既看不懂、也导不回去。
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
TRIGGERS = ("从简道云导出数据", "拉一下某某表的数据", "导出附件")
from jdy_client import (ATTACHMENT_TYPES, build_filter, cli_main, EXPORT_ID_COLUMN,
                        EXPORT_TIME_COLUMN, JdyClient, JdyError, describe_targets,
                        display_value, print_targets, resolve_app, resolve_entry)
from xlsx import write_sheet


def _safe(name):
    """能当目录名用的形式。列名里出现 / 或 \\ 会把路径劈成两截。"""
    out = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", str(name)).strip(" .")
    return out[:60] or "_"


def grab_attachments(client, row, widget, dest_dir, failures):
    """下载这一格的附件，返回单元格该写什么（相对路径，多个用 | 分隔）。

    下不下来**不静默跳过**：url 过期、文件被删都会让这一格变空，
    而空格在表格里看着就像"这条本来就没附件"。记进 failures，最后一并说。
    """
    values = row.get(widget["name"]) or []
    if not isinstance(values, list):
        return None
    # **一条记录一列一个子目录。** 附件名是用户上传时的原名，重名太正常了；
    # 堆在一个目录里，download_file 会加 `-2`、`-3` 后缀去重——而导回时是按
    # 文件名判断"这一格改没改"的，被改过名的文件永远对不上，于是每次原样导回
    # 都把那几行重传重写一遍。去重后缀是在**掩盖**冲突，分目录是让它不发生。
    #
    # 三种重名都要挡住，少挡一种就等于没挡：
    #   跨记录——两条记录各有一个「合同.pdf」；
    #   跨列  ——同一条记录的两个附件列各有一个「合同.pdf」；
    #   格内  ——同一格里上传了两个同名文件（简道云允许）。
    # 前两种靠目录层级，第三种只能再分一层序号目录。
    files = [v for v in values if isinstance(v, dict) and v.get("url")]
    names = [v.get("name") or "" for v in files]
    cell_dir = os.path.join(dest_dir, _safe(row.get("_id") or "无ID"),
                            _safe(widget.get("label") or widget["name"]))
    numbered = len(set(names)) != len(names)      # 格内重名，才多分一层
    out = []
    for i, v in enumerate(files, 1):
        try:
            path = client.download_file(
                v["url"], os.path.join(cell_dir, str(i)) if numbered else cell_dir,
                v.get("name"))
            out.append(os.path.relpath(path, os.path.dirname(os.path.abspath(dest_dir))))
        except JdyError as exc:
            failures.append((row.get("_id"), widget["label"], v.get("name"), str(exc)))
            out.append("【下载失败】%s" % (v.get("name") or "?"))
    return " | ".join(out) or None


def main():
    ap = argparse.ArgumentParser(description="导出简道云数据到 xlsx（只读）")
    ap.add_argument("--app", help="应用 ID；不确定就先 --list")
    ap.add_argument("--entry", help="表单 ID；配合 --app --list 可列出")
    ap.add_argument("--out", help="输出 xlsx 路径")
    ap.add_argument("--attachments", metavar="目录",
                    help="把附件真的下载到这个目录，单元格里写相对路径。"
                         "不给就只导文件名——附件 URL 带过期戳（约 15 天），"
                         "放进表格过两周全是死链")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="列出应用；配合 --app 则列出该应用的表单与行数")
    ap.add_argument("--where", help="筛选条件：'字段标识=值'，多条用 ; 分隔；或一段 filter JSON")
    ap.add_argument("--limit", type=int, help="最多导出多少行，默认全部")
    ap.add_argument("--columns", help="只导出这些显示名的列，逗号分隔")
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
    if args.do_list or not (args.app and args.entry):
        items = describe_targets(client, args.app)
        print_targets(items, "应用：" if not args.app else "该应用下的表单：")
        if not args.app:
            print("\n用 --app <app_id> --list 看表单，再 --app A --entry E --out 文件.xlsx 导出。")
        elif not args.entry:
            print("\n用 --app %s --entry <entry_id> --out 文件.xlsx 导出。" % args.app)
        return 0
    if not args.out:
        sys.stderr.write("缺少 --out（输出 xlsx 路径）\n")
        return 2
    by_label, by_name = client.field_map(args.app, args.entry)
    try:
        data_filter = build_filter(args.where, by_label, by_name)
    except (ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    wanted = [c.strip() for c in args.columns.split(",")] if args.columns else None
    cols = [w for label, w in by_label.items() if not wanted or label in wanted]
    if wanted:
        missing = [c for c in wanted if c not in by_label]
        if missing:
            sys.stderr.write("表单里没有这些列：%s\n" % "、".join(missing))
            return 2

    def progress(n):
        if sys.stderr.isatty():
            sys.stderr.write("\r已拉取 %d 行 …" % n)
            sys.stderr.flush()

    rows, failures = [], []
    if args.attachments:
        os.makedirs(args.attachments, exist_ok=True)
    try:
        for row in client.iter_data(args.app, args.entry, data_filter=data_filter,
                                    limit=args.limit, progress=progress):
            out = {EXPORT_ID_COLUMN: row.get("_id")}
            for w in cols:
                if args.attachments and w["type"] in ATTACHMENT_TYPES:
                    out[w["label"]] = grab_attachments(client, row, w, args.attachments,
                                                       failures)
                    continue
                out[w["label"]] = display_value(row.get(w["name"]), w["type"])
            out["创建时间"] = row.get("createTime")
            rows.append(out)
    except JdyError as exc:
        sys.stderr.write("\n拉取失败：%s\n" % exc)
        return 2
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 30 + "\r")

    headers = [EXPORT_ID_COLUMN] + [w["label"] for w in cols] + [EXPORT_TIME_COLUMN]
    write_sheet(args.out, headers, rows, "导出数据")

    print("已导出 %d 行 → %s" % (len(rows), args.out))
    print("列：%d 个（含 _id 与创建时间）" % len(headers))
    notes = []
    if any(w["type"] == "linkdata" for w in cols):
        notes.append("关联字段（linkdata）导出的是数据 ID，没有显示值，且无法通过 API 导回")
    if any(w["type"] == "subform" for w in cols):
        notes.append("子表单只标了行数——子表单数据请单独导出")
    if any(w["type"] in ATTACHMENT_TYPES for w in cols):
        if args.attachments:
            notes.append("附件已下载到 %s（%d 个文件），单元格里是相对路径"
                         % (args.attachments, len(os.listdir(args.attachments))))
        else:
            notes.append("附件只导出文件名：附件 URL 带过期戳（实测约 15 天），"
                         "放进表格很快就失效。要连文件一起导出加 --attachments <目录>")
    if failures:
        notes.append("⚠️ 有 %d 个附件没下下来（url 过期或文件已删），"
                     "对应单元格标了【下载失败】：" % len(failures))
        for did, label, name, why in failures[:5]:
            notes.append("    %s 的「%s」%s —— %s" % (did, label, name, why[:50]))
    if any(w["type"] == "datetime" for w in cols):
        notes.append("日期为 UTC（简道云按 +8 显示），回填前注意时区")
    if notes:
        print("\n注意：")
        for n in notes:
            print("  · " + n)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
