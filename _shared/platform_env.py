# -*- coding: utf-8 -*-
"""跨端适配层：认出宿主，并找到一个**真能写**的地方放状态。

## 为什么不按平台名分支

三端的沙箱白名单各不相同，而我们只实测过其中一端：WorkBuddy 的
`sandbox.extraAllowWrite` 里没有 `~/.jdy`，但 `~/WorkBuddy AI/` 整体放行。
豆包工作与千问办公的客户端本机没装，白名单**未知**——给未知的沙箱写死一份
猜来的路径，装上去之后只会得到一种结果：看起来适配了，其实每次都落空。

所以这一层只做两件事，都不认平台名：

1. `detect_host()` —— 从**技能自己被装在哪**反推宿主。技能总是被复制到
   `<宿主配置目录>/skills/<技能名>/`，于是 `__file__` 本身就是证据。
   认得出就给个名字，认不出就老实说 unknown 并把安装根目录报出来——
   那正是下次要补进名单的事实，而不是一句"不支持"。
   **它只用于报告与安装提示，绝不用来决定行为。**

2. `resolve_state_home()` —— 按顺序实写探测，取第一个能写的。
   原来的做法是 `~/.jdy` 一失败就地放弃：字段缓存退成内存、审计日志直接丢。
   可 `~/.jdy` 写不了**不等于没地方写**——WorkBuddy 上同一时刻会话工作目录
   是可写的，宿主自己的配置目录多半也可写。批量审批不留痕不是沙箱的错，
   是没找过第二个地方。

约束同内核：仅标准库，兼容 Python 3.8。
"""

import os
import tempfile

STATE_HOME_ENV = "JDY_HOME"          # 显式指定状态目录，优先级最高
DEFAULT_STATE_HOME = os.path.expanduser("~/.jdy")
STATE_DIR_NAME = "jdy-state"         # 落在宿主配置目录下时用这个名字
PROBE_NAME = ".jdy_writable_probe"

# 宿主配置目录名 → (标识, 显示名, 这条是否已实测过)。
# **只用于给报告贴个标签**；匹配不上不影响任何行为。
# 未实测的条目来自调研，标 False——报告里会照实写"未实测"。
HOST_SIGNATURES = (
    (".workbuddy-ai", "workbuddy", "腾讯 WorkBuddy", True),
    (".claude", "claude-code", "Claude Code", True),
    (".codebuddy", "codebuddy-cli", "CodeBuddy CLI", True),
    # 千问办公：客户端 v1.0.2，配置根 ~/.qwenworkcn，内置技能就在它的 skills/ 下
    (".qwenworkcn", "qwenwork", "千问办公 QwenWork", True),
    # 豆包工作 v2.27.10：技能根是 **~/DoubaoWork**（首次启动时由客户端自己建出来的，
    # 不是 dot 目录，也不在 Application Support 里）。目录是客户端建的，
    # 但"它确实从这里加载"要等真机触发一次才算数。
    ("DoubaoWork", "doubao-work", "豆包工作", False),
    (".agents", "shared-store", "共享技能库（~/.agents）", True),
)

# 环境变量旁证。同样只进报告。第四位同样是"这一端是否已实测"。
ENV_SIGNATURES = (
    ("WORKBUDDY_CONFIG_DIR", "workbuddy", "腾讯 WorkBuddy", True),
    ("CODEBUDDY_PLUGIN_ROOT", "codebuddy-cli", "CodeBuddy CLI", True),
    ("CLAUDECODE", "claude-code", "Claude Code", True),
    ("CLAUDE_CODE_ENTRYPOINT", "claude-code", "Claude Code", True),
)

_STATE_HOME = None                   # 探测会真的建目录写文件，一个进程只做一次


# --------------------------------------------------------------------------
# 宿主识别
# --------------------------------------------------------------------------

SKILLS_DIR_NAMES = ("skills", ".skills")   # 认哪些目录名算"技能目录"


def match_host(root):
    """从安装根目录认宿主。返回 (id, 名字, 是否已实测)，认不出返回 None。

    **先看根目录名，再往上找祖先。** 加祖先这一层是被豆包工作逼出来的：
    它的技能目录是 `…/DoubaoWork/Default/.doubaowork/agent_mode/workspace/.skills/`，
    根目录名是 `workspace`——太通用，不能拿它当签名；但路径里确确实实有
    `DoubaoWork` 这一段，那就是证据。只比根目录名，这一端永远认不出来。

    就近优先：越深的祖先越具体。
    """
    base = os.path.basename(root)
    for marker, hid, hname, ok in HOST_SIGNATURES:
        if base == marker:
            return hid, hname, ok
    parts = os.path.normpath(root).split(os.sep)
    for part in reversed(parts[:-1]):          # 从近到远，跳过根目录名本身
        for marker, hid, hname, ok in HOST_SIGNATURES:
            if part == marker:
                return hid, hname, ok
    return None


