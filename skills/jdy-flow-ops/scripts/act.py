#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量处理待办：同意 / 否决 / 回退 / 转交。

**默认 dry-run。** 审批是有责任归属的动作，发出去就改变了别人的流程状态，
所以：先列清楚要动哪些、拿到用户明确同意、`--execute` 才真做，
非交互环境下直接拒绝（Agent 平台里 stdin 不是 tty，问不了不等于可以替用户决定）。
每次操作都写审计日志。
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("批量审批", "批量同意", "批量否决", "退回", "转交待办")
from flow import (APPROVE, INSTANCE_STATUS, REJECT, ROLLBACK, TRANSFER, FlowError, audit,
                  content_text, default_username, get_instance, humanize, iter_tasks,
                  resolve_username, stuck_hours, task_content)
import platform_env
from jdy_client import (ask_yes, check_workflow_writable, cli_main, plan_code,
                        scale_gate, JdyClient, JdyError)

ACTIONS = {
    "approve": (APPROVE, "同意"),
    "reject": (REJECT, "否决"),
    "rollback": (ROLLBACK, "回退"),
    "transfer": (TRANSFER, "转交"),
}


def confirm_code(action, username, tasks):
    """确认码由**这一批具体的 task_id** 算出。

    为什么必须绑到 task_id 上：dry-run 与 --execute 是两次调用，
    各自实时重拉一次待办。两次之间新到的待办会被静默一起批掉——
    用户点头的是 3 条，执行的是 5 条，多出来的那 2 条他从没见过，
    而审批是不可自动撤销的。集合一变码就变，旧码立即失效。
    """
    return plan_code({"action": action, "user": username,
                      "tasks": sorted(str(t.get("task_id")) for t in tasks)})


def load_tasks(client, args, username):
    if args.tasks_json:
        with open(args.tasks_json, "r", encoding="utf-8") as fh:
            tasks = json.load(fh).get("tasks", [])
    else:
        tasks = list(iter_tasks(client, username))
    if args.form:
        tasks = [t for t in tasks if args.form in (t.get("form_title") or "")]
    if args.node:
        tasks = [t for t in tasks if args.node in (t.get("flow_name") or "")]
    if args.task_id:
        wanted = set(args.task_id)
        tasks = [t for t in tasks if t.get("task_id") in wanted]
    # 内容始终拉取：批量操作前必须让用户看清批的是什么，
    # 只报"3 条缺货申请"等于没说——三条长得一模一样
    for t in tasks:
        if "_content" not in t:
            t["_content"] = task_content(client, t)
    if args.contains:
        tasks = [t for t in tasks if args.contains in content_text(t["_content"])]
    return tasks


