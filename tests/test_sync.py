# -*- coding: utf-8 -*-
"""jdy-sync 引擎测试。

同步出错的方式大多是"安静的"：顺序反了引用翻译不出来、映射表漏登记导致
下次重复写、引用指向不存在的记录还回读"一致"。所以这里把边界钉死。

    python3 tests/test_sync.py
"""
import datetime
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "jdy-sync", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from _fixtures import unwritable_path  # noqa: E402
from jdy_client import JdyError  # noqa: E402
from miniyaml import parse as parse_yaml  # noqa: E402
import apply as sync_apply  # noqa: E402
import init_config  # noqa: E402
import relink  # noqa: E402
import sources  # noqa: E402
import plan as plan_mod  # noqa: E402
from sync import (IdMap, SyncError, business_key, canonical, load_config,  # noqa: E402
                  plan_fingerprint, plan_table,
                  resolve_fields, resolve_path, row_values, sync_shape, sync_value,
                  table_shapes, topo_sort, translate_refs, verify_complex)

W = lambda label, wtype: {"name": "_w_%s" % label, "label": label, "type": wtype}


def SUB(label, items):
    """造一个子表单 widget。items 是 [(内层显示名, 类型)]。"""
    return {"name": "_w_%s" % label, "label": label, "type": "subform",
            "items": [{"name": "_w_%s_%s" % (label, l), "label": l, "type": t}
                      for l, t in items]}


class TestTopoSort(unittest.TestCase):
    def test_referenced_table_comes_first(self):
        tables = [{"alias": "b", "refs": {"字段": "a"}}, {"alias": "a"}]
        self.assertEqual([t["alias"] for t in topo_sort(tables)], ["a", "b"])

    def test_chain(self):
        tables = [{"alias": "c", "refs": {"f": "b"}},
                  {"alias": "b", "refs": {"f": "a"}},
                  {"alias": "a"}]
        self.assertEqual([t["alias"] for t in topo_sort(tables)], ["a", "b", "c"])

    def test_diamond_each_visited_once(self):
        tables = [{"alias": "d", "refs": {"x": "b", "y": "c"}},
                  {"alias": "b", "refs": {"x": "a"}},
                  {"alias": "c", "refs": {"x": "a"}},
                  {"alias": "a"}]
        order = [t["alias"] for t in topo_sort(tables)]
        self.assertEqual(len(order), 4)
        self.assertLess(order.index("a"), order.index("b"))
        self.assertLess(order.index("b"), order.index("d"))

    def test_cycle_rejected_with_path(self):
        """成环时无法确定先同步谁，必须报错而不是随便选一个顺序。"""
        tables = [{"alias": "a", "refs": {"f": "b"}}, {"alias": "b", "refs": {"f": "a"}}]
        with self.assertRaises(SyncError) as ctx:
            topo_sort(tables)
        self.assertIn("成环", str(ctx.exception))


class TestConfigValidation(unittest.TestCase):
    BASE = ("name: t\nsource:\n  app: A\ntarget:\n  app: B\ntables:\n"
            "  - alias: x\n    source_entry: s\n    target_entry: d\n    key: K\n")

    def _load(self, text):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "c.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return load_config(path, parse_yaml)

    def test_valid(self):
        cfg = self._load(self.BASE)
        self.assertEqual(cfg["tables"][0]["alias"], "x")

    def test_missing_target_rejected(self):
        with self.assertRaises(SyncError):
            self._load("name: t\nsource:\n  app: A\ntables: [{alias: x}]\n")

    def test_missing_key_rejected(self):
        with self.assertRaises(SyncError) as ctx:
            self._load("name: t\nsource:\n  app: A\ntarget:\n  app: B\ntables:\n"
                       "  - alias: x\n    source_entry: s\n    target_entry: d\n")
        self.assertIn("key", str(ctx.exception))

    def test_duplicate_alias_rejected(self):
        with self.assertRaises(SyncError):
            self._load(self.BASE + "  - alias: x\n    source_entry: s2\n"
                                   "    target_entry: d2\n    key: K\n")

    def test_ref_to_unknown_alias_rejected(self):
        """指向不存在的 alias 若不拦，运行时会静默翻译不出引用。"""
        with self.assertRaises(SyncError) as ctx:
            self._load(self.BASE + "    refs:\n      字段: 不存在的表\n")
        self.assertIn("未定义", str(ctx.exception))


class TestPathResolution(unittest.TestCase):
    """相对路径按**配置文件所在目录**解析，不按当前工作目录。

    否则同一份配置换个目录跑会另建一份空映射表，两份悄悄分裂——
    而且备份文件本来就按配置目录落，两套规则并存更容易出错。
    """

    # 期望值一律过一遍 os.path.normpath：实现里就是 normpath(join(...))，
    # 而 Windows 上它给的是反斜杠（`\\data\\cfg\\idmap.json`）。
    # 把 "/data/cfg/idmap.json" 写死在断言里，等于要求实现返回 POSIX 分隔符——
    # 那才是错的：路径最后要拿去 open()，就该是本平台的写法。**这里改的是期望，
    # 不是实现。**

    def test_relative_resolves_against_config_dir(self):
        self.assertEqual(resolve_path("./idmap.json", "/data/cfg"),
                         os.path.normpath("/data/cfg/idmap.json"))

    def test_bare_name_resolves_against_config_dir(self):
        self.assertEqual(resolve_path("idmap.json", "/data/cfg"),
                         os.path.normpath("/data/cfg/idmap.json"))

    def test_absolute_kept(self):
        # 本平台的绝对路径：Windows 上 "/var/x" 没盘符，3.13 起不算绝对路径
        absp = os.path.abspath(os.path.join(os.sep, "var", "x", "m.json"))
        self.assertEqual(resolve_path(absp, "/data/cfg"), absp)

    def test_home_expanded(self):
        got = resolve_path("~/m.json", "/data/cfg")
        self.assertTrue(os.path.isabs(got))
        self.assertNotIn("~", got)

    def test_parent_relative(self):
        self.assertEqual(resolve_path("../m.json", "/data/cfg"),
                         os.path.normpath("/data/m.json"))

    def test_load_config_exposes_resolved_path(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "c.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("name: t\nsource:\n  app: A\ntarget:\n  app: B\n"
                     "id_map: ./m.json\ntables:\n"
                     "  - alias: x\n    source_entry: s\n    target_entry: d\n    key: K\n")
        cfg = load_config(path, parse_yaml)
        self.assertEqual(cfg["_id_map_path"], os.path.join(tmp, "m.json"))
        self.assertEqual(cfg["_base_dir"], tmp)

    def test_default_id_map_also_config_relative(self):
        """没写 id_map 时也不该落到当前工作目录。"""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "c.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("name: t\nsource:\n  app: A\ntarget:\n  app: B\ntables:\n"
                     "  - alias: x\n    source_entry: s\n    target_entry: d\n    key: K\n")
        cfg = load_config(path, parse_yaml)
        self.assertEqual(os.path.dirname(cfg["_id_map_path"]), tmp)


