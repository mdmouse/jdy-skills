#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步差异计划：算出要新增/更新/跳过什么。只读，不写任何数据。"""
import argparse
import datetime
import json
import os
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("同步计划", "两边数据对一下", "子表单搬不过去")
from jdy_client import cli_main, pad, JdyClient, JdyError
from miniyaml import parse as parse_yaml
from sync import IdMap, SyncError, load_config, plan_table, source_label, topo_sort


def render(cfg, plans, id_map):
    out = []
    out.append("=" * 72)
    out.append("同步计划：%s　（DRY-RUN，未写入任何数据）" % (cfg.get("name") or "未命名"))
    out.append("=" * 72)
    out.append("源：%s → 目标应用 %s" % (source_label(cfg), cfg["target"]["app"]))
    out.append("同步顺序（被引用的表在前）：%s" % " → ".join(p["alias"] for p in plans))

    total_c = total_u = total_s = 0
    for p in plans:
        total_c += len(p["creates"])
        total_u += len(p["updates"])
        total_s += len(p["skips"])
        out.append("")
        out.append("▌%s　业务键「%s」　源 %d 行%s / 目标 %d 行"
                   % (p["alias"], p["key"], p["source_rows"],
                      "（limit=%d 试水）" % p["limited"] if p.get("limited") else "",
                      p["target_rows"]))
        out.append("   新增 %d　更新 %d　无变化 %d　（按业务键匹配上 %d 条，已登记 ID 映射）"
                   % (len(p["creates"]), len(p["updates"]), len(p["skips"]), p["matched"]))
        out.append("   搬运字段 %d 个%s"
                   % (len(p["mapped_fields"]),
                      "，引用字段 %s" % "、".join(p["ref_fields"]) if p["ref_fields"] else ""))
        if p.get("subform_fields"):
            # 子表单只能整表替换（API 没有行级增量）——目标端子表单里的人工改动
            # 会被源端覆盖。这是同步语义本身，但得先说，别让人写完才发现。
            out.append("   ⚙️ 子表单 %s：整表替换——**目标端这几列里的人工改动会被源端覆盖**"
                       % "、".join(p["subform_fields"]))
        att = p.get("attachments") or {}
        if att.get("files"):
            # 附件要真的下载再上传，一个都跑不掉。大表是几个 G、几十分钟，
            # 让人在点 --execute **之前**就看见（写到一半才发现更糟）。
            size = att.get("bytes", 0)
            out.append("   📎 附件 %s：本次要搬 %d 个文件、约 %s"
                       "（每个都要下载再上传，请预留时间与磁盘）"
                       % ("、".join(p.get("attachment_fields") or []), att["files"],
                          "%.1f MB" % (size / 1048576.0) if size >= 1048576
                          else "%.0f KB" % (size / 1024.0)))
        if p["excluded"]:
            out.append("   ⚠️ 搬不过去的字段：")
            for name, why in p["excluded"]:
                out.append("      · %s %s" % (pad(name, 16), why))
        if p["problems"]:
            kinds = {}
            for pr in p["problems"]:
                kinds.setdefault(pr["kind"], []).append(pr)
            out.append("   ❌ 待解决 %d 处：" % len(p["problems"]))
            for kind, items in kinds.items():
                out.append("      · %s（%d 处）" % (kind, len(items)))
                for it in items[:3]:
                    out.append("          %s" % it["detail"])
                if len(items) > 3:
                    out.append("          … 另有 %d 处" % (len(items) - 3))
        for u in p["updates"][:3]:
            out.append("   ~ 更新 %s：%s" % (u["key"],
                                            "、".join(sorted(u["diff"]))[:60]))
        if len(p["updates"]) > 3:
            out.append("   ~ … 另有 %d 条更新" % (len(p["updates"]) - 3))

    out.append("")
    out.append("-" * 72)
    out.append("合计：新增 %d　更新 %d　无变化 %d" % (total_c, total_u, total_s))
    limited = [p for p in plans if p.get("limited")]
    if limited:
        # 每张表旁边虽然标了 limit，但读的人是照着**合计**下结论的。
        # 「合计：新增 0」看着像"两边已经一致"，实际只比了抽样的前几行。
        out.append("⚠️ 上面的数字**不是全量差异**：%s 只比对了抽样的前几行"
                   "（%s）。要看全量，把配置里的 limit 去掉再跑一次。"
                   % ("、".join(p["alias"] for p in limited),
                      "、".join("%s=%d" % (p["alias"], p["limited"]) for p in limited)))
    out.append("ID 映射表：%s（已有 %d 条）"
               % (id_map.path, sum(len(v) for v in id_map.data.values())))
    problems = sum(len(p["problems"]) for p in plans)
    if problems:
        out.append("⚠️ 有 %d 处待解决——**引用翻译不出来的不会写入**，"
                   "宁可留空让人看见，也不写一个指向虚无的引用" % problems)
    out.append("\n确认无误后：python3 scripts/apply.py <配置> --execute --yes")
    out.append("（加 --json-out 存下这份计划，apply.py --plan-json 就能直接照它执行，"
               "省掉一轮全量重拉，也保证执行的与你看的是同一份）")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="生成同步差异计划（只读）")
    ap.add_argument("config")
    ap.add_argument("--json-out",
                    help="把计划另存为 JSON，交给 apply.py --plan-json 直接执行，"
                         "省掉一轮全量重拉")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config, parse_yaml)
    except (SyncError, OSError) as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2

    id_map = IdMap(cfg["_id_map_path"])
    try:
        tables = topo_sort(cfg["tables"])
        plans = [plan_table(client, cfg, t, id_map) for t in tables]
    except (SyncError, JdyError) as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    print(render(cfg, plans, id_map))
    id_map.save()          # 规划阶段确定的匹配关系要落盘，供后续引用翻译与续跑
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            # generated_at 是给 apply.py 判断"这份计划有多旧"用的。
            # 复用快照省掉一轮全量重拉，但源数据可能已经变了——
            # 没有时间戳就没法说清这个风险，只能把它藏起来。
            json.dump({"config": os.path.abspath(args.config), "plans": plans,
                       "generated_at": datetime.datetime.now(
                           datetime.timezone.utc).isoformat()},
                      fh, ensure_ascii=False, indent=2)
        print("计划已保存：%s" % args.json_out)
        print("直接执行它（不再重拉一遍）：")
        print("    python3 scripts/apply.py %s --plan-json %s --execute --yes"
              % (args.config, args.json_out))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
