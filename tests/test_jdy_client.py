# -*- coding: utf-8 -*-
"""共享内核测试。

纯函数部分离线可跑；集成部分需要 ~/.jdy/config.json 或 JDY_API_KEY，缺失时自动跳过。
集成测试全程只读 + dry-run，不会写入任何数据。

    python3 tests/test_jdy_client.py
"""
import builtins
import os
import sys
import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_shared"))

import jdy_client as jc  # noqa: E402
from jdy_client import (  # noqa: E402
    JdyClient, NotWritableField, TokenBucket, ask_yes, build_filter, encode_row,
    encode_value, normalize_datetime, parse_iso,
)

W = lambda label, wtype, **kw: dict(name="_widget_%s" % label, label=label, type=wtype, **kw)


class TestDatetime(unittest.TestCase):
    def test_iso_z_is_utc_not_a_literal_z(self):
        """"…Z" 已经是 UTC，归一化后必须原样不动。

        strptime 把格式串里的 Z 当**字面量**匹配，解析出来是 naive 的，
        再按源时区（默认 +08:00）补一次，整个时间就倒退了 8 小时。
        导出→改→回导是最高频的工作流，每走一轮时间列就整体偏移一次。
        """
        self.assertEqual(normalize_datetime("2026-08-27T02:00:00.000Z"),
                         "2026-08-27T02:00:00.000Z")
        self.assertEqual(normalize_datetime("2026-08-27T02:00:00Z"),
                         "2026-08-27T02:00:00.000Z")
        # 源时区参数不该影响一个已经自带时区的值
        self.assertEqual(normalize_datetime("2026-08-27T02:00:00.000Z", tz=timezone.utc),
                         normalize_datetime("2026-08-27T02:00:00.000Z"))

    def test_offset_bearing_iso_is_converted_not_rejected(self):
        self.assertEqual(normalize_datetime("2026-08-27T10:00:00+08:00"),
                         "2026-08-27T02:00:00.000Z")

    def test_naive_still_takes_the_source_timezone(self):
        """不带时区的输入仍按源时区解释——这是既有约定，不能被上面的修复带偏。"""
        self.assertEqual(normalize_datetime("2026-08-27 10:00:00"),
                         "2026-08-27T02:00:00.000Z")
        self.assertEqual(normalize_datetime("2026-08-27 10:00:00", tz=timezone.utc),
                         "2026-08-27T10:00:00.000Z")

    def test_ambiguous_slash_date_is_refused_not_guessed(self):
        """`01/12/2025` 是 1 月 12 日还是 12 月 1 日？无从判断，所以不猜。

        原来 `%d/%m/%Y` 排在 `%m/%d/%Y` 前面，它**恒**被读成 12 月 1 日——
        谁在前是格式列表的先后顺序决定的，等于抛硬币。而错了没有任何迹象：
        存进去是个合法日期，回读比对也通过，整列日期悄悄错了几个月。
        """
        with self.assertRaises(ValueError) as cm:
            normalize_datetime("01/12/2025")
        self.assertIn("无法判断", str(cm.exception))
        self.assertIn("YYYY-MM-DD", str(cm.exception))    # 得说清怎么改

    def test_unambiguous_slash_dates_still_work(self):
        # 有一位 >12 就它是"日"，这时是能判断的
        self.assertEqual(normalize_datetime("25/12/2025", tz=timezone.utc),
                         "2025-12-25T00:00:00.000Z")
        self.assertEqual(normalize_datetime("12/25/2025", tz=timezone.utc),
                         "2025-12-25T00:00:00.000Z")
        # 四位年在前的从来不歧义
        self.assertEqual(normalize_datetime("2025/12/25", tz=timezone.utc),
                         "2025-12-25T00:00:00.000Z")

    def test_impossible_slash_date_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_datetime("25/25/2025")

    def test_ambiguous_date_lands_in_skipped_not_in_the_data(self):
        """走 encode_row 时它应该变成一条 bad_value，而不是一个错日期。"""
        widgets = {"日期": W("日期", "datetime")}
        data, skipped = encode_row(widgets, {"日期": "01/12/2025"})
        self.assertEqual(data, {})
        self.assertEqual([s["kind"] for s in skipped], ["bad_value"])

    def test_parse_iso_assumes_utc_for_naive(self):
        """流程侧拿它和 now(utc) 相减，naive 会直接抛 TypeError。"""
        self.assertEqual(parse_iso("2026-08-27T02:00:00").utcoffset(), timedelta(0))
        self.assertEqual(parse_iso("2026-08-27T02:00:00Z").utcoffset(), timedelta(0))
        self.assertIsNone(parse_iso("下周三"))
        self.assertIsNone(parse_iso(None))

    def test_excel_slash_format_is_normalized(self):
        """2026/08/27 是 Excel 最常见写法，简道云自己不认（实测存 null）。"""
        got = normalize_datetime("2026/08/27", tz=timezone.utc)
        self.assertEqual(got, "2026-08-27T00:00:00.000Z")

    def test_chinese_format(self):
        self.assertEqual(normalize_datetime("2026年8月27日", tz=timezone.utc),
                         "2026-08-27T00:00:00.000Z")

    def test_default_source_tz_is_beijing_not_machine_local(self):
        """默认按北京时间解释源数据，**不能**取机器本地时区——
        否则同一份 Excel 在不同时区的机器上会落成不同的值，且毫无迹象。"""
        got = normalize_datetime("2026-08-27 10:30:00")
        self.assertEqual(got, "2026-08-27T02:30:00.000Z")

    def test_midnight_matches_jdy_own_storage(self):
        """简道云自己存的北京时间零点就是前一天 16:00Z（真实数据核对过）。"""
        self.assertEqual(normalize_datetime("2026/08/27"), "2026-08-26T16:00:00.000Z")

    def test_explicit_tz_overrides(self):
        from jdy_client import parse_tz
        self.assertEqual(normalize_datetime("2026-08-27 10:30:00", tz=parse_tz("utc")),
                         "2026-08-27T10:30:00.000Z")
        self.assertEqual(normalize_datetime("2026-08-27 10:30:00", tz=parse_tz("-04:00")),
                         "2026-08-27T14:30:00.000Z")

    def test_bad_tz_spec_raises(self):
        from jdy_client import parse_tz
        with self.assertRaises(ValueError):
            parse_tz("北京")

    def test_already_iso_passthrough(self):
        self.assertEqual(normalize_datetime("2026-08-27T00:00:00.000Z", tz=timezone.utc),
                         "2026-08-27T00:00:00.000Z")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            normalize_datetime("下周三")

    def test_empty_is_none(self):
        self.assertIsNone(normalize_datetime(""))


class TestEncodeValue(unittest.TestCase):
    def test_linkdata_is_rejected(self):
        """实测十种写法全灭，内核必须提前拦住而不是静默写空。"""
        with self.assertRaises(NotWritableField):
            encode_value(W("客户信息", "linkdata"), "任何值")

    def test_sn_is_rejected(self):
        with self.assertRaises(NotWritableField):
            encode_value(W("订单编号", "sn"), "ZZZ-999")

    def test_user_accepts_username(self):
        self.assertEqual(encode_value(W("制单人", "user"), "sys_abc123"), "sys_abc123")

    def test_user_extracts_username_from_readback_object(self):
        """读回来是对象，直接回灌会静默丢失——内核替用户抽出 username。"""
        obj = {"name": "hao", "username": "sys_abc123", "status": 1}
        self.assertEqual(encode_value(W("制单人", "user"), obj), "sys_abc123")

    def test_user_display_name_raises(self):
        with self.assertRaises(ValueError):
            encode_value(W("制单人", "user"), "张三")

    def test_number_from_string(self):
        self.assertEqual(encode_value(W("数量", "number"), "123"), 123)
        self.assertEqual(encode_value(W("单价", "number"), "1,234.5"), 1234.5)

    def test_number_garbage_raises(self):
        with self.assertRaises(ValueError):
            encode_value(W("数量", "number"), "abc")

    def test_checkbox_splits_comma_string(self):
        self.assertEqual(encode_value(W("权限", "checkboxgroup"), "销售,财务"), ["销售", "财务"])


class TestLookupField(unittest.TestCase):
    """关联数据（lookup）可写，但接口不校验引用——最隐蔽的一类脏数据。"""

    W = staticmethod(lambda: W("关联线索", "lookup"))

    def test_accepts_data_id(self):
        self.assertEqual(encode_value(self.W(), "deadbeefdeadbeefdead0005"),
                         "deadbeefdeadbeefdead0005")

    def test_accepts_object_form(self):
        self.assertEqual(encode_value(self.W(), {"id": "deadbeefdeadbeefdead0005"}),
                         "deadbeefdeadbeefdead0005")

    def test_rejects_business_name(self):
        """填业务名称会原样存成指向虚无的引用，且回读还"一致"——必须在编码期拦住。"""
        with self.assertRaises(ValueError) as ctx:
            encode_value(self.W(), "某某线索")
        self.assertIn("data_id", str(ctx.exception))

    def test_rejects_malformed_id(self):
        for bad in ("6a8eb11f", "deadbeefdeadbeefdead0005f", "ZZZZZZZZZZZZZZZZZZZZZZZZ"):
            with self.assertRaises(ValueError):
                encode_value(self.W(), bad)

    def test_wellformed_but_fake_id_passes_format_check(self):
        """格式校验拦不住"格式合法但不存在"——那要靠 lookup_exists 查目标表。
        这条断言的是能力边界，不是缺陷。"""
        self.assertEqual(encode_value(self.W(), "0" * 24), "0" * 24)

    def test_empty_is_none(self):
        self.assertIsNone(encode_value(self.W(), ""))