class TestFieldResolution(unittest.TestCase):
    SRC = {"名称": W("名称", "text"), "数量": W("数量", "number"),
           "编号": W("编号", "sn"), "选择项": W("选择项", "linkdata"),
           "关联项": W("关联项", "lookup")}

    def test_default_maps_same_names(self):
        dst = {"名称": W("名称", "text"), "数量": W("数量", "number")}
        mapping, excluded = resolve_fields(self.SRC, dst, None)
        self.assertEqual(mapping, {"名称": "名称", "数量": "数量"})
        self.assertEqual(len(excluded), 3)          # sn / linkdata / 目标没有的

    def test_linkdata_excluded_with_actionable_reason(self):
        dst = {"选择项": W("选择项", "linkdata")}
        _, excluded = resolve_fields(self.SRC, dst, {"选择项": "选择项"})
        self.assertIn("关联数据", dict(excluded)["选择项"])

    def test_unverified_write_types_excluded(self):
        """写入格式没实测过的类型宁可不搬——搬过去静默丢比不搬更糟。"""
        src = {"签名": W("签名", "signature")}
        dst = {"签名": W("签名", "signature")}
        _, excluded = resolve_fields(src, dst, {"签名": "签名"})
        self.assertIn("尚未实测", dict(excluded)["签名"])

    def test_attachments_and_subforms_are_syncable_now(self):
        """附件与子表单**能搬了**（W2/W3）。这条钉住的是"不再整列排除"——
        它们曾经因为"读回来的形状不是能写回去的形状"被一票否决。"""
        src = {"附件": W("附件", "upload"), "图片": W("图片", "image"),
               "明细": SUB("明细", [("子文本", "text")])}
        mapping, excluded = resolve_fields(src, dict(src), None)
        self.assertEqual(set(mapping), {"附件", "图片", "明细"})
        self.assertEqual(excluded, [])

    def test_cross_type_pairs_are_still_refused(self):
        """D9：只在同类之间搬。子表单↔文本、附件↔文本都不是"搬"，是转换——
        转换要另一个功能，不该在同步里悄悄发生。"""
        cases = [(SUB("明细", [("子文本", "text")]), W("明细", "text")),
                 (W("明细", "text"), SUB("明细", [("子文本", "text")])),
                 (W("附件", "upload"), W("附件", "text")),
                 (W("附件", "text"), W("附件", "upload"))]
        for sw, dw in cases:
            mapping, excluded = resolve_fields({sw["label"]: sw}, {dw["label"]: dw},
                                               {sw["label"]: dw["label"]})
            self.assertEqual(mapping, {}, "%s→%s 不该放行" % (sw["type"], dw["type"]))
            self.assertIn("只在", dict(excluded)[sw["label"]])

    def test_image_and_upload_are_the_same_family(self):
        """图片和附件都是附件（ATTACHMENT_TYPES），互相搬算同类。"""
        mapping, _ = resolve_fields({"图": W("图", "image")}, {"件": W("件", "upload")},
                                    {"图": "件"})
        self.assertEqual(mapping, {"图": "件"})

    def test_subform_with_no_matching_inner_field_is_excluded(self):
        """内层按显示名对应，一个都对不上就整列搬不过去——要说清楚是"内层对不上"，
        而不是含糊一句"搬不动"。"""
        src = {"明细": SUB("明细", [("甲", "text")])}
        dst = {"明细": SUB("明细", [("乙", "text")])}
        mapping, excluded = resolve_fields(src, dst, {"明细": "明细"})
        self.assertEqual(mapping, {})
        self.assertIn("内层字段一个都对不上", dict(excluded)["明细"])

    def test_inner_fields_that_cannot_move_are_named_one_by_one(self):
        """D2/D3：内层搬不动的字段**逐个报出来**，格式「子表单名.内层名」。

        整列搬得动的时候最容易出事：只报"这列搬了"，用户以为整块都搬了，
        而内层的关联数据、附件其实一个没动。
        """
        src = {"明细": SUB("明细", [("子文本", "text"), ("子关联", "lookup"),
                                    ("子附件", "upload"), ("子选择", "linkdata"),
                                    ("目标没有的", "text")])}
        dst = {"明细": SUB("明细", [("子文本", "text"), ("子关联", "lookup"),
                                    ("子附件", "upload"), ("子选择", "linkdata")])}
        mapping, excluded = resolve_fields(src, dst, {"明细": "明细"})
        self.assertEqual(mapping, {"明细": "明细"})       # 整列还是搬得动的
        why = dict(excluded)
        self.assertIn("关联数据", why["明细.子关联"])
        self.assertIn("附件", why["明细.子附件"])
        self.assertIn("选择数据", why["明细.子选择"])
        self.assertIn("没有同名的内层字段", why["明细.目标没有的"])

    def test_phone_and_address_are_syncable(self):
        """已实测可写，不该再被排除。"""
        src = {"地址": W("地址", "address"), "手机": W("手机", "phone")}
        dst = {"地址": W("地址", "address"), "手机": W("手机", "phone")}
        mapping, excluded = resolve_fields(src, dst, None)
        self.assertEqual(set(mapping), {"地址", "手机"})
        self.assertEqual(excluded, [])

    def test_sn_excluded(self):
        dst = {"编号": W("编号", "sn")}
        _, excluded = resolve_fields(self.SRC, dst, {"编号": "编号"})
        self.assertIn("系统生成", dict(excluded)["编号"])

    def test_lookup_is_mappable_when_declared_in_refs(self):
        """关联数据可写——不能和选择数据一起被排除掉。但要在 refs 里声明。"""
        dst = {"关联项": W("关联项", "lookup")}
        mapping, _ = resolve_fields(self.SRC, dst, {"关联项": "关联项"},
                                    ref_fields={"关联项"})
        self.assertEqual(mapping, {"关联项": "关联项"})

    def test_undeclared_lookup_is_excluded_not_copied_verbatim(self):
        """没在 refs 里声明的关联数据字段**不搬**。

        原来它会走普通字段映射，把**源应用的 data_id** 原样写进目标应用——
        那个 ID 在目标端要么指向另一条记录、要么什么都不指。
        接口不校验引用，写进去照样"成功"；回读比对也发现不了，
        因为存进去的确实就是提交的那个字符串。这是本项目见过最隐蔽的脏数据。
        """
        dst = {"关联项": W("关联项", "lookup")}
        mapping, excluded = resolve_fields(self.SRC, dst, {"关联项": "关联项"})
        self.assertEqual(mapping, {})
        self.assertEqual(len(excluded), 1)
        self.assertIn("refs", excluded[0][1])

    def test_renamed_target_field(self):
        dst = {"品名": W("品名", "text")}
        mapping, _ = resolve_fields(self.SRC, dst, {"名称": "品名"})
        self.assertEqual(mapping, {"名称": "品名"})

    def test_fields_is_a_whitelist_and_omissions_are_reported(self):
        """给了 fields 就只搬列出的——这没问题；问题是"两边都有却没列出"的字段
        以前既不搬也不报，凭空消失，同步完才发现整列是空的。"""
        src = {"名称": W("名称", "text"), "部门": W("部门", "text"),
               "职务": W("职务", "text")}
        dst = {"品名": W("品名", "text"), "部门": W("部门", "text"),
               "职务": W("职务", "text")}
        mapping, excluded = resolve_fields(src, dst, {"名称": "品名"})
        self.assertEqual(mapping, {"名称": "品名"})
        omitted = {n for n, why in excluded if "白名单" in why}
        self.assertEqual(omitted, {"部门", "职务"})

    def test_no_fields_means_all_same_name_pairs(self):
        src = {"a": W("a", "text"), "b": W("b", "text")}
        dst = {"a": W("a", "text"), "b": W("b", "text")}
        mapping, excluded = resolve_fields(src, dst, None)
        self.assertEqual(set(mapping), {"a", "b"})
        self.assertEqual(excluded, [])

    def test_source_only_field_not_reported_as_whitelist_omission(self):
        """源端独有的字段本就搬不了，不该混进"没写进 fields"那类。"""
        src = {"名称": W("名称", "text"), "源端独有": W("源端独有", "text")}
        dst = {"品名": W("品名", "text")}
        _, excluded = resolve_fields(src, dst, {"名称": "品名"})
        self.assertNotIn("源端独有", {n for n, why in excluded if "白名单" in why})


class TestIdMap(unittest.TestCase):
    def test_roundtrip(self):
        path = os.path.join(tempfile.mkdtemp(), "m.json")
        m = IdMap(path)
        m.put("a", "s1", "t1")
        self.assertTrue(m.save())
        self.assertEqual(IdMap(path).get("a", "s1"), "t1")

    def test_missing_returns_none(self):
        self.assertIsNone(IdMap(os.path.join(tempfile.mkdtemp(), "x.json")).get("a", "s"))

    def test_unwritable_path_flags_readonly(self):
        """映射表写不下去要能被发现——否则下次同步会把已同步的当新增重写一遍。"""
        # 为什么不能再写 /proc（Windows 上那是可写的 D:\\proc\\…）：见 tests/_fixtures.py
        m = IdMap(unwritable_path("m.json"))
        m.put("a", "s", "t")
        self.assertFalse(m.save())
        self.assertTrue(m.readonly)


class TestRefTranslation(unittest.TestCase):
    SRC = {"关联项": W("关联项", "lookup")}
    TABLE = {"alias": "child", "refs": {"关联项": "parent"}}

    def test_translates_via_id_map(self):
        m = IdMap(os.path.join(tempfile.mkdtemp(), "m.json"))
        m.put("parent", "src-1", "dst-1")
        row = {"_w_关联项": "src-1"}
        translated, unresolved = translate_refs(row, self.SRC, self.TABLE, m)
        self.assertEqual(translated, {"关联项": "dst-1"})
        self.assertEqual(unresolved, [])

    def test_unmapped_ref_reported_not_written(self):
        """翻译不出来的绝不能照抄源 ID——那会写出一个指向虚无的引用，
        而且回读比对也发现不了。"""
        m = IdMap(os.path.join(tempfile.mkdtemp(), "m.json"))
        row = {"_w_关联项": "src-未同步"}
        translated, unresolved = translate_refs(row, self.SRC, self.TABLE, m)
        self.assertEqual(translated, {})
        self.assertEqual(len(unresolved), 1)
        self.assertIn("尚未同步", unresolved[0][1])

    def test_object_form_ref(self):
        m = IdMap(os.path.join(tempfile.mkdtemp(), "m.json"))
        m.put("parent", "src-1", "dst-1")
        translated, _ = translate_refs({"_w_关联项": {"id": "src-1"}},
                                       self.SRC, self.TABLE, m)
        self.assertEqual(translated, {"关联项": "dst-1"})

    def test_empty_ref_skipped_silently(self):
        m = IdMap(os.path.join(tempfile.mkdtemp(), "m.json"))
        translated, unresolved = translate_refs({"_w_关联项": None},
                                                self.SRC, self.TABLE, m)
        self.assertEqual((translated, unresolved), ({}, []))


class TestBusinessKey(unittest.TestCase):
    BY = {"编码": W("编码", "text")}

    def test_reads_key(self):
        self.assertEqual(business_key({"_w_编码": "K-1"}, self.BY, "编码"), "K-1")

    def test_empty_key_is_none(self):
        """业务键为空就无法判断目标端是否已存在，必须报出来而不是当新增。"""
        self.assertIsNone(business_key({"_w_编码": ""}, self.BY, "编码"))
        self.assertIsNone(business_key({}, self.BY, "编码"))

    def test_missing_key_field_rejected(self):
        with self.assertRaises(SyncError):
            business_key({}, self.BY, "不存在的字段")

    def test_numeric_key_normalized_to_string(self):
        by = {"编码": W("编码", "number")}
        self.assertEqual(business_key({"_w_编码": 1001}, by, "编码"), "1001")


