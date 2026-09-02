#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拿写前备份把记录改回去。默认 dry-run。

为什么要有它：清洗、同步、导入三条链路都会在写之前落一份整表备份，
但**没有任何东西读它**——备份成了一种仪式。真出事的时候，手上有一个
几千行的 JSON 和一个"自己想办法"，这不叫有退路。

能做什么、不能做什么，说清楚：

  · 能：把**还存在**的记录，字段值改回备份时的样子（逐条 update）。
  · 不能：把已经被删掉的记录**变回来**。简道云不允许指定 data_id 新建，
    新建出来的是另一条记录、另一个 ID，所有指向它的关联都还是断的。
    这类记录本工具只列出来，让人自己在界面上决定怎么办。
  · 不碰：备份之后**新增**的记录。它们不在备份里，恢复不该顺手删掉它们
    ——本项目从不删数据。

  · 不改：读回来的值不是"能写回去的形状"的列（成员/地址/部门/附件/子表单…）。
    这些列在 **dry-run 就会列出来**，而不是等执行时才在报错里冒出来。

回写用的是备份里的**原始值**，不是 display_value 的产物——那两者对结构化控件
是不同的东西。走的是和清洗完全相同的编码与回读核对，所以「哪些字段没写回去」
同样会被如实报出来。
"""
import argparse
import os
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
TRIGGERS = ("恢复备份", "改回去", "回滚", "撤销清洗")
from jdy_client import (JdyClient, JdyError, ask_yes, backup_path, cli_main, col_width,
                        confirm_threshold, display_value, load_backup, pad, plan_code,
                        report_skipped, scale_gate, writable_back)


def restorable(client, app_id, entry_id, rows):
    """把备份行分成「还在、且值有出入」「还在但没变」「已不存在」三堆。

    返回 (changed, same, gone, unrestorable)。

    只回写**真的不一样**的字段：全量回写会把没被动过的记录也重写一遍，
    白白扩大写入面，出问题时也分不清是谁改的。

    两件必须分清的事：

    · **比对**用 `display_value`——人能读，也能稳定判断"变没变"；
    · **回写**用**原始值**。这两者对结构化控件是不同的东西：成员回写只认
      username，而 display_value 给的是"张三"；地址回写要对象，
      display_value 给的是拼好的串。

    本文件第一版拿 display_value 的结果当回写源，后果是：成员/地址在编码期
    被拒（而 dry-run 一声不吭），部门更糟——提交了裸串"研发部"，
    dept 要的是 dept_no，接口照样回报成功、存进去是空的。
    同一批改动里 sync_value() 和清洗的 NORMALIZABLE 都为这件事做过防护，
    唯独新写的这里没有。判断现在统一在内核 writable_back()。
    """
    by_label, by_name = client.field_map(app_id, entry_id)
    current = client.fetch_rows_by_id(app_id, entry_id, [r.get("_id") for r in rows])
    changed, same, gone, unrestorable = [], [], [], []
    for name, widget in by_name.items():
        ok, why = writable_back(widget)
        if not ok:
            unrestorable.append((widget["label"], widget["type"], why))
    skip = {label for label, _t, _w in unrestorable}

    for row in rows:
        data_id = row.get("_id")
        if not data_id:
            continue
        now = current.get(data_id)
        if now is None:
            gone.append(row)
            continue
        diff = {}
        for name, widget in by_name.items():
            if name not in row or widget["label"] in skip:
                continue
            was = display_value(row.get(name), widget["type"])
            became = display_value(now.get(name), widget["type"])
            if str(was) != str(became):
                diff[widget["label"]] = {
                    "from": became, "to": was,        # 给人看的
                    "value": row.get(name),           # 真正回写的：原始值
                }
        if diff:
            changed.append({"data_id": data_id, "diff": diff})
        else:
            same.append(data_id)
    return changed, same, gone, unrestorable


def main():
    ap = argparse.ArgumentParser(description="用写前备份把记录改回去（默认 dry-run）")
    ap.add_argument("backup", help="备份文件（backup_<entry_id>_<时间>.json）")
    ap.add_argument("--execute", action="store_true", help="真正写入")
    ap.add_argument("--yes", action="store_true", help="已向用户取得确认（非交互必须给）")
    ap.add_argument("--confirm-code", help="大批量恢复的确认码，见提示")
    ap.add_argument("--confirm-threshold", type=int, default=None,
                    help="内部安全默认值：改动超过多少条要二次确认（默认 50，**只能往小调**）")
    args = ap.parse_args()
    threshold = confirm_threshold(args.confirm_threshold)

    app_id, entry_id, rows = load_backup(args.backup)
    if not app_id or not entry_id:
        sys.stderr.write("备份里没有 app_id/entry_id，无法确定恢复到哪张表。\n")
        return 2
    if not rows:
        print("备份是空的，没有可恢复的记录。")
        return 0

    client = JdyClient()
    changed, same, gone, unrestorable = restorable(client, app_id, entry_id, rows)

    print("=" * 66)
    print("恢复计划%s" % ("" if args.execute else "（DRY-RUN，未写入）"))
    print("=" * 66)
    print("备份文件      %s（%d 行）" % (os.path.basename(args.backup), len(rows)))
    print("目标          %s / %s" % (app_id, entry_id))
    print("需要改回      %d 条" % len(changed))
    print("已经一致      %d 条（不动）" % len(same))
    if unrestorable:
        # 必须在 **dry-run 就说**。留到执行时才在「未提交的字段」里冒出来，
        # 等于让人在"以为要恢复 N 个字段"的前提下点了头。
        print("恢复不了的列  %d 个（读回来的值不是能写回去的形状）：" % len(unrestorable))
        for label, wtype, why in unrestorable[:6]:
            print("              「%s」(%s) %s" % (label, wtype, why[:44]))
        if len(unrestorable) > 6:
            print("              … 另有 %d 个" % (len(unrestorable) - 6))
        print("              这些列**不会被恢复**，其值仍在备份文件里，请在界面上处理。")
    if gone:
        # 恢复不了就必须说恢复不了。悄悄新建一条 ID 不同的记录，
        # 看着像恢复了，实际上所有指向它的关联全是断的。
        print("已被删除      %d 条 —— **本工具恢复不了**：简道云不允许指定 data_id 新建，"
              % len(gone))
        print("              新建出来的是另一条记录、另一个 ID，指向它的关联仍然是断的。")
        print("              这些记录的内容都还在备份文件里，请在界面上自行处置。")
    if not changed:
        print("\n没有需要改回的字段。")
        return 0

    w = col_width([str(c["data_id"]) for c in changed[:5]], 10)
    print("\n改动样例（最多 5 条）：")
    for item in changed[:5]:
        for label, d in list(item["diff"].items())[:3]:
            print("  %s  「%s」 %r → %r"
                  % (pad(item["data_id"], w), label, d["from"], d["to"]))

    code = plan_code({"app": app_id, "entry": entry_id,
                      "ids": sorted(c["data_id"] for c in changed)})
    if not args.execute:
        print("\n以上均未写入。确认后执行：")
        print("    --execute --yes%s"
              % ("" if len(changed) <= threshold else " --confirm-code %s" % code))
        return 0

    gated = scale_gate(len(changed), code, args.confirm_code, threshold,
                       ["改回 %d 条记录" % len(changed)], what="恢复")
    if gated is not None:
        return gated

    if not args.yes:
        answered = ask_yes("确认把 %d 条记录改回备份时的值？输入 yes：" % len(changed))
        if answered is None:            # 不是 tty，或 Windows 的 NUL 让 input() EOF
            sys.stderr.write(
                "拒绝写入：当前是非交互环境，无法当面向用户确认。\n"
                "恢复同样是在改存量数据——请先把上面的改动样例复述给用户、\n"
                "取得明确同意后，再加 --yes 重新执行。\n")
            return 4
        if not answered:
            print("已取消")
            return 0

    # 恢复之前先给"现在的样子"再落一份备份：恢复本身也是一次批量写入，
    # 万一是恢复错了文件，没有这一份就真的回不去了。
    safety = backup_path(os.path.dirname(os.path.abspath(args.backup)), entry_id)
    try:
        n = client.backup(app_id, entry_id, safety)
        print("\n恢复前又备份了一份当前状态：%s（%d 行）" % (os.path.basename(safety), n))
    except (JdyError, OSError) as exc:
        sys.stderr.write("备份失败，已中止——恢复也是写入，没有备份不动数据。%s\n" % exc)
        return 3

    done, failed, dropped, not_submitted = 0, [], [], []
    for i, item in enumerate(changed, 1):
        if sys.stderr.isatty():
            sys.stderr.write("\r恢复 %d/%d …" % (i, len(changed)))
            sys.stderr.flush()
        # 回写**原始值**而不是 display_value 的产物——见 restorable() 的说明
        values = {label: d["value"] for label, d in item["diff"].items()}
        try:
            ok, skipped, mismatches = client.update(app_id, entry_id, item["data_id"], values)
        except JdyError as exc:
            failed.append((item["data_id"], str(exc)))
            continue
        if ok:
            done += 1
        not_submitted.extend(dict(s, data_id=item["data_id"]) for s in skipped)
        dropped.extend(mismatches)
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 30 + "\r")

    print("=" * 66)
    print("恢复完成：改回 %d/%d 条" % (done, len(changed)))
    if dropped:
        print("⚠️ 写入后为空的字段 %d 处（被简道云静默丢弃）" % len(dropped))
        for d in dropped[:5]:
            print("   「%s」(%s)" % (d.get("field"), d.get("type")))
    report_skipped(not_submitted)
    for data_id, err in failed[:5]:
        print("❌ %s：%s" % (data_id, err))
    if gone:
        print("⚠️ 另有 %d 条已被删除的记录**没有**被恢复——不要告诉用户已完整回滚" % len(gone))
    if unrestorable:
        print("⚠️ 另有 %d 列结构上恢复不了（见上），**不要说已完整回滚**" % len(unrestorable))
    if not dropped and not failed and not not_submitted and not gone and not unrestorable:
        print("✅ 逐字段回读核对通过")
    return 0 if not failed else 3


if __name__ == "__main__":
    sys.exit(cli_main(main))
