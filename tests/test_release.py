#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布产物必须是对的：包完整、校验和真能核、版本号对得上。

商店用户装的是 zip。zip 打错了、或者是几天前的旧版，界面上完全看不出来——
`dist/` 是 gitignore 的产物目录，没人盯着它。这套测试盯着它。

不依赖 pytest：`python3 tests/test_release.py`
"""
import os
import hashlib
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_into(tmp, layout=None):
    cmd = [sys.executable, os.path.join(ROOT, "build.py"), "--dist", tmp]
    if layout:
        cmd += ["--layout", layout]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True,
                       encoding="utf-8", errors="replace")
    return r


def skill_names():
    skills = os.path.join(ROOT, "skills")
    return sorted(n for n in os.listdir(skills)
                  if os.path.isfile(os.path.join(skills, n, "SKILL.md")))


def declared_version(name):
    with open(os.path.join(ROOT, "skills", name, "SKILL.md"), encoding="utf-8") as fh:
        head = fh.read().split("---", 2)[1]
    m = re.search(r"^version:\s*(\S+)\s*$", head, re.M)
    return m.group(1) if m else None


class Dist(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = cls._tmp.name
        cls.result = build_into(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_build_succeeds(self):
        self.assertEqual(0, self.result.returncode,
                         self.result.stdout + self.result.stderr)

    def test_every_skill_gets_a_zip(self):
        got = sorted(f[:-4] for f in os.listdir(self.out) if f.endswith(".zip"))
        self.assertEqual(skill_names(), got)

    def test_sha256sums_is_the_standard_two_column_format(self):
        """必须能被 `shasum -a 256 -c` 直接读。

        版本号搭便车写成第三列的话，文件看着更全，`shasum -c` 却会把
        "名字 v0.7.0" 整个当成文件名，报 No such file——**文档里给的核对命令
        就成了跑不通的命令**。所以这条测试盯的是格式，不只是哈希对不对。
        """
        path = os.path.join(self.out, "SHA256SUMS")
        self.assertTrue(os.path.exists(path), "没有 SHA256SUMS")
        with open(path, encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(len(skill_names()), len(lines))
        for line in lines:
            m = re.match(r"\A([0-9a-f]{64})  (\S+\.zip)\Z", line)
            self.assertIsNotNone(m, "不是标准两列格式：%r" % line)
            digest, fname = m.groups()
            with open(os.path.join(self.out, fname), "rb") as fh:
                self.assertEqual(digest, hashlib.sha256(fh.read()).hexdigest(),
                                 "%s 的校验和对不上" % fname)

    def test_checksum_files_are_lf_only(self):
        """SHA256SUMS / MANIFEST.txt 必须是 LF，**按字节读**才看得见。

        `open(..., encoding="utf-8")` 有 universal newlines：CRLF 在读回来时
        被悄悄折成 \n，所以上面那条"格式对不对"的测试在 Windows 上一直是绿的，
        而真正跑 `shasum -a 256 -c SHA256SUMS` 的人拿到的是
        `sha256sum: 'hello-jdy.zip'$'\r': No such file or directory`——
        文件名尾巴上多了个回车，11 个包一个都核不了。
        所以这条断言必须读 bytes：它盯的是磁盘上的字节，不是解码后的字符串。
        """
        for fname in ("SHA256SUMS", "MANIFEST.txt"):
            with open(os.path.join(self.out, fname), "rb") as fh:
                raw = fh.read()
            self.assertNotIn(b"\r", raw,
                             "%s 里有 CR（Windows 上默认 newline 写出来就是 CRLF）；"
                             "build.py 写它时要带 newline=\"\\n\"" % fname)

    def test_shasum_check_actually_passes(self):
        """不光格式对，真跑一遍核对命令。"""
        exe = "shasum" if sys.platform == "darwin" else "sha256sum"
        args = [exe, "-a", "256", "-c", "SHA256SUMS"] if exe == "shasum" \
            else [exe, "-c", "SHA256SUMS"]
        try:
            r = subprocess.run(args, cwd=self.out, capture_output=True, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            self.skipTest("这台机器上没有 %s" % exe)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)

    def test_manifest_versions_match_the_skills(self):
        with open(os.path.join(self.out, "MANIFEST.txt"), encoding="utf-8") as fh:
            rows = dict(re.findall(r"^(\S+)\s+v(\S+)", fh.read(), re.M))
        for name in skill_names():
            self.assertIn(name, rows, "MANIFEST 里少了 %s" % name)
            self.assertEqual(declared_version(name), rows[name],
                             "%s 的 MANIFEST 版本与 SKILL.md 不一致" % name)

    def test_zip_top_level_dir_is_the_skill_name(self):
        """各端的「导入本地技能」要求压缩包内以技能名为顶层目录。"""
        for name in skill_names():
            with zipfile.ZipFile(os.path.join(self.out, "%s.zip" % name)) as zf:
                tops = {n.split("/")[0] for n in zf.namelist()}
            self.assertEqual({name}, tops, "%s.zip 的顶层目录不对：%s" % (name, tops))

    def test_zip_carries_the_vendored_kernel_and_no_bytecode(self):
        """装出去的包必须自带内核（安装后 _shared/ 不会跟着走），且不夹 .pyc。"""
        for name in skill_names():
            with zipfile.ZipFile(os.path.join(self.out, "%s.zip" % name)) as zf:
                names = zf.namelist()
            self.assertFalse([n for n in names if n.endswith(".pyc")
                              or "__pycache__" in n], "%s.zip 里有字节码" % name)
            uses_kernel = any(n.endswith("_bootstrap.py") for n in names)
            if uses_kernel:
                self.assertTrue(any("/scripts/_shared/jdy_client.py" in n for n in names),
                                "%s.zip 带 _bootstrap 却没带内核，装完 import 会崩" % name)

    def test_no_secrets_survive_into_the_zips(self):
        """脱敏闸门管的是仓库，这条管的是**真正发出去的东西**。"""
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from test_no_secrets import scan
        problems = []
        for name in skill_names():
            with zipfile.ZipFile(os.path.join(self.out, "%s.zip" % name)) as zf:
                for member in zf.namelist():
                    if member.endswith("/"):
                        continue
                    try:
                        text = zf.read(member).decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    for hit in scan(text):
                        problems.append("%s!%s → %s" % (name, member, hit))
        self.assertEqual([], problems, "发布包里有未脱敏内容：\n  "
                         + "\n  ".join(problems))


class TwoLayouts(unittest.TestCase):
    """**两个渠道要的 zip 顶层目录不一样，打错只有上传时才知道。**

    GitHub Release / 千问办公要 `<name>/SKILL.md`；腾讯 WorkBuddy 开放平台的
    「技能」渠道要 `skills/<name>/SKILL.md`——最外层多一级 `skills/`。
    少了那一级，上传时对方报"找不到 SKILL.md"，而 zip 在本地解开来看毫无异样。
    所以这里对两种布局各断言一次包内路径，并盯着校验和把两种都收进去。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = cls._tmp.name
        cls.result = build_into(cls.out, "both")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_build_succeeds(self):
        self.assertEqual(0, self.result.returncode,
                         self.result.stdout + self.result.stderr)

    def test_both_layouts_produce_a_zip_per_skill(self):
        got = sorted(f[:-4] for f in os.listdir(self.out) if f.endswith(".zip"))
        want = sorted([n for n in skill_names()]
                      + ["%s-workbuddy" % n for n in skill_names()])
        self.assertEqual(want, got)

    def test_github_layout_top_level_dir_is_the_skill_name(self):
        for name in skill_names():
            with zipfile.ZipFile(os.path.join(self.out, "%s.zip" % name)) as zf:
                names = zf.namelist()
            self.assertEqual({name}, {n.split("/")[0] for n in names},
                             "%s.zip 的顶层目录不对" % name)
            self.assertIn("%s/SKILL.md" % name, names)

    def test_workbuddy_layout_nests_everything_under_skills(self):
        for name in skill_names():
            zp = os.path.join(self.out, "%s-workbuddy.zip" % name)
            with zipfile.ZipFile(zp) as zf:
                names = zf.namelist()
            self.assertEqual({"skills"}, {n.split("/")[0] for n in names},
                             "%s-workbuddy.zip 的顶层目录必须是 skills/" % name)
            self.assertEqual({name}, {n.split("/")[1] for n in names},
                             "%s-workbuddy.zip 的第二层必须是技能名" % name)
            self.assertIn("skills/%s/SKILL.md" % name, names,
                          "官方要求的路径正是 skills/{skill-name}/SKILL.md")

    def test_the_two_layouts_carry_exactly_the_same_files(self):
        """换的只是顶层目录名，内容一个文件都不许多不许少。"""
        for name in skill_names():
            with zipfile.ZipFile(os.path.join(self.out, "%s.zip" % name)) as zf:
                a = sorted(n.split("/", 1)[1] for n in zf.namelist())
            with zipfile.ZipFile(os.path.join(
                    self.out, "%s-workbuddy.zip" % name)) as zf:
                b = sorted(n.split("/", 2)[2] for n in zf.namelist())
            self.assertEqual(a, b, "%s 两种布局的内容不一致" % name)

    def test_checksums_cover_both_layouts(self):
        """只给一种布局写校验和，另一种就成了没人核得了的产物。"""
        with open(os.path.join(self.out, "SHA256SUMS"), encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(2 * len(skill_names()), len(lines))
        listed = set()
        for line in lines:
            m = re.match(r"\A([0-9a-f]{64})  (\S+\.zip)\Z", line)
            self.assertIsNotNone(m, "不是标准两列格式：%r" % line)
            digest, fname = m.groups()
            listed.add(fname)
            with open(os.path.join(self.out, fname), "rb") as fh:
                self.assertEqual(digest, hashlib.sha256(fh.read()).hexdigest(),
                                 "%s 的校验和对不上" % fname)
        for name in skill_names():
            self.assertIn("%s.zip" % name, listed)
            self.assertIn("%s-workbuddy.zip" % name, listed)

    def test_manifest_covers_both_layouts_with_the_declared_version(self):
        with open(os.path.join(self.out, "MANIFEST.txt"), encoding="utf-8") as fh:
            rows = dict(re.findall(r"^(\S+)\s+v(\S+)", fh.read(), re.M))
        for name in skill_names():
            for stem in (name, "%s-workbuddy" % name):
                self.assertIn(stem, rows, "MANIFEST 里少了 %s" % stem)
                self.assertEqual(declared_version(name), rows[stem])


if __name__ == "__main__":
    unittest.main(verbosity=2)