def install_root(start=None):
    """技能被装在哪个宿主目录下。认不出返回 None。

    技能包的形状是固定的 `<宿主配置目录>/skills/<技能名>/scripts/...`，
    所以从本文件往上找一层"父目录叫 skills"的祖先，再往上两层就是宿主配置目录。
    这条链路在任何宿主上都成立，**包括我们还没见过的宿主**——
    这正是不写死路径也能适配的地方。
    """
    path = os.path.abspath(start or __file__)
    seen = set()
    while True:
        parent = os.path.dirname(path)
        if parent == path or parent in seen:
            return None
        seen.add(parent)
        # `.skills` 也要认：豆包工作的技能目录是
        # `…/DoubaoWork/Default/.doubaowork/agent_mode/workspace/.skills/`，
        # 点开头。只认 `skills` 的话，装到那儿的技能一跑就报「未识别的宿主」——
        # 而那一端恰恰是官方连接器覆盖不到、最需要认出来的。
        if os.path.basename(parent) in SKILLS_DIR_NAMES:
            root = os.path.dirname(parent)
            # `skills` 就在文件系统根上时没有宿主可言。判"是不是根"用 dirname(root) == root，
            # 不比较 os.sep：Windows 的根是 `D:\\`，和 os.sep 永远不相等，
            # 装在 `D:\\skills\\…` 时会把盘符当成宿主目录。
            return root if root and os.path.dirname(root) != root else None
        path = parent


def detect_host(start=None):
    """返回 {"id", "name", "root", "verified", "evidence"}。

    `verified=False` 意味着"目录名对上了名单，但这一端我们没实测过"——
    报告里必须照实说，不能让一个猜测长得像一条结论。
    """
    root = install_root(start)
    evidence = []
    host_id, name, verified = "unknown", "未识别的宿主", False

    if root:
        evidence.append("技能安装在 %s" % root)
        hit = match_host(root)
        if hit:
            host_id, name, verified = hit

    for env_name, hid, hname, is_verified in ENV_SIGNATURES:
        if os.environ.get(env_name):
            evidence.append("环境变量 %s 存在" % env_name)
            # 安装位置比环境变量硬：找到了 root 却不认识它，答案就是"不认识"。
            # 让一个顺手继承来的环境变量顶上去，等于把"这是个没见过的端"
            # 这条最该被看见的信息盖掉——而那正是要回填进名单的东西。
            if host_id == "unknown" and root is None:
                host_id, name, verified = hid, hname, is_verified

    return {"id": host_id, "name": name, "root": root,
            "verified": verified, "evidence": evidence}


# --------------------------------------------------------------------------
# 可写状态目录
# --------------------------------------------------------------------------

class StateHome(object):
    """一次探测的结果。**拿不到目录时 path 是 None**，调用方要自己兜底。

    `stable` 区分两种"能写"：
      * True  —— 换个会话再跑还找得回来（`~/.jdy`、宿主配置目录、$JDY_HOME）
      * False —— 只在这一轮有效（会话工作目录会变、临时目录会被清）
    去重状态与 ID 映射落在 stable=False 的地方，下一轮就等于没有——
    这件事必须说出来，不能让用户以为哨兵记住了。
    """

    def __init__(self, path, source, stable, tried, ephemeral=False):
        self.path = path
        self.source = source          # env / default / host / cwd / temp / none
        self.stable = stable
        self.ephemeral = ephemeral
        self.tried = tried            # [(候选路径, 为什么不行)]

    @property
    def ok(self):
        return self.path is not None

    def note(self):
        """给命令行打印的一句人话。没什么可说的时候返回 None。"""
        if not self.ok:
            return ("⚠️ 找不到任何可写目录（试过：%s）——本轮状态只存在内存里，"
                    "跑完即失。设 %s 指向一个可写目录可解决。"
                    % ("、".join(p for p, _ in self.tried), STATE_HOME_ENV))
        if self.source in ("env", "default"):
            return None                                   # 正常情况不啰嗦
        if self.ephemeral:
            return ("⚠️ %s 不可写，状态已改存临时目录 %s——**下一轮找不回来**。"
                    "要让状态跨轮次保留，设 %s 指向一个沙箱允许写的目录。"
                    % (DEFAULT_STATE_HOME, self.path, STATE_HOME_ENV))
        if not self.stable:
            return ("ℹ️ %s 不可写，状态改存会话工作目录 %s。"
                    "**换个会话工作目录就找不回来了**——要固定下来，设 %s。"
                    % (DEFAULT_STATE_HOME, self.path, STATE_HOME_ENV))
        return ("ℹ️ %s 不可写，状态改存宿主目录 %s（跨轮次有效）。"
                % (DEFAULT_STATE_HOME, self.path))


