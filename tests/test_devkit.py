# -*- coding: utf-8 -*-
"""jdy-devkit：生成物必须真能跑。

这个技能的产出是**代码**，代码能不能跑是可验证的——所以测试的重点是
"生成的东西是合法可执行的"，而不是"文案写得好不好"。
"""
import ast
import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))
import jdy_client  # noqa: E402

_SCRIPTS = os.path.join(ROOT, "skills", "jdy-devkit", "scripts")


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


shapes = _load("devkit_shapes", "shapes.py")
gen = _load("devkit_gen", "gen.py")

WIDGETS = [
    {"label": "姓名", "name": "_widget_1", "type": "text"},
    {"label": "手机", "name": "_widget_2", "type": "phone"},
    {"label": "住址", "name": "_widget_3", "type": "address"},
    {"label": "负责人", "name": "_widget_4", "type": "user"},
    {"label": "关联客户", "name": "_widget_5", "type": "linkdata"},
    {"label": "编号", "name": "_widget_6", "type": "sn"},
    {"label": "入职", "name": "_widget_7", "type": "datetime"},
    {"label": "附件", "name": "_widget_8", "type": "upload"},
]


class TestShapes(unittest.TestCase):

    def test_unwritable_types_are_flagged(self):
        for t in ("linkdata", "sn"):
            ok, example, note = shapes.shape_of(t)
            self.assertFalse(ok, t)
            self.assertIsNone(example)
            self.assertTrue(note)

    def test_phone_shape_is_wrapped(self):
        # 读回来是 {verified, phone}，写进去只给 {"phone": ...}——写裸串会被丢
        _ok, example, _n = shapes.shape_of("phone")
        self.assertIn('"phone"', example)
        self.assertTrue(example.strip().startswith("{"))

    def test_address_shape_is_object(self):
        _ok, example, _n = shapes.shape_of("address")
        self.assertTrue(example.strip().startswith("{"))

    def test_unknown_type_is_conservative(self):
        # 没收录的类型不能假装知道，得让人先试写再回读
        ok, _e, note = shapes.shape_of("某个还没出现的新控件")
        self.assertTrue(ok)
        self.assertIn("回读", note)

    def test_field_payload_wraps_in_value(self):
        # 简道云要求每个字段都包一层 {"value": ...}，漏了就整条写不进去
        payload, _note = shapes.field_payload(WIDGETS[0])
        self.assertIn('"_widget_1": {"value":', payload)

    def test_field_payload_none_for_unwritable(self):
        payload, note = shapes.field_payload(WIDGETS[4])
        self.assertIsNone(payload)
        self.assertTrue(note)


class TestGeneratedCodeIsValid(unittest.TestCase):

    def setUp(self):
        self.rows = gen.field_table(WIDGETS)

    def test_python_sample_parses(self):
        src = gen.render_python("APP", "ENTRY", self.rows)
        ast.parse(src)                      # 语法不合法就直接炸在这

    def test_validator_parses_and_works(self):
        src = gen.render_validator(self.rows)
        ast.parse(src)
        ns = {}
        exec(compile(src, "<validate>", "exec"), ns)
        validate = ns["validate"]
        self.assertEqual(validate({"姓名": "张三"}), [])
        self.assertTrue(validate({"住址": "广东省深圳市"}))     # address 要对象
        self.assertTrue(validate({"入职": "2026/08/29"}))      # datetime 要 ISO
        self.assertTrue(validate({"附件": "a.pdf"}))           # upload 要数组
        self.assertTrue(validate({"没这列": "x"}))

    def test_validator_rejects_unwritable_fields(self):
        ns = {}
        exec(compile(gen.render_validator(self.rows), "<v>", "exec"), ns)
        problems = ns["validate"]({"关联客户": "任何值"})
        self.assertTrue(problems)
        self.assertIn("写不进去", problems[0])

    def test_validator_warns_about_lookup(self):
        """关联数据能写，但接口不校验引用是否存在——自动校验不了，只能提醒。

        这条是"能写但有风险"，所以走 warnings 而不是 problems：
        当成错误会让人绕过校验，不提又是最难发现的坑（写完一切正常，
        回读也对，因为读回来就是你写进去的那个假 ID）。
        """
        rows = gen.field_table(WIDGETS + [
            {"label": "所属订单", "name": "_widget_9", "type": "lookup"}])
        ns = {}
        exec(compile(gen.render_validator(rows), "<v>", "exec"), ns)
        warns = []
        problems = ns["validate"]({"所属订单": "a" * 24}, warns)
        self.assertEqual(problems, [])          # 不是错误
        self.assertTrue(warns)                  # 但要提醒
        self.assertIn("不校验引用是否存在", warns[0])

    def test_curl_sample_has_no_secret(self):
        src = gen.render_curl("APP", "ENTRY", self.rows)
        self.assertIn("JDY_API_KEY", src)
        self.assertNotIn("Bearer sk", src)
        self.assertNotIn("Bearer ey", src)

    def test_samples_only_include_writable_fields(self):
        src = gen.render_python("APP", "ENTRY", self.rows)
        self.assertNotIn("_widget_5", src)   # linkdata
        self.assertNotIn("_widget_6", src)   # sn

    def test_markdown_marks_unwritable(self):
        md = gen.render_markdown("APP", "ENTRY", "表", self.rows)
        self.assertIn("❌", md)
        self.assertIn("_widget_5", md)       # 写不进去也要列出来，不能不提


