# -*- coding: utf-8 -*-
"""jdy-excel-bridge 测试，覆盖计划里列的 8 类错误 Excel。

刻意也把**拦不住的**几类写成测试并断言"拦不住"——能力边界要写进测试，
不然会被误当成已解决。

    python3 tests/test_excel_bridge.py
"""
import datetime
import os
import sys
import tempfile
import shutil
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "jdy-excel-bridge", "scripts"))

from xlsx import read_rows, read_table, write_sheet  # noqa: E402
import preflight  # noqa: E402
import import_data  # noqa: E402
import export  # noqa: E402
from import_data import confirm_code  # noqa: E402
SCRIPTS = os.path.join(ROOT, "skills", "jdy-excel-bridge", "scripts")
from preflight import (build_user_index, check_cells, match_headers, normalize,  # noqa: E402
                       row_label, values_match)

W = lambda label, wtype, **kw: dict(name="_w_%s" % label, label=label, type=wtype, **kw)

FIELDS = {
    "客户名称": W("客户名称", "text"),
    "下单日期": W("下单日期", "datetime"),
    "数量": W("数量", "number"),
    "负责人": W("负责人", "user"),
    "订单编号": W("订单编号", "sn"),
    "关联客户": W("关联客户", "linkdata"),
    "备注": W("备注", "textarea"),
}


class TestHeaderNormalize(unittest.TestCase):
    def test_strips_required_marker_and_units(self):
        self.assertEqual(normalize("*客户名称"), "客户名称")
        self.assertEqual(normalize("数量（单位：件）"), "数量")
        self.assertEqual(normalize("下单日期："), "下单日期")

    def test_fullwidth_and_spaces(self):
        self.assertEqual(normalize("客 户　名称"), "客户名称")
        self.assertEqual(normalize("ＡＢＣ"), "abc")


class TestHeaderMatching(unittest.TestCase):
    def test_exact_match_wins(self):
        mapping, unmatched, _ = match_headers(["客户名称", "数量"], FIELDS)
        self.assertEqual(mapping["客户名称"]["how"], "精确匹配")
        self.assertEqual(unmatched, [])

    def test_decorated_header_matched_and_labeled(self):
        mapping, _, _ = match_headers(["*客户名称", "数量（单位：件）"], FIELDS)
        self.assertEqual(mapping["*客户名称"]["field"]["label"], "客户名称")
        self.assertIn("归一化", mapping["*客户名称"]["how"])

    def test_fuzzy_match_is_flagged_for_confirmation(self):
        """唯一的包含匹配可以采用，但要标出来让用户确认。"""
        mapping, _, ambiguous = match_headers(["数量合计"], FIELDS)
        self.assertIn("请确认", mapping["数量合计"]["how"])
        self.assertEqual(mapping["数量合计"]["field"]["label"], "数量")
        self.assertEqual(ambiguous, {})

    def test_multiple_candidates_reported_not_dropped(self):
        """「客户」同时像「客户名称」和「关联客户」——这是选择题，不是死路。"""
        mapping, unmatched, ambiguous = match_headers(["客户"], FIELDS)
        self.assertNotIn("客户", mapping)
        self.assertNotIn("客户", unmatched)
        self.assertEqual(sorted(ambiguous["客户"]), ["关联客户", "客户名称"])

    def test_unknown_column_left_unmatched(self):
        mapping, unmatched, _ = match_headers(["银行账号"], FIELDS)
        self.assertEqual(unmatched, ["银行账号"])

    def test_one_field_not_consumed_twice(self):
        """两列都想认领同一个字段时，不能双双映射过去。"""
        mapping, unmatched, _ = match_headers(["客户名称", "*客户名称"], FIELDS)
        targets = [m["field"]["name"] for m in mapping.values()]
        self.assertEqual(len(targets), len(set(targets)))


