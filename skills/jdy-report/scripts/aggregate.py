# -*- coding: utf-8 -*-
"""报表聚合引擎：周期解析、分组、指标计算、环比。

计划里写的是 pandas，但 V3 实测沙箱有 Python 3.13 却没有 pip 通道——
所以用标准库实现。报表要的 group-by 与聚合本来也不复杂，
换来的是"任何一端都能跑"，这个交换很划算。

时间一律按**报表时区**（默认 +08:00 北京时间）切分周期，再转 UTC 去查
——简道云存 UTC、按 +8 显示，用机器本地时区切会把周一切在错误的时刻。
"""
import datetime

from jdy_client import (AGGS, DEFAULT_TZ, UNFILLED, aggregate_rows, group_rows,
                        parse_iso, to_number)   # noqa: F401
#                       ^ AGGS / DEFAULT_TZ 在此转出供调用方用

DERIVED = ("ratio",)          # 派生指标：由其他指标算出，不直接查数据

RANGES = ("last_7_days", "last_14_days", "last_30_days",
          "this_week", "last_week", "this_month", "last_month", "custom")


class ConfigError(ValueError):
    pass


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _day_start(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def resolve_period(spec, now=None, tz=DEFAULT_TZ):
    """把周期定义解析成 (start, end, prev_start, prev_end)，均为 tz-aware datetime。

    区间统一取左闭右开 [start, end)，避免边界数据被两个周期各算一次。
    """
    spec = spec or {}
    kind = spec.get("range", "last_7_days")
    if kind not in RANGES:
        raise ConfigError("不支持的 range：%r（可用：%s）" % (kind, "、".join(RANGES)))
    now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz)
    today = _day_start(now)

    if kind == "custom":
        if not spec.get("start") or not spec.get("end"):
            raise ConfigError("range: custom 需要同时给 start 与 end（YYYY-MM-DD）")
        start = _day_start(datetime.datetime.strptime(str(spec["start"]), "%Y-%m-%d").replace(tzinfo=tz))
        end = _day_start(datetime.datetime.strptime(str(spec["end"]), "%Y-%m-%d").replace(tzinfo=tz)) \
            + datetime.timedelta(days=1)          # end 当天算在内
    elif kind.startswith("last_") and kind.endswith("_days"):
        days = int(kind.split("_")[1])
        end = today + datetime.timedelta(days=1)
        start = end - datetime.timedelta(days=days)
    elif kind == "this_week":
        start = today - datetime.timedelta(days=today.weekday())
        end = start + datetime.timedelta(days=7)
    elif kind == "last_week":
        this_start = today - datetime.timedelta(days=today.weekday())
        start = this_start - datetime.timedelta(days=7)
        end = this_start
    elif kind == "this_month":
        start = today.replace(day=1)
        end = (start + datetime.timedelta(days=32)).replace(day=1)
    else:                                          # last_month
        this_start = today.replace(day=1)
        start = (this_start - datetime.timedelta(days=1)).replace(day=1)
        end = this_start

    span = end - start
    return start, end, start - span, start


def period_filter(field_name, start, end):
    """左闭右开区间的 filter DSL。

    简道云的 `range` 是闭区间，直接用会把 end 当天的数据也算进来，
    导致相邻两期重复计数。所以 end 减 1 毫秒。
    """
    end_inclusive = end - datetime.timedelta(milliseconds=1)
    return {"rel": "and", "cond": [{"field": field_name, "method": "range",
                                    "value": [_iso(start), _iso(end_inclusive)]}]}


def _numeric(value):
    """走内核 to_number——数字口径全项目一份，见那里的说明。"""
    return to_number(value)


def compute_metric(rows, metric, resolve):
    """算一个指标。resolve(row, label) 取该行某字段的值。

    这里只做**配置层**的事：把报表定义里的写法错误翻译成一句人能照着改的话
    （ConfigError 会被入口渲染成一行提示，而不是一屏 traceback）。
    算数本身走内核 aggregate_rows——口径全项目一份，见那里的说明。
    """
    agg = metric.get("agg", "count")
    if agg not in AGGS:
        raise ConfigError("不支持的 agg：%r（可用：%s）" % (agg, "、".join(AGGS)))
    if agg != "count" and not metric.get("field"):
        raise ConfigError("指标「%s」用了 %s，必须同时给 field"
                          % (metric.get("label", "?"), agg))
    return aggregate_rows(rows, agg, metric.get("field"), resolve)