class TestRowValues(unittest.TestCase):
    def test_skips_empty(self):
        by = {"a": W("a", "text"), "b": W("b", "text")}
        got = row_values({"_w_a": "x", "_w_b": ""}, by, ["a", "b"])
        self.assertEqual(got, {"a": "x"})

    def test_user_field_carries_username_not_display_name(self):
        """读出来是姓名、写入只认 username。搬显示名会被静默丢弃，
        而且姓名能通过格式校验，连报错都没有——这是同步最容易踩的坑。"""
        by = {"人": W("人", "user")}
        got = row_values({"_w_人": {"name": "张三", "username": "sys_1"}}, by, ["人"])
        self.assertEqual(got, {"人": "sys_1"})

    def test_usergroup_carries_usernames(self):
        by = {"组": W("组", "usergroup")}
        got = row_values({"_w_组": [{"name": "张三", "username": "sys_1"},
                                    {"name": "李四", "username": "sys_2"}]}, by, ["组"])
        self.assertEqual(got, {"组": ["sys_1", "sys_2"]})

    def test_plain_types_use_display_value(self):
        self.assertEqual(sync_value({"name": "研发部"}, "combo"), "研发部")

    def test_dept_and_linkobject_move_ids_not_display_names(self):
        """2026-08-31 实测：dept 只认裸 dept_no、linkobject 只认 {"link_id": …}；
        搬显示名过去会被静默丢弃。和 user 同一种读写不对称。"""
        self.assertEqual(sync_value({"name": "研发部", "dept_no": 7}, "dept"), 7)
        self.assertEqual(
            sync_value({"link_form": "f", "link_id": "a" * 24, "name": "某客户"},
                       "linkobject"),
            {"link_id": "a" * 24})

    def test_multiselect_survives_the_round_trip_to_the_encoder(self):
        """读 → sync_value → encode_value 走一遍，选项不能被拼成一个。

        这条打通两端：sync_value 拼成「线上、线下」而编码端只拆半角逗号，
        两个"各自看着合理"的实现凑在一起就把两个选项写成了一个不存在的选项名。
        """
        from jdy_client import encode_value
        widget = W("渠道", "checkboxgroup")
        raw = ["线上", "线下"]
        self.assertEqual(encode_value(widget, sync_value(raw, "checkboxgroup")), raw)
        # Excel 里人手打的分隔符，中英文都要认
        for text in ("线上,线下", "线上，线下", "线上、线下", "线上;线下"):
            self.assertEqual(encode_value(widget, text), raw, text)

    def test_multiselect_stays_a_list(self):
        """多选读出来是列表，写入端也收列表——中间不要拐去 display_value。

        原来 sync_value 兜底走 display_value，拼成「线上、线下」，
        而编码端只按半角逗号拆，于是整串被当成**一个**选项名写进去。
        """
        self.assertEqual(sync_value(["线上", "线下"], "checkboxgroup"), ["线上", "线下"])
        self.assertEqual(sync_value(["A"], "combocheck"), ["A"])

    def test_phone_and_address_carry_objects_not_display_strings(self):
        """display_value 会把地址拼成一个串，而写入只认对象——搬串会被静默丢弃。"""
        addr = {"province": "江苏省", "city": "无锡市", "district": "锡山区", "detail": ""}
        self.assertEqual(sync_value(addr, "address"), addr)
        phone = {"verified": False, "phone": "13800138000"}
        self.assertEqual(sync_value(phone, "phone"), phone)


class TestPlanFingerprint(unittest.TestCase):
    """大批量写入的确认码。

    它**拦不住 Agent**——Agent 读得到输出自然也能把码传回来。
    它保证的是：被确认的计划和被执行的计划是同一个。
    """

    CFG = {"target": {"app": "APP"}}

    def _plan(self, creates, updates=0, alias="t1"):
        return [{"alias": alias,
                 "creates": [{"key": "K%d" % i} for i in range(creates)],
                 "updates": [{"key": "U%d" % i} for i in range(updates)]}]

    def test_stable_for_same_plan(self):
        a = plan_fingerprint(self.CFG, self._plan(3))
        b = plan_fingerprint(self.CFG, self._plan(3))
        self.assertEqual(a, b)

    def test_changes_when_count_changes(self):
        """源数据多了几条，旧码必须失效——否则确认的和执行的就不是一回事了。"""
        self.assertNotEqual(plan_fingerprint(self.CFG, self._plan(3)),
                            plan_fingerprint(self.CFG, self._plan(4)))

    def test_changes_when_keys_change(self):
        """条数一样但内容换了，也必须变。"""
        p1 = [{"alias": "t", "creates": [{"key": "A"}], "updates": []}]
        p2 = [{"alias": "t", "creates": [{"key": "B"}], "updates": []}]
        self.assertNotEqual(plan_fingerprint(self.CFG, p1),
                            plan_fingerprint(self.CFG, p2))

    def test_changes_when_target_app_changes(self):
        other = {"target": {"app": "OTHER"}}
        self.assertNotEqual(plan_fingerprint(self.CFG, self._plan(3)),
                            plan_fingerprint(other, self._plan(3)))

    def test_insensitive_to_table_order(self):
        """表的处理顺序由拓扑排序决定，不该影响指纹。"""
        a = [{"alias": "x", "creates": [{"key": "1"}], "updates": []},
             {"alias": "y", "creates": [{"key": "2"}], "updates": []}]
        self.assertEqual(plan_fingerprint(self.CFG, a),
                         plan_fingerprint(self.CFG, list(reversed(a))))

    def test_short_and_readable(self):
        code = plan_fingerprint(self.CFG, self._plan(3))
        self.assertEqual(len(code), 8)
        self.assertTrue(code.islower())        # 与内核 plan_code 同一种写法

    def test_same_shape_as_kernel_plan_code(self):
        """确认码全项目一种大小写。

        原来同步这边自己 sha256 再 .upper()，清洗/导入走内核的小写——
        同一个项目里两种码，调用方抄错一种就白跑一次。
        """
        from jdy_client import plan_code
        self.assertRegex(plan_fingerprint(self.CFG, self._plan(3)), r"^[0-9a-f]{8}$")
        self.assertRegex(plan_code({"x": 1}), r"^[0-9a-f]{8}$")




class TestIdMapWinsOverBusinessKey(unittest.TestCase):
    """目标端的业务键被改过之后，同步仍要认出这是同一条。

    实测踩到的：业务键是「手机号」，我用 Excel 把目标端某人的手机改了，
    下次同步按手机号匹配不上，于是**新建了一条重复记录**——接口不会拦，
    回读核对也发现不了（新记录本身是"写成功"的）。
    ID 映射表本来就是为这件事存在的，却唯独没在匹配这一步用上。
    """

    class FakeClient(object):
        def __init__(self, src, dst, fields):
            self.src, self.dst, self.fields = src, dst, fields

        def field_map(self, app, entry):
            by_label = {l: {"name": "_w_%s" % l, "label": l, "type": "text"}
                        for l in self.fields}
            return by_label, {w["name"]: w for w in by_label.values()}

        def fetch_all(self, app, entry, **kw):
            return self.dst if entry == "DST" else self.src

    def _plan(self, id_map, dst_phone):
        src = [{"_id": "S1", "_w_姓名": "薛宝", "_w_手机": "18861827777"}]  # 脱敏例外：造的号
        dst = [{"_id": "T1", "_w_姓名": "薛宝", "_w_手机": dst_phone}]
        client = self.FakeClient(src, dst, ["姓名", "手机"])
        table = {"alias": "t", "source_entry": "SRC", "target_entry": "DST",
                 "key": "手机"}
        cfg = {"source": {"app": "A"}, "target": {"app": "B"}}
        return plan_table(client, cfg, table, id_map)

    def test_key_unchanged_matches_by_key(self):
        im = IdMap("/nonexistent/idmap.json")
        plan = self._plan(im, "18861827777")
        self.assertEqual(len(plan["creates"]), 0)

    def test_changed_key_without_mapping_creates_duplicate(self):
        # 没有映射时只能按键匹配，认不出——这是修复前的行为，记录在案
        im = IdMap("/nonexistent/idmap.json")
        plan = self._plan(im, "18861820000")  # 脱敏例外：造的号
        self.assertEqual(len(plan["creates"]), 1)

    def test_changed_key_with_mapping_updates_instead(self):
        im = IdMap("/nonexistent/idmap.json")
        im.put("t", "S1", "T1")                 # 上次同步登记过
        plan = self._plan(im, "18861820000")    # 目标端手机被改过
        self.assertEqual(len(plan["creates"]), 0, "有映射还新建，就是重复记录")
        self.assertEqual(len(plan["updates"]) + len(plan["skips"]), 1)

    def test_stale_mapping_falls_back_and_reports(self):
        im = IdMap("/nonexistent/idmap.json")
        im.put("t", "S1", "已被删掉的ID")
        plan = self._plan(im, "18861827777")    # 键没变，能退回键匹配
        self.assertEqual(len(plan["creates"]), 0)
        kinds = [p["kind"] for p in plan["problems"]]
        self.assertIn("stale_mapping", kinds)   # 要说出来，不能默默换路


class TestDuplicateBusinessKeys(unittest.TestCase):
    """业务键重复必须说出来。

    整个同步模型的前提是「业务键唯一标识一条记录」。前提破了还照跑，
    结果就是猜。原来 dst_index 用 setdefault「取第一条」——另一条从此
    永远不会被更新，两边越漂越远，而且一句提示都没有。
    是 WorkBuddy 里的 Agent 在核对计划时点出来的。
    """

    class FakeClient(object):
        def __init__(self, src, dst):
            self.src, self.dst = src, dst

        def field_map(self, app, entry):
            by_label = {l: {"name": "_w_%s" % l, "label": l, "type": "text"}
                        for l in ("姓名", "手机")}
            return by_label, {w["name"]: w for w in by_label.values()}

        def fetch_all(self, app, entry, **kw):
            return self.dst if entry == "DST" else self.src

    def _plan(self, src, dst):
        table = {"alias": "t", "source_entry": "SRC", "target_entry": "DST",
                 "key": "手机"}
        return plan_table(self.FakeClient(src, dst), {"source": {"app": "A"},
                                                      "target": {"app": "B"}},
                          table, IdMap("/nonexistent/idmap.json"))

    def _kinds(self, plan):
        return [p["kind"] for p in plan["problems"]]

    def test_duplicate_in_target_is_reported(self):
        src = [{"_id": "S1", "_w_姓名": "甲", "_w_手机": "138"}]
        dst = [{"_id": "T1", "_w_姓名": "甲", "_w_手机": "138"},
               {"_id": "T2", "_w_姓名": "甲", "_w_手机": "138"}]
        plan = self._plan(src, dst)
        self.assertIn("duplicate_key_target", self._kinds(plan))
        detail = [p["detail"] for p in plan["problems"]][0]
        self.assertIn("T1", detail)
        self.assertIn("T2", detail)          # 要指名道姓，不能只说"有重复"

    def test_duplicate_in_source_is_reported(self):
        src = [{"_id": "S1", "_w_姓名": "甲", "_w_手机": "138"},
               {"_id": "S2", "_w_姓名": "乙", "_w_手机": "138"}]
        dst = []
        self.assertIn("duplicate_key_source", self._kinds(self._plan(src, dst)))

    def test_unique_keys_report_nothing(self):
        src = [{"_id": "S1", "_w_姓名": "甲", "_w_手机": "138"}]
        dst = [{"_id": "T1", "_w_姓名": "甲", "_w_手机": "139"}]
        kinds = self._kinds(self._plan(src, dst))
        self.assertNotIn("duplicate_key_target", kinds)
        self.assertNotIn("duplicate_key_source", kinds)

    def test_empty_keys_are_not_duplicates(self):
        # 两条都没填手机 ≠ 键重复，那是 missing_key
        src = [{"_id": "S1"}, {"_id": "S2"}]
        kinds = self._kinds(self._plan(src, []))
        self.assertNotIn("duplicate_key_source", kinds)
        self.assertEqual(kinds.count("missing_key"), 2)


