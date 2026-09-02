#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行清洗计划。默认 dry-run；写前整表备份；写后回读核对。

清洗改的是**存量数据**，比新增危险得多——错了没有"删掉重来"这条退路。
所以闸门比别处更密：备份失败即中止、大批量要确认码、非交互拒绝执行。
"""
import argparse
import json
import os
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("执行清洗", "清洗计划怎么跑")
from jdy_client import (JdyClient, JdyError, ask_yes, backup_path, cli_main, col_width,
                        confirm_threshold, pad, parse_tz, plan_code, report_skipped,
                        scale_gate)


def load_plan(path):
    with open(path, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    for key in ("app_id", "entry_id", "updates"):
        if key not in plan:
            raise ValueError("计划文件缺少 %s，请用 plan.py 重新生成" % key)
    return plan


def main():
    ap = argparse.ArgumentParser(description="执行数据清洗（默认 dry-run）")
    ap.add_argument("plan", help="plan.py --plan 产出的清洗计划 JSON")
    ap.add_argument("--execute", action="store_true", help="真正写入")
    ap.add_argument("--yes", action="store_true", help="已向用户取得确认（非交互必须给）")
    ap.add_argument("--no-backup", action="store_true", help="跳过写前备份（不建议）")
    ap.add_argument("--confirm-code", help="大批量写入的计划确认码，见提示")
    ap.add_argument("--confirm-threshold", type=int, default=None,
                    help="内部安全默认值：改动超过多少条要二次确认（默认 50，"
                         "**只能往小调**）")
    args = ap.parse_args()
    threshold = confirm_threshold(args.confirm_threshold)

    plan = load_plan(args.plan)
    app_id, entry_id = plan["app_id"], plan["entry_id"]
    updates = plan["updates"]
    if not updates:
        print("计划里没有要改的记录。")
        return 0

    client = JdyClient()
    code = plan_code({"app": app_id, "entry": entry_id,
                      "updates": len(updates),
                      "sample": sorted(k for u in updates[:50]
                                       for k in u["values"])[:40]})

    if not args.execute:
        print("=" * 66)
        print("DRY-RUN　未写入任何数据")
        print("=" * 66)
        print("待更新        %d 条记录" % len(updates))
        fields = sorted({k for u in updates for k in u["values"]})
        print("涉及字段      %s" % "、".join(fields))
        print("预计耗时      %.1f 秒"
              % client.estimate_seconds(len(updates), "/app/entry/data/update", 1))
        if len(updates) > threshold:
            print("\n本次改动 %d 条，属于大批量。请先跟用户说：" % len(updates))
            print("    「这次要修改简道云里 %d 条已有数据，确认执行吗？」" % len(updates))
            print("得到同意后执行：  --execute --yes --confirm-code %s" % code)
            print("（码由计划内容算出，重跑 plan.py 后会变。不要向用户提这个码）")
        else:
            print("\n确认无误后加 --execute --yes 执行。")
        return 0

    gated = scale_gate(len(updates), code, args.confirm_code, threshold,
                       ["更新 %d 条已有记录" % len(updates)], what="修改")
    if gated is not None:
        return gated

    if not args.yes:
        answered = ask_yes("确认修改 %d 条已有数据？输入 yes：" % len(updates))
        if answered is None:            # 不是 tty，或 Windows 的 NUL 让 input() EOF
            sys.stderr.write(
                "拒绝写入：当前是非交互环境，无法当面向用户确认。\n"
                "清洗改的是存量数据，错了没有退路——请先把改动样例复述给用户、\n"
                "取得明确同意后，再加 --yes 重新执行。\n")
            return 4
        if not answered:
            print("已取消")
            return 0

    if not args.no_backup:
        path = backup_path(os.path.dirname(os.path.abspath(args.plan)), entry_id)
        try:
            n = client.backup(app_id, entry_id, path)
            print("写前备份：%s（%d 行）" % (path, n))
            print("  要回滚就拿它喂给 restore.py：python3 scripts/restore.py %s"
                  % os.path.basename(path))
        except (JdyError, OSError) as exc:
            sys.stderr.write("备份失败，已中止——清洗动的是存量数据，没有备份不动。%s\n" % exc)
            return 3

    tz = parse_tz(plan.get("tz"))
    done, failed, dropped, not_submitted = 0, [], [], []
    for i, item in enumerate(updates, 1):
        if sys.stderr.isatty():
            sys.stderr.write("\r清洗 %d/%d …" % (i, len(updates)))
            sys.stderr.flush()
        try:
            ok, skipped, mismatches = client.update(app_id, entry_id,
                                                    item["data_id"],
                                                    item["values"], tz=tz)
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
    print("清洗完成")
    print("=" * 66)
    print("更新          %d/%d 条" % (done, len(updates)))
    if dropped:
        print("⚠️ 写入后为空的字段 %d 处（被简道云静默丢弃）：" % len(dropped))
        w = col_width([str(d.get("field")) for d in dropped[:5]], 8)
        for d in dropped[:5]:
            print("   %s (%s)" % (pad(d.get("field"), w), d.get("type")))
        print("**不要告诉用户已清洗完成**——请如实说明是哪些字段没写进去")
    report_skipped(not_submitted)
    for data_id, err in failed[:5]:
        print("❌ %s：%s" % (data_id, err))
    if not dropped and not failed and not not_submitted:
        print("✅ 逐字段回读核对通过")
    return 0 if not failed else 3


if __name__ == "__main__":
    sys.exit(cli_main(main))
