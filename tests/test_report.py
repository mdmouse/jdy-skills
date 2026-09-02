# -*- coding: utf-8 -*-
"""jdy-report 聚合引擎测试。

周期边界是这个技能最危险的地方：算错不会报错，只会产出**看似合理的错数字**。
所以边界条件按天钉死，不靠"跑一下看着对"。

    python3 tests/test_report.py
"""
import datetime
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "jdy-report", "scripts"))

from aggregate import (  # noqa: E402
    DEFAULT_TZ, ConfigError, apply_derived, build_section, build_trend, bucket_series,
    compute_metric, delta, group_by, period_filter, resolve_period, split_metrics,
)
import build_report  # noqa: E402
import push  # noqa: E402,F401  （入口脚本，触发词契约测试要用）
import webhook  # noqa: E402   （推送实现已下沉内核，flow-ops 催办也用它）

# 2026-08-28 是星期五
NOW = datetime.datetime(2026, 8, 28, 15, 30, tzinfo=DEFAULT_TZ)
D = lambda y, m, d: datetime.datetime(y, m, d, tzinfo=DEFAULT_TZ)


class TestPeriodBoundaries(unittest.TestCase):
    def test_last_7_days_includes_today(self):
        start, end, ps, pe = resolve_period({"range": "last_7_days"}, now=NOW)
        self.assertEqual(start, D(2026, 8, 22))
        self.assertEqual(end, D(2026, 8, 29))          # 右开，含今天一整天
        self.assertEqual((end - start).days, 7)

    def test_previous_period_is_same_span_immediately_before(self):
        start, end, ps, pe = resolve_period({"range": "last_7_days"}, now=NOW)
        self.assertEqual(pe, start)                     # 上期紧接本期，不重叠
        self.assertEqual((pe - ps).days, 7)
        self.assertEqual(ps, D(2026, 8, 15))

    def test_this_week_starts_monday(self):
        start, end, _, _ = resolve_period({"range": "this_week"}, now=NOW)
        self.assertEqual(start, D(2026, 8, 24))         # 2026-08-24 是周一
        self.assertEqual(start.weekday(), 0)
        self.assertEqual(end, D(2026, 8, 31))

    def test_last_week(self):
        start, end, _, _ = resolve_period({"range": "last_week"}, now=NOW)
        self.assertEqual(start, D(2026, 8, 17))
        self.assertEqual(end, D(2026, 8, 24))

    def test_this_month(self):
        start, end, ps, pe = resolve_period({"range": "this_month"}, now=NOW)
        self.assertEqual(start, D(2026, 8, 1))
        self.assertEqual(end, D(2026, 9, 1))

    def test_last_month_across_year_boundary(self):
        jan = datetime.datetime(2026, 1, 15, tzinfo=DEFAULT_TZ)
        start, end, _, _ = resolve_period({"range": "last_month"}, now=jan)
        self.assertEqual(start, D(2025, 12, 1))
        self.assertEqual(end, D(2026, 1, 1))

    def test_this_month_in_december(self):
        dec = datetime.datetime(2026, 12, 10, tzinfo=DEFAULT_TZ)
        start, end, _, _ = resolve_period({"range": "this_month"}, now=dec)
        self.assertEqual(start, D(2026, 12, 1))
        self.assertEqual(end, D(2027, 1, 1))

    def test_custom_end_day_is_included(self):
        start, end, _, _ = resolve_period(
            {"range": "custom", "start": "2026-08-01", "end": "2026-08-07"}, now=NOW)
        self.assertEqual(start, D(2026, 8, 1))
        self.assertEqual(end, D(2026, 8, 8))            # 8-07 当天要算进去

    def test_custom_requires_both_ends(self):
        with self.assertRaises(ConfigError):
            resolve_period({"range": "custom", "start": "2026-08-01"}, now=NOW)

    def test_unknown_range_rejected(self):
        with self.assertRaises(ConfigError):
            resolve_period({"range": "last_quarter"}, now=NOW)

    def test_period_uses_report_tz_not_machine_tz(self):
        """周期按 +08:00 切，不受跑脚本的机器时区影响。"""
        start, _, _, _ = resolve_period({"range": "this_month"}, now=NOW)
        self.assertEqual(start.utcoffset(), datetime.timedelta(hours=8))
        self.assertEqual(start.hour, 0)


