# -*- coding: utf-8 -*-
"""jdy-query：聚合口径与 HTML 生成。

重点不是"图好不好看"，是**数字对不对、少给了有没有说**。
"""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))
_SCRIPTS = os.path.join(ROOT, "skills", "jdy-query", "scripts")


def _load(name, filename):
    sys.path.insert(0, _SCRIPTS)
    try:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_SCRIPTS, filename))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(_SCRIPTS)


from jdy_client import build_filter, dwidth  # noqa: E402

chart = _load("jdyquery_chart", "chart.py")
query = _load("jdyquery_query", "query.py")

BY_LABEL = {"职务": {"name": "_w1", "label": "职务", "type": "text"},
            "定价": {"name": "_w2", "label": "定价", "type": "number"}}
BY_NAME = {w["name"]: w for w in BY_LABEL.values()}


class TestFilter(unittest.TestCase):

    def test_display_name_resolves_to_widget_id(self):
        f = build_filter("职务=CEO", BY_LABEL, BY_NAME)
        self.assertEqual(f["cond"][0]["field"], "_w1")

    def test_unknown_field_raises(self):
        # 简道云对不认识的字段既不报错也不过滤，会返回全表——必须自己拦
        with self.assertRaises(ValueError):
            build_filter("没这列=1", BY_LABEL, BY_NAME)

    def test_bad_method_raises(self):
        with self.assertRaises(ValueError):
            build_filter("职务:漂亮:CEO", BY_LABEL, BY_NAME)

    def test_empty_method_takes_no_value(self):
        f = build_filter("职务:empty:", BY_LABEL, BY_NAME)
        self.assertNotIn("value", f["cond"][0])


class TestAggregate(unittest.TestCase):

    ROWS = [{"_w1": "CEO", "_w2": 10}, {"_w1": "CEO", "_w2": 20},
            {"_w1": "", "_w2": 5}, {"_w1": "部长", "_w2": "不是数字"}]

    def test_count_by_group(self):
        got = dict(query.aggregate(self.ROWS, BY_LABEL, "职务", "count", None))
        self.assertEqual(got, {"CEO": 2, "(未填)": 1, "部长": 1})

    def test_empty_group_is_kept_not_dropped(self):
        # 空值是"大部分没人填"这个事实，丢掉它等于隐瞒
        got = dict(query.aggregate(self.ROWS, BY_LABEL, "职务", "count", None))
        self.assertIn("(未填)", got)

    def test_sum_skips_non_numeric(self):
        got = dict(query.aggregate(self.ROWS, BY_LABEL, "职务", "sum", "定价"))
        self.assertEqual(got["CEO"], 30)
        self.assertEqual(got["部长"], 0)     # 非数值跳过，不当成 0 参与平均

    def test_avg_denominator_excludes_non_numeric(self):
        rows = [{"_w1": "A", "_w2": 10}, {"_w1": "A", "_w2": "x"}]
        got = dict(query.aggregate(rows, BY_LABEL, "职务", "avg", "定价"))
        self.assertEqual(got["A"], 10)       # 分母是 1 不是 2

    def test_sorted_desc(self):
        pairs = query.aggregate(self.ROWS, BY_LABEL, "职务", "count", None)
        self.assertEqual([v for _k, v in pairs], sorted(
            [v for _k, v in pairs], reverse=True))


class TestChart(unittest.TestCase):

    def test_empty_data_does_not_crash(self):
        self.assertIn("没有数据", chart.bar_chart([]))

    def test_negative_values_marked(self):
        self.assertIn("neg", chart.bar_chart([("甲", 5), ("乙", -3)]))

    def test_labels_escaped(self):
        svg = chart.bar_chart([("<script>", 1)])
        self.assertNotIn("<script>", svg)

    def test_cjk_label_truncated_by_display_width(self):
        # 中文占两列，按字符数截会溢出图表
        out = chart.truncate("这是一个很长很长很长的中文标签", 12)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(dwidth(out), 12)

    def test_one_ruler_for_width(self):
        """宽度只有内核那一把尺子。

        chart.py 原来自带**两套**规则：判断"要不要截"按码点区间猜，
        决定"截到哪"按"非 ASCII 就算两列"——两把尺子对不上，
        而这条测试当时断言的正是其中错的那一把。
        """
        self.assertFalse(hasattr(chart, "_dwidth"))
        for text in ("abc", "中文字", "ｆｕｌｌ", "café", "混合abc中文"):
            out = chart.truncate(text, 6)
            self.assertLessEqual(dwidth(out), 6, text)

    def test_table_says_when_truncated(self):
        rows = [[i] for i in range(300)]
        out = chart.table(["列"], rows, limit=200)
        self.assertIn("共 300 行", out)      # 少给了必须说

    def test_page_is_self_contained(self):
        html = chart.page("标题", [chart.bar_chart([("甲", 1)])])
        head = html.split("</style>")[0]
        self.assertNotIn("http", head)       # 不引任何外部资源
        self.assertIn("prefers-color-scheme", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
