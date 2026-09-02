#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从两个应用的真实结构生成同步配置草稿。只读。

不生成空模板让人填 ID——app_id / entry_id 恰恰是用户最不知道的东西。
这里自动配对同名表单、按唯一性推荐业务键、识别关联字段并推断它指向哪张表，
把可用选项写进注释。人只需核对，不需要查。
"""
import argparse
import re
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("同步数据", "跨应用同步")
from jdy_client import (cli_main, LOOKUP_TYPES, NOT_WRITABLE_TYPES, READ_ONLY_TYPES,
                        JdyClient, writable_back,
                        JdyError, describe_targets, display_value, infer_lookup_target,
                        print_targets, resolve_app)
from miniyaml import yaml_quote
from sync import UNVERIFIED_WRITE, sync_shape


_DECOR = re.compile(r"[\*＊]|[（(\[【][^）)\]】]*[）)\]】]|[:：]\s*$")
_SPACE = re.compile(r"[\s　]+")


def normalize(text):
    s = "".join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
                for ch in str(text or ""))
    return _SPACE.sub("", _DECOR.sub("", s)).lower()


def suggest_field_mapping(src_by_label, dst_by_label):
    """猜源字段 → 目标字段。精确名 > 归一化名 > 单向包含且唯一。

    只按同名配对是不够的：真实场景里两个应用的同一概念常常叫不同名字
    （「客户名称」对「线索名称」），那样会连业务键都找不到。
    """
    norm_dst = {}
    for label in dst_by_label:
        norm_dst.setdefault(normalize(label), []).append(label)
    mapping, used, ambiguous = {}, set(), {}
    for src in src_by_label:
        if src in dst_by_label:
            mapping[src] = src
            used.add(src)
            continue
        cands = [d for d in norm_dst.get(normalize(src), []) if d not in used]
        if len(cands) == 1:
            mapping[src] = cands[0]
            used.add(cands[0])
            continue
        ns = normalize(src)
        loose = [d for d in dst_by_label
                 if d not in used and ns and
                 (ns in normalize(d) or normalize(d) in ns)]
        if len(loose) == 1:
            mapping[src] = loose[0]
            used.add(loose[0])
        elif len(loose) > 1:
            ambiguous[src] = loose
    return mapping, ambiguous


def uniqueness(rows, widget):
    """字段在样本里的唯一度与填充率——业务键要两者都高。"""
    if not rows:
        return 0.0, 0.0
    vals = [display_value(r.get(widget["name"]), widget["type"]) for r in rows]
    filled = [v for v in vals if v not in (None, "", [], {})]
    if not filled:
        return 0.0, 0.0
    return len(set(filled)) / float(len(filled)), len(filled) / float(len(rows))


def blocked_fields(shared, mapping, dst_by_label, src_by_label, unresolved_refs=()):
    """草稿里哪些字段**搬不过去**。

    搬不搬得动由 `sync_shape` 那一个判断说了算——计划（resolve_fields）问的
    也是它。这里原来自己列了三组类型、漏了 COMPLEX_WRITE（子表单/附件/图片）——
    于是草稿把它们写进 fields 白名单当"可搬"，而真正同步时 plan 又拒绝它们：
    同一份配置，生成它的人说能搬、执行它的人说不能搬。名单散在两处，迟早分叉。

    要**两边的 widget**：子表单要看内层对不对得上、附件要看目标端是不是也是附件（D9），
    只看目标端那一半判不出来。

    单拎成函数是为了能真的调它测——原来长在 main() 里三百行的中段，
    只能靠扫源码测，而扫源码的测试把判断改错了照样全绿。
    """
    return [l for l in shared
            if not sync_shape(src_by_label[l], dst_by_label[mapping[l]])[0]
            or l in unresolved_refs]


def suggest_key(src_by_label, dst_by_label, rows, mapping):
    """挑业务键：能映射到目标、唯一度高、填充率高。返回候选列表（降序）。"""
    cands = []
    for label, w in src_by_label.items():
        if label not in mapping:
            continue
        if not writable_back(w)[0]:
            continue                      # 搬不动的字段不能当业务键
        uniq, fill = uniqueness(rows, w)
        if uniq < 0.9 or fill < 0.8:
            continue                      # 不唯一或大量为空的字段当业务键会误判
        cands.append((uniq * 0.5 + fill * 0.5, label, uniq, fill))
    cands.sort(reverse=True)
    return cands


def main():
    ap = argparse.ArgumentParser(description="生成同步配置草稿（只读）")
    ap.add_argument("--source", help="源 app_id")
    ap.add_argument("--target", help="目标 app_id")
    ap.add_argument("--list", action="store_true", dest="do_list", help="列出全部应用")
    ap.add_argument("--out", help="输出 YAML 路径，缺省打印")
    ap.add_argument("--limit", type=int, default=5,
                    help="给草稿里的 limit 填多少，默认 5（先小范围试水）")
    args = ap.parse_args()

    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2

    try:
        if args.source:
            args.source = resolve_app(client, args.source)
        if args.target:
            args.target = resolve_app(client, args.target)
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    apps = client.list_apps()
    if args.do_list or not (args.source and args.target):
        print_targets(describe_targets(client), "授权范围内的应用：")
        if not (args.source and args.target):
            print("\n用 --source <应用名或ID> --target <应用名或ID> 生成草稿。")
        return 0

    names = {a["app_id"]: a["name"] for a in apps}
    src_forms = {f["name"]: f["entry_id"] for f in client.list_forms(args.source)}
    dst_forms = {f["name"]: f["entry_id"] for f in client.list_forms(args.target)}
    pairs = sorted(set(src_forms) & set(dst_forms))
    if not pairs:
        sys.stderr.write(
            "两个应用没有同名表单，无法自动配对。\n源表单：%s\n目标表单：%s\n"
            "请手工写配置，或先把目标表单改成与源同名。\n"
            % ("、".join(sorted(src_forms)[:12]), "、".join(sorted(dst_forms)[:12])))
        return 2

    lines = ["# 由 init_config.py 从真实结构生成。**请核对业务键与字段映射再执行。**",
             "name: %s → %s" % (names.get(args.source, args.source),
                                names.get(args.target, args.target)),
             "source: {app: %s}" % args.source,
             "target: {app: %s}" % args.target,
             "id_map: ./idmap.json",
             "",
             "tables:"]

    alias_by_form = {}
    for i, form in enumerate(pairs, 1):
        alias_by_form[form] = "t%d" % i

    unusable = []
    for form in pairs:
        se, de = src_forms[form], dst_forms[form]
        src_by_label, _ = client.field_map(args.source, se)
        dst_by_label, _ = client.field_map(args.target, de)
        try:
            rows = client.fetch_all(args.source, se, limit=50, page_size=50)
        except JdyError:
            rows = []
        mapping, ambiguous = suggest_field_mapping(src_by_label, dst_by_label)
        shared = list(mapping)
        keys = suggest_key(src_by_label, dst_by_label, rows, mapping)

        block = []
        block.append("  - alias: %s" % alias_by_form[form])
        block.append("    # 表单「%s」　源 %d 行（抽样）" % (form, len(rows)))
        block.append('    source_entry: "%s"' % se)
        block.append('    target_entry: "%s"' % de)
        if keys:
            _, label, uniq, fill = keys[0]
            block.append("    key: %s          # 唯一度 %.0f%%　填充率 %.0f%%"
                         % (yaml_quote(label), uniq * 100, fill * 100))
            if len(keys) > 1:
                block.append("    # 其他候选业务键：%s"
                             % "、".join("%s(唯一%.0f%%)" % (k[1], k[2] * 100)
                                         for k in keys[1:4]))
        else:
            uniq_src = []
            for label, w in src_by_label.items():
                u, f = uniqueness(rows, w)
                if u >= 0.9 and f >= 0.8:
                    uniq_src.append("%s(唯一%.0f%%)" % (label, u * 100))
            block.append("    key: 请填写业务键")
            block.append("    # 源端唯一的字段：%s" % ("、".join(uniq_src[:6]) or "无"))
            block.append("    # 目标端可选字段：%s" % "、".join(list(dst_by_label)[:8]))
        block.append("    limit: %d          # 先小范围试水，确认口径后再去掉" % args.limit)

        # 引用：源端的关联数据字段，且目标端对应字段也是关联数据
        refs, unresolved_refs = [], []
        for label in shared:
            sw, dw = src_by_label[label], dst_by_label[mapping[label]]
            if sw["type"] not in LOOKUP_TYPES:
                continue
            if dw["type"] not in LOOKUP_TYPES:
                block.append("    # ⚠️ 「%s」源端是关联数据，但目标端是 %s——关系搬不过去"
                             % (label, dw["type"]))
                continue
            target_entry = infer_lookup_target(client, args.source, se, sw)
            target_form = next((n for n, e in src_forms.items() if e == target_entry), None)
            if target_form and target_form in alias_by_form:
                refs.append((label, alias_by_form[target_form], target_form))
            else:
                # 推断不出来就**不要把它放进 fields 白名单**。
                # 原来只加一行注释，字段照进白名单：源端的 data_id 会被原样写进
                # 目标应用，成为一个指向虚无的引用——接口不校验，回读也"一致"。
                unresolved_refs.append(label)
                block.append("    # ⚠️ 「%s」指向的表%s不在本次同步范围内——已排除。"
                             % (label, "（%s）" % target_form if target_form else ""))
                block.append("    #    要搬它：把被引用的表也纳入同步，再在 refs 里声明。")
        if refs:
            block.append("    refs:")
            for label, alias, form_name in refs:
                block.append("      %s: %s          # 指向「%s」"
                             % (yaml_quote(label), alias, form_name))

        blocked = blocked_fields(shared, mapping, dst_by_label, src_by_label,
                                 unresolved_refs)
        movable = [l for l in shared if l not in blocked and l not in {r[0] for r in refs}]
        block.append("    # 字段映射 %d 对，其中可搬 %d 个" % (len(shared), len(movable)))
        if blocked:
            block.append("    # 搬不过去：%s" % "、".join(blocked[:8]))
            for l in blocked[:8]:
                if l in unresolved_refs:
                    continue
                # 理由也走 sync_shape——原来这里另外调一次 writable_back，
                # 于是"判定"和"说法"是两个函数算的，子表单搬得动之后就对不上了
                why = sync_shape(src_by_label[l], dst_by_label[mapping[l]])[1]
                block.append("    #    %s：%s" % (l, why))
        # 子表单整列搬得动、但内层有几个字段搬不动——这种"搬了但没全搬"最容易
        # 被当成全搬了，所以草稿里就写明白（D2/D3）
        for l in shared:
            if l in blocked or l in {r[0] for r in refs}:
                continue
            shape = sync_shape(src_by_label[l], dst_by_label[mapping[l]])[2]
            for where, why in (shape or {}).get("inner_excluded", [])[:6]:
                block.append("    #    ⚠️ %s 搬不过去：%s" % (where, why))
        # fields 是白名单：写了就只搬列出的。所以必须把**全部**可搬字段写进去，
        # 只写改名的那几对会让同名字段被静默丢掉（草稿说 6 对、实际只搬 3 对）。
        movable_map = {s_: d for s_, d in mapping.items()
                       if s_ not in blocked and s_ not in {r[0] for r in refs}}
        if movable_map:
            block.append("    fields:          # 白名单：只搬这里列出的。改名的已猜好，**请核对**")
            for s_, d in sorted(movable_map.items()):
                mark = "" if s_ == d else "    # 改名"
                # 显示名不加引号写进键位，带 `:` 或 `#` 的名字就把配置写坏了。
                # 字段名是用户在界面里随手起的，「金额(元)：含税」很常见。
                block.append("      %s: %s%s" % (yaml_quote(s_), yaml_quote(d), mark))
        if ambiguous:
            block.append("    # ❓ 这些源字段有多个可能的目标，未自动映射：")
            for s_, cands in list(ambiguous.items())[:5]:
                block.append("    #   %s → %s" % (s_, "、".join(cands)))

        # 没有可用业务键的表整块注释掉。留个 key: ??? 会让草稿"看着能跑"，
        # 实际在运行时炸出「业务键字段「???」在表里不存在」——一句看不出该怎么办的错。
        # 注释掉则草稿开箱即跑，被排除的表和原因也都在眼前。
        if not keys:
            unusable.append(form)
            lines.append("  # ⛔ 「%s」没有可用业务键，已注释掉——" % form)
            lines.append("  #    源端没有既唯一又填充充分、且能映射到目标的字段。")
            lines.append("  #    想同步它：把 key 填成源端某个唯一字段（必要时在 fields 里补映射），")
            lines.append("  #    再把下面每行开头的 '#|' 去掉即可。缩进已保留。")
            # 用独特前缀 '#|' 而非普通 '# '：草稿里还有说明性注释，
            # 形状一样的话"去掉每行开头的 #"就有歧义，容易把说明也变成配置。
            lines.extend("#|" + b if b.strip() else "#|" for b in block)
        else:
            lines.extend(block)
        lines.append("")

    only_src = sorted(set(src_forms) - set(dst_forms))
    only_dst = sorted(set(dst_forms) - set(src_forms))
    if only_src or only_dst:
        lines.append("# 未配对的表单（名字不同就配不上，需要时手工补）：")
        if only_src:
            lines.append("#   只在源应用：%s" % "、".join(only_src[:10]))
        if only_dst:
            lines.append("#   只在目标应用：%s" % "、".join(only_dst[:10]))

    text = "\n".join(lines)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        usable = len(pairs) - len(unusable)
        print("同步配置草稿已生成：%s" % args.out)
        print("  可直接用：%d 张表" % usable)
        if unusable:
            print("  已注释掉：%d 张（没有可用业务键）：%s" % (len(unusable), "、".join(unusable)))
        print("请核对：业务键选得对不对、字段名两边是否需要映射、引用有没有漏。")
        print("然后：python3 scripts/plan.py %s" % args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
