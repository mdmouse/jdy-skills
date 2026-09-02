#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出通讯录：部门树、成员归属、谁在哪个部门。全程只读。

除了"看一眼"，它还有两个正经用处：

  · **写通讯录之前的备份**——apply.py 会自己存一份，但你也该手上有一份；
  · **把姓名换成成员编号**。简道云的接口只认 `sys_xxx` 成员编号，
    而人嘴里说的永远是姓名。别的技能（流程、成员字段）此前只能拿业务数据
    反查姓名→编号，覆盖不全；这里是权威来源。
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
TRIGGERS = ("通讯录", "组织架构", "部门列表", "成员编号是多少", "谁在哪个部门",
            "导出通讯录")
from jdy_client import cli_main, col_width, pad, JdyClient
from org import ROOT_DEPT, fetch_managers, snapshot, tree_lines


def main():
    ap = argparse.ArgumentParser(description="导出通讯录（只读）")
    ap.add_argument("--json-out", help="另存结构化结果（也可当写前备份）")
    ap.add_argument("--members", action="store_true", help="逐个列出成员及其编号")
    ap.add_argument("--managers", action="store_true",
                    help="一并查部门主管（接口可能不可用，会如实说明）")
    args = ap.parse_args()

    client = JdyClient()
    snap = snapshot(client)
    depts, members = snap["departments"], snap["members"]

    print("=" * 70)
    print("通讯录　%d 个部门（不含根）　%d 名成员" % (len(depts), len(members)))
    print("=" * 70)
    for line in tree_lines(depts, members):
        print("  " + line)

    if args.managers:
        print("\n【部门主管】")
        got = False
        for no in [ROOT_DEPT] + [d.get("dept_no") for d in depts]:
            managers = fetch_managers(client, no)
            if managers is None:
                print("   主管接口不可用（本账号实测 403，多半是版本功能）——"
                      "**这不等于「没有主管」**，是没查到。")
                break
            got = True
            if managers:
                print("   部门 %s：%s" % (no, "、".join(managers)))
        if got:
            print("   （没列出的部门就是没设主管）")

    if args.members:
        print("\n【成员】接口只认成员编号，人说的是姓名——这张对照表是权威来源")
        w = col_width([m.get("name", "") for m in members], 8)
        for m in sorted(members, key=lambda x: x.get("name") or ""):
            print("   %s %s　部门 %s%s"
                  % (pad(m.get("name", "?"), w), m.get("username", "?"),
                     m.get("departments") or [],
                     "" if m.get("status") == 1 else "　（已停用）"))
        dupes = {}
        for m in members:
            dupes.setdefault(m.get("name"), []).append(m.get("username"))
        same = {k: v for k, v in dupes.items() if len(v) > 1}
        if same:
            print("\n   ⚠️ 有重名：%s"
                  % "；".join("%s → %s" % (k, "、".join(v)) for k, v in same.items()))
            print("   按姓名找人时**必须让用户挑**，不能替他选——挑错就是把别人的"
                  "待办批了、把别人写进负责人。")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=2)
        print("\n结构化结果：%s（改通讯录之前建议先存一份）" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
