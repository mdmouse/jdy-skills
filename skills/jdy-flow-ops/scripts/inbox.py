#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待办收件箱：把某人的待办按表单/发起人分组呈现。只读。"""
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
TRIGGERS = ("我的待办", "有哪些待审批", "待办有多少")
from flow import (FlowError, content_text, default_username, humanize, iter_tasks,
                  resolve_username, stuck_hours, task_content)
from jdy_client import cli_main, JdyClient, JdyError


GROUP_KEYS = {
    "form": lambda t: t.get("form_title") or "(未知表单)",
    "creator": lambda t: (t.get("creator") or {}).get("name") or "(未知发起人)",
    "node": lambda t: t.get("flow_name") or "(未知节点)",
}


def group_tasks(tasks, group_by="form"):
    """按维度分组，返回 [(组名, 组内待办)]。**纯函数，不碰网络**——所以能被测。

    排序是有意的，不是顺手：**组按条数降序、组内按已等时长降序**，
    于是"最该先处理的"总在最上面。人打开收件箱是要挑一件事做，
    不是要读一份清单；顺序错了，等最久的那条就沉到底下了。
    """
    if group_by not in GROUP_KEYS:
        raise ValueError("不支持的分组维度：%r（可用：%s）"
                         % (group_by, "、".join(sorted(GROUP_KEYS))))
    key = GROUP_KEYS[group_by]
    buckets = defaultdict(list)
    for t in tasks:
        buckets[key(t)].append(t)
    return [(name, sorted(buckets[name], key=lambda t: -(t.get("_stuck_hours") or 0)))
            for name in sorted(buckets, key=lambda k: (-len(buckets[k]), k))]


def main():
    ap = argparse.ArgumentParser(description="查看待办（只读）")
    ap.add_argument("--user", help="成员编号或姓名；缺省读 ~/.jdy/config.json 的 username")
    ap.add_argument("--group-by", choices=("form", "creator", "node"), default="form",
                    help="分组方式，默认按表单")
    ap.add_argument("--json-out", help="另存结构化结果，供批量操作使用")
    ap.add_argument("--refresh-users", action="store_true", help="重建姓名→成员编号索引")
    ap.add_argument("--no-detail", action="store_true",
                    help="不拉取每条待办的业务内容（快，但只剩元数据，无法按内容区分）")
    ap.add_argument("--contains", help="只看内容包含该关键词的待办")
    args = ap.parse_args()

    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2
    try:
        username = resolve_username(client, args.user or default_username(client),
                                    refresh=args.refresh_users)
    except FlowError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    try:
        tasks = list(iter_tasks(client, username))
    except JdyError as exc:
        sys.stderr.write("拉取待办失败：%s\n" % exc)
        return 2

    now = datetime.datetime.now(datetime.timezone.utc)
    for t in tasks:
        t["_stuck_hours"] = stuck_hours(t, now)
        # 默认拉内容：待办列表本身不含任何业务字段，不拉就没法按内容区分
        t["_content"] = {} if args.no_detail else task_content(client, t)
    if args.contains:
        tasks = [t for t in tasks if args.contains in content_text(t["_content"])]

    print("=" * 68)
    print("待办收件箱　%s" % username)
    print("共 %d 条" % len(tasks))
    print("=" * 68)
    if not tasks:
        print("\n没有%s待办。" % ("符合「%s」的" % args.contains if args.contains else ""))
        return 0

    for name, items in group_tasks(tasks, args.group_by):
        print("\n▌%s（%d 条）" % (name, len(items)))
        for t in items:
            creator = (t.get("creator") or {}).get("name", "?")
            print("   · %s 节点「%s」 发起人 %s　已等 %s"
                  % (t.get("form_title", "")[:14], t.get("flow_name", ""), creator,
                     humanize(t["_stuck_hours"])))
            if t["_content"]:
                print("     %s" % content_text(t["_content"])[:110])
            print("     task_id=%s" % t["task_id"])

    oldest = max(tasks, key=lambda t: t["_stuck_hours"] or 0)
    print("\n" + "-" * 68)
    print("等最久的：「%s」的「%s」节点，已等 %s"
          % (oldest.get("form_title"), oldest.get("flow_name"),
             humanize(oldest["_stuck_hours"])))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"username": username, "count": len(tasks), "tasks": tasks},
                      fh, ensure_ascii=False, indent=2)
        print("结构化结果：%s（可交给 act.py 做批量处理）" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
