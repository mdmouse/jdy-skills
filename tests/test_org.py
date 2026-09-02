# -*- coding: utf-8 -*-
"""jdy-org：通讯录的读与写。

这是全仓唯一**会花钱**的写入：官方明示新建成员自动激活、占用一个用户数。
所以这里测的重点不是"能不能建"，而是**该拦的拦住没、该说的说清楚没**——
数据写错能改回来，多占的坐席不能。

    python3 tests/test_org.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))

_SCRIPTS = os.path.join(ROOT, "skills", "jdy-org", "scripts")


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


org = _load("jdyorg_org", "org.py")
from miniyaml import parse as parse_yaml  # noqa: E402

CURRENT = {"departments": [{"dept_no": 2, "name": "研发", "parent_no": 1},
                           {"dept_no": 3, "name": "测试", "parent_no": 2}],
           "members": [{"username": "sys_a", "name": "甲", "departments": [2]},
                       {"username": "sys_b", "name": "乙", "departments": [3]}]}


class TestOrgWriteGate(unittest.TestCase):
    """通讯录写入要一道**自己的**开关。

    `JDY_WRITE_ALLOWLIST` 按 app_id/entry_id 限定可写的表单，而通讯录接口的
    body 里根本没有这两样——那道闸对它是瞎的。与其让一道看起来生效、实际
    不生效的闸门给人虚假的安全感，不如另设一道。（流程写接口是同一个结构性
    缺口，只是这里的影响面是整个企业。）
    """

    def setUp(self):
        self._saved = os.environ.get(org.ORG_WRITE_ENV)
        os.environ.pop(org.ORG_WRITE_ENV, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(org.ORG_WRITE_ENV, None)
        else:
            os.environ[org.ORG_WRITE_ENV] = self._saved

    def test_unset_refuses(self):
        with self.assertRaises(org.OrgWriteRefused) as cm:
            org.check_org_write()
        self.assertIn(org.ORG_WRITE_ENV, str(cm.exception))

    def test_truthy_values_open_it(self):
        for value in ("1", "true", "yes", "on"):
            os.environ[org.ORG_WRITE_ENV] = value
            org.check_org_write()               # 不抛就是放行

    def test_other_values_still_refuse(self):
        for value in ("0", "false", "", "  "):
            os.environ[org.ORG_WRITE_ENV] = value
            with self.assertRaises(org.OrgWriteRefused):
                org.check_org_write()

    def test_the_form_allowlist_is_not_what_guards_this(self):
        """写入白名单设了也不该放行通讯录——两道闸管的不是一件事。"""
        os.environ.pop(org.ORG_WRITE_ENV, None)
        saved = os.environ.get("JDY_WRITE_ALLOWLIST")
        os.environ["JDY_WRITE_ALLOWLIST"] = "E1,E2"
        try:
            with self.assertRaises(org.OrgWriteRefused):
                org.check_org_write()
        finally:
            if saved is None:
                os.environ.pop("JDY_WRITE_ALLOWLIST", None)
            else:
                os.environ["JDY_WRITE_ALLOWLIST"] = saved


class TestClassify(unittest.TestCase):
    """新建成员必须被**单独数出来**——它是计费后果，
    混在"共 N 项改动"里报，人是看不见那笔账的。"""

    def test_new_members_are_their_own_bucket(self):
        plan = {"members": [{"username": "sys_a", "departments": [3]},
                            {"name": "丙", "username": "sys_c"}]}
        got = org.classify(plan, CURRENT)
        self.assertEqual([m["username"] for m in got["member_update"]], ["sys_a"])
        self.assertEqual([m["name"] for m in got["member_create"]], ["丙"])

    def test_a_member_without_username_is_a_new_one(self):
        got = org.classify({"members": [{"name": "丁"}]}, CURRENT)
        self.assertEqual(len(got["member_create"]), 1)

    def test_departments_split_by_whether_the_number_exists(self):
        plan = {"departments": [{"dept_no": 2, "name": "研发中心"},
                                {"name": "新部门", "parent_no": 1},
                                {"dept_no": 99, "name": "编号还不存在"}]}
        got = org.classify(plan, CURRENT)
        self.assertEqual([d["dept_no"] for d in got["dept_update"]], [2])
        self.assertEqual(len(got["dept_create"]), 2)

    def test_root_counts_as_existing(self):
        """根部门 1 永远存在，不能被当成"要新建"。"""
        got = org.classify({"departments": [{"dept_no": 1, "name": "改根部门名"}]}, CURRENT)
        self.assertEqual(len(got["dept_update"]), 1)
        self.assertEqual(got["dept_create"], [])

    def test_describe_names_the_new_members(self):
        got = org.classify({"members": [{"name": "丙"}]}, CURRENT)
        self.assertIn("新增成员", "\n".join(org.describe(got)))


class TestPlanValidation(unittest.TestCase):
    def _plan(self, text):
        path = os.path.join(tempfile.mkdtemp(), "p.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_a_plan_that_does_nothing_is_refused(self):
        with self.assertRaises(org.OrgError):
            org.load_plan(self._plan("departments: []\n"), parse_yaml)

    def test_department_needs_a_name_or_a_number(self):
        with self.assertRaises(org.OrgError):
            org.load_plan(self._plan("departments:\n  - seq: 1\n"), parse_yaml)

    def test_member_needs_a_name_or_a_username(self):
        with self.assertRaises(org.OrgError):
            org.load_plan(self._plan("members:\n  - departments: [1]\n"), parse_yaml)

    def test_departments_must_be_a_list(self):
        with self.assertRaises(org.OrgError) as cm:
            org.load_plan(self._plan("members:\n  - username: sys_a\n"
                                     "    departments: 3\n"), parse_yaml)
        self.assertIn("编号列表", str(cm.exception))

    def test_a_valid_plan_round_trips(self):
        plan = org.load_plan(self._plan(
            "departments:\n  - name: 研发\n    parent_no: 1\n"
            "members:\n  - username: sys_a\n    departments: [2]\n"), parse_yaml)
        self.assertEqual(plan["departments"][0]["name"], "研发")


class TestTreeRendering(unittest.TestCase):
    def test_nesting_and_headcount(self):
        lines = org.tree_lines(CURRENT["departments"], CURRENT["members"])
        self.assertIn("研发（编号 2）", lines[1])
        self.assertTrue(lines[2].startswith("    "))          # 测试挂在研发下面
        self.assertIn("1 人", lines[1])

    def test_a_department_whose_parent_is_missing_is_still_shown(self):
        """挂在树外的部门不能悄悄漏掉——漏掉就等于告诉用户"没有这个部门"。"""
        orphan = [{"dept_no": 9, "name": "孤儿部门", "parent_no": 77}]
        lines = org.tree_lines(orphan, [])
        self.assertTrue(any("孤儿部门" in l for l in lines))
        self.assertTrue(any("不在本次返回里" in l for l in lines))

    def test_empty_org_still_renders_the_root(self):
        lines = org.tree_lines([], [])
        self.assertEqual(len(lines), 1)
        self.assertIn("编号 1", lines[0])


class TestNoDeleteEndpointsAreWired(unittest.TestCase):
    """官方有删除成员/批量删除/删除部门，本技能**一个都不接**。

    删一个部门，它下面的人和权限一起没了；而本项目的规矩是从不删数据。
    这条用测试钉住，免得哪天有人"顺手补全 CRUD"。
    """

    def test_no_delete_paths_anywhere_in_the_skill(self):
        offenders = []
        for name in sorted(os.listdir(_SCRIPTS)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(_SCRIPTS, name), encoding="utf-8") as fh:
                body = fh.read()
            for path in ("corp/user/delete", "corp/user/batch_delete",
                         "corp/department/delete"):
                if path in body:
                    offenders.append("%s 连了 %s" % (name, path))
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_skill_says_so_out_loud(self):
        with open(os.path.join(ROOT, "skills", "jdy-org", "SKILL.md"),
                  encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("不删除", md)
        self.assertIn("占用一个用户数", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