class TestLimitedPlanSaysSo(unittest.TestCase):
    """带 limit 的计划，合计数字不能被当成全量结论。

    每张表旁边虽然标了 limit，但人是照着**合计**下判断的：
    「合计：新增 0」看着像"两边已经一致"，实际只比了抽样的前 5 行。
    """

    def _render(self, limited):
        plan = {"alias": "t", "key": "手机", "limited": limited,
                "source_rows": 5, "target_rows": 206,
                "mapped_fields": {}, "excluded": [], "ref_fields": [],
                "creates": [], "updates": [], "skips": [], "problems": [],
                "matched": 0}
        cfg = {"name": "T", "source": {"app": "A"}, "target": {"app": "B"}}
        return plan_mod.render(cfg, [plan], IdMap("/nonexistent/idmap.json"))

    def test_limited_plan_warns(self):
        out = self._render(5)
        self.assertIn("不是全量差异", out)
        self.assertIn("limit", out)

    def test_full_plan_stays_quiet(self):
        self.assertNotIn("不是全量差异", self._render(None))


class TestPlanSnapshotReuse(unittest.TestCase):
    """plan.py --json-out 的产物，apply.py 要能直接照着执行。

    原来 `--json-out` 的帮助文本写着"供 apply.py 使用"，而 apply.py 根本没有
    对应的开关——于是一轮同步要全量重拉 3~4 遍，而且**执行前那次重新规划**
    可能让刚拿到的确认码失效，或在用户点头之后悄悄多出几条。

    复用快照的代价是它可能过时，所以这三条必须钉死：
    配置对得上、alias 存在、年龄如实报出来。
    """

    CFG = {"tables": [{"alias": "客户"}, {"alias": "订单"}]}

    def _snapshot(self, tmpdir, **over):
        blob = {"config": os.path.join(tmpdir, "sync.yaml"),
                "plans": [{"alias": "客户", "creates": [], "updates": []}],
                "generated_at": datetime.datetime.now(
                    datetime.timezone.utc).isoformat()}
        blob.update(over)
        path = os.path.join(tmpdir, "plan.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
        return path

    def test_round_trips_and_reports_a_fresh_age(self):
        tmp = tempfile.mkdtemp()
        plans, age = sync_apply.load_plan_snapshot(
            self._snapshot(tmp), os.path.join(tmp, "sync.yaml"), self.CFG)
        self.assertEqual([p["alias"] for p in plans], ["客户"])
        self.assertLessEqual(age, 1)

    def test_stale_snapshot_reports_its_age(self):
        tmp = tempfile.mkdtemp()
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(hours=5)).isoformat()
        _plans, age = sync_apply.load_plan_snapshot(
            self._snapshot(tmp, generated_at=old), os.path.join(tmp, "sync.yaml"), self.CFG)
        self.assertGreaterEqual(age, 299)          # 由调用方按 --max-plan-age 拒绝

    def test_refuses_a_plan_made_for_another_config(self):
        """拿 A 的计划去执行 B 会**写错表**——这不能只是警告。"""
        tmp = tempfile.mkdtemp()
        path = self._snapshot(tmp, config=os.path.join(tmp, "别的.yaml"))
        with self.assertRaises(SyncError) as cm:
            sync_apply.load_plan_snapshot(path, os.path.join(tmp, "sync.yaml"), self.CFG)
        self.assertIn("不是同一份", str(cm.exception))

    def test_refuses_an_alias_the_config_no_longer_has(self):
        tmp = tempfile.mkdtemp()
        path = self._snapshot(tmp, plans=[{"alias": "已删掉的表", "creates": [], "updates": []}])
        with self.assertRaises(SyncError):
            sync_apply.load_plan_snapshot(path, os.path.join(tmp, "sync.yaml"), self.CFG)

    def test_refuses_a_file_that_is_not_a_plan(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "x.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"随便什么": 1}, fh)
        with self.assertRaises(SyncError):
            sync_apply.load_plan_snapshot(path, os.path.join(tmp, "sync.yaml"), self.CFG)

    def test_missing_timestamp_is_reported_as_unknown_not_as_fresh(self):
        """没有时间戳时年龄是"不知道"，不能当成"刚生成"。"""
        tmp = tempfile.mkdtemp()
        path = self._snapshot(tmp, generated_at=None)
        _plans, age = sync_apply.load_plan_snapshot(
            path, os.path.join(tmp, "sync.yaml"), self.CFG)
        self.assertIsNone(age)


class TestSnapshotWithoutTimestampIsRefused(unittest.TestCase):
    """「不知道多旧」必须当成「太旧」，不能当成「刚生成」。

    原来的判断是 `if age is not None and age > 上限`，于是缺 generated_at 的
    快照无条件放行，屏幕上还打一个「? 分钟前」——年龄门槛静默失效。
    老版本 plan.py 存下的快照正好就是这个样子。
    """

    def test_age_is_none_when_the_timestamp_is_missing(self):
        tmp = tempfile.mkdtemp()
        cfgpath = os.path.join(tmp, "sync.yaml")
        path = os.path.join(tmp, "plan.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"config": cfgpath,
                       "plans": [{"alias": "客户", "creates": [], "updates": []}]}, fh)
        _plans, age = sync_apply.load_plan_snapshot(path, cfgpath, {"tables": [{"alias": "客户"}]})
        self.assertIsNone(age)

    def test_apply_refuses_instead_of_letting_it_through(self):
        """门槛的判断必须先处理 None，再比大小。"""
        src = open(os.path.join(ROOT, "skills", "jdy-sync", "scripts", "apply.py"),
                   encoding="utf-8").read()
        self.assertIn("if age is None:", src)
        self.assertNotIn("if age is not None and age > args.max_plan_age", src)


class TestRelinkPlan(unittest.TestCase):
    """把「选择数据」的关系搬到「关联数据」上。

    这件事之所以成立：选择数据**读得出来**（值就是目标记录的 data_id），
    只是**写不回去**——官方所有通道都不可写，唯一写入方式是人在表单里点选。
    而关联数据可以直写 data_id。

    中文名和 API 类型是反直觉的：选择数据 = linkdata（死）、
    关联数据 = lookup（活）。搞反了就是把关系写进一个永远写不进去的字段。
    """

    W = {"选择客户": {"name": "_w_link", "label": "选择客户", "type": "linkdata"},
         "关联客户": {"name": "_w_look", "label": "关联客户", "type": "lookup"},
         "备注": {"name": "_w_t", "label": "备注", "type": "text"}}

    class FakeClient(object):
        def __init__(self, rows, stale=False):
            self.rows = rows
            self.stale = stale        # 模拟"字段缓存里还没有刚加的那个字段"
            self.refreshed = False

        def _w(self):
            w = dict(TestRelinkPlan.W)
            if self.stale and not self.refreshed:
                w.pop("关联客户")      # 缓存是 24 小时前的，那时还没这个字段
            return w

        def field_map(self, app, entry, refresh=False):
            if refresh:
                self.refreshed = True
            w = self._w()
            return (w, {v["name"]: v for v in w.values()})

        def field_map_including(self, app, entry, labels):
            by_label, by_name = self.field_map(app, entry)
            if any(l and l not in by_label for l in labels):
                by_label, by_name = self.field_map(app, entry, refresh=True)
            return by_label, by_name

        def fetch_all(self, *a, **kw):
            return self.rows

    def _plan(self, rows):
        return relink.plan_backfill(self.FakeClient(rows), "A", "E", "选择客户", "关联客户")

    def test_a_field_added_just_now_is_not_hidden_by_the_24h_cache(self):
        """本工具的**整个用法**就是"刚在界面上加了个关联字段，马上回填"。

        字段结构本地缓存 24 小时，缓存里当然没有它——于是工具一口咬定
        「表单里没有这个字段」，人对着界面上明明有的字段干瞪眼。
        缓存是为了省请求，不该省出一个假答案。
        """
        c = self.FakeClient([{"_id": "r1", "_w_link": {"id": "t1"}}], stale=True)
        got = relink.plan_backfill(c, "A", "E", "选择客户", "关联客户")
        self.assertTrue(c.refreshed, "没有刷新字段缓存")
        self.assertEqual(got["todo"], [{"data_id": "r1", "ref": "t1",
                                        "overwrite": False}])

    def test_reads_the_id_out_of_the_linkdata_value(self):
        got = self._plan([{"_id": "r1", "_w_link": {"id": "t1"}}])
        self.assertEqual(got["todo"], [{"data_id": "r1", "ref": "t1", "overwrite": False}])

    def test_rows_without_a_relation_are_left_alone(self):
        got = self._plan([{"_id": "r1"}, {"_id": "r2", "_w_link": None}])
        self.assertEqual(got["todo"], [])
        self.assertEqual(got["empty"], 2)

    def test_already_migrated_rows_are_not_rewritten(self):
        """已经一致的不重写——白白扩大写入面，出问题时也分不清是谁改的。"""
        got = self._plan([{"_id": "r1", "_w_link": {"id": "t1"},
                           "_w_look": {"id": "t1"}}])
        self.assertEqual(got["todo"], [])
        self.assertEqual(got["already"], ["r1"])

    def test_a_different_existing_value_is_flagged_as_overwrite(self):
        """目标字段已有别的值时要标出来——那是覆盖，不是填空。"""
        got = self._plan([{"_id": "r1", "_w_link": {"id": "t1"},
                           "_w_look": {"id": "别的"}}])
        self.assertTrue(got["todo"][0]["overwrite"])

    def test_source_must_be_linkdata(self):
        """搞反了就是把关系写进一个永远写不进去的字段。"""
        client = self.FakeClient([])
        with self.assertRaises(ValueError) as cm:
            relink.plan_backfill(client, "A", "E", "关联客户", "选择客户")
        self.assertIn("不是「选择数据」", str(cm.exception))

    def test_target_must_be_lookup(self):
        client = self.FakeClient([])
        with self.assertRaises(ValueError) as cm:
            relink.plan_backfill(client, "A", "E", "选择客户", "备注")
        self.assertIn("只有关联数据能直写", str(cm.exception))

    def test_unknown_field_is_refused(self):
        with self.assertRaises(ValueError):
            relink.plan_backfill(self.FakeClient([]), "A", "E", "选择客户", "不存在的")


