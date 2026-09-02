# -*- coding: utf-8 -*-
"""jdy-clean：数据质量度量与清洗计划。

这个技能改的是**存量数据**，写错了原值就没了。所以测试的重点不是
"该改的改没改"，而是"**不该改的有没有被改**"。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))

# 多个技能都有 plan.py / apply.py / init_config.py。把技能目录直接塞进 sys.path
# 会让 `import plan` 变成"谁先进路径谁赢"——test_sync 就因此被 test_clean 抢走过。
# 按文件路径显式加载，各自挂在唯一的模块名下。
_SCRIPTS = os.path.join(ROOT, "skills", "jdy-clean", "scripts")


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


quality = _load("jdyclean_quality", "quality.py")
clean_plan = _load("jdyclean_plan", "plan.py")
clean_label = _load("jdyclean_label", "label.py")
clean_apply = _load("jdyclean_apply", "apply.py")
restore = _load("jdyclean_restore", "restore.py")

column_profile = quality.column_profile
duplicate_groups = quality.duplicate_groups
issues_of = quality.issues_of
normalized = quality.normalized
shape = quality.shape


class TestShape(unittest.TestCase):

    def test_digits_letters_hanzi(self):
        self.assertEqual(shape("13800000000"), "9{11}")
        self.assertEqual(shape("138-0000-0000"), "9{3}-9{4}-9{4}")
        self.assertEqual(shape("张三"), "中{2}")
        self.assertEqual(shape("a@b.com"), "a@a.a{3}")

    def test_same_value_different_writing_gets_different_shape(self):
        # 这正是"格式不统一"的判据：同一列冒出多种形状
        self.assertNotEqual(shape("13800000000"), shape("138 0000 0000"))

    def test_long_value_truncated_with_marker(self):
        self.assertTrue(shape("x" * 200).endswith("…"))


class TestNormalizeIsConservative(unittest.TestCase):
    """规范化只做**不改变语义**的处理。改多了就是破坏数据。"""

    def test_trims_and_collapses_spaces(self):
        self.assertEqual(normalized("  张三  丰  "), "张三 丰")

    def test_fullwidth_alnum_becomes_halfwidth(self):
        self.assertEqual(normalized("ＡＢＣ１２３"), "ABC123")

    def test_ideographic_space_becomes_normal(self):
        self.assertEqual(normalized("中文　空格"), "中文 空格")

    def test_chinese_punctuation_is_left_alone(self):
        # 「示例：王猛」的全角冒号在中文里是正确写法。
        # 第一版把 0xFF01-0xFF5E 全转了，会把它改成「示例:王猛」——那是破坏。
        for text in ("示例：王猛", "问：答。", "他说「好」！", "甲、乙、丙"):
            self.assertEqual(normalized(text), text, text)

    def test_does_not_guess_formats(self):
        # 不补零、不改大小写、不重排日期——那些是领域规则
        self.assertEqual(normalized("2026/9/1"), "2026/9/1")
        self.assertEqual(normalized("abc"), "abc")
        self.assertEqual(normalized("007"), "007")

    def test_none_stays_none(self):
        self.assertIsNone(normalized(None))


class TestIssues(unittest.TestCase):

    def test_detects_edge_space_and_double_space(self):
        self.assertIn("首尾空白", issues_of(" x"))
        self.assertIn("连续空格", issues_of("a  b"))

    def test_detects_fullwidth_alnum_not_punctuation(self):
        self.assertIn("含全角字母数字", issues_of("ＡＢＣ"))
        self.assertEqual(issues_of("示例：王猛"), [])

    def test_clean_value_has_no_issues(self):
        self.assertEqual(issues_of("张三"), [])


class TestProfileAndDuplicates(unittest.TestCase):

    def test_fill_and_uniqueness(self):
        prof = column_profile(["a", "b", "", None, "a"])
        self.assertEqual(prof["filled"], 3)
        self.assertAlmostEqual(prof["fill_rate"], 0.6)
        self.assertEqual(prof["distinct"], 2)

    def test_empty_key_is_not_a_duplicate(self):
        # 两条都没填 ≠ 重复，那是缺失
        rows = [{"k": ""}, {"k": None}, {"k": "x"}]
        self.assertEqual(duplicate_groups(rows, lambda r: r["k"]), {})

    def test_groups_only_when_repeated(self):
        rows = [{"k": "a"}, {"k": "a"}, {"k": "b"}]
        groups = duplicate_groups(rows, lambda r: r["k"])
        self.assertEqual(list(groups), ["a"])
        self.assertEqual(len(groups["a"]), 2)


class TestPlanBuilders(unittest.TestCase):

    BY_LABEL = {"姓名": {"name": "_w1", "label": "姓名", "type": "text"},
                "备注": {"name": "_w2", "label": "备注", "type": "text"},
                "手机": {"name": "_w3", "label": "手机", "type": "text"}}

    def test_normalize_only_proposes_real_changes(self):
        rows = [{"_id": "A", "_w1": "  甲 "}, {"_id": "B", "_w1": "乙"}]
        changes = clean_plan.build_normalize(rows, self.BY_LABEL, ["姓名"])
        self.assertEqual([c["data_id"] for c in changes], ["A"])
        self.assertEqual(changes[0]["diff"]["姓名"]["to"], "甲")

    def test_normalize_skips_correct_chinese_punctuation(self):
        rows = [{"_id": "A", "_w1": "示例：王猛"}]
        self.assertEqual(clean_plan.build_normalize(rows, self.BY_LABEL, ["姓名"]), [])

    def test_dedupe_marks_every_member(self):
        rows = [{"_id": "A", "_w3": "138"}, {"_id": "B", "_w3": "138"},
                {"_id": "C", "_w3": "139"}]
        changes, detail = clean_plan.build_dedupe(rows, self.BY_LABEL, "手机",
                                                  "备注", "重复：")
        self.assertEqual(sorted(c["data_id"] for c in changes), ["A", "B"])
        self.assertEqual(detail[0]["ids"], ["A", "B"])

    def test_dedupe_appends_instead_of_overwriting(self):
        # 标记列多半是「备注」这种有内容的字段，覆盖等于替用户删掉他写的东西
        rows = [{"_id": "A", "_w3": "138", "_w2": "老客户"},
                {"_id": "B", "_w3": "138"}]
        changes, _ = clean_plan.build_dedupe(rows, self.BY_LABEL, "手机",
                                             "备注", "重复：")
        by_id = {c["data_id"]: c["diff"]["备注"]["to"] for c in changes}
        self.assertEqual(by_id["A"], "老客户 | 重复：138")
        self.assertEqual(by_id["B"], "重复：138")

    def test_dedupe_is_idempotent(self):
        # 重跑不该再追加一遍
        rows = [{"_id": "A", "_w3": "138", "_w2": "重复：138"},
                {"_id": "B", "_w3": "138", "_w2": "重复：138"}]
        changes, _ = clean_plan.build_dedupe(rows, self.BY_LABEL, "手机",
                                            "备注", "重复：")
        self.assertEqual(changes, [])


class TestLabeling(unittest.TestCase):
    """分类打标：技能只管分批与校验，判断由 Agent 做。

    这里测的是"机械部分"有没有把关住——尤其是**源字段不能被顺手改掉**。
    打标只该写标签列；源数据被改了是最难发现的一类损坏，
    因为标签看着是对的，没人会回头核对依据。
    """

    BY_LABEL = {"反馈": {"name": "_w1", "label": "反馈", "type": "textarea"},
                "标题": {"name": "_w2", "label": "标题", "type": "text"},
                "分类": {"name": "_w3", "label": "分类", "type": "text"}}

    ROWS = [{"_id": "A", "_w1": "东西坏了", "_w2": "投诉", "_w3": ""},
            {"_id": "B", "_w1": "很好用", "_w2": "表扬", "_w3": "已分类"},
            {"_id": "C", "_w1": "", "_w2": "", "_w3": ""},
            {"_id": "D", "_w1": "能加个功能吗", "_w2": "建议", "_w3": ""}]

    def _batches(self, only_empty=True, size=10):
        return clean_label.build_batches(self.ROWS, self.BY_LABEL,
                                        ["反馈", "标题"], "分类",
                                        only_empty=only_empty, size=size)

    def test_skips_already_labelled(self):
        ids = [it["data_id"] for b in self._batches() for it in b]
        self.assertNotIn("B", ids)          # 已有标签，默认不重打

    def test_skips_rows_with_no_evidence(self):
        ids = [it["data_id"] for b in self._batches() for it in b]
        self.assertNotIn("C", ids)          # 依据全空，没什么可判的

    def test_redo_includes_labelled_rows(self):
        ids = [it["data_id"] for b in self._batches(only_empty=False) for it in b]
        self.assertIn("B", ids)

    def test_batches_respect_size(self):
        batches = clean_label.build_batches(
            [{"_id": str(i), "_w1": "x", "_w2": "y", "_w3": ""} for i in range(25)],
            self.BY_LABEL, ["反馈"], "分类", only_empty=True, size=10)
        self.assertEqual([len(b) for b in batches], [10, 10, 5])

    def test_target_field_starts_empty(self):
        item = self._batches()[0][0]
        self.assertEqual(item["分类"], "")   # 留空给 Agent 填，不给默认值暗示

    def _batch_file(self, items, snapshot=None):
        return {"app_id": "APP", "entry_id": "E", "target_field": "分类",
                "_source_snapshot": snapshot or {it["data_id"]: it["source"]
                                                 for it in items},
                "items": items}

    def test_collect_takes_filled_rows(self):
        items = [{"data_id": "A", "source": {"反馈": "x"}, "分类": "投诉"}]
        updates, blank, tampered = clean_label.collect(
            self._batch_file(items), "分类")
        self.assertEqual(updates, [{"data_id": "A", "values": {"分类": "投诉"}}])
        self.assertEqual((blank, tampered), (0, []))

    def test_collect_counts_blanks(self):
        items = [{"data_id": "A", "source": {"反馈": "x"}, "分类": ""}]
        updates, blank, _ = clean_label.collect(self._batch_file(items), "分类")
        self.assertEqual((updates, blank), ([], 1))

    def test_collect_rejects_tampered_source(self):
        items = [{"data_id": "A", "source": {"反馈": "被改过了"}, "分类": "投诉"}]
        snapshot = {"A": {"反馈": "原文"}}
        updates, _, tampered = clean_label.collect(
            self._batch_file(items, snapshot), "分类")
        self.assertEqual(updates, [])
        self.assertEqual(tampered, ["A"])

    def test_collect_writes_only_the_label_column(self):
        items = [{"data_id": "A", "source": {"反馈": "x"}, "分类": "投诉"}]
        updates, _, _ = clean_label.collect(self._batch_file(items), "分类")
        self.assertEqual(list(updates[0]["values"]), ["分类"])


class TestShapeSamples(unittest.TestCase):
    """每种形状必须带一个真实样例。

    形状是抽象的。实测里 Agent 看到 `中{4}-9{2}×150` 看不出那是什么，
    另外去捞了几行值，只捞到零头，把占 87% 的测试垃圾整个漏掉，
    最后得出"这张表人都在、名字基本有"的结论——而 180/206 行是垃圾。
    """

    def test_每种形状都有样例(self):
        prof = column_profile(["闸门测试-10", "闸门测试-11", "张三"])
        for sig, _n in prof["shapes"]:
            self.assertIn(sig, prof["samples"])
            self.assertTrue(prof["samples"][sig])

    def test_样例是真实值而不是形状(self):
        prof = column_profile(["闸门测试-10"] * 5)
        sig = prof["shapes"][0][0]
        self.assertEqual(prof["samples"][sig], "闸门测试-10")
        self.assertNotEqual(prof["samples"][sig], sig)

    def test_长样例被截断(self):
        prof = column_profile(["x" * 200])
        self.assertLessEqual(len(list(prof["samples"].values())[0]), 30)

    def test_空列没有样例(self):
        prof = column_profile(["", None])
        self.assertEqual(prof["samples"], {})


class TestOneShotLabels(unittest.TestCase):
    """小批量直给标签。

    批次那一圈（导出 → 逐个填回 → 回收）是为上千行准备的。15 行也走那套
    就是纯仪式——实测中 Agent 因此两次宁可自己写脚本，于是写前备份、
    规模闸门、只写标签列、写后回读全都没了，它自己也不会知道
    哪个字段被简道云静默丢掉。所以必须给小批量一条同样安全的短路。
    """

    def _collect(self, given, known, target="分类"):
        updates, blank, unknown = [], 0, []
        for data_id, value in given.items():
            if value in (None, "", [], {}):
                blank += 1
            elif data_id not in known:
                unknown.append(data_id)
            else:
                updates.append({"data_id": data_id, "values": {target: value}})
        return updates, blank, unknown

    def test_ghost_id_is_refused(self):
        # 记录不存在还照写，就会造出幽灵更新——接口不一定拦
        u, _b, unknown = self._collect({"A": "x", "ZZZ": "y"}, {"A"})
        self.assertEqual([x["data_id"] for x in u], ["A"])
        self.assertEqual(unknown, ["ZZZ"])

    def test_blank_counted_not_written(self):
        u, blank, _ = self._collect({"A": ""}, {"A"})
        self.assertEqual((u, blank), ([], 1))

    def test_only_target_column_written(self):
        u, _, _ = self._collect({"A": "投诉"}, {"A"})
        self.assertEqual(list(u[0]["values"]), ["分类"])


class TestRestoreFromBackup(unittest.TestCase):
    """三条写入链路都会落备份，却没有任何东西读它——备份成了一种仪式。

    恢复本身也是批量写入，所以它得守住同样的规矩，尤其是这两条：
    只改**真的不一样**的行；恢复不了的必须说恢复不了。
    """

    W = {"_widget_a": {"name": "_widget_a", "label": "姓名", "type": "text"},
         "_widget_b": {"name": "_widget_b", "label": "备注", "type": "text"}}

    class FakeClient(object):
        def __init__(self, rows):
            self.rows = {r["_id"]: r for r in rows}

        def field_map(self, *a, **kw):
            w = TestRestoreFromBackup.W
            return ({v["label"]: v for v in w.values()}, dict(w))

        def fetch_rows_by_id(self, app_id, entry_id, ids):
            return {i: self.rows[i] for i in ids if i in self.rows}

    def _run(self, backup_rows, live_rows):
        client = self.FakeClient(live_rows)
        return restore.restorable(client, "APP", "E", backup_rows)

    def test_only_rows_that_actually_differ_are_restored(self):
        """一致的行不该被重写——白白扩大写入面，出事时也分不清是谁改的。"""
        backup = [{"_id": "1" * 24, "_widget_a": "张三", "_widget_b": "旧备注"},
                  {"_id": "2" * 24, "_widget_a": "李四", "_widget_b": "没动过"}]
        live = [{"_id": "1" * 24, "_widget_a": "张三", "_widget_b": "被清洗改过了"},
                {"_id": "2" * 24, "_widget_a": "李四", "_widget_b": "没动过"}]
        changed, same, gone, _ = self._run(backup, live)
        self.assertEqual([c["data_id"] for c in changed], ["1" * 24])
        self.assertEqual(same, ["2" * 24])
        self.assertEqual(gone, [])

    def test_only_the_changed_fields_are_written_back(self):
        backup = [{"_id": "1" * 24, "_widget_a": "张三", "_widget_b": "旧备注"}]
        live = [{"_id": "1" * 24, "_widget_a": "张三", "_widget_b": "新备注"}]
        changed, _, _, _ = self._run(backup, live)
        self.assertEqual(list(changed[0]["diff"]), ["备注"])       # 姓名没动就别写
        self.assertEqual(changed[0]["diff"]["备注"]["to"], "旧备注")

    def test_deleted_rows_are_reported_not_recreated(self):
        """删掉的记录恢复不了，必须说恢复不了。

        新建一条内容相同的记录看着像恢复了，但它是另一个 data_id——
        所有指向原记录的关联仍然是断的，而用户以为回滚完成了。
        """
        backup = [{"_id": "1" * 24, "_widget_a": "张三"},
                  {"_id": "9" * 24, "_widget_a": "被删了"}]
        changed, same, gone, _ = self._run(backup, [backup[0]])
        self.assertEqual(changed, [])
        self.assertEqual([g["_id"] for g in gone], ["9" * 24])

    def test_rows_added_after_the_backup_are_left_alone(self):
        """备份之后新增的记录不在备份里——恢复不该顺手删掉它们。"""
        backup = [{"_id": "1" * 24, "_widget_a": "张三"}]
        live = [{"_id": "1" * 24, "_widget_a": "张三"},
                {"_id": "8" * 24, "_widget_a": "后来加的"}]
        changed, same, gone, _ = self._run(backup, live)
        self.assertEqual((changed, gone), ([], []))
        self.assertEqual(same, ["1" * 24])

    def test_backup_file_shape_is_validated(self):
        from jdy_client import load_backup
        tmp = os.path.join(tempfile.mkdtemp(), "x.json")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"不是备份": 1}, fh)
        with self.assertRaises(ValueError):
            load_backup(tmp)

    def test_backup_name_is_the_same_shape_everywhere(self):
        """三条链路一种命名，restore 才认得出任何一个技能落下的备份。"""
        from jdy_client import backup_path
        import datetime as _dt
        got = backup_path("/tmp", "E123",
                          when=_dt.datetime(2026, 8, 31, 5, 6, 7, tzinfo=_dt.timezone.utc))
        self.assertEqual(os.path.basename(got), "backup_E123_20260831-050607.json")


class TestRestoreWritesRawValuesNotDisplayValues(unittest.TestCase):
    """回写源必须是**原始值**，不是 display_value 的产物。

    第一版拿显示值当回写源，后果分三档，一档比一档隐蔽：
      · 成员/地址 → 编码期被拒，字段根本没提交（而 dry-run 一声不吭）；
      · 部门 → 提交了裸串"研发部"，dept 要的是 dept_no，**接口回报成功、存进去是空**；
      · 结果就是"结构上永远恢复不了"，而用户以为回滚完成了。

    讽刺的是同一批改动里 sync_value() 和清洗的 NORMALIZABLE 都为这件事做过防护。
    """

    RAW_USER = {"name": "张三", "username": "sys_a"}
    RAW_ADDR = {"province": "江苏省", "city": "无锡市", "district": "锡山区", "detail": "科技园"}
    W = {"_w_t": {"name": "_w_t", "label": "备注", "type": "text"},
         "_w_u": {"name": "_w_u", "label": "负责人", "type": "user"},
         "_w_a": {"name": "_w_a", "label": "地址", "type": "address"},
         "_w_d": {"name": "_w_d", "label": "部门", "type": "dept"},
         "_w_s": {"name": "_w_s", "label": "签名", "type": "signature"}}

    class FakeClient(object):
        def __init__(self, live):
            self.live = {r["_id"]: r for r in live}

        def field_map(self, *a, **kw):
            w = TestRestoreWritesRawValuesNotDisplayValues.W
            return ({v["label"]: v for v in w.values()}, dict(w))

        def fetch_rows_by_id(self, app_id, entry_id, ids):
            return {i: self.live[i] for i in ids if i in self.live}

    def _run(self, backup, live):
        return restore.restorable(self.FakeClient(live), "A", "E", backup)

    def test_user_field_is_restored_as_username_not_as_a_display_name(self):
        backup = [{"_id": "1" * 24, "_w_u": self.RAW_USER}]
        live = [{"_id": "1" * 24, "_w_u": {"name": "李四", "username": "sys_b"}}]
        changed, _same, _gone, _un = self._run(backup, live)
        payload = changed[0]["diff"]["负责人"]["value"]
        self.assertEqual(payload, self.RAW_USER)              # 原始对象，不是"张三"
        # 走一遍真正的编码：原始值能过，显示值过不了
        from jdy_client import encode_row
        ok, skipped = encode_row({"负责人": self.W["_w_u"]}, {"负责人": payload})
        self.assertEqual(skipped, [])
        self.assertEqual(ok["_w_u"]["value"], "sys_a")
        _bad, bad_skipped = encode_row({"负责人": self.W["_w_u"]}, {"负责人": "张三"})
        self.assertEqual([s["kind"] for s in bad_skipped], ["bad_value"])

    def test_address_field_is_restored_as_an_object(self):
        backup = [{"_id": "1" * 24, "_w_a": self.RAW_ADDR}]
        live = [{"_id": "1" * 24, "_w_a": {"province": "浙江省", "city": "杭州市"}}]
        changed, _s, _g, _u = self._run(backup, live)
        self.assertEqual(changed[0]["diff"]["地址"]["value"], self.RAW_ADDR)

    def test_the_preview_still_shows_human_readable_values(self):
        """回写用原始值，但给人看的仍然是显示值——两者都要有。"""
        backup = [{"_id": "1" * 24, "_w_u": self.RAW_USER}]
        live = [{"_id": "1" * 24, "_w_u": {"name": "李四", "username": "sys_b"}}]
        changed, _s, _g, _u = self._run(backup, live)
        d = changed[0]["diff"]["负责人"]
        self.assertEqual((d["from"], d["to"]), ("李四", "张三"))

    def test_structurally_unrestorable_columns_are_declared_up_front(self):
        """部门/签名这类不能在 dry-run 之后才冒出来——那等于让人在错误前提下点了头。"""
        backup = [{"_id": "1" * 24, "_w_d": {"name": "研发部", "dept_no": 7},
                   "_w_s": {"url": "x"}, "_w_t": "旧备注"}]
        live = [{"_id": "1" * 24, "_w_d": {"name": "市场部", "dept_no": 9},
                 "_w_s": {}, "_w_t": "新备注"}]
        changed, _s, _g, unrestorable = self._run(backup, live)
        labels = {x[0] for x in unrestorable}
        self.assertEqual(labels, {"签名"})      # 签名仍未实测；部门已解锁
        # 恢复不了的那列**不能**混进要回写的 diff 里
        self.assertEqual(sorted(changed[0]["diff"]), ["备注", "部门"])

    def test_dept_is_restorable_now_that_it_has_been_measured(self):
        """2026-08-31 实测：dept 写裸 dept_no 整数可写，读回来是展开的对象。

        所以恢复它现在是**自动**成立的——回写用的就是备份里的原始对象，
        内核从中取 dept_no。这正是"一次实验、四条写路径同时受益"：
        write_probe 跑完，restore 什么都没改就多恢复一类字段。
        """
        from jdy_client import encode_row
        raw = {"name": "研发部", "dept_no": 7, "type": 0}
        data, skipped = encode_row({"部门": self.W["_w_d"]}, {"部门": raw})
        self.assertEqual(skipped, [])
        self.assertEqual(data["_w_d"]["value"], 7)          # 写的是编号，不是名字

    def test_a_bare_dept_name_is_refused_instead_of_silently_dropped(self):
        """实测：部门名写进去会被简道云静默丢弃（回报成功、字段存成 null）。

        所以编码期就要拦住它——这恰恰是 display_value 的产物会长成的样子。
        """
        from jdy_client import encode_row, display_value
        shown = display_value({"name": "研发部", "dept_no": 7}, "dept")
        self.assertEqual(shown, "研发部")
        _data, skipped = encode_row({"部门": self.W["_w_d"]}, {"部门": shown})
        self.assertEqual([s["kind"] for s in skipped], ["bad_value"])
        self.assertIn("dept_no", skipped[0]["reason"])


class TestEmptyPlanStillComposes(unittest.TestCase):
    """给了 `--plan` 就得产出文件，哪怕没东西可改。

    不产出的话，"没事可做"和"出错了"在调用方看来一模一样：都是拿不到文件，
    下一步 apply.py 甩一句"读写文件失败：No such file"——一个本该说
    "没事可做"的场景，报成了故障。全技能验收跑一遍才撞出来的。
    """

    def test_an_empty_plan_is_still_a_valid_plan(self):
        plan = clean_plan.build_plan("APP", "E", {}, [])
        self.assertEqual(plan["updates"], [])
        for key in ("app_id", "entry_id", "updates"):
            self.assertIn(key, plan)

    def test_apply_reads_the_empty_plan_instead_of_dying_on_it(self):
        """下游要能把它当成"没事可做"正常处理，而不是当成坏文件。"""
        path = os.path.join(tempfile.mkdtemp(), "empty.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(clean_plan.build_plan("APP", "E", {}, []), fh)
        got = clean_apply.load_plan(path)          # 不抛就是过
        self.assertEqual(got["updates"], [])

    def test_a_normal_plan_still_carries_the_changes(self):
        merged = {"r1": {"备注": {"from": " 甲 ", "to": "甲", "why": "首尾空白"}}}
        plan = clean_plan.build_plan("APP", "E", merged, [])
        self.assertEqual(plan["updates"], [{"data_id": "r1", "values": {"备注": "甲"}}])
        self.assertIn("r1", plan["detail"])         # 明细也要留着，供人核对


if __name__ == "__main__":
    unittest.main(verbosity=2)
