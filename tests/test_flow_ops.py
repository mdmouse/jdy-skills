# -*- coding: utf-8 -*-
"""jdy-flow-ops：待办、积压、批量审批。

这个技能一直没有测试文件，而它是唯一会**改变别人流程状态**的技能——
审批发出去就不可自动撤销，比导错数据还难挽回。

之前没测的直接原因是**没有函数边界**：分组、瓶颈、卡住判定全焊在 main()
里跟渲染缠在一起。所以先把纯逻辑拆出来（backlog.analyze / inbox.group_tasks），
再对着它们写。拆完用真实账号逐字比对过输出没变。

    python3 tests/test_flow_ops.py
"""
import datetime
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))

import platform_env  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tests"))
from _fixtures import unwritable_path  # noqa: E402

# 多个技能都有同名脚本，按路径显式加载，别让 `import inbox` 变成"谁先进路径谁赢"
_SCRIPTS = os.path.join(ROOT, "skills", "jdy-flow-ops", "scripts")


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


flow = _load("flowops_flow", "flow.py")
inbox = _load("flowops_inbox", "inbox.py")
backlog = _load("flowops_backlog", "backlog.py")
act = _load("flowops_act", "act.py")
nudge = _load("flowops_nudge", "nudge.py")

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
ISO = lambda h: (NOW - datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def task(tid, node="审批节点", status=0, created_h=1.0, finished_h=None, **kw):
    t = {"task_id": tid, "flow_name": node, "status": status,
         "create_time": ISO(created_h),
         "finish_time": None if finished_h is None else ISO(finished_h)}
    t.update(kw)
    return t


class TestTimeParsing(unittest.TestCase):
    """时间字段实测是 ISO-8601 UTC 毫秒；但接口哪天少给个 Z，
    naive 减 aware 就是一个裸 TypeError——"看看等了多久"这种只读操作
    不该以 traceback 收场。"""

    def test_z_suffixed_iso(self):
        self.assertEqual(flow.parse_time("2026-08-28T08:35:14.385Z"),
                         datetime.datetime(2026, 8, 28, 8, 35, 14, 385000, tzinfo=UTC))

    def test_naive_string_is_assumed_utc_not_crashed_on(self):
        got = flow.parse_time("2026-08-28T08:35:14")
        self.assertEqual(got.utcoffset(), datetime.timedelta(0))

    def test_garbage_and_empty_are_none(self):
        for bad in ("下周三", "", None, "2026/08/28"):
            self.assertIsNone(flow.parse_time(bad), bad)

    def test_stuck_hours_never_raises_on_a_naive_create_time(self):
        t = {"create_time": "2026-08-31T06:00:00", "finish_time": None}
        self.assertEqual(flow.stuck_hours(t, NOW), 6.0)

    def test_finished_task_reports_actual_duration_not_time_since(self):
        """已完成的节点要报**实际耗时**，不是"距今多久"——
        否则越是老实例，节点耗时看着越长，瓶颈分析全错。"""
        t = task("a", status=1, created_h=10, finished_h=8)
        self.assertEqual(flow.stuck_hours(t, NOW), 2.0)

    def test_missing_create_time_is_unknown_not_zero(self):
        self.assertIsNone(flow.stuck_hours({"create_time": None}, NOW))


class TestHumanize(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(flow.humanize(0.5), "30 分钟")
        self.assertEqual(flow.humanize(1), "1.0 小时")
        self.assertEqual(flow.humanize(47.9), "47.9 小时")
        self.assertEqual(flow.humanize(48), "2.0 天")

    def test_unknown_says_unknown_rather_than_zero(self):
        self.assertEqual(flow.humanize(None), "未知")


class TestContentText(unittest.TestCase):
    """task/list 只返回元数据、零业务字段，同表单的多条待办长得一模一样；
    而人指代审批永远靠内容（"把那条五万的批了"）。"""

    def test_flattens_for_matching_and_display(self):
        got = flow.content_text({"申请标题": "补料", "数量": 5})
        self.assertIn("申请标题=补料", got)
        self.assertIn("数量=5", got)

    def test_empty_content_is_an_empty_string_not_a_crash(self):
        self.assertEqual(flow.content_text({}), "")


class TestInboxGrouping(unittest.TestCase):
    """排序是有意的：组按条数降序、组内按已等时长降序，
    "最该先处理的"要在最上面。人打开收件箱是要挑一件事做。"""

    TASKS = [
        dict(task("t1", node="审批"), form_title="请假", _stuck_hours=2,
             creator={"name": "张三"}),
        dict(task("t2", node="复核"), form_title="请假", _stuck_hours=50,
             creator={"name": "李四"}),
        dict(task("t3", node="审批"), form_title="报销", _stuck_hours=10,
             creator={"name": "张三"}),
        dict(task("t4", node="审批"), form_title="请假", _stuck_hours=1,
             creator={"name": "张三"}),
    ]

    def test_bigger_groups_come_first(self):
        got = inbox.group_tasks(self.TASKS, "form")
        self.assertEqual([name for name, _ in got], ["请假", "报销"])

    def test_longest_waiting_comes_first_within_a_group(self):
        got = dict(inbox.group_tasks(self.TASKS, "form"))
        self.assertEqual([t["task_id"] for t in got["请假"]], ["t2", "t1", "t4"])

    def test_group_by_node_and_creator(self):
        self.assertEqual([n for n, _ in inbox.group_tasks(self.TASKS, "node")],
                         ["审批", "复核"])
        self.assertEqual([n for n, _ in inbox.group_tasks(self.TASKS, "creator")],
                         ["张三", "李四"])

    def test_missing_fields_get_a_named_bucket_instead_of_vanishing(self):
        got = dict(inbox.group_tasks([{"task_id": "x"}], "form"))
        self.assertEqual(list(got), ["(未知表单)"])

    def test_unknown_dimension_raises_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            inbox.group_tasks(self.TASKS, "随便什么")

    def test_ties_are_ordered_deterministically(self):
        """条数相同的组，顺序不能跑来跑去——否则同一份待办两次打印不一样。"""
        pair = [dict(task("a"), form_title="乙", _stuck_hours=1),
                dict(task("b"), form_title="甲", _stuck_hours=1)]
        self.assertEqual([n for n, _ in inbox.group_tasks(pair, "form")],
                         [n for n, _ in inbox.group_tasks(list(reversed(pair)), "form")])


class TestBacklogAnalysis(unittest.TestCase):
    """用户会照着"瓶颈节点是谁"去改流程配置。这个数字算错，
    改的就是错的节点，而且没有任何东西会告诉他。"""

    INSTANCES = [
        {"status": 1, "form_title": "缺货申请", "tasks": [
            task("f1", node="流程发起节点", status=1, created_h=100, finished_h=100),
            task("f2", node="审批节点", status=1, created_h=100, finished_h=98)]},
        {"status": 1, "form_title": "缺货申请", "tasks": [
            task("f3", node="流程发起节点", status=1, created_h=50, finished_h=50),
            task("f4", node="审批节点", status=1, created_h=50, finished_h=44)]},
        {"status": 0, "form_title": "缺货申请", "tasks": [
            task("f5", node="流程发起节点", status=1, created_h=72, finished_h=72),
            task("p1", node="审批节点", status=0, created_h=72)]},
    ]

    def setUp(self):
        self.r = backlog.analyze(self.INSTANCES, now=NOW, threshold=24.0)

    def test_instance_status_counts(self):
        self.assertEqual(self.r["by_status"], {"已完成": 2, "进行中": 1})

    def test_pending_only_counts_unfinished_nodes(self):
        self.assertEqual([t["task_id"] for _h, t, _i in self.r["pending"]], ["p1"])

    def test_over_threshold_is_flagged(self):
        self.assertEqual(len(self.r["over"]), 1)
        self.assertEqual(backlog.analyze(self.INSTANCES, NOW, threshold=100.0)["over"], [])

    def test_node_stats_use_finished_nodes_only(self):
        """把"还没办完"的算进平均，等得越久平均越难看——
        而那恰恰不是"这个节点办得慢"的证据。卡住的 p1 不能进统计。"""
        stats = dict((name, (avg, n)) for name, avg, n, _w in self.r["node_stats"])
        self.assertEqual(stats["审批节点"], (4.0, 2))        # (2h + 6h) / 2
        self.assertEqual(stats["流程发起节点"], (0.0, 3))

    def test_bottleneck_is_the_slowest_average(self):
        self.assertEqual(self.r["bottleneck"], "审批节点")

    def test_worst_case_is_reported_next_to_the_average(self):
        worst = dict((name, w) for name, _a, _n, w in self.r["node_stats"])
        self.assertEqual(worst["审批节点"], 6.0)

    def test_no_finished_nodes_means_no_bottleneck_claim(self):
        """一个已完成节点都没有时不能硬报一个瓶颈——那是编的。"""
        only_pending = [{"status": 0, "tasks": [task("p", status=0, created_h=5)]}]
        r = backlog.analyze(only_pending, NOW)
        self.assertEqual(r["node_stats"], [])
        self.assertIsNone(r["bottleneck"])

    def test_no_instances_at_all(self):
        r = backlog.analyze([], NOW)
        self.assertEqual((r["pending"], r["node_stats"], r["bottleneck"]), ([], [], None))

    def test_pending_sorted_by_wait_descending(self):
        many = [{"status": 0, "tasks": [task("a", status=0, created_h=3),
                                        task("b", status=0, created_h=30),
                                        task("c", status=0, created_h=10)]}]
        r = backlog.analyze(many, NOW)
        self.assertEqual([t["task_id"] for _h, t, _i in r["pending"]], ["b", "c", "a"])


class TestApprovalConfirmCode(unittest.TestCase):
    """审批不可自动撤销，所以确认码必须绑在**这一批 task_id 的集合**上。

    dry-run 与 --execute 是两次调用、各自实时重拉；中间新到的待办原来会被
    静默一起批掉——用户点头的是 3 条，执行的是 5 条，多出来的他从没见过。
    """

    A = [{"task_id": "t1"}, {"task_id": "t2"}]

    def test_same_set_same_code_regardless_of_order(self):
        self.assertEqual(act.confirm_code("approve", "u", self.A),
                         act.confirm_code("approve", "u", list(reversed(self.A))))

    def test_one_more_task_invalidates_the_code(self):
        more = self.A + [{"task_id": "t3"}]
        self.assertNotEqual(act.confirm_code("approve", "u", self.A),
                            act.confirm_code("approve", "u", more))

    def test_one_fewer_task_invalidates_the_code(self):
        self.assertNotEqual(act.confirm_code("approve", "u", self.A),
                            act.confirm_code("approve", "u", self.A[:1]))

    def test_a_different_action_is_a_different_code(self):
        """拿"同意"的码去执行"否决"必须不通过。"""
        self.assertNotEqual(act.confirm_code("approve", "u", self.A),
                            act.confirm_code("reject", "u", self.A))

    def test_a_different_operator_is_a_different_code(self):
        self.assertNotEqual(act.confirm_code("approve", "u", self.A),
                            act.confirm_code("approve", "别人", self.A))

    def test_code_is_short_and_lowercase_like_everywhere_else(self):
        self.assertRegex(act.confirm_code("approve", "u", self.A), r"^[0-9a-f]{8}$")


class TestTaskFiltering(unittest.TestCase):
    """筛错了就是批错了人的单子。"""

    class FakeClient(object):
        def __init__(self, tasks):
            self.tasks = tasks

    class Args(object):
        def __init__(self, **kw):
            self.tasks_json = None
            self.form = self.node = self.contains = None
            self.task_id = None
            self.__dict__.update(kw)

    TASKS = [
        dict(task("t1"), form_title="缺货申请", flow_name="审批节点",
             _content={"申请标题": "常规补料", "数量": 5}),
        dict(task("t2"), form_title="缺货申请", flow_name="复核节点",
             _content={"申请标题": "加急补料", "数量": 50}),
        dict(task("t3"), form_title="请假申请", flow_name="审批节点",
             _content={"申请标题": "年假"}),
    ]

    def _run(self, **kw):
        mod = act
        saved = mod.iter_tasks, mod.task_content
        mod.iter_tasks = lambda client, username: list(self.TASKS)
        mod.task_content = lambda client, t: t.get("_content", {})
        try:
            return [t["task_id"] for t in
                    mod.load_tasks(self.FakeClient(self.TASKS), self.Args(**kw), "u")]
        finally:
            mod.iter_tasks, mod.task_content = saved

    def test_no_filter_takes_everything(self):
        self.assertEqual(self._run(), ["t1", "t2", "t3"])

    def test_by_form_is_a_substring_match(self):
        self.assertEqual(self._run(form="缺货"), ["t1", "t2"])

    def test_by_node(self):
        self.assertEqual(self._run(node="审批"), ["t1", "t3"])

    def test_by_explicit_task_ids(self):
        self.assertEqual(self._run(task_id=["t2"]), ["t2"])

    def test_by_content_because_people_refer_to_approvals_by_content(self):
        """"把那条加急的批了"——人说的是内容，不是表单名。"""
        self.assertEqual(self._run(contains="加急"), ["t2"])

    def test_filters_are_combined_with_and(self):
        self.assertEqual(self._run(form="缺货", node="审批"), ["t1"])

    def test_a_filter_matching_nothing_yields_nothing_rather_than_everything(self):
        """筛不中就得是空——"筛选静默失效"在这个技能里等于批了不该批的。"""
        self.assertEqual(self._run(contains="根本没有这个词"), [])


class TestAuditRecord(unittest.TestCase):
    """审批是有责任归属的动作，出了事要查得到谁批的。"""

    def test_record_carries_who_what_which_and_the_outcome(self):
        import json
        import tempfile
        target = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        saved = flow.audit_path
        flow.audit_path = lambda: target
        try:
            path = flow.audit("approve", "sys_a",
                              {"task_id": "t1", "instance_id": "i1",
                               "form_title": "缺货申请", "flow_name": "审批节点"},
                              "success", comment="同意")
            self.assertTrue(path)
            with open(path, encoding="utf-8") as fh:
                rec = json.loads(fh.readline())
        finally:
            flow.audit_path = saved
        for key in ("at", "action", "operator", "task_id", "instance_id",
                    "form_title", "node", "comment", "result"):
            self.assertIn(key, rec)
        self.assertEqual((rec["action"], rec["operator"], rec["result"]),
                         ("approve", "sys_a", "success"))

    def test_unwritable_sandbox_returns_none_instead_of_blocking_the_operation(self):
        """一个可写目录都找不到时不该阻断操作，但必须**如实返回 None**，
        好让调用方说"操作已执行但没有留痕"。"""
        saved = flow.audit_path
        # 路径为什么这么造、为什么不能再写 /proc：见 tests/_fixtures.py
        flow.audit_path = lambda: unwritable_path("audit.jsonl")
        try:
            self.assertIsNone(flow.audit("approve", "u", {}, "success"))
        finally:
            flow.audit_path = saved

    def test_no_writable_dir_at_all_still_returns_none(self):
        """`audit_path()` 返回 None（哪儿都写不了）时，audit 不能拿 None 去拼路径炸掉。"""
        saved = flow.audit_path
        flow.audit_path = lambda: None
        try:
            self.assertIsNone(flow.audit("approve", "u", {}, "success"))
        finally:
            flow.audit_path = saved

    def test_readonly_jdy_home_no_longer_throws_the_log_away(self):
        """**这条是三端适配的核心。**

        `~/.jdy` 不可写（WorkBuddy 沙箱的实测事实，另外两端未知）时，
        旧代码是写死路径 + 失败即 return None —— 每一次批量审批都不留痕。
        而同一时刻会话工作目录明明可写。现在应当**换个地方写下去**。
        """
        import json
        import tempfile
        box = tempfile.mkdtemp()
        # 造"~/.jdy 写不进去"**不能用 chmod 0o500**：Windows 上 chmod 对目录不起作用，
        # 那边这个候选照样可写，于是"换个地方写下去"这条断言在 Windows 上测的是
        # 原地写成功的路径。改用普通文件当父目录（见 tests/_fixtures.py）。
        blocked = unwritable_path(".jdy")
        workdir = os.path.join(box, "session")
        os.makedirs(workdir)
        env, cwd = dict(os.environ), os.getcwd()
        os.environ.pop(platform_env.STATE_HOME_ENV, None)
        saved_default = platform_env.DEFAULT_STATE_HOME
        try:
            platform_env.DEFAULT_STATE_HOME = blocked
            os.chdir(workdir)
            platform_env.resolve_state_home(refresh=True)
            platform_env._STATE_HOME = None
            path = flow.audit("approve", "sys_a", {"task_id": "t1"}, "success")
            self.assertIsNotNone(path, "~/.jdy 不可写不等于没地方写——审批必须留痕")
            self.assertTrue(os.path.exists(path))
            # 只断言"落到了某个地方"是不够的：那样一来，哪天 blocked 其实是可写的
            # （Windows 上 chmod 造的假只读目录就是），这条测试照样绿——它测的
            # 变成了"原地写成功"，而不是"换个地方写下去"。所以钉死**落在哪**。
            # macOS 的 /var 是指向 /private/var 的软链，两边都过 realpath。
            self.assertTrue(os.path.realpath(path).startswith(os.path.realpath(workdir)),
                            "该降级到会话工作目录，实际落在 %s" % path)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.loads(fh.readline())["task_id"], "t1")
        finally:
            os.chdir(cwd)
            platform_env.DEFAULT_STATE_HOME = saved_default
            platform_env._STATE_HOME = None
            os.environ.clear()
            os.environ.update(env)


class TestNudgeGrouping(unittest.TestCase):
    """催办按**人**汇总，不按条列。

    一条一条列，群里刷屏且没人对号入座；按人汇总，每个人一眼看到
    "我有 3 条、最久等了 5 天"，这才叫催办。
    """

    def _pending(self, spec):
        return [(hours, dict(task("t%d" % i), assignee={"name": who} if who else None),
                 {"form_title": "缺货申请"})
                for i, (who, hours) in enumerate(spec)]

    def test_more_backlog_comes_first(self):
        got = nudge.by_assignee(self._pending([("张三", 30), ("李四", 50),
                                               ("张三", 40), ("张三", 10)]))
        self.assertEqual([who for who, _ in got], ["张三", "李四"])
        self.assertEqual(len(dict(got)["张三"]), 3)

    def test_longest_wait_first_within_a_person(self):
        """催办要先说最久那条——人对"等了 5 天"有反应，对"有 3 条"没有。"""
        got = dict(nudge.by_assignee(self._pending([("张三", 10), ("张三", 99),
                                                    ("张三", 50)])))
        self.assertEqual([h for h, _t, _i in got["张三"]], [99, 50, 10])

    def test_unassigned_gets_a_named_bucket_instead_of_vanishing(self):
        got = dict(nudge.by_assignee(self._pending([(None, 30)])))
        self.assertEqual(list(got), ["(未指派)"])

    def test_ties_are_ordered_deterministically(self):
        a = self._pending([("乙", 5), ("甲", 5)])
        self.assertEqual([w for w, _ in nudge.by_assignee(a)],
                         [w for w, _ in nudge.by_assignee(list(reversed(a)))])


class TestNudgeMessage(unittest.TestCase):
    """群机器人**不支持表格**，所以催办消息本来就写成一行一条。"""

    def _msg(self, spec, threshold=24.0, **kw):
        pending = [(h, dict(task("t%d" % i), assignee={"name": w}),
                    {"form_title": "缺货申请"}) for i, (w, h) in enumerate(spec)]
        return nudge.render(nudge.by_assignee(pending), threshold, "缺货申请", **kw)

    def test_says_how_many_and_how_many_people(self):
        got = self._msg([("张三", 30), ("李四", 50)])
        self.assertIn("共 2 条", got)
        self.assertIn("涉及 2 人", got)

    def test_每个人一行小结带最久等待(self):
        got = self._msg([("张三", 30), ("张三", 99)])
        self.assertIn("**张三**（2 条，最久 4.1 天）", got)

    def test_long_lists_are_truncated_per_person(self):
        """一个人二十条全列出来，消息就没法看了。"""
        got = self._msg([("张三", 30 + i) for i in range(8)], limit_per_person=3)
        self.assertIn("…另有 5 条", got)
        self.assertEqual(got.count("节点「"), 3)

    def test_no_markdown_tables(self):
        got = self._msg([("张三", 30)])
        self.assertNotIn("|", got)

    def test_no_business_content_leaks_into_the_group(self):
        """催办是发到群里的，收件人不止当事人——只说"哪个流程卡了多久"，
        不带申请标题、金额这些业务字段。"""
        pending = [(30, dict(task("t1"), assignee={"name": "张三"},
                             _content={"申请标题": "五万块的采购", "金额": 50000}),
                    {"form_title": "缺货申请"})]
        got = nudge.render(nudge.by_assignee(pending), 24.0, "缺货申请")
        self.assertNotIn("五万", got)
        self.assertNotIn("50000", got)


class TestNudgeUsesTheSharedTransport(unittest.TestCase):
    """推送实现下沉内核，是因为**不止报表要用**。

    同一份知识长在某个技能里，下一个用得上它的人就享受不到——
    本项目已经在 sync_value 之于 restore、UNVERIFIED_WRITE 之于新代码
    上踩过两次。
    """

    def test_nudge_and_report_share_one_implementation(self):
        import webhook
        src = open(os.path.join(_SCRIPTS, "nudge.py"), encoding="utf-8").read()
        self.assertIn("from webhook import", src)
        self.assertEqual(nudge.build_payload, webhook.build_payload)
        self.assertEqual(nudge.mask, webhook.mask)

    def test_the_shared_module_is_not_named_push(self):
        """jdy-report 的入口脚本就叫 push.py，同名会让 `from push import …`
        变成"谁先进 sys.path 谁赢"——实测直接撞出循环 import。"""
        self.assertFalse(os.path.exists(os.path.join(ROOT, "_shared", "push.py")))
        self.assertTrue(os.path.exists(os.path.join(ROOT, "_shared", "webhook.py")))

    def test_webhook_urls_are_masked_because_they_carry_a_secret(self):
        self.assertNotIn("abcdef0123456789xyz",
                         nudge.mask("https://qyapi.weixin.qq.com/x?key=abcdef0123456789xyz"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