class TestEightErrorCases(unittest.TestCase):
    """计划里的 8 类错误 Excel。"""

    def setUp(self):
        self.user_index = {"张三": ["sys_zhangsan"], "李四": ["sys_lisi_a", "sys_lisi_b"]}

    def _check(self, rows):
        mapping, _, _ = match_headers(list(rows[0].keys()), FIELDS)
        issues, clean, held, _warn = check_cells(rows, mapping, self.user_index)
        return issues, [c["values"] for c in clean], held

    # --- 能拦住的 ---------------------------------------------------------

    def test_case4_mixed_date_formats(self):
        """4. 日期格式混杂：可归一的放行，真垃圾的拦下。"""
        issues, clean, held = self._check([
            {"下单日期": "2026/08/27"}, {"下单日期": "2026-08-27"},
            {"下单日期": "2026年8月27日"}, {"下单日期": "下周三"}])
        kinds = [i["kind"] for i in issues]
        self.assertEqual(kinds, ["bad_value"])              # 只有「下周三」拦下
        self.assertEqual(len(clean), 3)
        self.assertEqual(len(held), 1)                      # 坏行扣下，不混进 clean
        self.assertEqual(issues[0]["row"], 5)               # 第 4 条数据 = 表格第 5 行

    def test_case2_ambiguous_member(self):
        """2. 成员重名：绝不自动挑一个，交用户裁决。"""
        issues, _, _ = self._check([{"负责人": "李四"}])
        self.assertEqual(issues[0]["kind"], "user_ambiguous")
        self.assertIn("sys_lisi_a", issues[0]["detail"])

    def test_member_unique_name_resolved(self):
        issues, clean, held = self._check([{"负责人": "张三"}])
        self.assertEqual(issues, [])
        self.assertEqual(held, [])
        self.assertEqual(clean[0]["负责人"], "sys_zhangsan")

    def test_member_unknown_name_reported(self):
        issues, _, _ = self._check([{"负责人": "王五"}])
        self.assertEqual(issues[0]["kind"], "user_unresolved")

    def test_bad_number(self):
        issues, _, _ = self._check([{"数量": "约一百"}])
        self.assertEqual(issues[0]["kind"], "bad_value")

    def test_case1_and_7_unwritable_columns_excluded_from_payload(self):
        """1. 关联值不存在 / 7. 流水号冲突——这两列根本写不进去，直接排除。"""
        mapping, _, _ = match_headers(["关联客户", "订单编号", "客户名称"], FIELDS)
        _, _clean, _, _w = check_cells([{"关联客户": "示例客户A", "订单编号": "X-1", "客户名称": "甲"}],
                                  mapping, self.user_index)
        clean = [c["values"] for c in _clean]
        self.assertEqual(clean[0], {"客户名称": "甲"})

    def test_case8_oversize_file_detectable(self):
        """8. 超限：文件体积是能提前量出来的。"""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "big.xlsx")
        write_sheet(path, ["备注"], [{"备注": "x" * 2000} for _ in range(200)])
        self.assertGreater(os.path.getsize(path), 0)
        self.assertLess(os.path.getsize(path), 20 * 1024 * 1024)

    # --- 拦不住的：能力边界，写进测试免得被当成已解决 -----------------------

    def test_case5_required_field_NOT_detectable(self):
        """5. 必填缺失——拦不住。widget/list 不返回必填标记，拿不到这个信息。"""
        issues, clean, _ = self._check([{"客户名称": None, "数量": 1}])
        self.assertEqual(issues, [])
        self.assertNotIn("客户名称", clean[0])

    def test_case6_long_text_NOT_detectable(self):
        """6. 超长文本——拦不住。接口不返回字段长度上限，无从校验。"""
        issues, clean, _ = self._check([{"备注": "很长" * 5000}])
        self.assertEqual(issues, [])
        self.assertIn("备注", clean[0])

    def test_case3_subform_row_mismatch_NOT_handled(self):
        """3. 子表单行错乱——本版不支持。扁平 Excel 无法表达子表单的行归属，
        需要「主表 + 子表两张 sheet + 关联键」的约定，留待后续版本。"""
        self.assertNotIn("subform", {w["type"] for w in FIELDS.values()})


class TestUserIndex(unittest.TestCase):
    class FakeClient(object):
        def __init__(self, rows):
            self.rows = rows

        def fetch_all(self, *a, **kw):
            return self.rows

    def test_builds_name_to_username_and_keeps_duplicates(self):
        rows = [{"_w_负责人": {"name": "张三", "username": "sys_a"}},
                {"_w_负责人": {"name": "李四", "username": "sys_b"}},
                {"_w_负责人": {"name": "李四", "username": "sys_c"}}]
        idx = build_user_index(rows, [FIELDS["负责人"]])
        self.assertEqual(idx["张三"], ["sys_a"])
        self.assertEqual(idx["李四"], ["sys_b", "sys_c"])   # 重名全留，不擅自选


class TestEndToEndOffline(unittest.TestCase):
    def test_dirty_excel_roundtrip_through_reader(self):
        """把 8 类脏数据写成真 xlsx，再读回来走一遍预检。"""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "dirty.xlsx")
        headers = ["*客户名称", "下单日期", "数量", "负责人", "关联客户", "银行账号"]
        rows = [
            {"*客户名称": "甲公司", "下单日期": "2026/08/27", "数量": 10,
             "负责人": "张三", "关联客户": "示例客户A", "银行账号": "6222"},
            {"*客户名称": "乙公司", "下单日期": "下周三", "数量": "约一百",
             "负责人": "李四", "关联客户": "别的客户", "银行账号": "6223"},
        ]
        write_sheet(path, headers, rows)
        got_headers, records = read_table(path)
        self.assertEqual(got_headers, headers)

        mapping, unmatched, _ = match_headers(got_headers, FIELDS)
        self.assertEqual(unmatched, ["银行账号"])            # 表单里没有的列
        issues, _clean, held, _warn = check_cells(records, mapping,
                                          {"张三": ["sys_zhangsan"], "李四": ["sys_a", "sys_b"]})
        clean = [c["values"] for c in _clean]
        kinds = sorted(i["kind"] for i in issues)
        self.assertEqual(kinds, ["bad_value", "bad_value", "user_ambiguous"])
        self.assertEqual(len(clean), 1)                     # 干净的只有第 1 行
        self.assertEqual(len(held), 1)                      # 第 2 行三处问题，整行扣下
        self.assertNotIn("关联客户", clean[0])                # linkdata 不进 payload
        self.assertEqual(clean[0]["负责人"], "sys_zhangsan")


