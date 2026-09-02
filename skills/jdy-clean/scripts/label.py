#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分类打标 / 信息提取：把待处理的行分批交给 Agent，再把结果写回。

**技能不做判断。** 分类口径、提取规则都是业务知识，引擎不该知道，
也不该替用户定。它只负责三件机械的事：
    分批导出 → 校验回填 → 生成更新计划

为什么这样能突破官方 AI 节点的 50 行 × 10 列上限：那是平台侧节点的限制，
而这里做判断的是 Agent 自己，一批处理多少行只取决于它的上下文，
也不消耗平台点数。

    export   拉出待标的行，写成一个批次文件（含空的目标字段）
    （Agent 打开批次文件，逐行填上判断，存回去）
    collect  读回批次文件，校验后生成更新计划，交给 apply.py 执行
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("打标签", "分类打标", "批量分类", "给数据分类", "信息提取", "贴标签", "自动归类")
from jdy_client import (NOT_WRITABLE_TYPES, READ_ONLY_TYPES, JdyClient,
                        cli_main, describe_targets, display_value, print_targets,
                        resolve_app, resolve_entry)

DEFAULT_BATCH = 50       # 一批多少行。太大 Agent 读不完，太小来回次数多


def build_batches(rows, by_label, source_fields, target_field, only_empty, size):
    """把待标的行切成批次。每行只带**判断需要的那几列**，不带无关字段。"""
    tw = by_label[target_field]
    items = []
    for row in rows:
        current = display_value(row.get(tw["name"]), tw["type"])
        if only_empty and current not in (None, "", [], {}):
            continue
        source = {}
        for label in source_fields:
            w = by_label[label]
            source[label] = display_value(row.get(w["name"]), w["type"])
        if all(v in (None, "", [], {}) for v in source.values()):
            continue          # 判断依据全空，没什么可判的
        items.append({"data_id": row["_id"], "source": source, target_field: ""})
    return [items[i:i + size] for i in range(0, len(items), size)]


def collect(batch, target_field):
    """读回填好的批次。返回 (待写更新, 未填的行数, 被改坏的行)。

    只接受目标字段的值；**源字段若被改动一律拒绝**——
    打标就该只写标签，源数据被顺手改了是最难发现的一类损坏。
    """
    updates, blank, tampered = [], 0, []
    for item in batch.get("items", []):
        value = item.get(target_field)
        if value in (None, "", [], {}):
            blank += 1
            continue
        original = batch.get("_source_snapshot", {}).get(item["data_id"])
        if original is not None and item.get("source") != original:
            tampered.append(item["data_id"])
            continue
        updates.append({"data_id": item["data_id"], "values": {target_field: value}})
    return updates, blank, tampered


