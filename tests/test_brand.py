#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""品牌署名：开关、三种 footer、以及**每一个落点**。

免费版在自己生成的文件里留一行署名。这件事有三条会静默出错的边：

  1. **落点漏了一个**——署名散在 6 个脚本里，加的时候漏一个没人看得出来；
  2. **关不掉**——`JDY_BRAND=0` 是承诺，承诺不兑现比不承诺更糟；
  3. **漏进群消息**——署名落在周报 Markdown 里，而那份文件正是推给群机器人的
     正文。加了署名却没在 push 那半摘掉，就等于把广告发进用户的工作群。
     一件事的两半只照顾了一半——本仓库反复出现的那种 bug。

所以这里对每个落点都跑一遍真的产出：开着要有 URL，`JDY_BRAND=0` 要一个字都没有。

    python3 tests/test_brand.py
"""
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))

import brand  # noqa: E402
from xlsx import read_table  # noqa: E402


def _load(name, path, extra_paths=()):
    """按路径加载一个脚本模块（技能脚本不是包，只能这么进来）。"""
    for p in extra_paths:
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scripts(skill):
    return os.path.join(ROOT, "skills", skill, "scripts")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@contextlib.contextmanager
def brand_env(value):
    """临时设置（或删除）JDY_BRAND。value 为 None 表示不设这个变量。"""
    saved = os.environ.get("JDY_BRAND")
    if value is None:
        os.environ.pop("JDY_BRAND", None)
    else:
        os.environ["JDY_BRAND"] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("JDY_BRAND", None)
        else:
            os.environ["JDY_BRAND"] = saved


chart = _load("brand_chart", os.path.join(_scripts("jdy-query"), "chart.py"),
              [_scripts("jdy-query")])
build_report = _load("brand_build_report",
                     os.path.join(_scripts("jdy-report"), "build_report.py"),
                     [_scripts("jdy-report")])
export_dict = _load("brand_export_dict",
                    os.path.join(_scripts("jdy-doc"), "export_dict.py"),
                    [_scripts("jdy-doc")])
gen = _load("brand_gen", os.path.join(_scripts("jdy-devkit"), "gen.py"),
            [_scripts("jdy-devkit")])
import_data = _load("brand_import_data",
                    os.path.join(_scripts("jdy-excel-bridge"), "import_data.py"),
                    [_scripts("jdy-excel-bridge")])
setup = _load("brand_setup", os.path.join(_scripts("hello-jdy"), "setup.py"))


class TestEnabled(unittest.TestCase):
    """开关。默认开，只有明确写下的那几个值才关。"""

    def test_default_is_on(self):
        with brand_env(None):
            self.assertTrue(brand.enabled())

    def test_off_values(self):
        for value in ("0", "false", "off", "FALSE", "Off", "OFF", "  0  "):
            with brand_env(value):
                self.assertFalse(brand.enabled(), "%r 应该关掉署名" % value)

    def test_other_values_stay_on(self):
        """拼错不该静默关掉——那样没人知道产物里为什么没有署名。"""
        for value in ("1", "true", "on", "yes", "", "no", "0.0"):
            with brand_env(value):
                self.assertTrue(brand.enabled(), "%r 不该关掉署名" % value)


class TestFooters(unittest.TestCase):
    """三种 footer：开着带 URL，关掉是空串（不是空行、不是占位符）。"""

    def test_on(self):
        with brand_env(None):
            for got in (brand.md_footer(), brand.html_footer(), brand.comment("# ")):
                self.assertIn(brand.URL, got)
            self.assertTrue(brand.html_footer().startswith("<p"))
            self.assertIn('href="%s"' % brand.URL, brand.html_footer())
            self.assertTrue(brand.comment("// ").startswith("// "))

    def test_off_is_the_empty_string(self):
        with brand_env("0"):
            self.assertEqual(brand.md_footer(), "")
            self.assertEqual(brand.html_footer(), "")
            self.assertEqual(brand.comment("# "), "")

    def test_line_carries_both_name_and_url(self):
        self.assertIn(brand.NAME, brand.LINE)
        self.assertIn(brand.URL, brand.LINE)

    def test_constants_are_pinned(self):
        """把品牌三件套的**字面值**钉在这里。

        下面每个落点的断言写的都是 `brand.URL in 产物`——URL 改坏了，
        断言和产物会一起跟着变，**测试照样全绿**。那种断言只能证明
        "接上了"，证明不了"接的是对的东西"。这条是唯一的锚点：
        把 URL 打错一个字母，从这里开始红。
        """
        self.assertEqual(brand.NAME, "aicliagent")
        self.assertEqual(brand.URL, "https://aicliagent.com")
        self.assertEqual(brand.LINE, "由 aicliagent 生成 · https://aicliagent.com")


class TestNoTelemetry(unittest.TestCase):
    """署名是**静态文字**。SECURITY.md 承诺"没有任何回传给作者的通道"。

    这条测试是那句承诺的闸门：brand.py 里出现任何网络模块就红。
    """

    NETWORKY = {"urllib", "socket", "http", "ssl", "requests", "subprocess",
                "ftplib", "smtplib", "telnetlib", "asyncio", "webbrowser"}

    def test_brand_module_imports_nothing_networky(self):
        """看 **import 语句**，不看文本。

        文本匹配会被自己的文档字符串绊倒（那句"往这个文件里加 import urllib
        就等于把承诺变成假话"），于是守卫要么误报、要么被人删掉注释了事。
        build.py 的依赖推断当年也栽在同一个坑上，这里直接用 AST。
        """
        import ast
        tree = ast.parse(_read(os.path.join(ROOT, "_shared", "brand.py")))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & self.NETWORKY, set(),
                         "brand.py 不许碰网络，却 import 了：%s"
                         % sorted(imported & self.NETWORKY))
        self.assertEqual(imported, {"os"},
                         "brand.py 只该 import os，实际：%s" % sorted(imported))


class TestQueryHtmlReport(unittest.TestCase):
    """落点：jdy-query 的自包含 HTML 报告页脚。"""

    def _page(self):
        return chart.page("测试报告", [chart.table(["列"], [["值"]])], subtitle="子标题")

    def test_on(self):
        with brand_env(None):
            html = self._page()
        self.assertIn(brand.URL, html)
        self.assertIn("</body></html>", html)
        self.assertLess(html.index(brand.URL), html.index("</body>"),
                        "署名要在正文之后、body 之内")

    def test_off(self):
        with brand_env("0"):
            html = self._page()
        self.assertNotIn(brand.URL, html)
        self.assertNotIn(brand.NAME, html)


class TestReportMarkdown(unittest.TestCase):
    """落点：jdy-report 的周报 Markdown 尾行。"""

    def _render(self):
        import datetime
        now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
        return build_report.render({"name": "测试报表"}, (now, now, now, now), [], now)

    def test_on(self):
        with brand_env(None):
            md = self._render()
        self.assertIn(brand.URL, md)
        self.assertTrue(md.rstrip().endswith("_"), "署名是最后一行")

    def test_off(self):
        with brand_env("0"):
            md = self._render()
        self.assertNotIn(brand.URL, md)
        self.assertFalse(md.endswith("\n---\n\n"), "关掉时连分隔线和空行都不该留")


class TestPushStripsTheBrandLine(unittest.TestCase):
    """**群消息正文一个字的品牌都不带。**

    周报 Markdown 里有署名，而 push.py 推的就是那份文件。两半只做一半，
    署名就会出现在用户的工作群里。
    """

    def test_footer_is_stripped_from_report_text(self):
        with brand_env(None):
            md = build_report.render(
                {"name": "T"}, self._period(), [], self._period()[0])
        self.assertIn(brand.URL, md)
        stripped = brand.strip_md_footer(md)
        self.assertNotIn(brand.URL, stripped)
        self.assertNotIn(brand.NAME, stripped)
        self.assertIn("统计区间", stripped)          # 正文一个字都不能被削掉

    def test_strip_works_even_when_the_switch_is_now_off(self):
        """报表可能是开着署名生成的，推送时环境变量已经不同了。"""
        with brand_env(None):
            md = build_report.render(
                {"name": "T"}, self._period(), [], self._period()[0])
        with brand_env("0"):
            self.assertNotIn(brand.URL, brand.strip_md_footer(md))

    def test_strip_leaves_unbranded_text_alone(self):
        text = "# 标题\n\n正文\n"
        self.assertEqual(brand.strip_md_footer(text), text)

    def test_push_script_actually_calls_it(self):
        """光有函数不算数——push.py 得真的调它。"""
        src = _read(os.path.join(_scripts("jdy-report"), "push.py"))
        self.assertIn("brand.strip_md_footer(content)", src)
        self.assertLess(src.index("brand.strip_md_footer(content)"),
                        src.index("build_payload("),
                        "要在拼消息体之前摘掉，否则预览和真发各摘各的")

    def _period(self):
        import datetime
        now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
        return (now, now, now, now)


class TestWebhookBodyStaysClean(unittest.TestCase):
    """推送内核和另外两个推送方一个品牌字都不许有。"""

    def test_push_senders_do_not_embed_the_brand(self):
        for rel in ("_shared/webhook.py",
                    "skills/jdy-watch/scripts/watch.py",
                    "skills/jdy-flow-ops/scripts/nudge.py"):
            path = os.path.join(ROOT, rel)
            if not os.path.exists(path):
                continue
            self.assertNotIn(brand.NAME, _read(path), "%s 不该带品牌" % rel)


class TestDocDictionary(unittest.TestCase):
    """落点：jdy-doc 的数据字典 Markdown。"""

    FORMS = [{"name": "表单甲", "entry_id": "e1", "field_count": 1,
              "widgets": [{"label": "姓名", "name": "_w1", "type": "text"}]}]

    def test_on(self):
        with brand_env(None):
            md = export_dict.render("测试应用", "a1", self.FORMS)
        self.assertIn(brand.URL, md)

    def test_off(self):
        with brand_env("0"):
            md = export_dict.render("测试应用", "a1", self.FORMS)
        self.assertNotIn(brand.URL, md)
        self.assertNotIn(brand.NAME, md)


class TestDevkitGenerated(unittest.TestCase):
    """落点：jdy-devkit 生成的 curl / Python 样例与校验函数的头注释。"""

    ROWS = [{"label": "姓名", "name": "_widget_1", "type": "text",
             "writable": True, "example": '"张三"', "note": ""},
            {"label": "编号", "name": "_widget_6", "type": "sn",
             "writable": False, "example": "", "note": "系统生成"}]

    def _all(self):
        return {"sample.sh": gen.render_curl("APP", "ENTRY", self.ROWS),
                "sample.py": gen.render_python("APP", "ENTRY", self.ROWS),
                "validate.py": gen.render_validator(self.ROWS)}

    def test_on(self):
        with brand_env(None):
            for name, src in self._all().items():
                self.assertIn(brand.URL, src, "%s 少了署名" % name)
                head = src.splitlines()[:3]
                self.assertTrue(any(brand.URL in l for l in head),
                                "%s 的署名要在头注释里，实际在：%r" % (name, head))
                self.assertTrue(any(l.strip().startswith("#") and brand.URL in l
                                    for l in head),
                                "%s 的署名必须是注释行" % name)

    def test_generated_python_still_parses_with_the_brand_line(self):
        """生成物是**要能跑的代码**——加一行注释不能把它弄坏。"""
        import ast
        with brand_env(None):
            ast.parse(gen.render_python("APP", "ENTRY", self.ROWS))
            ast.parse(gen.render_validator(self.ROWS))
        with brand_env("0"):
            ast.parse(gen.render_python("APP", "ENTRY", self.ROWS))
            ast.parse(gen.render_validator(self.ROWS))

    def test_off(self):
        with brand_env("0"):
            for name, src in self._all().items():
                self.assertNotIn(brand.URL, src, "%s 关不掉署名" % name)
                self.assertNotIn(brand.NAME, src)
                self.assertNotIn("\n\n#!", src)      # 不留多余空行


class TestExcelFixSheet(unittest.TestCase):
    """落点：jdy-excel-bridge 的修复建议表。

    署名不能变成"多一条待修复"——那张表是拿去逐条改数据的。
    """

    PLAN = {"warnings": [], "blocked_columns": [],
            "issues": [{"row": 2, "column": "数字", "value": "约一百",
                        "detail": "无法解析为数字", "kind": "bad_value"}]}

    def _write(self):
        path = os.path.join(tempfile.mkdtemp(), "fix.xlsx")
        count = import_data.write_fix_sheet(path, self.PLAN, None)
        return path, count

    def test_on(self):
        with brand_env(None):
            path, count = self._write()
        self.assertEqual(count, 1, "署名行不该被算成一条待修复")
        _headers, rows = read_table(path)
        self.assertEqual(rows[0]["列"], "数字")        # 问题行仍在最前面
        blob = "\n".join("".join(str(v) for v in r.values()) for r in rows)
        self.assertIn(brand.URL, blob)

    def test_off(self):
        with brand_env("0"):
            path, count = self._write()
        self.assertEqual(count, 1)
        _headers, rows = read_table(path)
        blob = "\n".join("".join(str(v) for v in r.values()) for r in rows)
        self.assertNotIn(brand.URL, blob)
        self.assertNotIn(brand.NAME, blob)

    def test_nothing_to_fix_still_writes_no_file(self):
        """没问题就不该为了署名而凭空生出一张表。"""
        path = os.path.join(tempfile.mkdtemp(), "fix.xlsx")
        with brand_env(None):
            self.assertIsNone(import_data.write_fix_sheet(
                path, {"warnings": [], "issues": [], "blocked_columns": []}, None))
        self.assertFalse(os.path.exists(path))


class TestHelloJdySetup(unittest.TestCase):
    """落点：配好 Key 之后那句收尾。

    hello-jdy 刻意零依赖（`tests/test_cli_contract.py` 守着"包里不许有内核"），
    所以这两个常量是**抄写**的。抄写就得有人盯着两边一致——这里就是那个人。
    """

    def test_constants_match_the_single_source(self):
        self.assertEqual(setup.BRAND_LINE, brand.LINE,
                         "hello-jdy 抄的署名和 _shared/brand.py 不一致了")
        self.assertEqual(tuple(setup.BRAND_OFF_VALUES), tuple(brand.OFF_VALUES))

    def test_switch_behaves_the_same(self):
        for value in (None, "0", "false", "OFF", "1", "true"):
            with brand_env(value):
                self.assertEqual(setup.brand_enabled(), brand.enabled(),
                                 "JDY_BRAND=%r 两边判断不一致" % value)

    def _run_stdin_setup(self):
        """跑真正那条分支：验证通过 → 落盘 → 收尾输出。外部动作打桩。"""
        saved = (setup.verify, setup.write_config, sys.stdin, sys.argv)
        setup.verify = lambda key: (True, "可用")
        setup.write_config = lambda key: "/tmp/fake/config.json"
        sys.stdin = io.StringIO("KEY\n")
        sys.argv = ["setup.py", "--stdin"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = setup.main()
        finally:
            setup.verify, setup.write_config, sys.stdin, sys.argv = saved
        return rc, buf.getvalue()

    def test_on(self):
        with brand_env(None):
            rc, out = self._run_stdin_setup()
        self.assertEqual(rc, 0)
        self.assertIn("这台机器上的简道云技能现在都能用了。", out)
        self.assertIn(brand.URL, out)

    def test_off(self):
        with brand_env("0"):
            rc, out = self._run_stdin_setup()
        self.assertEqual(rc, 0)
        self.assertIn("这台机器上的简道云技能现在都能用了。", out)
        self.assertNotIn(brand.URL, out)
        self.assertNotIn(brand.NAME, out)


class TestInstallerTail(unittest.TestCase):
    """落点：install.py 安装成功的收尾输出。"""

    def _run_install(self):
        """跑 main() 的安装分支，但把真正往磁盘写的那一步打桩掉。"""
        sys.path.insert(0, ROOT)
        import install as installer
        saved_install, saved_argv = installer.install, sys.argv
        installer.install = lambda *a, **k: None
        sys.argv = ["install.py", "hello-jdy"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = installer.main()
        finally:
            installer.install, sys.argv = saved_install, saved_argv
        return rc, buf.getvalue()

    def test_on(self):
        with brand_env(None):
            rc, out = self._run_install()
        self.assertEqual(rc, 0)
        self.assertIn("安装 1 个技能", out)
        self.assertIn(brand.URL, out)

    def test_off(self):
        with brand_env("0"):
            rc, out = self._run_install()
        self.assertEqual(rc, 0)
        self.assertIn("安装 1 个技能", out)
        self.assertNotIn(brand.URL, out)
        self.assertNotIn(brand.NAME, out)


class TestSkillMdIsUntouched(unittest.TestCase):
    """**SKILL.md 里不许出现品牌——只有 `author:` 那一行例外。**

    frontmatter 的 description 是 Agent 的触发依据和商店卡片文案；正文是给
    Agent 读的操作指令。往里塞品牌等于让 Agent 替我念广告，还会污染触发。

    唯一的例外是 frontmatter 的 `author:`：WorkBuddy 开放平台「技能」渠道
    把它列为必填，审核方按它认合作方，填别的名字等于把作者写错。
    它是**结构化元数据**，不进 description、不进正文，Agent 不会照着念。
    所以这条测试从"整个文件不许出现"收窄成"**除 author 那一行外**不许出现"，
    并额外钉住两件事：author 的值必须**正好**是品牌名（不是"含有"），
    以及品牌名在整个文件里只出现这一次——多出一处就红。
    """

    AUTHOR_LINE = re.compile(r"^author:\s*(\S.*?)\s*$", re.M)

    def test_no_skill_md_mentions_the_brand_outside_the_author_field(self):
        bad = []
        for skill in sorted(os.listdir(os.path.join(ROOT, "skills"))):
            md = os.path.join(ROOT, "skills", skill, "SKILL.md")
            if not os.path.isfile(md):
                continue
            rest = self.AUTHOR_LINE.sub("", _read(md))
            if brand.NAME in rest:
                bad.append(skill)
        self.assertEqual(bad, [], "这些 SKILL.md 在 author 之外出现了品牌：%s" % bad)

    def test_author_field_is_exactly_the_brand_and_appears_once(self):
        """例外只开这么大：一处、一行、值正好是品牌名。"""
        for skill in sorted(os.listdir(os.path.join(ROOT, "skills"))):
            md = os.path.join(ROOT, "skills", skill, "SKILL.md")
            if not os.path.isfile(md):
                continue
            text = _read(md)
            authors = self.AUTHOR_LINE.findall(text)
            self.assertEqual(authors, [brand.NAME],
                             "%s 的 author 行应恰好一条且值为 %r，实际 %r"
                             % (skill, brand.NAME, authors))
            self.assertEqual(text.count(brand.NAME), 1,
                             "%s 里品牌出现了 %d 次，只许 author 那一次"
                             % (skill, text.count(brand.NAME)))


class TestDocsPromiseTheSwitch(unittest.TestCase):
    """承诺写在 README/SECURITY 里，就得真的写着。"""

    def test_readme_documents_the_off_switch(self):
        readme = _read(os.path.join(ROOT, "README.md"))
        self.assertIn("JDY_BRAND=0", readme)
        self.assertIn(brand.URL, readme)

    def test_security_says_the_line_is_static(self):
        sec = _read(os.path.join(ROOT, "SECURITY.md"))
        self.assertIn("JDY_BRAND=0", sec)
        self.assertIn("没有任何回传给作者的通道", sec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
