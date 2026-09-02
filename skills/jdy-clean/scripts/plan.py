#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成清洗计划。只读，不写任何数据。

两种清洗，都只产生**更新**，永不删除：

  --normalize  去首尾空白、压缩连续空格、全角转半角、去控制字符
  --dedupe     按某列找出重复组，往指定字段写标记，供人工核对后自行处置

**为什么不删**：重复不等于错误——同名两人、同号两单，删错了不可逆。
引擎能做的是把重复摆出来、打上标记；删哪条是业务判断，得人自己在界面上做。
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("清洗数据", "格式不统一", "全角半角", "去掉多余空格", "批量规范化", "查重", "有重复数据")
from jdy_client import (JdyClient, cli_main, col_width, describe_targets,
                        display_value, pad, print_targets, resolve_app, resolve_entry)
from quality import duplicate_groups, issues_of, normalized


# 规范化只动纯文本列。**显式点名的列同样要过这一关**：
# 成员/地址/多选这些列，display_value 给的是拼好的显示串（"张三"、"江苏省无锡市…"），
# 规范化后写回去会在编码阶段被拒（成员只认 username、地址只认对象），
# 于是整条记录进 skipped、清洗报告却照打"✅ 核对通过"——它压根没被提交过。
NORMALIZABLE = ("text", "textarea")


def build_normalize(rows, by_label, columns):
    """找出值有毛病、且规范化后会变的单元格。"""
    changes = []
    for row in rows:
        diff = {}
        for label in columns:
            w = by_label[label]
            raw = display_value(row.get(w["name"]), w["type"])
            if raw in (None, "", [], {}):
                continue
            fixed = normalized(raw)
            if fixed != str(raw):
                diff[label] = {"from": str(raw), "to": fixed,
                               "why": "、".join(issues_of(raw)) or "格式规范化"}
        if diff:
            changes.append({"data_id": row["_id"], "diff": diff})
    return changes


def build_dedupe(rows, by_label, key_label, mark_label, mark_text):
    """按 key_label 找重复组，给**组内每一条**打标记。

    不选"保留哪条"——那是业务判断。全部标出来，让人在界面上按标记筛选处理。
    """
    w = by_label[key_label]
    groups = duplicate_groups(
        rows, lambda r: display_value(r.get(w["name"]), w["type"]))
    changes, detail = [], []
    for key, members in sorted(groups.items()):
        detail.append({"key": key, "ids": [m["_id"] for m in members]})
        mw = by_label[mark_label]
        mark = "%s%s" % (mark_text, key)
        for m in members:
            old = display_value(m.get(mw["name"]), mw["type"])
            old = "" if old in (None, "", [], {}) else str(old)
            if mark in old:
                continue            # 已经标过，重跑不重复追加
            # **追加而不是覆盖**：标记列多半是「备注」这种有内容的字段，
            # 直接覆盖等于替用户删掉一段他写的东西。
            new_value = ("%s | %s" % (old, mark)) if old else mark
            changes.append({"data_id": m["_id"], "diff": {mark_label: {
                "from": old or None, "to": new_value,
                "why": "与另外 %d 条的「%s」相同" % (len(members) - 1, key_label)}}})
    return changes, detail


def build_plan(app_id, entry_id, merged, dup_detail):
    """计划的形状。**没有要改的记录时同样产出一份合法计划**（updates 为空）。

    给了 `--plan` 却不生成文件的话，"没事可做"和"出错了"在调用方看来一模一样：
    都是拿不到文件，下一步 apply.py 甩一句"读写文件失败：No such file"。
    空计划让流水线照常走完——apply.py 读到它会说"计划里没有要改的记录"并正常退出。
    """
    return {"app_id": app_id, "entry_id": entry_id,
            "updates": [{"data_id": k, "values": {l: c["to"] for l, c in v.items()}}
                        for k, v in merged.items()],
            "detail": {k: v for k, v in merged.items()},
            "duplicate_groups": dup_detail}


