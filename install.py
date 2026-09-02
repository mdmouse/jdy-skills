#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把技能装进各端，或打成可手工导入的 zip。

默认**复制真实目录**到各端，不用符号链接。

本机 ~/.codebuddy/skills 里原有的 12 个技能都是指向 ~/.agents/skills 的软链，
一开始照此办理，结果 WorkBuddy 完全看不到新装的技能——多个端的技能扫描是否
跟随符号链接并无保证。复制虽然占空间、改动要重装，但每个端都认。
想用软链走 --link。

    python3 install.py                  # 装全部技能到共享库并复制到检测到的各端
    python3 install.py hello-jdy        # 只装指定技能
    python3 install.py --list           # 只看检测到哪些端
    python3 install.py --discover       # 在本机搜宿主的技能目录（装了新端先跑这个）
    python3 install.py --target '某客户端=~/某处/skills'  # 手工指定一个端，可重复
    python3 install.py --zip dist/      # 打 zip（千问办公「导入本地技能文件」用）
                                        # 发版请用 `python3 build.py --dist dist`（带校验和）
    python3 install.py --uninstall      # 移除本仓库装过的技能
"""
import argparse
import collections
import os
import shutil
import sys
import textwrap
import zipfile


def _force_utf8_stdio():
    """把 stdout/stderr 钉成 UTF-8。

    Windows 中文控制台默认 GBK，打印 ✅ / ⬜ 这类符号会抛 UnicodeEncodeError
    把整个脚本崩掉——不是显示成乱码，是直接退出。三端主力用户在 Windows，
    所以这一句必须跑在任何 print 之前。

    宿主把 stdout 换成了非 TextIOWrapper 的对象（或 pythonw 下是 None）时
    reconfigure 不存在，静默跳过——不能因为修不了编码反而崩掉。

    本脚本不经过 scripts/_bootstrap.py（它是独立入口），所以这份是自带的副本。
    """
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_stdio()

ROOT = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(ROOT, "skills")
STORE = os.path.expanduser("~/.agents/skills")

# 署名。install.py 只在仓库里跑（它就是把仓库装出去的那一步），
# 所以直接从 _shared/ 取唯一来源，不抄常量。
sys.path.insert(0, os.path.join(ROOT, "_shared"))
import brand  # noqa: E402


def _expand(path):
    """展开 ~ 和环境变量。

    Windows 的宿主目录写成 %APPDATA%\\... ——`expanduser` 不认百分号变量，
    只有 `expandvars` 认；反过来 POSIX 上 `expandvars` 会原样留下 %APPDATA%，
    那条候选自然 isdir 不中，不会误判。两个都调，顺序无所谓。
    """
    return os.path.expanduser(os.path.expandvars(path))

# 各端的技能目录。**已实测的和没实测的分开标**——
# 一条猜来的路径混在实测结论里，装完之后没人分得清"没装上"是路径错了还是端不支持。
#
# 每个端三样东西：
#   env       —— 环境变量覆盖。永远优先，客户端装在别处的用户不必改代码。
#                 `*_CONFIG_DIR` 类的是宿主自己的变量，值是**配置目录**，要再拼 skills；
#                 `JDY_SKILLS_DIR_*` 是本仓库的约定，值就是**技能目录**本身。
#   paths     —— 候选目录，**只在已存在时才用**，绝不凭空创建（may_create 例外，见下）。
#   verified  —— 这个落点是不是在真机上验过。没验过的照实标，--list 里会写出来。
#
# 千问办公的路径是 2026-08-31 装上客户端后 `--discover` 扫出来的实测值：
#   千问办公 v1.0.2 → ~/.qwenworkcn/skills（内置的 12 个技能就在这儿，形态与我们一致）
# 「目录对」和「它确实从这里加载」是两回事，后者要在客户端里真触发一次才算数——
# 千问办公 2026-09-01 已在客户端内触发验证通过。

class Target(object):
    def __init__(self, label, key, env, paths, verified, may_create=False,
                 caution=None):
        self.label = label
        self.key = key
        self.env = env                # [(变量名, 'config' | 'skills')]
        self.paths = paths
        self.verified = verified
        self.may_create = may_create
        # 这一端有没有"装了也不一定留得住"之类的坑。有就每次装完都说一遍——
        # 只写在文档里，下一个人照样会以为装完就完事了。
        self.caution = caution

    def candidates(self):
        """候选路径。

        现存的端用的都是 `~/.xxx` 这种 home 相对路径——`expanduser` 在 Windows 上
        返回 C:\\Users\\<user>\\.xxx，本来就是对的，不需要分平台。
        哪天有端把配置塞进 macOS 的 Application Support（或 Windows 的 %APPDATA%），
        再按平台分支；`_expand()` 已经能展开 %VAR%，tests/test_windows.py 守着这条。
        """
        return list(self.paths)

    def resolve(self):
        """返回 (路径, 来源说明)。环境变量优先，其次第一个**已存在**的候选，
        都没有就退回第一个候选（用于打印"没找到，预期在这里"）。"""
        for name, kind in self.env:
            raw = os.environ.get(name)
            if raw:
                base = _expand(raw)
                return (os.path.join(base, "skills") if kind == "config" else base,
                        "环境变量 %s" % name)
        cands = self.candidates()
        for path in cands:
            full = _expand(path)
            if os.path.isdir(full):
                return full, "候选目录"
        return _expand(cands[0]), "候选目录（不存在）"


TARGETS = [
    Target("Claude Code", "claude-code",
           [("JDY_SKILLS_DIR_CLAUDE", "skills"), ("CLAUDE_CONFIG_DIR", "config")],
           ["~/.claude/skills"], verified=True),
    # ↓ WorkBuddy 真正扫描的用户技能目录（~/.workbuddy-ai/skills 内的迁移标记
    #   文件 scanned:0 证实它扫这里）。这是 V3 唯一有效的落点。
    #   注意**不是** ~/.workbuddy——那个目录只有 logs 和 device-id。
    Target("腾讯 WorkBuddy", "workbuddy",
           [("JDY_SKILLS_DIR_WORKBUDDY", "skills"), ("WORKBUDDY_CONFIG_DIR", "config")],
           ["~/.workbuddy-ai/skills"], verified=True, may_create=True),
    # skill-creator 文档说用户技能放这儿，但实测 WorkBuddy 不从这里加载。
    # 保留是因为 CodeBuddy CLI 认（WorkBuddy 内置的那个 CLI 读的就是它）。
    Target("CodeBuddy CLI", "codebuddy-cli",
           [("JDY_SKILLS_DIR_CODEBUDDY", "skills")],
           ["~/.codebuddy/skills"], verified=True),
    # 豆包工作**本版本不支持**，所以这里没有它的条目。
    #
    # 不是没找对路径——路径找对了：`~/Library/Application Support/DoubaoWork/
    # Default/.doubaowork/agent_mode/workspace/.skills`（2026-09-01 实测）。
    # 问题是那个 `.skills` 由客户端按服务端清单同步（偏好 remote_skill_install_info_v2），
    # **外来技能会被清掉**——实测装进去的 11 个在 14 分钟后没了。
    #
    # 往一个会静默清空的目录里装东西，比不提供这个选项更糟：用户看到"安装成功"，
    # 一刻钟后技能不见了，而他不会把这两件事联系起来。
    #
    # 那一端唯一留得住的路是客户端「技能中心 → 导入本地技能」（文件落在同级的
    # `.user_skills/`），**只能人在界面上做**，装不进去也不该由这里假装能装。
    # 完整实测记录见 docs/platform-compat-matrix.md V1。
    Target("千问办公 QwenWork", "qwenwork",
           [("JDY_SKILLS_DIR_QWENWORK", "skills"), ("QWENWORK_CONFIG_DIR", "config")],
           # 2026-09-01 客户端内触发实测通过：会话日志里有实际执行的命令
           # `python3 ~/.qwenworkcn/skills/hello-jdy/scripts/probe.py` 与 TRACK_SKILL 结论行。
           ["~/.qwenworkcn/skills"], verified=True),
]


def skill_names():
    if not os.path.isdir(SKILLS_DIR):
        return []
    return sorted(d for d in os.listdir(SKILLS_DIR)
                  if os.path.isdir(os.path.join(SKILLS_DIR, d)) and not d.startswith("."))


def detect(create_missing=False, extra=()):
    """返回 [(标签, 路径, 是否可用, 来源, 是否实测过)]。

    端目录不存在通常意味着该端没装，不擅自创建；但标了 may_create 的目录
    （WorkBuddy 的技能目录）是"用过才会出现"，装技能时可以补建。

    **环境变量／--target 指定的路径一律当成"用户说了算"**：他既然指了，
    就按他说的建。猜来的候选路径则相反——不存在就跳过，绝不凭空造一个
    看起来装好了、其实哪个端都不读的目录。
    """
    found = []
    for t in list(TARGETS) + list(extra):
        path, source = t.resolve()
        exists = os.path.isdir(path)
        user_said = source.startswith("环境变量") or source == "--target"
        if not exists and create_missing and (t.may_create or user_said):
            os.makedirs(path, exist_ok=True)
            exists = True
        found.append(Found(t.label, path, exists, source, t.verified, t.caution))
    return found


# 用具名元组而不是裸元组。**这一条是踩出来的**：给 Target 加了 caution 字段、
# detect() 多返回一项之后，三个调用点我只改了两个——`--uninstall` 和 `--discover`
# 当场 ValueError，而**没有任何测试碰过它们**。裸元组把"加一个字段"变成了
# 一次跨全文件的手工同步；具名元组让调用点按名字取，加字段不再牵动谁。
Found = collections.namedtuple("Found", "label path exists source verified caution")


def stale_zips(dist_dir="dist"):
    """dist/ 里的 zip 比 skills/ 旧多少。返回过期的文件名列表。

    **这一条是踩出来的**：豆包工作的「导入本地技能」把 `dist/hello-jdy.zip` 导了进去，
    而那个 zip 是 08-27 打的——里面是 **v0.1.0** 的探针，没有 C10、也没有后来那个
    崩溃修复。界面上看它好端端地列在"我的技能"里，跑出来的却是几个版本前的结论。
    dist/ 是 gitignore 的产物目录，没人盯着它；构建校验也只管 skills/ 里的 vendor 副本。
    所以在**要拿它去导入的地方**把话说出来。
    """
    if not os.path.isdir(dist_dir):
        return []
    newest = 0
    for dirpath, dirnames, files in os.walk(SKILLS_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
            except OSError:
                pass
    out = []
    for f in sorted(os.listdir(dist_dir)):
        if not f.endswith(".zip"):
            continue
        try:
            if os.path.getmtime(os.path.join(dist_dir, f)) < newest:
                out.append(f)
        except OSError:
            pass
    return out

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def _clear(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def install(names, link=False, extra=()):
    os.makedirs(STORE, exist_ok=True)
    for name in names:
        dst = os.path.join(STORE, name)
        _clear(dst)
        shutil.copytree(os.path.join(SKILLS_DIR, name), dst, ignore=IGNORE)
        print("  共享库  %s" % dst)

    for f in detect(create_missing=True, extra=extra):
        label, path, exists, verified, caution = (f.label, f.path, f.exists,
                                                  f.verified, f.caution)
        if not exists:
            print("  跳过    %-16s（%s 不存在——该端多半没装；装了的话用 --discover 找，"
                  "或 --target '%s=<路径>'）" % (label, path, label))
            continue
        for name in names:
            dst = os.path.join(path, name)
            _clear(dst)
            if link:
                os.symlink(os.path.relpath(os.path.join(STORE, name), path), dst)
            else:
                shutil.copytree(os.path.join(SKILLS_DIR, name), dst, ignore=IGNORE)
        mark = "" if verified else "　⚠️ 该端落点未实测，装完请在客户端里实际触发一次确认"
        print("  %s  %-16s %s%s" % ("已软链" if link else "已复制", label, path, mark))
        if caution:
            for line in textwrap.wrap(caution, 76):
                print("          " + line)


def uninstall(names, extra=()):
    for name in names:
        for path in [STORE] + [f.path for f in detect(extra=extra) if f.exists]:
            t = os.path.join(path, name)
            if os.path.islink(t) or os.path.isfile(t):
                os.remove(t)
                print("  移除    %s" % t)
            elif os.path.isdir(t):
                shutil.rmtree(t)
                print("  移除    %s" % t)


def discover():
    """在本机搜"看起来像 Agent 宿主技能目录"的地方。

    为什么需要它：豆包工作与千问办公的客户端本机没装，它们的技能目录**只能靠猜**，
    而猜来的路径写进代码是有害的——装完不报错，也不生效。
    与其猜，不如装上客户端之后让机器自己找：宿主的技能目录一律叫 `skills`，
    且位于宿主自己的配置目录下，这个形状是可搜的。

    只读，不改任何东西。输出直接就是 --target 的参数。
    """
    home = os.path.expanduser("~")
    roots = [home, os.path.join(home, "Library", "Application Support")]
    known = {os.path.realpath(f.path) for f in detect()}
    hits, seen = [], set()
    for root in roots:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            base = os.path.join(root, entry)
            cand = os.path.join(base, "skills")
            real = os.path.realpath(cand)
            if real in seen or not os.path.isdir(cand):
                continue
            seen.add(real)
            try:
                n = len([d for d in os.listdir(cand)
                         if os.path.isdir(os.path.join(cand, d))])
            except OSError:
                n = -1
            try:
                mtime = os.path.getmtime(cand)
            except OSError:
                mtime = 0
            hits.append((cand, n, real in known, mtime))
    # 按最近改动排前面。本机有 50 来个 agent 都建了 skills/ 目录，一股脑列出来
    # 等于没列；而**刚装的客户端目录一定是最新的**——豆包工作就是这么找出来的。
    hits.sort(key=lambda h: (-h[3], h[0]))
    return hits


def make_zip(names, out_dir, arc_prefix="", name_suffix=""):
    """每个技能打一个 zip。

    默认（两个参数都空）：`<name>.zip`，压缩包内以技能名为顶层目录——
    千问办公的「导入本地技能文件」和 GitHub Release 需要这种形态。

    **两种布局，因为两个渠道要的顶层目录不一样**：腾讯 WorkBuddy 开放平台的
    「技能」渠道要 `skills/<name>/SKILL.md`，最外层多一级 `skills/`。
    同一个包换个顶层目录名就得重打一次，所以这里参数化而不是复制一份函数：
      * `arc_prefix="skills"` → 包内路径变成 `skills/<name>/...`
      * `name_suffix="-workbuddy"` → 文件名变成 `<name>-workbuddy.zip`
    两个都留空时行为与改动前**逐字节一致**。
    """
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for name in names:
        src = os.path.join(SKILLS_DIR, name)
        out = os.path.join(out_dir, "%s%s.zip" % (name, name_suffix))
        top = os.path.join(arc_prefix, name) if arc_prefix else name
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, files in os.walk(src):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for f in files:
                    if f.endswith(".pyc"):
                        continue
                    full = os.path.join(dirpath, f)
                    arc = os.path.join(top, os.path.relpath(full, src))
                    zf.write(full, arc)
        made.append((out, os.path.getsize(out)))
        print("  %s（%.1f KB）" % (out, os.path.getsize(out) / 1024.0))
    return made


def main():
    ap = argparse.ArgumentParser(description="安装/打包简道云技能")
    ap.add_argument("names", nargs="*", help="技能名，缺省为全部")
    ap.add_argument("--list", action="store_true", help="只检测各端目录")
    ap.add_argument("--zip", metavar="DIR", help="打 zip 到该目录，不做安装")
    ap.add_argument("--uninstall", action="store_true",
                    help="移除已安装的技能。**全局生效**：共享库与所有已检测到的端"
                         "一起清；--target 只是多加一个端，不会把删除限定到它")
    ap.add_argument("--link", action="store_true",
                    help="用符号链接代替复制（WorkBuddy 实测看不到软链技能，慎用）")
    ap.add_argument("--discover", action="store_true",
                    help="在本机搜宿主的技能目录，只读。装了新端而 --list 认不出时用它")
    ap.add_argument("--target", action="append", metavar="标签=路径", default=[],
                    help="手工指定一个端的技能目录，可重复。名单里没有的端用这个装")
    args = ap.parse_args()

    extra = []
    for spec in args.target:
        if "=" not in spec:
            sys.stderr.write("--target 要写成 标签=路径，比如 --target '某客户端=~/x/skills'；"
                             "收到的是：%s\n" % spec)
            return 2
        label, _, path = spec.partition("=")
        label, path = label.strip(), path.strip()
        if not label or not path:
            sys.stderr.write("--target 的标签和路径都不能为空：%s\n" % spec)
            return 2
        # 用户明确指定的路径，verified 记 False —— 他指的地方我们没验过，
        # 装完照样提示"去客户端里触发一次确认"。
        extra.append(Target(label, "custom", [], [path], verified=False))

    if args.discover:
        hits = discover()
        print("本机疑似 Agent 宿主技能目录（只读扫描，未做任何改动）：")
        if not hits:
            print("  一个都没找到。宿主要么没装，要么技能目录不叫 skills——"
                  "去客户端的「技能/插件」设置里看它写的路径，再用 --target 指定。")
        import time as _time
        for path, n, known, mtime in hits:
            print("  %s %-46s %s  %s"
                  % ("已在名单" if known else "新发现　", path,
                     ("%d 个技能" % n) if n >= 0 else "读不出内容",
                     _time.strftime("%Y-%m-%d %H:%M", _time.localtime(mtime))))
        unknown = [h for h in hits if not h[2]]
        if unknown:
            top = unknown[:10]
            print("\n名单里没有的，按最近改动排序（刚装的客户端一定在最前面）。"
                  "若其中某个属于你要装的端：")
            for path, _n, _k, _m in top:
                print("  python3 install.py --target '<端名>=%s'" % path)
            if len(unknown) > len(top):
                print("  …… 另有 %d 个更早的没列出来（多半是别的 Agent 的技能目录）"
                      % (len(unknown) - len(top)))
            print("确认之后把路径回填进 install.py 的 TARGETS，就不用每次都传了。")
        return 0

    available = skill_names()
    names = args.names or available
    unknown = [n for n in names if n not in available]
    if unknown:
        sys.stderr.write("没有这些技能：%s\n现有：%s\n" % ("、".join(unknown), "、".join(available)))
        return 2

    if args.list:
        print("检测到的各端技能目录：")
        for f in detect(extra=extra):
            tag = "" if f.verified else "　落点未实测"
            print("  %s %-18s %-42s %s%s"
                  % ("✅" if f.exists else "—", f.label, f.path, f.source, tag))
        stale = stale_zips()
        if stale:
            print("\n⚠️ dist/ 里有 %d 个 zip 比 skills/ 旧（%s%s）——"
                  "各端的「导入本地技能」如果导的是它，装进去的是过期版本。"
                  "重打：`python3 install.py --zip dist`"
                  % (len(stale), "、".join(stale[:3]), " …" if len(stale) > 3 else ""))
        print("\n共享库：%s%s" % (STORE, "" if os.path.isdir(STORE) else "（尚未创建）"))
        print("没找到的端：装上客户端后跑 `--discover`，或用 `--target '端名=路径'`；"
              "也可以设环境变量（见 TARGETS 里各端的变量名）。")
        return 0

    if args.zip:
        print("打包 %d 个技能：" % len(names))
        make_zip(names, args.zip)
        print("\n用于千问办公的「导入本地技能文件」。")
        return 0

    if args.uninstall:
        print("移除 %d 个技能：" % len(names))
        uninstall(names, extra=extra)
        return 0

    print("安装 %d 个技能：%s" % (len(names), "、".join(names)))
    install(names, link=args.link, extra=extra)
    print("\n提示：装完后到各端用自然语言触发，例如「跑一下简道云探针」。")
    if brand.enabled():
        print(brand.LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