def _writable(path):
    """真写一次。**不能用 os.access**——沙箱的拒绝发生在系统调用层，
    access() 看的是 POSIX 权限位，两者不是一回事，它会说"能写"然后写崩。

    返回 None 表示可写，否则返回一句"为什么不行"。
    """
    probe = os.path.join(path, PROBE_NAME)
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
    except (OSError, IOError) as exc:
        return str(exc)
    try:
        os.remove(probe)
    except OSError:
        pass                                     # 建得出写得进就够了，删不掉不影响
    return None


def candidates(host_root=None, cwd=None):
    """按优先级列出候选。每项是 (路径, 来源, 是否跨轮次有效, 是否临时目录)。"""
    out = []
    explicit = os.environ.get(STATE_HOME_ENV)
    if explicit:
        out.append((os.path.expanduser(explicit), "env", True, False))
    out.append((DEFAULT_STATE_HOME, "default", True, False))
    if host_root:
        # 宿主自己的配置目录：它是宿主建的，沙箱放行它的概率远高于 ~/.jdy。
        # 这一条是**运行时推出来的**，不是写死的平台路径——所以对没见过的端也成立。
        out.append((os.path.join(host_root, STATE_DIR_NAME), "host", True, False))
    out.append((os.path.join(cwd or os.getcwd(), ".jdy"), "cwd", False, False))
    out.append((os.path.join(tempfile.gettempdir(), "jdy-state"), "temp", False, True))
    return out


def resolve_state_home(refresh=False, host_root=None, cwd=None):
    """找到第一个真能写的目录。探测有副作用（建目录），所以按进程缓存。

    显式设了 `$JDY_HOME` 却写不进去时**不静默降级**——用户指了个地方，
    我们改存别处而不吭声，就是把他的意图丢了。这一条只在 note() 里说不够，
    tried 里会留下原因，调用方照样能看到。
    """
    global _STATE_HOME
    # 传了 host_root/cwd 的调用是"问一个假设情形"（测试与探针都这么用），
    # 它的答案不该顶掉进程级缓存——否则一次假设查询就把真实落点改了。
    hypothetical = host_root is not None or cwd is not None
    if _STATE_HOME is not None and not refresh and not hypothetical:
        return _STATE_HOME
    if host_root is None:
        host_root = install_root()
    tried = []
    result = None
    for path, source, stable, ephemeral in candidates(host_root, cwd):
        why = _writable(path)
        if why is None:
            result = StateHome(path, source, stable, tried, ephemeral)
            break
        tried.append((path, why))
    if result is None:
        result = StateHome(None, "none", False, tried)
    if not hypothetical:
        _STATE_HOME = result          # refresh 也要更新缓存，否则后面 state_path() 拿的还是旧的
    return result


def state_path(*parts):
    """状态文件的绝对路径。目录不可写时返回 None——调用方必须处理这种情况。"""
    home = resolve_state_home()
    return os.path.join(home.path, *parts) if home.ok else None


# --------------------------------------------------------------------------
# 密钥配置：读和写是两件事，不能跟着状态目录一起搬
# --------------------------------------------------------------------------

def config_candidates():
    """密钥配置文件的查找顺序。

    **和状态目录分开。** 状态是我们写的，找个能写的地方就行；
    配置是用户写的，只能到用户放它的地方去找。把两者绑在一起，
    结果就是状态一降级、密钥跟着找不到了。
    """
    out = []
    explicit = os.environ.get(STATE_HOME_ENV)
    if explicit:
        out.append(os.path.join(os.path.expanduser(explicit), "config.json"))
    out.append(os.path.join(DEFAULT_STATE_HOME, "config.json"))
    return out


def find_config():
    """第一个存在的配置文件路径；都不存在时返回默认位置（供报错文案用）。"""
    for path in config_candidates():
        if os.path.exists(path):
            return path
    return config_candidates()[-1]
