#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""专家包（CodeBuddy 插件格式）必须符合 WorkBuddy 开放平台的上架规范。

这一层的错**全都是上传那一刻才出现的**：本地怎么看都正常，压缩包解开来
一切齐全，点提交才被告知「displayDescription 中文字数不对」「tags 不是 3 个」
「压缩包缺少 .codebuddy-plugin/plugin.json 文件」。审核方不会告诉你差在哪一位，
而每次改完要重新走一遍上传流程。所以把规范写成断言，在本机就红。

判据来自 `build_experts.validate()`——测试不另抄一份。抄一份的下场是两边
慢慢分叉，然后打包通过、上传被拒。

**变异检查**：光断言"现在是对的"证明不了守卫真的会拦。所以这里还把
plugin.json 改坏三种典型的坏法，各自必须红，然后还原。

不依赖 pytest：`python3 tests/test_experts.py`
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

for _s in (sys.stdout, sys.stderr):          # Windows 中文控制台默认 GBK
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import build_experts as be  # noqa: E402

NAMES = be.expert_names()


class Sources(unittest.TestCase):
    """experts/ 下的源文件本身。"""

    def test_there_are_experts_at_all(self):
        """空目录会让下面每一条断言都变成"零次循环"，全绿而什么都没测。"""
        self.assertTrue(NAMES, "experts/ 下没有专家包")

    def test_every_expert_passes_the_full_spec(self):
        problems = []
        for name in NAMES:
            problems += be.validate(name)
        self.assertEqual([], problems, "\n  " + "\n  ".join(problems))

    def test_plugin_json_identity_fields(self):
        for name in NAMES:
            p = be.load_plugin(name)
            self.assertEqual(name, p["name"], "name 要与目录名一致")
            self.assertRegex(p["name"], be.KEBAB, "name 必须是 kebab-case")
            self.assertEqual("agent", p["expertType"])
            self.assertEqual(p["name"], p["plugin"], "plugin 必须与 name 相同")
            self.assertRegex(str(p["version"]), be.SEMVER)

    def test_agents_paths_exist_and_agent_name_matches_the_file(self):
        for name in NAMES:
            p = be.load_plugin(name)
            d = os.path.join(be.EXPERTS, name)
            for rel in p["agents"]:
                self.assertTrue(os.path.isfile(os.path.join(d, rel[2:])),
                                "%s：agents 里的 %s 不存在" % (name, rel))
            md = os.path.join(d, "agents", "%s.md" % p["agentName"])
            self.assertTrue(os.path.isfile(md),
                            "%s：agentName 与 agents/ 下的文件名对不上" % name)

    def test_display_description_zh_is_40_to_50_chars(self):
        """官方规范写死的硬区间，不是建议。"""
        for name in NAMES:
            zh = be.load_plugin(name)["displayDescription"]["zh"]
            self.assertTrue(40 <= len(zh) <= 50,
                            "%s：displayDescription.zh 有 %d 字（要求 40–50）：%s"
                            % (name, len(zh), zh))

    def test_exactly_three_tags_and_three_quick_prompts(self):
        for name in NAMES:
            p = be.load_plugin(name)
            self.assertEqual(3, len(p["tags"]), "%s：tags 必须恰好 3 个" % name)
            self.assertEqual(3, len(p["quickPrompts"]),
                             "%s：quickPrompts 必须恰好 3 个" % name)
            for i, item in enumerate(p["tags"] + p["quickPrompts"]):
                self.assertTrue(item.get("en") and item.get("zh"),
                                "%s：第 %d 项缺 en 或 zh" % (name, i))

    def test_default_init_prompt_matches_the_first_quick_prompt(self):
        """**中英两种都要一致。**

        只对上中文那半，市场上英文界面的首句和第一个推荐提示词会对不上——
        而那正是两半只做一半的老毛病。
        """
        for name in NAMES:
            p = be.load_plugin(name)
            for lang in ("zh", "en"):
                self.assertEqual(p["defaultInitPrompt"][lang],
                                 p["quickPrompts"][0][lang],
                                 "%s：defaultInitPrompt.%s 与 quickPrompts[0].%s 不一致"
                                 % (name, lang, lang))

    def test_category_id_is_in_the_official_enum(self):
        for name in NAMES:
            self.assertIn(be.load_plugin(name)["categoryId"], be.CATEGORY_IDS)

    def test_avatar_is_a_512_png_under_500kb(self):
        for name in NAMES:
            p = be.load_plugin(name)
            path = os.path.join(be.EXPERTS, name, p["avatar"])
            self.assertTrue(os.path.isfile(path), "%s：头像不存在" % name)
            self.assertEqual((512, 512), be.png_size(path),
                             "%s：头像必须是 512×512 PNG" % name)
            self.assertLessEqual(os.path.getsize(path), 500 * 1024,
                                 "%s：头像超过 500KB" % name)

    def test_declared_skills_really_exist_in_the_repo(self):
        """skills/ 不入库，靠构建时从仓库拷——声明了一个不存在的技能，
        只会在装到用户机器上、Agent 去读它时才发现。"""
        for name in NAMES:
            for rel in be.load_plugin(name).get("skills") or []:
                skill = rel[len("./skills/"):]
                self.assertTrue(
                    os.path.isfile(os.path.join(ROOT, "skills", skill, "SKILL.md")),
                    "%s 声明的技能 %s 在 skills/ 下不存在" % (name, skill))

    def test_author_is_the_project_brand(self):
        for name in NAMES:
            author = be.load_plugin(name)["author"]
            self.assertEqual("aicliagent", author["name"])
            self.assertEqual("hi@aicliagent.com", author["email"])

    def test_english_description_is_really_english(self):
        """官方规范：plugin.json 的 description 与 agent md 的 description
        都是**英文**——后者是 AI 判断何时激活这个专家的依据。"""
        import re
        for name in NAMES:
            self.assertNotRegex(be.load_plugin(name)["description"], r"[一-鿿]",
                                "%s：plugin.json description 必须是英文" % name)


