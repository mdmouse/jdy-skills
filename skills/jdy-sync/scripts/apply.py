#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行同步计划。默认 dry-run；写前备份目标表；写后回读核对。"""
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
TRIGGERS = ("数据搬迁", "增量同步", "把某应用的数据搬到另一个",
            "子表单同步", "附件一起搬过去")
from jdy_client import (ask_yes, backup_path, cli_main, confirm_threshold,
                        new_transaction_id, pad, report_skipped, scale_gate,
                        JdyClient, JdyError, MAX_BATCH)
from miniyaml import parse as parse_yaml
from sync import (IdMap, SyncError, load_config, plan_fingerprint, plan_table,
                  topo_sort, verify_complex)


def copy_row_attachments(client, app_id, entry_id, values, attach_fields, txn):
    """把这一行的附件真的搬过去：下载 → 重传 → 值换成上传拿到的 key 列表。

    附件是全项目唯一一个"搬运必须真的过一遍本地磁盘"的字段类型：读回来只有带
    过期戳的 url，写入要的是重新上传得到的 key，而 key 只对**同一个 transaction_id**
    的写入请求有效（官方硬约束，1 小时）——所以上传和提交必须挨着做。

    搬不动就抛。调用方按 D7 把**整行扣下**：宁可这一行不写，也不要写一条
    "别的字段都在、附件那列是空的"的记录——那种半成品最难发现。
    """
    out = dict(values)
    for label in attach_fields:
        raw = values.get(label)
        if not raw:
            continue
        want = [v for v in raw if isinstance(v, dict) and v.get("url")]
        if not want:
            continue
        keys = client.copy_attachments(want, app_id, entry_id, txn)
        if len(keys) != len(want):
            raise JdyError("ATTACH", "「%s」有 %d 个附件，只搬成功 %d 个"
                           % (label, len(want), len(keys)))
        out[label] = keys
    return out


def prepare_rows(client, app_id, entry_id, items, attach_fields, txn):
    """把一批行的附件搬过去。返回 (可提交的, 被扣下的)。

    items 是 [(标记, values)]，标记原样带回来（新增用行号、更新用那条计划）。

    D7：**这一行的附件搬不动，就整行不写**。不是"跳过这一列继续写"——
    那会留下一条"别的字段都在、附件列是空的"记录，看起来完全正常，
    而它和源端已经不是同一条数据了。同 excel-bridge「下载失败不装作没附件」。
    """
    ready, held = [], []
    for tag, values in items:
        if not attach_fields:
            ready.append((tag, values))
            continue
        try:
            ready.append((tag, copy_row_attachments(client, app_id, entry_id, values,
                                                    attach_fields, txn)))
        except (JdyError, OSError) as exc:
            held.append((tag, str(exc)))
    return ready, held


def attachment_batches(pending, attach_fields):
    """分批：带附件时每批 ≤100 行，**每批一个自己的事务号**（D5/D6）。

    附件的 key 绑在 transaction_id 上，而分块共用一个号会互相覆盖（write-behavior 三）——
    第二批之后的附件会全部失效，接口还照样回报成功。

    sync **没有** excel-bridge 那条"带附件总共最多 100 行"的限制：
    两边的幂等模型不一样。excel-bridge 靠事务号幂等，所以一次导入只能一个号；
    sync 靠业务键比对幂等（重跑时已一致就跳过、根本不重传），
    所以每批可以各用各的号，行数不设上限。

    没有附件时不分批，一次交给内核（它自己会按 100 分块），行为和以前一样。
    """
    if not pending:
        return []
    if not attach_fields:
        return [(None, pending)]
    return [(new_transaction_id(), pending[i:i + MAX_BATCH])
            for i in range(0, len(pending), MAX_BATCH)]


