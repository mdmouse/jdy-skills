#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行导入：分块幂等写入 → 回读比对 → 产出修复建议表。

文件名不叫 import.py：`import` 是 Python 关键字，那样命名的模块永远无法被
`import` 语句引用，测试和复用都会卡住。

默认 dry-run。--execute 才真正写入。
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
TRIGGERS = ("Excel 转简道云", "导入后数据不对", "批量上传附件")
import brand
from jdy_client import (ATTACHMENT_TYPES, MAX_BATCH, ask_yes, backup_path,
                        new_transaction_id, plan_code, report_skipped, scale_gate,
                        cli_main, confirm_threshold, JdyClient, JdyError, parse_tz)
from xlsx import write_sheet


def load_plan(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def attachment_columns(client, app_id, entry_id, rows):
    """这批行里用到了哪些附件列。没有就整条上传逻辑都不用走。"""
    by_label, _ = client.field_map(app_id, entry_id)
    used = {k for r in rows for k in r}
    return [label for label in used
            if by_label.get(label, {}).get("type") in ATTACHMENT_TYPES]


def upload_attachments(client, app_id, entry_id, rows, columns, transaction_id):
    """把行里的本地文件路径换成上传后的 key。返回 (新的行, 上传了几个文件)。

    只上传**这一批**要用的文件，且和写入请求共用同一个 transaction_id——
    凭证是绑事务的，换个事务号这些文件就作废了。
    """
    plan = []                      # [(第几行, 哪一列, 这个格子里的文件们)]
    out = [dict(r) for r in rows]
    for i, row in enumerate(out):
        for col in columns:
            paths = row.get(col)
            if not paths:
                continue
            plan.append((i, col, list(paths) if isinstance(paths, (list, tuple))
                         else [paths]))
    total = sum(len(p) for _, _, p in plan)
    if not total:
        return out, 0
    # 凭证一次请求给 100 组。原来每个格子调一次 upload_files、就取一次凭证——
    # 50 行就是 50 次请求、白取 5000 组用 50 组，限流 20/s 上很容易把自己卡住。
    pool = client.upload_pool(app_id, entry_id, transaction_id, total)
    at = 0
    for i, col, paths in plan:
        keys = []
        for path in paths:
            slot = pool[at]
            at += 1
            keys.append(client.upload_file(slot["url"], slot["token"], path))
        out[i][col] = keys
    return out, total

def verdict(verification, update_skipped, create_skipped, included_held,
            update_dropped=()):
    """这一趟到底能不能打那句 ✅。返回要打印的行。

    单拎成纯函数，是因为这个判断有过一次真机事故：`--include-held` 导的是
    预检**已经把坏格剔掉**的行——坏值根本没提交，编码期不报错，回读也"干净"，
    于是一路打到 ✅，而那几列确实是空的。屏幕上是一句"没有静默丢失"，
    落库的却是 None。

    它原来直接长在 main() 里，只能靠"扫源码里有没有 included_held 这个词"来测——
    那种测试把守卫取反照样全绿。搬出来之后可以真的调它。
    """
    if verification.get("aligned") is False and not verification.get("checked"):
        return ["  ⚠️ 未做逐字段核对：%s"
                % verification.get("reason", "提交行与返回 ID 对不上")]
    # 更新那半的回读结果**不在 verification 里**——它逐条回读，结果攒在
    # update_dropped 里。原来这句 ✅ 只看新增那半的 verification：更新真丢了字段
    # 照样打「没有静默丢失」。混合导入（几行新增 + 几行更新）时这是常态，
    # 而这是"回报成功但事实不符"最后一个还活着的入口。
    if update_dropped:
        return ["  ⚠️ 不能说「逐字段核对通过」：更新时有 %d 处字段提交了值、"
                "写入后为空（上面已逐项列出）" % len(update_dropped)]
    if not verification.get("clean"):
        return []
    blockers = []
    if update_skipped or create_skipped:
        blockers.append("有字段在编码期就没提交（回读核对看不到它们）")
    if included_held:
        blockers.append("有 --include-held 的行：坏格已被预检剔掉才导进去的，"
                        "那几列现在是空的")
    if blockers:
        return ["  ⚠️ 不能说「逐字段核对通过」：%s" % "；".join(blockers)]
    return ["  ✅ 逐字段核对通过，没有静默丢失"]


def creates_of(plan):
    """新增行统一成 [{values, row, who}]。

    旧版计划里 creates 是一个裸的 values 列表（行号和"这行是谁"当时就丢了），
    还能读，只是报问题时指不出是哪一行。
    """
    out = []
    for item in plan.get("creates", plan.get("rows", [])):
        if isinstance(item, dict) and "values" in item:
            out.append({"values": item["values"], "row": item.get("row"),
                        "who": item.get("who")})
        else:
            out.append({"values": item, "row": None, "who": None})
    return out


def where(row=None, who=None):
    """给一处问题标上"哪一行、这行是谁"。

    只说「第 6 行」不够——用户脑子里是名字不是行号，Agent 会自己去文件里数行
    来补充说明，然后数错（preflight 的 row_label() 里记着这次教训）。
    """
    if row is None:
        return "（行号未知，计划是旧版生成的）"
    return "第 %s 行%s" % (row, "  %s" % who if who else "")


def write_fix_sheet(path, plan, report, not_submitted=(), update_dropped=()):
    """修复建议表：一处问题一行，四个来源合成一张表。

    来源：预检拦下的逐格问题、整列导不进去的、**编码期未提交的字段**、
    回读发现被静默丢弃的。

    第三项原来漏了：控制台明明报了「未提交的字段 2 处」，这张表却写"无需
    修复建议表"。而这张表恰恰是拿去逐条修数据的那份东西——控制台会滚走，
    表不会。两处口径不一致时，人信的是表。
    """
    rows = []
    for wn in plan.get("warnings", []):   # 预检的提醒（不扣行，但要落到表上）
        rows.append({"来源": "预检提醒", "行号": wn.get("row", ""), "列": wn.get("column", ""),
                     "问题值": wn.get("value", ""), "问题": wn.get("detail", ""),
                     "建议": _advice(wn.get("kind"))})
    for issue in plan.get("issues", []):  # 预检拦下的逐格问题
        rows.append({"来源": "预检", "行号": issue.get("row", ""), "列": issue.get("column", ""),
                     "问题值": issue.get("value", ""), "问题": issue.get("detail", ""),
                     "建议": _advice(issue.get("kind"))})
    for col in plan.get("blocked_columns", []):
        rows.append({"来源": "预检", "行号": "全部", "列": col, "问题值": "",
                     "问题": "该字段类型无法通过 API 写入",
                     "建议": "在简道云界面手工补录，或改用可写字段承载"})
    for item in not_submitted or []:
        rows.append({"来源": "编码期", "行号": item.get("column", ""), "列": "",
                     "问题值": "", "问题": item.get("reason", ""),
                     "建议": _advice(item.get("kind"))})
    verification = (report or {}).get("verification") or {}
    for drop in verification.get("silently_dropped", []):
        rows.append({"来源": "回读比对", "行号": drop.get("data_id", ""), "列": drop.get("field", ""),
                     "问题值": json.dumps(drop.get("submitted"), ensure_ascii=False),
                     "问题": drop.get("reason", ""),
                     "建议": "检查该字段类型的写入格式；必要时在界面补录"})
    # 更新那半的回读结果不在 verification 里（它逐条回读，结果单独攒着）。
    # 漏掉它，这张表就会漏掉整整一类真实丢失——而这张表才是拿去逐条修数据的那份。
    for drop in update_dropped or []:
        rows.append({"来源": "回读比对（更新）",
                     "行号": drop.get("row") or drop.get("data_id", ""),
                     "列": drop.get("field", ""),
                     "问题值": json.dumps(drop.get("submitted"), ensure_ascii=False),
                     "问题": drop.get("reason", ""),
                     "建议": "检查该字段类型的写入格式；必要时在界面补录"})
    if not rows:
        return None
    count = len(rows)          # 署名行不是"一条待修复"，条数在加它之前就定下来
    if brand.enabled():
        # 空一行再署名，且只占第一列：这张表是拿去逐条改数据的，
        # 署名放在所有问题之后、不占任何一个内容列，不影响从上往下读。
        rows.append({})
        rows.append({"来源": brand.LINE})
    write_sheet(path, ["来源", "行号", "列", "问题值", "问题", "建议"], rows, "修复建议")
    return count


def _advice(kind):
    return {
        "bad_value": "按字段类型修正该单元格的值",
        "user_ambiguous": "存在同名成员，请把该格改成确切的 username",
        "user_unresolved": "该姓名在本表历史数据里查不到，请填 username",
        "lookup_missing": "关联的目标记录不存在——请核对该 data_id，或先把目标记录建好",
        "lookup_unverified": "推断不出关联字段指向哪张表，本次未校验引用；请人工确认",
        "unknown_option": "核对拼写；确认是新增选项就忽略——接口不校验选项，写错了会原样存进去",
    }.get(kind, "核对后修正")


def confirm_code(app_id, entry_id, creates, updates):
    """本次计划的确认码。dry-run 与真正执行必须算出同一个码，
    所以只能有这一处实现——两边各写一遍迟早会漂移。"""
    return plan_code({"app": app_id, "entry": entry_id,
                      "creates": len(creates), "updates": len(updates),
                      "sample": sorted(k for r in creates[:50] for k in r)[:40]})


def main():
    ap = argparse.ArgumentParser(description="执行简道云导入（默认 dry-run）")
    ap.add_argument("plan", help="preflight.py --plan 产出的导入计划 JSON")
    ap.add_argument("--execute", action="store_true", help="真正写入。不加则只做 dry-run")
    ap.add_argument("--no-backup", action="store_true", help="跳过写前备份（不建议）")
    ap.add_argument("--fix-sheet", help="修复建议表输出路径（xlsx）")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（非交互环境用）")
    ap.add_argument("--confirm-code", help="大批量写入时的计划确认码，见提示")
    ap.add_argument("--confirm-threshold", type=int, default=None,
                    help="内部安全默认值：改动超过多少条要二次确认（默认 50，"
                         "**只能往小调**）。**不要拿这个参数去问用户**")
    ap.add_argument("--include-held", action="store_true",
                    help="连同「扣下待修」的行一起写入（这些行会缺字段，默认不写）")
    args = ap.parse_args()
    threshold = confirm_threshold(args.confirm_threshold)

    plan = load_plan(args.plan)
    # creates/updates 由 _id 列分流；rows 是旧版计划的字段名，保持能读
    creates = creates_of(plan)
    updates = list(plan.get("updates", []))
    held = plan.get("held_rows", [])
    # --include-held 导的是预检**已经把坏格剔掉**的行：坏值不会被提交，
    # 所以编码期不报错、回读也"干净"——于是它会一路打到 ✅，而那几列确实是空的。
    # 缺的正是预检警告过的那一格，必须单独说。
    included_held = list(held) if args.include_held else []
    if args.include_held:
        for h in held:
            (updates.append({"data_id": h["data_id"], "values": h["data"],
                             "row": h.get("row"), "who": h.get("who")})
             if h.get("data_id") else
             creates.append({"values": h["data"], "row": h.get("row"),
                             "who": h.get("who")}))
    rows = [c["values"] for c in creates]
    if not rows and not updates:
        print("导入计划里没有可写入的行。")
        return 0

    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2

    app_id, entry_id = plan["app_id"], plan["entry_id"]

    if not args.execute:
        report = client.batch_create(app_id, entry_id, rows, dry_run=True,
                                     tz=parse_tz(plan.get("tz")))
        print("=" * 66)
        print("DRY-RUN　未写入任何数据")
        print("=" * 66)
        if updates:
            print("更新已有记录    %d 行（按 _id 定位，逐条更新，不产生重复行）" % len(updates))
        print("新增记录        %d 行，分 %d 批（每批 ≤100）"
              % (len(report["submitted_rows"]), report["chunks"]))
        if report["empty_rows"]:
            # 一个可写字段都没有的行不会被提交。原来它只是"从提交序列里消失"，
            # 计划里报 N 行、实际进去 N-k 行，差额没人说。
            print("空行不提交      %d 行（第 %s 行：一个可写字段都没有）"
                  % (len(report["empty_rows"]),
                     "、".join(str(i + 1) for i in report["empty_rows"][:10])))
        if held and not args.include_held:
            print("扣下待修        %d 行（第 %s 行）—— 修好后重跑预检再导"
                  % (len(held), "、".join(str(h["row"]) for h in held[:10])))
        print("预计耗时        %.1f 秒" % report["estimated_seconds"])
        print("源数据时区      %s" % (plan.get("tz") or "+08:00"))
        attach_cols = attachment_columns(client, app_id, entry_id, rows) if rows else []
        if attach_cols:
            files = sum(len(r.get(c) or []) for r in rows for c in attach_cols)
            print("附件            %d 个文件，来自列 %s（执行时上传）"
                  % (files, "、".join(attach_cols)))
            if len(rows) > MAX_BATCH:
                print("  ⚠️ 带附件一次最多 %d 行（附件绑定在一个事务号上），"
                      "现在 %d 行——请把 Excel 拆开分次导入" % (MAX_BATCH, len(rows)))
        if report["unwritable_columns"]:
            print("导不进去的列    %s" % "、".join(report["unwritable_columns"]))
        if report.get("bad_value_columns"):
            print("值有问题的列    %s" % "、".join(report["bad_value_columns"]))
        if report.get("unknown_columns"):
            print("表单里没有的列  %s" % "、".join(report["unknown_columns"]))
        if plan.get("issues"):
            print("预检未解决问题  %d 处" % len(plan["issues"]))
        scale = len(rows) + len(updates)
        if scale > threshold:
            # 确认码必须在 dry-run 阶段就给出。原来只有「先跑一次 --execute 被拒」
            # 才拿得到，等于训练调用方在取得同意之前先试着写一次——
            # 安全机制不该逼人先做一次不安全的动作。
            print("\n本次改动 %d 条，属于大批量。请先跟用户说：" % scale)
            print("    「这次要往简道云写入 %d 条数据，确认执行吗？」" % scale)
            print("得到同意后执行：  --execute --yes --confirm-code %s"
                  % confirm_code(app_id, entry_id, rows, updates))
            print("（码由计划内容算出，重跑 preflight 后会变。不要向用户提这个码）")
        else:
            print("\n确认无误后加 --execute 执行。")
        if args.fix_sheet:
            # dry-run 的报告里已经有编码期问题了，一并写进去——
            # 这张表的用法就是"导之前照着它把数据修好"，等导完再给就晚了
            n = write_fix_sheet(args.fix_sheet, plan, None,
                                not_submitted=[
                                    dict(s, column="%s　「%s」"
                                         % (where(creates[s["row"]]["row"],
                                                  creates[s["row"]]["who"]), s["column"]))
                                    for s in (report.get("not_submitted") or [])])
            print("修复建议表：%s（%s 条）" % (args.fix_sheet, n) if n else "无需修复建议表")
        return 0

    # 规模闸门：和 jdy-sync 用同一套。批量导入才是最容易一次写坏几百条的地方，
    # 而它原来一道闸门都没有——更保守的同步反倒有。安全措施不能挑地方放。
    scale = len(rows) + len(updates)
    code = confirm_code(app_id, entry_id, rows, updates)
    gated = scale_gate(scale, code, args.confirm_code, threshold,
                       ["新增 %d 行" % len(rows), "更新 %d 行" % len(updates)])
    if gated is not None:
        return gated

    if not args.yes:
        # 判"能不能问"交给内核的 ask_yes：Windows 的 NUL 设备 isatty() 谎报 True，
        # 自己写 `if isatty(): input()` 会在那里 EOFError 崩掉（退出码 1 + traceback），
        # 下面这段拒绝文案一个字都说不出来。
        answered = ask_yes("即将向 %s / %s 新增 %d 行、更新 %d 行。\n确认？输入 yes 继续："
                           % (app_id, entry_id, len(rows), len(updates)))
        if answered is None:
            # Agent 平台里 stdin 从来不是 tty。若在这里放行，二次确认就等于不存在——
            # "问不了"不能等同于"默认同意"。改为拒绝执行，把确认责任交回调用方。
            sys.stderr.write(
                "拒绝写入：当前是非交互环境，无法向用户当面确认。\n"
                "请先把将要写入的内容（新增 %d 行、更新 %d 行 → %s/%s）复述给用户并\n"
                "取得明确同意，再加 --yes 重新执行。\n"
                % (len(rows), len(updates), app_id, entry_id))
            return 4
        if not answered:
            print("已取消")
            return 0

    if not args.no_backup:
        path = backup_path(os.path.dirname(os.path.abspath(args.plan)), entry_id)
        try:
            n = client.backup(app_id, entry_id, path)
            print("写前备份：%s（%d 行）" % (path, n))
        except (JdyError, OSError) as exc:
            sys.stderr.write("备份失败，已中止——没有备份不动数据。%s\n" % exc)
            return 3

    def progress(done, total):
        if sys.stderr.isatty():
            sys.stderr.write("\r写入 %d/%d …" % (done, total))
            sys.stderr.flush()

    tz = parse_tz(plan.get("tz"))

    # 附件列要按**新增 + 更新两边**一起算。原来只看 rows（新增），于是更新路径上
    # 附件从来没被上传过：本地路径 `/Users/x/合同.pdf` 被当成上传后的 key 直接提交。
    # 而本技能文档自己推荐的就是"导出 → 改 → 导回"，那条路走的正是更新——
    # 用户照着文档做，附件列静默作废。
    update_values = [u["values"] for u in updates]
    attach_cols = (attachment_columns(client, app_id, entry_id, rows + update_values)
                   if (rows or update_values) else [])
    uploaded = 0

    # 先更新再新增：更新失败不该拦住新增，且更新是幂等的，重跑无害。
    updated, update_failures, update_dropped, update_skipped = 0, [], [], []
    for i, item in enumerate(updates, 1):
        if sys.stderr.isatty():
            sys.stderr.write("\r更新 %d/%d …" % (i, len(updates)))
            sys.stderr.flush()
        values, row_txn = item["values"], None
        try:
            if attach_cols and any(values.get(c) for c in attach_cols):
                # 凭证绑事务，写入请求必须带**同一个**事务号。逐行各用一个号：
                # 一个号跨多次写入会不会互相覆盖没有实测过，而这里本来就是逐条写。
                row_txn = new_transaction_id()
                (values,), n = upload_attachments(client, app_id, entry_id, [values],
                                                  attach_cols, row_txn)
                uploaded += n
            ok, skipped, mismatches = client.update(app_id, entry_id, item["data_id"],
                                                    values, tz=tz,
                                                    transaction_id=row_txn)
        except (JdyError, OSError) as exc:
            update_failures.append((item.get("row"), item["data_id"], str(exc)))
            continue
        if ok:
            updated += 1
        # 编码失败的字段根本没提交，回读核对看不到它们——必须单独呈现
        update_skipped.extend(
            dict(s, column="%s　「%s」" % (where(item.get("row"), item.get("who")),
                                          s["column"]))
            for s in skipped)
        # 带上身份：报了"哪个字段丢了"却指不出哪一行，拿到修复表也没法逐条改。
        update_dropped.extend(
            dict(m, data_id=item["data_id"],
                 row=where(item.get("row"), item.get("who")))
            for m in mismatches)
    if sys.stderr.isatty() and updates:
        sys.stderr.write("\r" + " " * 30 + "\r")

    # 附件：先把本地文件传上去换成 key，再带着**同一个** transaction_id 写。
    # 凭证绑事务，换个号这些文件就作废；而分块时每块要用不同的号（相同的会互相
    # 覆盖），所以带附件一次最多 MAX_BATCH 行——超了就直说，别默默拆。
    txn = None
    if attach_cols and rows:
        if len(rows) > MAX_BATCH:
            sys.stderr.write(
                "这批有附件列（%s），而附件绑定在一个事务号上——一次最多 %d 行，"
                "现在是 %d 行。\n请把 Excel 拆开分次导入。\n"
                % ("、".join(attach_cols), MAX_BATCH, len(rows)))
            return 2
        txn = new_transaction_id()
        try:
            rows, n = upload_attachments(client, app_id, entry_id, rows,
                                         attach_cols, txn)
            uploaded += n          # 更新路径也传了文件，别把它的计数冲掉
        except (JdyError, OSError) as exc:
            sys.stderr.write("附件上传失败，已中止（还没写任何数据）：%s\n" % exc)
            return 3
    if uploaded:
        print("附件已上传        %d 个文件（%s）" % (uploaded, "、".join(attach_cols)))

    report = (client.batch_create(app_id, entry_id, rows, dry_run=False, verify=True,
                                  tz=tz, progress=progress, transaction_id=txn)
              if rows else {"total_rows": 0, "success_count": 0, "created_ids": [],
                            "verification": {}})
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 30 + "\r")

    # 新增路径的"编码期未提交字段"——与更新路径同一个变量、同一个渲染函数，
    # 身份也要拼成一样的形状：报了"哪一列坏了"却指不出哪一行，等于没报。
    create_skipped = [
        dict(s, column="%s　「%s」" % (where(creates[s["row"]]["row"],
                                            creates[s["row"]]["who"]), s["column"]))
        for s in (report.get("not_submitted") or [])]
    v = report.get("verification") or {}
    print("=" * 66)
    print("导入完成")
    print("=" * 66)
    if included_held:
        print("扣下待修但仍导  %d 行（--include-held）——这些行**缺字段**，"
              "缺的就是预检报过的那几格：" % len(included_held))
        for h in included_held[:5]:
            miss = "、".join(sorted({i["column"] for i in h.get("issues", [])}))
            print("      %s　缺「%s」" % (where(h.get("row"), h.get("who")), miss))
        if len(included_held) > 5:
            print("      … 另有 %d 行" % (len(included_held) - 5))
        print("**不要说这几行导完整了**——它们是带着已知缺口写进去的")
    if updates:
        print("更新            %d/%d 条" % (updated, len(updates)))
        if update_dropped:
            print("  ⚠️ 更新后为空  %d 处字段（被简道云静默丢弃）" % len(update_dropped))
            for d in update_dropped[:5]:
                print("      %s　「%s」(%s)"
                      % (d.get("row") or d.get("data_id", ""), d.get("field"),
                         d.get("type")))
        report_skipped(update_skipped)
        for row_num, did, err in update_failures[:5]:
            print("  ❌ 第 %s 行 %s：%s" % (row_num, did, err))
    if rows:
        if report.get("empty_rows"):
            print("空行未提交      %d 行（一个可写字段都没有）" % len(report["empty_rows"]))
        print("新增提交        %d 行" % len(report.get("submitted_rows", [])))
        report_skipped(create_skipped)
        print("接口回报成功    %d 条" % report.get("success_count", 0))
        print("拿到数据 ID     %d 个" % len(report["created_ids"]))
    elif updates:
        # 纯更新时没有新增统计。原来照打「接口回报成功 0 条 / 拿到数据 ID 0 个」，
        # 读起来像全军覆没，实测中 Agent 得专门去确认这不是失败。
        print("本次没有新增记录（全部走更新）")
    if v:
        print("回读核对        %d 条" % v.get("checked", 0))
        if v.get("missing_rows"):
            print("  ⚠️ 回读缺失    %d 条" % len(v["missing_rows"]))
        dropped = v.get("silently_dropped", [])
        if dropped:
            print("  ⚠️ 静默丢字段  %d 处（提交了值但写入后为空）" % len(dropped))
            for d in dropped[:8]:
                print("      「%s」(%s) 提交 %r" % (d["field"], d["type"],
                                                  json.dumps(d["submitted"], ensure_ascii=False)[:40]))
            if len(dropped) > 8:
                print("      … 另有 %d 处" % (len(dropped) - 8))
        if v.get("unverified_rows"):
            print("  ⚠️ 未核对的行  %d 行（所在批次返回的 ID 数与提交数对不上，"
                  "谁对谁不可知）" % v["unverified_rows"])
        for line in verdict(v, update_skipped, create_skipped, included_held,
                            update_dropped):
            print(line)

    if not v and updates:
        # 纯更新的批次没有 verification，原来这一趟一句结论都不打
        for line in verdict({"clean": True}, update_skipped, create_skipped,
                            included_held, update_dropped):
            print(line)

    if args.fix_sheet:
        n = write_fix_sheet(args.fix_sheet, plan, report,
                            not_submitted=create_skipped + update_skipped,
                            update_dropped=update_dropped)
        print("\n修复建议表：%s（%s 条）" % (args.fix_sheet, n) if n else "\n无需修复建议表")
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