class TestNonInteractiveSafety(unittest.TestCase):
    """Agent 平台里 stdin 从来不是 tty。二次确认不能因为"问不了"就消失。"""

    def _plan_and_env(self):
        import json
        tmp = tempfile.mkdtemp()
        plan = os.path.join(tmp, "plan.json")
        with open(plan, "w", encoding="utf-8") as fh:
            json.dump({"app_id": "APP", "entry_id": "ENTRY", "tz": "+08:00",
                       "rows": [{"名称": "甲"}], "held_rows": [], "issues": []}, fh)
        script = os.path.join(ROOT, "skills", "jdy-excel-bridge", "scripts", "import_data.py")
        # 密钥要自己给：这条闸门在建 client 之后、任何网络请求之前。没有 Key 的机器
        # （CI）会在建 client 那一步就以 2 退出，根本走不到闸门——本机有真 Key，
        # 所以这个依赖一直没露出来。给一把假 Key、把 JDY_HOME 指到空目录，
        # 既不碰本机配置，也证明闸门在拿到 Key 之后照样拦。
        env = dict(os.environ, JDY_API_KEY="dummy-key-for-tty-gate", JDY_HOME=tmp)
        return script, plan, env

    def _assert_refused(self, returncode, stderr):
        self.assertEqual(returncode, 4,
                         "非交互下必须拒绝写入（stderr：%s）" % stderr.strip())
        self.assertIn("拒绝写入", stderr)
        self.assertIn("--yes", stderr)                 # 要告诉调用方正确做法

    def test_execute_without_yes_is_refused_when_not_a_tty(self):
        import subprocess
        script, plan, env = self._plan_and_env()
        result = subprocess.run([sys.executable, script, plan, "--execute"],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                env=env, encoding="utf-8", errors="replace")
        self._assert_refused(result.returncode, result.stderr)

    @unittest.skipIf(os.name == "nt", "pty 是 POSIX 专有；这条是在 POSIX 上**复现 Windows**")
    def test_a_tty_that_reads_eof_is_refused_too(self):
        """把 Windows 的 `NUL` 搬到本机上跑一遍。

        上面那条用 `stdin=subprocess.DEVNULL`，在 POSIX 上是 /dev/null——
        `isatty()` 老实说 False，走的是"不是 tty"那一支。**Windows 不一样**：
        同样的 DEVNULL 给的是 `NUL`，一个**字符设备**，`isatty()` 返回 True，
        可它一读就 EOF。于是手写的 `if sys.stdin.isatty(): input(...)` 在那边
        走进 input()、抛 EOFError，脚本以退出码 1 带着 traceback 死掉——
        首次 Windows CI 上这条测试拿到的就是 `rc=1` + `EOFError: EOF when reading a line`。

        本机造得出同样的东西：开一个 pty，**立刻关掉 master 端**，
        slave 端依然是货真价实的 tty（isatty True），而 read() 立即返回 EOF。
        所以这条不是"模拟"，它就是那个条件本身，只是换了个来源。
        把 jdy_client.ask_yes 里的 `except EOFError` 删掉，这条立刻回到 rc=1。
        """
        import pty
        import subprocess
        script, plan, env = self._plan_and_env()
        master, slave = pty.openpty()
        os.close(master)                     # 写端一关，slave 上 read() 立即 EOF
        try:
            proc = subprocess.Popen([sys.executable, script, plan, "--execute"],
                                    stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
            _out, err = proc.communicate(timeout=60)
        finally:
            os.close(slave)
        self._assert_refused(proc.returncode, err.decode("utf-8", "replace"))




class TestIdRoundTrip(unittest.TestCase):
    """`_id` 列的回写定位。

    export.py 一直在导 `_id` 并注明「供后续回写定位」，但导入侧从来没读过它——
    改完导回去会新增一批重复行，而不是更新原记录。导出承诺了一个不存在的能力，
    这比缺功能更糟：用户按提示做，拿到的是重复数据。
    """

    MAPPING = {"姓名": {"field": {"name": "_w1", "label": "姓名", "type": "text"},
                        "how": "精确"}}
    REAL = "a" * 24

    def _run(self, records, id_check=None):
        issues, clean, held, _warn = check_cells(records, dict(self.MAPPING), {},
                                                 id_check=id_check)
        return issues, clean, held

    def test_blank_id_is_create(self):
        _, clean, _ = self._run([{"_id": "", "姓名": "甲"}])
        self.assertIsNone(clean[0]["data_id"])
        self.assertEqual(clean[0]["values"], {"姓名": "甲"})

    def test_missing_id_column_is_create(self):
        _, clean, _ = self._run([{"姓名": "甲"}])
        self.assertIsNone(clean[0]["data_id"])

    def test_valid_id_is_update(self):
        _, clean, _ = self._run([{"_id": self.REAL, "姓名": "甲"}],
                                id_check=lambda d: True)
        self.assertEqual(clean[0]["data_id"], self.REAL)

    def test_id_never_written_as_field(self):
        _, clean, _ = self._run([{"_id": self.REAL, "姓名": "甲"}],
                                id_check=lambda d: True)
        self.assertNotIn("_id", clean[0]["values"])

    def test_malformed_id_held_not_created(self):
        # 关键：坏 _id 不能降级成新增——那会静默产生一条重复记录
        issues, clean, held = self._run([{"_id": "看起来像个ID", "姓名": "甲"}])
        self.assertEqual(clean, [])
        self.assertEqual(held[0]["issues"][0]["kind"], "bad_data_id")

    def test_nonexistent_id_held_not_created(self):
        issues, clean, held = self._run([{"_id": self.REAL, "姓名": "甲"}],
                                        id_check=lambda d: False)
        self.assertEqual(clean, [])
        self.assertEqual(held[0]["issues"][0]["kind"], "data_id_missing")

    def test_unknown_existence_does_not_block(self):
        # 拉不到已有 ID 集合时（id_check 返回 None）不该把预检卡死
        _, clean, held = self._run([{"_id": self.REAL, "姓名": "甲"}],
                                   id_check=lambda d: None)
        self.assertEqual(len(clean), 1)
        self.assertEqual(held, [])

    def test_mixed_file_splits(self):
        # 真实用法：改几行 + 加一行新的
        _, clean, _ = self._run([{"_id": self.REAL, "姓名": "改过的"},
                                 {"_id": "", "姓名": "新增的"}],
                                id_check=lambda d: True)
        self.assertEqual([c["data_id"] for c in clean], [self.REAL, None])


class TestSkipUnchanged(unittest.TestCase):
    """没改的行不该重发更新。

    实测：导出 23 行、只改 2 行、整份导回，预检说要更新 21 行。
    另外 19 行的写入零收益，却各带一次静默丢字段的风险。
    """

    BY_LABEL = {"姓名": {"name": "_w1", "label": "姓名", "type": "text"},
                "手机": {"name": "_w2", "label": "手机", "type": "phone"}}

    def test_identical_row_matches(self):
        stored = {"_w1": "张三", "_w2": {"verified": False, "phone": "13800000000"}}
        self.assertTrue(values_match(stored, {"姓名": "张三", "手机": "13800000000"},
                                     self.BY_LABEL))

    def test_changed_value_does_not_match(self):
        stored = {"_w1": "张三", "_w2": {"verified": False, "phone": "13800000000"}}
        self.assertFalse(values_match(stored, {"姓名": "张三-已改"}, self.BY_LABEL))

    def test_phone_compared_by_display_value(self):
        # 库里存的是 {verified, phone} 结构，Excel 里是裸号码。
        # 不转显示值就会永远判为"变了"，跳过逻辑等于没有。
        stored = {"_w2": {"verified": True, "phone": "13800000000"}}
        self.assertTrue(values_match(stored, {"手机": "13800000000"}, self.BY_LABEL))

    def test_empty_and_none_are_the_same(self):
        self.assertTrue(values_match({"_w1": None}, {"姓名": ""}, self.BY_LABEL))

    def test_unknown_field_forces_write(self):
        # 字段对不上时宁可写一次，也不要静默跳过用户的修改
        self.assertFalse(values_match({}, {"没这个字段": "x"}, self.BY_LABEL))


class TestRowLabel(unittest.TestCase):
    """问题行必须带上「这行是谁」。

    实测：预检只报「第 6 行 _id 坏了」，Agent 就自己去文件里数行补充说明，
    数错一行，把第 7 行的人安到了第 6 行，然后让用户对着错的记录拍板。
    用户脑子里是名字，不是行号。
    """

    def test_takes_first_non_empty_business_values(self):
        rec = {"_id": "a" * 24, "姓名": "王先生", "职务": "", "手机": "13222222222",
               "创建时间": "2026-08-28"}
        self.assertEqual(row_label(rec), "王先生 / 13222222222")

    def test_skips_system_columns(self):
        # _id 和创建时间对人没有辨识度，不能占掉名额
        rec = {"_id": "a" * 24, "创建时间": "2026-08-28", "姓名": "李雷"}
        self.assertEqual(row_label(rec), "李雷")

    def test_empty_row_says_so(self):
        self.assertEqual(row_label({"_id": "", "姓名": "", "职务": None}), "（整行为空）")

    def test_long_values_truncated(self):
        rec = {"备注": "很长" * 40}
        self.assertLessEqual(len(row_label(rec)), 16)


class TestConfirmCodeSingleSource(unittest.TestCase):
    """dry-run 与真正执行必须算出同一个确认码。

    实测里 Agent 为了拿码，先跑了一次 `--execute --yes` 让它拒绝——
    等于安全机制在训练调用方"先试着写一次"。改成 dry-run 就给码之后，
    两处若各算各的迟早会漂移，所以只保留一处实现，这条测试盯住它。
    """

    def test_same_inputs_same_code(self):
        creates = [{"姓名": "a"}, {"姓名": "b"}]
        updates = [{"data_id": "x" * 24, "values": {}}]
        a = confirm_code("APP", "ENTRY", creates, updates)
        b = confirm_code("APP", "ENTRY", list(creates), list(updates))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 8)

    def test_code_changes_when_plan_changes(self):
        base = confirm_code("APP", "ENTRY", [{"姓名": "a"}], [])
        self.assertNotEqual(base, confirm_code("APP", "ENTRY",
                                               [{"姓名": "a"}, {"姓名": "b"}], []))
        self.assertNotEqual(base, confirm_code("APP", "OTHER", [{"姓名": "a"}], []))

    def test_reading_shapes_are_what_the_docs_claim(self):
        # read_rows → list[list]（含表头）；read_table → (headers, list[dict])
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.xlsx")
        write_sheet(path, ["姓名", "职务"], [["甲", "经理"], ["乙", "专员"]])
        rows = read_rows(path)
        self.assertEqual(rows[0], ["姓名", "职务"])
        headers, recs = read_table(path)
        self.assertEqual(headers, ["姓名", "职务"])
        self.assertEqual(recs[0]["姓名"], "甲")
        shutil.rmtree(tmp)