def main():
    ap = argparse.ArgumentParser(description="分类打标 / 信息提取的批次导出与回收")
    ap.add_argument("--app", help="应用名或 ID；不确定就先 --list")
    ap.add_argument("--entry", help="表单名或 ID")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="列出应用；配合 --app 则列出该应用的表单")
    ap.add_argument("--source", help="判断依据的列（逗号分隔），如：反馈内容,标题")
    ap.add_argument("--target", help="标签写到哪一列")
    ap.add_argument("--only-empty", action="store_true", default=True,
                    help="只处理目标列为空的行（默认开）")
    ap.add_argument("--redo", action="store_true",
                    help="连已有标签的行也重新打（会覆盖，慎用）")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH,
                    help="每批多少行，默认 %d" % DEFAULT_BATCH)
    ap.add_argument("--out", help="批次文件输出前缀，会生成 <前缀>-1.json …")
    ap.add_argument("--labels",
                    help="直接给一份 {data_id: 标签} 的 JSON——小批量时不必走"
                         "导出/回填/回收那一圈，你自己读完数据填好这一个文件即可")
    ap.add_argument("--collect", nargs="+",
                    help="回收：读这些填好的批次文件，生成更新计划")
    ap.add_argument("--plan", help="回收时的计划输出路径")
    args = ap.parse_args()

    client = JdyClient()

    if args.labels:
        # 小批量的出路。批次那一圈是为上千行准备的：一批批导出、逐个填回、
        # 再回收。15 行也走那套就是纯仪式——实测中 Agent 因此宁可自己写脚本，
        # 于是备份、规模闸门、只写标签列、写后回读全都没了。
        # 这里只留真正值钱的部分：校验 + 生成计划，写入仍走 apply.py。
        if not (args.app and args.entry and args.target):
            sys.stderr.write("--labels 需要同时给 --app --entry --target\n")
            return 2
        args.app = resolve_app(client, args.app)
        args.entry = resolve_entry(client, args.app, args.entry)
        by_label, _ = client.field_map(args.app, args.entry)
        if args.target not in by_label:
            sys.stderr.write("表单里没有列「%s」\n" % args.target)
            return 2
        tw = by_label[args.target]
        if tw["type"] in NOT_WRITABLE_TYPES or tw["type"] in READ_ONLY_TYPES:
            sys.stderr.write("标签列「%s」是 %s 类型，接口写不进去——换一列\n"
                             % (args.target, tw["type"]))
            return 2
        with open(args.labels, "r", encoding="utf-8") as fh:
            given = json.load(fh)
        known = {r["_id"] for r in client.fetch_all(args.app, args.entry,
                                                    fields=["_id"])}
        updates, blank, unknown = [], 0, []
        for data_id, value in given.items():
            if value in (None, "", [], {}):
                blank += 1
                continue
            if data_id not in known:
                unknown.append(data_id)      # 记录不存在就别写，别造出幽灵更新
                continue
            updates.append({"data_id": data_id, "values": {args.target: value}})
        print("收到 %d 条标签：可写入 %d，未填 %d，记录不存在 %d"
              % (len(given), len(updates), blank, len(unknown)))
        for u in unknown[:5]:
            print("   ❌ %s 不在该表里" % u)
        if not updates:
            print("没有可写入的内容。")
            return 0
        if args.plan:
            with open(args.plan, "w", encoding="utf-8") as fh:
                json.dump({"app_id": args.app, "entry_id": args.entry,
                           "updates": updates}, fh, ensure_ascii=False, indent=2)
            print("计划已保存：%s" % args.plan)
            print("把样例复述给用户、取得同意后："
                  "python3 scripts/apply.py %s --execute --yes" % args.plan)
        else:
            print("加 --plan <路径> 保存计划，再用 apply.py 执行"
                  "（这样才有写前备份、规模闸门、写后回读）。")
        return 0

    if args.collect:
        all_updates, blank, tampered, target = [], 0, [], None
        app_id = entry_id = None
        for path in args.collect:
            with open(path, "r", encoding="utf-8") as fh:
                batch = json.load(fh)
            target = target or batch.get("target_field")
            app_id = app_id or batch.get("app_id")
            entry_id = entry_id or batch.get("entry_id")
            u, b, t = collect(batch, batch["target_field"])
            all_updates.extend(u)
            blank += b
            tampered.extend(t)
        print("回收 %d 个批次：可写入 %d 行，未填 %d 行" % (len(args.collect),
                                                          len(all_updates), blank))
        if tampered:
            print("⚠️ 拒绝 %d 行：源字段被改动过——打标只该写标签列，"
                  "源数据被顺手改了是最难发现的损坏" % len(tampered))
            for t in tampered[:5]:
                print("     %s" % t)
        if not all_updates:
            print("没有可写入的内容。")
            return 0
        if args.plan:
            with open(args.plan, "w", encoding="utf-8") as fh:
                json.dump({"app_id": app_id, "entry_id": entry_id,
                           "updates": all_updates}, fh, ensure_ascii=False, indent=2)
            print("计划已保存：%s" % args.plan)
            print("把样例复述给用户、取得同意后：python3 scripts/apply.py %s --execute --yes"
                  % args.plan)
        else:
            print("加 --plan <路径> 保存计划，再用 apply.py 执行。")
        return 0

    if args.do_list or not (args.app and args.entry):
        aid = resolve_app(client, args.app) if args.app else None
        print_targets(describe_targets(client, aid),
                      "应用：" if not aid else "该应用下的表单：")
        print("\n用法：label.py --app <应用> --entry <表单> "
              "--source <依据列> --target <标签列> --out 批次")
        return 0
    args.app = resolve_app(client, args.app)
    args.entry = resolve_entry(client, args.app, args.entry)
    if not (args.source and args.target):
        sys.stderr.write("需要 --source（判断依据的列）与 --target（标签写到哪列）\n")
        return 2

    by_label, _ = client.field_map(args.app, args.entry)
    sources = [c.strip() for c in args.source.split(",") if c.strip()]
    missing = [c for c in sources + [args.target] if c not in by_label]
    if missing:
        sys.stderr.write("表单里没有这些列：%s\n可用：%s\n"
                         % ("、".join(missing), "、".join(list(by_label)[:12])))
        return 2
    tw = by_label[args.target]
    if tw["type"] in NOT_WRITABLE_TYPES or tw["type"] in READ_ONLY_TYPES:
        sys.stderr.write("标签列「%s」是 %s 类型，接口写不进去——换一列\n"
                         % (args.target, tw["type"]))
        return 2

    rows = client.fetch_all(args.app, args.entry)
    batches = build_batches(rows, by_label, sources, args.target,
                            only_empty=not args.redo, size=args.batch_size)
    total = sum(len(b) for b in batches)
    print("=" * 70)
    print("待打标 %d 行（表内共 %d 行）→ %d 个批次，每批 ≤%d"
          % (total, len(rows), len(batches), args.batch_size))
    print("=" * 70)
    print("判断依据：%s" % "、".join(sources))
    print("标签写入：%s（%s）" % (args.target, tw["type"]))
    if not args.redo:
        print("已有标签的行已跳过；要重打加 --redo（会覆盖）")
    if not total:
        print("\n没有需要打标的行。")
        return 0

    if not args.out:
        print("\n加 --out <前缀> 导出批次文件。")
        return 0
    for i, items in enumerate(batches, 1):
        path = "%s-%d.json" % (args.out, i)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"app_id": args.app, "entry_id": args.entry,
                       "target_field": args.target, "source_fields": sources,
                       "_source_snapshot": {it["data_id"]: it["source"]
                                            for it in items},
                       "items": items}, fh, ensure_ascii=False, indent=2)
        print("  %s（%d 行）" % (path, len(items)))
    print("\n接下来：**你自己**逐行读 items、把判断填进每行的「%s」字段，存回原文件。"
          % args.target)
    print("填完回收：python3 scripts/label.py --collect %s-*.json --plan p.json"
          % args.out)
    print("**不要改 source 里的值**——回收时会比对快照，改过的行一律拒绝写入。")
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
