#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性查数：筛选、分组、算指标，出一份自包含的 HTML 报告。只读。

和 jdy-report 的分工：那个是**周期性**报表（YAML 定义、Markdown、推群机器人）；
这个是**临时提问**——"华东区这个月有多少单""按行业分一下客户"——
问完给一张能直接发给人看的页面。

简道云没有仪表盘 API，查询结果想给人看只能自己画。图表是内联 SVG、
零第三方依赖，双击就能打开。
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from chart import bar_chart, page, table
from jdy_client import (AGGS, JdyClient, UNFILLED, aggregate_rows, build_filter,
                        cli_main, col_width, describe_targets, display_value,
                        group_rows, pad, print_targets, resolve_app, resolve_entry)

TRIGGERS = ("查一下", "有多少条", "按什么分组看看", "出个临时报表", "查数",
            "筛选看看", "做张图", "生成 HTML 报告", "分布情况")


def _resolver(by_label):
    """按显示名取该行的值。分组与算指标共用同一条取值路径——
    两条路径就是两种口径，迟早在某个控件类型上分叉。"""
    def resolve(row, label):
        w = by_label[label]
        value = display_value(row.get(w["name"]), w["type"])
        return None if value in (None, "", [], {}) else value
    return resolve


def aggregate(rows, by_label, group_label, metric, metric_label):
    """返回 [(分组值, 数值)]，按数值降序。空值归入「(未填)」而不是丢掉。

    分组与算指标都走内核（group_rows / aggregate_rows），本技能只决定
    **怎么排**——按数值降序是临时查数的展示需要（"哪个最多"），
    而周期报表按维度值排。排序意图属于展示层，不该固化进引擎。
    """
    if not group_label:
        return [("全部", aggregate_rows(rows, metric, metric_label, _resolver(by_label)))]
    resolve = _resolver(by_label)
    out = [(key[0], aggregate_rows(group, metric, metric_label, resolve))
           for key, group in group_rows(rows, [group_label], resolve)]
    return sorted(out, key=lambda kv: -kv[1])


def main():
    ap = argparse.ArgumentParser(description="临时查数与 HTML 报告（只读）")
    ap.add_argument("--app", help="应用名或 ID；不确定就先 --list")
    ap.add_argument("--entry", help="表单名或 ID")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="列出应用；配合 --app 则列出该应用的表单")
    ap.add_argument("--where", help="筛选：'字段=值' 或 '字段:method:值'，多条用 ; 连")
    ap.add_argument("--group-by", help="按这一列分组")
    ap.add_argument("--metric", default="count",
                    help="count（默认）/ sum:列 / avg:列 / max:列 / min:列 / "
                         "distinct:列（该列有多少个不同的值）")
    ap.add_argument("--top", type=int, help="只看前 N 组")
    ap.add_argument("--limit", type=int, help="最多拉多少行")
    ap.add_argument("--out", help="输出 HTML 报告路径")
    ap.add_argument("--json-out", help="同时另存结构化结果")
    args = ap.parse_args()

    client = JdyClient()
    if args.do_list or not (args.app and args.entry):
        aid = resolve_app(client, args.app) if args.app else None
        print_targets(describe_targets(client, aid),
                      "应用：" if not aid else "该应用下的表单：")
        print("\n用法：query.py --app <应用> --entry <表单> "
              "[--where 条件] [--group-by 列] [--out 报告.html]")
        return 0
    args.app = resolve_app(client, args.app)
    args.entry = resolve_entry(client, args.app, args.entry)
    form_name = next((f["name"] for f in client.list_forms(args.app)
                      if f["entry_id"] == args.entry), args.entry)

    by_label, by_name = client.field_map(args.app, args.entry)
    data_filter = build_filter(args.where, by_label, by_name)

    metric, metric_label = args.metric, None
    if ":" in args.metric:
        metric, metric_label = args.metric.split(":", 1)
        if metric_label not in by_label:
            raise ValueError("指标字段「%s」在表单里不存在" % metric_label)
    # 可用指标以内核 AGGS 为准。原来这里另抄了一份，内核支持 distinct
    # 而这边照样拒绝——同一个引擎，两个技能能算的东西不一样。
    if metric not in AGGS:
        raise ValueError("不支持的指标：%s（可用：%s）" % (metric, "、".join(AGGS)))
    if metric != "count" and not metric_label:
        raise ValueError("%s 要指定字段，写成 --metric %s:列名" % (metric, metric))
    if args.group_by and args.group_by not in by_label:
        raise ValueError("分组字段「%s」在表单里不存在。可用：%s"
                         % (args.group_by, "、".join(list(by_label)[:12])))

    rows = client.fetch_all(args.app, args.entry,
                            data_filter=data_filter, limit=args.limit)
    pairs = aggregate(rows, by_label, args.group_by, metric, metric_label)
    shown = pairs[:args.top] if args.top else pairs

    label = {"count": "记录数", "sum": "合计", "avg": "平均",
             "max": "最大", "min": "最小", "distinct": "去重计数"}[metric]
    if metric_label:
        label = "%s（%s）" % (label, metric_label)

    print("=" * 70)
    print("%s　命中 %d 行%s" % (form_name, len(rows),
                               "（已按条件筛选）" if data_filter else ""))
    print("=" * 70)
    w = col_width([k for k, _v in shown], 8)
    for k, v in shown:
        print("  %s %s" % (pad(k, w), ("%.2f" % v) if v != int(v) else int(v)))
    if args.top and len(pairs) > args.top:
        print("  …共 %d 组，只列了前 %d 组" % (len(pairs), args.top))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"form": form_name, "matched": len(rows),
                       "metric": label, "group_by": args.group_by,
                       "groups": [{"key": k, "value": v} for k, v in pairs]},
                      fh, ensure_ascii=False, indent=2)
        print("\n结构化结果：%s" % args.json_out)

    if args.out:
        blocks = []
        if args.group_by:
            blocks.append("<h2>按「%s」分组 · %s</h2>" % (args.group_by, label))
            if args.top and len(pairs) > args.top:
                # 图只画前 N 组，下面的表是全量——不说清楚，看图的人会
                # 以为一共就这几组。少给了就得讲。
                blocks.append('<p class="note">图中只画了前 %d 组，'
                              '共 %d 组；完整清单见下表。</p>'
                              % (args.top, len(pairs)))
            blocks.append(bar_chart(shown, title=label))
            blocks.append(table([args.group_by, label],
                                [[k, ("%.2f" % v) if v != int(v) else int(v)]
                                 for k, v in pairs]))
        else:
            blocks.append("<h2>%s</h2><p>%s</p>" % (label, shown[0][1]))
        sub = "命中 %d 行" % len(rows) + ("　条件：%s" % args.where if args.where else "")
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(page("%s · 临时查询" % form_name, blocks, subtitle=sub))
        print("HTML 报告：%s（自包含，双击可看）" % args.out)
    elif not args.json_out:
        print("\n加 --out 报告.html 生成带图表的页面。")
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
