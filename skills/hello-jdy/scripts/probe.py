#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简道云连接诊断：装了简道云技能却用不了时，定位断在哪一环。

十项检查：能不能跑脚本、能不能读到密钥、能不能连上 api.jiandaoyun.com、
状态往哪儿落、这一端装没装官方「AI 连接」。设计约束（都是有意的）：
  * 只用标准库——沙箱大概率禁止 pip 安装，任何第三方依赖都会把
    "平台不支持" 和 "依赖装不上" 两种失败混在一起。
  * 无 API Key 也能跑出结论——V2/V4 要测的是出网与读文件本身，
    密钥缺失不该让探针罢工。
  * 兼容 Python 3.8——各端内置解释器版本未知，不用 3.9+ 语法。
  * 只读。探针绝不写简道云的数据。
退出码恒为 0：非零退出会被部分平台当成执行失败而吞掉输出。
"""

import argparse
import json
import os
import platform
import re
import socket
import ssl
import sys
import tempfile
import urllib.error
import urllib.request


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

API_BASE = "https://api.jiandaoyun.com/api/v5"
API_HOST = "api.jiandaoyun.com"
CONFIG_PATH = os.path.expanduser("~/.jdy/config.json")
ENV_KEY = "JDY_API_KEY"
STATE_HOME_ENV = "JDY_HOME"
STATE_DIR_NAME = "jdy-state"

# 宿主签名表。**这是 _shared/platform_env.py 那张表的副本**——探针刻意零依赖，
# 它要能在内核还没落地的端上跑，所以不能 import 内核。副本会漂，
# tests/test_platform_env.py 里有一条守卫测试逐条比对两张表，漂了就红。
# 本版本不支持的端：**识别照常，但报告里要当面说清**。
# 把签名删掉更省事，代价是用户在那一端跑诊断只会看到"未识别的宿主"——
# 而他要的恰恰是"我这儿到底行不行"的答案。认得出却不支持，就照实说。
UNSUPPORTED_HOSTS = {
    "doubao-work": ("豆包工作", "它的 .skills 由宿主按服务端清单同步，"
                    "复制进去的技能会被清掉（实测 11 个在 14 分钟后消失）。"
                    "唯一留得住的路是它自己的「技能中心 → 导入本地技能」，"
                    "只能人在界面上操作。"),
}

HOST_SIGNATURES = (
    (".workbuddy-ai", "workbuddy", "腾讯 WorkBuddy", True),
    (".claude", "claude-code", "Claude Code", True),
    (".codebuddy", "codebuddy-cli", "CodeBuddy CLI", True),
    (".qwenworkcn", "qwenwork", "千问办公 QwenWork", True),
    ("DoubaoWork", "doubao-work", "豆包工作", False),
    (".agents", "shared-store", "共享技能库（~/.agents）", True),
)
ENV_SIGNATURES = (
    ("WORKBUDDY_CONFIG_DIR", "workbuddy", "腾讯 WorkBuddy", True),
    ("CODEBUDDY_PLUGIN_ROOT", "codebuddy-cli", "CodeBuddy CLI", True),
    ("CLAUDECODE", "claude-code", "Claude Code", True),
    ("CLAUDE_CODE_ENTRYPOINT", "claude-code", "Claude Code", True),
)

# 连接器配置文件的文件名线索。**这不是第二份平台清单**——去哪儿找由
# HOST_SIGNATURES（已有、且有守卫测试盯着不漂）和技能自己的安装位置决定，
# 这里只回答"那个目录里哪些文件像 MCP 配置"。
CONNECTOR_FILE_HINTS = ("mcp.json", ".mcp.json", "mcp-adaptor.config",
                        "mcp_settings.json", "connectors.json")
CONNECTOR_SCAN_DEPTH = 4
CONNECTOR_SCAN_SKIP = frozenset((
    "skills", "node_modules", "cache", "caches", "logs", "blobs", "binaries",
    "tmp", "temp", "__pycache__", "history", "projects", "sessions", "app",
    "artifact-index", "changes-index", "audit-log", "clipboard-images", "bin",
))
CONNECTOR_MAX_FILES = 40
CONNECTOR_MAX_BYTES = 512 * 1024
JDY_HINTS = ("jiandaoyun", "简道云")
# 认证类 header 名。名字对不上时还有长度兜底——凭证一般不短。
SECRET_HEADER = re.compile(r"authorization|token|key|secret|cookie|session|sign", re.I)
SECRET_VALUE_LEN = 20

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_MARK = {PASS: "[ OK ]", FAIL: "[FAIL]", WARN: "[WARN]", SKIP: "[SKIP]"}


def mask(secret):
    """密钥永远不整串输出——报告会被贴进验证表和聊天记录。"""
    if not secret:
        return None
    if len(secret) <= 12:
        return secret[:2] + "*" * (len(secret) - 2)
    return "%s...%s (len=%d)" % (secret[:4], secret[-4:], len(secret))


def _blur(text):
    return "%s…%s" % (text[:4], text[-4:]) if len(text) > 12 else "***"


def mask_url(url):
    """URL 里的凭证不能进报告。**这是 `_shared/webhook.py` 里 mask() 的副本**——
    探针刻意零依赖（它要在内核还没落地的端上跑），所以只能抄一份；
    tests/test_platform_env.py 有一条守卫测试逐个 URL 比对两份的输出，漂了就红。

    抄的时候连它修过的那个坑一起抄：查询串型（`?key=` / `?access_token=`）
    和**路径型**（凭证是路径最后一段、压根没有 `=`）是同一件事的两半，
    只遮查询串等于一个字都没遮。
    """
    url = re.sub(r"(key|access_token)=([^&]+)",
                 lambda m: "%s=%s" % (m.group(1), _blur(m.group(2))), url)
    url = re.sub(r"(/(?:hook|hooks|robot|webhook|services)/)([A-Za-z0-9_-]{16,})"
                 r"(?=[/?#]|$)", lambda m: m.group(1) + _blur(m.group(2)), url)
    url = re.sub(r"/([0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}|[A-Za-z0-9_-]{32,})(?=[/?#]|$)",
                 lambda m: "/" + _blur(m.group(1)), url)
    return url


def mask_headers(headers):
    """header 名留着、值该遮就遮。

    全遮掉就看不出这条连接是连谁家的了，而遮蔽的目的是"还认得出目标、
    但拿不走凭证"。千问办公的 `mcp-adaptor.config` 本机实测带 64 位 token 和
    `x-api-key`，WorkBuddy 的 `staticHeaders` 同样可能带。
    """
    out = {}
    if not isinstance(headers, dict):
        return out
    for k, v in headers.items():
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        if SECRET_HEADER.search(str(k)) or len(text) >= SECRET_VALUE_LEN:
            out[str(k)] = mask(text)
        else:
            out[str(k)] = text
    return out


def check(cid, name, status, detail, **evidence):
    item = {"id": cid, "name": name, "status": status, "detail": detail}
    if evidence:
        item["evidence"] = evidence
    return item


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
    """技能被装在哪个宿主目录下（`<宿主目录>/skills/<技能名>/scripts/probe.py`）。

    这是**不靠猜也能认出没见过的端**的关键：宿主把技能复制到自己的技能目录里，
    于是 `__file__` 自己就是证据。认不出返回 None，报告里照实写"未识别"，
    并把安装路径贴出来——那是下一次要补进名单的事实。
    """
    path = os.path.abspath(start or __file__)
    seen = set()
    while True:
        parent = os.path.dirname(path)
        if parent == path or parent in seen:
            return None
        seen.add(parent)
        # `.skills` 也要认——豆包工作的技能目录是点开头的。
        if os.path.basename(parent) in SKILLS_DIR_NAMES:
            root = os.path.dirname(parent)
            return root if root and root != os.sep else None
        path = parent


def detect_host():
    root = install_root()
    evidence, host_id, name, verified = [], "unknown", "未识别的宿主", False
    if root:
        evidence.append("技能安装在 %s" % root)
        hit = match_host(root)
        if hit:
            host_id, name, verified = hit
    for env_name, hid, hname, is_verified in ENV_SIGNATURES:
        if os.environ.get(env_name):
            evidence.append("环境变量 %s 存在" % env_name)
            if host_id == "unknown" and root is None:   # 安装位置比环境变量硬
                host_id, name, verified = hid, hname, is_verified
    return {"id": host_id, "name": name, "root": root,
            "verified": verified, "evidence": evidence}


# --- C1 运行时 ------------------------------------------------------------
def c_runtime():
    return check(
        "C1", "脚本执行与运行时", PASS,
        "Python %s 已执行本脚本" % platform.python_version(),
        python=platform.python_version(),
        executable=sys.executable,
        platform=platform.platform(),
        cwd=os.getcwd(),
        argv0=sys.argv[0],
    )


# --- C2 读技能包内文件 ----------------------------------------------------
def c_read_bundle():
    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probe_file = os.path.join(skill_root, "references", "probe-marker.txt")
    try:
        with open(probe_file, "r", encoding="utf-8") as fh:
            first = fh.readline().strip()
    except Exception as exc:
        return check("C2", "读取技能包内 references/", FAIL,
                     "读不到 %s：%s" % (probe_file, exc), skill_root=skill_root)
    ok = first.startswith("JDY-PROBE-MARKER")
    return check("C2", "读取技能包内 references/", PASS if ok else WARN,
                 "读到标记：%s" % first if ok else "文件可读但标记不符：%s" % first,
                 skill_root=skill_root)


# --- C3 读本地密钥配置 ----------------------------------------------------
def c_local_config():
    if not os.path.exists(CONFIG_PATH):
        return check("C3", "读取 ~/.jdy/config.json", SKIP,
                     "文件不存在（未配置，不代表平台禁止读取）", path=CONFIG_PATH)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except PermissionError as exc:
        return check("C3", "读取 ~/.jdy/config.json", FAIL,
                     "沙箱拒绝读取 HOME 下的配置文件：%s" % exc, path=CONFIG_PATH)
    except Exception as exc:
        return check("C3", "读取 ~/.jdy/config.json", WARN,
                     "文件可达但解析失败：%s" % exc, path=CONFIG_PATH)
    key = cfg.get("api_key") or cfg.get("apiKey")
    return check("C3", "读取 ~/.jdy/config.json", PASS if key else WARN,
                 "读取成功，已取到 api_key" if key else "读取成功，但没有 api_key 字段",
                 path=CONFIG_PATH, keys=sorted(cfg.keys()), api_key=mask(key))


# --- C4 读环境变量 --------------------------------------------------------
def c_env():
    key = os.environ.get(ENV_KEY)
    proxies = {k: v for k, v in os.environ.items()
               if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
    return check("C4", "读取环境变量 %s" % ENV_KEY, PASS if key else SKIP,
                 "已取到（%s）" % mask(key) if key else "未设置（不代表平台禁止读取环境变量）",
                 api_key=mask(key), proxy_env=proxies or None,
                 home=os.environ.get("HOME"), env_count=len(os.environ))


# --- C5 写临时文件 --------------------------------------------------------
def c_write():
    results = {}
    for label, path in (("tempdir", tempfile.gettempdir()), ("cwd", os.getcwd())):
        target = os.path.join(path, ".jdy_probe_write_test")
        try:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("probe")
            os.remove(target)
            results[label] = "writable"
        except Exception as exc:
            results[label] = "denied: %s" % exc
    ok = any(v == "writable" for v in results.values())
    return check("C5", "写本地文件", PASS if ok else FAIL,
                 "可写目录：%s" % ", ".join(k for k, v in results.items() if v == "writable")
                 if ok else "所有候选目录均不可写——技能无法落地导出文件",
                 **results)


# --- C9 状态目录落点 ------------------------------------------------------
def c_state_home(host):
    """技能的状态（字段缓存、审计日志、哨兵去重）到底能落在哪。

    C5 只回答"有没有地方可写"，这一条回答"**按技能真实的候选顺序**，
    第一个能写的是哪个、其余为什么不行"。新端最需要的就是这份清单——
    它等价于把该端的沙箱写白名单测出来，不用去翻它的配置文件。
    """
    root = host.get("root")
    cands = []
    explicit = os.environ.get(STATE_HOME_ENV)
    if explicit:
        cands.append((os.path.expanduser(explicit), "env", True))
    cands.append((os.path.expanduser("~/.jdy"), "default", True))
    if root:
        cands.append((os.path.join(root, STATE_DIR_NAME), "host", True))
    cands.append((os.path.join(os.getcwd(), ".jdy"), "cwd", False))
    cands.append((os.path.join(tempfile.gettempdir(), "jdy-state"), "temp", False))

    tried, winner, created = [], None, []
    for path, source, stable in cands:
        probe = os.path.join(path, ".jdy_probe_write_test")
        existed = os.path.isdir(path)
        try:
            os.makedirs(path, exist_ok=True)
            if not existed:
                created.append(path)
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("probe")
            os.remove(probe)
            tried.append({"path": path, "source": source, "writable": True,
                          "stable": stable})
            if winner is None:
                winner = (path, source, stable)
        except Exception as exc:
            tried.append({"path": path, "source": source, "writable": False,
                          "why": str(exc)})
    # 探针要把**每个**候选都试一遍（那份清单就是该端的写白名单），
    # 于是会顺手建出几个用不上的空目录。只读的探针不该在用户机器上留垃圾——
    # 建了又没选中的，空着就删掉。
    for path in created:
        if winner and os.path.realpath(path) == os.path.realpath(winner[0]):
            continue
        try:
            os.rmdir(path)
        except OSError:
            pass
    if winner is None:
        return check("C9", "状态目录落点", FAIL,
                     "所有候选目录均不可写——缓存/审计/去重状态都留不下来。"
                     "设 %s 指向一个沙箱允许写的目录" % STATE_HOME_ENV,
                     candidates=tried)
    path, source, stable = winner
    status = PASS if stable else WARN
    detail = "状态将落在 %s（来源 %s）" % (path, source)
    if not stable:
        detail += "——**只在本次会话有效**，换个工作目录就找不回来；设 %s 可固定" % STATE_HOME_ENV
    return check("C9", "状态目录落点", status, detail,
                 chosen=path, source=source, stable=stable, candidates=tried)


# --- C6 DNS ---------------------------------------------------------------
def c_dns():
    try:
        infos = socket.getaddrinfo(API_HOST, 443, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        return check("C6", "DNS 解析 %s" % API_HOST, FAIL, "解析失败：%s" % exc)
    addrs = sorted({i[4][0] for i in infos})
    return check("C6", "DNS 解析 %s" % API_HOST, PASS,
                 "解析到 %s" % ", ".join(addrs), addresses=addrs)


# --- C7/C8 HTTPS 出网与鉴权 ----------------------------------------------
def _post(path, payload, token, timeout):
    """返回 (http_status, body_text)。HTTPError 也是一次成功的往返。"""
    req = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "hello-jdy-probe/0.1"},
        method="POST",
    )
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def c_egress(timeout):
    """不带密钥打一次。拿到任何 HTTP 状态码都说明 HTTPS 出网是通的。"""
    try:
        status, body = _post("/app/list", {"limit": 1}, None, timeout)
    except ssl.SSLError as exc:
        return check("C7", "HTTPS 出网", FAIL,
                     "TLS 失败（沙箱代理可能在做中间人）：%s" % exc)
    except urllib.error.URLError as exc:
        return check("C7", "HTTPS 出网", FAIL,
                     "连不上 %s：%s" % (API_HOST, exc.reason))
    except Exception as exc:
        return check("C7", "HTTPS 出网", FAIL, "请求异常：%s" % exc)
    return check("C7", "HTTPS 出网", PASS,
                 "无密钥请求收到 HTTP %d —— 出网通畅（该状态码为预期的鉴权拒绝）" % status,
                 http_status=status, body=body[:300])


def c_api(token, timeout):
    if not token:
        return check("C8", "简道云 API 鉴权调用", SKIP,
                     "未找到密钥，跳过。配置 %s 或 %s 后重跑可验证完整链路"
                     % (ENV_KEY, CONFIG_PATH))
    try:
        status, body = _post("/app/list", {"limit": 100}, token, timeout)
    except Exception as exc:
        return check("C8", "简道云 API 鉴权调用", FAIL, "请求异常：%s" % exc)
    if status != 200:
        return check("C8", "简道云 API 鉴权调用", FAIL,
                     "HTTP %d：%s" % (status, body[:200]),
                     http_status=status, hint="8300/8301 多为密钥无效或权限不足")
    try:
        apps = json.loads(body).get("apps", [])
    except Exception as exc:
        return check("C8", "简道云 API 鉴权调用", WARN,
                     "HTTP 200 但响应解析失败：%s" % exc, body=body[:300])
    return check("C8", "简道云 API 鉴权调用", PASS,
                 "app/list 返回 %d 个授权应用 —— 技能→API 完整链路打通" % len(apps),
                 app_count=len(apps),
                 apps=[{"name": a.get("name"), "app_id": a.get("app_id")} for a in apps[:10]])


# --- C10 官方「AI 连接」连接器 -------------------------------------------
def connector_roots(host_root=None, home=None):
    """去哪儿找连接器配置。两个来源，都不是新写死的平台清单：

      · 技能自己被装在哪（`install_root()`）——对**没见过的端**也成立；
      · `HOST_SIGNATURES` 里那几个宿主配置目录名——那张表已经存在，
        而且有守卫测试盯着它不和内核漂移。

    再新列一份"各平台连接器路径"，就是又造一份会漂的东西：
    路径改了、名单没跟上，探针会安静地报"未知"，而没人知道它其实在找错地方。
    """
    home = home if home is not None else os.path.expanduser("~")
    roots = []
    if host_root:
        roots.append(host_root)
    for marker, _hid, _name, _verified in HOST_SIGNATURES:
        path = os.path.join(home, marker)
        if path not in roots:
            roots.append(path)
    return roots


def _connector_files(root):
    """root 下像 MCP 配置的文件。**只读，且有上限**——探针不该在别人的
    配置目录里无限深挖，也不该因为撞上一个巨大的目录就卡住。"""
    found = []
    if not os.path.isdir(root):
        return found
    base = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, files in os.walk(root):
        if dirpath.count(os.sep) - base >= CONNECTOR_SCAN_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in CONNECTOR_SCAN_SKIP and d != ".git"]
        for name in sorted(files):
            low = name.lower()
            if low in CONNECTOR_FILE_HINTS or low.endswith("mcp.json"):
                found.append(os.path.join(dirpath, name))
                if len(found) >= CONNECTOR_MAX_FILES:
                    return found
    return found


def _is_catalog(path):
    """市场目录 ≠ 已安装。

    宿主会把整个连接器市场的清单缓存到本地（WorkBuddy 就有一份）。
    在那份清单里看见简道云，只说明"市场上有"，不说明"这台机器装了"。
    两者混为一谈，报告就会把"能装"说成"已装"——那正是本轮要避免的越界。
    """
    return any("marketplace" in part.lower()
               for part in os.path.normpath(path).split(os.sep))


def _entries_mentioning_jdy(text):
    """返回 [(条目名, 该条目的原始 dict 或 None)]。

    形状有两种：`{"mcpServers": {...}}`（WorkBuddy／千问办公都这样）；
    以及扁平的单条配置（千问办公的 `mcp-adaptor.config` 就是 url/token/headers）。
    解析不了就退回纯文本搜——认不出结构不等于看不见线索。
    """
    hit = lambda blob: any(h in blob.lower() if h.isascii() else h in blob
                           for h in JDY_HINTS)
    try:
        data = json.loads(text)
    except Exception:
        return [("(整个文件，未能解析为 JSON)", None)] if hit(text) else []
    out = []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(servers, dict):
        for name, conf in servers.items():
            blob = "%s %s" % (name, json.dumps(conf, ensure_ascii=False))
            if hit(blob):
                out.append((name, conf if isinstance(conf, dict) else None))
        return out
    if hit(text):
        out.append(("(整个文件)", data if isinstance(data, dict) else None))
    return out


def _describe(conf):
    """把一条连接器配置摘出来——**url 与 header 都先过掩码**。"""
    if not isinstance(conf, dict):
        return {}
    info = {}
    if isinstance(conf.get("url"), str):
        info["url"] = mask_url(conf["url"])
    headers = conf.get("staticHeaders") or conf.get("headers")
    if headers:
        info["headers"] = mask_headers(headers)
    for key in ("enabled", "disabled", "type", "timeout"):
        if key in conf:
            info[key] = conf[key]
    return info


def _enabled_state(conf):
    """这条连接器**配置里写没写**启用状态。写了照实报，没写返回 None（未知）。

    原来这里是一句笼统的"启用状态读不到"——那只对了一半：
    WorkBuddy 确实把它加密在 `connector-states.v3.json`（aes-256-gcm，
    密钥 `.master.key`），文件层读不出来；而**千问办公直接写在 mcp.json 里**
    （本机实测 `"enabled": true`）。把一端的限制说成所有端的限制，
    等于在读得到的那一端主动丢掉一条真信息。
    """
    if not isinstance(conf, dict):
        return None
    if isinstance(conf.get("enabled"), bool):
        return conf["enabled"]
    if isinstance(conf.get("disabled"), bool):
        return not conf["disabled"]
    return None


def scan_ai_connect(roots):
    """扫一遍给定的根目录，找简道云的 AI 连接条目。纯只读。

    返回 (state, findings, files, errors)。state 四种：
      · dual-track   —— 装了（本机配置里有条目）
      · market-only  —— 只在市场目录里见到，本机还没装
      · skill-only   —— 读到了配置，里面没有简道云
      · unknown      —— **一个 MCP 配置文件都没找到**。这不是"没有"，
                        是"没探到"——写成"无"，下一个人就照着这条假结论填表了。
    """
    findings, files, errors = [], [], []
    for root in roots:
        for path in _connector_files(root):
            try:
                if os.path.getsize(path) > CONNECTOR_MAX_BYTES:
                    errors.append({"path": path, "why": "文件过大，跳过"})
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except Exception as exc:
                errors.append({"path": path, "why": str(exc)})
                continue
            catalog = _is_catalog(path)
            files.append({"path": path, "where": "catalog" if catalog else "installed"})
            for name, conf in _entries_mentioning_jdy(text):
                item = {"path": path, "entry": name,
                        "where": "catalog" if catalog else "installed",
                        "enabled": _enabled_state(conf)}
                item.update(_describe(conf))
                findings.append(item)
    if any(f["where"] == "installed" for f in findings):
        state = "dual-track"
    elif findings:
        state = "market-only"
    elif files:
        state = "skill-only"
    else:
        state = "unknown"
    return state, findings, files, errors


_TRACK_TEXT = {
    "dual-track": "双轨（技能 ＋ 官方 AI 连接）",
    "market-only": "技能单轨（官方连接器在市场目录里，本机未装）",
    "skill-only": "技能单轨",
    "unknown": "未知（没探到任何 MCP 连接器配置）",
}


def c_ai_connect(host, roots=None, home=None):
    """C10：这一端是双轨还是技能单轨。

    **「已配置」一定报得出，「已启用」看该端写不写在文件里。**
    WorkBuddy 把启用状态加密在 `connector-states.v3.json`（aes-256-gcm，
    密钥 `.master.key`），读不出来；千问办公直接写在 `mcp.json` 里
    （本机实测 `"enabled": true`）。所以这件事是**逐条**判断的，
    不是一句"都读不到"——那会在读得到的那一端主动丢掉一条真信息。

    读得到时也只是**配置的说法**：它不等于宿主此刻真在用。
    这个界线要写进输出，让读表的人自己去猜，这一列就等于没填。
    """
    # **认出宿主时只扫它自己那一份。**
    # 这里踩过一次：候选根目录是"本机所有已知宿主目录"，于是在 WorkBuddy 上跑出来的
    # 填表行写着「腾讯 WorkBuddy … jiandaoyun @ ~/.qwenworkcn/mcp.json」——
    # 把另一端的连接器算成了这一端的。填表列是**按端**的，扫描范围也必须是。
    # 别的端有没有装仍然有用（本机全景），但它进 other_hosts，不进这一端的结论。
    root = host.get("root")
    if roots is not None:
        primary, others = list(roots), []
    elif root:
        primary = [root]
        others = [p for p in connector_roots(None, home)
                  if os.path.realpath(p) != os.path.realpath(root)]
    else:
        primary, others = connector_roots(None, home), []
    state, findings, files, errors = scan_ai_connect(primary)
    installed = [f for f in findings if f["where"] == "installed"]
    # 「启用状态知不知道」是**逐条**的，不是一个全局结论：
    # WorkBuddy 加密着读不到，千问办公明文写在 mcp.json 里。
    known = [f for f in installed if f["enabled"] is not None]
    evidence = {"state": state, "track": _TRACK_TEXT[state],
                "enabled_known": bool(installed) and len(known) == len(installed),
                "roots": list(primary), "files": files,
                "findings": findings, "errors": errors or None}
    if state == "dual-track":
        on = len([f for f in known if f["enabled"]])
        off = len(known) - on
        unknown = len(installed) - len(known)
        parts = []
        if on:
            parts.append("配置里写着已启用 %d 条" % on)
        if off:
            parts.append("已停用 %d 条" % off)
        if unknown:
            parts.append("**%d 条读不出启用状态**（该端把它加密存在连接器状态文件里）"
                         % unknown)
        detail = ("已配置官方 AI 连接 %d 条 → %s。%s。"
                  "配置里的 enabled 是**配置的说法**，不等于宿主此刻真在用——"
                  "要确认得去该端界面里看。" % (len(installed), _TRACK_TEXT[state],
                                             "；".join(parts)))
        status = PASS
    elif state == "market-only":
        detail = ("在该端的**连接器市场目录**里见到简道云，但本机配置里没有已装条目 → %s。"
                  "市场上有 ≠ 这台机器装了。" % _TRACK_TEXT[state])
        status = PASS
    elif state == "skill-only":
        detail = ("读到 %d 个 MCP 配置文件，里面**未见简道云条目** → %s。"
                  % (len(files), _TRACK_TEXT[state]))
        status = SKIP
    else:
        # `roots` 是**参数**，不传时是 None；真正扫过的是 primary。
        # 改名时漏改这一处，未识别宿主(root 找得到但名字不认识，比如直接跑仓库副本)
        # 恰好落在这个分支上，探针整个崩掉——而单测全都显式传了 roots=，一条都没走到。
        detail = ("**未知**：在 %d 个候选根目录下没找到任何 MCP 连接器配置文件。"
                  "没探到不等于没有——该端的配置落点可能还没被认出来，"
                  "把实际路径回填进来即可。" % len(primary))
        status = SKIP
    if others:
        o_state, o_findings, o_files, _ = scan_ai_connect(others)
        evidence["other_hosts"] = {"state": o_state, "roots": others,
                                   "files": len(o_files), "findings": o_findings}
        if state != "dual-track" and o_state == "dual-track":
            detail += ("　（**本机别的端装了**：%s——那是那一端的结论，不是这一端的。）"
                       % "、".join(sorted({f["path"] for f in o_findings})))
    evidence["matrix_row"] = _matrix_row(host, state, findings)
    return check("C10", "官方「AI 连接」连接器", status, detail, **evidence)


def _matrix_row(host, state, findings):
    """一行能直接贴进 platform-compat-matrix V5 三端表（端 / 官方是否点名 /
    接入方式 / 实测结果 / 备注）。

    产出如果还要人再翻译一道，填表的人就会凭印象写——而凭印象写出来的
    "已接入"，正是这张表最不能有的东西。
    """
    name = host.get("name") or host.get("agent_host_name") or "未识别的宿主"
    on = [f for f in findings if f.get("where") == "installed" and f.get("enabled") is True]
    unknown_on = [f for f in findings
                  if f.get("where") == "installed" and f.get("enabled") is None]
    dual = ("✅ 已配置且配置里写着已启用" if on and not unknown_on
            else "✅ 已配置（启用状态读不到）")
    result = {"dual-track": dual,
              "market-only": "⬜ 市场有、本机未装",
              "skill-only": "⬜ 未见条目",
              "unknown": "⬜ 未知（未探到配置）"}[state]
    note = "；".join("%s @ %s" % (f["entry"], f["path"]) for f in findings[:2]) or "—"
    return "| %s | —（探针不读官方清单） | 本机配置文件 | %s | %s |" % (name, result, note)


def build_report(args):
    host = detect_host()
    checks = [c_runtime(), c_read_bundle(), c_local_config(), c_env(), c_write()]
    token = os.environ.get(ENV_KEY)
    if not token and os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            token = cfg.get("api_key") or cfg.get("apiKey")
        except Exception:
            token = None
    if args.no_network:
        checks += [check("C6", "DNS 解析", SKIP, "--no-network"),
                   check("C7", "HTTPS 出网", SKIP, "--no-network"),
                   check("C8", "简道云 API 鉴权调用", SKIP, "--no-network")]
    else:
        checks.append(c_dns())
        checks.append(c_egress(args.timeout))
        checks.append(c_api(token, args.timeout))
    checks.append(c_ai_connect(host))     # 只读：这一端是双轨还是技能单轨
    checks.append(c_state_home(host))     # 放最后：它会建目录，别让副作用抢在只读检查前面

    tally = {}
    by_id = {}
    for c in checks:
        tally[c["status"]] = tally.get(c["status"], 0) + 1
        by_id[c["id"]] = c["status"]
    if by_id.get("C7") == SKIP:
        verdict = "INCOMPLETE"          # 没测出网就没资格下结论
    elif by_id.get("C7") == FAIL or by_id.get("C6") == FAIL:
        verdict = "TRACK_MCP"
    elif tally.get(FAIL):
        verdict = "DEGRADED"
    else:
        verdict = "TRACK_SKILL"
    return {
        "probe": "hello-jdy",
        "version": "0.3.0",
        "host": {"platform": platform.platform(), "python": platform.python_version(),
                 "agent_host": host["id"], "agent_host_name": host["name"],
                 "agent_host_root": host["root"],
                 "agent_host_verified": host["verified"],
                 "agent_host_evidence": host["evidence"]},
        "summary": tally,
        "verdict": verdict,
        # 轨道结论提到顶层：填兼容性验证表时它是一整列，
        # 埋在 checks 里等于让填表的人自己去翻，翻着翻着就凭印象写了。
        "track": next((c.get("evidence", {}) for c in checks if c["id"] == "C10"), {}),
        "checks": checks,
    }


VERDICT_TEXT = {
    "TRACK_SKILL": "技能轨可用：脚本能执行、能出网、能读写本地文件——本端按技能轨推进（密钥读取见 C3/C4）。",
    "TRACK_MCP": "出网受限：该端技能脚本连不上简道云，降级走 MCP 连接器轨。",
    "DEGRADED": "存在硬失败项：看下方 FAIL 条目决定降级方案。",
    "INCOMPLETE": "网络检查被跳过，尚不足以判定路线——去掉 --no-network 重跑。",
}


def host_line(host):
    """一句话说清"这是哪一端"，以及这个判断有多硬。

    认不出来时**不要空着**：把安装路径贴出来。那不是失败，
    那是这一端的目录名——补进 HOST_SIGNATURES 就认识了。
    """
    if host["agent_host"] == "unknown":
        if host["agent_host_root"]:
            return ("Agent 宿主：未识别 —— 技能装在 %s。"
                    "把这个路径回填进 HOST_SIGNATURES 即可认出这一端。"
                    % host["agent_host_root"])
        return "Agent 宿主：未识别（技能不在 <宿主目录>/skills/ 下，可能是直接跑的仓库副本）"
    tag = "已实测" if host["agent_host_verified"] else "**该端尚未实测，此判断仅据目录名**"
    line = "Agent 宿主：%s（%s，%s）" % (host["agent_host_name"], host["agent_host"], tag)
    unsupported = UNSUPPORTED_HOSTS.get(host["agent_host"])
    if unsupported:
        line += ("\n⛔ **本版本不支持这一端。** %s\n"
                 "   下面的检查照跑——脚本本身在这儿多半是能跑的，"
                 "但技能装不住，所以结论仅供参考。" % unsupported[1])
    return line


def default_save_name(host):
    """报告文件名带上宿主标识——三端各跑一次却存成同名文件，比不存还糟。"""
    return "jdy-probe-%s.json" % (host["agent_host"] if host["agent_host"] != "unknown"
                                  else "unknown-host")


def render(report):
    lines = []
    lines.append("=" * 64)
    lines.append("简道云连接诊断报告 v%s" % report["version"])
    lines.append(host_line(report["host"]))
    lines.append("宿主机：%s / Python %s" % (report["host"]["platform"], report["host"]["python"]))
    lines.append("=" * 64)
    # 执行顺序和阅读顺序不是一回事：C9 会建目录，所以它**跑**在最后
    # （别让副作用抢在只读检查前面）；但人读报告是按编号找条目的，
    # 打出来 C10 排在 C9 前面，看着像出了错。列表保留执行顺序，显示按编号排。
    for c in sorted(report["checks"], key=lambda x: int(x["id"][1:])):
        lines.append("%s %s  %s" % (_MARK[c["status"]], c["id"], c["name"]))
        lines.append("       %s" % c["detail"])
    lines.append("-" * 64)
    lines.append("统计：" + "  ".join("%s=%d" % (k, v) for k, v in sorted(report["summary"].items())))
    lines.append("结论：%s" % VERDICT_TEXT[report["verdict"]])
    track = report.get("track") or {}
    if track:
        lines.append("官方「AI 连接」：%s" % track.get("track", "未知"))
        # matrix_row 仍留在 JSON 里（维护者自己的兼容性表要用），但不进给用户看的
        # 报告正文——那是本项目的内部记账格式，对装了技能却连不上的人没有意义。
    lines.append("=" * 64)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="简道云连接诊断（只读）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 报告")
    ap.add_argument("--no-network", action="store_true", help="跳过所有网络检查")
    ap.add_argument("--timeout", type=float, default=15.0, help="网络超时秒数，默认 15")
    ap.add_argument("--save", metavar="PATH",
                    help="把 JSON 报告另存到指定路径；给的是目录就自动命名为 "
                         "jdy-probe-<宿主>.json")
    args = ap.parse_args()

    report = build_report(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
        print("\n--- JSON（贴进验证表用） ---")
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.save:
        target = os.path.expanduser(args.save)
        if os.path.isdir(target):
            target = os.path.join(target, default_save_name(report["host"]))
        try:
            with open(target, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            sys.stderr.write("报告已保存：%s\n" % target)
        except Exception as exc:
            sys.stderr.write("报告保存失败（本身也是一条结论）：%s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
