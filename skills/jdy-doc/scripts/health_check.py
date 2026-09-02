#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简道云应用结构体检。只读。

判定依据见 references/health-rules.md。每条发现都带"为什么这是问题"，
因为用户要的是"该不该改"，不是"有几个字段叫单行文本_1"。
"""
import argparse
import re
import sys
from collections import defaultdict

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("简道云体检", "梳理应用结构")
from jdy_client import resolve_app, resolve_entry, cli_main, pad, JdyClient, JdyError

FIELD_LIMIT = 400              # 单表单字段上限
NEAR_LIMIT = 0.85

# 简道云新建控件的默认名，形如「单行文本」「数字_1」
DEFAULT_NAME = re.compile(
    r"^(单行文本|多行文本|数字|日期时间|下拉框|单选按钮组|复选框组|下拉复选框|"
    r"地址|定位|图片|附件|子表单|成员单选|成员多选|部门单选|部门多选|流水号|"
    r"手机|签名|分割线|说明文字)(_\d+)?$")

SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2}


class Finding(object):
    def __init__(self, severity, rule, impact, form, detail):
        self.severity, self.rule, self.impact = severity, rule, impact
        self.form, self.detail = form, detail


def check_form(client, app_id, form, widgets, samples):
    out = []
    fname = form["name"]
    flat = list(widgets)
    for w in widgets:
        if w.get("type") == "subform":
            flat.extend(w.get("items", []))

    # 1. 不可 API 导入的关联字段
    unwritable = [w["label"] for w in flat if w.get("type") == "linkdata"]
    if unwritable:
        out.append(Finding("高", "含不可 API 导入的关联字段",
                           "Excel 导入时这些列必然为空且不报错——「导入成功但数据不对」的头号成因",
                           fname, "字段：%s" % "、".join(unwritable)))

    # 2. 流水号冲突风险
    sn = [w["label"] for w in flat if w.get("type") == "sn"]
    if sn:
        out.append(Finding("中", "流水号存在重复风险",
                           "流水号由系统计数器生成，实测计数器可能与既有数据不同步而产生重复编号",
                           fname, "字段：%s" % "、".join(sn)))

    # 3. 默认字段名
    numbered, bare = [], []
    for w in flat:
        m = DEFAULT_NAME.match(str(w.get("label", "")))
        if m:
            (numbered if m.group(2) else bare).append(w["label"])
    if numbered:
        out.append(Finding("中", "字段仍用默认名（带序号）",
                           "「单行文本_1」这类名字人和 AI 都认不出含义，Excel 表头也匹配不上，导入映射会失败",
                           fname, "%d 个：%s%s" % (len(numbered), "、".join(numbered[:8]),
                                                  " …" if len(numbered) > 8 else "")))
    if bare:
        # 「附件」「签名」这类裸类型名往往是有意命名，只作提示
        out.append(Finding("低", "字段名与控件类型同名",
                           "不一定是问题，但同一表单有多个时会难以区分",
                           fname, "%d 个：%s%s" % (len(bare), "、".join(bare[:8]),
                                                  " …" if len(bare) > 8 else "")))

    # 4. 接近字段上限
    if len(flat) >= FIELD_LIMIT * NEAR_LIMIT:
        out.append(Finding("高" if len(flat) >= FIELD_LIMIT else "中", "字段数接近上限",
                           "单表单上限 %d 个字段，接近上限后无法再加字段，需要拆表" % FIELD_LIMIT,
                           fname, "当前 %d 个（含子表单内部字段）" % len(flat)))

    # 5. 孤儿字段：抽样若干行，从未被填过
    rows = []
    try:
        rows = client.fetch_all(app_id, form["entry_id"], limit=samples, page_size=100)
    except JdyError:
        pass
    if len(rows) >= 5:
        filled = defaultdict(int)
        for r in rows:
            for w in widgets:
                v = r.get(w["name"])
                if v not in (None, "", [], {}):
                    filled[w["name"]] += 1
        orphans = [w["label"] for w in widgets
                   if w.get("type") not in ("subform",) and filled.get(w["name"], 0) == 0]
        if orphans:
            out.append(Finding("低", "疑似孤儿字段",
                               "从未被填写过——可能是废弃字段，徒增表单复杂度与导入映射负担",
                               fname, "%d 个（抽样 %d 行）：%s%s"
                               % (len(orphans), len(rows), "、".join(orphans[:8]),
                                  " …" if len(orphans) > 8 else "")))
    return out, len(rows)


def main():
    ap = argparse.ArgumentParser(description="简道云应用结构体检（只读）")
    ap.add_argument("--app", required=True, help="应用 ID")
    ap.add_argument("--samples", type=int, default=30, help="孤儿字段检测抽样行数，默认 30")
    args = ap.parse_args()

    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2

    apps = client.list_apps()
    app_name = next((a["name"] for a in apps if a["app_id"] == args.app), args.app)
    try:
        if getattr(args, "app", None):
            args.app = resolve_app(client, args.app)
        if getattr(args, "entry", None) and getattr(args, "app", None):
            args.entry = resolve_entry(client, args.app, args.entry)
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    forms = client.list_forms(args.app)

    tty = sys.stderr.isatty()          # 非交互时不刷进度，否则污染 Agent 上下文
    findings, scanned_rows = [], 0
    for i, f in enumerate(forms, 1):
        if tty:
            sys.stderr.write("\r体检中 %d/%d …" % (i, len(forms)))
            sys.stderr.flush()
        widgets = client.widgets(args.app, f["entry_id"])
        got, n = check_form(client, args.app, f, widgets, args.samples)
        findings.extend(got)
        scanned_rows += n
    if tty:
        sys.stderr.write("\r" + " " * 40 + "\r")

    print("=" * 68)
    print("结构体检：%s" % app_name)
    print("%d 张表单，抽样 %d 行数据" % (len(forms), scanned_rows))
    print("=" * 68)

    if not findings:
        print("\n未发现结构问题。")
        return 0

    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.rule))
    by_rule = defaultdict(list)
    for f in findings:
        by_rule[(f.severity, f.rule, f.impact)].append(f)

    for (severity, rule, impact), items in by_rule.items():
        print("\n[%s] %s　（%d 张表单）" % (severity, rule, len(items)))
        print("  影响：%s" % impact)
        for it in items:
            print("    · %s %s" % (pad(it.form, 22), it.detail))

    counts = defaultdict(int)
    for f in findings:
        counts[f.severity] += 1
    print("\n" + "-" * 68)
    print("合计 %d 项：%s" % (len(findings),
                             "　".join("%s %d" % (s, counts[s]) for s in ("高", "中", "低") if counts[s])))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
