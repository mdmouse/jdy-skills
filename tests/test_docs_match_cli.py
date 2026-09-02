# -*- coding: utf-8 -*-
"""文档里出现的命令行参数，必须真的存在。

来历：README 里还写着 `--where '_widget_x=值'`，而 --where 早就改成
认显示名了；技能数写着 5 个，实际 6 个。文档是别人装这套东西时看的第一样
东西，它描述的却是修复前的行为——照着做只会得到"参数不存在"或更糟的静默错误。

这里把 README / SKILL.md / references 里的命令抽出来，逐个核对参数是否存在。
不执行命令，所以不需要网络和密钥。
"""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD = re.compile(r"^\s*(?:python3?|\S*/python3?)\s+(\S+?\.py)\s*(.*)$")
FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")


def _docs():
    yield os.path.join(ROOT, "README.md")
    # experts/ 也扫：专家包的系统提示词里写的命令，Agent 会照着敲。
    # 那里的参数写错，用户看到的是一句 "unrecognized arguments"，
    # 而这个专家是要上架给别人装的——错的命令行会跟着发出去。
    for base in ("skills", "references", "docs", "experts"):
        for root, _d, files in os.walk(os.path.join(ROOT, base)):
            if "%s_shared" % os.sep in root:
                continue
            for f in files:
                if f.endswith(".md"):
                    yield os.path.join(root, f)


def _declared_flags(script_path):
    """脚本 argparse 里声明过的长参数。"""
    with open(script_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "add_argument"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("--"):
                    flags.add(arg.value)
    return flags


def _resolve(script_ref, doc_path):
    """把文档里写的路径映射到仓库里的真实脚本。

    优先按字面路径找——README 写的是 skills/jdy-report/scripts/init_config.py
    这种完整路径，只按文件名去猜会撞上同名脚本（三个技能都有 init_config.py）。
    """
    literal = os.path.join(ROOT, script_ref)
    if os.path.isfile(literal):
        return literal
    name = os.path.basename(script_ref)
    for root, _d, files in os.walk(os.path.join(ROOT, "skills")):
        if "%s_shared" % os.sep in root:
            continue
        if name in files:
            # 同名脚本存在于多个技能里（init_config.py），优先取文档所在技能
            skill = None
            rel = os.path.relpath(doc_path, ROOT).split(os.sep)
            if len(rel) > 1 and rel[0] == "skills":
                skill = rel[1]
            if skill and os.sep + skill + os.sep not in root:
                continue
            return os.path.join(root, name)
    return None


class TestDocumentedFlagsExist(unittest.TestCase):

    def test_every_documented_flag_is_real(self):
        problems = []
        for doc in _docs():
            with open(doc, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    m = CMD.match(line)
                    if not m:
                        continue
                    script = _resolve(m.group(1), doc)
                    if script is None:
                        continue          # 文档里的示意路径，不是真脚本
                    declared = _declared_flags(script)
                    for flag in FLAG.findall(m.group(2)):
                        if flag not in declared:
                            problems.append(
                                "%s:%d  %s 没有参数 %s"
                                % (os.path.relpath(doc, ROOT), lineno,
                                   os.path.basename(script), flag))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_readme_skill_count_matches_reality(self):
        skills = [d for d in os.listdir(os.path.join(ROOT, "skills"))
                  if os.path.isdir(os.path.join(ROOT, "skills", d))]
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        m = re.search(r"(\d+)\s*个技能", readme)
        self.assertIsNotNone(m, "README 里应写明技能数量")
        self.assertEqual(int(m.group(1)), len(skills),
                         "README 说 %s 个技能，实际 %d 个：%s"
                         % (m.group(1) if m else "?", len(skills), sorted(skills)))

    def test_every_skill_appears_in_readme(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        missing = [d for d in sorted(os.listdir(os.path.join(ROOT, "skills")))
                   if os.path.isdir(os.path.join(ROOT, "skills", d))
                   and d not in readme]
        self.assertEqual(missing, [], "README 没提到这些技能：%s" % missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
