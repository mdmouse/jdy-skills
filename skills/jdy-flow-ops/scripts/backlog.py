#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""积压扫描：哪个节点卡住了、卡了谁、平均多久。只读。

思路：instance_id 等同 data_id，所以从表单数据就能枚举流程实例；
每个实例的 tasks[] 带着每个节点的 create_time / finish_time，
停留时长与瓶颈节点都能算出来。
"""
import argparse
import datetime
import json
import sys
from collections import defaultdict

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("审批积压", "流程卡在哪", "哪个节点最慢", "流程效率")
from flow import INSTANCE_STATUS, get_instance, humanize, stuck_hours
from jdy_client import (JdyClient, JdyError, cli_main, describe_targets, pad,
                        print_targets, resolve_app, resolve_entry)


def scan(client, app_id, entry_id, limit, progress=None):
    """返回 (实例列表, 无流程的行数)。"""
    instances, no_flow = [], 0
    rows = client.fetch_all(app_id, entry_id, limit=limit)
    for i, row in enumerate(rows, 1):
        try:
            inst = get_instance(client, row["_id"])
        except JdyError:
            no_flow += 1                          # 该行早于流程配置，或本就没挂流程
            continue
        if not inst.get("instance_id"):
            no_flow += 1
            continue
        instances.append(inst)
        if progress:
            progress(i, len(rows))
    return instances, no_flow


def analyze(instances, now=None, threshold=24.0):
    """把一堆实例算成结论。**纯函数，不碰网络**——所以能被测。

    原来这段和渲染焊在 main() 里，于是"瓶颈节点是谁""卡了几条"这些
    用户会据以行动的数字，一条测试都没有。

    返回 {by_status, pending, node_stats, bottleneck, over}：
      pending    当前卡住的待办，按已等时长降序，[(hours, task, instance)]
      node_stats 已完成节点的耗时统计，按平均降序，[(节点, 平均, 样本数, 最长)]
      bottleneck 平均耗时最长的节点名；没有已完成节点时为 None
      over       停留超过 threshold 的那部分 pending
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    by_status, node_wait, pending = defaultdict(int), defaultdict(list), []
    for inst in instances:
        by_status[INSTANCE_STATUS.get(inst.get("status"), inst.get("status"))] += 1
        for t in inst.get("tasks") or []:
            hours = stuck_hours(t, now)
            if t.get("status") == 0:
                pending.append((hours, t, inst))
            elif hours is not None:
                # 已完成节点才进耗时统计：把"还没办完"的算进平均，
                # 等得越久平均越难看，而那恰恰不是"这个节点办得慢"的证据
                node_wait[t.get("flow_name") or "?"].append(hours)

    pending.sort(key=lambda x: -(x[0] or 0))
    stats = sorted([(name, sum(v) / len(v), len(v), max(v))
                    for name, v in node_wait.items()], key=lambda x: -x[1])
    return {
        "by_status": dict(by_status),
        "pending": pending,
        "over": [p for p in pending if (p[0] or 0) >= threshold],
        "node_stats": stats,
        "bottleneck": stats[0][0] if stats else None,
    }


def main():
    ap = argparse.ArgumentParser(description="流程积压扫描（只读）")
    ap.add_argument("--app", help="应用名或 ID；不确定就先 --list")
    ap.add_argument("--entry", help="表单名或 ID")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="列出应用；配合 --app 则列出该应用的表单")
    ap.add_argument("--limit", type=int, default=200, help="最多扫多少条数据，默认 200")
    ap.add_argument("--threshold", type=float, default=24.0,
                    help="停留超过多少小时算积压，默认 24")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2

    if args.do_list or not (args.app and args.entry):
        app_id = resolve_app(client, args.app) if args.app else None
        print_targets(describe_targets(client, app_id),
                      "应用：" if not app_id else "该应用下的表单：")
        print("\n用法：backlog.py --app <应用名或ID> --entry <表单名或ID>")
        return 0
    args.app = resolve_app(client, args.app)
    args.entry = resolve_entry(client, args.app, args.entry)

    tty = sys.stderr.isatty()

    def progress(i, total):
        if tty:
            sys.stderr.write("\r扫描实例 %d/%d …" % (i, total))
            sys.stderr.flush()

    try:
        instances, no_flow = scan(client, args.app, args.entry, args.limit, progress)
    except JdyError as exc:
        sys.stderr.write("\n扫描失败：%s\n" % exc)
        return 2
    if tty:
        sys.stderr.write("\r" + " " * 40 + "\r")

    print("=" * 70)
    print("流程积压扫描")
    print("扫描 %d 条数据 → %d 个流程实例（%d 条没有流程实例）"
          % (min(args.limit, len(instances) + no_flow), len(instances), no_flow))
    print("=" * 70)
    if not instances:
        print("\n没有流程实例。若这张表刚配好流程，已有数据不会补建实例——"
              "只有新提交的记录才会走流程。")
        return 0

    result = analyze(instances, threshold=args.threshold)
    by_status, pending = result["by_status"], result["pending"]

    print("\n【实例状态】")
    for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print("   %s %d" % (pad(status, 8), n))

    print("\n【当前卡住的待办】共 %d 条" % len(pending))
    if pending:
        over = result["over"]
        for hours, t, inst in pending[:15]:
            flag = "🔴" if (hours or 0) >= args.threshold else "  "
            who = (t.get("assignee") or {}).get("name", "?")
            print("   %s 「%s」卡在「%s」　已等 %s"
                  % (flag, inst.get("form_title", ""), t.get("flow_name", ""),
                     humanize(hours)))
            print("      待办人 %s　发起人 %s　task_id=%s"
                  % (who, (inst.get("creator") or {}).get("name", "?"), t.get("task_id")))
        if len(pending) > 15:
            print("   … 另有 %d 条" % (len(pending) - 15))
        if over:
            print("\n   ⚠️ 超过 %g 小时的有 %d 条" % (args.threshold, len(over)))

    stats = result["node_stats"]
    if stats:
        print("\n【各节点平均耗时】（已完成的节点）")
        print("   %s %s %s %s" % (pad("节点", 18), pad("平均", 12), pad("最长", 12), "样本"))
        for name, avg, n, worst in stats:
            print("   %s %s %s %d" % (pad(name, 18), pad(humanize(avg), 12), pad(humanize(worst), 12), n))
        print("\n   瓶颈节点：**%s**（平均 %s）" % (result["bottleneck"], humanize(stats[0][1])))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"instances": len(instances), "no_flow": no_flow,
                       "pending": [{"hours": h, "task": t, "form": i.get("form_title")}
                                   for h, t, i in pending],
                       "node_avg_hours": {name: avg for name, avg, _n, _w in stats}},
                      fh, ensure_ascii=False, indent=2)
        print("\n结构化结果：%s" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