class TestProblemsCarryARowIdentity(unittest.TestCase):
    """报了"哪一列坏了"却指不出哪一行，等于没报。

    根因是计划的两半不对称：`updates` 带 row，`creates` 只写 values——
    行号和"这行是谁"在**预检写计划的时候**就丢了，导入阶段想拼也拼不出来。
    而 row_label() 的存在本来就是为了"行号旁边必须带上这行是谁"
    （只报行号时，实测里 Agent 自己去数行、把第 6 行说成了第 7 行的人）。
    """

    def test_plan_creates_keep_row_and_who(self):
        src = open(os.path.join(SCRIPTS, "preflight.py"), encoding="utf-8").read()
        self.assertIn('"creates": [{"values": r["values"], "row": r["row"]', src)

    def test_creates_of_reads_the_new_shape(self):
        got = import_data.creates_of(
            {"creates": [{"values": {"甲": 1}, "row": 7, "who": "张三 / 13800"}]})
        self.assertEqual(got, [{"values": {"甲": 1}, "row": 7, "who": "张三 / 13800"}])

    def test_creates_of_still_reads_old_flat_plans(self):
        """旧计划还能跑，只是指不出行号——而且要**说出来**，不是装作知道。"""
        got = import_data.creates_of({"creates": [{"甲": 1}]})
        self.assertEqual(got, [{"values": {"甲": 1}, "row": None, "who": None}])
        self.assertIn("旧版", import_data.where(None, None))

    def test_where_puts_the_name_next_to_the_row_number(self):
        self.assertEqual(import_data.where(6, "张三 / 13800"), "第 6 行  张三 / 13800")
        self.assertEqual(import_data.where(6), "第 6 行")


