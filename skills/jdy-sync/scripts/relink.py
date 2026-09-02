#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关系迁移：把「选择数据」(linkdata) 的关系搬到「关联数据」(lookup) 上。

**为什么需要这件事**：选择数据在简道云的**所有官方通道**都不可写——API、
GUI 的 Excel 导入、智能助手、自建插件、前端事件、数据联动，全部白名单排除，
唯一的写入方式是人在表单里点选。所以只要一张表用选择数据承载关系，
它就没法自动化：导入进不去、同步搬不过来、恢复也补不回来。

而这个关系**读得出来**——选择数据的值就是 `{"id": "<目标记录 data_id>"}`。
关联数据(lookup)又是可以直写 data_id 的。所以这条路是通的：
读出来、写到一个新的关联数据字段上，关系就活了。

中文名和 API 类型是反直觉的，别记混：**选择数据 = linkdata（死）、
关联数据 = lookup（活）**。

三步：
    relink.py --app <应用>                          扫描 + 迁移处方
    （按处方在简道云界面上加一个「关联数据」字段——这一步只能人来做）
    relink.py --app A --entry E --from 选择客户 --to 关联客户 [--execute --yes]
"""
import argparse
import os
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
TRIGGERS = ("选择数据搬不过去", "关联字段迁移", "关系搬不过来", "linkdata 怎么办",
            "换成关联数据")
from jdy_client import (JdyClient, JdyError, ask_yes, backup_path, cli_main,
                        confirm_threshold, infer_lookup_target, lookup_exists,
                        plan_code, report_skipped, resolve_app, resolve_entry,
                        scale_gate)


class Finder(object):
    """反查「这个 data_id 住在哪张表」，带记忆。

    为什么必须带记忆：`widget/list` 对 linkdata/lookup **不返回目标表**，
    只能拿已有的引用 ID 到各表逐个 data/get 试。天真地"每个字段各试一遍"
    是 O(字段 × 表 × ID) 次请求——CRM 那种二十来张表、十几个关联字段的应用
    直接跑到两分钟以上（实测超时）。

    而现实里这些字段**指向的就是那么几张表**（客户、商机、销售订单），
    所以按 data_id 记住结果，命中率很高。同一张表探过一次也不再探。
    """

    def __init__(self, client, app_id):
        self.client = client
        self.app_id = app_id
        self.forms = client.list_forms(app_id)
        self.names = {f["entry_id"]: f["name"] for f in self.forms}
        self._where = {}          # data_id → entry_id / None
        self._probes = 0
        # 预算要**随应用规模走**。原来是写死的 60 次、还是整个 Finder 累计的：
        # 二十来张表的应用探两三个字段就用光了，之后每一次 locate 都返回 None，
        # 而 None 的意思是"这个 ID 哪张表里都没有"——于是"预算用完了"被报成
        # "这条引用是坏的"。两件事必须分得开。
        self.budget = max(60, len(self.forms) * 4)
        self.gave_up = False      # 有没有因为预算用尽而没探完

    def locate(self, data_id, budget=None):
        """这个 ID 在本应用的哪张表里。

        探不到返回 None。**但 None 有两种**：真的每张表都试过了没有，
        和预算用尽提前收工。后者会把 self.gave_up 置上，且**不写进记忆**——
        没探完的结论记下来，下次还照着它答。
        """
        budget = self.budget if budget is None else budget
        if data_id in self._where:
            return self._where[data_id]
        found, ran_out = None, False
        for form in self.forms:
            if self._probes >= budget:
                ran_out = True
                self.gave_up = True
                break
            self._probes += 1
            try:
                got = self.client.post("/app/entry/data/get",
                                       {"app_id": self.app_id,
                                        "entry_id": form["entry_id"], "data_id": data_id})
            except JdyError:
                continue
            if (got.get("data") or {}).get("_id") == data_id:
                found = form["entry_id"]
                break
        if not ran_out or found:
            self._where[data_id] = found      # 没探完的不记，免得把半截结论当定论
        return found

    def target_of(self, rows, widget, sample=3):
        """一个关联/选择字段指向哪张表：拿它已有的几个引用去反查，要求一致。"""
        ids = []
        for row in rows:
            raw = row.get(widget["name"])
            ref = raw.get("id") if isinstance(raw, dict) else raw
            if isinstance(ref, str) and ref:
                ids.append(ref)
            if len(ids) >= sample:
                break
        if not ids:
            return None, []
        hits = [self.locate(i) for i in ids]
        real = [h for h in hits if h]
        if real and len(set(real)) == 1 and len(real) == len(hits):
            return real[0], ids
        return None, ids


def scan(client, app_id, progress=None):
    """找出这个应用里所有「选择数据」字段，连同它们指向哪张表、有多少行有值。

    只读。整张表的数据只拉一次，反查结果全程共享（见 Finder 的说明）。
    """
    finder = Finder(client, app_id)
    out = []
    scan.gave_up = False        # 探测预算有没有用尽（见下）
    for i, form in enumerate(finder.forms, 1):
        if progress:
            progress(i, len(finder.forms), form["name"])
        try:
            widgets = client.widgets(app_id, form["entry_id"])
        except JdyError:
            continue
        links = [w for w in widgets if w["type"] == "linkdata"]
        if not links:
            continue
        lookups = [w for w in widgets if w["type"] == "lookup"]
        rows = client.fetch_all(app_id, form["entry_id"], limit=200)
        ready = [(lk, finder.target_of(rows, lk)[0]) for lk in lookups]
        for w in links:
            filled = [r for r in rows if r.get(w["name"])]
            target, ids = finder.target_of(rows, w)
            # 反查不到时要分清是"样本不够"还是"引用本身已经断了"——
            # 混成一句"反查不到"，用户会以为是工具不行，
            # 而实际上他要面对的是一批已经断掉的引用。
            dangling = bool(ids) and target is None and all(
                finder.locate(i) is None for i in ids)
            out.append({
                "form": form["name"], "entry_id": form["entry_id"],
                "widget": w, "filled": len(filled), "total": len(rows),
                "target_entry": target,
                "target_name": finder.names.get(target) if target else None,
                "dangling": dangling, "budget_ran_out": finder.gave_up,
                "lookups": ready,
            })
    return out


def prescribe(item):
    """给一条 linkdata 开处方。说清楚**哪一步只能人做**。"""
    w, target = item["widget"], item["target_name"]
    same = [lk for lk, t in item["lookups"] if t and t == item["target_entry"]]
    lines = []
    if item["target_entry"] is None:
        if item.get("dangling"):
            lines.append("⚠️ 抽查的引用**在本应用的任何一张表里都找不到**——"
                         "要么目标记录已被删除（关系本身就是断的），"
                         "要么它指向别的应用。")
            lines.append("先在界面上打开一条看看：还能点开就是跨应用，"
                         "点不开就是断了。断了的话迁不迁移都一样，"
                         "要么重新点选、要么把这一列清掉。")
            return lines
        if not item["filled"]:
            lines.append("这一列一行值都没有，没有样本可供反查——"
                         "有数据之后再扫一次，或手工指定 --to。")
            return lines
        lines.append("在本应用里反查不到目标表——可能指向**别的应用**，"
                     "也可能样本不够。先在界面上确认它指向哪张表，再手工指定 --to。")
        return lines
    if same:
        lines.append("已经有指向「%s」的关联数据字段「%s」——可以直接回填："
                     % (target, same[0]["label"]))
        lines.append("    relink.py --app <应用> --entry %s --from %s --to %s"
                     % (item["entry_id"], w["label"], same[0]["label"]))
        return lines
    lines.append("在简道云界面给表单「%s」加一个**关联数据**字段，指向「%s」。"
                 % (item["form"], target))
    lines.append("**这一步只能人做**——选择数据不可写，加字段也没有 API。")
    lines.append("加完之后回填：")
    lines.append("    relink.py --app <应用> --entry %s --from %s --to <新字段名>"
                 % (item["entry_id"], w["label"]))
    return lines


def plan_backfill(client, app_id, entry_id, src_label, dst_label):
    """算出要回填哪些行。只读。"""
    # **必须允许刷新字段缓存。** 字段结构本地缓存 24 小时，而本工具的整个用法
    # 就是"刚在界面上加了个关联字段，马上回填"——缓存里当然没有它，
    # 于是工具一口咬定"表单里没有这个字段"，人对着界面上明明有的字段干瞪眼。
    by_label, _ = client.field_map_including(app_id, entry_id, [src_label, dst_label])
    for label in (src_label, dst_label):
        if label not in by_label:
            raise ValueError("表单里没有字段「%s」（字段缓存已刷新过，确实没有）"
                             % label)
    src, dst = by_label[src_label], by_label[dst_label]
    if src["type"] != "linkdata":
        raise ValueError("「%s」不是「选择数据」(linkdata)，是 %s" % (src_label, src["type"]))
    if dst["type"] != "lookup":
        raise ValueError("「%s」不是「关联数据」(lookup)，是 %s —— "
                         "只有关联数据能直写 data_id" % (dst_label, dst["type"]))

    rows = client.fetch_all(app_id, entry_id)
    todo, already, empty = [], [], 0
    for row in rows:
        raw = row.get(src["name"])
        ref = raw.get("id") if isinstance(raw, dict) else raw
        if not ref:
            empty += 1
            continue
        current = row.get(dst["name"])
        current_id = current.get("id") if isinstance(current, dict) else current
        if current_id == ref:
            already.append(row["_id"])
            continue
        todo.append({"data_id": row["_id"], "ref": str(ref),
                     "overwrite": bool(current_id)})
    return {"src": src, "dst": dst, "todo": todo, "already": already,
            "empty": empty, "total": len(rows)}


def main():
    ap = argparse.ArgumentParser(
        description="把「选择数据」的关系迁到「关联数据」上（默认 dry-run）")
    ap.add_argument("--app", required=True, help="应用名或 ID")
    ap.add_argument("--entry", help="表单名或 ID；不给就扫描整个应用出处方")
    ap.add_argument("--from", dest="src", help="源「选择数据」字段显示名")
    ap.add_argument("--to", dest="dst", help="目标「关联数据」字段显示名")
    ap.add_argument("--execute", action="store_true", help="真正写入")
    ap.add_argument("--yes", action="store_true", help="已向用户取得确认（非交互必须给）")
    ap.add_argument("--no-backup", action="store_true", help="跳过写前备份（不建议）")
    ap.add_argument("--confirm-code", help="大批量写入的确认码，见提示")
    ap.add_argument("--confirm-threshold", type=int, default=None,
                    help="内部安全默认值：改动超过多少条要二次确认（默认 50，只能往小调）")
    args = ap.parse_args()

    client = JdyClient()
    app_id = resolve_app(client, args.app)

    # ---- 扫描模式 ----
    if not (args.entry and args.src and args.dst):
        def progress(i, total, name):
            if sys.stderr.isatty():
                sys.stderr.write("\r扫描 %d/%d　%s …          " % (i, total, name[:16]))
                sys.stderr.flush()

        items = scan(client, app_id, progress)
        if sys.stderr.isatty():
            sys.stderr.write("\r" + " " * 50 + "\r")
        print("=" * 72)
        print("「选择数据」扫描：%s" % args.app)
        print("=" * 72)
        if not items:
            print("\n没有「选择数据」字段——这个应用的关系都是可自动化的。")
            return 0
        print("\n选择数据(linkdata)在简道云**所有官方通道都不可写**，唯一写入方式是"
              "人在表单里点选。\n但它的值读得出来（就是目标记录的 data_id），"
              "所以可以搬到「关联数据」(lookup) 上。\n")
        if any(i.get("budget_ran_out") for i in items):
            # 说清楚"没查完"和"查过了没有"的区别。这个应用大到把反查预算用光了，
            # 下面凡是写"反查不到"的地方都可能只是没探到，不代表引用是坏的。
            print("⚠️ 这个应用的表多到把反查预算用完了——下面的「反查不到」里，"
                  "有一些**只是没探到**，不等于引用已经断了。\n"
                  "   要确认某一个，单独跑一次：--entry <表单> 只扫那一张。\n")
        for item in items:
            w = item["widget"]
            print("▌%s / 「%s」" % (item["form"], w["label"]))
            print("   有值 %d / %d 行　指向：%s"
                  % (item["filled"], item["total"], item["target_name"] or "反查不到"))
            for line in prescribe(item):
                print("   %s" % line)
            print("")
        print("-" * 72)
        print("共 %d 个选择数据字段。加字段那一步只能在界面上做，加完回来回填。"
              % len(items))
        return 0

    # ---- 回填模式 ----
    entry_id = resolve_entry(client, app_id, args.entry)
    try:
        plan = plan_backfill(client, app_id, entry_id, args.src, args.dst)
    except (ValueError, JdyError) as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    todo = plan["todo"]
    print("=" * 72)
    print("关系回填：「%s」→「%s」%s"
          % (args.src, args.dst, "" if args.execute else "（DRY-RUN，未写入）"))
    print("=" * 72)
    print("总行数        %d" % plan["total"])
    print("要回填        %d" % len(todo))
    print("已经一致      %d（不动）" % len(plan["already"]))
    print("源字段为空    %d（不动）" % plan["empty"])
    overwrite = [t for t in todo if t["overwrite"]]
    if overwrite:
        print("⚠️ 其中 %d 行的目标字段**已有别的值**，会被覆盖：" % len(overwrite))
        for t in overwrite[:5]:
            print("      %s" % t["data_id"])
    if not todo:
        print("\n没有需要回填的。")
        return 0

    # 引用完整性：接口写入时**不校验**引用是否存在，写个不存在的 ID 照样入库、
    # 回读还"一致"——所以只能自己先查一遍。
    target = infer_lookup_target(client, app_id, entry_id, plan["src"])
    if target is None:
        print("\n⚠️ 反查不出源字段指向哪张表，**无法校验引用是否存在**。"
              "这不代表数据没问题，只代表我没验过。")
    else:
        bad = [t for t in todo[:20]
               if lookup_exists(client, app_id, target, t["ref"]) is False]
        if bad:
            print("\n❌ 抽查前 20 条，有 %d 条的引用在目标表里不存在：%s"
                  % (len(bad), "、".join(t["ref"] for t in bad[:3])))
            print("   继续回填只会把这些指向虚无的引用搬到新字段上。已中止。")
            return 2
        print("引用抽查      前 %d 条都存在于「%s」" % (min(20, len(todo)), target))

    code = plan_code({"app": app_id, "entry": entry_id, "src": args.src,
                      "dst": args.dst, "ids": sorted(t["data_id"] for t in todo)})
    threshold = confirm_threshold(args.confirm_threshold)
    if not args.execute:
        print("\n以上均未写入。确认后执行：")
        print("    --execute --yes%s"
              % ("" if len(todo) <= threshold else " --confirm-code %s" % code))
        print("\n⚠️ 回填只是把关系**复制**到新字段上，原来的选择数据字段不动。"
              "确认新字段好用之后，要不要隐藏旧字段由你在界面上决定——本工具不删东西。")
        return 0

    gated = scale_gate(len(todo), code, args.confirm_code, threshold,
                       ["回填 %d 行的关系" % len(todo)], what="回填")
    if gated is not None:
        return gated
    if not args.yes:
        answered = ask_yes("确认回填 %d 行？输入 yes：" % len(todo))
        if answered is None:            # 不是 tty，或 Windows 的 NUL 让 input() EOF
            sys.stderr.write(
                "拒绝写入：当前是非交互环境，无法当面向用户确认。\n"
                "请先把上面的计划复述给用户、取得明确同意，再加 --yes 重新执行。\n")
            return 4
        if not answered:
            print("已取消")
            return 0

    if not args.no_backup:
        path = backup_path(os.getcwd(), entry_id)
        try:
            n = client.backup(app_id, entry_id, path)
            print("写前备份：%s（%d 行）" % (path, n))
        except (JdyError, OSError) as exc:
            sys.stderr.write("备份失败，已中止——没有备份不动数据。%s\n" % exc)
            return 3

    # 先写一条探路：关联数据字段指向哪张表，`widget/list` 是不说的，
    # 而写错目标表的 ID 会原样存进去、回读还"一致"。写一条之后就能反查了。
    first = todo[0]
    ok, skipped, mism = client.update(app_id, entry_id, first["data_id"],
                                      {args.dst: first["ref"]})
    if not ok or mism:
        sys.stderr.write("探路的第一条没写成功，已中止（只动了这一条）：%s\n"
                         % (mism or skipped))
        return 3
    dst_target = infer_lookup_target(client, app_id, entry_id, plan["dst"])
    if target and dst_target and dst_target != target:
        sys.stderr.write(
            "❌ 已中止（只写了探路的一条）：目标字段「%s」指向的是另一张表。\n"
            "   源字段指向 %s，而目标字段指向 %s。\n"
            "   写进去的 ID 会成为指向虚无的引用——请在界面上核对新字段的关联目标。\n"
            % (args.dst, target, dst_target))
        return 3
    print("探路一条已写入并核对：目标字段指向 %s" % (dst_target or "（样本不足，反查不到）"))

    done, failed, not_submitted = 1, [], []
    for i, item in enumerate(todo[1:], 2):
        if sys.stderr.isatty():
            sys.stderr.write("\r回填 %d/%d …" % (i, len(todo)))
            sys.stderr.flush()
        try:
            ok, skipped, mism = client.update(app_id, entry_id, item["data_id"],
                                              {args.dst: item["ref"]})
        except JdyError as exc:
            failed.append((item["data_id"], str(exc)))
            continue
        if ok:
            done += 1
        not_submitted.extend(dict(s, data_id=item["data_id"]) for s in skipped)
        if mism:
            failed.append((item["data_id"], "写入后为空"))
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 30 + "\r")

    print("=" * 72)
    print("回填完成：%d/%d 行" % (done, len(todo)))
    report_skipped(not_submitted)
    for data_id, why in failed[:5]:
        print("❌ %s：%s" % (data_id, why))
    if not failed and not not_submitted:
        print("✅ 逐条回读核对通过")
    print("\n原来的「%s」字段没有动过。要不要在界面上隐藏它由你决定——本工具不删东西。"
          % args.src)
    return 0 if not failed else 3


if __name__ == "__main__":
    sys.exit(cli_main(main))