class AgentMarkdown(unittest.TestCase):
    """agents/*.md：frontmatter 与系统提示词。"""

    def _mds(self):
        for name in NAMES:
            d = os.path.join(be.EXPERTS, name, "agents")
            for f in sorted(os.listdir(d)):
                if f.endswith(".md"):
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        yield "%s/%s" % (name, f), f[:-3], fh.read()

    def test_frontmatter_has_the_required_fields_and_name_matches_filename(self):
        import re
        for where, stem, text in self._mds():
            m = be.FRONTMATTER.match(text)
            self.assertIsNotNone(m, "%s：开头必须是 --- frontmatter" % where)
            fm = m.group(1)
            for key in ("name", "description", "displayName", "profession"):
                self.assertRegex(fm, r"(?m)^%s:" % key,
                                 "%s：frontmatter 缺 %s" % (where, key))
            got = re.search(r'(?m)^name:\s*"?([^"\n]+?)"?\s*$', fm).group(1)
            self.assertEqual(stem, got, "%s：name 与文件名不一致" % where)

    def test_frontmatter_must_not_declare_tools(self):
        """**开发者不可自行添加 tools**，工具权限由平台统一分配。"""
        import re
        for where, _stem, text in self._mds():
            fm = be.FRONTMATTER.match(text).group(1)
            self.assertNotRegex(fm, r"(?m)^tools:",
                                "%s：frontmatter 里不许有 tools" % where)

    def test_system_prompt_carries_the_non_negotiable_rules(self):
        """措辞可以改，这几条约束不能丢。

        它们不是文案：少一条，这个专家就会在别人的端上去干我们明确说过
        不干的事——而那要装上之后才会发生，本地一次都不会复现。
        """
        for where, _stem, text in self._mds():
            body = text[be.FRONTMATTER.match(text).end():]
            for mark in be.REQUIRED_PROMPT_MARKS:
                self.assertIn(mark, body, "%s：系统提示词里没写到「%s」" % (where, mark))

    def test_system_prompt_states_the_trademark_boundary(self):
        for where, _stem, text in self._mds():
            for mark in ("帆软", "商标", "aicliagent"):
                self.assertIn(mark, text, "%s：没写清与帆软的关系（缺「%s」）"
                              % (where, mark))

    def test_system_prompt_forbids_looping_the_official_single_create_tool(self):
        """双轨分工里最要命的一条：官方单条新增工具静默存 null 还返回成功。"""
        for where, _stem, text in self._mds():
            self.assertIn("null", text,
                          "%s：要写明官方单条新增工具会把脏值静默存成 null" % where)
            self.assertIn("回读", text, "%s：要写明写后回读核对" % where)