def split_metrics(metrics):
    """把指标分成「直接算的」和「由别的指标派生的」。

    派生指标（回款率、良率、达成率、周转率……）跨领域都要用，
    但它依赖别的指标先算完，所以必须分两趟。
    """
    base, derived = [], []
    for m in metrics or []:
        (derived if m.get("agg") in DERIVED else base).append(m)
    return base, derived


def _ref(spec, section_title):
    """指标引用：`指标标签`（本板块）或 `板块标题.指标标签`（跨板块）。"""
    text = str(spec)
    if "." in text:
        sec, label = text.split(".", 1)
        return sec.strip(), label.strip()
    return section_title, text.strip()


def _safe_ratio(num, den, as_percent):
    """分母为 0 或缺失时返回 None——除零算出的数不是"很大"，是没有意义。"""
    if num is None or den in (None, 0):
        return None
    value = num / float(den)
    return round(value * 100, 1) if as_percent else round(value, 4)


def apply_derived(sections):
    """所有板块的基础指标算完后，第二趟解析派生指标。

    跨板块引用之所以能对齐趋势，是因为时间桶按区间预生成、序列等长——
    只对有数据的时间分组的话，这里就会把 A 的第 3 个月对到 B 的第 5 个月。
    """
    totals = {}
    trends = {}
    for sec in sections:
        for t in sec["totals"]:
            totals[(sec["title"], t["label"])] = t["value"]
        tr = sec.get("trend")
        if tr:
            for series in tr["series"]:
                trends[(sec["title"], series["label"])] = (tr["labels"], series["values"])

    for sec in sections:
        for m in sec.get("derived", []):
            label = m.get("label") or "比率"
            as_percent = m.get("as", "percent") == "percent"
            num_ref = _ref(m.get("numerator"), sec["title"])
            den_ref = _ref(m.get("denominator"), sec["title"])
            for ref in (num_ref, den_ref):
                if ref not in totals:
                    raise ConfigError(
                        "指标「%s」引用了不存在的指标：%s。可引用的有：%s"
                        % (label, "%s.%s" % ref,
                           "、".join("%s.%s" % k for k in sorted(totals))))

            sec["totals"].append({
                "label": label,
                "value": _safe_ratio(totals[num_ref], totals[den_ref], as_percent),
                "previous": None, "delta": None,
                "unit": "%" if as_percent else "",
                "note": None if totals[den_ref] else "分母为 0，无法计算"})

            tr = sec.get("trend")
            if tr and num_ref in trends and den_ref in trends:
                n_labels, n_vals = trends[num_ref]
                d_labels, d_vals = trends[den_ref]
                if n_labels != d_labels:
                    # 两个板块的时间轴或粒度不同，对齐没有意义，宁可不给
                    tr["series"].append({"label": label + "（时间轴不一致，未计算）",
                                         "values": [None] * len(tr["labels"])})
                else:
                    tr["series"].append({
                        "label": label,
                        "values": [_safe_ratio(n, d, as_percent)
                                   for n, d in zip(n_vals, d_vals)]})
    return sections


def group_by(rows, dimensions, resolve):
    """按维度分组。走内核 group_rows——分组口径（尤其"空值归到哪"）两个技能一致。"""
    return group_rows(rows, dimensions, resolve)


def delta(current, previous):
    """环比。上期为 0 时不返回百分比——除零算出来的"+∞%"是噪音不是信息。"""
    if previous in (None, 0):
        return {"abs": current, "pct": None,
                "note": "上期无数据" if previous is None else "上期为 0"}
    return {"abs": round(current - previous, 4),
            "pct": round((current - previous) / abs(previous) * 100, 1), "note": None}


GRANULARITY = ("day", "week", "month")


