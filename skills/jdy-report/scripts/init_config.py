#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从应用的真实结构生成报表定义骨架。只读。

不是给个空模板让用户自己猜字段名——而是读出每张表实际有哪些
日期字段（能当周期）、分类字段（能当维度）、数字字段（能求和），
把可用选项直接写进注释里。
"""
import argparse
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("生成简道云周报", "做个月报", "出个日报")
from jdy_client import resolve_app, resolve_entry, cli_main, JdyClient, JdyError, display_value
from miniyaml import yaml_quote

DIM_TYPES = ("combo", "radiogroup", "user", "dept", "text", "company")
NUM_TYPES = ("number",)
DATE_TYPES = ("datetime",)
# 维度候选里排除这些——它们几乎每行都不同，分组出来全是 1
BAD_DIM_HINT = ("编号", "编码", "名称", "标题", "备注", "说明", "内容", "地址",
                "电话", "手机", "邮箱", "链接", "网址")


def classify(widgets):
    flat = list(widgets)
    for w in widgets:
        if w.get("type") == "subform":
            flat.extend(w.get("items", []))
    dates = [w for w in flat if w.get("type") in DATE_TYPES]
    nums = [w for w in flat if w.get("type") in NUM_TYPES]
    dims = [w for w in flat
            if w.get("type") in DIM_TYPES
            and not any(h in str(w.get("label", "")) for h in BAD_DIM_HINT)]
    return dates, nums, dims


def rank_dimensions(dims, rows):
    """用样本数据给维度候选打分，挑真正能拆出东西的那个。

    按名字顺序取第一个会挑到全空的孤儿字段，或每行都不同的准主键——
    两种都拆不出有意义的分组。这里要求：填充率够高、基数在可读区间内。
    """
    scored = []
    for w in dims:
        values = [display_value(r.get(w["name"]), w["type"]) for r in rows]
        filled = [v for v in values if v not in (None, "")]
        if not rows:
            continue
        fill = len(filled) / float(len(rows))
        distinct = len(set(filled))
        if fill < 0.5 or distinct < 2:
            continue                      # 基本全空、或只有一个取值，拆了没意义
        if distinct > max(20, len(rows) * 0.5):
            continue                      # 接近主键，拆出来全是 1
        # 基数 3~10 最好读；填充率越高越好
        ideal = 1.0 - min(abs(distinct - 6), 14) / 14.0
        scored.append((fill * 0.6 + ideal * 0.4, distinct, fill, w))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def main():
    ap = argparse.ArgumentParser(description="生成报表定义骨架（只读）")
    ap.add_argument("--app", required=True)
    ap.add_argument("--out", help="输出 YAML 路径，缺省打印")
    ap.add_argument("--max-sections", type=int, default=3,
                    help="最多生成几个板块，默认 3（挑数据最多的表）")
    args = ap.parse_args()

    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2
    try:
        if getattr(args, "app", None):
            args.app = resolve_app(client, args.app)
        if getattr(args, "entry", None) and getattr(args, "app", None):
            args.entry = resolve_entry(client, args.app, args.entry)
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    apps = client.list_apps()
    app_name = next((a["name"] for a in apps if a["app_id"] == args.app), args.app)
    forms = client.list_forms(args.app)
    if not forms:
        sys.stderr.write("该应用下没有表单\n")
        return 2

    # 按数据量排序——空表做不出有意义的报表。样本顺便留着给维度打分
    scored = []
    for f in forms:
        try:
            sample = client.fetch_all(args.app, f["entry_id"], limit=200, page_size=100)
        except JdyError:
            sample = []
        scored.append((len(sample), f, sample))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [f for n, f, _ in scored[:args.max_sections] if n > 0]
    if not picked:
        sys.stderr.write("该应用所有表单都没有数据，先灌点数据再生成报表\n")
        return 2

    lines = ["# 由 init_config.py 从「%s」的真实结构生成，字段名已核对过" % app_name,
             "name: %s 周报" % app_name,
             "app: %s" % args.app,
             "",
             "period:",
             "  field: 创建时间        # 可换成下面各板块列出的日期字段",
             "  range: last_7_days   # last_7_days｜last_30_days｜this_week｜last_month｜custom",
             "  compare: previous",
             "",
             "sections:"]

    for n, f, sample in [(n, f, sp) for n, f, sp in scored[:args.max_sections] if n > 0]:
        dates, nums, dims = classify(client.widgets(args.app, f["entry_id"]))
        ranked = rank_dimensions(dims, sample)
        # 表单名与字段名都是用户在界面里随手起的，带 `:`「，」`#` 的很常见。
        # 不加引号写出去，这份草稿自己就解析不回来——而它看着一切正常。
        lines.append("  - title: %s" % yaml_quote(f["name"]))
        lines.append('    entry: "%s"          # 约 %d 行数据' % (f["entry_id"], n))
        lines.append("    metrics:")
        lines.append("      - {label: 记录数, agg: count}")
        for num in nums[:2]:
            lines.append("      - {label: %s, agg: sum, field: %s}"
                         % (yaml_quote(num["label"]), yaml_quote(num["label"])))
        if ranked:
            best = ranked[0]
            lines.append("    dimensions: [%s]   # %d 个取值，填充率 %.0f%%"
                         % (yaml_quote(best[3]["label"]), best[1], best[2] * 100))
            lines.append("    top: 5")
        else:
            lines.append("    # 没有合适的维度：分类字段要么基本为空，要么每行都不同")
        lines.append("    # 可用日期字段：%s"
                     % ("、".join(d["label"] for d in dates) if dates else "无（只能用创建时间）"))
        if ranked:
            lines.append("    # 其他可选维度：%s"
                         % "、".join("%s(%d值/%.0f%%)" % (r[3]["label"], r[1], r[2] * 100)
                                     for r in ranked[1:6]) or "无")
        lines.append("    # 可求和字段  ：%s"
                     % ("、".join(x["label"] for x in nums[:8]) if nums else "无"))
        lines.append("")

    text = "\n".join(lines)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("报表定义骨架已生成：%s（%d 个板块）" % (args.out, len(picked)))
        print("请打开核对：周期字段选得对不对、维度是不是想要的那个。")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