class TestPeriodFilter(unittest.TestCase):
    def test_right_open_interval(self):
        """简道云 range 是闭区间，直接用会让相邻两期重复计数。"""
        flt = period_filter("createTime", D(2026, 8, 22), D(2026, 8, 29))
        lo, hi = flt["cond"][0]["value"]
        self.assertEqual(lo, "2026-08-21T16:00:00.000Z")   # 北京 8-22 00:00
        self.assertEqual(hi, "2026-08-28T15:59:59.000Z")   # 比 8-29 00:00 早一点
        self.assertEqual(flt["cond"][0]["method"], "range")

    def test_adjacent_periods_do_not_overlap(self):
        s, e, ps, pe = resolve_period({"range": "last_7_days"}, now=NOW)
        cur_lo = period_filter("createTime", s, e)["cond"][0]["value"][0]
        prev_hi = period_filter("createTime", ps, pe)["cond"][0]["value"][1]
        self.assertLess(prev_hi, cur_lo)


ROWS = [
    {"客户": "甲", "金额": 100, "人": "张三"},
    {"客户": "乙", "金额": "200", "人": "张三"},
    {"客户": "甲", "金额": 300, "人": "李四"},
    {"客户": None, "金额": "无效", "人": None},
]
RESOLVE = lambda row, label: row.get(label)


class TestMetrics(unittest.TestCase):
    def test_count(self):
        self.assertEqual(compute_metric(ROWS, {"agg": "count"}, RESOLVE), 4)

    def test_sum_coerces_numeric_strings_and_skips_garbage(self):
        self.assertEqual(compute_metric(ROWS, {"agg": "sum", "field": "金额"}, RESOLVE), 600)

    def test_avg_over_parsable_only(self):
        self.assertEqual(compute_metric(ROWS, {"agg": "avg", "field": "金额"}, RESOLVE), 200)

    def test_max_min(self):
        self.assertEqual(compute_metric(ROWS, {"agg": "max", "field": "金额"}, RESOLVE), 300)
        self.assertEqual(compute_metric(ROWS, {"agg": "min", "field": "金额"}, RESOLVE), 100)

    def test_distinct_ignores_none(self):
        self.assertEqual(compute_metric(ROWS, {"agg": "distinct", "field": "人"}, RESOLVE), 2)

    def test_sum_of_nothing_is_zero_not_error(self):
        self.assertEqual(compute_metric([], {"agg": "sum", "field": "金额"}, RESOLVE), 0)

    def test_agg_needing_field_without_field_rejected(self):
        with self.assertRaises(ConfigError):
            compute_metric(ROWS, {"agg": "sum", "label": "金额"}, RESOLVE)

    def test_unknown_agg_rejected(self):
        with self.assertRaises(ConfigError):
            compute_metric(ROWS, {"agg": "median", "field": "金额"}, RESOLVE)


class TestGrouping(unittest.TestCase):
    def test_missing_value_bucketed_explicitly(self):
        groups = dict(group_by(ROWS, ["客户"], RESOLVE))
        self.assertEqual(sorted(groups), [("(未填)",), ("乙",), ("甲",)])   # 键是元组，支持多维
        self.assertEqual(len(groups[("甲",)]), 2)

    def test_multiple_dimensions(self):
        groups = dict(group_by(ROWS, ["客户", "人"], RESOLVE))
        self.assertIn(("甲", "张三"), groups)
        self.assertIn(("甲", "李四"), groups)

    def test_no_dimension_returns_single_bucket(self):
        self.assertEqual(len(group_by(ROWS, [], RESOLVE)), 1)


