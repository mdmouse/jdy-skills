#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 上跑得起来：控制台编码 + 宿主目录路径。

三端主力用户在 Windows，中文 Windows 控制台默认 GBK；打印 ✅ / ⬜ 会抛
UnicodeEncodeError 直接退出——不是乱码，是崩溃。修法是在任何 print 之前把
stdio 钉成 UTF-8（见 skills/*/scripts/_bootstrap.py 的 _force_utf8_stdio）。

路径那一半：多数端是 ~/.xxx，expanduser 在 Windows 上就对；只有把配置塞进
Application Support 的端要单列 %APPDATA% 候选，靠 install.py 的 _expand 展开。

这套测试用 PYTHONIOENCODING=gbk 在**子进程**里复现真实条件，不是模拟。
把 _force_utf8_stdio() 那一行删掉，test_every_entry_point_pins_utf8 会红。

不依赖 pytest：`python3 tests/test_windows.py`
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GBK_ENV = dict(os.environ, PYTHONIOENCODING="gbk")
# 子进程的 stdout 一经 _bootstrap 就被钉成 UTF-8，所以这边按 UTF-8 解码。
# 不能用 text=True：它按本进程 locale 解码，Windows runner 是 cp1252，
# 遇到 UTF-8 的续字节（0x81/0x8f/0x90）直接 UnicodeDecodeError——首次 CI 就是这么挂的。

# 会被 GBK 拒收的两个符号，仓库里到处在打
SENTINELS = "✅⬜"          # ✅ ⬜


def _gbk_ok(ch):
    try:
        ch.encode("gbk")
        return True
    except UnicodeEncodeError:
        return False


def entry_points():
    """所有会被用户直接执行的脚本：仓库根两个 + 各技能 scripts/ 下带 __main__ 的。

    _bootstrap.py 和 vendor 进来的 _shared/ 不是入口，跳过。
    """
    found = [os.path.join(ROOT, n) for n in ("install.py", "build.py")]
    skills = os.path.join(ROOT, "skills")
    for skill in sorted(os.listdir(skills)):
        scripts = os.path.join(skills, skill, "scripts")
        if not os.path.isdir(scripts):
            continue
        for name in sorted(os.listdir(scripts)):
            if not name.endswith(".py") or name == "_bootstrap.py":
                continue
            path = os.path.join(scripts, name)
            with open(path, encoding="utf-8") as fh:
                if "__main__" in fh.read():
                    found.append(path)
    return found


class GbkHarnessIsSensitive(unittest.TestCase):
    """先证明这套测试真的能看见问题——否则下面全是空过。"""

    def test_gbk_really_rejects_the_sentinels(self):
        r = subprocess.run([sys.executable, "-c", "print('%s')" % SENTINELS],
                           env=GBK_ENV, capture_output=True, encoding="utf-8", errors="replace")
        self.assertNotEqual(r.returncode, 0,
                            "GBK 竟然收下了 ✅⬜——本文件所有断言都失去意义了")
        self.assertIn("UnicodeEncodeError", r.stderr)


class EntryPointsSurviveGbk(unittest.TestCase):

    def test_every_entry_point_pins_utf8(self):
        """导入即钉住：脚本被 import 完，stdout 必须已经是 UTF-8。

        这是覆盖率断言——新加的入口忘了走 _bootstrap 就会在这里红。
        """
        # run_name 不是 __main__，所以 main() 不会被触发，只跑模块级副作用
        # （编码修复就是模块级副作用）。不能用字符串切 "if __name__"——
        # jdy-devkit/gen.py 在三引号样例代码里就写着这句。
        probe = (
            "import runpy, sys\n"
            "runpy.run_path(sys.argv[1], run_name='notmain')\n"
            "sys.stderr.write('ENC=' + (sys.stdout.encoding or 'none'))\n"
        )
        offenders = []
        for path in entry_points():
            r = subprocess.run([sys.executable, "-c", probe, path],
                               env=GBK_ENV, capture_output=True, encoding="utf-8", errors="replace",
                               cwd=os.path.dirname(path))
            marker = (r.stderr or "").rsplit("ENC=", 1)
            enc = marker[1].strip().lower() if len(marker) == 2 else ""
            if r.returncode != 0 or enc not in ("utf-8", "utf8"):
                offenders.append("%s -> rc=%d enc=%s%s"
                                 % (os.path.relpath(path, ROOT), r.returncode,
                                    enc or "?",
                                    "\n      " + r.stderr.strip()[-160:] if r.returncode else ""))
        self.assertEqual([], offenders,
                         "这些入口在 GBK 下没把 stdout 钉成 UTF-8：\n  "
                         + "\n  ".join(offenders))

    def test_probe_runs_end_to_end_under_gbk(self):
        """hello-jdy 是最可能被先跑的一个，端到端过一遍真实输出。"""
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "skills/hello-jdy/scripts/probe.py"),
             "--no-network"],
            env=GBK_ENV, capture_output=True, encoding="utf-8", errors="replace")
        self.assertNotIn("UnicodeEncodeError", r.stderr)
        self.assertEqual(0, r.returncode, r.stderr[-500:])
        # 不写死某个符号（probe 用的是 [ OK ] 不是 ✅）——直接问：这段输出里
        # 到底有没有 GBK 编不动的字符？没有的话这条断言等于没测编码。
        unencodable = sorted({c for c in r.stdout if not _gbk_ok(c)})
        self.assertTrue(unencodable,
                        "probe 的输出 GBK 全编得动——这条断言没在验编码了")

    def test_installer_lists_hosts_under_gbk(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "install.py"), "--list"],
                           env=GBK_ENV, capture_output=True, encoding="utf-8", errors="replace", cwd=ROOT)
        self.assertNotIn("UnicodeEncodeError", r.stderr)
        self.assertEqual(0, r.returncode, r.stderr[-500:])


class BootstrapCopiesStayIdentical(unittest.TestCase):
    """10 份 _bootstrap.py 是手工复制的，没有生成器把关。

    编码修复就住在里面——改了一份没改其他份，就又是「一件事两半只焊了一半」。
    """

    def test_all_ten_are_byte_identical(self):
        skills = os.path.join(ROOT, "skills")
        blobs = {}
        for skill in sorted(os.listdir(skills)):
            path = os.path.join(skills, skill, "scripts", "_bootstrap.py")
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    blobs.setdefault(fh.read(), []).append(skill)
        self.assertEqual(1, len(blobs),
                         "_bootstrap.py 出现分叉：%s" % sorted(blobs.values()))
        content = next(iter(blobs)).decode("utf-8")
        self.assertIn("_force_utf8_stdio", content,
                      "_bootstrap.py 里没有编码修复了")



class WindowsHostPaths(unittest.TestCase):
    """宿主目录在 Windows 上要指得对。

    现存的端用的都是 `~/.xxx`——`expanduser` 在 Windows 上返回
    C:\\Users\\<user>\\.xxx，本来就是对的，所以这里没有平台分支要守。
    要守的是**别再有人加一条只在 macOS 上成立的路径**：豆包工作曾经是那样
    （Application Support 深处），本版本已不支持它；下一个这么写的端，
    要在这里红一次，被提醒补上 %APPDATA% 那一支。
    """

    @staticmethod
    def _install():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "jdy_install", os.path.join(ROOT, "install.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_expand_handles_percent_vars(self):
        """%VAR% 的展开靠 expandvars，而且**只有 Windows 上的 expandvars 认它**。

        posixpath.expandvars 只认 $VAR，会把 %APPDATA% 原样丢回来——这在 macOS
        上是对的（那条候选 isdir 不中，自然跳过），但也意味着这台机器验证不了
        Windows 的行为。所以分两步验：
          1. ntpath.expandvars 确实认 %VAR%（Windows 上 os.path 就是 ntpath）；
          2. _expand 链路里确实有 expandvars（用 POSIX 认得的 $VAR 证明）。

        现在没有端用得上它，但机制留着：下一个需要 %APPDATA% 的端不用重新发明。
        """
        import ntpath
        mod = self._install()
        os.environ["JDY_TEST_FAKE_APPDATA"] = ntpath.join("C:\\Users", "u", "AppData")
        try:
            win = ntpath.expandvars("%JDY_TEST_FAKE_APPDATA%\\SomeApp")
            self.assertNotIn("%", win, "Windows 上 %%VAR%% 没展开：%s" % win)
            self.assertTrue(win.endswith("SomeApp"), win)

            posix = mod._expand("$JDY_TEST_FAKE_APPDATA/SomeApp")
            self.assertNotIn("$", posix, "_expand 没走 expandvars：%s" % posix)
        finally:
            del os.environ["JDY_TEST_FAKE_APPDATA"]

    def test_percent_var_is_inert_on_posix(self):
        """macOS/Linux 上 %APPDATA% 展不开是**预期行为**，不能因此崩或误判。"""
        if os.name == "nt":
            self.skipTest("这条讲的是非 Windows 的行为")
        mod = self._install()
        got = mod._expand(r"%APPDATA%\SomeApp")
        self.assertIn("%", got, "POSIX 上竟然展开了 %APPDATA%？")
        self.assertFalse(os.path.isdir(got), "这条候选应当直接落空，不该命中")

    def test_every_target_resolves_to_an_absolute_path_on_windows(self):
        """每个端在 Windows 上都得能算出一个不含 ~ 的绝对路径。"""
        mod = self._install()
        broken = []
        for target in mod.TARGETS:
            for path in target.candidates():
                expanded = mod._expand(path)
                if expanded.startswith("~"):
                    broken.append("%s: ~ 没展开 -> %s" % (target.label, expanded))
                if "%" in expanded and os.name == "nt":
                    broken.append("%s: %%VAR%% 没展开 -> %s" % (target.label, expanded))
        self.assertEqual([], broken, "\n  ".join(broken))

    def test_no_target_has_a_macos_only_path(self):
        """再有人加只在 macOS 上成立的路径，这里要红。

        `~/Library/Application Support/...` 在 Windows 上会展成
        `C:\\Users\\<user>\\Library\\Application Support\\...`——一个永远不存在的目录。
        它不报错，只是永远匹配不上，于是那一端在 Windows 上"就是装不上"，
        而 `--list` 会把这条 macOS 路径当作"预期位置"打给用户看。

        真要加，就同时给一条 %APPDATA% 候选（_expand 已经能展开），
        并在注释里写明那条是实测的还是推的。
        """
        mod = self._install()
        offenders = []
        for target in mod.TARGETS:
            for path in target.candidates():
                if "Library/Application Support" in path or "Library\\Application Support" in path:
                    offenders.append("%s → %s" % (target.label, path))
        self.assertEqual([], offenders,
                         "这些端只有 macOS 路径，Windows 上找不到：\n  "
                         + "\n  ".join(offenders))

    def test_doubao_is_not_an_install_target(self):
        """豆包工作**本版本不支持**，不许出现在安装目标里。

        不是路径没找对——路径是对的。是那个 `.skills` 由客户端按服务端清单同步，
        外来技能会被清掉（实测 11 个在 14 分钟后没了）。往一个会静默清空的目录
        里装东西，比不提供这个选项更糟：用户看到"安装成功"，一刻钟后技能不见了，
        而他不会把这两件事联系起来。
        """
        mod = self._install()
        keys = [t.key for t in mod.TARGETS]
        self.assertNotIn("doubao-work", keys,
                         "豆包工作又回到安装目标里了：%s" % keys)
        labels = " ".join(t.label for t in mod.TARGETS)
        self.assertNotIn("豆包", labels, "安装目标里出现了豆包：%s" % labels)

if __name__ == "__main__":
    unittest.main(verbosity=2)
