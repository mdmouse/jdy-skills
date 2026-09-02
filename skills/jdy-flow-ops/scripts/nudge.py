#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""催办：把卡住的待办按人汇总，推到群里。默认只预览，不发送。

为什么值得做：简道云**没有催办 API**，界面上的催办也只能一条一条点。
而 backlog.py 已经算得出"卡在谁手上、卡了多久"——差的只是把它说成一句
人看得懂的话，再发出去。

**为什么按人汇总而不是按条列**：一条一条列出来，群里刷屏且没人对号入座；
按人汇总，每个人一眼看到"我有 3 条，最久的等了 5 天"，这才叫催办。

推送是对外动作、发出去收不回，所以和 report 的 push 同一条规矩：
默认预览、显式 --send、非交互环境不许替用户点头。
"""
import argparse
import os
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
TRIGGERS = ("催办", "催一下", "提醒审批", "谁卡着没处理")
from backlog import analyze, scan
from flow import humanize
from jdy_client import (JdyClient, JdyError, ask_yes, cli_main, col_width,
                        describe_targets, pad, print_targets, resolve_app,
                        resolve_entry)
from webhook import (FLAVORS, build_payload, check, detect, looks_ok, mask, post,
                     preview_text)


def by_assignee(pending):
    """把卡住的待办按**待办人**归拢，返回 [(姓名, [条目])]，按积压条数降序。

    组内按已等时长降序：催办要先说最久的那条，人对"等了 5 天"有反应，
    对"有 3 条待办"没有。
    """
    buckets = {}
    for hours, task, inst in pending:
        who = (task.get("assignee") or {}).get("name") or "(未指派)"
        buckets.setdefault(who, []).append((hours, task, inst))
    for items in buckets.values():
        items.sort(key=lambda x: -(x[0] or 0))
    return sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def render(groups, threshold, form_name, limit_per_person=3):
    """催办消息。群机器人**不支持表格**，所以本来就写成一行一条。"""
    total = sum(len(v) for v in dict(groups).values())
    out = ["**审批催办：%s**" % form_name,
           "共 %d 条超过 %g 小时没处理，涉及 %d 人。" % (total, threshold, len(groups)),
           ""]
    for who, items in groups:
        worst = items[0][0]
        out.append("**%s**（%d 条，最久 %s）" % (who, len(items), humanize(worst)))
        for hours, task, inst in items[:limit_per_person]:
            out.append("· %s　节点「%s」　已等 %s"
                       % (inst.get("form_title") or "", task.get("flow_name") or "",
                          humanize(hours)))
        if len(items) > limit_per_person:
            out.append("· …另有 %d 条" % (len(items) - limit_per_person))
        out.append("")
    out.append("请到简道云处理。（本条由 jdy-flow-ops 汇总，不含业务内容）")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="把卡住的待办按人汇总并催办（默认只预览）")
    ap.add_argument("--app", help="应用名或 ID；不确定就先 --list")
    ap.add_argument("--entry", help="表单名或 ID")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="列出应用；配合 --app 则列出该应用的表单")
    ap.add_argument("--limit", type=int, default=200, help="最多扫多少条数据，默认 200")
    ap.add_argument("--threshold", type=float, default=24.0,
                    help="停留超过多少小时才算要催，默认 24")
    ap.add_argument("--webhook", help="群机器人 URL；缺省读环境变量 JDY_REPORT_WEBHOOK")
    ap.add_argument("--platform", choices=sorted(set(FLAVORS.values())),
                    help="强制指定消息体格式。默认按 URL 主机名判断")
    ap.add_argument("--title", default="审批催办", help="钉钉需要标题")
    ap.add_argument("--send", action="store_true", help="真的发送。不加只打印将要发的内容")
    ap.add_argument("--yes", action="store_true",
                    help="已向用户取得发送同意（非交互环境配合 --send 时必须给）")
    ap.add_argument("--check", action="store_true",
                    help="只验 webhook 通不通，**不发任何消息**")
    args = ap.parse_args()

    client = JdyClient()
    if args.do_list or not (args.app and args.entry):
        app_id = resolve_app(client, args.app) if args.app else None
        print_targets(describe_targets(client, app_id),
                      "应用：" if not app_id else "该应用下的表单：")
        print("\n用法：nudge.py --app <应用> --entry <表单> [--threshold 24]")
        return 0

    url = args.webhook or os.environ.get("JDY_REPORT_WEBHOOK")
    if args.check:
        if not url:
            sys.stderr.write("缺少 webhook：用 --webhook 或设环境变量 JDY_REPORT_WEBHOOK\n")
            return 2
        code, err = check(url)
        if err:
            sys.stderr.write("连不上 %s：%s\n" % (mask(url), err))
            return 2
        print("可达：%s（HTTP %s）—— 未发送任何消息" % (mask(url), code))
        return 0

    args.app = resolve_app(client, args.app)
    args.entry = resolve_entry(client, args.app, args.entry)
    form_name = next((f["name"] for f in client.list_forms(args.app)
                      if f["entry_id"] == args.entry), args.entry)

    try:
        instances, no_flow = scan(client, args.app, args.entry, args.limit)
    except JdyError as exc:
        sys.stderr.write("扫描失败：%s\n" % exc)
        return 2
    if not instances:
        print("没有流程实例。若这张表刚配好流程，已有数据不会补建实例——"
              "只有新提交的记录才会走流程。")
        return 0

    result = analyze(instances, threshold=args.threshold)
    groups = by_assignee(result["over"])
    print("=" * 70)
    print("催办对象：%s（扫了 %d 个实例，%d 条没有流程实例）"
          % (form_name, len(instances), no_flow))
    print("=" * 70)
    if not groups:
        print("没有超过 %g 小时的待办——不用催。" % args.threshold)
        return 0

    w = col_width([who for who, _ in groups], 8)
    for who, items in groups:
        print("  %s %d 条　最久 %s" % (pad(who, w), len(items), humanize(items[0][0])))

    message = render(groups, args.threshold, form_name)
    if not url:
        print("\n（没配 webhook，只在这里汇总。要推到群里：--webhook <URL> 或设 "
              "JDY_REPORT_WEBHOOK）")
        print("-" * 70)
        print(message)
        return 0

    flavor = args.platform or detect(url)
    if not flavor:
        sys.stderr.write("认不出这个 webhook 属于哪家（支持企业微信/飞书/钉钉）：%s\n"
                         % mask(url))
        return 2
    payload = build_payload(flavor, args.title, message)

    if not args.send:
        print("\n" + "=" * 70)
        print("预览模式 —— 未发送任何消息")
        print("=" * 70)
        print("目标   ：%s（%s）" % (mask(url), flavor))
        print("-" * 70)
        print(preview_text(payload))
        print("-" * 70)
        print("\n把上面这条给用户确认后，加 --send --yes 真正发送。")
        print("⚠️ 催办是**发给一群人**的消息，比报表更需要先看清楚再发。")
        return 0

    if not args.yes:
        # 原来这里只写 `not args.yes and not sys.stdin.isatty()` 就放行了——
        # isatty() 一旦谎报 True（Windows 的 NUL 设备就是），这道闸门等于不存在，
        # 消息**直接发进群**、没人确认过。所以改成"没 --yes 就必须真问一次"：
        # ask_yes 返回 None（问不了）走拒绝，返回 False 走取消。
        answered = ask_yes("确认发送这条催办？输入 yes：")
        if answered is None:
            sys.stderr.write(
                "拒绝发送：当前是非交互环境，无法当面向用户确认。\n"
                "催办会 @ 到一群人且收不回——请先去掉 --send 跑一次预览、\n"
                "把内容给用户看、取得明确同意，再加 --yes 重新执行。\n")
            return 4
        if not answered:
            print("已取消")
            return 0

    status, body = post(url, payload)
    if status is None:
        sys.stderr.write("发送失败（网络）：%s\n" % body)
        return 3
    print("已发送到 %s（%s）→ HTTP %d" % (mask(url), flavor, status))
    print("响应：%s" % body[:300])
    if not looks_ok(status, body):
        sys.stderr.write("\n⚠️ 响应不像成功——群机器人常见失败是关键词不匹配、"
                         "IP 白名单、加签校验，请核对机器人配置。\n")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
