#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把共享内核 vendor 进每个技能包。

为什么需要这一步：三端的安装方式都是**复制单个技能目录**
（~/.workbuddy/skills/、~/.qwenworkcn/skills/、豆包工作导入本地技能文件），
同级的 _shared/ 不会跟着走。技能若 import 仓库里的 _shared 就会在安装后崩掉。

所以构建时把 _shared/*.py 复制到每个技能的 scripts/_shared/，
技能内统一用 `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_shared"))` 引用。

    python3 build.py            # 同步
    python3 build.py --check    # 只校验是否同步（CI/测试用），不修改文件
    python3 build.py --dist dist  # 发布：vendor + 打 zip + 写 SHA256SUMS
"""
import argparse
import ast
import hashlib
import os
import re
import shutil
import sys


def _force_utf8_stdio():
    """把 stdout/stderr 钉成 UTF-8——理由见 skills/*/scripts/_bootstrap.py。

    build.py 今天打印的字符 GBK 全都编得动，所以它现在不会崩。钉住是为了
    让规则统一：**每个入口都钉**，没有例外名单——例外名单会烂，规则不会。
    """
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_stdio()

ROOT = os.path.dirname(os.path.abspath(__file__))
SHARED = os.path.join(ROOT, "_shared")
SKILLS = os.path.join(ROOT, "skills")
BANNER = "# 本文件由 build.py 从 _shared/ 自动生成，请勿直接编辑——改动会被覆盖。\n"


def shared_modules():
    if not os.path.isdir(SHARED):
        return []
    return sorted(f for f in os.listdir(SHARED) if f.endswith(".py"))


def skill_dirs():
    if not os.path.isdir(SKILLS):
        return []
    return sorted(d for d in os.listdir(SKILLS)
                  if os.path.isdir(os.path.join(SKILLS, d)) and not d.startswith("."))


def _imports(body, names, where):
    """源码里**真的 import 了**哪些内核模块。

    原来这里是正则搜模块名，于是**注释里提一句就算依赖**。
    hello-jdy 的 probe.py 里写了一句"这张表是 _shared/platform_env.py 的副本"，
    结果内核被 vendor 进了那个刻意零依赖的探针包——它恰恰是用来验证
    "这个端能不能跑技能"的，包里多一个内核就把结论污染了。
    改成看 AST：只认 `import X` 与 `from X import ...`。
    """
    try:
        tree = ast.parse(body, where)
    except SyntaxError:
        return set()
    wanted = set(names)
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                if head in wanted:
                    used.add(head)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                head = node.module.split(".")[0]
                if head in wanted:
                    used.add(head)
    return used


def needed_modules(scripts_dir, mods):
    """只 vendor 技能真正 import 的模块，**并递归带上它们自己依赖的内核模块**。

    hello-jdy 这类探针刻意只用标准库，塞进内核会让包变胖、也污染平台兼容性验证。
    判据是 **import 语句**，不是文本出现——注释里提到模块名不算依赖。

    传递闭包是后补的：原来只看技能源码提到了谁。内核模块之间将来一旦互相 import
    （比如 jdy_client 用到 miniyaml），被依赖的那个不会被 vendor 进去，
    而这在仓库内开发时**完全看不出来**——仓库根的 _shared/ 一直在 sys.path 上，
    只有装到用户机器上才炸 ImportError。
    """
    names = [os.path.splitext(m)[0] for m in mods]
    used = set()
    for dirpath, dirnames, files in os.walk(scripts_dir):
        dirnames[:] = [d for d in dirnames if d != "_shared"]     # 不扫 vendor 目录自身
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    body = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            used |= _imports(body, names, f)
    pending = list(used)
    while pending:                                    # 内核模块之间的依赖，递归补齐
        name = pending.pop()
        try:
            with open(os.path.join(SHARED, name + ".py"), encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            continue
        for dep in _imports(body, names, name + ".py") - used:
            used.add(dep)
            pending.append(dep)
    return sorted(m + ".py" for m in used)


def vendor(check=False):
    mods = shared_modules()
    if not mods:
        print("_shared/ 下没有模块，无需构建")
        return 0
    stale, synced, touched = [], 0, 0
    for skill in skill_dirs():
        scripts = os.path.join(SKILLS, skill, "scripts")
        if not os.path.isdir(scripts):
            continue
        target = os.path.join(scripts, "_shared")
        wanted = needed_modules(scripts, mods)
        if not wanted:
            if os.path.isdir(target) and not check:
                shutil.rmtree(target)                              # 不再需要就清掉
                print("  清理 %s（该技能未引用内核）" % os.path.relpath(target, ROOT))
            continue
        touched += 1
        for mod in wanted:
            src = os.path.join(SHARED, mod)
            dst = os.path.join(target, mod)
            with open(src, encoding="utf-8") as fh:
                content = BANNER + fh.read()
            current = None
            if os.path.exists(dst):
                with open(dst, encoding="utf-8") as fh:
                    current = fh.read()
            if current == content:
                synced += 1
                continue
            rel = os.path.relpath(dst, ROOT)
            if check:
                stale.append(rel)
                continue
            os.makedirs(target, exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(content)
            print("  同步 %s" % rel)
            synced += 1
    if not check:
        # 打包前清掉字节码：技能目录会被整个复制/打 zip 分发，
        # 里面带着别人机器路径的 .pyc 既无用也不干净
        for skill in skill_dirs():
            for dirpath, dirnames, _ in os.walk(os.path.join(SKILLS, skill)):
                for d in list(dirnames):
                    if d == "__pycache__":
                        shutil.rmtree(os.path.join(dirpath, d))
                        dirnames.remove(d)

    if check and stale:
        print("以下 vendor 副本已过期，请运行 `python3 build.py`：")
        for s in stale:
            print("  - " + s)
        return 1
    print("OK — %d 个技能引用内核，%d 份副本%s（共 %d 个技能包）"
          % (touched, synced, "已校验" if check else "已同步", len(skill_dirs())))
    return 0


def skill_version(name):
    """从 SKILL.md 的 frontmatter 里读版本号。tests/test_skill_format.py 保证它存在。"""
    md = os.path.join(SKILLS, name, "SKILL.md")
    with open(md, encoding="utf-8") as fh:
        head = fh.read().split("---", 2)[1]
    m = re.search(r"^version:\s*(\S+)\s*$", head, re.M)
    return m.group(1) if m else "0.0.0"


# 两个渠道要的 zip 顶层目录不一样，所以同一批技能要打两种布局：
#   github     `<name>.zip`            包内 `<name>/SKILL.md`
#              —— GitHub Release、千问办公「导入本地技能文件」
#   workbuddy  `<name>-workbuddy.zip`  包内 `skills/<name>/SKILL.md`
#              —— 腾讯 WorkBuddy 开放平台「技能」渠道，最外层多一级 skills/
# 传错布局的后果是**上传时才报错**，本地怎么看都是对的，所以两种都打、都写进校验和。
LAYOUTS = {
    "github": {"arc_prefix": "", "name_suffix": ""},
    "workbuddy": {"arc_prefix": "skills", "name_suffix": "-workbuddy"},
}


def dist(out_dir, layout="github"):
    """vendor → 打 zip → 写校验和。**发布只走这一条路。**

    手工 `install.py --zip` 打出来的包没有校验和、也不保证 vendor 是新的——
    dist/ 是 gitignore 的产物目录，没人盯着它，于是 08-27 打的那个 v0.1.0 探针
    在豆包工作的「我的技能」里安安静静躺了好几天。所以：
      * 先 vendor，保证包里的内核不是旧的；
      * 打完连版本号和 sha256 一起写进 SHA256SUMS，用户报问题时能对上是哪一版。
    """
    if vendor(check=False) != 0:
        return 1
    sys.path.insert(0, ROOT)
    import install                       # 打包逻辑只此一份，不再抄一遍
    names = install.skill_names()
    wanted = list(LAYOUTS) if layout == "both" else [layout]
    print("打包 %d 个技能到 %s/（布局：%s）"
          % (len(names), out_dir, "、".join(wanted)))
    stems = []                       # 产出的 zip（不含 .zip），按这个写校验和
    for one in wanted:
        opt = LAYOUTS[one]
        install.make_zip(names, out_dir, **opt)
        stems += [(name, name + opt["name_suffix"]) for name in names]

    # SHA256SUMS 必须是**严格两列**的标准格式，否则 `shasum -c` 读不了。
    # 版本号想搭个便车写在第三列——那样文件是好看了，核对命令直接报
    # "No such file or directory"。版本另写 MANIFEST.txt。
    #
    # **两种布局都要进这两个文件**：只给其中一种写校验和，另一种就成了
    # 没人核得了的产物——而它恰恰是要传给审核方的那一个。
    sums, manifest = [], []
    for name, stem in sorted(stems, key=lambda t: t[1]):
        zp = os.path.join(out_dir, "%s.zip" % stem)
        digest = hashlib.sha256(open(zp, "rb").read()).hexdigest()
        sums.append("%s  %s.zip" % (digest, stem))
        manifest.append("%-24s v%-8s %s" % (stem, skill_version(name), digest[:16]))
    # newline="\n" 不能省：文本模式默认走 os.linesep，**Windows 上写出来是 CRLF**。
    # `shasum -a 256 -c SHA256SUMS` 读到的文件名就带着一个尾随的 \r，
    # 于是报 `sha256sum: 'hello-jdy.zip'$'\r': No such file or directory`——
    # 校验和本身完全正确，核对命令却全线失败，而文件用编辑器打开看着一模一样。
    # MANIFEST.txt 同理：它会被贴进发布说明，CRLF 在别处一样碍事。
    for fname, lines in (("SHA256SUMS", sums), ("MANIFEST.txt", manifest)):
        with open(os.path.join(out_dir, fname), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
    print("已写 %s/SHA256SUMS 与 MANIFEST.txt（%d 个包）" % (out_dir, len(sums)))
    print("核对：cd %s && shasum -a 256 -c SHA256SUMS" % out_dir)
    return 0


def main():
    # 用 argparse 而不是 `"--check" in sys.argv`：文档里写到的参数有一条测试
    # 逐个核对它真的存在（test_docs_match_cli），而那条测试是读 argparse 声明的。
    # 手搓参数解析的脚本在它眼里等于"没有任何参数"。
    ap = argparse.ArgumentParser(description="把共享内核 vendor 进每个技能包")
    ap.add_argument("--check", action="store_true",
                    help="只校验 vendor 副本是否与 _shared/ 同步，不修改文件")
    ap.add_argument("--dist", metavar="DIR",
                    help="发布：vendor + 打 zip + 写 SHA256SUMS 到该目录")
    ap.add_argument("--layout", choices=sorted(LAYOUTS) + ["both"],
                    default="github",
                    help="zip 内的顶层目录布局（配合 --dist）："
                         "github=<name>/（默认，GitHub Release 与千问办公）、"
                         "workbuddy=skills/<name>/（WorkBuddy 开放平台技能渠道）、"
                         "both=两种都打")
    args = ap.parse_args()
    if args.dist:
        return dist(args.dist, args.layout)
    return vendor(check=args.check)


if __name__ == "__main__":
    sys.exit(main())