class TestRelinkPrescription(unittest.TestCase):
    """处方要说清楚**哪一步只能人做**，以及"反查不到"到底是哪一种。"""

    def _item(self, **kw):
        base = {"form": "订单", "entry_id": "E", "filled": 5, "total": 10,
                "widget": {"label": "选择客户", "name": "_w"},
                "target_entry": None, "target_name": None,
                "dangling": False, "lookups": []}
        base.update(kw)
        return base

    def test_existing_lookup_means_backfill_right_away(self):
        lk = {"label": "关联客户", "name": "_w2"}
        lines = relink.prescribe(self._item(target_entry="T", target_name="客户",
                                            lookups=[(lk, "T")]))
        self.assertIn("可以直接回填", lines[0])
        self.assertIn("--to 关联客户", "\n".join(lines))

    def test_no_lookup_yet_says_the_gui_step_is_unavoidable(self):
        lines = relink.prescribe(self._item(target_entry="T", target_name="客户"))
        joined = "\n".join(lines)
        self.assertIn("加一个**关联数据**字段", joined)
        self.assertIn("只能人做", joined)

    def test_a_lookup_pointing_elsewhere_is_not_offered(self):
        """指向别的表的关联字段不能拿来当落点——写进去就是指向虚无的引用。"""
        lk = {"label": "关联商机", "name": "_w2"}
        lines = relink.prescribe(self._item(target_entry="T", target_name="客户",
                                            lookups=[(lk, "别的表")]))
        self.assertIn("加一个**关联数据**字段", "\n".join(lines))

    def test_dangling_refs_are_called_dangling_not_just_unknown(self):
        """「反查不到」和「引用本身已经断了」是两件事。混成一句，
        用户会以为是工具不行，然后去手工配一遍——而他要面对的是一批断掉的引用。"""
        lines = relink.prescribe(self._item(dangling=True))
        joined = "\n".join(lines)
        self.assertIn("找不到", joined)
        self.assertIn("迁不迁移都一样", joined)

    def test_an_empty_column_says_so_instead_of_blaming_the_lookup(self):
        lines = relink.prescribe(self._item(filled=0))
        self.assertIn("一行值都没有", "\n".join(lines))


class TestFinderIsEconomical(unittest.TestCase):
    """反查必须带记忆。天真实现是 O(字段 × 表 × ID) 次请求——
    CRM 那种二十来张表、十几个关联字段的应用直接跑到两分钟以上（实测超时）。"""

    class FakeClient(object):
        def __init__(self, where):
            self.where = where            # data_id → entry_id
            self.calls = 0

        def list_forms(self, app_id):
            return [{"name": "表%d" % i, "entry_id": "e%d" % i} for i in range(10)]

        def post(self, path, body):
            self.calls += 1
            if self.where.get(body["data_id"]) == body["entry_id"]:
                return {"data": {"_id": body["data_id"]}}
            raise JdyError("4001", "Data does not exist.")

    def test_a_located_id_is_not_probed_again(self):
        c = self.FakeClient({"t1": "e7"})
        f = relink.Finder(c, "A")
        self.assertEqual(f.locate("t1"), "e7")
        first = c.calls
        self.assertEqual(f.locate("t1"), "e7")
        self.assertEqual(c.calls, first)          # 第二次一个请求都不发

    def test_a_missing_id_is_also_remembered(self):
        """探不到也要记住——否则每个字段都会把十张表再探一遍。"""
        c = self.FakeClient({})
        f = relink.Finder(c, "A")
        self.assertIsNone(f.locate("nope"))
        first = c.calls
        self.assertIsNone(f.locate("nope"))
        self.assertEqual(c.calls, first)

    def test_probe_budget_stops_runaway_scans(self):
        c = self.FakeClient({})
        f = relink.Finder(c, "A")
        for i in range(50):
            f.locate("id%d" % i, budget=20)
        self.assertLessEqual(c.calls, 20)

    def test_target_needs_every_sample_to_agree(self):
        """几个样本指向不同的表 = 反查不可信，宁可说不知道。"""
        c = self.FakeClient({"a": "e1", "b": "e2"})
        f = relink.Finder(c, "A")
        rows = [{"_w": {"id": "a"}}, {"_w": {"id": "b"}}]
        target, ids = f.target_of(rows, {"name": "_w"})
        self.assertIsNone(target)
        self.assertEqual(ids, ["a", "b"])


class TestExternalSources(unittest.TestCase):
    """CSV / JSONL / SQLite 当同步的源端。

    价值在于**把同步那套保证带给外部数据**：按业务键比对、只写有变化的、
    ID 映射持久化、写后回读核对。Excel 导入给不了这些（它只认新增/按 _id 更新）。
    """

    def _write(self, name, text):
        path = os.path.join(tempfile.mkdtemp(), name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_csv_columns_become_text_widgets(self):
        """每列都当 text，**类型转换留给写入端按目标字段的类型做**——
        外部文件里什么都是字符串，猜它是数字还是日期只会猜错。"""
        path = self._write("a.csv", "姓名,金额\n甲,1000\n")
        by_label, rows = sources.read(path)
        self.assertEqual(sorted(by_label), ["姓名", "金额"])
        self.assertEqual(by_label["金额"]["type"], "text")
        self.assertEqual(rows, [{"姓名": "甲", "金额": "1000"}])

    def test_csv_with_a_bom_still_finds_the_first_column(self):
        """带 BOM 是 Excel 导出的常态；不处理的话第一列列名会变成 "\ufeff姓名"，
        然后"表里没有这个字段"。"""
        path = os.path.join(tempfile.mkdtemp(), "b.csv")
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write("姓名,金额\n甲,1\n")
        by_label, _rows = sources.read(path)
        self.assertIn("姓名", by_label)

    def test_tsv(self):
        path = self._write("a.tsv", "姓名\t金额\n甲\t1\n")
        _by, rows = sources.read(path)
        self.assertEqual(rows, [{"姓名": "甲", "金额": "1"}])

    def test_jsonl_keeps_first_seen_column_order(self):
        path = self._write("a.jsonl", '{"乙": 1, "甲": 2}\n{"丙": 3}\n')
        by_label, rows = sources.read(path)
        self.assertEqual(list(by_label), ["乙", "甲", "丙"])
        self.assertEqual(len(rows), 2)

    def test_jsonl_bad_line_names_the_line(self):
        path = self._write("a.jsonl", '{"甲": 1}\n这不是 json\n')
        with self.assertRaises(sources.SourceError) as cm:
            sources.read(path)
        self.assertIn("第 2 行", str(cm.exception))

    def test_jsonl_non_object_line_is_refused(self):
        path = self._write("a.jsonl", '[1, 2]\n')
        with self.assertRaises(sources.SourceError):
            sources.read(path)

    def test_sqlite_reads_a_named_table(self):
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "a.sqlite")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE staff (姓名 TEXT, 金额 TEXT)")
        conn.execute("INSERT INTO staff VALUES ('甲', '1')")
        conn.commit()
        conn.close()
        by_label, rows = sources.read(path, "staff")
        self.assertEqual(sorted(by_label), ["姓名", "金额"])
        self.assertEqual(rows, [{"姓名": "甲", "金额": "1"}])

    def test_sqlite_without_a_table_name_says_what_to_do(self):
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "a.sqlite")
        sqlite3.connect(path).close()
        with self.assertRaises(sources.SourceError) as cm:
            sources.read(path)
        self.assertIn("source_entry", str(cm.exception))

    def test_sqlite_unknown_table_lists_the_real_ones(self):
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "a.sqlite")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE staff (x TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaises(sources.SourceError) as cm:
            sources.read(path, "没这张表")
        self.assertIn("staff", str(cm.exception))

    def test_unknown_suffix_is_refused(self):
        with self.assertRaises(sources.SourceError):
            sources.kind_of("/tmp/a.xlsx")

    def test_missing_file_says_so(self):
        with self.assertRaises(sources.SourceError) as cm:
            sources.read("/tmp/根本没有这个文件.csv")
        self.assertIn("找不到文件", str(cm.exception))

    def test_duplicate_columns_are_refused(self):
        """按显示名映射时重名列会互相盖掉，静默取最后一个不可接受。"""
        path = self._write("a.csv", "姓名,姓名\n甲,乙\n")
        with self.assertRaises(sources.SourceError) as cm:
            sources.read(path)
        self.assertIn("列名重复", str(cm.exception))


class TestExternalSourceIds(unittest.TestCase):
    """外部行没有简道云的 _id，只能用业务键合成——后果必须说清楚。"""

    BY = {"手机": {"name": "手机", "label": "手机", "type": "text"}}

    def test_id_is_derived_from_the_business_key(self):
        got = sources.stamp_ids([{"手机": "138"}], self.BY, "手机")
        self.assertEqual(got[0]["_id"], "file:138")

    def test_an_empty_key_is_refused(self):
        """外部源靠业务键认人，空的就没法匹配——静默跳过会漏数据。"""
        with self.assertRaises(sources.SourceError) as cm:
            sources.stamp_ids([{"手机": "  "}], self.BY, "手机")
        self.assertIn("不能为空", str(cm.exception))

    def test_duplicate_keys_name_both_lines(self):
        with self.assertRaises(sources.SourceError) as cm:
            sources.stamp_ids([{"手机": "138"}, {"手机": "139"}, {"手机": "138"}],
                              self.BY, "手机")
        self.assertIn("第 1 行和第 3 行", str(cm.exception))

    def test_a_key_not_in_the_file_lists_the_columns(self):
        with self.assertRaises(sources.SourceError) as cm:
            sources.stamp_ids([{"手机": "138"}], self.BY, "工号")
        self.assertIn("手机", str(cm.exception))


class TestExternalSourceConfig(unittest.TestCase):
    def _cfg(self, text):
        path = os.path.join(tempfile.mkdtemp(), "s.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    BASE = ("source:\n  file: a.csv\ntarget:\n  app: A\n"
            "tables:\n  - alias: 甲\n    source_entry: a.csv\n"
            "    target_entry: E\n    key: 手机\n")

    def test_file_source_is_accepted(self):
        cfg = load_config(self._cfg(self.BASE), parse_yaml)
        self.assertTrue(cfg["source"]["file"].endswith("a.csv"))

    def test_relative_path_resolves_against_the_config(self):
        """否则同一份配置换个目录跑就找不到源文件。"""
        path = self._cfg(self.BASE)
        cfg = load_config(path, parse_yaml)
        self.assertEqual(os.path.dirname(cfg["source"]["file"]),
                         os.path.dirname(os.path.abspath(path)))

    def test_app_and_file_together_are_refused(self):
        with self.assertRaises(SyncError):
            load_config(self._cfg(self.BASE.replace("  file: a.csv",
                                                    "  file: a.csv\n  app: X")), parse_yaml)

    def test_neither_app_nor_file_is_refused(self):
        with self.assertRaises(SyncError):
            load_config(self._cfg(self.BASE.replace("  file: a.csv", "  name: x")),
                        parse_yaml)

    def test_target_must_still_be_an_app(self):
        """本工具不往外部文件写——那不需要同步的这套保证。"""
        with self.assertRaises(SyncError) as cm:
            load_config(self._cfg(self.BASE.replace("target:\n  app: A",
                                                    "target:\n  file: b.csv")), parse_yaml)
        self.assertIn("target 必须是简道云应用", str(cm.exception))

    def test_refs_with_a_file_source_are_refused(self):
        """引用翻译要拿源端的 data_id 去查 ID 映射，而外部文件里没有 data_id。"""
        text = self.BASE + "    refs:\n      某字段: 甲\n"
        with self.assertRaises(SyncError) as cm:
            load_config(self._cfg(text), parse_yaml)
        self.assertIn("外部文件里没有简道云的", str(cm.exception))

    def test_two_tables_cannot_share_one_flat_file(self):
        """一个 CSV 只有一张表。两张表配同一个 file，原来是**各读一遍全部内容**、
        一声不吭——source_entry 在 SQLite 上是选表的，在 CSV 上被整个忽略，
        同一个字段两种行为，是最难发现的那种。"""
        text = self.BASE + ("  - alias: 乙\n    source_entry: b.csv\n"
                            "    target_entry: E2\n    key: 手机\n")
        with self.assertRaises(SyncError) as cm:
            load_config(self._cfg(text), parse_yaml)
        self.assertIn("平面文件", str(cm.exception))

    def test_sqlite_may_have_many_tables(self):
        """SQLite 本来就一个文件多张表，这条限制不该落到它头上。"""
        text = (self.BASE.replace("file: a.csv", "file: a.db")
                         .replace("source_entry: a.csv", "source_entry: 客户")
                + "  - alias: 乙\n    source_entry: 订单\n"
                  "    target_entry: E2\n    key: 手机\n")
        load_config(self._cfg(text), parse_yaml)          # 不抛就对了

    def test_a_directory_source_picks_the_file_by_source_entry(self):
        """file 指向目录时 source_entry 就是文件名——多表配置这么写。"""
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "客户.csv"), "w", encoding="utf-8") as fh:
            fh.write("编号,名称\n1,甲\n")
        by_label, rows = sources.read(d, "客户")
        self.assertEqual(rows[0]["名称"], "甲")
        self.assertEqual(sorted(by_label), ["名称", "编号"])

    def test_a_directory_without_source_entry_is_refused(self):
        with self.assertRaises(sources.SourceError):
            sources.read(tempfile.mkdtemp(), None)

    def test_same_name_different_suffix_is_refused_not_guessed(self):
        """客户.csv 和 客户.jsonl 并存时，按排序取第一个就是**替用户猜**——
        猜错了不报错，只是同步进一份不是他要的数据，事后看不出取的是哪个。"""
        d = tempfile.mkdtemp()
        for name, body in (("客户.csv", "编号,名称\n1,甲\n"),
                           ("客户.jsonl", '{"编号":"1","名称":"乙"}\n')):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        with self.assertRaises(sources.SourceError) as cm:
            sources.read(d, "客户")
        self.assertIn("客户.csv", str(cm.exception))
        self.assertIn("客户.jsonl", str(cm.exception))
        _by, rows = sources.read(d, "客户.jsonl")      # 写全后缀就不含糊了
        self.assertEqual(rows[0]["名称"], "乙")

    def test_a_missing_name_in_the_directory_says_what_is_there(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "订单.csv"), "w", encoding="utf-8") as fh:
            fh.write("a\n1\n")
        with self.assertRaises(sources.SourceError) as cm:
            sources.read(d, "客户")
        self.assertIn("订单.csv", str(cm.exception))


