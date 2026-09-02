#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按计划改通讯录：建部门、改部门、调成员归属、加成员。默认 dry-run。

**这是本仓库里唯一一个会花钱的写入。** 官方明示：新建成员自动激活，
**占用一个用户数**。数据写错了能改回来，多占的坐席不能——所以新增成员
在闸门里是单独一行、单独一句话，不许混在"共 N 项改动"里蒙混过去。

**不接任何删除接口。** 官方有删除成员/批量删除/删除部门，这里一个都不连：
删错一个部门，它下面的人和权限一起没了。要删请到界面上删。

四道闸，缺一不可：
    1. JDY_ORG_WRITE=1        —— 通讯录专用开关（写入白名单管不到这里）
    2. 新增成员单独复述        —— 那是计费后果
    3. 规模闸门 + 确认码
    4. 非交互环境直接拒绝
写前自动备份整棵通讯录，写后逐项回读核对。
"""
import argparse
import datetime
import json
import os
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
TRIGGERS = ("建部门", "调整部门", "把某人调到某部门", "加成员", "改组织架构")
from jdy_client import (JdyClient, JdyError, ask_yes, cli_main, confirm_threshold,
                        plan_code, scale_gate)
from miniyaml import parse as parse_yaml
from org import (ROOT_DEPT, OrgError, OrgWriteRefused, check_org_write, classify, describe,
                 existing_dept_nos, existing_usernames, load_plan, snapshot)


def do_department(client, item, existing):
    if item.get("dept_no") and item["dept_no"] in existing:
        body = {"dept_no": item["dept_no"]}
        for key in ("name", "parent_no", "seq"):
            if item.get(key) is not None:
                body[key] = item[key]
        client.post("/v6/corp/department/update", body)
        return item["dept_no"]
    body = {"name": item["name"]}
    if item.get("parent_no") is not None:
        body["parent_no"] = item["parent_no"]
    if item.get("dept_no") is not None:
        body["dept_no"] = item["dept_no"]
    resp = client.post("/v6/corp/department/create", body)
    return (resp.get("department") or {}).get("dept_no") or resp.get("dept_no")


def do_member(client, item, existing):
    if item.get("username") and item["username"] in existing:
        body = {"username": item["username"]}
        for key in ("name", "departments"):
            if item.get(key) is not None:
                body[key] = item[key]
        client.post("/v5/corp/user/update", body)
        return item["username"]
    body = {"name": item["name"]}
    for key in ("username", "departments"):
        if item.get(key) is not None:
            body[key] = item[key]
    resp = client.post("/v5/corp/user/create", body)
    return (resp.get("user") or {}).get("username") or item.get("username")


def main():
    ap = argparse.ArgumentParser(description="按计划修改通讯录（默认 dry-run）")
    ap.add_argument("plan", help="改动计划（YAML 或 JSON）")
    ap.add_argument("--execute", action="store_true", help="真正写入")
    ap.add_argument("--yes", action="store_true", help="已向用户取得确认（非交互必须给）")
    ap.add_argument("--no-backup", action="store_true", help="跳过写前备份（不建议）")
    ap.add_argument("--confirm-code", help="大批量改动的确认码，见提示")
    ap.add_argument("--confirm-threshold", type=int, default=None,
                    help="内部安全默认值：改动超过多少项要二次确认（默认 50，只能往小调）")
    args = ap.parse_args()

    try:
        plan = load_plan(args.plan, parse_yaml)
    except (OrgError, OSError) as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    client = JdyClient()
    current = snapshot(client)
    buckets = classify(plan, current)
    total = sum(len(v) for v in buckets.values())
    new_members = buckets["member_create"]

    print("=" * 70)
    print("通讯录改动%s" % ("" if args.execute else "（DRY-RUN，未写入）"))
    print("=" * 70)
    print("当前：%d 个部门（不含根）　%d 名成员"
          % (len(current["departments"]), len(current["members"])))
    for line in describe(buckets):
        print("  " + line)
    if not total:
        print("\n计划里没有要做的改动。")
        return 0

    if new_members:
        # 单独一行、单独一句。混在"共 N 项"里报，人是看不见这笔账的。
        print("\n" + "!" * 70)
        print("⚠️ 本次要**新增 %d 名成员**。官方明示：新建成员自动激活，"
              "**每人占用一个用户数**。" % len(new_members))
        print("   数据写错了能改回来，多占的坐席不能。请**逐个**跟用户确认："
              "%s" % "、".join(m.get("name", "?") for m in new_members[:8]))
        print("!" * 70)

    # departments 是**整体替换**不是追加。只写 [5] 会把这个人从原有的其它部门里
    # 摘出来——那是"看不见的删除"：计划上写着"调到某部门"，实际还悄悄退了几个部门。
    moves = []
    now_by_user = {m.get("username"): (m.get("departments") or [])
                   for m in current["members"]}
    for m in buckets["member_update"]:
        if m.get("departments") is None:
            continue
        was, will = set(now_by_user.get(m["username"]) or []), set(m["departments"])
        if was - will:
            moves.append((m["username"], sorted(was - will), sorted(will)))
    if moves:
        print("\n⚠️ `departments` 是**整体替换**，下面这些人会被移出原有部门：")
        for who, lost, will in moves[:8]:
            print("     %s：移出 %s，改为 %s" % (who, lost, will))
        print("   要保留原有归属，把完整的部门列表都写上（dump.py --members 能看到现状）。")

    print("\n本工具**不删除任何东西**——官方的删除成员/部门接口一个都没接。"
          "要删请到简道云界面上删。")

    code = plan_code({"buckets": {k: len(v) for k, v in buckets.items()},
                      "names": sorted(json.dumps(i, ensure_ascii=False, sort_keys=True)
                                      for v in buckets.values() for i in v)})
    threshold = confirm_threshold(args.confirm_threshold)
    if not args.execute:
        print("\n以上均未写入。确认后执行：")
        print("    export JDY_ORG_WRITE=1        # 通讯录专用开关")
        print("    apply.py %s --execute --yes%s"
              % (args.plan, "" if total <= threshold else " --confirm-code %s" % code))
        return 0

    try:
        check_org_write()
    except OrgWriteRefused as exc:
        sys.stderr.write("%s\n" % exc)
        return 4

    gated = scale_gate(total, code, args.confirm_code, threshold,
                       describe(buckets), what="改动通讯录")
    if gated is not None:
        return gated

    if not args.yes:
        answered = ask_yes("确认改动通讯录 %d 项？输入 yes：" % total)
        if answered is None:            # 不是 tty，或 Windows 的 NUL 让 input() EOF
            sys.stderr.write(
                "拒绝写入：当前是非交互环境，无法当面向用户确认。\n"
                "通讯录动的是整个企业的组织架构%s——请先把上面的改动复述给用户、\n"
                "取得明确同意后，再加 --yes 重新执行。\n"
                % ("，而且本次会新增成员、占用用户数" if new_members else ""))
            return 4
        if not answered:
            print("已取消")
            return 0

    if not args.no_backup:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = os.path.join(os.path.dirname(os.path.abspath(args.plan)),
                            "backup_org_%s.json" % stamp)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(current, fh, ensure_ascii=False, indent=2)
            print("写前备份：%s" % path)
        except OSError as exc:
            sys.stderr.write("备份失败，已中止——没有备份不动组织架构。%s\n" % exc)
            return 3

    # 和 classify 用**同一个**判断——两边各写各的时候，执行这半漏了根部门，
    # 于是"改根部门名"显示成「修改部门 1 项」、发出去的却是新建请求。
    have_depts = existing_dept_nos(current)
    have_users = existing_usernames(current)
    done, failed = [], []
    for key, fn, existing in (("dept_create", do_department, have_depts),
                              ("dept_update", do_department, have_depts),
                              ("member_update", do_member, have_users),
                              ("member_create", do_member, have_users)):
        for item in buckets[key]:
            try:
                done.append((key, fn(client, item, existing)))
            except JdyError as exc:
                failed.append((key, json.dumps(item, ensure_ascii=False)[:50], str(exc)))

    after = snapshot(client)
    # 逐项回读核对。**只比总数是验不出修改的**——改个部门名、调个人的归属，
    # 部门数成员数一个都不变，接口回个成功就算数了。本仓库别处都做回读，
    # 这里原来只在 docstring 里写着做了。
    unverified = []
    after_depts = {d.get("dept_no"): d for d in after["departments"]}
    after_users = {m.get("username"): m for m in after["members"]}
    for key, ident in done:
        if ident is None:
            unverified.append((key, "?", "接口没回目标编号，无从回读"))
            continue
        item = next((i for i in buckets[key]
                     if (i.get("dept_no") == ident or i.get("username") == ident
                         or i.get("name") == ident)), None)
        got = after_depts.get(ident) if key.startswith("dept") else after_users.get(ident)
        if got is None:
            if not (key == "dept_update" and ident == ROOT_DEPT):   # 根部门不在返回里
                unverified.append((key, ident, "写完之后回读不到这一项"))
            continue
        for field in ("name", "parent_no", "departments"):
            want = (item or {}).get(field)
            if want is None:
                continue
            have = got.get(field)
            if isinstance(want, list) or isinstance(have, list):
                same = sorted(want or []) == sorted(have or [])
            else:
                same = str(want) == str(have)
            if not same:
                unverified.append((key, ident, "%s 提交 %r，回读是 %r" % (field, want, have)))

    print("\n" + "=" * 70)
    print("改动完成：成功 %d 项，失败 %d 项" % (len(done), len(failed)))
    print("部门 %d → %d　成员 %d → %d"
          % (len(current["departments"]), len(after["departments"]),
             len(current["members"]), len(after["members"])))
    for key, item, why in failed[:5]:
        print("   ❌ %s %s：%s" % (key, item, why))
    if unverified:
        print("\n⚠️ 回读核对对不上 %d 处（接口回了成功，实际不是这样）：" % len(unverified))
        for key, ident, why in unverified[:8]:
            print("     %s %s：%s" % (key, ident, why))
    else:
        print("✅ 逐项回读核对通过")
    grew = len(after["members"]) - len(current["members"])
    if grew > 0:
        print("\n⚠️ 成员数增加了 %d —— 对应 %d 个用户数被占用。" % (grew, grew))
    return 0 if not failed and not unverified else 3


if __name__ == "__main__":
    sys.exit(cli_main(main))