class TestDelta(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(delta(120, 100), {"abs": 20, "pct": 20.0, "note": None})

    def test_decline(self):
        self.assertEqual(delta(80, 100)["pct"], -20.0)

    def test_previous_zero_gives_no_percentage(self):
        """上期为 0 时除零算出的百分比是噪音，不是信息。"""
        d = delta(50, 0)
        self.assertIsNone(d["pct"])
        self.assertEqual(d["abs"], 50)
        self.assertIn("0", d["note"])

    def test_previous_none(self):
        self.assertIsNone(delta(50, None)["pct"])


class TestBuildSection(unittest.TestCase):
    def test_top_n_and_group_total(self):
        sec = build_section(ROWS, None,
                            {"title": "T", "dimensions": ["客户"], "top": 1,
                             "metrics": [{"label": "数", "agg": "count"}]}, RESOLVE)
        self.assertEqual(len(sec["breakdown"]), 1)
        self.assertEqual(sec["group_total"], 3)          # 截断判断要看分组数，不是行数
        self.assertEqual(sec["breakdown"][0]["key"], ("甲",))   # 按首个指标降序

    def test_no_truncation_when_top_exceeds_groups(self):
        sec = build_section(ROWS, None,
                            {"title": "T", "dimensions": ["客户"], "top": 99}, RESOLVE)
        self.assertEqual(len(sec["breakdown"]), sec["group_total"])

    def test_default_metric_is_count(self):
        sec = build_section(ROWS, None, {"title": "T"}, RESOLVE)
        self.assertEqual(sec["totals"][0]["value"], 4)

    def test_compare_absent_when_no_previous(self):
        sec = build_section(ROWS, None, {"title": "T"}, RESOLVE)
        self.assertIsNone(sec["totals"][0]["delta"])


class TestTrendBuckets(unittest.TestCase):
    """时间分桶。空桶必须保留——只对有数据的时间分组会让序列错位。"""

    ROWS = [
        {"d": "2026-08-03T02:00:00.000Z", "n": 10},     # 北京 08-03 10:00
        {"d": "2026-08-03T05:00:00.000Z", "n": 20},
        {"d": "2026-08-20T02:00:00.000Z", "n": 30},
        {"d": None, "n": 40},                            # 无日期
        {"d": "不是日期", "n": 50},                       # 解析不了
    ]
    R = staticmethod(lambda row, label: row.get("d"))

    def test_empty_buckets_are_kept(self):
        buckets, undated = bucket_series(
            self.ROWS, "d", "week", D(2026, 8, 1), D(2026, 8, 29), self.R)
        self.assertEqual([b[0] for b in buckets],
                         ["07-27 周", "08-03 周", "08-10 周", "08-17 周", "08-24 周"])
        self.assertEqual([len(b[3]) for b in buckets], [0, 2, 0, 1, 0])

    def test_unparseable_dates_counted_not_dropped_silently(self):
        _, undated = bucket_series(self.ROWS, "d", "week", D(2026, 8, 1), D(2026, 8, 29), self.R)
        self.assertEqual(undated, 2)                     # None 与「不是日期」都要报出来

    def test_weeks_start_monday(self):
        buckets, _ = bucket_series([], "d", "week", D(2026, 8, 5), D(2026, 8, 20), self.R)
        self.assertEqual(buckets[0][1].weekday(), 0)

    def test_month_buckets_across_year(self):
        buckets, _ = bucket_series([], "d", "month", D(2025, 11, 1), D(2026, 2, 1), self.R)
        self.assertEqual([b[0] for b in buckets], ["2025-11", "2025-12", "2026-01"])

    def test_all_series_same_length_as_labels(self):
        """不等长的序列两两对齐时会错位，且不会报错——只会画出一条错的曲线。"""
        trend = build_trend(self.ROWS, {"metrics": [{"label": "数", "agg": "count"},
                                                    {"label": "和", "agg": "sum", "field": "n"}]},
                            self.R, "d", "week", D(2026, 8, 1), D(2026, 8, 29))
        for s in trend["series"]:
            self.assertEqual(len(s["values"]), len(trend["labels"]))

    def test_timezone_boundary(self):
        """UTC 16:00 已是北京次日 00:00，要落进次日的桶。"""
        rows = [{"d": "2026-08-02T16:00:00.000Z"}]
        buckets, _ = bucket_series(rows, "d", "day", D(2026, 8, 1), D(2026, 8, 5), self.R)
        got = {b[0]: len(b[3]) for b in buckets}
        self.assertEqual(got["08-03"], 1)
        self.assertEqual(got["08-02"], 0)

    def test_unknown_granularity_rejected(self):
        with self.assertRaises(ConfigError):
            bucket_series([], "d", "quarter", D(2026, 8, 1), D(2026, 8, 29), self.R)


class TestPeriodFieldOverride(unittest.TestCase):
    """各表的业务日期字段名不同，只有一根全局时间轴就只能整份退回「创建时间」。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "skills", "jdy-report", "scripts"))
        from build_report import period_field_name
        self.fn = period_field_name
        self.by_label = {"订单签订日期": {"name": "_w_sign", "label": "订单签订日期",
                                          "type": "datetime"},
                         "金额": {"name": "_w_amt", "label": "金额", "type": "number"}}

    def test_defaults_to_create_time(self):
        self.assertEqual(self.fn(self.by_label, None), ("createTime", "创建时间"))

    def test_global_field(self):
        self.assertEqual(self.fn(self.by_label, {"field": "订单签订日期"})[0], "_w_sign")

    def test_section_override_wins(self):
        got = self.fn(self.by_label, {"field": "创建时间"}, override="订单签订日期")
        self.assertEqual(got, ("_w_sign", "订单签订日期"))

    def test_missing_field_rejected(self):
        with self.assertRaises(ConfigError):
            self.fn(self.by_label, None, override="不存在的字段")

    def test_non_datetime_field_rejected(self):
        """拿数字字段当时间轴会静默算出空趋势，必须提前拦住。"""
        with self.assertRaises(ConfigError):
            self.fn(self.by_label, None, override="金额")


def _sec(title, totals, trend=None, derived=None):
    out = {"title": title, "totals": [{"label": l, "value": v, "previous": None,
                                       "delta": None} for l, v in totals],
           "derived": derived or []}
    if trend:
        labels, series = trend
        out["trend"] = {"labels": labels, "undated": 0,
                        "series": [{"label": l, "values": v} for l, v in series]}
    return out


class TestDerivedMetrics(unittest.TestCase):
    """比率是跨领域的通用需求：回款率、良率、达成率、周转率。"""

    def test_split_metrics(self):
        base, derived = split_metrics([{"agg": "count"}, {"agg": "ratio"}, {"agg": "sum"}])
        self.assertEqual(len(base), 2)
        self.assertEqual(len(derived), 1)

    def test_same_section_ratio(self):
        secs = apply_derived([_sec("质检", [("合格数", 90), ("总数", 100)],
                                   derived=[{"label": "良率", "agg": "ratio",
                                             "numerator": "合格数", "denominator": "总数"}])])
        self.assertEqual(secs[0]["totals"][-1]["value"], 90.0)
        self.assertEqual(secs[0]["totals"][-1]["unit"], "%")

    def test_cross_section_ratio(self):
        """回款率的分子分母在两张不同的表——这是最常见的形态。"""
        secs = apply_derived([
            _sec("销售订单", [("订单金额", 1000)]),
            _sec("回款", [("回款金额", 829)],
                 derived=[{"label": "回款率", "agg": "ratio", "numerator": "回款金额",
                           "denominator": "销售订单.订单金额"}])])
        self.assertEqual(secs[1]["totals"][-1]["value"], 82.9)

    def test_raw_mode_not_percent(self):
        secs = apply_derived([_sec("库存", [("出库", 300), ("均库存", 100)],
                                   derived=[{"label": "周转率", "agg": "ratio", "as": "raw",
                                             "numerator": "出库", "denominator": "均库存"}])])
        self.assertEqual(secs[0]["totals"][-1]["value"], 3.0)
        self.assertEqual(secs[0]["totals"][-1]["unit"], "")

    def test_zero_denominator_is_none_with_note(self):
        """除零算出来的不是"很大"，是没有意义。"""
        secs = apply_derived([_sec("达成", [("实际", 50), ("目标", 0)],
                                   derived=[{"label": "达成率", "agg": "ratio",
                                             "numerator": "实际", "denominator": "目标"}])])
        t = secs[0]["totals"][-1]
        self.assertIsNone(t["value"])
        self.assertIn("分母为 0", t["note"])

    def test_unknown_reference_lists_available(self):
        with self.assertRaises(ConfigError) as ctx:
            apply_derived([_sec("A", [("X", 1)],
                                derived=[{"label": "R", "agg": "ratio",
                                          "numerator": "不存在", "denominator": "X"}])])
        self.assertIn("可引用的有", str(ctx.exception))

    def test_trend_ratio_series(self):
        secs = apply_derived([
            _sec("订单", [("金额", 300)], trend=(["01", "02", "03"], [("金额", [100, 200, 0])])),
            _sec("回款", [("回款", 150)],
                 trend=(["01", "02", "03"], [("回款", [50, 100, 0])]),
                 derived=[{"label": "回款率", "agg": "ratio", "numerator": "回款",
                           "denominator": "订单.金额"}])])
        ratio = secs[1]["trend"]["series"][-1]
        self.assertEqual(ratio["values"], [50.0, 50.0, None])   # 第 3 期分母为 0

    def test_mismatched_trend_axes_not_silently_aligned(self):
        """两个板块粒度/时间轴不同就别硬对——错位不报错，只会画出错的曲线。"""
        secs = apply_derived([
            _sec("订单", [("金额", 300)], trend=(["01", "02"], [("金额", [100, 200])])),
            _sec("回款", [("回款", 150)],
                 trend=(["2023-01", "2023-02", "2023-03"], [("回款", [50, 50, 50])]),
                 derived=[{"label": "回款率", "agg": "ratio", "numerator": "回款",
                           "denominator": "订单.金额"}])])
        last = secs[1]["trend"]["series"][-1]
        self.assertIn("时间轴不一致", last["label"])
        self.assertTrue(all(v is None for v in last["values"]))

    def test_section_with_only_derived_rejected(self):
        with self.assertRaises(ConfigError):
            build_section([], None, {"title": "T", "metrics": [
                {"label": "R", "agg": "ratio", "numerator": "a", "denominator": "b"}]}, RESOLVE)




class TestPush(unittest.TestCase):
    """推送到群机器人。三家的消息体各不相同，任何一处写错都是发出去才知道。"""

    MD = "# 标题\n\n正文一行。"      # 不含表格：这组测的是信封形状，摊平另有测试

    def test_payload_shapes(self):
        self.assertEqual(webhook.build_payload("wecom", "T", self.MD),
                         {"msgtype": "markdown", "markdown": {"content": self.MD}})
        self.assertEqual(webhook.build_payload("dingtalk", "T", self.MD),
                         {"msgtype": "markdown", "markdown": {"title": "T", "text": self.MD}})
        # 飞书群机器人的 markdown 支持有限，走纯文本最稳
        self.assertEqual(webhook.build_payload("feishu", "T", self.MD),
                         {"msg_type": "text", "content": {"text": self.MD}})

    def test_unknown_flavor_refuses(self):
        with self.assertRaises(ValueError):
            webhook.build_payload("slack", "T", self.MD)

    def test_detect_by_host(self):
        self.assertEqual(webhook.detect("https://qyapi.weixin.qq.com/x?key=1"), "wecom")
        self.assertEqual(webhook.detect("https://open.feishu.cn/x"), "feishu")
        self.assertEqual(webhook.detect("https://open.larksuite.com/x"), "feishu")
        self.assertEqual(webhook.detect("https://oapi.dingtalk.com/x"), "dingtalk")
        self.assertIsNone(webhook.detect("https://gateway.example.com/hook"))

    def test_secret_is_masked(self):
        # webhook 里的 key/access_token 就是密钥，不能进日志或对话
        masked = webhook.mask("https://qyapi.weixin.qq.com/send?key=abcdef1234567890")
        self.assertNotIn("abcdef1234567890", masked)
        self.assertIn("abcd", masked)
        masked2 = webhook.mask("https://oapi.dingtalk.com/send?access_token=abcdef1234567890")
        self.assertNotIn("abcdef1234567890", masked2)

    def test_feishu_secret_lives_in_the_path_not_the_query(self):
        """三家的密钥不在同一个位置，而原来只遮了带 `=` 的那两种。

        飞书/Lark 的凭证是路径最后一段（`/hook/<uuid>`），一个 `=` 都没有——
        于是每跑一次 nudge/push 就把完整的群机器人地址明文打出来，
        而这些输出会被 Agent 平台原样贴给用户。拿到它的人能直接往那个群发消息。
        """
        secret = "8f3b2c1d-4e5a-6b7c-8d9e-0f1a2b3c4d5e"  # 脱敏例外：造的 webhook secret
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/" + secret
        masked = webhook.mask(url)
        self.assertNotIn(secret, masked)
        self.assertIn("open.feishu.cn", masked)      # 还得看得出是发给谁的

    def test_a_path_secret_that_is_not_a_uuid_is_masked_too(self):
        """不能只靠"长得像 UUID"这条兜底——路径型密钥不都是 UUID 形状。"""
        secret = "aB3xQ9zK7mN2pR5tV8wY1cE4"
        masked = webhook.mask("https://open.feishu.cn/open-apis/bot/v2/hook/" + secret)
        self.assertNotIn(secret, masked)

    def test_masking_keeps_the_endpoint_readable(self):
        """遮蔽的目的是"认得出目标、拿不走凭证"。把 /robot/send 的 send 也遮了，
        就看不出这条是发给哪家的了。"""
        masked = webhook.mask(
            "https://oapi.dingtalk.com/robot/send?access_token=abcdef1234567890")
        self.assertIn("/robot/send", masked)
        self.assertNotIn("abcdef1234567890", masked)

    def test_short_secret_fully_hidden(self):
        # 短到掐头去尾还能猜出来的，直接全遮
        self.assertIn("***", webhook.mask("https://qyapi.weixin.qq.com/send?key=short"))

    def test_oversize_is_truncated_with_notice(self):
        payload = webhook.build_payload("wecom", "T", "x" * 9000)
        body = payload["markdown"]["content"]
        self.assertLess(len(body), 9000)
        self.assertIn("截断", body)          # 截断了要说，不能悄悄少发一半


class TestEmptyPeriodFieldIsDisclosed(unittest.TestCase):
    """周期字段为空的行必须被说出来。

    实测：跟进记录 7 条，报表写「跟进次数 5」——2 条因「跟进时间」为空
    被服务端过滤掉了，代码根本看不见。读报表的人只会以为本来就是 5 次。
    references 里当时还写着"不会被静默丢弃、会标出条数"——文档承诺了
    实现没有的能力，比没写更糟。
    """

    def _render(self, gap):
        """用真的 build_section 造板块，避免测试对着一个我猜出来的结构跑。"""
        rows = [{"_w1": "a"}, {"_w1": "b"}]
        section = {"title": "跟进记录", "metrics": [{"label": "跟进次数", "agg": "count"}]}
        resolve = build_report.make_resolver(
            {"跟进内容": {"name": "_w1", "label": "跟进内容", "type": "text"}})
        sec = build_section(rows, [], section, resolve)
        sec["excluded_empty"] = gap
        now = datetime.datetime(2026, 8, 28, tzinfo=datetime.timezone.utc)
        return build_report.render({"name": "T"}, (now, now, now, now), [sec], now)

    def test_gap_is_stated(self):
        out = self._render({"count": 2, "capped": False})
        self.assertIn("2 条", out)
        self.assertIn("未纳入本报表任何统计", out)

    def test_no_gap_stays_quiet(self):
        self.assertNotIn("未纳入", self._render({"count": 0, "capped": False}))

    def test_capped_says_at_least(self):
        # 探测有上限，超过就不能报一个确切数字冒充全部
        self.assertIn("至少", self._render({"count": 500, "capped": True}))

    def test_probe_failure_is_not_reported_as_zero(self):
        # 探测失败时是 None，不能当成"没有被排除的行"
        self.assertNotIn("未纳入", self._render(None))


class TestFlattenTablesForChatBots(unittest.TestCase):
    """群机器人不支持 Markdown 表格。

    企微只认标题/加粗/链接/行内代码/引用/字体颜色，钉钉同理，飞书这边走纯文本。
    报表是表格密集的，原样推过去就是一屏竖线——接口返回 200、脚本报「已发送」，
    群里那条消息却没法看。是"成功了但没用"，比失败更难发现。
    """

    MD = ("## 线索\n\n"
          "| 指标 | 本期 | 上期 | 环比 |\n"
          "|---|---:|---:|---|\n"
          "| 新增线索 | **4** | 0 | +4 |\n\n"
          "正文照旧。\n")

    def test_table_becomes_one_line_per_row(self):
        out = webhook.flatten_tables(self.MD)
        self.assertNotIn("|", out)
        self.assertIn("· 新增线索：本期 **4**，上期 0，环比 +4", out)

    def test_non_table_text_untouched(self):
        out = webhook.flatten_tables(self.MD)
        self.assertIn("## 线索", out)
        self.assertIn("正文照旧。", out)

    def test_separator_row_dropped(self):
        self.assertNotIn("---", webhook.flatten_tables(self.MD))

    def test_empty_cells_skipped(self):
        md = "| 指标 | 本期 | 上期 |\n|---|---|---|\n| 记录数 | 5 | — |\n"
        out = webhook.flatten_tables(md)
        self.assertIn("本期 5", out)
        self.assertNotIn("上期", out)          # 「—」不值得占一句话

    def test_payload_uses_flattened_by_default(self):
        body = webhook.build_payload("wecom", "T", self.MD)["markdown"]["content"]
        self.assertNotIn("|", body)

    def test_keep_tables_opt_out(self):
        body = webhook.build_payload("wecom", "T", self.MD,
                                  keep_tables=True)["markdown"]["content"]
        self.assertIn("| 新增线索 |", body)


class TestReportTimezoneActuallyApplies(unittest.TestCase):
    """报表定义里的 tz 必须真的生效。

    原来 main() 写死 +08:00，页脚却回显 cfg["tz"]——配了 utc 的报表，
    周期按北京时间切、页脚说是 UTC。数字看着合理，边界差了整整 8 小时，
    而且没有任何迹象；对"本周""上月"这类周期，边界差 8 小时就是几条记录的进出。
    """

    def test_config_tz_is_parsed_not_ignored(self):
        self.assertEqual(build_report.report_tz({"tz": "utc"}), datetime.timezone.utc)
        self.assertEqual(build_report.report_tz({"tz": "+00:00"}), datetime.timezone.utc)
        self.assertEqual(build_report.report_tz({}), DEFAULT_TZ)
        with self.assertRaises(ValueError):
            build_report.report_tz({"tz": "北京时间"})

    def test_period_boundaries_follow_the_configured_tz(self):
        now = datetime.datetime(2026, 8, 28, 15, 30, tzinfo=DEFAULT_TZ)
        beijing = resolve_period({"range": "this_week"}, now=now,
                                 tz=build_report.report_tz({}))
        utc = resolve_period({"range": "this_week"}, now=now,
                             tz=build_report.report_tz({"tz": "utc"}))
        self.assertNotEqual(beijing[0], utc[0])       # 切在不同的时刻，这正是重点

    def test_footer_label_comes_from_the_time_it_prints(self):
        """页脚的时区标签由实际生成时间算出，抄不错也撒不了谎。"""
        self.assertEqual(
            build_report.tz_label(datetime.datetime(2026, 8, 28, tzinfo=DEFAULT_TZ)), "+08:00")
        self.assertEqual(
            build_report.tz_label(datetime.datetime(2026, 8, 28,
                                                    tzinfo=datetime.timezone.utc)), "+00:00")
        west = datetime.timezone(datetime.timedelta(hours=-4, minutes=-30))
        self.assertEqual(
            build_report.tz_label(datetime.datetime(2026, 8, 28, tzinfo=west)), "-04:30")


if __name__ == "__main__":
    unittest.main(verbosity=2)
