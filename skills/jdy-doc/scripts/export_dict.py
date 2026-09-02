#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出简道云应用的 Markdown 数据字典。只读。"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401  定位共享内核

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("数据字典", "应用结构", "字段说明")
import brand
from jdy_client import (cli_main, NOT_WRITABLE_TYPES, READ_ONLY_TYPES, JdyClient, JdyError,
                        describe_targets, resolve_app, resolve_entry, print_targets)

TYPE_CN = {
    "text": "单行文本", "textarea": "多行文本", "number": "数字", "datetime": "日期时间",
    "radiogroup": "单选", "checkboxgroup": "复选", "combo": "下拉单选", "combocheck": "下拉复选",
    "address": "地址", "location": "定位", "image": "图片", "upload": "附件",
    "subform": "子表单", "user": "成员单选", "usergroup": "成员多选",
    "dept": "部门单选", "deptgroup": "部门多选", "sn": "流水号", "phone": "电话",
    "company": "企业名称", "signature": "签名", "lookup": "关联查询(lookup)",
    "linkdata": "数据关联(linkdata)", "linkobject": "关联表单(linkobject)",
}


def writability(wtype):
    if wtype in NOT_WRITABLE_TYPES:
        return "❌ 不可写"
    if wtype in READ_ONLY_TYPES:
        return "系统生成"
    if wtype == "linkobject":
        return "⚠️ 待实测"
    return "✅"


def field_rows(widgets, indent=""):
    lines = []
    for w in widgets:
        wtype = w.get("type", "")
        lines.append("| %s%s | %s | `%s` | %s |" % (
            indent, w.get("label", ""), TYPE_CN.get(wtype, wtype), w.get("name", ""),
            writability(wtype)))
        if wtype == "subform":
            lines.extend(field_rows(w.get("items", []), indent="&nbsp;&nbsp;↳ "))
    return lines


def render(app_name, app_id, forms):
    out = ["# 数据字典：%s" % app_name, "",
           "- 应用 ID：`%s`" % app_id,
           "- 表单数：%d" % len(forms),
           "- 字段总数：%d" % sum(f["field_count"] for f in forms),
           "", "> 「可 API 写入」一列基于实测：`linkdata` 无法通过 API 写入，",
           "> `sn` 由系统生成。含 ❌ 的列在 Excel 导入时会静默留空。", ""]
    out += ["## 表单一览", "", "| 表单 | 字段数 | 表单 ID |", "|---|---|---|"]
    for f in forms:
        out.append("| %s | %d | `%s` |" % (f["name"], f["field_count"], f["entry_id"]))
    out.append("")
    for f in forms:
        out += ["## %s" % f["name"], "",
                "表单 ID：`%s`　字段数：%d" % (f["entry_id"], f["field_count"]), "",
                "| 字段 | 类型 | 字段标识 | 可 API 写入 |", "|---|---|---|---|"]
        out += field_rows(f["widgets"])
        out.append("")
    foot = brand.md_footer()
    if foot:                       # 关掉时连那条分隔线和空行都不留
        out += ["---", "", foot]
    return "\n".join(out)


def collect(client, app_id, progress=True):
    progress = progress and sys.stderr.isatty()   # 非交互时静默，避免污染 Agent 上下文
    forms = []
    listing = client.list_forms(app_id)
    for i, f in enumerate(listing, 1):
        widgets = client.widgets(app_id, f["entry_id"])
        count = len(widgets) + sum(len(w.get("items", [])) for w in widgets if w.get("type") == "subform")
        forms.append({"name": f["name"], "entry_id": f["entry_id"],
                      "widgets": widgets, "field_count": count})
        if progress:
            sys.stderr.write("\r抓取表单结构 %d/%d …" % (i, len(listing)))
            sys.stderr.flush()
    if progress and listing:
        sys.stderr.write("\r" + " " * 40 + "\r")
    return forms


def main():
    ap = argparse.ArgumentParser(description="导出简道云数据字典（只读）")
    ap.add_argument("--app", help="应用 ID")
    ap.add_argument("--list", action="store_true", dest="do_list", help="列出全部应用")
    ap.add_argument("--out", help="Markdown 输出路径，缺省打印到标准输出")
    ap.add_argument("--json-out", "--json", dest="json_out", help="同时另存结构化 JSON")
    args = ap.parse_args()

    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2

    apps = client.list_apps()
    try:
        if args.app:
            args.app = resolve_app(client, args.app)
        if getattr(args, "entry", None) and args.app:
            args.entry = resolve_entry(client, args.app, args.entry)
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    if args.do_list or not args.app:
        items = describe_targets(client, args.app)
        print_targets(items, "授权范围内的应用：" if not args.app else "该应用下的表单：")
        if not args.app:
            print("\n用 --app <app_id> --list 看表单，或 --app <app_id> --out 字典.md 导出。")
        return 0

    app_name = next((a["name"] for a in apps if a["app_id"] == args.app), args.app)
    forms = collect(client, args.app)
    doc = render(app_name, args.app, forms)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print("数据字典已导出：%s（%d 张表单，%d 个字段）"
              % (args.out, len(forms), sum(f["field_count"] for f in forms)))
    else:
        print(doc)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"app_id": args.app, "app_name": app_name, "forms": forms},
                      fh, ensure_ascii=False, indent=2)
        print("结构化副本：%s" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
