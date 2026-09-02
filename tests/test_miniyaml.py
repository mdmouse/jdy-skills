# -*- coding: utf-8 -*-
"""miniyaml 测试。

重点不是"能解析常见写法"，而是**边界清楚**：支持的必须对，不支持的必须报错，
绝不静默猜错——配置解析错会让报表算出看似合理实则错误的数字。

    python3 tests/test_miniyaml.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_shared"))

from miniyaml import YamlError, parse, yaml_quote  # noqa: E402


class TestScalars(unittest.TestCase):
    def test_type_inference(self):
        got = parse("a: 1\nb: 1.5\nc: true\nd: false\ne: null\nf: ~\ng: hello")
        self.assertEqual(got, {"a": 1, "b": 1.5, "c": True, "d": False,
                               "e": None, "f": None, "g": "hello"})

    def test_quoted_stays_string(self):
        got = parse('a: "1"\nb: \'true\'\nc: "2026-08-27"')
        self.assertEqual(got, {"a": "1", "b": "true", "c": "2026-08-27"})

    def test_chinese_and_spaces(self):
        got = parse("标题: 销售周报\n说明: 含环比 与 同比")
        self.assertEqual(got, {"标题": "销售周报", "说明": "含环比 与 同比"})

    def test_negative_and_exponent(self):
        self.assertEqual(parse("a: -3\nb: 1e3\nc: -0.5"), {"a": -3, "b": 1000.0, "c": -0.5})


class TestComments(unittest.TestCase):
    def test_full_line_and_trailing(self):
        got = parse("# 头部注释\na: 1  # 行尾注释\n\nb: 2")
        self.assertEqual(got, {"a": 1, "b": 2})

    def test_hash_inside_quotes_kept(self):
        got = parse('a: "颜色 #ff0000"')
        self.assertEqual(got, {"a": "颜色 #ff0000"})


class TestNesting(unittest.TestCase):
    def test_nested_map(self):
        got = parse("period:\n  field: 创建时间\n  range: last_7_days\n  compare: previous")
        self.assertEqual(got, {"period": {"field": "创建时间", "range": "last_7_days",
                                          "compare": "previous"}})

    def test_list_of_scalars(self):
        got = parse("dims:\n  - 客户\n  - 产品")
        self.assertEqual(got, {"dims": ["客户", "产品"]})

    def test_list_of_maps(self):
        got = parse("metrics:\n  - label: 订单数\n    agg: count\n  - label: 金额\n    agg: sum\n    field: 订单总额")
        self.assertEqual(got, {"metrics": [{"label": "订单数", "agg": "count"},
                                           {"label": "金额", "agg": "sum", "field": "订单总额"}]})

    def test_deep_mix(self):
        text = ("name: 销售周报\n"
                "sections:\n"
                "  - title: 订单概览\n"
                "    entry: abc123\n"
                "    metrics:\n"
                "      - label: 订单数\n"
                "        agg: count\n"
                "    top: 5\n")
        got = parse(text)
        self.assertEqual(got["sections"][0]["metrics"][0]["label"], "订单数")
        self.assertEqual(got["sections"][0]["top"], 5)

    def test_empty_map_value(self):
        got = parse("push:\nname: x")
        self.assertEqual(got, {"push": {}, "name": "x"})


class TestFlow(unittest.TestCase):
    def test_inline_map(self):
        got = parse("m: {label: 订单数, agg: count}")
        self.assertEqual(got, {"m": {"label": "订单数", "agg": "count"}})

    def test_inline_list(self):
        self.assertEqual(parse("d: [客户, 产品, 区域]"), {"d": ["客户", "产品", "区域"]})

    def test_list_of_inline_maps(self):
        got = parse("metrics:\n  - {label: 订单数, agg: count}\n  - {label: 金额, agg: sum, field: 总额}")
        self.assertEqual(got["metrics"][1], {"label": "金额", "agg": "sum", "field": "总额"})

    def test_comma_inside_quotes(self):
        got = parse('m: {label: "订单数, 含退单", agg: count}')
        self.assertEqual(got["m"]["label"], "订单数, 含退单")

    def test_nested_flow(self):
        self.assertEqual(parse("a: {b: [1, 2], c: {d: 3}}"),
                         {"a": {"b": [1, 2], "c": {"d": 3}}})


class TestBlockScalar(unittest.TestCase):
    def test_literal_keeps_newlines(self):
        got = parse("desc: |\n  第一行\n  第二行\nname: x")
        self.assertEqual(got["desc"], "第一行\n第二行")
        self.assertEqual(got["name"], "x")

    def test_folded_joins(self):
        got = parse("desc: >\n  第一行\n  第二行\nname: x")
        self.assertEqual(got["desc"], "第一行 第二行")


class TestFrontmatter(unittest.TestCase):
    """同一个解析器也要能吃 SKILL.md 的 frontmatter。"""

    def test_skill_frontmatter(self):
        text = ("---\n"
                "name: hello-jdy\n"
                "description: |\n"
                "  平台探针。\n"
                "  触发词：跑一下简道云探针、hello-jdy。\n"
                "license: Apache-2.0\n"
                "---\n")
        got = parse(text)
        self.assertEqual(got["name"], "hello-jdy")
        self.assertIn("触发词", got["description"])
        self.assertEqual(got["license"], "Apache-2.0")


class TestErrorsNotSilentGuesses(unittest.TestCase):
    """不支持的特性必须报错。配置解析错会算出看似合理实则错误的数字。"""

    def test_anchor_rejected(self):
        with self.assertRaises(YamlError):
            parse("base: &a\n  x: 1")

    def test_alias_rejected(self):
        with self.assertRaises(YamlError):
            parse("a: 1\nb: *base")

    def test_tag_rejected(self):
        with self.assertRaises(YamlError):
            parse("!!python/object: x")

    def test_line_without_colon_rejected(self):
        with self.assertRaises(YamlError):
            parse("a: 1\n这行没有冒号")

    def test_bad_indent_rejected(self):
        with self.assertRaises(YamlError):
            parse("a: 1\n    b: 2")

    def test_flow_map_without_colon_rejected(self):
        with self.assertRaises(YamlError):
            parse("m: {label 订单数}")


class TestEdges(unittest.TestCase):
    def test_empty_input(self):
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("\n# 只有注释\n"))

    def test_top_level_list(self):
        self.assertEqual(parse("- a\n- b"), ["a", "b"])

    def test_value_containing_colon(self):
        got = parse('url: https://api.jiandaoyun.com/api/v5')
        self.assertEqual(got["url"], "https://api.jiandaoyun.com/api/v5")

    def test_second_document_ignored(self):
        self.assertEqual(parse("a: 1\n---\nb: 2"), {"a": 1})


class TestColonNeedsWhitespace(unittest.TestCase):
    """冒号要成为键值分界，后面必须跟空白或行尾——这是 YAML 自己的规则。

    原来只要行里有冒号就当映射，于是 `- http://a.com` 被解析成
    `{"http": "//a.com"}`：一个 URL 变成了一个键，而且不报错。
    报表定义里放个链接（推送地址、文档链接）就会踩到。
    """

    def test_url_in_a_list_stays_a_string(self):
        self.assertEqual(parse("- http://a.com\n- https://b.com/x?y=1"),
                         ["http://a.com", "https://b.com/x?y=1"])

    def test_url_as_a_value_still_works(self):
        self.assertEqual(parse("url: http://a.com/p:q"), {"url": "http://a.com/p:q"})

    def test_list_of_maps_still_parses(self):
        self.assertEqual(parse("- label: 记录数\n  agg: count"),
                         [{"label": "记录数", "agg": "count"}])

    def test_time_like_value_is_not_split_twice(self):
        self.assertEqual(parse("at: 09:00"), {"at": "09:00"})

    def test_colon_without_space_is_an_error_not_a_guess(self):
        # 支持的必须对，不支持的必须报错——绝不静默猜
        with self.assertRaises(YamlError):
            parse("a:1")

    def test_quoted_key_containing_a_colon(self):
        self.assertEqual(parse('"金额: 含税": 100'), {"金额: 含税": 100})

    def test_flow_map_requires_a_space_after_the_colon(self):
        self.assertEqual(parse("{a: 1, b: 2}"), {"a": 1, "b": 2})
        with self.assertRaises(YamlError):
            parse("{a:1}")


class TestYamlQuoteRoundTrips(unittest.TestCase):
    """生成侧的对偶：写出去的配置必须自己解析得回来。

    字段显示名是用户在简道云界面里随手起的，「金额(元)：含税」「A, B」
    这种名字很常见。生成器不加引号就会产出一份看着正常、一跑就错的草稿。
    """

    NASTY = ["金额: 含税", "备注 # 内部", "A, B", "带\"引号\"", "带'单引号'", " 前后空 ",
             "2026", "on", "null", "- 开头", "{花括号}", "[方括号]"]

    def test_every_nasty_name_survives_as_a_key(self):
        for name in self.NASTY:
            text = "%s: v" % yaml_quote(name)
            self.assertEqual(parse(text), {name: "v"}, text)

    def test_every_nasty_name_survives_as_a_flow_value(self):
        for name in self.NASTY:
            text = "{label: %s}" % yaml_quote(name)
            self.assertEqual(parse(text), {"label": name}, text)
            text = "d: [%s]" % yaml_quote(name)
            self.assertEqual(parse(text), {"d": [name]}, text)

    def test_a_name_with_both_quote_kinds_raises_instead_of_corrupting(self):
        """本子集只剥外层引号、不处理转义，所以这种名字表达不了。
        报错好过写出一份自己解析不回来的配置。"""
        with self.assertRaises(YamlError):
            yaml_quote("""他说"行"，我说'不'""")

    def test_ordinary_names_are_left_alone(self):
        for name in ("客户名称", "跟进人", "amount_total"):
            self.assertEqual(yaml_quote(name), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
