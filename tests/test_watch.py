# -*- coding: utf-8 -*-
"""jdy-watch：规则、命中判定、去重状态。

哨兵出错的方式有两种，方向相反、都很安静：

  · **刷屏** —— 同一条记录每轮都报。用户会把它关掉，然后它等于不存在。
  · **漏报** —— 该报的没报。用户以为在盯着，其实没盯，这比不装哨兵更糟。

所以这里主要测的是去重那条线：什么时候该压住、什么时候必须放行。

    python3 tests/test_watch.py
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))

sys.path.insert(0, os.path.join(ROOT, "tests"))
from _fixtures import unwritable_path  # noqa: E402

_SCRIPTS = os.path.join(ROOT, "skills", "jdy-watch", "scripts")


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


rules = _load("watch_rules", "rules.py")
from miniyaml import parse as parse_yaml  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
BY_LABEL = {"产品名称": {"name": "_w_n", "label": "产品名称", "type": "text"},
            "数量": {"name": "_w_q", "label": "数量", "type": "number"}}
ROW = lambda i, name="甲", qty=3: {"_id": "%024x" % i, "_w_n": name, "_w_q": qty}


def fresh_state():
    return rules.State(os.path.join(tempfile.mkdtemp(), "state.json"))


class TestConfigValidation(unittest.TestCase):
    """看不懂的写法一律报错。一条被静默忽略的规则 =
    一个用户以为在盯着、其实没盯的指标。"""

    def _write(self, text):
        path = os.path.join(tempfile.mkdtemp(), "r.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_minimal_valid_config(self):
        cfg = rules.load_config(self._write(
            "rules:\n  - name: 甲\n    app: A\n    entry: E\n    when: 数量:lt:10\n"),
            parse_yaml)
        self.assertEqual(len(cfg["rules"]), 1)

    def test_rules_must_exist(self):
        for text in ("name: x\n", "rules: []\n"):
            with self.assertRaises(rules.RuleError):
                rules.load_config(self._write(text), parse_yaml)

    def test_missing_target_is_refused(self):
        with self.assertRaises(rules.RuleError):
            rules.load_config(self._write("rules:\n  - name: 甲\n    app: A\n"), parse_yaml)

    def test_a_rule_that_watches_nothing_is_refused(self):
        """既没有 when 也没有 new_rows——不知道要盯什么，不能装作在盯。"""
        with self.assertRaises(rules.RuleError) as cm:
            rules.load_config(self._write(
                "rules:\n  - name: 甲\n    app: A\n    entry: E\n"), parse_yaml)
        self.assertIn("不知道要盯什么", str(cm.exception))

    def test_duplicate_rule_names_are_refused(self):
        """去重状态按规则名存，重名会让两条规则互相吃掉对方的状态。"""
        with self.assertRaises(rules.RuleError) as cm:
            rules.load_config(self._write(
                "rules:\n  - name: 甲\n    app: A\n    entry: E\n    when: a=1\n"
                "  - name: 甲\n    app: B\n    entry: F\n    when: b=2\n"), parse_yaml)
        self.assertIn("重名", str(cm.exception))

    def test_bad_cooldown_is_refused(self):
        with self.assertRaises(rules.RuleError):
            rules.load_config(self._write(
                "rules:\n  - name: 甲\n    app: A\n    entry: E\n    when: a=1\n"
                "    remind_after_hours: 一天\n"), parse_yaml)


class TestThresholdRule(unittest.TestCase):
    """阈值型：当前满足条件就报，但同一条不能每轮都报。"""

    RULE = {"name": "存量告警", "app": "A", "entry": "E", "when": "数量:lt:10",
            "message": "{产品名称} 只剩 {数量}"}

    def test_first_time_everything_hits(self):
        st = fresh_state()
        got = rules.evaluate(self.RULE, [ROW(1), ROW(2)], BY_LABEL, st, NOW)
        self.assertEqual(len(got["hits"]), 2)
        self.assertFalse(got["first_run"])          # 阈值型没有"建基准"这回事

    def test_second_run_is_silent_by_default(self):
        """哨兵是定时跑的，一直命中是常态。默认只报一次。"""
        st = fresh_state()
        rules.evaluate(self.RULE, [ROW(1)], BY_LABEL, st, NOW)
        again = rules.evaluate(self.RULE, [ROW(1)], BY_LABEL, st,
                               NOW + datetime.timedelta(hours=1))
        self.assertEqual(again["hits"], [])
        self.assertEqual(len(again["suppressed"]), 1)

    def test_suppressed_count_is_reported_not_hidden(self):
        """压住了几条要说出来，否则用户以为哨兵瞎了。"""
        st = fresh_state()
        rules.evaluate(self.RULE, [ROW(1)], BY_LABEL, st, NOW)
        again = rules.evaluate(self.RULE, [ROW(1)], BY_LABEL, st, NOW)
        self.assertIn("已提醒过", again["suppressed"][0][1])

    def test_cooldown_lets_it_speak_again(self):
        rule = dict(self.RULE, remind_after_hours=24)
        st = fresh_state()
        rules.evaluate(rule, [ROW(1)], BY_LABEL, st, NOW)
        soon = rules.evaluate(rule, [ROW(1)], BY_LABEL, st,
                              NOW + datetime.timedelta(hours=23))
        later = rules.evaluate(rule, [ROW(1)], BY_LABEL, st,
                               NOW + datetime.timedelta(hours=25))
        self.assertEqual(soon["hits"], [])
        self.assertEqual(len(later["hits"]), 1)

    def test_a_row_that_recovers_can_alarm_again(self):
        """不再命中就忘掉它——否则它下次再出问题会被当成"已经提醒过"。"""
        st = fresh_state()
        rules.evaluate(self.RULE, [ROW(1)], BY_LABEL, st, NOW)
        rules.evaluate(self.RULE, [], BY_LABEL, st, NOW)          # 补货了，不再命中
        back = rules.evaluate(self.RULE, [ROW(1)], BY_LABEL, st, NOW)
        self.assertEqual(len(back["hits"]), 1)

    def test_unreadable_state_timestamp_errs_toward_repeating(self):
        """状态里的时间读不懂时**重复提醒**而不是静默跳过：
        重复是噪音，漏掉是事故。"""
        st = fresh_state()
        st.data["存量告警"] = {ROW(1)["_id"]: "看不懂的时间"}
        got = rules.evaluate(dict(self.RULE, remind_after_hours=1),
                             [ROW(1)], BY_LABEL, st, NOW)
        self.assertEqual(len(got["hits"]), 1)


class TestNewRowsRule(unittest.TestCase):
    """新增型：只报没见过的记录。"""

    RULE = {"name": "新记录", "app": "A", "entry": "E", "new_rows": True,
            "message": "{产品名称}"}

    def test_first_run_builds_a_baseline_and_stays_quiet(self):
        """第一次没有基准，把整表当成"新增"推出去是灾难——
        一张两千行的表会变成一条两千行的消息，哨兵当天就被关掉。"""
        st = fresh_state()
        got = rules.evaluate(self.RULE, [ROW(i) for i in range(50)], BY_LABEL, st, NOW)
        self.assertTrue(got["first_run"])
        self.assertEqual(got["hits"], [])
        self.assertEqual(got["scanned"], 50)

    def test_only_genuinely_new_rows_are_reported(self):
        st = fresh_state()
        rules.evaluate(self.RULE, [ROW(1), ROW(2)], BY_LABEL, st, NOW)
        got = rules.evaluate(self.RULE, [ROW(1), ROW(2), ROW(3)], BY_LABEL, st, NOW)
        self.assertEqual([h["row_id"] for h in got["hits"]], [ROW(3)["_id"]])

    def test_old_rows_are_not_reported_as_suppressed(self):
        """新增型里"见过的旧记录"不是被压住的命中，是本来就不该报的东西。
        报成"18 条被去重压住"会让人以为哨兵吞了 18 条要紧事。"""
        st = fresh_state()
        rules.evaluate(self.RULE, [ROW(i) for i in range(18)], BY_LABEL, st, NOW)
        again = rules.evaluate(self.RULE, [ROW(i) for i in range(18)], BY_LABEL, st, NOW)
        self.assertEqual(again["hits"], [])
        self.assertEqual(again["suppressed"], [])

    def test_old_rows_are_remembered_even_after_they_leave_the_query(self):
        """新增型不能忘：一条记录暂时没被拉到（limit、筛选），
        回来时不该被当成新增再报一遍。"""
        st = fresh_state()
        rules.evaluate(self.RULE, [ROW(1)], BY_LABEL, st, NOW)
        rules.evaluate(self.RULE, [], BY_LABEL, st, NOW)
        got = rules.evaluate(self.RULE, [ROW(1)], BY_LABEL, st, NOW)
        self.assertEqual(got["hits"], [])


class TestMessageTemplate(unittest.TestCase):
    def test_fields_are_substituted(self):
        got = rules.format_row("{产品名称} 只剩 {数量}", ROW(1, "扳手", 2), BY_LABEL)
        self.assertEqual(got, "扳手 只剩 2")

    def test_unknown_field_stays_visible_instead_of_becoming_blank(self):
        """空白看着像"这个字段是空的"，而实际是模板写错了名字——两件事得能区分。"""
        got = rules.format_row("{不存在的字段} 和 {产品名称}", ROW(1, "扳手"), BY_LABEL)
        self.assertIn("{不存在的字段}", got)
        self.assertIn("扳手", got)

    def test_empty_value_becomes_empty_string_not_none(self):
        row = dict(ROW(1), _w_n=None)
        self.assertEqual(rules.format_row("[{产品名称}]", row, BY_LABEL), "[]")

    def test_no_template_means_no_text(self):
        self.assertIsNone(rules.format_row(None, ROW(1), BY_LABEL))


class TestStatePersistence(unittest.TestCase):
    def test_round_trips(self):
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        st = rules.State(path)
        st.mark("甲", "id1", NOW)
        self.assertTrue(st.save())
        self.assertEqual(rules.State(path).last_notified("甲", "id1"), NOW.isoformat())

    def test_unwritable_state_degrades_to_repeating_not_to_silence(self):
        """重复是噪音，漏掉是事故——写不进去时要让调用方知道会重复。"""
        # 为什么不能再写 /proc（Windows 上那是可写的 D:\\proc\\…）：见 tests/_fixtures.py
        st = rules.State(unwritable_path("s.json"))
        st.mark("甲", "id1", NOW)
        self.assertFalse(st.save())
        self.assertTrue(st.readonly)

    def test_corrupt_state_file_does_not_crash_the_sentinel(self):
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ 这不是 json")
        self.assertEqual(rules.State(path).data, {})

    def test_recovered_rows_are_dropped_from_state(self):
        st = fresh_state()
        st.mark("甲", "id1", NOW)
        st.mark("甲", "id2", NOW)
        st.forget_missing("甲", {"id2"})
        self.assertEqual(list(st.data["甲"]), ["id2"])


class TestCorruptStateIsLoudNotSilent(unittest.TestCase):
    """哨兵最不该出的事不是报错，是**不响**。

    原来状态文件坏掉有两种结局，都不对：形状不对（顶层是列表）时
    `json.load(fh).get(...)` 直接抛 AttributeError，命令行工具甩 traceback；
    而 JSON 坏掉那一半被接住后静默 data={}，于是「新增行」类规则把这一轮
    当成首次运行——只建基准、一条都不提醒。哨兵没坏，它只是不响了。
    """

    def _state(self, body):
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_a_list_at_the_top_does_not_crash(self):
        st = rules.State(self._state("[1, 2, 3]"))
        self.assertIn("列表", st.corrupt.replace("list", "列表"))
        self.assertEqual(st.data, {})

    def test_bad_json_is_reported_not_swallowed(self):
        st = rules.State(self._state("{不是 json"))
        self.assertTrue(st.corrupt)

    def test_a_wrong_shape_inside_rules_is_caught(self):
        self.assertTrue(rules.State(self._state('{"rules": "字符串"}')).corrupt)
        self.assertTrue(rules.State(self._state('{"rules": {"甲": 5}}')).corrupt)

    def test_a_good_file_reads_clean(self):
        st = rules.State(self._state('{"rules": {"甲": {"id1": "2026-08-01T00:00:00Z"}}}'))
        self.assertIsNone(st.corrupt)
        self.assertTrue(st.seen_before("甲"))

    def test_the_broken_file_is_kept_aside_once_the_new_state_lands(self):
        """坏文件是唯一能看出"上次提醒到哪儿"的东西，save() 就要覆盖同一个路径。"""
        path = self._state("[]")
        st = rules.State(path)
        self.assertTrue(st.save())
        self.assertTrue(os.path.exists(path + ".corrupt"))

    def test_a_round_that_never_lands_keeps_the_corruption_visible(self):
        """**窄窗口，但结局是哨兵闭嘴。**

        原来是一读到损坏就立刻把坏文件改名挪开。于是这一轮如果没落盘
        （--dry-state，或者目录不可写），下一轮回来看到的是"根本没有状态文件"：
        corrupt 是 False、seen_before 也是 False，新增型规则又一次被当成
        「首次运行」——只建基准、一条都不提醒，而且这次连那句警告都没有了。
        损坏的信号被"保全证据"这个动作自己销毁了。
        """
        path = self._state("[]")
        rule = {"name": "新单", "new_rows": True}
        widgets = {"金额": {"name": "_w_a", "label": "金额", "type": "number"}}
        rows = [{"_id": "a" * 24, "_w_a": 1}]
        now = datetime.datetime(2026, 8, 31, tzinfo=datetime.timezone.utc)
        for attempt in (1, 2, 3):                 # 每轮都不 save()
            st = rules.State(path)
            self.assertTrue(st.corrupt, "第 %d 轮已经看不见损坏了" % attempt)
            got = rules.evaluate(rule, rows, widgets, st, now)
            self.assertEqual(got["kind"], "new_rows")
            self.assertFalse(got["first_run"], "第 %d 轮退回了「首次运行」" % attempt)
            self.assertEqual(len(got["hits"]), 1)

    def test_it_only_claims_the_rename_when_it_actually_happened(self):
        """契约挪了位置，话术要跟着挪。

        坏文件是**落盘成功之后**才挪的，而"已改名存到 .corrupt"那句话原来在
        决策之前就打了——于是 --dry-state 下它是个空头承诺：屏幕上说挪了，
        磁盘上什么都没发生。用户按它去找那个文件，找不到。
        """
        path = self._state("[]")
        st = rules.State(path)
        self.assertIsNone(st.kept_aside, "还没落盘就说挪走了")
        self.assertTrue(os.path.exists(path), "还没落盘就把坏文件动了")
        self.assertTrue(st.save())
        self.assertEqual(st.kept_aside, path + ".corrupt")

    def test_a_healthy_state_never_claims_a_rename(self):
        st = rules.State(self._state('{"rules": {}}'))
        st.save()
        self.assertIsNone(st.kept_aside)

    def test_once_it_lands_the_next_round_is_normal_again(self):
        path = self._state("[]")
        st = rules.State(path)
        st.mark("新单", "a" * 24, datetime.datetime(2026, 8, 31,
                                                   tzinfo=datetime.timezone.utc))
        self.assertTrue(st.save())
        st2 = rules.State(path)
        self.assertIsNone(st2.corrupt)
        self.assertTrue(st2.seen_before("新单"))

    def test_corruption_does_not_masquerade_as_a_first_run(self):
        """这是真正会漏报的那一半：损坏 → data 空 → seen_before 假 →
        新增型规则当成首次运行 → 只建基准、一条都不提醒。"""
        st = rules.State(self._state("[]"))
        rule = {"name": "新单", "new_rows": True}   # rule_kind 认的是这个键
        widgets = {"金额": {"name": "_w_a", "label": "金额", "type": "number"}}
        rows = [{"_id": "a" * 24, "_w_a": 1}]
        got = rules.evaluate(rule, rows, widgets, st,
                             datetime.datetime(2026, 8, 31, tzinfo=datetime.timezone.utc))
        # 先确认这条规则**真的**走的是新增分支，否则下面两句是空转的：
        # 第一版就栽在这儿——rule_kind 认的是 new_rows 键，我写成了 kind，
        # 于是它走了 threshold 分支，把守卫改回旧逻辑测试照样全绿。
        self.assertEqual(got["kind"], "new_rows")
        self.assertFalse(got["first_run"], "状态损坏被当成了首次运行——会整轮不提醒")
        self.assertEqual(len(got["hits"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