def _bucket_start(dt, granularity):
    if granularity == "day":
        return _day_start(dt)
    if granularity == "week":
        d = _day_start(dt)
        return d - datetime.timedelta(days=d.weekday())      # 周一起算
    return _day_start(dt).replace(day=1)


def _next_bucket(dt, granularity):
    if granularity == "day":
        return dt + datetime.timedelta(days=1)
    if granularity == "week":
        return dt + datetime.timedelta(days=7)
    return (dt + datetime.timedelta(days=32)).replace(day=1)


def _bucket_label(dt, granularity):
    if granularity == "month":
        return dt.strftime("%Y-%m")
    if granularity == "week":
        return dt.strftime("%m-%d") + " 周"
    return dt.strftime("%m-%d")


def bucket_series(rows, period_label, granularity, start, end, resolve, tz=DEFAULT_TZ):
    """把区间切成等距桶，每个桶带上落在其中的行。

    **桶按区间预先生成，空桶保留为 0** —— 只对有数据的时间分组会让不同指标
    的序列长度不一致，两两对齐时就会错位（把 A 指标的第 3 个月对到 B 指标的第 5 个月）。
    这类错位不会报错，只会画出一条错的曲线。
    """
    if granularity not in GRANULARITY:
        raise ConfigError("不支持的 trend：%r（可用：%s）" % (granularity, "、".join(GRANULARITY)))
    buckets, cursor = [], _bucket_start(start, granularity)
    while cursor < end:
        buckets.append([_bucket_label(cursor, granularity), cursor,
                        _next_bucket(cursor, granularity), []])
        cursor = _next_bucket(cursor, granularity)

    undated = 0
    for row in rows:
        dt = parse_iso(resolve(row, period_label))
        if dt is None:
            undated += 1
            continue
        local = dt.astimezone(tz)
        for b in buckets:
            if b[1] <= local < b[2]:
                b[3].append(row)
                break
    return buckets, undated


def build_trend(rows, section, resolve, period_label, granularity, start, end, tz=DEFAULT_TZ):
    """按时间桶算各指标序列。返回 {labels, series, undated}。"""
    metrics, _ = split_metrics(section.get("metrics") or [{"label": "记录数", "agg": "count"}])
    buckets, undated = bucket_series(rows, period_label, granularity, start, end, resolve, tz)
    labels = [b[0] for b in buckets]
    series = []
    for m in metrics:
        series.append({"label": m.get("label") or m.get("field") or m.get("agg"),
                       # 与 labels 等长，空桶为 0——序列间才能安全对齐
                       "values": [compute_metric(b[3], m, resolve) for b in buckets]})
    return {"labels": labels, "series": series, "undated": undated}


def build_section(rows_cur, rows_prev, section, resolve):
    """算出一个 section 的总计、环比与 Top 榜。"""
    metrics, derived = split_metrics(section.get("metrics") or [{"label": "记录数", "agg": "count"}])
    if not metrics:
        raise ConfigError("板块「%s」只有派生指标，没有可计算的基础指标" % section.get("title"))
    dims = section.get("dimensions") or []
    top_n = section.get("top")

    totals = []
    for m in metrics:
        cur = compute_metric(rows_cur, m, resolve)
        prev = compute_metric(rows_prev, m, resolve) if rows_prev is not None else None
        totals.append({"label": m.get("label") or m.get("field") or m.get("agg"),
                       "value": cur, "previous": prev,
                       "delta": delta(cur, prev) if rows_prev is not None else None})

    breakdown, group_total = [], 0
    if dims:
        for key, bucket in group_by(rows_cur, dims, resolve):
            values = [compute_metric(bucket, m, resolve) for m in metrics]
            breakdown.append({"key": key, "values": values})
        group_total = len(breakdown)
        breakdown.sort(key=lambda b: (b["values"][0] if b["values"] else 0), reverse=True)
        if top_n:
            breakdown = breakdown[:int(top_n)]

    return {"title": section.get("title") or "未命名", "rows": len(rows_cur),
            "metric_labels": [t["label"] for t in totals],
            "dimensions": dims, "totals": totals, "breakdown": breakdown,
            "derived": derived,
            # 分组总数，用来判断 Top 榜是否真的截断了——拿它和行数比会误报
            "group_total": group_total}