class TestUnwritableListHasOneSource(unittest.TestCase):
    """不可写清单只有内核一份。

    原来 shapes.py 自己另列了一份，比内核多出一个 autonum：
    devkit 生成的代码说「这列写不进去」，而预检/同步/清洗照样放它过去。
    同一个概念两份清单，说法必然分叉，而分叉的那一侧是静默的。
    """

    def test_derived_from_the_kernel(self):
        import jdy_client as jc
        self.assertEqual(set(shapes.NOT_WRITABLE), jc.NOT_WRITABLE_TYPES | jc.READ_ONLY_TYPES)

    def test_every_unwritable_type_has_a_reason(self):
        for wtype, reason in shapes.NOT_WRITABLE.items():
            self.assertTrue(reason and reason.strip(), wtype)
            writable, _example, note = shapes.shape_of(wtype)
            self.assertFalse(writable, wtype)
            self.assertEqual(note, reason)

    def test_autonum_is_blocked_everywhere_not_just_in_devkit(self):
        """内核拦得住，三条写入链路才拦得住——它们都只认内核那份清单。"""
        import jdy_client as jc
        with self.assertRaises(jc.NotWritableField) as cm:
            jc.encode_value({"label": "编号", "type": "autonum"}, "A-001")
        self.assertEqual(cm.exception.kind, "system_generated")


class TestShapesAgreeWithTheKernel(unittest.TestCase):
    """**每个示例都要能被内核的编码器接受。**

    来历：这张表是二期写入实验**之前**写的，实验推翻了其中几条却没人回来改它——
    `dept` 写着"部门名"（实测只认裸 dept_no 整数，写名字静默存 null）、
    附件写着读回来的 {name,url}（实测要写上传后的 key 字符串列表）。
    devkit 的示例是给人**直接抄进生产代码**的，错的示例比没有示例更糟。

    光靠"记得同步"是不行的，所以这里让示例真的过一遍 encode_value：
    形状对不上，测试就红。
    """

    def _widget(self, wtype):
        return {"name": "_widget_x", "label": "某字段", "type": wtype}

    def test_every_example_survives_encode_value(self):
        bad = []
        for wtype, (example, _note) in sorted(shapes.WRITE_SHAPE.items()):
            if wtype == "subform":
                continue           # 子表单走 encode_row 的专用分支，不经 encode_value
            try:
                value = json.loads(example)
            except ValueError as exc:
                bad.append("%s 的示例不是合法 JSON：%s" % (wtype, exc))
                continue
            try:
                jdy_client.encode_value(self._widget(wtype), value)
            except Exception as exc:
                bad.append("%s 的示例 %s 内核不认：%s" % (wtype, example, exc))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_unverified_types_get_no_made_up_example(self):
        """没实测过的类型**不许给形状**——猜一个填上去，抄的人一跑就是静默丢值。"""
        for wtype in jdy_client.UNVERIFIED_WRITE:
            ok, example, note = shapes.shape_of(wtype)
            self.assertFalse(ok, wtype)
            self.assertIsNone(example, wtype)
            self.assertIn("尚未实测", note)

    def test_complex_types_say_it_takes_more_than_a_value(self):
        for wtype in jdy_client.COMPLEX_WRITE:
            if wtype == "subform":
                continue
            _ok, _example, note = shapes.shape_of(wtype)
            self.assertIn("流程", note, wtype)


if __name__ == "__main__":
    unittest.main(verbosity=2)
