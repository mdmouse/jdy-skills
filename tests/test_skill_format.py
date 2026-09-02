#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验每个技能包符合 agentskills.io 骨架——三端都靠这套格式识别技能。

不依赖 pytest：`python3 tests/test_skill_format.py`
"""
import os
import re
import sys

# 和 tests/run_all.py 开头那几行一样：把 stdio 钉成 UTF-8。
# 本文件最后要打 "OK — %d 个技能包格式校验通过：…"，里面全是中文和一个长破折号。
# 被 run_all.py 当子进程跑时 stdout 是**管道**，Windows 上 Python 按 ANSI 代码页
# （runner 上是 cp1252）编码它——那一行 print 直接 UnicodeEncodeError，
# 于是"格式全部校验通过"以退出码 1 报了红。校验没问题，是打印崩了。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(ROOT, "skills")
sys.path.insert(0, os.path.join(ROOT, "_shared"))
from miniyaml import parse as parse_yaml  # noqa: E402
FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)

failures = []


def fail(skill, msg):
    failures.append("%s: %s" % (skill, msg))


def check_skill(path, name):
    md = os.path.join(path, "SKILL.md")
    if not os.path.isfile(md):
        return fail(name, "缺少 SKILL.md")
    with open(md, encoding="utf-8") as fh:
        text = fh.read()

    m = FM.match(text)
    if not m:
        return fail(name, "SKILL.md 开头必须是 --- 包裹的 YAML frontmatter")

    try:
        meta = parse_yaml(m.group(1)) or {}
    except Exception as exc:
        return fail(name, "frontmatter 解析失败：%s" % exc)

    if meta.get("name") != name:
        fail(name, "frontmatter name=%r 与目录名不一致" % meta.get("name"))
    desc = meta.get("description", "")
    if not desc:
        fail(name, "缺少 description —— 技能能否被触发全靠它")
    elif len(desc) < 40:
        fail(name, "description 过短（%d 字符），写清场景词与触发话术" % len(desc))

    # 版本号：商店装的是 zip，没有版本号就无从判断用户手上是哪一版。
    # 必须是 x.y.z——发布流程（build.py --dist 写的 SHA256SUMS）按它命名与追溯。
    version = meta.get("version")
    if not version:
        fail(name, "缺少 version —— 商店用户装的是 zip，没版本号就查不出他装的是哪一版")
    elif not re.match(r"\A\d+\.\d+\.\d+\Z", str(version)):
        fail(name, "version=%r 不是 x.y.z" % version)

    # 密钥零容忍：技能目录里不许出现疑似真密钥
    for dirpath, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(dirpath, f)
            try:
                with open(fp, encoding="utf-8") as fh:
                    body = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            for pat in (r"Bearer\s+[A-Za-z0-9]{24,}", r"api_key\"\s*:\s*\"[A-Za-z0-9]{16,}\""):
                if re.search(pat, body):
                    fail(name, "疑似硬编码密钥：%s" % os.path.relpath(fp, ROOT))


def check_vendored_kernel_is_current():
    """技能被单独复制安装时 _shared/ 不会跟着走，所以内核要 vendor 进技能包。
    这里确保 vendor 副本没有落后于 _shared/ 源码。"""
    import subprocess
    result = subprocess.run([sys.executable, os.path.join(ROOT, "build.py"), "--check"],
                            cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        fail("build", "vendor 副本与 _shared/ 不同步，请运行 `python3 build.py`\n" +
             result.stdout.strip())


def check_bootstrap_present(path, name):
    """引用内核的技能必须带 _bootstrap.py，否则安装后 import 会崩。"""
    scripts = os.path.join(path, "scripts")
    if not os.path.isdir(scripts):
        return
    uses_kernel = False
    for f in os.listdir(scripts):
        if f.endswith(".py") and f != "_bootstrap.py":
            with open(os.path.join(scripts, f), encoding="utf-8") as fh:
                if "jdy_client" in fh.read():
                    uses_kernel = True
    if uses_kernel and not os.path.exists(os.path.join(scripts, "_bootstrap.py")):
        fail(name, "脚本引用了 jdy_client 但缺少 scripts/_bootstrap.py")


def main():
    if not os.path.isdir(SKILLS_DIR):
        print("没有 skills/ 目录")
        return 1
    names = sorted(d for d in os.listdir(SKILLS_DIR)
                   if os.path.isdir(os.path.join(SKILLS_DIR, d)) and not d.startswith("."))
    if not names:
        print("skills/ 下没有技能")
        return 1
    for n in names:
        check_skill(os.path.join(SKILLS_DIR, n), n)
        check_bootstrap_present(os.path.join(SKILLS_DIR, n), n)
    check_vendored_kernel_is_current()

    if failures:
        print("FAIL (%d)" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("OK — %d 个技能包格式校验通过：%s" % (len(names), ", ".join(names)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