def main():
    ap = argparse.ArgumentParser(description="生成数据清洗计划（只读）")
    ap.add_argument("--app", help="应用名或 ID；不确定就先 --list")
    ap.add_argument("--entry", help="表单名或 ID")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="列出应用；配合 --app 则列出该应用的表单")
    ap.add_argument("--normalize", help="要规范化的列（逗号分隔）；给 * 表示全部文本列")
    ap.add_argument("--dedupe", help="按这一列找重复")
    ap.add_argument("--mark-field", help="重复标记写到哪一列（配合 --dedupe）")
    ap.add_argument("--mark-text", default="重复待核对：",
                    help="标记文案前缀，默认「重复待核对：」")
    ap.add_argument("--limit", type=int, help="只看前 N 行")
    ap.add_argument("--plan", help="计划输出路径（JSON）")
    args = ap.parse_args()

    client = JdyClient()
    if args.do_list or not (args.app and args.entry):
        app_id = resolve_app(client, args.app) if args.app else None
        print_targets(describe_targets(client, app_id),
                      "应用：" if not app_id else "该应用下的表单：")
        print("\n用法：plan.py --app <应用> --entry <表单> --normalize <列> [--plan p.json]")
        return 0
    args.app = resolve_app(client, args.app)
    args.entry = resolve_entry(client, args.app, args.entry)
    if not (args.normalize or args.dedupe):
        sys.stderr.write("要做什么？--normalize <列> 或 --dedupe <列> 至少给一个\n")
        return 2

    by_label, _ = client.field_map(args.app, args.entry)
    rows = client.fetch_all(args.app, args.entry, limit=args.limit)

    changes, dup_detail = [], []
    if args.normalize:
        if args.normalize.strip() == "*":
            cols = [l for l, w in by_label.items() if w["type"] in NORMALIZABLE]
        else:
            cols = [c.strip() for c in args.normalize.split(",") if c.strip()]
        missing = [c for c in cols if c not in by_label]
        if missing:
            sys.stderr.write("表单里没有这些列：%s\n可用：%s\n"
                             % ("、".join(missing), "、".join(list(by_label)[:12])))
            return 2
        not_text = [c for c in cols if by_label[c]["type"] not in NORMALIZABLE]
        if not_text:
            print("跳过非文本列：%s"
                  % "、".join("%s（%s）" % (c, by_label[c]["type"]) for c in not_text))
            print("  这些列读出来是结构化值，规范化后写回去会在编码阶段被拒，"
                  "结果是「计划里有、实际没提交」。要改它们请在简道云界面处理。")
            cols = [c for c in cols if c not in set(not_text)]
        if not cols:
            sys.stderr.write("没有可规范化的文本列。\n")
            return 2
        changes.extend(build_normalize(rows, by_label, cols))

    if args.dedupe:
        if args.dedupe not in by_label:
            sys.stderr.write("表单里没有列「%s」\n" % args.dedupe)
            return 2
        if not args.mark_field:
            sys.stderr.write(
                "去重需要 --mark-field 指定标记写到哪一列。\n"
                "本技能**不删数据**：重复不等于错误，删错了不可逆。\n"
                "它只把重复标出来，删哪条由你在界面上决定。\n")
            return 2
        if args.mark_field not in by_label:
            sys.stderr.write("表单里没有列「%s」\n" % args.mark_field)
            return 2
        dup_changes, dup_detail = build_dedupe(rows, by_label, args.dedupe,
                                               args.mark_field, args.mark_text)
        changes.extend(dup_changes)

    # 同一条记录的多处改动合并成一次更新
    merged = {}
    for c in changes:
        merged.setdefault(c["data_id"], {}).update(c["diff"])

    print("=" * 72)
    print("清洗计划（DRY-RUN，未写入任何数据）：%d 行中 %d 条要改"
          % (len(rows), len(merged)))
    print("=" * 72)
    if dup_detail:
        print("\n【重复组】共 %d 组，涉及 %d 条记录"
              % (len(dup_detail), sum(len(d["ids"]) for d in dup_detail)))
        for d in dup_detail[:10]:
            print("  「%s」= %s　%d 条：%s"
                  % (args.dedupe, d["key"], len(d["ids"]),
                     "、".join(i[:8] + "…" for i in d["ids"][:4])))
        if len(dup_detail) > 10:
            print("  …另有 %d 组" % (len(dup_detail) - 10))
        print("  ⚠️ 本技能只打标记，**不会删除任何记录**——删哪条请你在界面上决定")

    samples = list(merged.items())[:8]
    if samples:
        print("\n【改动样例】")
        w = col_width([l for _, d in samples for l in d], 6)
        for data_id, diff in samples:
            print("  %s" % data_id)
            for label, ch in diff.items():
                print("    %s %r → %r　（%s）"
                      % (pad(label, w), ch["from"], ch["to"], ch["why"]))
        if len(merged) > 8:
            print("  …另有 %d 条" % (len(merged) - 8))

    print("\n" + "-" * 72)
    if not merged:
        print("没有需要清洗的内容。")
        if not args.plan:
            return 0
        # 给了 --plan 就得产出文件，哪怕是空的。原来这里直接 return，
        # 于是"没东西可改"和"出错了"在调用方看来一模一样：
        # 都是拿不到文件，下一步 apply.py 甩一句"读写文件失败"。
        # 空计划让流水线照常走完，apply.py 会说"计划里没有要改的记录"。
    if args.plan:
        with open(args.plan, "w", encoding="utf-8") as fh:
            json.dump(build_plan(args.app, args.entry, merged, dup_detail), fh,
                      ensure_ascii=False, indent=2)
        if merged:
            print("计划已保存：%s" % args.plan)
            print("把改动样例复述给用户、取得同意后："
                  "python3 scripts/apply.py %s --execute --yes" % args.plan)
        else:
            print("已保存一份**空计划**：%s（没有要改的记录）" % args.plan)
        return 0
    else:
        print("加 --plan <路径> 保存计划，再用 apply.py 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