class TestObjectValuedTypes(unittest.TestCase):
    """电话与地址：写入要对象，显示串会被静默丢弃——和成员字段同一类陷阱。"""

    P = staticmethod(lambda: W("手机", "phone"))
    A = staticmethod(lambda: W("地址", "address"))

    def test_phone_from_plain_string_gets_wrapped(self):
        self.assertEqual(encode_value(self.P(), "13800138000"), {"phone": "13800138000"})

    def test_phone_from_readback_object(self):
        """读回来是 {"verified": false, "phone": "…"}，写回去只留 phone。"""
        got = encode_value(self.P(), {"verified": False, "phone": "13800138000"})
        self.assertEqual(got, {"phone": "13800138000"})

    def test_phone_object_without_number_rejected(self):
        with self.assertRaises(ValueError):
            encode_value(self.P(), {"verified": False})

    def test_address_object_passes(self):
        addr = {"province": "江苏省", "city": "无锡市", "district": "锡山区", "detail": ""}
        self.assertEqual(encode_value(self.A(), addr), addr)

    def test_address_string_rejected_with_reason(self):
        """拼接好的地址串实测会被静默丢弃，必须在编码期拦住。"""
        with self.assertRaises(ValueError) as ctx:
            encode_value(self.A(), "江苏省无锡市锡山区")
        self.assertIn("静默丢弃", str(ctx.exception))


class TestEncodeRow(unittest.TestCase):
    def setUp(self):
        self.fields = {
            "名称": W("名称", "text"),
            "数量": W("数量", "number"),
            "日期": W("日期", "datetime"),
            "客户": W("客户", "linkdata"),
            "编号": W("编号", "sn"),
            "明细": W("明细", "subform", items=[
                {"name": "_widget_sub_品名", "label": "品名", "type": "text"},
                {"name": "_widget_sub_单价", "label": "单价", "type": "number"},
            ]),
        }

    def test_every_value_is_wrapped(self):
        """裸值会让整行静默变空——外壳是不可省的。"""
        data, _ = encode_row(self.fields, {"名称": "甲", "数量": 3})
        self.assertEqual(data["_widget_名称"], {"value": "甲"})
        self.assertEqual(data["_widget_数量"], {"value": 3})

    def test_subform_wraps_both_layers(self):
        data, _ = encode_row(self.fields, {"明细": [{"品名": "A", "单价": "9.9"}]})
        self.assertEqual(data["_widget_明细"],
                         {"value": [{"_widget_sub_品名": {"value": "A"},
                                     "_widget_sub_单价": {"value": 9.9}}]})

    def test_unwritable_columns_are_reported_not_silently_dropped(self):
        data, skipped = encode_row(self.fields, {"名称": "甲", "客户": "示例客户A", "编号": "X-1"})
        self.assertNotIn("_widget_客户", data)
        self.assertNotIn("_widget_编号", data)
        kinds = {item["column"]: item["kind"] for item in skipped}
        self.assertEqual(kinds["客户"], "unwritable")
        self.assertEqual(kinds["编号"], "system_generated")

    def test_unknown_column_is_reported(self):
        """简道云会静默忽略未知字段，用户必须被告知。"""
        _, skipped = encode_row(self.fields, {"名称": "甲", "不存在的列": "x"})
        kinds = {item["column"]: item["kind"] for item in skipped}
        self.assertEqual(kinds["不存在的列"], "unknown_column")

    def test_bad_value_reported_not_written(self):
        data, skipped = encode_row(self.fields, {"数量": "abc"})
        self.assertNotIn("_widget_数量", data)
        kinds = {item["column"]: item["kind"] for item in skipped}
        self.assertEqual(kinds["数量"], "bad_value")


class TestTokenBucket(unittest.TestCase):
    def test_throttles_beyond_capacity(self):
        import time
        bucket = TokenBucket(10)
        for _ in range(10):
            bucket.take()
        start = time.monotonic()
        bucket.take()
        self.assertGreater(time.monotonic() - start, 0.05)


HAS_KEY = bool(os.environ.get("JDY_API_KEY") or os.path.exists(os.path.expanduser("~/.jdy/config.json")))


