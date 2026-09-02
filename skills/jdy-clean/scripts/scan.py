#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描一张表的数据质量。只读，不改任何数据。

报四件事：填充率、格式是否统一、逐值毛病（首尾空白/全角/控制字符）、重复值。
**只描述现象，不下业务判断**——"手机号该 11 位"是领域知识，
引擎只能说"这列 90% 是 9{11}，另外 10% 是 9{3}-9{4}-9{4}"。
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("数据质量", "数据体检", "这表能用吗", "数据太脏")
from jdy_client import (JdyClient, cli_main, describe_targets,
                        col_width, display_value, pad, print_targets, resolve_app,
                        resolve_entry)
from quality import column_profile, duplicate_groups

SHAPE_ALERT = 3          # 一列出现超过这么多种形状就值得看一眼


def scan(client, app_id, entry_id, limit=None):
    by_label, _ = client.field_map(app_id, entry_id)
    rows = client.fetch_all(app_id, entry_id, limit=limit)
    report = {"rows": len(rows), "columns": []}
    for label, widget in by_label.items():
        values = [display_value(r.get(widget["name"]), widget["type"]) for r in rows]
        prof = column_profile(values)
        prof.update({"label": label, "type": widget["type"]})
        prof["duplicates"] = len(duplicate_groups(
            rows, lambda r, w=widget: display_value(r.get(w["name"]), w["type"])))
        report["columns"].append(prof)
    return report, rows, by_label


def main():
    ap = argparse.ArgumentParser(description="数据质量扫描（只读）")
    ap.add_argument("--app", help="应用名或 ID；不确定就先 --list")
    ap.add_argument("--entry", help="表单名或 ID")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="列出应用；配合 --app 则列出该应用的表单")
    ap.add_argument("--limit", type=int, help="只扫前 N 行（大表先抽样）")
    ap.add_argument("--json-out", help="同时另存结构化结果")
    args = ap.parse_args()

    client = JdyClient()
    if args.do_list or not (args.app and args.entry):
        app_id = resolve_app(client, args.app) if args.app else None
        print_targets(describe_targets(client, app_id),
                      "应用：" if not app_id else "该应用下的表单：")
        print("\n用法：scan.py --app <应用名或ID> --entry <表单名或ID>")
        return 0
    args.app = resolve_app(client, args.app)
    args.entry = resolve_entry(client, args.app, args.entry)

    report, rows, _ = scan(client, args.app, args.entry, args.limit)
    cols = report["columns"]
    width = col_width([c["label"] for c in cols], 4)

    print("=" * 72)
    print("数据质量扫描：%d 行 × %d 列%s"
          % (report["rows"], len(cols),
             "（只扫了前 %d 行）" % args.limit if args.limit else ""))
    print("=" * 72)

    print("\n【填充率】")
    for c in sorted(cols, key=lambda c: c["fill_rate"]):
        bar = "█" * int(c["fill_rate"] * 20)
        print("  %s %5.1f%%  %-20s %s"
              % (pad(c["label"], width), c["fill_rate"] * 100, bar,
                 "← 整列为空" if c["filled"] == 0 else ""))

    mixed = [c for c in cols if len(c["shapes"]) > SHAPE_ALERT]
    print("\n【格式不统一】共 %d 列（形状旁边是该形状的一个真实样例）" % len(mixed))
    for c in mixed:
        print("  %s %d 种写法：" % (pad(c["label"], width), len(c["shapes"])))
        for sig, n in c["shapes"][:4]:
            share = n / float(c["filled"]) if c["filled"] else 0
            flag = "  ← 占 %.0f%%" % (share * 100) if share >= 0.5 else ""
            print("  %s   %-22s ×%-4d 例：%r%s"
                  % (pad("", width), sig, n, c["samples"].get(sig, ""), flag))
        if len(c["shapes"]) > 4:
            print("  %s   …另有 %d 种" % (pad("", width), len(c["shapes"]) - 4))
    if not mixed:
        print("  无")

    flawed = [c for c in cols if c["flaws"]]
    print("\n【逐值毛病】共 %d 列" % len(flawed))
    for c in flawed:
        print("  %s %s" % (pad(c["label"], width),
                           "、".join("%s×%d" % (k, n) for k, n in c["flaws"])))
    if not flawed:
        print("  无")

    dup_cols = [c for c in cols if c["duplicates"] and c["uniqueness"] > 0.5]
    print("\n【值重复】（唯一度 >50% 的列才报——低唯一度的列本来就该重复）")
    for c in sorted(dup_cols, key=lambda c: -c["duplicates"]):
        print("  %s %d 组重复（唯一度 %.0f%%）"
              % (pad(c["label"], width), c["duplicates"], c["uniqueness"] * 100))
    if not dup_cols:
        print("  无")

    print("\n" + "-" * 72)
    print("这是**现象**，不是结论。哪些该修、怎么修，要结合业务判断——")
    print("拿这份扫描去问用户，再用 plan.py 生成清洗计划。")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("结构化结果：%s" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