def load_plan_snapshot(path, config_path, cfg):
    """读 plan.py --json-out 存下的计划。返回 (plans, 已生成多少分钟)。

    复用快照有两个好处：省掉一轮全量重拉（一轮同步原本要拉 3~4 遍），
    以及**执行的就是用户看过的那一份**——重新规划会让刚拿到的确认码失效，
    也可能在用户点头之后悄悄多出几条。

    代价是它可能过时，所以这里把三件事钉死：配置得对得上、alias 得存在、
    年龄要如实报出来（超龄由调用方拒绝）。
    """
    with open(path, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    plans = blob.get("plans")
    if not isinstance(plans, list) or not plans:
        raise SyncError("%s 里没有 plans，不像是 plan.py --json-out 的产物" % path)
    saved_cfg = blob.get("config")
    if saved_cfg and os.path.abspath(saved_cfg) != os.path.abspath(config_path):
        raise SyncError("这份计划是照 %s 算的，和当前配置 %s 不是同一份——\n"
                        "拿 A 的计划去执行 B 会写错表。请重新生成。"
                        % (saved_cfg, config_path))
    known = {t["alias"] for t in cfg["tables"]}
    unknown = sorted({p.get("alias") for p in plans} - known)
    if unknown:
        raise SyncError("计划里的 alias 在配置里不存在：%s —— 配置改过了，请重新生成计划"
                        % "、".join(str(u) for u in unknown))
    age = None
    made = blob.get("generated_at")
    if made:
        try:
            then = datetime.datetime.fromisoformat(str(made).replace("Z", "+00:00"))
            if then.tzinfo is None:
                then = then.replace(tzinfo=datetime.timezone.utc)
            age = int((datetime.datetime.now(datetime.timezone.utc)
                       - then).total_seconds() // 60)
        except ValueError:
            age = None
    return plans, age


def main():
    ap = argparse.ArgumentParser(description="执行同步（默认 dry-run）")
    ap.add_argument("config")
    ap.add_argument("--execute", action="store_true", help="真正写入；不加只重算一遍计划")
    ap.add_argument("--yes", action="store_true", help="已向用户取得确认（非交互环境必须给）")
    ap.add_argument("--no-backup", action="store_true", help="跳过目标表备份（不建议）")
    ap.add_argument("--only", action="append", help="只同步指定 alias，可重复")
    ap.add_argument("--plan-json",
                    help="plan.py --json-out 的产物。给了就照它执行，不再重新拉一遍源表"
                         "（一轮同步原本要全量拉 3~4 遍）")
    ap.add_argument("--max-plan-age", type=int, default=60,
                    help="快照最多允许多旧（分钟，默认 60）。超过就拒绝执行——"
                         "源数据可能已经变了")
    ap.add_argument("--confirm-code", help="大批量写入时的计划确认码，见提示")
    ap.add_argument("--confirm-threshold", type=int, default=None,
                    help="内部安全默认值：改动超过多少条要二次确认（默认 50，"
                         "**只能往小调**）。"
                         "**不要拿这个参数去问用户**——用户只关心同步多少条，"
                         "那是配置里的 limit")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config, parse_yaml)
        client = JdyClient()
        tables = topo_sort(cfg["tables"])
    except (SyncError, JdyError, OSError) as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    if args.only:
        tables = [t for t in tables if t["alias"] in set(args.only)]
        if not tables:
            sys.stderr.write("--only 没有匹配到任何表\n")
            return 2

    id_map = IdMap(cfg["_id_map_path"])
    if args.plan_json:
        try:
            plans, age = load_plan_snapshot(args.plan_json, args.config, cfg)
        except (SyncError, OSError, ValueError) as exc:
            sys.stderr.write("%s\n" % exc)
            return 2
        if age is None:
            # "不知道多旧"必须当成"太旧"，不能当成"刚生成"。
            # 原来的判断是 `if age is not None and age > 上限`，于是缺时间戳的快照
            # 无条件放行，屏幕上还打一个「? 分钟前」——门槛静默失效，
            # 而这正是老版本 plan.py 产出的快照的样子。
            sys.stderr.write(
                "这份计划里没有生成时间，无从判断它有多旧——拒绝执行。\n"
                "（老版本 plan.py 存的快照就是这样。）重新跑一次 "
                "plan.py --json-out 再执行。\n")
            return 2
        if age > args.max_plan_age:
            sys.stderr.write(
                "这份计划是 %d 分钟前生成的（上限 %d 分钟）——源数据可能已经变了。\n"
                "重新跑一次 plan.py --json-out 再执行。\n" % (age, args.max_plan_age))
            return 2
        print("照用已保存的计划：%s（%d 分钟前生成，未重新拉取源表）"
              % (os.path.basename(args.plan_json), age))
        # --only 也要过滤计划，不能只过滤 tables：下面是按位置 zip 的，
        # 两个列表长度一旦不同就会把 A 表的计划写进 B 表。
        keep = {t["alias"] for t in tables}
        plans = [p for p in plans if p["alias"] in keep]
        tables = [t for t in tables if t["alias"] in {p["alias"] for p in plans}]
        if not plans:
            sys.stderr.write("这份计划里没有 --only 指定的表。\n")
            return 2
        # 快照跳过了 plan_table，也就跳过了它"把匹配上的记录登记进 id_map"的副作用；
        # 但 plan.py 生成快照时已经 id_map.save() 落过盘了，这里读到的就是那一份。
        tables.sort(key=lambda t: [p["alias"] for p in plans].index(t["alias"]))
    else:
        try:
            plans = [plan_table(client, cfg, t, id_map) for t in tables]
        except (SyncError, JdyError) as exc:
            sys.stderr.write("%s\n" % exc)
            return 2

    total_c = sum(len(p["creates"]) for p in plans)
    total_u = sum(len(p["updates"]) for p in plans)
    print("将要写入：新增 %d　更新 %d（顺序 %s）"
          % (total_c, total_u, " → ".join(p["alias"] for p in plans)))
    if not total_c and not total_u:
        print("没有需要写入的变更。")
        return 0

    if not args.execute:
        print("\nDRY-RUN，未写入。确认后加 --execute --yes。"
              "详细差异用 plan.py 查看。")
        return 0

    # 规模闸门：小批量给 --yes 即可，大批量必须带上本次计划的指纹。
    # 数据变了指纹就变，旧码失效——确保确认的与执行的是同一个计划。
    # 走内核 scale_gate：闸门原来在这里手写了一份，三条写入链路的文案与
    # 确认码大小写各不相同；而且阈值直传，`--confirm-threshold 999999` 就把它拆了。
    scale = total_c + total_u
    gated = scale_gate(scale, plan_fingerprint(cfg, plans), args.confirm_code,
                       confirm_threshold(args.confirm_threshold),
                       ["%s 新增 %d　更新 %d" % (pad(p["alias"], 10), len(p["creates"]),
                                                len(p["updates"])) for p in plans])
    if gated is not None:
        return gated

    if not args.yes:
        answered = ask_yes("确认写入？输入 yes：")
        if answered is None:            # 不是 tty，或 Windows 的 NUL 让 input() EOF
            sys.stderr.write(
                "拒绝写入：当前是非交互环境，无法当面向用户确认。\n"
                "同步会改动目标应用的数据——请先把上面的计划复述给用户、\n"
                "取得明确同意后，再加 --yes 重新执行。\n")
            return 4
        if not answered:
            print("已取消")
            return 0

    dst_app = cfg["target"]["app"]
    if not args.no_backup:
        base = cfg["_base_dir"]        # 与 id_map 同一套解析规则
        for t in tables:
            # 文件名按 entry_id 排，与清洗/导入一致——三种命名就等于没有恢复入口
            path = backup_path(base, t["target_entry"])
            try:
                n = client.backup(dst_app, t["target_entry"], path)
                print("备份 %s %d 行 → %s" % (pad(t["alias"], 10), n, os.path.basename(path)))
            except (JdyError, OSError) as exc:
                sys.stderr.write("备份失败，已中止——没有备份不动数据。%s\n" % exc)
                return 3

    summary = []
    by_alias = {t["alias"]: t for t in tables}
    for plan in plans:
        alias = plan["alias"]
        table = by_alias[alias]      # 按 alias 取，不按位置——位置对齐太容易悄悄错位
        created = updated = 0
        dropped, failed, not_submitted, held, checks = [], [], [], [], []
        # 哪些列的值是"附件原值、要先把文件搬过去"。名单来自计划——
        # 照快照执行时不重算映射，这份名单必须跟着快照走，否则附件那列会被
        # 原样提交（读回来的 url 里没有 key），静默存空。
        attach_fields = plan.get("attachment_fields") or []
        complex_fields = attach_fields + (plan.get("subform_fields") or [])

        # 新增：分块写，拿到目标 id 立刻记进映射表——中断也能续跑。
        # **带附件时每批 ≤100 行、每批一个自己的事务号**（D5/D6）：key 绑在事务号上，
        # 而分块共用一个号会互相覆盖。sync 没有 excel-bridge 那条"带附件总共最多
        # 100 行"的限制——那边靠事务号幂等，这边靠业务键比对幂等（重跑已一致就跳过），
        # 所以每批可以各用各的号。
        pending = list(enumerate(c["values"] for c in plan["creates"]))
        for txn, batch in attachment_batches(pending, attach_fields):
            ready, dropped_rows = prepare_rows(client, dst_app, table["target_entry"],
                                               batch, attach_fields, txn)
            held.extend((plan["creates"][idx]["key"], why) for idx, why in dropped_rows)
            index = [idx for idx, _v in ready]
            rows = [v for _idx, v in ready]
            if not rows:
                continue
            report = client.batch_create(dst_app, table["target_entry"], rows,
                                         dry_run=False, verify=True, transaction_id=txn)
            ids = report.get("created_ids", [])
            # 按**提交序**回映，不按 plan["creates"] 的原序：
            # 编不出任何字段的行不会被提交（内核在编码阶段就剔除了），
            # 两个序列一旦长度不同，逐位 zip 就会把 A 的目标 ID 记到 B 名下——
            # 下次同步照着这份映射去更新，就更新错了记录，而两次都"成功"。
            submitted = report.get("submitted_rows", list(range(len(rows))))
            if len(ids) != len(submitted):
                # 接口只认了一部分，谁对谁不可知。宁可不记映射（下次按业务键重新匹配），
                # 也不能记错——记错的映射比没有映射更难发现。
                print("   ⚠️ 接口返回 %d 个 ID 但提交了 %d 行，对应关系不可知——"
                      "本次不写 ID 映射，下次同步会按业务键重新匹配"
                      % (len(ids), len(submitted)))
            else:
                for row_idx, target_id in zip(submitted, ids):
                    create = plan["creates"][index[row_idx]]
                    id_map.put(alias, create["source_id"], target_id)
                    if complex_fields:
                        checks.append((target_id, create["key"],
                                       {k: v for k, v in create["values"].items()
                                        if k in complex_fields}))
            created += len(ids)
            id_map.save()
            # 新增路径同样有"编码期就没提交"的字段。回读核对只看提交过的，
            # 不收这一份就会在漏了一整列的情况下报"没有静默丢失"。
            # 拼上业务键：同步没有"行号"，但有业务键，那才是人认得出的身份
            not_submitted.extend(
                dict(s, column="「%s」　%s"
                     % (plan["creates"][index[s["row"]]]["key"], s["column"]))
                for s in (report.get("not_submitted") or []))
            v = report.get("verification") or {}
            dropped.extend(v.get("silently_dropped", []))
            if v.get("unverified_rows"):
                print("   ⚠️ %d 行未做逐字段核对（所在批次返回的 ID 数与提交数对不上）"
                      % v["unverified_rows"])

        for item in plan["updates"]:
            # 更新也走**同一条**附件搬运路径。教训在案：附件上传当初只挂在新增路径上，
            # 更新路径把本地路径当 key 提交、静默存空，而文档推荐的正是"导出→改→导回"。
            has_att = attach_fields and any(item["values"].get(l) for l in attach_fields)
            txn = new_transaction_id() if has_att else None
            ready, dropped_rows = prepare_rows(
                client, dst_app, table["target_entry"], [(item, item["values"])],
                attach_fields if has_att else [], txn)
            for held_item, why in dropped_rows:
                held.append((held_item["key"], why))           # D7：整行扣下
            if not ready:
                continue
            values = ready[0][1]
            try:
                ok, skipped, mismatches = client.update(
                    dst_app, table["target_entry"], item["target_id"], values,
                    transaction_id=txn)
            except JdyError as exc:
                failed.append((item["key"], str(exc)))
                continue
            if ok:
                updated += 1
                id_map.put(alias, item["source_id"], item["target_id"])
                if complex_fields:
                    checks.append((item["target_id"], item["key"],
                                   {k: v for k, v in item["values"].items()
                                    if k in complex_fields}))
            # 编码失败的字段根本没提交，回读核对看不到它们——必须单独呈现
            not_submitted.extend(skipped)
            dropped.extend(mismatches)
        id_map.save()

        # 子表单与附件要**按自己的口径**再核对一遍：内核那一关只问"写进去是不是空的"，
        # 而内层映射错了、附件搬串了，字段都不是空的——那一关照样过。
        # 注意这里比的是**源端期望值**（附件比的是搬之前的 name/size），不是刚提交的 key。
        mism = []
        if checks:
            try:
                mism = verify_complex(client, dst_app, table["target_entry"], checks,
                                      client.field_map(dst_app, table["target_entry"])[0])
                # 核过了就说一声核了多少行。不说的话，"没有报错"和"根本没核"
                # 在屏幕上长得一模一样——而这一关是否真的挂着，纯函数测不到，
                # 只有 acceptance 能守（它就靠这行）。
                print("   ✅ 子表单/附件回读核对 %d 行（按内层显示名 / (name,size) 比）"
                      % len(checks))
            except JdyError as exc:
                print("   ⚠️ 子表单/附件的回读核对没做成：%s" % exc)

        summary.append((alias, created, updated, dropped, failed, not_submitted,
                        held, mism))
        print("\n▌%s　新增 %d　更新 %d" % (alias, created, updated))
        if dropped:
            print("   ⚠️ 写入后为空的字段 %d 处（被简道云静默丢弃）：" % len(dropped))
            for d in dropped[:5]:
                print("      「%s」(%s) 提交 %s"
                      % (d.get("field"), d.get("type"),
                         json.dumps(d.get("submitted"), ensure_ascii=False)[:40]))
        if mism:
            print("   ⚠️ 子表单/附件回读对不上 %d 处（内核的空值核对看不见这类）：" % len(mism))
            for m in mism[:5]:
                print("      「%s」%s\n         期望 %s\n         实际 %s"
                      % (m["key"], m["field"], m["expected"], m["actual"]))
        if held:
            print("   ⛔ 附件搬不动、**整行扣下** %d 条（没有半写）：" % len(held))
            for key, why in held[:5]:
                print("      %s：%s" % (key, why))
        report_skipped(not_submitted)
        for key, err in failed:
            print("   ❌ %s：%s" % (key, err))

    print("\n" + "-" * 68)
    tc = sum(s[1] for s in summary)
    tu = sum(s[2] for s in summary)
    td = sum(len(s[3]) for s in summary)
    tf = sum(len(s[4]) for s in summary)
    tn = sum(len(s[5]) for s in summary)
    th = sum(len(s[6]) for s in summary)
    tm = sum(len(s[7]) for s in summary)
    print("同步完成：新增 %d　更新 %d　静默丢字段 %d 处　未提交字段 %d 处　失败 %d 条"
          % (tc, tu, td, tn, tf))
    if th or tm:
        print("　　　　　附件搬不动整行扣下 %d 条　子表单/附件回读对不上 %d 处" % (th, tm))
    if id_map.readonly:
        print("⚠️ ID 映射表写入失败（目录不可写）——**下次同步会把已同步的记录当成新增重复写一遍**，"
              "请把 id_map 指到可写位置后重跑")
    else:
        print("ID 映射表：%s" % id_map.path)
    if td or tn or tm:
        print("⚠️ 有字段没写进去——**不要告诉用户已完整同步**，请如实说明是哪些字段")
    if th:
        print("⚠️ 有 %d 条记录因为附件搬不动被整行扣下，**这几条根本没写**——"
              "请把业务键报给用户，别说成「已同步」" % th)
    # 扣下的行和对不上的字段都算没做成：退 0 会让调用方（Agent）照打"同步完成"
    return 0 if not (tf or th or tm) else 3


if __name__ == "__main__":
    sys.exit(cli_main(main))