class TestFixSheetSeesEverythingTheConsoleSees(unittest.TestCase):
    """控制台报了、修复建议表却写"无需修复"——两处口径不一致时，人信的是表。

    这张表就是拿去逐条修数据的那份东西；控制台会滚走，表不会。
    编码期未提交的字段原来只进控制台，没进表。
    """

    PLAN = {"issues": [], "blocked_columns": []}

    def test_encode_stage_problems_reach_the_sheet(self):
        tmp = os.path.join(tempfile.mkdtemp(), "fix.xlsx")
        count = import_data.write_fix_sheet(
            tmp, self.PLAN, None,
            not_submitted=[{"column": "第 2 行  GAP-A　「数字」",
                            "kind": "bad_value", "reason": "无法解析为数字：'约一百'"}])
        self.assertEqual(count, 1)
        headers, rows = read_table(tmp)
        self.assertEqual(rows[0]["来源"], "编码期")
        self.assertIn("第 2 行", rows[0]["行号"])
        self.assertIn("无法解析", rows[0]["问题"])

    def test_still_none_when_there_is_genuinely_nothing(self):
        tmp = os.path.join(tempfile.mkdtemp(), "fix.xlsx")
        self.assertIsNone(import_data.write_fix_sheet(tmp, self.PLAN, None))


class TestIncludeHeldDoesNotClaimSuccess(unittest.TestCase):
    """`--include-held` 导的是预检**已经把坏格剔掉**的行。

    坏值不会被提交 → 编码期不报错 → 回读"干净" → 一路打到 ✅，
    而那几列确实是空的，缺的正是预检警告过的那一格。
    真机复现过：GAP-A / GAP-C 落库时「数字」= None，屏幕上是一句 ✅。
    """

    CLEAN = {"clean": True, "checked": 3, "aligned": True}

    def _say(self, **kw):
        kw.setdefault("update_skipped", [])
        kw.setdefault("create_skipped", [])
        kw.setdefault("included_held", False)
        kw.setdefault("update_dropped", [])
        return "\n".join(import_data.verdict(
            kw.get("verification", self.CLEAN), kw["update_skipped"],
            kw["create_skipped"], kw["included_held"], kw["update_dropped"]))

    def test_a_genuinely_clean_run_still_gets_the_checkmark(self):
        self.assertIn("✅", self._say())

    def test_include_held_never_gets_the_checkmark(self):
        """这条原来是**扫源码**测的——看 ✅ 附近有没有 included_held 这个词。
        那种测试把守卫取反照样全绿：词还在，行为已经反了。现在真的调它。"""
        said = self._say(included_held=True)
        self.assertNotIn("✅", said)
        self.assertIn("预检剔掉", said)

    def test_fields_that_never_got_submitted_block_the_checkmark(self):
        """回读核对只看提交过的字段，没提交的当然"没有不一致"。"""
        self.assertNotIn("✅", self._say(create_skipped=[{"column": "数字"}]))
        self.assertNotIn("✅", self._say(update_skipped=[{"column": "数字"}]))

    def test_unaligned_verification_says_so_instead(self):
        said = self._say(verification={"aligned": False, "checked": 0,
                                       "reason": "对不上"})
        self.assertIn("未做逐字段核对", said)
        self.assertNotIn("✅", said)

    def test_held_rows_are_listed_with_what_they_are_missing(self):
        src = open(os.path.join(SCRIPTS, "import_data.py"), encoding="utf-8").read()
        self.assertIn("扣下待修但仍导", src)
        self.assertIn("不要说这几行导完整了", src)
    def test_fields_dropped_on_the_update_path_block_the_checkmark(self):
        """更新那半的回读结果**不在 verification 里**——它逐条回读，结果单独攒着。

        原来这句 ✅ 只看新增那半：更新真丢了字段照样打「没有静默丢失」。
        混合导入（几行新增 + 几行更新）时这是常态，而这是"回报成功但事实不符"
        最后一个还活着的入口。
        """
        said = self._say(update_dropped=[{"field": "合同查看", "type": "upload",
                                          "reason": "提交了值但写入后为空"}])
        self.assertNotIn("✅", said)
        self.assertIn("写入后为空", said)

    def test_a_clean_update_only_run_still_gets_a_verdict(self):
        """纯更新的批次没有 verification，别一句结论都不打。"""
        self.assertIn("✅", "\n".join(import_data.verdict(
            {"clean": True}, [], [], False, [])))