class TestSubformValues(unittest.TestCase):
    """子表单：读回来的扁平行 → 能写回去的形状。

    实测（2026-09-01）：写要 `[{内层: {"value": v}}]` 双层包裹，
    读回来是 `[{"_id": …, 内层name: v}]` 扁平——把读回来的原样写回去，
    接口回报 success 而**整列被清空**。中间这一层翻译就是本类守的东西。
    """

    SRC = SUB("明细", [("品名", "text"), ("经办人", "user"), ("数量", "number")])
    DST = SUB("明细", [("品名", "text"), ("经办人", "user"), ("数量", "number")])

    def _shape(self):
        ok, why, shape = sync_shape(self.SRC, self.DST)
        self.assertTrue(ok, why)
        return shape

    def test_flat_rows_become_label_keyed_rows(self):
        raw = [{"_id": "R1", "_w_明细_品名": "螺丝", "_w_明细_数量": 3},
               {"_id": "R2", "_w_明细_品名": "螺母", "_w_明细_数量": 5}]
        self.assertEqual(sync_value(raw, "subform", self._shape()),
                         [{"品名": "螺丝", "数量": 3}, {"品名": "螺母", "数量": 5}])

    def test_inner_id_is_not_carried_over(self):
        """子行的 `_id` 是**源端**的编号。带过去毫无意义，更要命的是它每次重写都变，
        进了比对就永远判"有变化"。"""
        raw = [{"_id": "R1", "_w_明细_品名": "螺丝"}]
        self.assertNotIn("_id", sync_value(raw, "subform", self._shape())[0])

    def test_inner_user_field_carries_username_not_display_name(self):
        """内层值要**递归**过 sync_value：内层的成员字段同样是"读展开对象、写 username"。
        不递归就把"张三"搬过去，然后被静默丢弃——外层早就做对了，内层是另一半。"""
        raw = [{"_w_明细_经办人": {"name": "张三", "username": "sys_1"}}]
        self.assertEqual(sync_value(raw, "subform", self._shape()),
                         [{"经办人": "sys_1"}])

    def test_all_empty_rows_are_dropped(self):
        """实测：提交 3 行（中间一行全空）回读只有 2 行——简道云自己吞掉全空子行。
        留着它，回读永远比提交少一行、永远判"没搬干净"，重跑一次重写一次。"""
        raw = [{"_w_明细_品名": "甲"}, {"_id": "R2"}, {"_w_明细_品名": "丙"}]
        self.assertEqual(sync_value(raw, "subform", self._shape()),
                         [{"品名": "甲"}, {"品名": "丙"}])

    def test_without_a_shape_nothing_moves(self):
        """没有内层映射就别硬搬——搬过去也是一列空的。"""
        self.assertIsNone(sync_value([{"_w_明细_品名": "甲"}], "subform", None))

    def test_output_feeds_encode_row_and_comes_out_double_wrapped(self):
        """打通两端：sync_value 的产物喂给 encode_row，必须编成**双层包裹**。

        两个"各自看着合理"的实现凑在一起才是坑：翻译对了、编码退成单层，
        接口照样 success，而子表单是空的。
        """
        from jdy_client import encode_row
        rows = sync_value([{"_w_明细_品名": "螺丝", "_w_明细_数量": 3}],
                          "subform", self._shape())
        data, skipped = encode_row({"明细": self.DST}, {"明细": rows})
        self.assertEqual(skipped, [])
        self.assertEqual(data["_w_明细"],
                         {"value": [{"_w_明细_品名": {"value": "螺丝"},
                                     "_w_明细_数量": {"value": 3}}]})


class TestSubformDiffCannotUseDisplayValue(unittest.TestCase):
    """**本期最大的陷阱**：`display_value` 对子表单只给"N 行子表单"。

    拿它比对，"行数相同、内容全变了"会被判成无变化——整列悄悄不同步，
    而计划、执行、回读三处都显示一切正常。
    """

    A = [{"品名": "甲"}, {"品名": "乙"}]
    B = [{"品名": "丙"}, {"品名": "丁"}]

    def test_display_value_really_is_blind_here(self):
        """先证明陷阱是真的：两份完全不同的子表单，display_value 给的是同一句话。"""
        from jdy_client import display_value
        self.assertEqual(display_value(self.A, "subform"),
                         display_value(self.B, "subform"))

    def test_canonical_tells_them_apart(self):
        self.assertNotEqual(canonical(self.A, "subform"), canonical(self.B, "subform"))

    def test_key_order_inside_a_row_is_not_a_change(self):
        """两边的键顺序取决于各自的字段顺序，不该因此判成有变化。"""
        self.assertEqual(canonical([{"甲": 1, "乙": 2}], "subform"),
                         canonical([{"乙": 2, "甲": 1}], "subform"))

    def test_seven_and_seven_point_zero_are_not_a_change(self):
        self.assertEqual(canonical([{"数量": 7}], "subform"),
                         canonical([{"数量": 7.0}], "subform"))

    def test_row_order_is_a_change(self):
        """子表单是有序的，整表替换会照源端顺序重写——顺序变了就是变了。"""
        self.assertNotEqual(canonical([{"品名": "甲"}, {"品名": "乙"}], "subform"),
                            canonical([{"品名": "乙"}, {"品名": "甲"}], "subform"))