class BuiltZip(unittest.TestCase):
    """构建产物：**zip 的根就是包内容，不套一层目录。**

    开放平台按 `.codebuddy-plugin/plugin.json` 认包。外面多一层 `<name>/`，
    它就报「压缩包缺少 .codebuddy-plugin/plugin.json 文件」——而本地解压
    看着一切正常，这个错只在上传那一刻出现。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = cls._tmp.name
        cls.result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "build_experts.py"), "--out", cls.out],
            cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_build_succeeds(self):
        self.assertEqual(0, self.result.returncode,
                         self.result.stdout + self.result.stderr)

    def test_wrapped_variant_has_exactly_one_top_level_dir(self):
        """`<name>-wrapped.zip` 照官方 package_expert.py 的布局：全部条目在 `<name>/` 下。"""
        for name in NAMES:
            with zipfile.ZipFile(os.path.join(self.out, "%s-wrapped.zip" % name)) as zf:
                names = zf.namelist()
            self.assertTrue(names, "%s-wrapped.zip 是空的" % name)
            self.assertEqual({n.split("/", 1)[0] for n in names}, {name},
                             "%s-wrapped.zip 顶层不是唯一的 %s/：%s" % (name, name, names[:5]))
            self.assertIn("%s/.codebuddy-plugin/plugin.json" % name, names)

    def test_every_expert_gets_a_zip(self):
        got = sorted(f[:-4] for f in os.listdir(self.out)
                     if f.endswith(".zip") and not f.endswith("-wrapped.zip"))
        self.assertEqual(sorted(NAMES), got)

    def test_plugin_json_is_at_the_first_level(self):
        for name in NAMES:
            with zipfile.ZipFile(os.path.join(self.out, "%s.zip" % name)) as zf:
                names = zf.namelist()
            self.assertIn(".codebuddy-plugin/plugin.json", names,
                          "%s.zip 的第一层没有 .codebuddy-plugin/plugin.json：%s"
                          % (name, sorted({n.split("/")[0] for n in names})))
            tops = {n.split("/")[0] for n in names}
            self.assertNotIn(name, tops, "%s.zip 多套了一层 %s/ 目录" % (name, name))

    def test_agents_avatars_and_readme_are_at_the_root(self):
        for name in NAMES:
            p = be.load_plugin(name)
            with zipfile.ZipFile(os.path.join(self.out, "%s.zip" % name)) as zf:
                names = zf.namelist()
            self.assertIn("agents/%s.md" % p["agentName"], names)
            self.assertIn(p["avatar"], names)
            self.assertIn("README.md", names)

    def test_every_declared_skill_is_inside_with_its_skill_md(self):
        for name in NAMES:
            p = be.load_plugin(name)
            with zipfile.ZipFile(os.path.join(self.out, "%s.zip" % name)) as zf:
                names = zf.namelist()
            for rel in p.get("skills") or []:
                skill = rel[len("./skills/"):]
                self.assertIn("skills/%s/SKILL.md" % skill, names,
                              "%s.zip 里少了 skills/%s/SKILL.md" % (name, skill))

    def test_skills_carry_the_vendored_kernel_and_no_bytecode(self):
        """技能被复制走时 _shared/ 不跟着走，内核必须已经在包里。"""
        for name in NAMES:
            with zipfile.ZipFile(os.path.join(self.out, "%s.zip" % name)) as zf:
                names = zf.namelist()
            self.assertEqual([], [n for n in names
                                  if n.endswith(".pyc") or "__pycache__" in n],
                             "%s.zip 里有字节码" % name)
            self.assertEqual([], [n for n in names if n.endswith(".DS_Store")])
            for n in names:
                if n.endswith("/scripts/_bootstrap.py"):
                    skill = n.split("/")[1]
                    self.assertIn("skills/%s/scripts/_shared/jdy_client.py" % skill,
                                  names,
                                  "%s.zip 的 %s 带 _bootstrap 却没带内核"
                                  % (name, skill))

    def test_checksums_are_the_standard_two_column_lf_format(self):
        path = os.path.join(self.out, "SHA256SUMS")
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r", raw, "SHA256SUMS 里有 CR，shasum -c 会认不出文件名")
        import hashlib
        import re
        lines = raw.decode("utf-8").splitlines()
        self.assertEqual(2 * len(NAMES), len(lines))     # 每个专家两份：不套目录 + -wrapped
        for line in lines:
            m = re.match(r"\A([0-9a-f]{64})  (\S+\.zip)\Z", line)
            self.assertIsNotNone(m, "不是标准两列格式：%r" % line)
            with open(os.path.join(self.out, m.group(2)), "rb") as fh:
                self.assertEqual(m.group(1), hashlib.sha256(fh.read()).hexdigest())


class Mutations(unittest.TestCase):
    """**变异检查：把包改坏，守卫必须红。**

    上面那些断言只证明"现在是对的"。守卫失灵（正则写歪、判断被短路）时
    它们照样全绿——因为现在的包本来就是对的。这里反过来问：改坏了会不会红。

    改的是**临时目录里的副本**，仓库里的 experts/ 一个字节都不动。
    """

    EXPERT = "jdy-ops-expert"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved_experts = be.EXPERTS
        be.EXPERTS = self.tmp
        shutil.copytree(os.path.join(self.saved_experts, self.EXPERT),
                        os.path.join(self.tmp, self.EXPERT))
        self.pj = be.plugin_path(self.EXPERT)

    def tearDown(self):
        be.EXPERTS = self.saved_experts
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mutate(self, change):
        with open(self.pj, encoding="utf-8") as fh:
            data = json.load(fh)
        change(data)
        with open(self.pj, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        return be.validate(self.EXPERT)

    def test_baseline_copy_is_clean(self):
        """先证明副本本身是绿的，否则下面三条红得毫无意义。"""
        self.assertEqual([], be.validate(self.EXPERT))

    def test_39_char_zh_description_is_rejected(self):
        def change(d):
            d["displayDescription"]["zh"] = "简" * 39
        errors = self._mutate(change)
        self.assertTrue(any("displayDescription.zh" in e for e in errors),
                        "39 字的中文展示描述没被拦下：%s" % errors)

    def test_two_tags_is_rejected(self):
        def change(d):
            d["tags"] = d["tags"][:2]
        errors = self._mutate(change)
        self.assertTrue(any("tags" in e for e in errors),
                        "只剩 2 个 tag 没被拦下：%s" % errors)

    def test_default_init_prompt_drifting_from_the_first_quick_prompt_is_rejected(self):
        def change(d):
            d["defaultInitPrompt"]["zh"] = d["defaultInitPrompt"]["zh"] + "（改过了）"
        errors = self._mutate(change)
        self.assertTrue(any("defaultInitPrompt" in e for e in errors),
                        "首句与第一个推荐提示词不一致没被拦下：%s" % errors)

    def test_english_only_drift_is_also_rejected(self):
        """两半只做一半的老毛病：中文对上了、英文没对上，一样要红。"""
        def change(d):
            d["defaultInitPrompt"]["en"] = "Something else entirely"
        errors = self._mutate(change)
        self.assertTrue(any("defaultInitPrompt.en" in e for e in errors),
                        "英文首句漂了没被拦下：%s" % errors)

    def test_tools_in_agent_frontmatter_is_rejected(self):
        """开发者不可自行添加 tools——加了必须红。"""
        p = be.load_plugin(self.EXPERT)
        md = os.path.join(self.tmp, self.EXPERT, "agents", "%s.md" % p["agentName"])
        with open(md, encoding="utf-8") as fh:
            text = fh.read()
        text = text.replace("maxTurns: 50", "maxTurns: 50\ntools: [Bash, Read]", 1)
        with open(md, "w", encoding="utf-8") as fh:
            fh.write(text)
        errors = be.validate(self.EXPERT)
        self.assertTrue(any("tools" in e for e in errors),
                        "frontmatter 里的 tools 没被拦下：%s" % errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