class TestUnknownOptionWarning(unittest.TestCase):
    """实测：写一个**不存在的选项**，接口原样存下（checkboxgroup/combocheck 都是）。

    而 `widget/list` 只返回 name/label/type，**不给选项列表**——
    和"不返回 lookup 指向哪张表"同一个缺口。所以只能拿历史数据当索引，
    而那是**启发式**：只提醒，绝不据此扣行（新增一个选项完全合法）。
    """

    W = {"name": "_w_c", "label": "权限", "type": "checkboxgroup"}
    INDEX = {"_w_c": ["研发", "销售"]}

    def test_known_option_is_silent(self):
        self.assertEqual(preflight.unknown_options("销售", self.W, self.INDEX), [])
        self.assertEqual(preflight.unknown_options(["销售", "研发"], self.W, self.INDEX), [])

    def test_typo_is_flagged(self):
        self.assertEqual(preflight.unknown_options("销錯", self.W, self.INDEX), ["销錯"])

    def test_only_the_unseen_parts_are_flagged(self):
        self.assertEqual(preflight.unknown_options("销售、新选项", self.W, self.INDEX),
                         ["新选项"])

    def test_all_the_separators_people_actually_type(self):
        for text in ("销售,新选项", "销售，新选项", "销售、新选项", "销售;新选项"):
            self.assertEqual(preflight.unknown_options(text, self.W, self.INDEX),
                             ["新选项"], text)

    def test_no_samples_means_no_guessing(self):
        """一个历史值都没有时无从判断，别瞎报。"""
        self.assertEqual(preflight.unknown_options("随便", self.W, {}), [])

    def test_option_index_is_built_from_existing_rows(self):
        rows = [{"_w_c": ["销售"]}, {"_w_c": ["研发", "销售"]}, {"_w_c": None}]
        got = preflight.build_option_index(rows, [self.W])
        self.assertEqual(got, {"_w_c": ["研发", "销售"]})

    def test_a_warning_never_holds_the_row(self):
        """新增一个选项是合法的——提醒可以，扣行不行。"""
        mapping = {"权限": {"field": self.W, "how": "精确"}}
        issues, clean, held, warnings = preflight.check_cells(
            [{"权限": "全新选项"}], mapping, {}, option_index=self.INDEX)
        self.assertEqual((issues, held), ([], []))
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["kind"], "unknown_option")
        self.assertEqual(clean[0]["values"], {"权限": "全新选项"})   # 照写不误


class TestAttachmentPaths(unittest.TestCase):
    """附件列填的是**本地文件路径**，多个用 `|` 分隔（导出侧就是这么写的）。

    相对路径按 **Excel 所在目录**解析，不按当前工作目录——同一份表格换个目录跑
    就找不到文件，而那种失败很难看出原因。
    """

    def test_relative_paths_resolve_against_the_excel(self):
        got = preflight.attachment_paths("a.txt", "/work/sheets")
        self.assertEqual(got, [os.path.normpath("/work/sheets/a.txt")])

    def test_absolute_paths_are_left_alone(self):
        # 用**本平台**的绝对路径。写死 "/tmp/a.txt" 在 Windows 上不是绝对路径
        # （Python 3.13 起 ntpath.isabs 对无盘符的根路径返回 False），会被拼到 base 下。
        absp = os.path.abspath(os.path.join(os.sep, "tmp", "a.txt"))
        self.assertEqual(preflight.attachment_paths(absp, "/work"), [absp])

    def test_multiple_files_split_on_the_pipe(self):
        got = preflight.attachment_paths(" a.txt | 子目录/b.png ", "/w")
        self.assertEqual(got, [os.path.normpath("/w/a.txt"), os.path.normpath("/w/子目录/b.png")])

    def test_blank_parts_are_dropped(self):
        self.assertEqual(preflight.attachment_paths("a.txt ||  ", "/w"),
                         [os.path.normpath("/w/a.txt")])

    def test_missing_file_is_held_before_anything_is_written(self):
        """留到上传时才发现，前面的行已经写进去了，只能补第二遍。"""
        w = {"name": "_w_f", "label": "附件", "type": "upload"}
        mapping = {"附件": {"field": w, "how": "精确"}}
        tmp = tempfile.mkdtemp()
        issues, clean, held, _warn = preflight.check_cells(
            [{"附件": "根本没有.pdf"}], mapping, {}, base_dir=tmp)
        self.assertEqual([i["kind"] for i in issues], ["file_missing"])
        self.assertEqual(clean, [])
        self.assertEqual(len(held), 1)

    def test_an_existing_file_becomes_a_path_list_for_the_upload_step(self):
        w = {"name": "_w_f", "label": "附件", "type": "upload"}
        mapping = {"附件": {"field": w, "how": "精确"}}
        tmp = tempfile.mkdtemp()
        real = os.path.join(tmp, "a.txt")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("x")
        _issues, clean, _held, _warn = preflight.check_cells(
            [{"附件": "a.txt"}], mapping, {}, base_dir=tmp)
        self.assertEqual(clean[0]["values"], {"附件": [real]})


class TestAttachmentUploadStep(unittest.TestCase):
    """本地路径 → 上传 → key，且**和写入请求共用同一个事务号**。"""

    W = {"附件": {"name": "_w_f", "label": "附件", "type": "upload"},
         "岗位": {"name": "_w_p", "label": "岗位", "type": "text"}}

    class FakeClient(object):
        def __init__(self):
            self.calls = []

        def field_map(self, *a, **kw):
            w = TestAttachmentUploadStep.W
            return (dict(w), {v["name"]: v for v in w.values()})

        def upload_files(self, app, entry, paths, txn):
            self.calls.append((tuple(paths), txn))
            return ["key-%s" % os.path.basename(p) for p in paths]

        def upload_pool(self, app, entry, txn, need):
            self.token_requests = getattr(self, "token_requests", 0) + 1
            self.calls.append(("pool:%d" % need, txn))
            return [{"url": "u%d" % i, "token": "t%d" % i} for i in range(need)]

        def upload_file(self, url, token, path):
            return "key-%s" % os.path.basename(path)

    def test_only_attachment_columns_are_detected(self):
        c = self.FakeClient()
        self.assertEqual(
            import_data.attachment_columns(c, "A", "E", [{"岗位": "x", "附件": ["/a"]}]),
            ["附件"])
        self.assertEqual(import_data.attachment_columns(c, "A", "E", [{"岗位": "x"}]), [])

    def test_paths_become_keys_and_share_one_transaction(self):
        c = self.FakeClient()
        rows = [{"岗位": "甲", "附件": ["/a.txt"]},
                {"岗位": "乙", "附件": ["/b.png", "/c.pdf"]},
                {"岗位": "丙"}]
        out, count = import_data.upload_attachments(c, "A", "E", rows, ["附件"], "txn-1")
        self.assertEqual(count, 3)
        self.assertEqual(out[0]["附件"], ["key-a.txt"])
        self.assertEqual(out[1]["附件"], ["key-b.png", "key-c.pdf"])
        self.assertNotIn("附件", out[2])
        self.assertEqual({txn for _paths, txn in c.calls}, {"txn-1"})   # 同一个事务号

    def test_tokens_are_fetched_once_for_the_whole_batch(self):
        """取凭证一次给 100 组。原来每个格子取一次——50 行就是 50 次请求、
        白取 5000 组用 50 组，限流 20/s 上很容易把自己卡住。"""
        c = self.FakeClient()
        rows = [{"附件": ["/%d.txt" % i]} for i in range(20)]
        _out, count = import_data.upload_attachments(c, "A", "E", rows, ["附件"], "t")
        self.assertEqual(count, 20)
        self.assertEqual(c.token_requests, 1)

    def test_the_original_rows_are_not_mutated(self):
        """就地改会让调用方手里的计划变样，出错时对不上账。"""
        c = self.FakeClient()
        rows = [{"附件": ["/a.txt"]}]
        import_data.upload_attachments(c, "A", "E", rows, ["附件"], "t")
        self.assertEqual(rows[0]["附件"], ["/a.txt"])