class TestPlanSeesSubformContentChanges(unittest.TestCase):
    """把陷阱钉在**计划这一层**：纯函数比对对了，不代表 plan_table 用了它。"""

    class FakeClient(object):
        def __init__(self, src, dst, widgets):
            self.src, self.dst, self.widgets = src, dst, widgets

        def field_map(self, app, entry):
            return self.widgets, {w["name"]: w for w in self.widgets.values()}

        def fetch_all(self, app, entry, **kw):
            return self.dst if entry == "DST" else self.src

    def _plan(self, src_rows, dst_rows):
        widgets = {"编号": W("编号", "text"),
                   "明细": SUB("明细", [("品名", "text"), ("数量", "number")])}
        client = self.FakeClient(src_rows, dst_rows, widgets)
        table = {"alias": "t", "source_entry": "SRC", "target_entry": "DST", "key": "编号"}
        cfg = {"source": {"app": "A"}, "target": {"app": "B"}}
        return plan_table(client, cfg, table, IdMap("/nonexistent/idmap.json"))

    def _row(self, rid, detail):
        return {"_id": rid, "_w_编号": "K1",
                "_w_明细": [dict({"_id": "%s-%d" % (rid, i)},
                                 **{"_w_明细_品名": d[0], "_w_明细_数量": d[1]})
                            for i, d in enumerate(detail)]}

    def test_same_row_count_different_content_is_an_update(self):
        """**这条就是钉子。** 行数相同、内容不同，必须判"有变化"。
        退回用 display_value 比，这里会变成"无变化"，整列永远同步不过去。"""
        plan = self._plan([self._row("S1", [("甲", 1), ("乙", 2)])],
                          [self._row("T1", [("丙", 3), ("丁", 4)])])
        self.assertEqual(len(plan["updates"]), 1, "内容全变了却判成无变化")
        self.assertIn("明细", plan["updates"][0]["diff"])

    def test_identical_subforms_are_no_change(self):
        """另一半：一样的就是一样的，别每次都重写（重写一次就是一次静默丢的机会）。"""
        plan = self._plan([self._row("S1", [("甲", 1), ("乙", 2)])],
                          [self._row("T1", [("甲", 1), ("乙", 2)])])
        self.assertEqual(plan["updates"], [])
        self.assertEqual(plan["skips"], ["K1"])

    def test_row_count_change_is_an_update(self):
        plan = self._plan([self._row("S1", [("甲", 1)])],
                          [self._row("T1", [("甲", 1), ("乙", 2)])])
        self.assertEqual(len(plan["updates"]), 1)

    def test_dropped_empty_rows_are_reported_not_silent(self):
        """全空子行搬不过去（简道云会吞），但**不能不吭声**。"""
        src = self._row("S1", [("甲", 1)])
        src["_w_明细"].append({"_id": "S1-9"})          # 一整行都是空的
        plan = self._plan([src], [])
        kinds = [p["kind"] for p in plan["problems"]]
        self.assertIn("subform_empty_rows", kinds)


class TestAttachmentDiffIgnoresTheExpiringUrl(unittest.TestCase):
    """附件按 (name, size) 比（D4；W1-d 实测重传后 size 逐字节稳定）。

    url 每次读回来都带**新的过期戳**，拿它比对等于每次都判"有变化"——
    每次重跑把整表附件重新下载再上传一遍，白耗上传凭证、把日志刷满，
    而每次重写都是一次静默丢字段的机会。excel-bridge 那边栽过同一条（变异 53）。
    """

    def _att(self, name, size, url):
        return [{"name": name, "size": size, "url": url}]

    def test_same_files_different_urls_are_not_a_change(self):
        a = self._att("合同.pdf", 1024, "https://x/1?e=111&token=aaa")
        b = self._att("合同.pdf", 1024, "https://x/9?e=999&token=zzz")
        self.assertEqual(canonical(a, "upload"), canonical(b, "upload"))

    def test_a_different_name_is_a_change(self):
        self.assertNotEqual(canonical(self._att("甲.pdf", 1, "u"), "upload"),
                            canonical(self._att("乙.pdf", 1, "u"), "upload"))

    def test_a_different_size_is_a_change(self):
        self.assertNotEqual(canonical(self._att("甲.pdf", 1, "u"), "upload"),
                            canonical(self._att("甲.pdf", 2, "u"), "upload"))

    def test_the_url_is_kept_for_the_move_even_though_it_is_not_compared(self):
        """两件事不能混：比对**不看** url，但搬运**要靠**它下载原文件。
        为了"比得干净"把 url 丢掉，附件就永远搬不过去了。"""
        got = sync_value(self._att("合同.pdf", 1024, "https://x/1?e=1"), "upload")
        self.assertEqual(got[0]["url"], "https://x/1?e=1")


class TestPlanDoesNotReuploadUnchangedAttachments(unittest.TestCase):
    """幂等：第二遍必须"无变化、0 次上传"。钉在 plan 这一层。"""

    class FakeClient(object):
        def __init__(self, src, dst, widgets):
            self.src, self.dst, self.widgets = src, dst, widgets

        def field_map(self, app, entry):
            return self.widgets, {w["name"]: w for w in self.widgets.values()}

        def fetch_all(self, app, entry, **kw):
            return self.dst if entry == "DST" else self.src

    def _plan(self, src_att, dst_att):
        widgets = {"编号": W("编号", "text"), "合同": W("合同", "upload")}
        src = [{"_id": "S1", "_w_编号": "K1", "_w_合同": src_att}]
        dst = [{"_id": "T1", "_w_编号": "K1", "_w_合同": dst_att}]
        client = self.FakeClient(src, dst, widgets)
        table = {"alias": "t", "source_entry": "SRC", "target_entry": "DST", "key": "编号"}
        return plan_table(client, {"source": {"app": "A"}, "target": {"app": "B"}},
                          table, IdMap("/nonexistent/idmap.json"))

    def test_identical_files_with_fresh_urls_are_no_change(self):
        plan = self._plan([{"name": "合同.pdf", "size": 9, "url": "https://x/1?e=111"}],
                          [{"name": "合同.pdf", "size": 9, "url": "https://x/2?e=999"}])
        self.assertEqual(plan["updates"], [], "url 变了就重传，等于每次全量重传")
        self.assertEqual(plan["skips"], ["K1"])
        self.assertEqual(plan["attachments"]["files"], 0, "无变化就不该有文件要搬")

    def test_a_replaced_file_is_an_update(self):
        plan = self._plan([{"name": "合同-v2.pdf", "size": 9, "url": "https://x/1"}],
                          [{"name": "合同.pdf", "size": 9, "url": "https://x/2"}])
        self.assertEqual(len(plan["updates"]), 1)
        self.assertEqual(plan["attachments"]["files"], 1)

    def test_plan_says_how_many_files_and_how_big(self):
        """D8：附件要真的过一遍本地磁盘，总量必须在点 --execute **之前**就可见。"""
        plan = self._plan([{"name": "大.pdf", "size": 5000, "url": "https://x/1"}], [])
        self.assertEqual(plan["attachments"], {"files": 1, "bytes": 5000})
        self.assertEqual(plan["attachment_fields"], ["合同"])


class TestAttachmentFailureHoldsTheWholeRow(unittest.TestCase):
    """D7：这一行的附件搬不动，**整行不写**。

    不是"跳过这一列继续写"——那会留下一条"别的字段都在、附件列是空的"记录，
    看着完全正常，而它和源端已经不是同一条数据了。
    """

    class FakeClient(object):
        def __init__(self, bad=()):
            self.bad, self.calls = set(bad), []

        def copy_attachments(self, values, app, entry, txn, workdir=None):
            self.calls.append(txn)
            name = values[0].get("name")
            if name in self.bad:
                raise JdyError("DOWNLOAD", "下载附件失败（url 可能已过期）：HTTP 404")
            return ["key-%s" % name]

    def _items(self):
        return [("甲", {"合同": [{"name": "好.pdf", "url": "u1"}]}),
                ("乙", {"合同": [{"name": "坏.pdf", "url": "u2"}]}),
                ("丙", {"合同": [{"name": "也好.pdf", "url": "u3"}]})]

    def test_the_failing_row_is_held_and_the_others_still_go(self):
        c = self.FakeClient(bad=["坏.pdf"])
        ready, held = sync_apply.prepare_rows(c, "A", "E", self._items(), ["合同"], "txn")
        self.assertEqual([tag for tag, _v in ready], ["甲", "丙"])
        self.assertEqual([tag for tag, _why in held], ["乙"])

    def test_the_held_row_says_why(self):
        c = self.FakeClient(bad=["坏.pdf"])
        _ready, held = sync_apply.prepare_rows(c, "A", "E", self._items(), ["合同"], "txn")
        self.assertIn("下载附件失败", held[0][1])

    def test_values_are_replaced_by_upload_keys_not_the_read_back_shape(self):
        """写入只认上传后的 key 串列表；把读回来的 [{name,size,url}] 提交上去
        是**静默丢弃**（实测）。"""
        c = self.FakeClient()
        ready, _held = sync_apply.prepare_rows(c, "A", "E", self._items()[:1],
                                               ["合同"], "txn")
        self.assertEqual(ready[0][1]["合同"], ["key-好.pdf"])

    def test_partial_upload_is_a_failure_not_a_silent_half(self):
        """3 个附件只传上去 2 个，这一行就是搬坏了——不能当成搬好了。"""
        class Half(object):
            def copy_attachments(self, values, app, entry, txn, workdir=None):
                return ["only-one"]
        c = Half()
        with self.assertRaises(JdyError):
            sync_apply.copy_row_attachments(
                c, "A", "E", {"合同": [{"name": "a", "url": "u1"},
                                       {"name": "b", "url": "u2"}]}, ["合同"], "txn")

    def test_rows_without_attachment_fields_pass_through_untouched(self):
        c = self.FakeClient()
        items = [("甲", {"名称": "x"})]
        ready, held = sync_apply.prepare_rows(c, "A", "E", items, [], None)
        self.assertEqual((ready, held), (items, []))
        self.assertEqual(c.calls, [], "没有附件列还去取上传凭证，纯属白跑")


