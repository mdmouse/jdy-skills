# -*- coding: utf-8 -*-
"""脚本入口的硬性约定。

来历：一次全量自查里跑了每个脚本的每个开关，发现 5 处直接甩 Python traceback
（health_check 不认应用名、report init_config 同样、--now 给 ISO 时间戳、
坏 YAML、坏 xlsx）。命令行工具甩 traceback 永远是错的输出——用户读不出该改什么，
Agent 看到它往往会绕开技能自己造轮子。

这里用静态检查把约定钉死，不依赖我下次还记得。
"""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))

# probe.py 是平台探针，刻意零依赖：它要在内核还没落地的平台上也能跑
STANDALONE = {"probe.py", "setup.py"}    # hello-jdy 的两个入口刻意零依赖：
                                         # 它们跑在"还不确定这台机器能不能用内核"的时候

from jdy_client import WRITE_PATH  # noqa: E402


def write_skills():
    """**从代码里数出**哪些技能会写数据，而不是手工列一份名单。

    来历：这份名单原来是手写的元组，二期新增的 jdy-org 忘了加进去——
    于是本文件四个把关测试对它全部失效：整个"对 Agent 的约束"层缺位，
    而它恰恰是唯一会花钱、动的是整个企业组织架构的写入技能。
    名单和现实分叉的老毛病，只能靠不留名单来治。

    判据两条，任一即算写入技能：
      · 调用了内核的写入方法（client.update / batch_create / batch_update）；
      · post() 的第一个参数就是一条写接口路径字面量（通讯录是这么写的）；
      · 模块级常量里有一条写接口路径（流程技能的 approve/reject 是这么写的，
        路径存在自己的常量里，扫调用点扫不到）。
      文档字符串里提到的路径不算——只看模块级赋值。
    """
    out = []
    for skill in sorted(os.listdir(os.path.join(ROOT, "skills"))):
        sd = os.path.join(ROOT, "skills", skill, "scripts")
        if not os.path.isdir(sd):
            continue
        for name in sorted(os.listdir(sd)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(sd, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), name)
            hit = False
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("update", "batch_create", "batch_update")
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in ("client", "self")):
                    hit = True
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "post" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and WRITE_PATH.search(node.args[0].value)):
                    hit = True          # 通讯录就是这么写的：post() 里直接一条字面路径
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                for sub in ast.walk(node.value):
                    if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                            and WRITE_PATH.search(sub.value)):
                        hit = True
            if hit:
                out.append(skill)
                break
    return tuple(out)


# 派生只做一次。**两个测试类共用同一份**——原来两处各写一行
# `WRITE_SKILLS = write_skills()`，看着无害，实际把 mutate.py 的锚点变成了
# 不唯一：第 47 条变异（把派生名单退回手工维护）从此退不回去、被跳过，
# 而那条变异守的正是"名单和现实分叉"这个老毛病。
# 加一个类的时候顺手废掉了一条变异检查——这就是本仓库反复出现的那种半边改动。
DERIVED_WRITE_SKILLS = write_skills()


def _cli_scripts():
    for d in sorted(os.listdir(os.path.join(ROOT, "skills"))):
        sd = os.path.join(ROOT, "skills", d, "scripts")
        if not os.path.isdir(sd):
            continue
        for name in sorted(os.listdir(sd)):
            if not name.endswith(".py") or name == "_bootstrap.py":
                continue
            path = os.path.join(sd, name)
            src = open(path, encoding="utf-8").read()
            if "if __name__" in src and "sys.exit(" in src:
                yield d, name, path, src