class TestFixSheetCoversTheUpdatePath(unittest.TestCase):
    """修复建议表是拿去**逐条改数据**的那份东西，控制台会滚走、表不会。

    更新那半的回读结果不在 report["verification"] 里，漏收它，这张表就漏掉
    整整一类真实丢失——而控制台上明明报了。两处口径不一致时，人信的是表。
    """

    PLAN = {"warnings": [], "issues": [], "blocked_columns": []}

    def test_update_side_drops_land_in_the_sheet(self):
        tmp = os.path.join(tempfile.mkdtemp(), "fix.xlsx")
        n = import_data.write_fix_sheet(
            tmp, self.PLAN, {"verification": {}},
            update_dropped=[{"field": "合同查看", "data_id": "a" * 24,
                             "row": "第 3 行", "submitted": ["/x/a.pdf"],
                             "reason": "提交了值但写入后为空"}])
        self.assertEqual(n, 1)
        _headers, rows = read_table(tmp)
        self.assertIn("更新", rows[0]["来源"])
        self.assertEqual(rows[0]["列"], "合同查看")
        self.assertEqual(rows[0]["行号"], "第 3 行")     # 指不出哪一行等于没报

    def test_nothing_to_report_still_writes_nothing(self):
        tmp = os.path.join(tempfile.mkdtemp(), "fix.xlsx")
        self.assertIsNone(import_data.write_fix_sheet(
            tmp, self.PLAN, {"verification": {}}, update_dropped=[]))


class TestAttachmentColumnsCompareByFilename(unittest.TestCase):
    """「导出 → 改两行 → 整份导回」是本技能推荐的用法。

    附件列两边根本不是一种东西：库里是 [{name,size,mime,url}]，Excel 里是
    本地路径。比显示值永远不相等，于是**每次导回都把所有带附件的行判成改过了**——
    文件重传一遍、记录重写一遍。白耗上传凭证、刷满操作日志，
    而每次重写都是一次静默丢字段的机会。
    """

    BY = {"合同": {"name": "_w_f", "label": "合同", "type": "upload"},
          "姓名": {"name": "_w_n", "label": "姓名", "type": "text"}}
    STORED = {"_w_f": [{"name": "合同A.pdf", "url": "https://x?e=1&token=z"},
                       {"name": "图纸.png", "url": "https://y?e=1"}],
              "_w_n": "张三"}

    def test_the_same_files_exported_and_re_imported_count_as_unchanged(self):
        self.assertTrue(values_match(
            self.STORED, {"合同": "附件/合同A.pdf | 附件/图纸.png"}, self.BY))

    def test_order_does_not_matter(self):
        self.assertTrue(values_match(
            self.STORED, {"合同": "附件/图纸.png | 附件/合同A.pdf"}, self.BY))

    def test_a_different_file_is_a_real_change(self):
        self.assertFalse(values_match(
            self.STORED, {"合同": "附件/合同B.pdf | 附件/图纸.png"}, self.BY))

    def test_removing_a_file_is_a_real_change(self):
        self.assertFalse(values_match(self.STORED, {"合同": "附件/合同A.pdf"}, self.BY))
        self.assertFalse(values_match(self.STORED, {"合同": ""}, self.BY))

    def test_the_export_dedup_suffix_is_what_broke_this_in_practice(self):
        """真机跑出来的：单元格里是 `附件/更新实验-2.txt`，库里是 `更新实验.txt`。

        导出侧把重名文件改成 -2、-3 去重，导入侧按文件名比对——两半各行其是，
        于是原样导回仍有 3 行被判成"改过了"。修法不是在这里去猜后缀
        （真有文件就叫 `合同-2.pdf`），是让导出**一条记录一个子目录**，
        冲突根本不发生。这条测试钉住那个前提：一旦导出侧退回平铺，它就红。
        """
        self.assertFalse(values_match(
            self.STORED, {"合同": "附件/合同A-2.pdf | 附件/图纸.png"}, self.BY),
            "带去重后缀的文件名不该被当成同一个文件——那要靠导出分目录来避免")

    def test_other_columns_are_still_compared_normally(self):
        self.assertFalse(values_match(
            self.STORED, {"合同": "附件/合同A.pdf | 附件/图纸.png", "姓名": "李四"},
            self.BY))


