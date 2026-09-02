#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按报表定义生成周报/月报。只读。

流程：读 YAML 配置 → 按周期做服务端筛选拉数 → 本地聚合 → 渲染 Markdown。
本地聚合的好处：不占简道云 AI 点数、不受界面 200 条限制、口径完全可控。
"""
import argparse
import datetime
import json
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("数据汇总", "这周的数据怎么样", "环比", "Top 排行", "分组统计")
import brand
from aggregate import (ConfigError, apply_derived, build_section,
                       build_trend, period_filter, resolve_period)
from jdy_client import cli_main, parse_tz, JdyClient, JdyError, display_value
from miniyaml import YamlError
from miniyaml import parse as parse_yaml

SYSTEM_FIELDS = {"创建时间": "createTime", "更新时间": "updateTime",
                 "createTime": "createTime", "updateTime": "updateTime"}


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        cfg = parse_yaml(text) if not path.endswith(".json") else json.loads(text)
    except (YamlError, ValueError) as exc:
        raise ConfigError("配置解析失败：%s" % exc)
    if not isinstance(cfg, dict):
        raise ConfigError("配置顶层必须是映射")
    for key in ("app", "sections"):
        if not cfg.get(key):
            raise ConfigError("配置缺少必填项：%s" % key)
    if not isinstance(cfg["sections"], list):
        raise ConfigError("sections 必须是列表")
    return cfg


def make_resolver(by_label):
    """按显示名取值，统一走内核的 display_value，口径与导出一致。"""
    def resolve(row, label):
        if label in SYSTEM_FIELDS:
            return row.get(SYSTEM_FIELDS[label])
        widget = by_label.get(label)
        if widget is None:
            return None
        return display_value(row.get(widget["name"]), widget["type"])
    return resolve


def period_field_name(by_label, spec, override=None):
    """周期字段。默认取全局 `period.field`（缺省为系统字段 createTime），
    板块可用 `period_field` 覆盖。

    为什么需要覆盖：各表的业务日期字段名不一样（跟进时间／报备时间／订单签订日期），
    只有一根全局时间轴的话，想按业务日期看趋势就只能整份报表退回「创建时间」——
    而创建时间往往是批量导入的时刻，画不出真实走势。
    """
    label = override or (spec or {}).get("field") or "创建时间"
    if label in SYSTEM_FIELDS:
        return SYSTEM_FIELDS[label], label
    widget = by_label.get(label)
    if widget is None:
        raise ConfigError("周期字段「%s」在表单里不存在" % label)
    if widget["type"] != "datetime":
        raise ConfigError("周期字段「%s」类型是 %s，不是日期时间" % (label, widget["type"]))
    return widget["name"], label


def fmt(value):
    if isinstance(value, float):
        return ("%.2f" % value).rstrip("0").rstrip(".")
    return str(value)


def fmt_delta(d):
    if d is None:
        return "—"
    if d["pct"] is None:
        return "%s%s（%s）" % ("+" if d["abs"] >= 0 else "", fmt(d["abs"]), d["note"])
    arrow = "▲" if d["pct"] > 0 else ("▼" if d["pct"] < 0 else "＝")
    return "%s %s%s%%" % (arrow, "+" if d["pct"] > 0 else "", fmt(d["pct"]))


EMPTY_PROBE_LIMIT = 500          # 只为报个数，不值得为此拉全表


def count_empty_period(client, app_id, entry_id, field_name):
    """周期字段为空的行有多少。

    区间过滤是在**服务端**做的，这些行压根不会返回——代码看不见，
    于是「7 条跟进记录」在报表里显示成 5，没有任何提示。
    实测中 Agent 自己核对原始数据才发现对不上。数字少了要说出来，
    否则读报表的人只会以为本来就是 5。
    """
    try:
        rows = client.fetch_all(app_id, entry_id, fields=["_id"],
                                limit=EMPTY_PROBE_LIMIT,
                                data_filter={"rel": "and",
                                             "cond": [{"field": field_name,
                                                       "method": "empty"}]})
    except JdyError:
        return None                  # 探测失败不影响出报表，但也不假装是 0
    n = len(rows)
    return {"count": n, "capped": n >= EMPTY_PROBE_LIMIT}


def report_tz(cfg):
    """报表时区。**只有这一处解析**——页脚回显的和真正用来切周期的必须是同一个。

    原来 main() 写死 DEFAULT_TZ、页脚却回显 cfg["tz"]：配了 utc 的报表，
    周期按 +08:00 切、页脚说是 UTC。数字看着合理，边界整整差了 8 小时。
    """
    return parse_tz(cfg.get("tz"))


def tz_label(dt):
    """时区标签从**实际生成时间**上取，而不是从配置里另抄一遍——这样它没法再撒谎。"""
    off = dt.utcoffset() or datetime.timedelta(0)
    minutes = int(off.total_seconds()) // 60
    return "%s%02d:%02d" % ("-" if minutes < 0 else "+", abs(minutes) // 60, abs(minutes) % 60)


def render(cfg, period, sections, generated_at):
    start, end, prev_start, prev_end = period
    out = ["# %s" % (cfg.get("name") or "简道云报表"), ""]
    out.append("- 统计区间：**%s ~ %s**（左闭右开）"
               % (start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")))
    if cfg.get("period", {}).get("compare", "previous") != "none":
        out.append("- 对比区间：%s ~ %s"
                   % (prev_start.strftime("%Y-%m-%d"), prev_end.strftime("%Y-%m-%d")))
    out.append("- 生成时间：%s（%s）"
               % (generated_at.strftime("%Y-%m-%d %H:%M"), tz_label(generated_at)))
    out.append("")

    for sec in sections:
        out.append("## %s" % sec["title"])
        out.append("")
        if sec.get("period_field_used"):
            out.append("_时间轴：%s_" % sec["period_field_used"])
            out.append("")
        gap = sec.get("excluded_empty")
        if gap and gap["count"]:
            out.append("> ⚠️ 另有 %s%d 条记录因该时间字段为空，**未纳入本报表任何统计**。"
                       % ("至少 " if gap["capped"] else "", gap["count"]))
            out.append("")
        if not sec["rows"]:
            out.append("_本期无数据。_")
            out.append("")
            continue
        out.append("| 指标 | 本期 | 上期 | 环比 |")
        out.append("|---|---:|---:|---|")
        for t in sec["totals"]:
            if t["value"] is None:
                shown = t.get("note") or "—"
            else:
                shown = "**%s%s**" % (fmt(t["value"]), t.get("unit", ""))
            out.append("| %s | %s | %s | %s |"
                       % (t["label"], shown,
                          "—" if t["previous"] is None else fmt(t["previous"]),
                          fmt_delta(t["delta"])))
        out.append("")
        trend = sec.get("trend")
        if trend and trend["labels"]:
            out.append("**趋势**（按%s）" % {"day": "天", "week": "周", "month": "月"}[sec["trend_by"]])
            out.append("")
            out.append("| 期间 | %s |" % " | ".join(x["label"] for x in trend["series"]))
            out.append("|---|%s" % ("---:|" * len(trend["series"])))
            for i, lab in enumerate(trend["labels"]):
                out.append("| %s | %s |"
                           % (lab, " | ".join("—" if x["values"][i] is None
                                              else fmt(x["values"][i])
                                              for x in trend["series"])))
            out.append("")
            if trend["undated"]:
                out.append("> ⚠️ 另有 **%d 行**该字段为空或格式无法识别，未计入趋势（但计入了上方总计）。"
                           % trend["undated"])
                out.append("")
        if sec["breakdown"]:
            dims = "／".join(sec["dimensions"])
            truncated = sec.get("group_total", 0) > len(sec["breakdown"])
            out.append("**按%s拆分**%s"
                       % (dims, "（Top %d，共 %d 组）" % (len(sec["breakdown"]), sec["group_total"])
                          if truncated else "（共 %d 组）" % sec["group_total"]))
            out.append("")
            out.append("| %s | %s |" % (dims, " | ".join(sec["metric_labels"])))
            out.append("|---|%s" % ("---:|" * len(sec["metric_labels"])))
            for b in sec["breakdown"]:
                out.append("| %s | %s |" % ("／".join(b["key"]),
                                            " | ".join(fmt(v) for v in b["values"])))
            out.append("")
    foot = brand.md_footer()
    if foot:                       # 关掉时连那一行空行也不留
        out += ["---", "", foot]
    return "\n".join(out)


def parse_now(text, tz):
    """--now 收 YYYY-MM-DD，也收完整 ISO 时间戳。

    只收日期、又不校验，写成 ISO 就抛一句 strptime 的
    「unconverted data remains」——那不是给人看的话。
    """
    raw = str(text).strip()
    try:
        moment = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise ConfigError("--now 看不懂：%r。用 YYYY-MM-DD，或完整 ISO 时间戳"
                          "（2026-08-01T00:00:00+08:00）" % text)
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


def main():
    ap = argparse.ArgumentParser(description="生成简道云周报/月报（只读）")
    ap.add_argument("config", help="报表定义（.yaml 或 .json）")
    ap.add_argument("--out", help="Markdown 输出路径，缺省打印到标准输出")
    ap.add_argument("--json-out", help="同时另存结构化结果，供推送或二次加工")
    ap.add_argument("--now", help="把「现在」固定成某天（YYYY-MM-DD），用于复现历史报表")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    try:
        client = JdyClient()
    except JdyError as exc:
        sys.stderr.write("%s\n" % exc.msg)
        return 2

    # 报表定义里的 tz 必须真的传下去。原来这里写死 DEFAULT_TZ，
    # 页脚却回显 cfg["tz"]——配了 +00:00 的报表，周期按 +08:00 切、页脚说是 UTC，
    # 数字看着合理，实际切错了一整个时区的边界。
    try:
        tz = report_tz(cfg)
    except ValueError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    now = None
    if args.now:
        now = parse_now(args.now, tz)
    try:
        period = resolve_period(cfg.get("period"), now=now, tz=tz)
    except ConfigError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    start, end, prev_start, prev_end = period
    compare = (cfg.get("period") or {}).get("compare", "previous") != "none"

    results = []
    for sec in cfg["sections"]:
        entry = sec.get("entry")
        if not entry:
            sys.stderr.write("section「%s」缺少 entry\n" % sec.get("title"))
            return 2
        by_label, _ = client.field_map(cfg["app"], entry)
        resolve = make_resolver(by_label)
        try:
            field_name, field_label = period_field_name(
                by_label, cfg.get("period"), sec.get("period_field"))
            for dim in sec.get("dimensions") or []:
                if dim not in by_label and dim not in SYSTEM_FIELDS:
                    raise ConfigError("section「%s」的维度「%s」在表单里不存在"
                                      % (sec.get("title"), dim))
            # 指标字段同样要校验。不校验的话 sum/avg 指向不存在的字段会算出 0，
            # 报告里就是一行「金额 0」——读的人当成"确实是零"，比报错危险得多。
            for m in sec.get("metrics") or []:
                need = m.get("field")
                if not need or m.get("agg") in ("count", "ratio"):
                    continue
                if need not in by_label and need not in SYSTEM_FIELDS:
                    raise ConfigError(
                        "section「%s」的指标「%s」用了字段「%s」，表单里不存在。"
                        "可用字段：%s"
                        % (sec.get("title"), m.get("label") or m.get("agg"), need,
                           "、".join(list(by_label)[:12])))
        except ConfigError as exc:
            sys.stderr.write("%s\n" % exc)
            return 2

        if sys.stderr.isatty():
            sys.stderr.write("\r拉取「%s」…" % sec.get("title"))
            sys.stderr.flush()
        try:
            cur = client.fetch_all(cfg["app"], entry,
                                   data_filter=period_filter(field_name, start, end))
            prev = client.fetch_all(cfg["app"], entry,
                                    data_filter=period_filter(field_name, prev_start, prev_end)) \
                if compare else None
        except JdyError as exc:
            sys.stderr.write("\n拉取失败：%s\n" % exc)
            return 2
        try:
            built = build_section(cur, prev, sec, resolve)
            built["period_field_used"] = field_label if sec.get("period_field") else None
            built["excluded_empty"] = count_empty_period(client, cfg["app"], entry,
                                                         field_name)
            if sec.get("trend"):
                built["trend_by"] = sec["trend"]
                built["trend"] = build_trend(cur, sec, resolve, field_label,
                                             sec["trend"], start, end, tz)
            results.append(built)
        except ConfigError as exc:
            sys.stderr.write("\n%s\n" % exc)
            return 2
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 40 + "\r")

    try:
        results = apply_derived(results)          # 第二趟：派生指标
    except ConfigError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    generated = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz)
    doc = render(cfg, period, results, generated)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print("报表已生成：%s（%d 个板块）" % (args.out, len(results)))
    else:
        print(doc)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"name": cfg.get("name"), "app": cfg["app"],
                       "period": {"start": start.isoformat(), "end": end.isoformat(),
                                  "prev_start": prev_start.isoformat(),
                                  "prev_end": prev_end.isoformat()},
                       "generated_at": generated.isoformat(),
                       "sections": results}, fh, ensure_ascii=False, indent=2)
        print("结构化结果：%s" % args.json_out)

    empty = [s["title"] for s in results if not s["rows"]]
    if empty:
        print("\n注意：%d 个板块本期无数据（%s）——先确认周期字段与区间是否符合预期。"
              % (len(empty), "、".join(empty)))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