def main():
    ap = argparse.ArgumentParser(description="批量处理待办（默认 dry-run）")
    ap.add_argument("action", choices=sorted(ACTIONS))
    ap.add_argument("--user", help="成员编号或姓名；缺省读 ~/.jdy/config.json")
    ap.add_argument("--tasks-json", help="inbox.py --json-out 的产物；缺省实时拉取")
    ap.add_argument("--form", help="只处理表单名包含该字符串的待办")
    ap.add_argument("--node", help="只处理节点名包含该字符串的待办")
    ap.add_argument("--task-id", action="append", help="指定 task_id，可重复")
    ap.add_argument("--contains", help="只处理业务内容包含该关键词的待办（人通常按内容指代审批）")
    ap.add_argument("--comment", help="审批意见。是否必填取决于节点配置，"
                                      "要求而没给会报 5004")
    ap.add_argument("--transfer-to", help="转交给谁（成员编号或姓名）")
    ap.add_argument("--flow-id", type=int, help="回退目标节点（节点配置为回退到指定节点时用）")
    ap.add_argument("--back-type", type=int, choices=(1, 2),
                    help="回退人选择：1 正常流转，2 直达目标节点")
    ap.add_argument("--execute", action="store_true", help="真正执行；不加只做 dry-run")
    ap.add_argument("--yes", action="store_true", help="已向用户取得确认（非交互环境必须显式给）")
    ap.add_argument("--confirm-code", help="本批待办的确认码，dry-run 会给出")
    args = ap.parse_args()

    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2
    try:
        username = resolve_username(client, args.user or default_username(client))
        transfer_to = resolve_username(client, args.transfer_to) if args.transfer_to else None
    except FlowError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    if args.action == "transfer" and not transfer_to:
        sys.stderr.write("转交必须用 --transfer-to 指定接收人\n")
        return 2

    try:
        tasks = load_tasks(client, args, username)
    except (JdyError, OSError, ValueError) as exc:
        sys.stderr.write("读取待办失败：%s\n" % exc)
        return 2
    if not tasks:
        print("没有符合条件的待办。")
        return 0

    path, label = ACTIONS[args.action]
    print("=" * 70)
    print("%s　%d 条待办%s" % (label, len(tasks), "" if args.execute else "（DRY-RUN，未执行）"))
    print("=" * 70)
    for t in tasks:
        print("  · 「%s」节点「%s」 发起人 %s　已等 %s"
              % (t.get("form_title"), t.get("flow_name"),
                 (t.get("creator") or {}).get("name", "?"), humanize(stuck_hours(t))))
        if t.get("_content"):
            print("    %s" % content_text(t["_content"])[:110])
        print("    task_id=%s" % t.get("task_id"))
    print("\n操作者：%s" % username)
    if transfer_to:
        print("转交给：%s" % transfer_to)
    if args.comment:
        print("审批意见：%s" % args.comment)
    if args.action == "reject" and not args.comment:
        print("提示：部分节点要求填写审批意见，缺失会报 5004（本账号实测该节点不强制）")

    code = confirm_code(args.action, username, tasks)
    if not args.execute:
        # 确认码在 dry-run 阶段就给出。让人先跑一次 --execute 被拒才拿得到码，
        # 等于训练调用方在取得同意之前先试着写一次——安全机制不该逼人这么做。
        print("\n以上均未执行。把清单给用户确认后执行：")
        print("    --execute --yes --confirm-code %s" % code)
        print("（码由这一批 task_id 算出；待办清单一变即失效。不要向用户提这个码）")
        return 0

    # 确认集闸门：不分批量大小都要求确认码。dry-run 与执行是两次实时拉取，
    # 中间新到的待办原来会被静默一起批掉，而审批不可自动撤销。
    gated = scale_gate(len(tasks), code, args.confirm_code, 0,
                       ["「%s」%s" % (t.get("form_title"),
                                     content_text(t.get("_content") or {})[:40])
                        for t in tasks[:8]], what=label)
    if gated is not None:
        if args.confirm_code:
            print("（清单与你确认时不一致：可能有新待办到达，或其中几条已被别人处理。"
                  "请重新跑一次 dry-run，把新清单交给用户再确认。）")
        return gated

    # 写入白名单：流程写接口的 body 里没有 app_id/entry_id，post() 那道统一关卡
    # 对它是瞎的，只能在这里按待办自带的 app_id/form_id 逐条查。
    check_workflow_writable(tasks)

    if not args.yes:
        answered = ask_yes("确认%s这 %d 条？输入 yes：" % (label, len(tasks)))
        if answered is None:            # 不是 tty，或 Windows 的 NUL 让 input() EOF
            sys.stderr.write(
                "拒绝执行：当前是非交互环境，无法当面向用户确认。\n"
                "审批会改变他人的流程状态且不可自动撤销——请先把上面的清单复述给用户、\n"
                "取得明确同意后，再加 --yes 重新执行。\n")
            return 4
        if not answered:
            print("已取消")
            return 0

    ok, failed, succeeded, audit_path = 0, [], [], None
    for t in tasks:
        body = {"username": username, "instance_id": t["instance_id"], "task_id": t["task_id"]}
        if args.comment:
            body["comment"] = args.comment
        if args.action == "transfer":
            body["transfer_username"] = transfer_to
        if args.action == "rollback":
            if args.flow_id is not None:
                body["flow_id"] = args.flow_id
            if args.back_type is not None:
                body["back_type"] = args.back_type
        try:
            client.post(path, body)
        except JdyError as exc:
            failed.append((t, str(exc)))
            audit_path = audit(args.action, username, t, "failure: %s" % exc.msg, args.comment)
            continue
        ok += 1
        succeeded.append(t)
        audit_path = audit(args.action, username, t, "success", args.comment)

    print("\n%s：接口回报成功 %d 条，失败 %d 条" % (label, ok, len(failed)))
    for t, err in failed:
        print("   ❌ %s（%s）：%s" % (t.get("form_title"), t.get("task_id"), err))

    # 写后核对：接口说成功不等于状态真的变了。
    # 简道云在别处已经多次出现"回报成功但什么都没发生"（batch_create 静默丢字段、
    # 流程写接口 HTTP200 包着 failure），流程操作没理由例外。
    if succeeded:
        print("\n【写后核对】重新读取实例，确认状态真的变了")
        moved, stuck = [], []
        for t in succeeded:
            try:
                inst = get_instance(client, t["instance_id"])
            except JdyError as exc:
                stuck.append((t, "核对失败：%s" % exc.msg))
                continue
            node = next((x for x in inst.get("tasks") or []
                         if x.get("task_id") == t["task_id"]), None)
            if node is None:
                stuck.append((t, "核对时找不到该节点"))
            elif node.get("status") == 0:
                stuck.append((t, "节点仍为待处理——接口回报成功但状态未变"))
            else:
                moved.append((t, inst, node))
        for t, inst, node in moved:
            print("   ✅ %s → 实例%s，节点动作 %s"
                  % (content_text(t.get("_content", {}))[:40] or t["task_id"],
                     INSTANCE_STATUS.get(inst.get("status"), inst.get("status")),
                     node.get("finish_action")))
        for t, why in stuck:
            print("   ⚠️ %s → %s" % (t.get("task_id"), why))
        if stuck:
            print("\n   有 %d 条接口回报成功但核对不通过——**不要告诉用户已处理**，"
                  "请如实说明并让其在简道云界面确认。" % len(stuck))
        else:
            print("   全部核对通过：%d 条状态确已变更。" % len(moved))
    if audit_path:
        print("审计日志：%s" % audit_path)
        note = platform_env.resolve_state_home().note()
        if note:
            print(note)                 # 落点不是默认位置时说清楚，别让人去 ~/.jdy 找
    else:
        print("⚠️ 审计日志写入失败（所有候选目录都不可写），操作已执行但没有留痕。"
              "设 JDY_HOME 指向一个沙箱允许写的目录可解决。")
    return 0 if not failed else 3


if __name__ == "__main__":
    sys.exit(cli_main(main))