class TestCliEntrypoints(unittest.TestCase):

    def test_every_cli_wraps_main_in_cli_main(self):
        bad = []
        for skill, name, _path, src in _cli_scripts():
            if name in STANDALONE:
                continue
            if "sys.exit(cli_main(main))" not in src:
                bad.append("%s/%s" % (skill, name))
        self.assertEqual(bad, [], "这些脚本没走 cli_main，异常会变成 traceback：%s" % bad)

    def test_standalone_probe_stays_dependency_free(self):
        path = os.path.join(ROOT, "skills", "hello-jdy", "scripts", "probe.py")
        src = open(path, encoding="utf-8").read()
        self.assertNotIn("from jdy_client", src,
                         "探针必须零依赖——它跑在还不确定能不能用内核的平台上")

    def test_probe_package_carries_no_vendored_kernel(self):
        """光看源码不够——**包里有没有内核**才是结论。

        来历：probe.py 的注释里写了一句"这张表是 _shared/platform_env.py 的副本"，
        而 build.py 当时是正则搜模块名，于是把内核 vendor 进了这个刻意零依赖的包。
        源码一行没变，包却胖了——而这个包恰恰是用来判定"该端能不能跑技能"的，
        里面多一个内核就把结论污染了。build.py 已改成看 import 语句，这里守住结果。
        """
        vendored = os.path.join(ROOT, "skills", "hello-jdy", "scripts", "_shared")
        self.assertFalse(os.path.exists(vendored),
                         "hello-jdy 包里出现了 vendor 进来的内核：%s" % vendored)

    def test_custom_exceptions_are_catchable(self):
        """自定义异常必须落在 cli_main 接得住的族里（ValueError 或 JdyError）。

        原先 5 个都直接继承 Exception，于是每一个都能穿透到顶层变成 traceback。
        """
        bad = []
        for base_dir in ("_shared", "skills"):
            for root, _dirs, files in os.walk(os.path.join(ROOT, base_dir)):
                if "%s_shared" % os.sep in root and base_dir == "skills":
                    continue                       # build.py 拷进去的副本
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(root, name)
                    tree = ast.parse(open(path, encoding="utf-8").read())
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.ClassDef):
                            continue
                        if not node.name.endswith("Error"):
                            continue
                        if node.name == "JdyError":
                            continue        # cli_main 显式接住它，它就是那一族的根
                        bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
                        if bases == ["Exception"]:
                            bad.append("%s: %s" % (os.path.relpath(path, ROOT), node.name))
        self.assertEqual(bad, [],
                         "这些异常直接继承 Exception，会穿透 cli_main 变成 traceback：\n"
                         + "\n".join(bad))

    def test_json_output_option_named_consistently(self):
        """「另存结构化结果到文件」这个开关，各技能里必须都叫 --json-out。

        `--json` 作为布尔开关（把 JSON 打到 stdout）是另一件事，不在此列——
        判据是它收不收一个路径参数。
        """
        bad = []
        for skill, name, path, _src in _cli_scripts():
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", None) == "add_argument"):
                    continue
                names = [a.value for a in node.args if isinstance(a, ast.Constant)]
                if "--json" not in names or "--json-out" in names:
                    continue
                is_flag = any(k.arg == "action" for k in node.keywords)
                if not is_flag:
                    bad.append("%s/%s" % (skill, name))
        self.assertEqual(bad, [], "收路径的那个开关统一叫 --json-out：%s" % bad)