class TestAttachmentBatchesEachGetTheirOwnTransaction(unittest.TestCase):
    """D5/D6：带附件时每批 ≤100 行，**每批一个自己的事务号**。

    附件的 key 绑在 transaction_id 上，分块共用一个号会互相覆盖
    （write-behavior 三）——第二批之后的附件全部失效，接口还照样回报成功。

    另一半是 sync 与 excel-bridge 的**幂等模型不同**：excel-bridge 靠事务号幂等，
    所以一次导入只能一个号、带附件最多 100 行；sync 靠业务键比对幂等
    （重跑已一致就跳过、根本不重传），所以每批各用各的号，行数不设上限。
    """

    def test_each_batch_gets_a_distinct_transaction_id(self):
        pending = list(enumerate([{"合同": []}] * 250))
        batches = sync_apply.attachment_batches(pending, ["合同"])
        self.assertEqual([len(rows) for _txn, rows in batches], [100, 100, 50])
        txns = [txn for txn, _rows in batches]
        self.assertEqual(len(set(txns)), 3, "跨批复用事务号，后两批的附件会全部失效")

    def test_over_a_hundred_rows_with_attachments_is_not_refused(self):
        """excel-bridge 超过 100 行会直接报错；sync 分批就好，不该继承那条限制。"""
        pending = list(enumerate([{"合同": []}] * 250))
        self.assertEqual(sum(len(rows) for _t, rows in
                             sync_apply.attachment_batches(pending, ["合同"])), 250)

    def test_without_attachments_nothing_is_batched_or_stamped(self):
        """没有附件就照旧：一次交给内核，它自己按 100 分块。"""
        pending = list(enumerate([{"名称": "x"}] * 250))
        batches = sync_apply.attachment_batches(pending, [])
        self.assertEqual(len(batches), 1)
        self.assertIsNone(batches[0][0])

    def test_nothing_to_write_means_no_batches(self):
        self.assertEqual(sync_apply.attachment_batches([], ["合同"]), [])


class TestVerifyComplexCatchesWhatTheKernelCannot(unittest.TestCase):
    """写后回读：子表单/附件要**按自己的口径**比，不能只问"是不是空的"。

    内核的 verify_written / update 回读只有一句判断：提交了值、写进去是不是空的。
    内层映射错了搬过去半列、附件搬成了别的文件——字段**都不是空的**，
    那一关照样过，然后打印"逐字段回读核对通过"。这类失败只有这里能接住。
    """

    # 目标端的子表单比源端**多一列**（公式算出来的小计）——这是常态，
    # 而它正是"拿目标端全部内层列去比"会永远判不一致的那一列。
    WIDGETS = {"明细": SUB("明细", [("品名", "text"), ("数量", "number"),
                                    ("小计", "number")]),
               "合同": W("合同", "upload")}

    class FakeClient(object):
        def __init__(self, stored):
            self.stored = stored

        def fetch_rows_by_id(self, app, entry, ids):
            return {i: self.stored[i] for i in ids if i in self.stored}

    def _check(self, stored_row, expected):
        c = self.FakeClient({"T1": stored_row})
        return verify_complex(c, "A", "E", [("T1", "K1", expected)], self.WIDGETS)

    def test_matching_subform_passes(self):
        stored = {"_w_明细": [{"_id": "x", "_w_明细_品名": "螺丝", "_w_明细_数量": 3}]}
        self.assertEqual(self._check(stored, {"明细": [{"品名": "螺丝", "数量": 3}]}), [])

    def test_a_subform_that_landed_with_different_content_is_caught(self):
        """行数一样、内容不一样——**内核那一关看不见**（字段不是空的）。"""
        stored = {"_w_明细": [{"_id": "x", "_w_明细_品名": "螺母", "_w_明细_数量": 9}]}
        mism = self._check(stored, {"明细": [{"品名": "螺丝", "数量": 3}]})
        self.assertEqual(len(mism), 1)
        self.assertEqual(mism[0]["field"], "明细")

    def test_extra_inner_columns_on_the_target_are_not_a_mismatch(self):
        """目标端多出来的内层列（公式算的小计之类）不参与比对——
        比它会永远判不一致，而我们根本没写那一列。"""
        stored = {"_w_明细": [{"_id": "x", "_w_明细_品名": "螺丝",
                               "_w_明细_数量": 3, "_w_明细_小计": 99}]}
        self.assertEqual(self._check(stored, {"明细": [{"品名": "螺丝", "数量": 3}]}), [])

    def test_attachment_compared_by_name_and_size_not_url(self):
        stored = {"_w_合同": [{"name": "合同.pdf", "size": 9, "url": "https://新/1?e=2"}]}
        expect = {"合同": [{"name": "合同.pdf", "size": 9, "url": "https://旧/1?e=1"}]}
        self.assertEqual(self._check(stored, expect), [])

    def test_an_attachment_that_landed_as_a_different_file_is_caught(self):
        stored = {"_w_合同": [{"name": "合同-2.pdf", "size": 9, "url": "u"}]}
        expect = {"合同": [{"name": "合同.pdf", "size": 9, "url": "u"}]}
        self.assertEqual(len(self._check(stored, expect)), 1)

    def test_a_row_that_cannot_be_read_back_is_reported(self):
        c = self.FakeClient({})
        mism = verify_complex(c, "A", "E", [("T1", "K1", {"合同": [{"name": "a"}]})],
                              self.WIDGETS)
        self.assertEqual(len(mism), 1)
        self.assertIn("回读不到", mism[0]["actual"])


class TestDraftAgreesWithWhatPlanWillDo(unittest.TestCase):
    """init_config 生成的草稿，和 plan 真正执行时的判断必须是**同一个**判断。

    来历：草稿这边自己列了三组"搬不动"的类型，漏了 COMPLEX_WRITE
    （子表单/附件/图片）——于是它们被写进 fields 白名单当"可搬"，
    而同步执行时 plan 又拒绝它们。同一份配置，生成它的人说能搬、
    执行它的人说不能搬。名单散在两处，迟早分叉。
    """

    def test_every_type_the_kernel_refuses_is_excluded_from_the_draft(self):
        import jdy_client
        refused = (jdy_client.NOT_WRITABLE_TYPES | jdy_client.READ_ONLY_TYPES
                   | jdy_client.UNVERIFIED_WRITE | jdy_client.COMPLEX_WRITE)
        for wtype in sorted(refused):
            widget = {"name": "_w_x", "label": "某字段", "type": wtype}
            self.assertFalse(jdy_client.writable_back(widget)[0], wtype)

    def test_both_sides_exclude_exactly_the_same_fields(self):
        """**这条才对得起这个类名。** 前面几条各验一侧，两侧各自对着内核点头，
        却从来没有相互比对过——而"草稿说能搬、计划说不能搬"正是两侧不一致。
        这里把同一批字段同时喂给两边，要求结论逐字段一致。
        """
        import jdy_client
        types = (["text", "number", "datetime", "radiogroup", "user", "dept"]
                 + sorted(jdy_client.NOT_WRITABLE_TYPES | jdy_client.READ_ONLY_TYPES
                          | jdy_client.UNVERIFIED_WRITE | jdy_client.COMPLEX_WRITE))
        dst = {t: {"name": "_w_%s" % t, "label": t, "type": t} for t in types}
        mapping = {t: t for t in types}

        draft = set(init_config.blocked_fields(sorted(dst), mapping, dst, dst))
        _m, excluded = resolve_fields(dst, dst, mapping)
        plan_side = {label for label, _why in excluded}
        self.assertEqual(draft, plan_side,
                         "草稿与计划对这些字段的结论不一致：%s"
                         % sorted(draft ^ plan_side))
        # 附件/子表单曾经"两边都排除"，现在搬得动了，所以这里不再断言它们被排除——
        # **要守的是两侧结论一致**，而不是结论具体是什么。

    def test_a_new_kernel_bucket_would_be_excluded_by_both(self):
        """内核哪天多认一种"不能原样写回去"的类型，两边都得自动拦住它。

        原来 resolve_fields 把四组类型各 if 了一遍——那是第二套实现，
        新类型会直接漏过去、搬进目标表再静默丢。
        """
        import jdy_client
        saved = set(jdy_client.UNVERIFIED_WRITE)
        jdy_client.UNVERIFIED_WRITE.add("某种新控件")
        try:
            dst = {"新": {"name": "_w_x", "label": "新", "type": "某种新控件"}}
            _m, excluded = resolve_fields(dst, dst, {"新": "新"})
            self.assertEqual([l for l, _ in excluded], ["新"])
            self.assertEqual(init_config.blocked_fields(["新"], {"新": "新"}, dst, dst),
                             ["新"])
        finally:
            jdy_client.UNVERIFIED_WRITE.clear()
            jdy_client.UNVERIFIED_WRITE.update(saved)

    def test_the_draft_actually_excludes_them(self):
        """光验内核不算数——要验**草稿这一侧真的问了内核**。

        第一版就只测了 writable_back 本身，于是把 init_config 改回旧的三组名单，
        测试照样全绿：测的是被调用方，不是调用方。
        """
        dst = {"备注": W("备注", "text"),
               "明细": SUB("明细", [("子文本", "text")]),
               "空明细": SUB("空明细", []),
               "合同": W("合同", "upload"),
               "编号": W("编号", "sn")}
        mapping = {l: l for l in dst}
        got = init_config.blocked_fields(sorted(dst), mapping, dst, dst)
        # 「明细」「合同」现在搬得动了（W2/W3）；「编号」是流水号系统生成；
        # 「空明细」没有内层字段可对，整列搬不过去。
        self.assertEqual(sorted(got), sorted(["编号", "空明细"]))

    def test_complex_write_types_are_the_ones_that_used_to_slip_through(self):
        """钉住这一条：附件/图片/子表单曾经被草稿当成可搬。"""
        import jdy_client
        for wtype in ("subform", "image", "upload"):
            self.assertIn(wtype, jdy_client.COMPLEX_WRITE)
            widget = {"name": "_w_x", "label": "某字段", "type": wtype}
            ok, why = jdy_client.writable_back(widget)
            self.assertFalse(ok)
            self.assertIn("重新组装", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