class TestExportGivesEachRecordItsOwnFolder(unittest.TestCase):
    """一条记录一个子目录。见 TestAttachmentColumnsCompareByFilename 里那条说明：
    平铺 + 去重后缀会让"原样导回"每次都重传重写。"""

    W = {"name": "_w_f", "label": "合同", "type": "upload"}

    class FakeClient(object):
        def __init__(self):
            self.saved = []

        def download_file(self, url, dest_dir, name):
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
            self.saved.append(path)
            return path

    W2 = {"name": "_w_g", "label": "附图", "type": "upload"}

    def _cell(self, client, row, widget, dest):
        return export.grab_attachments(client, row, widget, dest, [])

    def test_same_filename_in_two_columns_of_one_record(self):
        """跨列重名。**上一版只按记录分目录**，于是同一条记录的第二个附件列
        拿到的是 `合同-3.pdf`——去重后缀又回来了，那一行每次导回都重传。"""
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "附件")
        c = self.FakeClient()
        row = {"_id": "a" * 24,
               "_w_f": [{"name": "合同.pdf", "url": "u1"}],
               "_w_g": [{"name": "合同.pdf", "url": "u2"}]}
        cells = [self._cell(c, row, w, dest) for w in (self.W, self.W2)]
        for cell in cells:
            self.assertTrue(cell.endswith("合同.pdf"), cell)   # 都还叫原名
        self.assertNotEqual(cells[0], cells[1])                # 但落在不同目录
        by = {"合同": self.W, "附图": self.W2}
        self.assertTrue(values_match(row, {"合同": cells[0], "附图": cells[1]}, by))

    def test_two_files_with_one_name_inside_a_single_cell(self):
        """格内重名——简道云允许同一格上传两个同名文件。
        目录层级挡不住这一种，只能再分一层序号目录。"""
        d = tempfile.mkdtemp()
        row = {"_id": "a" * 24,
               "_w_f": [{"name": "合同.pdf", "url": "u1"},
                        {"name": "合同.pdf", "url": "u2"}]}
        cell = self._cell(self.FakeClient(), row, self.W, os.path.join(d, "附件"))
        self.assertEqual([os.path.basename(p) for p in cell.split(" | ")],
                         ["合同.pdf", "合同.pdf"])           # 两个都还叫原名
        self.assertTrue(values_match(row, {"合同": cell}, {"合同": self.W}))
        for part in cell.split(" | "):
            self.assertTrue(os.path.isfile(os.path.join(d, part.strip())), part)

    def test_a_cell_without_duplicates_stays_flat(self):
        """没冲突就不要多一层——目录深度不该白涨。"""
        d = tempfile.mkdtemp()
        row = {"_id": "a" * 24, "_w_f": [{"name": "甲.pdf", "url": "u1"},
                                         {"name": "乙.pdf", "url": "u2"}]}
        cell = self._cell(self.FakeClient(), row, self.W, os.path.join(d, "附件"))
        for part in cell.split(" | "):
            self.assertEqual(os.path.basename(os.path.dirname(part.strip())), "合同")

    def test_a_column_name_with_a_slash_does_not_split_the_path(self):
        d = tempfile.mkdtemp()
        w = {"name": "_w_f", "label": "合同/正本", "type": "upload"}
        row = {"_id": "a" * 24, "_w_f": [{"name": "甲.pdf", "url": "u"}]}
        cell = self._cell(self.FakeClient(), row, w, os.path.join(d, "附件"))
        self.assertNotIn("合同/正本", cell)
        self.assertTrue(os.path.isfile(os.path.join(d, cell)), cell)

    def test_same_filename_in_two_records_does_not_collide(self):
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "附件")
        c, failures = self.FakeClient(), []
        cells = [export.grab_attachments(
                     c, {"_id": rid, "_w_f": [{"name": "合同.pdf", "url": "u"}]},
                     self.W, dest, failures)
                 for rid in ("a" * 24, "b" * 24)]
        self.assertEqual(failures, [])
        for cell, rid in zip(cells, ("a" * 24, "b" * 24)):
            self.assertIn(rid, cell)
            self.assertTrue(cell.endswith("合同.pdf"), cell)   # 没有 -2 后缀
        self.assertEqual(len({os.path.dirname(p) for p in c.saved}), 2)

    def test_no_downloaded_file_is_ever_renamed(self):
        """一句话版本的契约：**落盘的文件名必须等于库里的名字。**

        注释里写着"分目录让冲突根本不发生"——这条测试是那句话的凭据。
        少挡一种重名（跨记录 / 跨列 / 格内），它就红。
        """
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "附件")
        c = self.FakeClient()
        rows = [{"_id": "a" * 24,
                 "_w_f": [{"name": "同名.pdf", "url": "u"},
                          {"name": "同名.pdf", "url": "u"}],
                 "_w_g": [{"name": "同名.pdf", "url": "u"}]},
                {"_id": "b" * 24, "_w_f": [{"name": "同名.pdf", "url": "u"}],
                 "_w_g": [{"name": "同名.pdf", "url": "u"}]}]
        for row in rows:
            for w in (self.W, self.W2):
                self._cell(c, row, w, dest)
        self.assertEqual(len(c.saved), 5)
        for path in c.saved:
            self.assertEqual(os.path.basename(path), "同名.pdf", path)
        self.assertEqual(len(set(c.saved)), 5, "有文件被覆盖了")

    def test_the_cell_path_resolves_from_the_sheet_directory(self):
        """单元格写的是相对路径，导入时按 Excel 所在目录解析——要对得上。"""
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "附件")
        cell = export.grab_attachments(
            self.FakeClient(), {"_id": "c" * 24, "_w_f": [{"name": "图纸.png", "url": "u"}]},
            self.W, dest, [])
        self.assertTrue(os.path.isfile(os.path.join(d, cell)), cell)


if __name__ == "__main__":
    unittest.main(verbosity=2)