class TestIrreversibleActionsGuarded(unittest.TestCase):
    """能造成不可逆对外影响的脚本，非交互环境下没有 --yes 必须拒绝执行。

    「问不了用户」不等于「默认同意」。这四条路各自的后果：
      import_data 写业务数据、apply 改目标应用、act 走审批、push 发进群。
    push 是最晚被纳进来的——实测中 Agent 为了验连通性，绕开脚本直接
    POST 了一条「连通性探测」进群。它读了文档，但文档把规矩写成了
    「加 --send 之前要确认」，于是它理解成那是关于参数的规矩，
    自己写个请求就不算。现在文档改成「关于那个 webhook」，
    并给了 --check 这个不发消息也能验通的出口。
    """

    OUTWARD = {
        "jdy-excel-bridge/import_data.py": "写入简道云业务数据",
        "jdy-sync/apply.py": "改动目标应用的数据",
        "jdy-flow-ops/act.py": "执行审批动作",
        "jdy-report/push.py": "把消息发进群",
    }

    def _src(self, rel):
        skill, name = rel.split("/")
        with open(os.path.join(ROOT, "skills", skill, "scripts", name),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_all_have_yes_flag(self):
        missing = [r for r in self.OUTWARD if '"--yes"' not in self._src(r)]
        self.assertEqual(missing, [], "这些对外动作没有 --yes 闸门：%s" % missing)

    def test_all_refuse_when_non_interactive(self):
        """判据是**走没走内核的 ask_yes**，不再是源码里有没有 `isatty()`。

        原来盯 `isatty()` 恰恰盯错了地方：手写 `if sys.stdin.isatty(): input(...)`
        在 Windows 上是**假的闸门**——NUL 是字符设备，isatty() 返回 True，
        input() 第一次读就 EOFError，脚本以退出码 1 带着 traceback 死掉，
        说好的 4 根本到不了调用方。而源码里 `isatty()` 三个字一直都在，
        这条测试从头到尾都是绿的。判据换成 ask_yes：能不能问、问没问到，
        由内核那一处统一回答，None 一律落到「拒绝(4)」。
        """
        bad = []
        for rel in self.OUTWARD:
            src = self._src(rel)
            if "ask_yes(" not in src or "return 4" not in src:
                bad.append(rel)
        self.assertEqual(bad, [],
                         "这些脚本在非交互环境下没有走「拒绝执行(4)」这条路：%s" % bad)

    def test_none_from_ask_yes_is_never_treated_as_consent(self):
        """`ask_yes(...) is None` 这一支必须紧跟着拒绝，不能只判真假。

        `if ask_yes(...):` 写法下 None 和 False 混成一路：非交互环境会被当成
        「用户说了不」而静静返回 0——对外动作看着像"没做"，实际是闸门塌了
        之后的偶然结果。要的是**明说拒绝、退 4**。
        """
        bad = [rel for rel in self.OUTWARD if "is None" not in self._src(rel)]
        self.assertEqual(bad, [],
                         "这些脚本没有单独处理 ask_yes 的 None（问不了）：%s" % bad)

    def test_push_offers_a_no_message_connectivity_check(self):
        # 不给"不发消息也能验通"的出口，调用方就会自己发一条探测进群
        self.assertIn('"--check"', self._src("jdy-report/push.py"))


class TestColumnWidthUsesDisplayWidth(unittest.TestCase):
    """对齐列宽必须按显示宽度算，不能用 len()。

    中文占两个显示列。`max(len(标签))` 算出来的宽度会让中文行比英文行短，
    整张表是歪的。我栽过两次：一次是 --list 的表单清单（Agent 因此读串了行、
    把 A 表的行数安到 B 表头上），一次是 jdy-clean 的填充率表。
    内核提供 col_width()，这条测试盯住别人别再手写。
    """

    def test_no_hand_rolled_len_widths(self):
        bad = []
        for skill, name, path, src in _cli_scripts():
            if "pad(" not in src:
                continue
            for lineno, line in enumerate(src.split("\n"), 1):
                if "max(" in line and "len(" in line and "width" in line:
                    bad.append("%s/%s:%d  %s" % (skill, name, lineno, line.strip()))
        self.assertEqual(bad, [],
                         "对齐宽度请用内核的 col_width()，不要手写 max(len(...))：\n"
                         + "\n".join(bad))

    def test_col_width_counts_cjk_as_two(self):
        import jdy_client as jc
        self.assertEqual(jc.col_width(["ab"]), 2)
        self.assertEqual(jc.col_width(["中文"]), 4)
        self.assertEqual(jc.col_width(["ab", "中文"]), 4)
        self.assertEqual(jc.col_width([], 6), 6)


class TestCleanNeverDeletes(unittest.TestCase):
    """jdy-clean 不能有任何删除路径。

    去重最自然的实现就是"留一条删其余"，而那正是最危险的一步：
    重复不等于错误——同名两人、同号两单都可能是真的，删错了不可逆。
    这条测试保证以后没人图省事把删除加回来。
    """

    def _sources(self):
        base = os.path.join(ROOT, "skills", "jdy-clean", "scripts")
        for name in sorted(os.listdir(base)):
            if name.endswith(".py") and name != "_bootstrap.py":
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    yield name, fh.read()

    def test_no_delete_endpoint_referenced(self):
        bad = [n for n, src in self._sources() if "data/delete" in src]
        self.assertEqual(bad, [], "jdy-clean 出现了删除接口：%s" % bad)

    def test_skill_md_states_the_rule(self):
        with open(os.path.join(ROOT, "skills", "jdy-clean", "SKILL.md"),
                  encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("不删", md, "SKILL.md 必须写明本技能不删数据")

    def test_dedupe_requires_a_mark_field(self):
        # 没有标记列就无处安放结论，只剩"删"这一条路——所以必须强制要求
        src = dict(self._sources())["plan.py"]
        self.assertIn("--mark-field", src)


class TestTriggersReachTheDescription(unittest.TestCase):
    """每个脚本自报的触发词，必须出现在所在技能的 description 里。

    实测代价：jdy-clean 的 label.py（分类打标）做完了、SKILL.md 正文也写了，
    但触发词没进 description——里面全是「清洗数据、格式不统一、查重」这类。
    结果 Agent 面对「帮我按职务打个分类标签」时**完全没触发这个技能**，
    自己从零写了脚本：我的源字段防篡改、只写标签列、规模闸门、
    统一的备份与回读，一个都没生效。

    同样的错之前在 jdy-report 的趋势功能上犯过一次。
    造了能力却没放到 Agent 会看见的地方，等于没造。
    """

    def _skills(self):
        base = os.path.join(ROOT, "skills")
        for skill in sorted(os.listdir(base)):
            sd = os.path.join(base, skill, "scripts")
            md = os.path.join(base, skill, "SKILL.md")
            if os.path.isdir(sd) and os.path.isfile(md):
                yield skill, sd, open(md, encoding="utf-8").read()

    @staticmethod
    def _triggers(path):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "TRIGGERS":
                    v = node.value
                    if isinstance(v, ast.Constant):
                        # 单个元素忘了写逗号就成了裸字符串，不是元组——
                        # 收下但当成一个词，别在这里炸掉
                        return [v.value]
                    return [e.value for e in getattr(v, "elts", [])
                            if isinstance(e, ast.Constant)]
        return None

    def test_every_declared_trigger_is_in_the_description(self):
        missing = []
        for skill, sd, md in self._skills():
            desc = md.split("---")[1] if md.startswith("---") else md
            for name in sorted(os.listdir(sd)):
                if not name.endswith(".py") or name == "_bootstrap.py":
                    continue
                words = self._triggers(os.path.join(sd, name))
                if not words:
                    continue
                for w in words:
                    if w not in desc:
                        missing.append("%s/%s：「%s」不在 description 里" %
                                       (skill, name, w))
        self.assertEqual(missing, [], "\n" + "\n".join(missing))

    def test_cli_scripts_declare_triggers(self):
        """有 CLI 的脚本都得自报触发词——不然上一条测试形同虚设。"""
        naked = []
        for skill, sd, _md in self._skills():
            for name in sorted(os.listdir(sd)):
                if not name.endswith(".py") or name == "_bootstrap.py":
                    continue
                with open(os.path.join(sd, name), encoding="utf-8") as fh:
                    src = fh.read()
                if "argparse" not in src or "add_argument" not in src:
                    continue          # 库模块，不是入口
                if skill == "hello-jdy":
                    continue          # 探针只有一个入口，触发词就是技能名本身
                if self._triggers(os.path.join(sd, name)) is None:
                    naked.append("%s/%s" % (skill, name))
        self.assertEqual(naked, [], "这些脚本没声明 TRIGGERS：%s" % naked)


class TestWriteRulesAreStatedAsBinding(unittest.TestCase):
    """会写数据的技能，SKILL.md 必须把安全规则写成**对 Agent 的约束**。

    来历：这些规则原本只活在脚本里——用了脚本才生效。而实测中 Agent 四次
    都绕开脚本自己写代码，其中一次一口气改了 180 条**全程没问过用户**。
    走 apply.py 会被规模闸门拦下，自己写就没人拦。

    所以规则不能只写成"本工具会如何"，得写成"你必须如何"，
    不管它最后用不用我的脚本。
    """

    WRITE_SKILLS = DERIVED_WRITE_SKILLS

    def test_the_derivation_itself_still_finds_things(self):
        """派生逻辑坏掉时会返回空元组，那时下面四个测试会**全部空转变绿**。

        这正是本轮复查点名的那种测试：看起来在把关，实际上取反了也全绿。
        所以先把派生本身钉住——已知的写入技能一个都不能少。
        """
        self.assertGreaterEqual(len(self.WRITE_SKILLS), 5, self.WRITE_SKILLS)
        for known in ("jdy-clean", "jdy-excel-bridge", "jdy-sync",
                      "jdy-flow-ops", "jdy-org"):
            self.assertIn(known, self.WRITE_SKILLS)
        for readonly in ("jdy-query", "jdy-doc", "jdy-watch", "jdy-report"):
            self.assertNotIn(readonly, self.WRITE_SKILLS)

    def _md(self, skill):
        with open(os.path.join(ROOT, "skills", skill, "SKILL.md"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_each_write_skill_states_the_rules(self):
        missing = [s for s in self.WRITE_SKILLS if "不可绕过的规则" not in self._md(s)]
        self.assertEqual(missing, [], "这些技能没写明不可绕过的规则：%s" % missing)

    def test_rules_cover_the_four_invariants(self):
        need = ["取得同意", "备份", "回读", "绝不删除数据"]
        bad = []
        for skill in self.WRITE_SKILLS:
            md = self._md(skill)
            for kw in need:
                if kw not in md:
                    bad.append("%s 缺「%s」" % (skill, kw))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_no_delete_rule_survives_an_imperative_user(self):
        """实测破防点：诊断类问法守得住规则，「把重复的处理掉」这种祈使句守不住。

        90 轮 GUI 实测里 jdy-clean 10 轮破了 1 轮——Agent 自己写了个 delete.py，
        还把「删除重复行」摆成选项第一项，用户一点就删了 2 条。所以规则必须
        点名这两件事：用户说要删也不算授权，且不许摆删除选项。
        """
        for skill in self.WRITE_SKILLS:
            md = self._md(skill)
            self.assertIn("不构成删除授权", md, "%s 没写明「用户说要删也不算授权」" % skill)
            self.assertIn("不要摆一个「删除」选项", md, "%s 没禁止摆删除选项" % skill)

    def test_rules_apply_regardless_of_tooling(self):
        # 关键在这句：规则约束的是 Agent，不是"用了这个脚本才生效"
        for skill in self.WRITE_SKILLS:
            self.assertIn("不管你用什么方式写入", self._md(skill), skill)


class TestWriteSkillsDrawTheLineAgainstTheOfficialConnector(unittest.TestCase):
    """双轨并存后，Agent 手边多了一条「官方的、看起来完全正当」的写入路径。

    来历：官方 2026-09-01 把 MCP 服务改名「AI 连接」，工具从 4 项扩到 12 项，
    并【官方声称】上架 WorkBuddy／千问办公连接器市场（doc/26886）。
    写侧仍只有 `member_data_create` 一条单条新增——**但单条新增循环起来就是批量导入**，
    而它与 REST 写接口同源：脏值静默存 null，照样一条条返回成功。
    循环跑完，Agent 看到 N 个「成功」，用户表里那几列是空的。

    上一条测试（TestWriteRulesAreStatedAsBinding）守的是「Agent 自己写代码」那条绕道；
    这条守的是新开的这条——**用官方工具绕比自己写代码更像正当操作**，
    预检、备份、回读、规模闸门、写入白名单一道都不经过，而每一步失败都是静默的。

    本仓库已有实证：能力只写在 references/ 里 Agent 读不到，写进 SKILL.md 正文它才照做。
    所以这段分工必须在**正文**，不能只在 frontmatter 的 description 里躺着。
    """

    WRITE_SKILLS = DERIVED_WRITE_SKILLS
    MARK = "与官方简道云 AI 连接的分工"

    def _body(self, skill):
        """只取正文。frontmatter 里写一句不算——那层是给检索用的，不是给 Agent 读的。"""
        with open(os.path.join(ROOT, "skills", skill, "SKILL.md"),
                  encoding="utf-8") as fh:
            md = fh.read()
        parts = md.split("---\n")
        return "---\n".join(parts[2:]) if len(parts) > 2 else md

    def _section(self, skill):
        return self._body(skill).split(self.MARK, 1)[-1]

    def test_every_write_skill_has_the_section(self):
        missing = [s for s in self.WRITE_SKILLS if self.MARK not in self._body(s)]
        self.assertEqual(missing, [],
                         "这些写入技能的正文里没有与官方 AI 连接的分工段：%s" % missing)

    def test_read_only_questions_are_explicitly_allowed(self):
        """防线不能写成「官方连接器一律别用」——那是假的，用户装了它就是要用。

        分工要立得住，得先认下它该承担的那一半：查数问答走它没问题。
        只禁不让，Agent 下次照样自己判断该听谁的，而它的判断没有护栏。
        """
        bad = []
        for skill in self.WRITE_SKILLS:
            body = self._body(skill)
            if "查数" not in body or "官方连接器" not in body:
                bad.append(skill)
        self.assertEqual(bad, [],
                         "这些技能没写明「查数问答可以走官方连接器」：%s" % bad)

    def test_writes_are_reserved_for_this_skill(self):
        bad = [s for s in self.WRITE_SKILLS
               if "必须走本技能的脚本" not in self._section(s)]
        self.assertEqual(bad, [], "这些技能没把写入收回到脚本里：%s" % bad)

    def test_the_single_create_loop_is_named_and_forbidden(self):
        """必须**点名** member_data_create。

        写成「不要用官方工具做批量」拦不住：Agent 手里那个工具不叫「批量」，
        它叫「新增数据」，一条一条来看着完全合规。只有点名到工具本身、
        再说清「循环起来就是批量」，禁令才落在它真会做的那个动作上。
        """
        bad = []
        for skill in self.WRITE_SKILLS:
            # 三个词各自出现在段里**不算**——理由段里本来就会提到
            # `member_data_create` 和"循环"。要求它们落在**同一句禁令**上：
            # 变异检查第 60 条把禁令换成泛化的"别用官方工具做批量"时，
            # 松版断言照样全绿（实测过），那就等于没测。
            if not any("禁止" in line and "member_data_create" in line and "循环" in line
                       for line in self._section(skill).split("\n")):
                bad.append(skill)
        self.assertEqual(bad, [],
                         "这些技能没在同一句里点名禁止 member_data_create 循环：%s" % bad)

    def test_all_three_reasons_survive(self):
        """三条理由缺一条，禁令就退化成「因为我说了算」。

        Agent 权衡的是代价：不知道会静默失败，它觉得两条路一样；
        不知道没有备份，它觉得错了还能改；不知道写权限要管理员审核，
        它就预料不到写到一半被拒——那比全失败更难收拾。
        三条各自封住一种「那我试试」，所以一条都不许省。
        """
        need = ("静默", "回读", "备份", "管理员审核")
        bad = []
        for skill in self.WRITE_SKILLS:
            section = self._section(skill)
            for kw in need:
                if kw not in section:
                    bad.append("%s 的分工段缺理由关键词「%s」" % (skill, kw))
        self.assertEqual(bad, [], "\n" + "\n".join(bad))


class TestInstallerDetectHasAStableShape(unittest.TestCase):
    """`install.py` 的 detect() 返回值不许再被裸元组解包。

    来历：给 Target 加了 caution 字段、detect() 多返回一项之后，
    **三个调用点我只改了两个**——`--uninstall` 与 `--discover` 当场 ValueError，
    而没有任何测试碰过它们。裸元组把"加一个字段"变成一次跨全文件的手工同步，
    漏一处就炸，而炸的是最少被跑到的那条路径。

    已改成具名元组；这条钉住"别再改回去"：任何 `for x in detect(...)`
    的循环变量必须是**单个名字**，按字段名取值。
    """

    def _tree(self):
        path = os.path.join(ROOT, "install.py")
        with open(path, encoding="utf-8") as fh:
            return ast.parse(fh.read(), "install.py")

    @staticmethod
    def _calls_detect(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "detect")

    def test_no_tuple_unpacking_of_detect(self):
        bad = []
        for node in ast.walk(self._tree()):
            if isinstance(node, (ast.For, ast.comprehension)):
                it = node.iter
                if self._calls_detect(it) and isinstance(node.target, ast.Tuple):
                    bad.append(getattr(node, "lineno", getattr(it, "lineno", "?")))
        self.assertEqual(bad, [],
                         "install.py 这些行又在按位置解包 detect()：%s" % bad)

    def test_detect_is_actually_used_somewhere(self):
        """派生式检查的老毛病：找不到调用点就自动全绿。先钉住它真的找得到。"""
        n = sum(1 for node in ast.walk(self._tree()) if self._calls_detect(node))
        self.assertGreaterEqual(n, 3, "只找到 %d 处 detect() 调用，检查本身可能失效了" % n)

    def test_uninstall_removes_from_the_store_too(self):
        """`--uninstall` 是**全局**的：连共享库带所有已检测到的端一起清。

        `--target` 只是**多加一个**端，不会把删除限定到它——踩过一次：
        想只清掉某个多余目录，结果把共享库和四个端全清了（重装可恢复，但当时不知道）。
        行为不改（全局清除是对的），但这条钉住它确实碰 STORE，
        免得哪天被"优化"成只清 --target 而没人发现。
        """
        src = open(os.path.join(ROOT, "install.py"), encoding="utf-8").read()
        body = src.split("def uninstall(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("STORE", body, "uninstall 不再碰共享库了？那 --target 的语义就变了")


class TestReadOnlySkillsStateTheirTraps(unittest.TestCase):
    """只读技能也得把陷阱写成对 Agent 的提醒。

    实测：jdy-query 第一次 GUI 测试就被绕开了——Agent 自己写脚本
    出了一份质量相当的 HTML 报告。对只读场景这不算安全事故，
    但简道云有几个"踩了不报错、只给错数字"的陷阱：
    筛选字段名写错等于不筛选（返回全表）、求平均把非数值当 0、
    分组丢掉空值。这些不能只写在脚本里——Agent 不用脚本时同样会踩。
    """

    def test_query_skill_lists_the_traps(self):
        with open(os.path.join(ROOT, "skills", "jdy-query", "SKILL.md"),
                  encoding="utf-8") as fh:
            md = fh.read()
        for kw in ("既不报错也不过滤", "非数值", "空值不能丢", "转义"):
            self.assertIn(kw, md, kw)
        self.assertIn("自己写代码也一样要躲", md)


class TestUserPhrasesReachTheDescription(unittest.TestCase):
    """反向保护：用户会怎么说，必须出现在 description 里。

    上一条测试只保证「脚本自报的 TRIGGERS ⊆ description」，是**单向**的——
    改写 description 时把词删掉，只要脚本里也没声明就照样全绿。
    实测中就这么丢过 5 个词：jdy-report 的「同比」「按月趋势」「按周拆分」「算个比率」、
    jdy-query 的「这个字段都有哪些值」，一次 description 重写全没了。

    这里维护一份**用户话术金名单**。它不是文案要求，是触发面：
    少一个词，就是少一类用户问法命中不了这个技能。
    """

    GOLDEN = {
        "jdy-report": ["周报", "月报", "日报", "环比", "同比", "按周拆分", "按月趋势",
                       "算个比率", "达成率", "Top", "数据汇总", "分组统计", "定时报表"],
        "jdy-query": ["查数", "有多少条", "按什么分组看看", "筛选", "做张图",
                      "分布情况", "这个字段都有哪些值", "HTML"],
        "jdy-clean": ["清洗", "重复", "查重", "格式不统一", "去掉多余空格",
                      "全角半角", "打标", "批量分类", "数据体检"],
        "jdy-doc": ["应用结构", "有哪些表", "字段", "体检", "数据字典"],
        "jdy-excel-bridge": ["Excel", "导入", "导出", "导入报错"],
        "jdy-flow-ops": ["待办", "待审批", "批量审批", "批量否决", "转交", "积压", "流程"],
        "jdy-sync": ["同步", "跨应用", "数据搬迁", "增量同步",
                     "子表单", "附件", "明细行同步"],
        "jdy-devkit": ["字段标识", "_widget_", "请求样例", "集成"],
        "hello-jdy": ["探针", "兼容", "连不上"],
        "jdy-watch": ["数据哨兵", "库存告警", "新单提醒", "盯着", "定时巡检",
                      "有变化就通知我", "阈值"],
        "jdy-org": ["通讯录", "组织架构", "部门", "成员编号", "建部门", "加成员",
                    "谁在哪个部门", "用户数"],
    }

    def test_every_golden_phrase_is_present(self):
        missing = []
        base = os.path.join(ROOT, "skills")
        for skill, phrases in sorted(self.GOLDEN.items()):
            path = os.path.join(base, skill, "SKILL.md")
            self.assertTrue(os.path.isfile(path), "没有 %s" % path)
            with open(path, encoding="utf-8") as fh:
                md = fh.read()
            desc = md.split("---")[1] if md.startswith("---") else md
            for p in phrases:
                if p not in desc:
                    missing.append("%s：description 里没有「%s」" % (skill, p))
        self.assertEqual(missing, [], "\n" + "\n".join(missing))

    def test_golden_list_covers_every_skill(self):
        """新增技能时别忘了给它一份金名单，否则这条保护对它不存在。"""
        have = {d for d in os.listdir(os.path.join(ROOT, "skills"))
                if os.path.isfile(os.path.join(ROOT, "skills", d, "SKILL.md"))}
        self.assertEqual(have - set(self.GOLDEN), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