@unittest.skipUnless(HAS_KEY, "无密钥，跳过集成测试")
class TestIntegrationReadOnly(unittest.TestCase):
    """只读 + dry-run，不写入任何数据。"""

    # 鉴权失败的业务码。**只有这些算"环境问题"**，其余一律照常失败。
    AUTH_CODES = {17018}          # The API key is invalid.
    AUTH_STATUS = {401, 403}

    @classmethod
    def setUpClass(cls):
        cls.c = JdyClient()
        try:
            cls.apps = cls.c.list_apps()
        except jc.JdyError as exc:
            # HAS_KEY 只看"配置文件在不在"，看不出那把 Key 还有没有效。
            # 换过 Key 而本地配置没跟着改，是这里最常见的失败——那是环境问题，
            # 不是代码回归。让它 skip 并说清怎么办，否则整个文件报 error，
            # run_all.py 一片红，真正的回归反而被盖住。
            if exc.code in cls.AUTH_CODES or exc.http_status in cls.AUTH_STATUS:
                raise unittest.SkipTest(
                    "简道云拒绝了本机的 Key（%s）——集成测试跳过。\n"
                    "  多半是后台换过 Key 而 ~/.jdy/config.json 还指着旧的。\n"
                    "  修：echo '<新KEY>' | python3 "
                    "skills/hello-jdy/scripts/setup.py --stdin" % exc)
            raise
        if not cls.apps:
            raise unittest.SkipTest("这把 Key 一个应用都看不到，集成测试无从跑起")
        cls.app_id = cls.apps[0]["app_id"]

    def test_list_apps(self):
        self.assertTrue(self.apps)
        self.assertIn("app_id", self.apps[0])

    def test_field_map_and_cache(self):
        forms = self.c.list_forms(self.app_id)
        self.assertTrue(forms)
        entry_id = forms[0]["entry_id"]
        by_label, by_name = self.c.field_map(self.app_id, entry_id)
        self.assertTrue(by_label)
        self.assertEqual(len(by_label), len(by_name))
        again = self.c.field_map(self.app_id, entry_id)          # 走缓存
        self.assertEqual(set(by_label), set(again[0]))

    def test_cursor_pagination_yields_unique_ids(self):
        forms = self.c.list_forms(self.app_id)
        entry_id = next(f["entry_id"] for f in forms)
        rows = self.c.fetch_all(self.app_id, entry_id, limit=15, page_size=5)
        ids = [r["_id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "游标分页出现重复行")
        self.assertEqual(ids, sorted(ids), "返回应恒按数据 ID 正序")

    def test_dry_run_writes_nothing_and_flags_unwritable(self):
        """对含 linkdata/sn 的真实表跑 dry-run，确认能提前列出不可导入列。"""
        forms = self.c.list_forms(self.app_id)
        target = None
        for f in forms:
            types = {w["type"] for w in self.c.widgets(self.app_id, f["entry_id"])}
            if "linkdata" in types or "sn" in types:
                target = f
                break
        if target is None:
            self.skipTest("该应用无 linkdata/sn 字段")
        by_label, _ = self.c.field_map(self.app_id, target["entry_id"])
        row = {}
        for label, w in by_label.items():
            if w["type"] in ("linkdata", "sn"):
                row[label] = "任意值"
            elif w["type"] == "text":
                row[label] = "dry-run-不会写入"
        report = self.c.batch_create(self.app_id, target["entry_id"], [row], dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["created_ids"], [])
        self.assertTrue(report["unwritable_columns"], "应当列出不可写入的列")




class TestTargetResolution(unittest.TestCase):
    """名字 → ID 的解析。实测中 Agent 因为拿不到表单清单，漏掉了一张确实存在的表，
    还把错误的候选摆给了用户——所以这条路径必须有测试。"""

    APPS = [{"name": "CRM", "app_id": "a" * 24},
            {"name": "销售CRM", "app_id": "b" * 24},
            {"name": "销售台账", "app_id": "c" * 24}]

    class _Fake(object):
        def __init__(self, apps, forms=None):
            self._apps, self._forms = apps, forms or []

        def list_apps(self):
            return self._apps

        def list_forms(self, app_id):
            return self._forms

    def _client(self, forms=None):
        return self._Fake(self.APPS, forms)

    def test_id_passes_through(self):
        self.assertEqual(jc.resolve_app(self._client(), "b" * 24), "b" * 24)

    def test_exact_name_beats_substring(self):
        # 「CRM」是「销售CRM」的子串，但精确名必须优先，否则就成了歧义
        self.assertEqual(jc.resolve_app(self._client(), "CRM"), "a" * 24)

    def test_unique_substring(self):
        self.assertEqual(jc.resolve_app(self._client(), "销售C"), "b" * 24)

    def test_ambiguous_refuses(self):
        with self.assertRaises(jc.AmbiguousName) as cm:
            jc.resolve_app(self._client(), "销售")
        self.assertEqual(len(cm.exception.candidates), 2)
        self.assertIn("销售CRM", str(cm.exception))

    def test_not_found_lists_options(self):
        with self.assertRaises(jc.TargetError) as cm:
            jc.resolve_app(self._client(), "进销存")
        self.assertIn("销售CRM", str(cm.exception))   # 报错要带上现有清单

    def test_target_error_is_jdy_error(self):
        # 各脚本都是 except JdyError，新异常必须落在同一族里
        self.assertTrue(issubclass(jc.TargetError, jc.JdyError))
        err = jc.TargetError("x")
        self.assertEqual(err.msg, "x")               # init_config.py 用 exc.msg

    def test_resolve_entry(self):
        forms = [{"name": "联系人", "entry_id": "e" * 24},
                 {"name": "客户档案", "entry_id": "f" * 24}]
        self.assertEqual(jc.resolve_entry(self._client(forms), "b" * 24, "联系人"),
                         "e" * 24)

    def test_describe_targets_lists_forms(self):
        forms = [{"name": "联系人", "entry_id": "e" * 24}]
        items = jc.describe_targets(self._client(forms), "b" * 24, with_counts=False)
        self.assertEqual(items[0]["name"], "联系人")
        self.assertEqual(items[0]["kind"], "form")


class TestDisplayValueStructured(unittest.TestCase):
    """结构化控件的显示值。

    实测扫了账号里全部 25 种控件，有 5 种掉进 json.dumps 兜底——
    导出的 Excel 单元格里是一整坨 `{"verified": false, "phone": "138…"}`。
    报表分组、同步比对用的是同一个函数，所以是三处一起坏。
    """

    def test_phone_gives_bare_number(self):
        self.assertEqual(jc.display_value({"verified": False, "phone": "13800000001"},
                                          "phone"), "13800000001")  # 脱敏例外：造的号

    def test_structured_widgets_fall_back_to_name(self):
        # 按形状认，不逐个枚举控件类型：平台加新控件时不用改代码
        for wtype, value, want in [
                ("signature", {"name": "signature_1.png", "size": 27864}, "signature_1.png"),
                ("leads_pool", {"_id": "x", "name": "示例：无锡线索池"}, "示例：无锡线索池"),
                ("account_pool", {"_id": "x", "name": "示例：杭州公海池"}, "示例：杭州公海池"),
                ("sale_stage", {"template_id": "t", "stage_id": "s", "name": "赢单"}, "赢单"),
                ("某个还没出现的新控件", {"id": "1", "name": "新控件"}, "新控件")]:
            self.assertEqual(jc.display_value(value, wtype), want, wtype)

    def test_no_name_still_falls_back_to_json(self):
        # 认不出来就如实给 JSON，不要瞎猜一个字段冒充显示值
        got = jc.display_value({"a": 1, "b": 2}, "未知控件")
        self.assertTrue(got.startswith("{"))

    def test_linkdata_not_hijacked_by_name_fallback(self):
        # linkdata 有自己的规则（只有 id），不能被通用 name 兜底抢走
        self.assertEqual(jc.display_value({"id": "6a8d7b93"}, "linkdata"), "6a8d7b93")


class TestScaleGate(unittest.TestCase):
    """大批量写入的二次确认。

    来历：全量自查时发现 jdy-sync 有规模闸门，jdy-excel-bridge 没有——
    而 Excel 批量导入恰恰是最容易一次写坏几百条的地方。我自己就在验证时
    用一份 60 条的计划 --execute，直接把 60 条垃圾写进了真实账号。
    安全措施不能挑地方放。
    """

    def _gate(self, *args):
        """闸门的提示是打给用户看的，跑测试时要收走，否则淹掉测试结果。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return jc.scale_gate(*args), buf.getvalue()

    def test_small_batch_passes(self):
        rc, out = self._gate(10, "abc", None, 50)
        self.assertIsNone(rc)
        self.assertEqual(out, "")            # 放行时不该刷屏

    def test_large_batch_blocked(self):
        rc, out = self._gate(60, "abc", None, 50)
        self.assertEqual(rc, 5)
        self.assertIn("--confirm-code abc", out)

    def test_correct_code_passes(self):
        self.assertIsNone(self._gate(60, "abc", "abc", 50)[0])

    def test_stale_code_still_blocked(self):
        # 码由计划内容算出。数据变了码就变，旧码必须失效——
        # 否则用户确认的计划和真正执行的计划可以是两回事
        rc, out = self._gate(60, "new", "old", 50)
        self.assertEqual(rc, 5)
        self.assertIn("不符", out)

    def test_code_changes_with_plan(self):
        a = jc.plan_code({"creates": 10, "app": "x"})
        b = jc.plan_code({"creates": 11, "app": "x"})
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 8)

    def test_code_is_stable_for_same_plan(self):
        plan = {"app": "x", "creates": 3, "sample": ["姓名", "职务"]}
        self.assertEqual(jc.plan_code(plan), jc.plan_code(dict(plan)))


class TestWriteAllowlist(unittest.TestCase):
    """可选的写入白名单。

    来历有代价：验证规模闸门时我构造了一份 60 条的计划直接 --execute，
    而当时 jdy-excel-bridge 还没有闸门，180 条测试数据就这样进了业务表。
    闸门后来补上了，但闸门是"大批量才拦"；白名单拦的是"根本不该动的表"。
    对把 Agent 放在真实业务账号上的人，这是一道更根本的闸。
    """

    def setUp(self):
        self._saved = os.environ.get(jc.WRITE_ALLOWLIST_ENV)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(jc.WRITE_ALLOWLIST_ENV, None)
        else:
            os.environ[jc.WRITE_ALLOWLIST_ENV] = self._saved

    def _set(self, value):
        os.environ[jc.WRITE_ALLOWLIST_ENV] = value

    def test_unset_means_no_restriction(self):
        os.environ.pop(jc.WRITE_ALLOWLIST_ENV, None)
        jc.check_writable("APP", "ENTRY")          # 不抛就是放行

    def test_entry_in_list_passes(self):
        self._set("E1,E2")
        jc.check_writable("APP", "E1")

    def test_app_level_allow(self):
        # 允许整个应用，省得逐张表列
        self._set("APP")
        jc.check_writable("APP", "任意表单")

    def test_not_in_list_refuses(self):
        self._set("E1")
        with self.assertRaises(jc.TargetError) as cm:
            jc.check_writable("APP", "E2")
        self.assertIn("已拒绝写入", str(cm.exception))
        self.assertIn("E1", str(cm.exception))     # 要说清名单里有什么

    def test_separators_and_spaces(self):
        self._set(" E1 ; E2 , E3 ")
        for e in ("E1", "E2", "E3"):
            jc.check_writable("APP", e)

    # 流程写端点，以 ENDPOINT_RATE 为准（正确答案一直在同一个文件里）
    WORKFLOW_WRITES = ("/v1/workflow/task/approve", "/v1/workflow/task/reject",
                       "/v1/workflow/task/transfer", "/v2/workflow/task/rollback")

    def test_write_paths_are_recognised(self):
        """用**真实**端点名断言，不是想当然的名字。

        原来这里写的是 `/v1/workflow/instance/task/agree`——那个端点根本不存在，
        而正则里也配套地写着 `agree`。测试和实现一起错，于是「批量同意」
        （真实端点 approve）整整绕过了白名单，这条测试还是绿的。
        """
        for path in ("/app/entry/data/create", "/app/entry/data/batch_create",
                     "/app/entry/data/update", "/app/entry/data/delete"):
            self.assertTrue(jc.WRITE_PATH.search(path), path)

    def test_workflow_write_endpoints_are_the_real_ones(self):
        for path in self.WORKFLOW_WRITES:
            self.assertIn(path, jc.ENDPOINT_RATE, "%s 不是真实端点" % path)
            self.assertTrue(jc.WRITE_PATH.search(path), path)

    def test_workflow_paths_are_carved_out_of_the_post_level_check(self):
        """流程写请求的 body 里只有 task_id，没有 app_id/entry_id。

        post() 那道统一关卡对它是瞎的——硬查会拿 (None, None) 无条件拒掉
        **所有**流程操作（这正是 reject/rollback/transfer 之前的实际行为）。
        所以它们由调用方逐条查，这里确认两侧的划分是清楚的。
        """
        for path in self.WORKFLOW_WRITES:
            self.assertTrue(jc.WORKFLOW_PATH.match(path), path)
        self.assertIsNone(jc.WORKFLOW_PATH.match("/app/entry/data/update"))

    def test_workflow_tasks_are_checked_one_by_one(self):
        self._set("E1")
        jc.check_workflow_writable([{"app_id": "APP", "form_id": "E1"}])
        with self.assertRaises(jc.TargetError):
            jc.check_workflow_writable([{"app_id": "APP", "form_id": "E1"},
                                        {"app_id": "APP", "form_id": "E2"}])

    def test_read_paths_are_not_gated(self):
        for path in ("/app/list", "/app/entry/list", "/app/entry/widget/list",
                     "/app/entry/data/list", "/v6/workflow/task/list"):
            self.assertIsNone(jc.WRITE_PATH.search(path), path)


class TestCorpWritesHaveTheirOwnGate(unittest.TestCase):
    """通讯录写接口的 body 里也没有 app_id/entry_id。

    和流程接口是**同一个结构性缺口**，而当初只给流程开了豁免：于是设了
    JDY_WRITE_ALLOWLIST 的账号上，check_writable 拿着 (None, None) 把**所有**
    通讯录写入无条件拒掉——越是谨慎设了白名单的账号，jdy-org 越是完全不能用。

    修法不是也开个豁免了事（那等于通讯录裸奔），是让它走**自己那道闸**，
    并且同样安在 post() 这个唯一出口上：绕开 apply.py 直接调接口也拦得住。
    """

    CORP_WRITES = ("/v6/corp/department/create", "/v6/corp/department/update",
                   "/v5/corp/user/create", "/v5/corp/user/update")

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("JDY_WRITE_ALLOWLIST", "JDY_ORG_WRITE")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_they_are_recognised_as_writes(self):
        for path in self.CORP_WRITES:
            self.assertTrue(jc.WRITE_PATH.search(path), path)
            self.assertTrue(jc.CORP_PATH.match(path), path)

    def test_contacts_reads_are_not_gated(self):
        for path in ("/v6/corp/department/list", "/v5/corp/user/get",
                     "/v5/corp/department/user/list"):
            self.assertIsNone(jc.WRITE_PATH.search(path), path)

    def test_the_form_allowlist_does_not_apply_to_them(self):
        """这就是那个 bug：设了白名单，通讯录写入被 (None, None) 拒掉。"""
        self.assertIsNone(jc.CORP_PATH.match("/app/entry/data/update"))
        for path in self.CORP_WRITES:
            self.assertIsNone(jc.WORKFLOW_PATH.match(path), path)

    def test_the_org_switch_is_what_gates_them(self):
        os.environ["JDY_WRITE_ALLOWLIST"] = "E1"      # 白名单设了也不该由它裁决
        os.environ.pop("JDY_ORG_WRITE", None)
        with self.assertRaises(jc.OrgWriteRefused):
            jc.check_org_write()
        os.environ["JDY_ORG_WRITE"] = "1"
        jc.check_org_write()                          # 不抛就是放行

    def test_post_itself_routes_them_to_the_org_gate(self):
        """**这条才是那个 bug 的正面复现。** 上面几条测的是常量和闸门本身，
        而出事的地方是 post() 的分发：它对通讯录路径走了表单白名单那一支，
        拿到 (None, None) 直接拒。所以必须真的调一次 post()。

        两道闸都不放行，所以请求根本发不出去——不碰网络。
        """
        os.environ["JDY_WRITE_ALLOWLIST"] = "E1"
        os.environ.pop("JDY_ORG_WRITE", None)
        client = jc.JdyClient(api_key="x" * 8)
        for path in self.CORP_WRITES:
            with self.assertRaises(jc.OrgWriteRefused, msg=path) as cm:
                client.post(path, {"name": "某部门"})
            # 不能是"表单不在白名单里"那句——那句对通讯录毫无意义
            # （闸门的说明里会**解释**白名单为什么管不到这儿，那是另一回事）
            self.assertNotIn("不在名单里", str(cm.exception), path)
            self.assertIn(jc.ORG_WRITE_ENV, str(cm.exception), path)

    def test_post_still_gates_ordinary_form_writes_by_the_allowlist(self):
        """给通讯录开的这条岔路不许把普通表单写入也漏过去。"""
        os.environ["JDY_WRITE_ALLOWLIST"] = "E1"
        os.environ["JDY_ORG_WRITE"] = "1"
        client = jc.JdyClient(api_key="x" * 8)
        with self.assertRaises(jc.TargetError) as cm:
            client.post("/app/entry/data/update",
                        {"app_id": "A", "entry_id": "E2", "data_id": "d"})
        self.assertIn(jc.WRITE_ALLOWLIST_ENV, str(cm.exception))

    def test_the_gate_does_not_depend_on_the_body_being_a_dict(self):
        """整段闸门原来挂在 `isinstance(body, dict)` 下面。

        body 传成列表，刚焊死的通讯录闸门连同表单白名单**一起整体绕过去**——
        默认拒绝的闸门被一个形状判断连坐掉，是最糟的那种失效：它悄无声息。
        """
        os.environ["JDY_WRITE_ALLOWLIST"] = "E1"
        os.environ.pop("JDY_ORG_WRITE", None)
        client = jc.JdyClient(api_key="x" * 8)
        for body in ([{"name": "甲"}], "字符串", 123, None):
            for path in self.CORP_WRITES:
                with self.assertRaises(jc.OrgWriteRefused,
                                       msg="%s / %r" % (path, body)):
                    client.post(path, body)

    def test_a_form_write_with_a_shapeless_body_is_refused_too(self):
        """拿不到 app_id/entry_id 就不放行——默认拒绝，不是默认放行。"""
        os.environ["JDY_WRITE_ALLOWLIST"] = "E1"
        client = jc.JdyClient(api_key="x" * 8)
        with self.assertRaises(jc.TargetError) as cm:
            client.post("/app/entry/data/create", [{"app_id": "A", "entry_id": "E1"}])
        self.assertIn("无从判断要写哪张表", str(cm.exception))

    def test_the_refusal_reads_as_ours_not_as_an_api_error(self):
        """cli_main 把 TargetError 原样打给用户，把 JdyError 套上"简道云接口报错"。
        这是我们自己拦的，不是接口报的错。"""
        self.assertTrue(issubclass(jc.OrgWriteRefused, jc.TargetError))


class TestFilterJsonEntry(unittest.TestCase):
    """`--where` 直接给一段 filter JSON 时，手写出来的形状要能被接住。

    原来这条入口一点都不查：cond 写成对象就地抛 AttributeError（命令行工具
    甩 traceback），value 写成裸字符串会被 filter_value 当序列**逐字拆开**
    （"1000" → 四个值 1、0、0、0），method 拼错则直接发给接口——
    而简道云对不认识的 method 是**静默忽略**的，返回整表，看着像筛过了。
    """

    BY_LABEL = {"客户名称": {"name": "_w_n", "label": "客户名称", "type": "text"},
                "订单总额": {"name": "_w_a", "label": "订单总额", "type": "number"}}
    BY_NAME = {v["name"]: v for v in BY_LABEL.values()}

    def _build(self, spec):
        return jc.build_filter(spec, self.BY_LABEL, self.BY_NAME)

    def test_a_bare_string_value_is_not_split_into_characters(self):
        got = self._build('{"cond": [{"field": "客户名称", "method": "eq", '
                          '"value": "北京"}]}')
        self.assertEqual(got["cond"][0]["value"], ["北京"])

    def test_a_bare_number_string_is_coerced_not_shredded(self):
        got = self._build('{"cond": [{"field": "订单总额", "method": "lt", '
                          '"value": "1000"}]}')
        self.assertEqual(got["cond"][0]["value"], [1000])

    def test_a_single_cond_object_is_accepted(self):
        """只有一个条件时人常常忘了外面那层方括号——原来这里直接裸崩。"""
        got = self._build('{"cond": {"field": "客户名称", "method": "eq", '
                          '"value": "甲"}}')
        self.assertEqual(len(got["cond"]), 1)

    def test_a_wrong_method_is_refused_instead_of_silently_ignored(self):
        with self.assertRaises(ValueError) as cm:
            self._build('{"cond": [{"field": "客户名称", "method": "contains", '
                        '"value": "甲"}]}')
        self.assertIn("静默忽略", str(cm.exception))

    def test_a_cond_that_is_not_a_list_of_objects_is_refused(self):
        with self.assertRaises(ValueError):
            self._build('{"cond": ["客户名称=甲"]}')

    def test_an_unknown_field_name_is_still_refused_here(self):
        """字段名写错等于不筛选（接口返回整表）——JSON 这条入口也得守住。"""
        with self.assertRaises(ValueError):
            self._build('{"cond": [{"field": "不存在的列", "method": "eq", '
                        '"value": ["甲"]}]}')

    def test_a_non_string_spec_is_a_clean_error_not_a_crash(self):
        """哨兵的规则是从 YAML 读的：`when: 123` 拿到的是 int，
        `spec.lstrip()` 当场抛 AttributeError。

        那个异常**两层安全网都接不住**：调用方按规则 catch 的是
        (JdyError, ValueError)，cli_main 也只接 ValueError——于是整个哨兵进程
        因为一条规则写错一个字就死了，别的规则一条都没跑。
        """
        for spec in (123, {"field": "x"}, ["客户名称=甲"], True):
            with self.assertRaises(ValueError, msg=repr(spec)):
                self._build(spec)

    def test_a_bare_condition_at_the_top_level_is_still_validated(self):
        """顶层直接写一个条件、忘了外面那层 {"cond": [...]}。

        加固时留下的口子：`raw.get("cond", [])` 拿到空列表，下面五道校验
        **一道都走不到**，整个对象原样透传给接口——而字段名写错、method 拼错、
        值是裸字符串，这三样简道云全是静默忽略的。
        """
        got = self._build('{"field": "客户名称", "method": "eq", "value": "北京"}')
        self.assertEqual(got["cond"][0]["field"], "_w_n")     # 字段名解析了
        self.assertEqual(got["cond"][0]["value"], ["北京"])   # 裸标量补成了列表

        with self.assertRaises(ValueError):                   # method 也校验了
            self._build('{"field": "客户名称", "method": "contains", "value": "x"}')
        with self.assertRaises(ValueError):                   # 字段名也校验了
            self._build('{"field": "不存在的列", "method": "eq", "value": "x"}')

    def test_something_that_is_neither_a_filter_nor_a_condition_is_refused(self):
        with self.assertRaises(ValueError):
            self._build('{"rel": "and"}')

    def test_a_bad_rel_is_refused(self):
        with self.assertRaises(ValueError):
            self._build('{"rel": "并且", "cond": [{"field": "客户名称", '
                        '"method": "eq", "value": ["甲"]}]}')

    def test_field_names_are_still_resolved_to_widget_ids(self):
        got = self._build('{"cond": [{"field": "客户名称", "method": "eq", '
                          '"value": ["甲"]}]}')
        self.assertEqual(got["cond"][0]["field"], "_w_n")


class TestConfirmThreshold(unittest.TestCase):
    """规模闸门不该由它自己的参数解除。

    三个脚本原来把 `--confirm-threshold` 原样交给闸门，于是
    `--confirm-threshold 999999` 就是一句「把闸门拆了」。
    """

    def test_only_tightens(self):
        self.assertEqual(jc.confirm_threshold(999999), jc.CONFIRM_THRESHOLD)
        self.assertEqual(jc.confirm_threshold(10), 10)
        self.assertEqual(jc.confirm_threshold(None), jc.CONFIRM_THRESHOLD)
        self.assertEqual(jc.confirm_threshold("垃圾"), jc.CONFIRM_THRESHOLD)

    def test_gate_clamps_even_if_caller_forgets(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(jc.scale_gate(500, "code", None, 999999), 5)


class TestBatchCreateRowAlignment(unittest.TestCase):
    """空行必须在编码阶段剔除，提交序列与回读序列是同一份。

    原来空行留在 `encoded` 里、提交前才被过滤，于是回读比对拿到的是**未过滤**的
    序列：第 5 条的提交值挂到第 4 条的 data_id 名下，两边都"成功"，没有任何迹象。
    调用方（jdy-sync）照同一份错位写 ID 映射并落盘，下次同步就更新错记录。
    """

    WIDGETS = [W("客户", "text"), W("数量", "number")]

    class FakeClient(JdyClient):
        """不联网的 batch_create：记下真正发出去的批次与回读拿到的序列。"""

        def __init__(self, widgets):
            self._ws = widgets
            self.sent = []
            self.verified = None

        def field_map(self, *a, **kw):
            return ({w["label"]: w for w in self._ws}, {w["name"]: w for w in self._ws})

        def post(self, path, body):
            self.sent.append(body)
            n = len(body["data_list"])
            return {"success_count": n,
                    "success_ids": ["%024x" % (len(self.sent) * 100 + i) for i in range(n)]}

        def verify_written(self, app_id, entry_id, created_ids, submitted):
            self.verified = (list(created_ids), list(submitted))
            return {"checked": len(created_ids), "aligned": True, "clean": True,
                    "missing_rows": [], "silently_dropped": []}

    def _client(self):
        return self.FakeClient(self.WIDGETS)

    def test_empty_rows_are_dropped_at_encode_time(self):
        client = self._client()
        rows = [{"客户": "甲"}, {}, {"客户": "乙"}, {"没有这列": "x"}, {"客户": "丙"}]
        report = client.batch_create("APP", "E", rows, dry_run=False, verify=True)
        # 第 1 行是空字典、第 3 行只有表单里没有的列 —— 两条都编不出任何字段
        self.assertEqual(report["empty_rows"], [1, 3])
        self.assertEqual(report["submitted_rows"], [0, 2, 4])
        sent = [d for body in client.sent for d in body["data_list"]]
        self.assertEqual(len(sent), 3)

    def test_verify_gets_the_same_sequence_that_was_submitted(self):
        client = self._client()
        rows = [{"客户": "甲"}, {}, {"客户": "乙"}]
        client.batch_create("APP", "E", rows, dry_run=False, verify=True)
        ids, submitted = client.verified
        self.assertEqual(len(ids), len(submitted))
        # 提交序列里不能残留空行，否则 zip 会把「乙」的值对到「甲」的 data_id 上
        name = self.WIDGETS[0]["name"]
        self.assertEqual([d[name]["value"] for d in submitted], ["甲", "乙"])

    def test_all_empty_means_nothing_is_sent(self):
        client = self._client()
        report = client.batch_create("APP", "E", [{}, {}], dry_run=False, verify=True)
        self.assertEqual(client.sent, [])
        self.assertEqual(report["created_ids"], [])
        self.assertEqual(report["empty_rows"], [0, 1])
        self.assertEqual(report["chunks"], 0)


class TestVerifyRefusesToGuessAlignment(unittest.TestCase):
    """返回的 ID 数与提交行数对不上时，不许按前缀硬对。

    分块内部分成功就是这种情形：哪个 ID 是哪一行不可知，
    按前缀对出来的「丢失字段」会指向错的行——比不核对更糟。
    """

    def test_length_mismatch_is_reported_not_guessed(self):
        client = JdyClient.__new__(JdyClient)
        got = JdyClient.verify_written(client, "APP", "E", ["a" * 24], [{}, {}])
        self.assertFalse(got["clean"])
        self.assertIs(got["aligned"], False)
        self.assertIn("无法确定对应关系", got["reason"])
        self.assertEqual(got["silently_dropped"], [])


class TestToNumberIsOneRuler(unittest.TestCase):
    """数字口径全项目一份。

    原来 jdy-query 用裸 float()、jdy-report 另写一份（剥千分位逗号 + 拒 bool）：
    同一列文本数字，两个技能求和给出不同答案，而两边都不报错——
    用户拿到两个数，无从知道该信哪个。
    """

    def test_thousands_separators_are_stripped(self):
        self.assertEqual(jc.to_number("1,234.5"), 1234.5)
        self.assertEqual(jc.to_number("1，234"), 1234)     # 全角逗号也认

    def test_bool_is_not_a_number(self):
        # True 会被 float() 当成 1.0，把"是/否"列悄悄算进求和
        self.assertIsNone(jc.to_number(True))
        self.assertIsNone(jc.to_number(False))

    def test_unparsable_is_none_not_zero(self):
        # 当成 0 会把平均值拉低，且看不出来
        self.assertIsNone(jc.to_number("约一百"))
        self.assertIsNone(jc.to_number(None))
        self.assertIsNone(jc.to_number(""))

    def test_numbers_pass_through(self):
        self.assertEqual(jc.to_number(7), 7)
        self.assertEqual(jc.to_number(" 7.5 "), 7.5)

    def test_report_and_query_share_it(self):
        """两个技能各自的入口必须落到同一把尺子上。"""
        import importlib.util
        for skill, module, func in (("jdy-report", "aggregate", "_numeric"),):
            spec = importlib.util.spec_from_file_location(
                module, os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "skills", skill, "scripts", module + ".py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for probe in ("1,234.5", True, "约一百", 7):
                self.assertEqual(getattr(mod, func)(probe), jc.to_number(probe), probe)


class TestListAppsPagination(unittest.TestCase):
    """第 101 个应用不该是隐形的。

    原来固定 limit 100 不翻页：超出的应用在任何技能里都不存在，
    `--list` 看不到、按名字解析报的还是"找不到这个应用"——像是名字写错了。
    """

    class FakeClient(JdyClient):
        def __init__(self, total):
            self.apps = [{"app_id": "a%03d" % i, "name": "应用%d" % i} for i in range(total)]
            self.skips = []

        def post(self, path, body):
            self.skips.append(body.get("skip"))
            start = body.get("skip", 0)
            return {"apps": self.apps[start:start + body["limit"]]}

    def test_walks_every_page(self):
        client = self.FakeClient(250)
        got = client.list_apps()
        self.assertEqual(len(got), 250)
        self.assertEqual([a["app_id"] for a in got], [a["app_id"] for a in client.apps])
        self.assertEqual(client.skips, [0, 100, 200])

    def test_stops_on_a_short_page(self):
        client = self.FakeClient(30)
        self.assertEqual(len(client.list_apps()), 30)
        self.assertEqual(client.skips, [0])          # 不多打一次请求


class TestFetchRowsById(unittest.TestCase):
    """按 ID 取一批行的代价，要跟着这批 ID 走，不跟着表的大小走。

    原来回读核对无条件全表扫：50 万行的表核对 300 条要拉 5000 页。
    而 filter 这条路走不通——实测 `_id` 进 filter DSL 会被**静默忽略**，
    接口照常返回整表前 N 条；拿它核对等于把别人的行当成自己写的行，还"通过"。
    所以只能扫 + 逐条 get，两头都得有上界。
    """

    class FakeClient(JdyClient):
        def __init__(self, total):
            self.rows = [{"_id": "%024x" % i, "n": i} for i in range(total)]
            self.pages = 0
            self.gets = 0

        def iter_data(self, app_id, entry_id, **kw):
            size = kw.get("page_size") or 100
            for i, row in enumerate(self.rows):
                if i % size == 0:
                    self.pages += 1
                yield row

        def get_row(self, app_id, entry_id, data_id):
            self.gets += 1
            return next((r for r in self.rows if r["_id"] == data_id), None)

    def test_finds_them_and_stops_early(self):
        c = self.FakeClient(1000)
        want = [c.rows[0]["_id"], c.rows[5]["_id"]]
        got = c.fetch_rows_by_id("A", "E", want)
        self.assertEqual(sorted(got), sorted(want))
        self.assertEqual(c.gets, 0)                    # 头一页就齐了
        self.assertEqual(c.pages, 1)

    def test_big_table_falls_back_to_per_id_get(self):
        """表比这批 id 大得多时切成逐条 get——代价从表的大小上解绑。"""
        c = self.FakeClient(100000)
        want = [c.rows[99000]["_id"], c.rows[99500]["_id"]]
        got = c.fetch_rows_by_id("A", "E", want)
        self.assertEqual(sorted(got), sorted(want))
        self.assertEqual(c.gets, 2)
        self.assertLessEqual(c.pages, len(want) + 1)   # 没把十万行扫完

    def test_small_table_is_scanned_whole_without_extra_gets(self):
        c = self.FakeClient(50)
        want = [c.rows[49]["_id"]]
        self.assertEqual(list(c.fetch_rows_by_id("A", "E", want)), want)
        self.assertEqual(c.gets, 0)

    def test_missing_ids_are_simply_absent(self):
        c = self.FakeClient(20)
        got = c.fetch_rows_by_id("A", "E", [c.rows[1]["_id"], "f" * 24])
        self.assertEqual(list(got), [c.rows[1]["_id"]])

    def test_no_ids_costs_nothing(self):
        c = self.FakeClient(1000)
        self.assertEqual(c.fetch_rows_by_id("A", "E", []), {})
        self.assertEqual((c.pages, c.gets), (0, 0))


class TestDescribeTargetsDoesNotHideScale(unittest.TestCase):
    """行数封顶时要说出来，否则 5 万行的表和 300 行的表在清单里一样大。"""

    class FakeClient(JdyClient):
        def __init__(self, counts):
            self.counts = counts
            self.projections = []

        def list_forms(self, app_id):
            return [{"name": n, "entry_id": "e_" + n} for n in self.counts]

        def widgets(self, app_id, entry_id, refresh=False):
            return [{"name": "_widget_1", "label": "甲", "type": "text"}]

        def iter_data(self, app_id, entry_id, **kw):
            self.projections.append(kw.get("fields"))
            limit = kw.get("limit")
            for i in range(self.counts[entry_id[2:]]):
                if limit and i >= limit:
                    return
                yield {"_id": "%024x" % i}

    def test_capped_forms_are_marked(self):
        c = self.FakeClient({"大表": 5000, "小表": 7})
        got = {it["name"]: it for it in jc.describe_targets(c, "APP", limit=300)}
        self.assertEqual((got["大表"]["rows"], got["大表"]["capped"]), (300, True))
        self.assertEqual((got["小表"]["rows"], got["小表"]["capped"]), (7, False))

    def test_only_one_column_is_pulled(self):
        """数行数没必要把整行拉下来——实测 33 列的表投影后小 7 倍。"""
        c = self.FakeClient({"表": 5})
        jc.describe_targets(c, "APP")
        self.assertEqual(c.projections, [["_widget_1"]])

    def test_capped_shows_a_plus_sign(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jc.print_targets([{"name": "大表", "id": "e1", "rows": 300, "capped": True}], "表单：")
        self.assertIn("300+", out.getvalue())


class TestAggregateRows(unittest.TestCase):
    """分组与指标的引擎。jdy-report 与 jdy-query 原来各写一套。

    分叉的是**口径**，不是代码风格：一边剥千分位逗号一边不剥、
    一边把 True 当 1 一边不当、一边四舍五入一边不舍。
    两边都不报错，用户拿到两个数，无从知道该信哪个。
    """

    ROWS = [{"额": "1,200", "人": "张三"},
            {"额": 300.5, "人": "李四"},
            {"额": "约一百", "人": "张三"},      # 非数值
            {"额": True, "人": None},            # 布尔不是数字
            {"额": "", "人": "李四"}]
    R = staticmethod(lambda row, f: row.get(f) if row.get(f) not in ("", None) else None)

    def test_count_ignores_the_field(self):
        self.assertEqual(jc.aggregate_rows(self.ROWS, "count"), 5)

    def test_sum_skips_non_numeric_instead_of_zeroing_it(self):
        # 当成 0 会把平均值拉低，而且看不出来
        self.assertEqual(jc.aggregate_rows(self.ROWS, "sum", "额", self.R), 1500.5)
        self.assertEqual(jc.aggregate_rows(self.ROWS, "avg", "额", self.R), 750.25)

    def test_max_min(self):
        self.assertEqual(jc.aggregate_rows(self.ROWS, "max", "额", self.R), 1200)
        self.assertEqual(jc.aggregate_rows(self.ROWS, "min", "额", self.R), 300.5)

    def test_distinct_ignores_empty(self):
        self.assertEqual(jc.aggregate_rows(self.ROWS, "distinct", "人", self.R), 2)

    def test_no_usable_values_is_zero(self):
        self.assertEqual(jc.aggregate_rows([{"额": "abc"}], "sum", "额", self.R), 0)
        self.assertEqual(jc.aggregate_rows([], "sum", "额", self.R), 0)
        self.assertEqual(jc.aggregate_rows([], "distinct", "人", self.R), 0)

    def test_unknown_agg_and_missing_field_are_refused(self):
        with self.assertRaises(ValueError):
            jc.aggregate_rows(self.ROWS, "中位数", "额", self.R)
        with self.assertRaises(ValueError):
            jc.aggregate_rows(self.ROWS, "sum")          # 少了 field


class TestGroupRows(unittest.TestCase):
    ROWS = [{"区": "华东", "额": 1}, {"区": "", "额": 2},
            {"区": "华北", "额": 3}, {"区": "华东", "额": 4}, {"区": None, "额": 5}]

    def test_empty_values_go_to_unfilled_not_into_the_void(self):
        """丢掉空值会让各组之和小于总数，而看的人不会去做这道减法。"""
        got = dict(jc.group_rows(self.ROWS, ["区"]))
        self.assertEqual(sorted(k[0] for k in got), sorted(["华东", "华北", jc.UNFILLED]))
        self.assertEqual(sum(len(v) for v in got.values()), len(self.ROWS))
        self.assertEqual(len(got[(jc.UNFILLED,)]), 2)

    def test_no_dimensions_is_one_group(self):
        self.assertEqual(jc.group_rows(self.ROWS, []), [((), self.ROWS)])

    def test_multi_dimension_key_is_a_tuple(self):
        rows = [{"a": 1, "b": 2}, {"a": 1, "b": 3}, {"a": 1, "b": 2}]
        got = jc.group_rows(rows, ["a", "b"])
        self.assertEqual([k for k, _ in got], [("1", "2"), ("1", "3")])

    def test_order_is_by_key_and_reproducible(self):
        self.assertEqual([k for k, _ in jc.group_rows(self.ROWS, ["区"])],
                         sorted(k for k, _ in jc.group_rows(self.ROWS, ["区"])))


class TestBothSkillsAgreeOnTheNumbers(unittest.TestCase):
    """同一批行、同一个指标，jdy-report 与 jdy-query 必须给出**同一个数**。

    这是把聚合引擎下沉内核的全部意义。分叉时两边都不报错，
    用户拿到两个数、无从判断该信哪个——比报错难查得多。
    """

    ROWS = [{"_w_额": "1,200", "_w_人": {"name": "张三", "username": "sys_a"}},
            {"_w_额": 300.5, "_w_人": {"name": "李四", "username": "sys_b"}},
            {"_w_额": "约一百", "_w_人": None},
            {"_w_额": "", "_w_人": {"name": "张三", "username": "sys_a"}}]
    BY_LABEL = {"额": {"name": "_w_额", "label": "额", "type": "number"},
                "人": {"name": "_w_人", "label": "人", "type": "user"}}

    @staticmethod
    def _load(skill, module):
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts = os.path.join(root, "skills", skill, "scripts")
        sys.path.insert(0, scripts)
        try:
            spec = importlib.util.spec_from_file_location(
                "xagg_" + module, os.path.join(scripts, module + ".py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            sys.path.remove(scripts)

    def test_same_totals_from_both_engines(self):
        agg = self._load("jdy-report", "aggregate")
        query = self._load("jdy-query", "query")
        resolve = lambda row, label: jc.display_value(
            row.get(self.BY_LABEL[label]["name"]), self.BY_LABEL[label]["type"]) or None
        for kind in ("count", "sum", "avg", "max", "min", "distinct"):
            field = None if kind == "count" else "额"
            report_value = agg.compute_metric(
                self.ROWS, {"label": "x", "agg": kind, "field": field}, resolve)
            query_value = query.aggregate(
                self.ROWS, self.BY_LABEL, None, kind, field)[0][1]
            self.assertEqual(report_value, query_value,
                             "%s：report 给 %r，query 给 %r" % (kind, report_value, query_value))

    def test_same_grouping_from_both_engines(self):
        agg = self._load("jdy-report", "aggregate")
        query = self._load("jdy-query", "query")
        resolve = lambda row, label: jc.display_value(
            row.get(self.BY_LABEL[label]["name"]), self.BY_LABEL[label]["type"]) or None
        report_groups = {k[0]: agg.compute_metric(g, {"agg": "count"}, resolve)
                         for k, g in agg.group_by(self.ROWS, ["人"], resolve)}
        query_groups = dict(query.aggregate(self.ROWS, self.BY_LABEL, "人", "count", None))
        self.assertEqual(report_groups, query_groups)
        self.assertIn(jc.UNFILLED, report_groups)          # 空值两边同一个说法


class TestCreatePathExposesSkipped(unittest.TestCase):
    """新增路径也必须把"编码期就没提交的字段"交出来。

    三处 update 路径都记得报了，两处 batch_create 分支却忘了——
    根源是形状不一样：update 直接返回一个 skipped 列表，
    batch_create 把它埋在 report["skipped"] 的嵌套结构里，长得不像同一件事。
    回读核对只看**提交过的**字段，所以漏一整列时它照样报"没有静默丢失"。
    """

    WIDGETS = [W("客户", "text"), W("数量", "number")]

    class FakeClient(JdyClient):
        def __init__(self, widgets):
            self._ws = widgets

        def field_map(self, *a, **kw):
            return ({w["label"]: w for w in self._ws}, {w["name"]: w for w in self._ws})

        def post(self, path, body):
            n = len(body["data_list"])
            return {"success_count": n, "success_ids": ["%024x" % i for i in range(n)]}

        def verify_written(self, app_id, entry_id, created_ids, submitted):
            # 简道云真实行为：没提交过的字段当然"没有不一致"
            return {"checked": len(created_ids), "aligned": True, "clean": True,
                    "missing_rows": [], "silently_dropped": []}

    def test_not_submitted_is_flat_and_same_shape_as_update(self):
        c = self.FakeClient(self.WIDGETS)
        report = c.batch_create("A", "E", [{"客户": "甲", "数量": "约一百"}],
                                dry_run=False, verify=True)
        self.assertTrue(report["verification"]["clean"])       # 回读确实"干净"
        self.assertEqual([s["column"] for s in report["not_submitted"]], ["数量"])
        for item in report["not_submitted"]:                   # 和 update 的形状一致
            self.assertEqual(sorted(item), ["column", "kind", "reason", "row"])

    def test_empty_when_nothing_was_skipped(self):
        c = self.FakeClient(self.WIDGETS)
        report = c.batch_create("A", "E", [{"客户": "甲", "数量": 1}],
                                dry_run=False, verify=True)
        self.assertEqual(report["not_submitted"], [])


class TestPartialChunkFailureDoesNotSinkTheWholeBatch(unittest.TestCase):
    """某一块部分成功，只让**那一块**失去核对资格。

    原来是整批判断：200 行分两块，第二块少认了 60 条，
    第一块那 100 条本来同序等长、完全可以核对，也被一起放弃了。
    """

    WIDGETS = [W("客户", "text")]

    class FakeClient(JdyClient):
        def __init__(self, widgets, short_chunk):
            self._ws = widgets
            self.short = short_chunk
            self.calls = 0

        def field_map(self, *a, **kw):
            return ({w["label"]: w for w in self._ws}, {w["name"]: w for w in self._ws})

        def post(self, path, body):
            n = len(body["data_list"])
            keep = n - 60 if self.calls == self.short else n
            self.calls += 1
            return {"success_count": keep,
                    "success_ids": ["%024x" % (self.calls * 1000 + i) for i in range(keep)]}

        def verify_written(self, app_id, entry_id, created_ids, submitted):
            self.verified = (len(created_ids), len(submitted))
            return {"checked": len(created_ids), "aligned": True, "clean": True,
                    "missing_rows": [], "silently_dropped": []}

    def _run(self, short_chunk):
        c = self.FakeClient(self.WIDGETS, short_chunk)
        rows = [{"客户": "c%d" % i} for i in range(200)]
        return c, c.batch_create("A", "E", rows, dry_run=False, verify=True)

    def test_first_chunk_still_gets_checked(self):
        c, report = self._run(short_chunk=1)          # 第 2 块短
        self.assertEqual(c.verified, (100, 100))      # 第 1 块照常逐字段核对
        v = report["verification"]
        self.assertEqual(v["checked"], 100)
        self.assertEqual(v["unverified_rows"], 100)
        self.assertFalse(v["clean"])                  # 有没核对的行就不算干净

    def test_all_chunks_short_means_nothing_is_checked(self):
        c = self.FakeClient(self.WIDGETS, short_chunk=0)
        c.post = lambda path, body: {"success_count": 1, "success_ids": ["a" * 24]}
        report = c.batch_create("A", "E", [{"客户": "c%d" % i} for i in range(200)],
                                dry_run=False, verify=True)
        v = report["verification"]
        self.assertIs(v["aligned"], False)
        self.assertFalse(v["clean"])
        self.assertEqual(v["silently_dropped"], [])   # 不硬对，就什么都不报


class TestWritableBack(unittest.TestCase):
    """「读回来的值能不能原样写回去」只有内核一份。

    这条知识原来长在 jdy-sync 里，于是后来新写的 restore.py 没享受到，
    又踩了一遍同一个坑：拿 display_value 的产物去回写。
    """

    def test_still_unmeasured_types_are_refused(self):
        # deptgroup 账号里没有样本；image/upload/signature 要先走文件上传凭证接口；
        # subform 形状特殊（encode_row 另有分支）。见 tests/real/write_probe.py
        for wtype in ("deptgroup", "image", "upload", "signature", "subform"):
            ok, why = jc.writable_back({"label": "x", "type": wtype})
            self.assertFalse(ok, wtype)
            self.assertTrue(why)

    def test_measured_types_are_unlocked(self):
        """2026-08-31 实测解锁的五类：一次实验，四条写路径同时受益。"""
        for wtype in ("checkboxgroup", "combocheck", "dept", "company", "linkobject"):
            ok, _why = jc.writable_back({"label": "x", "type": wtype})
            self.assertTrue(ok, wtype)

    def test_system_and_unwritable_types_are_refused(self):
        for wtype in ("sn", "autonum", "linkdata"):
            ok, why = jc.writable_back({"label": "x", "type": wtype})
            self.assertFalse(ok, wtype)

    def test_plain_and_object_types_pass(self):
        # user/address/phone 的**原始值**是可以写回去的（display_value 的产物不行）
        for wtype in ("text", "number", "datetime", "user", "address", "phone", "lookup",
                      "dept", "linkobject", "company"):
            ok, _why = jc.writable_back({"label": "x", "type": wtype})
            self.assertTrue(ok, wtype)

    def test_sync_reads_the_same_list(self):
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts = os.path.join(root, "skills", "jdy-sync", "scripts")
        sys.path.insert(0, scripts)
        try:
            spec = importlib.util.spec_from_file_location(
                "wb_sync", os.path.join(scripts, "sync.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(scripts)
        self.assertIs(mod.UNVERIFIED_WRITE, jc.UNVERIFIED_WRITE)


class TestUnlockedWriteShapes(unittest.TestCase):
    """2026-08-31 逐类实测的写入形状（tests/real/write_probe.py）。

    每一条都对应一次"写进去、读回来"的真机观察，不是从文档抄的。
    这些类型此前整体压在 UNVERIFIED_WRITE 名单里，四条写路径只能整列排除。
    """

    def test_dept_takes_the_bare_number(self):
        """实测：只有裸 dept_no 整数能写进去。写 ID、读回展开对象——
        和 user 同一种不对称。"""
        w = W("部门", "dept")
        self.assertEqual(encode_value(w, 7), 7)
        self.assertEqual(encode_value(w, "7"), 7)
        self.assertEqual(encode_value(w, {"name": "研发部", "dept_no": 7}), 7)

    def test_dept_refuses_the_name_because_the_api_drops_it_silently(self):
        """实测：完整对象、{"dept_no": n} 之外的形状、部门名，全部静默丢弃
        （接口回报成功、字段存成 null）。所以编码期就要拦。"""
        with self.assertRaises(ValueError) as cm:
            encode_value(W("部门", "dept"), "研发部")
        self.assertIn("dept_no", str(cm.exception))
        with self.assertRaises(ValueError):
            encode_value(W("部门", "dept"), {"name": "研发部"})     # 没有 dept_no
        with self.assertRaises(ValueError):
            encode_value(W("部门", "dept"), True)                  # 布尔不是编号

    def test_linkobject_takes_a_link_id_object(self):
        """实测：{"link_id": data_id} 可写，接口自己补 link_form 和目标记录真名；
        裸 data_id 字符串报 3005。"""
        w = W("选择客户", "linkobject")
        did = "a" * 24
        self.assertEqual(encode_value(w, {"link_id": did}), {"link_id": did})
        self.assertEqual(encode_value(w, did), {"link_id": did})   # 裸串在这里包上
        self.assertEqual(
            encode_value(w, {"link_form": "f", "link_id": did, "name": "旧名"}),
            {"link_id": did})                                      # name 由接口覆盖，不带

    def test_linkobject_refuses_a_business_name(self):
        with self.assertRaises(ValueError):
            encode_value(W("选择客户", "linkobject"), "某某客户")

    def test_company_is_a_plain_string(self):
        self.assertEqual(encode_value(W("客户名称", "company"), "无锡某某"), "无锡某某")

    def test_multiselect_needs_a_list_because_a_bare_string_is_dropped(self):
        """实测：裸字符串和顿号拼接串都被**静默丢弃**，只有字符串列表能写。"""
        for wtype in ("checkboxgroup", "combocheck"):
            w = W("多选", wtype)
            self.assertEqual(encode_value(w, ["甲", "乙"]), ["甲", "乙"])
            self.assertEqual(encode_value(w, "甲、乙"), ["甲", "乙"])   # 拆成列表再提交

    def test_structured_values_are_refused_rather_than_stringified(self):
        """兜底分支原来对 dict/list 直接 str()，提交上去是一串 Python repr
        （"{'name': 'mdmouse'}"），接口收下、存成一坨没人看得懂的东西。"""
        with self.assertRaises(ValueError) as cm:
            encode_value(W("某个没实测过的类型", "leads_pool"), {"name": "x"})
        self.assertIn("没有已实测的写入形状", str(cm.exception))
        with self.assertRaises(ValueError):
            encode_value(W("某个没实测过的类型", "leads_pool"), [1, 2])


class TestAttachmentWriteShape(unittest.TestCase):
    """附件实测（2026-08-31）：写的是**上传后拿到的 key 组成的字符串列表**。

    `[{"key": k}]` 会被静默丢弃；读回来展开成
    `[{"name","size","mime","url"}]`，url 还带过期戳——
    所以**读回来的值不能直接回灌**，搬附件只能重新下载再上传。
    """

    def test_keys_go_in_as_a_list_of_strings(self):
        for wtype in ("upload", "image"):
            w = W("附件", wtype)
            self.assertEqual(encode_value(w, ["k1", "k2"]), ["k1", "k2"])
            self.assertEqual(encode_value(w, "k1"), ["k1"])          # 单个也包成列表

    def test_a_dict_carrying_a_key_is_accepted(self):
        self.assertEqual(encode_value(W("附件", "upload"), [{"key": "k1"}]), ["k1"])

    def test_the_read_back_shape_is_refused_with_the_reason(self):
        """读回来的对象里只有带过期戳的 url，没有 key——回灌是无效的，
        必须说清楚要重新上传，而不是让它静默变成一串没用的东西。"""
        readback = [{"name": "a.pdf", "size": 12, "mime": "application/pdf",
                     "url": "https://files.jiandaoyun.com/xxx?e=1789466399&token=…"}]
        with self.assertRaises(ValueError) as cm:
            encode_value(W("附件", "upload"), readback)
        self.assertIn("重新上传", str(cm.exception))


class TestAttachmentTransactionBinding(unittest.TestCase):
    """附件绑定在 transaction_id 上，只有同号的写入请求才能用那些文件。

    这个参数曾经被当成死参数删掉——它当时确实没人用，但它不是多余，
    是**功能还没做**。分块时每块必须用不同的事务号（相同的会互相覆盖），
    于是带附件就只能一批装得下，这条限制必须说出来而不是默默拆块。
    """

    WIDGETS = [W("名称", "text")]

    class FakeClient(JdyClient):
        def __init__(self, widgets):
            self._ws = widgets
            self.txns = []

        def field_map(self, *a, **kw):
            return ({w["label"]: w for w in self._ws}, {w["name"]: w for w in self._ws})

        def post(self, path, body):
            self.txns.append(body.get("transaction_id"))
            n = len(body["data_list"])
            return {"success_count": n,
                    "success_ids": ["%024x" % (len(self.txns) * 1000 + i) for i in range(n)]}

    def _rows(self, k):
        return [{"名称": "n%d" % i} for i in range(k)]

    def test_caller_supplied_id_is_used_verbatim(self):
        c = self.FakeClient(self.WIDGETS)
        c.batch_create("A", "E", self._rows(3), dry_run=False, verify=False,
                       transaction_id="txn-1")
        self.assertEqual(c.txns, ["txn-1"])

    def test_without_one_each_chunk_gets_its_own(self):
        """不带附件时照旧：每块一个事务号，否则后一块会覆盖前一块。"""
        c = self.FakeClient(self.WIDGETS)
        c.batch_create("A", "E", self._rows(250), dry_run=False, verify=False)
        self.assertEqual(len(c.txns), 3)
        self.assertEqual(len(set(c.txns)), 3)          # 三块三个号，互不相同

    def test_too_many_rows_with_attachments_is_refused_not_silently_split(self):
        """默默拆块会让后面几块的附件全部失效，而接口照样回报成功。"""
        c = self.FakeClient(self.WIDGETS)
        with self.assertRaises(ValueError) as cm:
            c.batch_create("A", "E", self._rows(150), dry_run=False, verify=False,
                           transaction_id="txn-1")
        self.assertIn("附件", str(cm.exception))
        self.assertEqual(c.txns, [])                   # 一个请求都没发出去


class TestMultipartBody(unittest.TestCase):
    """multipart 是手搓的（本项目不引第三方依赖），所以它自己要被测。"""

    def test_file_is_the_last_part_because_the_api_requires_it(self):
        body, ctype = jc._multipart({"token": "T"}, "a.txt", b"hi", "text/plain")
        text = body.decode("utf-8")
        self.assertLess(text.index('name="token"'), text.index('name="file"'))

    def test_boundary_matches_the_content_type(self):
        body, ctype = jc._multipart({"token": "T"}, "a.txt", b"hi", "text/plain")
        boundary = ctype.split("boundary=")[1]
        self.assertTrue(body.decode("utf-8").startswith("--" + boundary))
        self.assertTrue(body.decode("utf-8").endswith("--%s--\r\n" % boundary))

    def test_binary_content_survives(self):
        raw = bytes(range(256))
        body, _ = jc._multipart({"token": "T"}, "a.bin", raw, "application/octet-stream")
        self.assertIn(raw, body)

    def test_filename_and_mime_are_declared(self):
        body, _ = jc._multipart({"token": "T"}, "报告.pdf", b"x", "application/pdf")
        text = body.decode("utf-8")
        self.assertIn('filename="报告.pdf"', text)
        self.assertIn("Content-Type: application/pdf", text)

    def test_transaction_ids_are_unique(self):
        self.assertNotEqual(jc.new_transaction_id(), jc.new_transaction_id())


class TestAttachmentDownload(unittest.TestCase):
    """附件 url 带 e= 过期戳（约 15 天）——**导出的表里放 url 是没用的**，
    用户过两周点开全是死链。要么当场下载，要么只留文件名。"""

    class FakeClient(JdyClient):
        def __init__(self):
            self.user_agent = "t"
            self.timeout = 5

    def test_duplicate_names_do_not_overwrite_each_other(self):
        """附件的 name 是用户上传时的原名，一张表里重名太正常了；
        覆盖掉就等于悄悄丢文件。实测导出时确实撞上了 7 个同名 jpg。"""
        c = self.FakeClient()
        tmp = tempfile.mkdtemp()
        saved = jc.urllib.request.urlopen

        class Resp(object):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"x"

        jc.urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            names = [os.path.basename(c.download_file("http://x/f", tmp, "同名.jpg"))
                     for _ in range(3)]
        finally:
            jc.urllib.request.urlopen = saved
        self.assertEqual(names, ["同名.jpg", "同名-2.jpg", "同名-3.jpg"])
        self.assertEqual(len(os.listdir(tmp)), 3)

    def test_path_separators_in_a_filename_cannot_escape_the_directory(self):
        """附件名是用户填的，里面有 / 的话就成了往别处写文件。"""
        c = self.FakeClient()
        tmp = tempfile.mkdtemp()
        saved = jc.urllib.request.urlopen

        class Resp(object):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"x"

        jc.urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            path = c.download_file("http://x/f", tmp, "../../etc/passwd")
        finally:
            jc.urllib.request.urlopen = saved
        self.assertEqual(os.path.dirname(os.path.abspath(path)), os.path.abspath(tmp))


class TestCopyAttachmentsKeepsTheOriginalName(unittest.TestCase):
    """搬附件时**文件名要显式带过去**。

    2026-09-01 `copy_attachments()` 首次真机调用当场抓到：一格里两个同名附件，
    `download_file` 为了不覆盖把第二个存成 `timg (8)-2.jpg`，上传半却拿本地文件名
    当文件名——目标端于是多出一个源端根本没有的 `timg (8)-2.jpg`。

    对同步是致命的：附件 diff 按文件名比，源与目标**永远对不上**，
    每次重跑都判「有变化」再重传一遍，幂等直接破了。
    下载半为防覆盖改了名、上传半当它没改，又一次「两半不对称」。
    """

    class FakeClient(JdyClient):
        def __init__(self):
            self.user_agent = "t"
            self.timeout = 5
            self.uploaded = []

        def upload_tokens(self, app_id, entry_id, transaction_id, need=1):
            return [{"url": "http://up/%d" % i, "token": "T%d" % i} for i in range(need)]

        def upload_file(self, url, token, path, filename=None, mime=None):
            # 真的 upload_file 不给 filename 时就退回本地文件名——这里照搬那条规则，
            # 否则这条测试在旧代码上也会绿。
            self.uploaded.append(filename or os.path.basename(path))
            return "key-%d" % len(self.uploaded)

    def _copy(self, values):
        c = self.FakeClient()
        saved = jc.urllib.request.urlopen

        class Resp(object):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"x"

        jc.urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            keys = c.copy_attachments(values, "A", "E", "txn-1",
                                      workdir=tempfile.mkdtemp())
        finally:
            jc.urllib.request.urlopen = saved
        return c, keys

    def test_two_same_named_attachments_keep_that_name(self):
        c, keys = self._copy([{"name": "同名.jpg", "url": "http://x/1", "size": 9},
                              {"name": "同名.jpg", "url": "http://x/2", "size": 9}])
        self.assertEqual(len(keys), 2)
        # 旧代码这里是 ["同名.jpg", "同名-2.jpg"]——第二个名字是本地防覆盖改出来的
        self.assertEqual(c.uploaded, ["同名.jpg", "同名.jpg"])

    def test_distinct_names_are_untouched(self):
        c, _ = self._copy([{"name": "a.jpg", "url": "http://x/1"},
                           {"name": "b.jpg", "url": "http://x/2"}])
        self.assertEqual(c.uploaded, ["a.jpg", "b.jpg"])

    def test_names_must_line_up_with_paths(self):
        """名字和文件一旦错位就是张冠李戴，而且两边都"成功"，回读也看不出来。"""
        c = self.FakeClient()
        with self.assertRaises(ValueError):
            c.upload_files("A", "E", ["/tmp/a", "/tmp/b"], "txn", names=["只有一个"])


class TestFilterValueTypes(unittest.TestCase):
    """条件值的类型必须跟着字段类型走。

    实测（2026-08-31）：简道云对**类型不匹配的 filter 静默忽略**——
    数字字段传 `["1000"]` 字符串，接口照常 200、把**整表**还给你。
    真机上 `订单总额:lt:1000` 返回了全部 26 行、最大值 16080，而调用方以为筛过了。

    这和"不认识的字段"是同一种事故的两个入口：resolve_filter_field 一直守着
    字段名那一侧，值的类型这一侧没人守——所以 --where 里的**数字比较从来
    就没真正生效过**（jdy-query、excel-bridge 导出、jdy-watch 都受影响）。
    """

    BY_LABEL = {"金额": W("金额", "number"), "名称": W("名称", "text"),
                "日期": W("日期", "datetime")}
    BY_NAME = {w["name"]: w for w in BY_LABEL.values()}

    def _value(self, spec):
        return build_filter(spec, self.BY_LABEL, self.BY_NAME)["cond"][0]["value"]

    def test_number_fields_get_numbers_not_strings(self):
        self.assertEqual(self._value("金额:lt:1000"), [1000])
        self.assertEqual(self._value("金额:gt:9.5"), [9.5])

    def test_a_thousand_separator_is_caught_not_silently_split(self):
        """逗号是多值分隔符，`1,000` 会被切成两个值——而 lt 收两个值没有意义。
        原来它会静默变成 [1.0, 0.0]。"""
        with self.assertRaises(ValueError) as cm:
            self._value("金额:lt:1,000")
        self.assertIn("逗号是多值分隔符", str(cm.exception))

    def test_multi_value_methods_still_take_many(self):
        self.assertEqual(self._value("金额:in:1,2,3"), [1, 2, 3])
        self.assertEqual(self._value("金额:range:1,100"), [1, 100])

    def test_a_non_number_is_refused_instead_of_silently_ignored(self):
        with self.assertRaises(ValueError) as cm:
            self._value("金额:lt:一千")
        self.assertIn("静默忽略", str(cm.exception))

    def test_datetime_fields_are_normalised(self):
        self.assertEqual(self._value("日期:gt:2026-08-27"),
                         [normalize_datetime("2026-08-27")])

    def test_text_fields_are_left_as_strings(self):
        self.assertEqual(self._value("名称=甲"), ["甲"])

    def test_value_less_methods_are_untouched(self):
        cond = build_filter("金额:empty:", self.BY_LABEL, self.BY_NAME)["cond"][0]
        self.assertNotIn("value", cond)

    def test_raw_filter_json_is_coerced_too(self):
        """直接给 filter JSON 的那条路同样会踩这个坑。"""
        spec = '{"rel": "and", "cond": [{"field": "金额", "method": "lt", "value": ["1000"]}]}'
        got = build_filter(spec, self.BY_LABEL, self.BY_NAME)
        self.assertEqual(got["cond"][0]["value"], [1000])


class _FakeStdin(object):
    """假 stdin：**"自称是不是 tty" 和 "读起来会怎样" 是两件独立的事。**

    真实世界里它们会打架，而那正是这组测试的全部理由：
    Windows 的 `NUL` 是字符设备，`isatty()` 返回 **True**，
    可它一读就是 EOF。POSIX 的 `/dev/null` 则老老实实说自己不是 tty。
    所以夹具把两者分开设。
    """

    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        if isinstance(self._tty, Exception):
            raise self._tty
        return self._tty


@contextlib.contextmanager
def _stdin_says(tty, answer=None):
    """tty=isatty() 的返回（或要抛的异常）；answer=input() 的返回（或要抛的异常）。"""
    saved_stdin, saved_input = sys.stdin, builtins.input
    calls = []
    sys.stdin = None if tty is None else _FakeStdin(tty)

    def _input(prompt=""):
        calls.append(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    builtins.input = _input
    try:
        yield calls
    finally:
        sys.stdin = saved_stdin
        builtins.input = saved_input


class TestAskYes(unittest.TestCase):
    """`ask_yes` 是七处写入闸门 + 三处发送闸门唯一的"能不能问人"判据。

    它必须分得清三种情况，因为调用方要按三种情况分别处置：
      · None  —— **问不了**（不是 tty，或读到 EOF）→ 拒绝执行，退出码 4；
      · False —— 问了，用户没说 yes            → 已取消，退出码 0；
      · True  —— 用户说了 yes                  → 继续。
    把 None 和 False 混成一路，非交互环境就会被当成"用户说了不"而静静返回 0，
    调用方拿不到约定的 4，也就不知道该回头去找用户确认。
    """

    def test_not_a_tty_is_none_and_never_asks(self):
        with _stdin_says(tty=False, answer="yes") as calls:
            self.assertIsNone(ask_yes("确认？"))
        self.assertEqual([], calls, "不是 tty 就不该调用 input()")

    def test_windows_nul_claims_to_be_a_tty_but_input_hits_eof(self):
        """**本函数存在的理由。**

        `stdin=subprocess.DEVNULL` 在 POSIX 上给的是 /dev/null（isatty False），
        在 Windows 上给的是 `NUL`——字符设备，**isatty() 返回 True**。
        于是手写的 `if sys.stdin.isatty(): input(...)` 在 Windows 上会走进 input()，
        第一次读就 EOFError：脚本带着 traceback 以退出码 1 死掉，
        说好的"拒绝写入：当前是非交互环境"一个字也没说出来。
        这条模拟的就是那台机器：说自己是 tty，读起来立刻 EOF。
        """
        with _stdin_says(tty=True, answer=EOFError()) as calls:
            self.assertIsNone(ask_yes("确认？"),
                              "EOF 是'问不了'，绝不能当成同意，也不能让异常漏出去")
        self.assertEqual(1, len(calls), "这一路是真的试着问了一次")

    def test_yes_is_the_only_yes(self):
        for answer in ("yes", " yes ", "yes\n"):
            with _stdin_says(tty=True, answer=answer):
                self.assertIs(True, ask_yes("确认？"), repr(answer))

    def test_anything_else_is_a_no_not_a_none(self):
        """"输入了别的"和"问不了"必须区分：前者是用户表了态，走"已取消"。"""
        for answer in ("", "y", "no", "YES", "是", "yes please"):
            with _stdin_says(tty=True, answer=answer):
                self.assertIs(False, ask_yes("确认？"), repr(answer))

    def test_no_stdin_at_all_is_none(self):
        """pythonw / 某些宿主下 sys.stdin 是 None——问不了，不是同意。"""
        with _stdin_says(tty=None):
            self.assertIsNone(ask_yes("确认？"))

    def test_a_closed_stdin_is_none(self):
        """isatty() 自己抛 ValueError（文件已关闭）时同样是"问不了"。"""
        with _stdin_says(tty=ValueError("I/O operation on closed file")):
            self.assertIsNone(ask_yes("确认？"))

    def test_prompt_is_only_printed_when_it_can_actually_ask(self):
        """非交互时不该先打一句无人应答的提示语——它会把 stderr 上的拒绝文案冲淡。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with _stdin_says(tty=False, answer="yes"):
                ask_yes("即将写入 3 行。\n确认？输入 yes 继续：")
        self.assertEqual("", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
