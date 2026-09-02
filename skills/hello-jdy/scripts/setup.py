#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引导用户配好简道云 API Key，并当场验证它真的能用。

写入类技能的用户是运营和行政，不是开发——让他们自己去开放平台建 Key、
再手写一个 JSON 文件，这一步就会劝退大半。所以把它变成一问一答。

**Key 绝不走命令行参数。** argv 会进 shell 历史、也会出现在 `ps` 的输出里，
密钥不能待在这两个地方。只从标准输入或环境变量拿。

    python3 scripts/setup.py                  # 看现在配没配、该怎么配
    echo '<KEY>' | python3 scripts/setup.py --stdin
    JDY_API_KEY=<KEY> python3 scripts/setup.py --from-env
    python3 scripts/setup.py --verify         # 只验证现有的 Key 还能不能用

和 probe.py 一样刻意零依赖：它跑在"还没确认这台机器能不能用内核"的时候。
"""

import argparse
import json
import os
import stat
import sys
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
CONFIG_DIR = os.path.expanduser(os.environ.get("JDY_HOME") or "~/.jdy")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# 署名行。**这里是唯一一处抄写**：本脚本和 probe.py 一样刻意零依赖
# （它跑在"还没确认这台机器能不能用内核"的时候），所以不能 import
# _shared/brand.py——一 import，build.py 就会把内核 vendor 进这个包，
# 而 tests/test_cli_contract.py 正守着"hello-jdy 包里不许有内核"。
# 抄写的代价由 tests/test_brand.py 兜住：它逐字比对这两行和 brand.py 的常量，
# 改了那边没改这边就红。
BRAND_LINE = "由 aicliagent 生成 · https://aicliagent.com"
BRAND_OFF_VALUES = ("0", "false", "off")


def brand_enabled():
    """与 _shared/brand.py 的 enabled() 同规则：JDY_BRAND=0/false/off 时关。"""
    return str(os.environ.get("JDY_BRAND", "")).strip().lower() not in BRAND_OFF_VALUES


HOW_TO_GET_A_KEY = """\
怎么拿到 API Key（跟用户逐字念这四步）：

  1. 浏览器打开简道云，右上角头像 →「开放平台」
  2. 左侧「密钥管理」→ 右上角「创建 API KEY」
  3. **建最小权限的**：
       · 应用范围  —— 只勾这次要用的那个应用，不要勾"全部"
       · 接口权限  —— 只读就够的话不要给写权限
       · IP 白名单 —— 填这台机器的出口 IP（不确定就先留空，之后再补）
  4. 创建后**立刻复制**——弹窗关掉就再也看不到了，只能重建

官方文档写着"企业版及以上"，但试用账号实测可以调通，验证阶段不必先付费。
"""


def mask(key):
    """只露前 4 后 4。报告、日志、错误信息里都用它。"""
    if not key:
        return "(空)"
    return "%s...%s (共 %d 位)" % (key[:4], key[-4:], len(key)) if len(key) > 12 else "***"


def read_config():
    """返回 (key, 来源说明)。环境变量优先——它常用于临时覆盖。"""
    env = os.environ.get("JDY_API_KEY")
    if env:
        return env, "环境变量 JDY_API_KEY"
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                return (json.load(fh) or {}).get("api_key"), CONFIG_PATH
        except (ValueError, OSError) as exc:
            return None, "%s（读不出来：%s）" % (CONFIG_PATH, exc)
    return None, None


def verify(key):
    """拿这把 Key 真调一次只读接口。返回 (成功?, 说明)。"""
    body = json.dumps({"limit": 1}).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + "/app/list", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        apps = data.get("apps") or data.get("data") or []
        return True, "调通了，这把 Key 能看到 %d 个应用" % len(apps)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass

        # **按简道云的业务码判断，不要按 HTTP 状态码。** 实测：格式不对的 Key
        # 回 400/3005，而一把被删掉的 Key 回的也是 400——只是 code 是 17018。
        # 光看 400 就说"格式不对"，会让一个刚换过 Key 的人去检查有没有粘漏，
        # 而真正的原因是本地配置还指着旧的那把。
        code = None
        try:
            code = json.loads(detail).get("code")
        except (ValueError, AttributeError):
            pass
        by_code = {
            3005: "Key 的格式不对——多半是粘漏了、或者把别的东西粘进来了",
            17018: ("这把 Key 无效——**在开放平台被删掉或换过了**。\n"
                    "     刚轮换过 Key 的话：后台建了新的，本地配置还指着旧的，"
                    "把新 Key 重新写进来即可。"),
        }
        by_status = {
            401: "Key 无效或已被删除——回开放平台确认，或重建一把",
            403: "权限不够，或这台机器的 IP 不在白名单里",
            429: "调用太频繁，等一会儿再试",
        }
        hint = by_code.get(code) or by_status.get(exc.code, "")
        return False, "HTTP %s%s%s\n     原始返回：%s" % (
            exc.code, "/%s" % code if code else "",
            "　" + hint if hint else "", detail)
    except urllib.error.URLError as exc:
        return False, ("连不上 api.jiandaoyun.com（%s）。"
                       "先确认这台机器能出网、以及有没有代理／防火墙挡着。" % exc.reason)


def write_config(key):
    """写 config.json，权限 600。**不覆盖已有的其它字段。**"""
    existing = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                existing = json.load(fh) or {}
        except (ValueError, OSError):
            existing = {}                   # 坏文件就重写，但别把 key 丢了
    existing["api_key"] = key

    os.makedirs(CONFIG_DIR, exist_ok=True)
    # 先建成 600 再写，避免"先以 644 落盘、再 chmod"那一瞬间的暴露窗口
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)   # 已存在的文件补一刀
    except OSError:
        pass                                # Windows 上不一定支持，不是致命的
    return CONFIG_PATH


def show_status():
    key, source = read_config()
    print("=" * 64)
    print("简道云 API Key 配置状态")
    print("=" * 64)
    if not key:
        print("⬜ 还没配。配置文件预期落点：%s" % CONFIG_PATH)
        print()
        print(HOW_TO_GET_A_KEY)
        print("拿到 Key 之后，**让用户自己把它粘进下面这条命令**（别让他发给你）：")
        print()
        print("    echo '把KEY粘在这里' | python3 %s --stdin"
              % os.path.relpath(__file__, os.getcwd()))
        return 1
    print("✅ 已配置：%s" % mask(key))
    print("   来源：%s" % source)
    if os.path.exists(CONFIG_PATH):
        mode = oct(os.stat(CONFIG_PATH).st_mode & 0o777)[-3:]
        print("   权限：%s%s" % (mode, "" if mode == "600" else "  ⚠️ 建议 chmod 600"))
    print()
    print("验证它还能不能用：python3 %s --verify"
          % os.path.relpath(__file__, os.getcwd()))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="引导配置简道云 API Key 并当场验证（Key 只从 stdin 或环境变量读）")
    ap.add_argument("--stdin", action="store_true",
                    help="从标准输入读 Key 并写入配置文件")
    ap.add_argument("--from-env", action="store_true",
                    help="从环境变量 JDY_API_KEY 读 Key 并写入配置文件")
    ap.add_argument("--verify", action="store_true",
                    help="只验证当前配置的 Key 还能不能用，不修改任何文件")
    ap.add_argument("--no-verify", action="store_true",
                    help="写入后不联网验证（平台禁止出网时用）")
    args = ap.parse_args()

    if args.verify:
        key, source = read_config()
        if not key:
            print("还没配 Key。先跑一次不带参数的本脚本看怎么配。")
            return 1
        print("验证 %s（来自 %s）……" % (mask(key), source))
        ok, detail = verify(key)
        print(("✅ " if ok else "❌ ") + detail)
        return 0 if ok else 1

    if args.stdin or args.from_env:
        if args.stdin:
            key = sys.stdin.read().strip()
            if not key:
                print("标准输入是空的。用法：echo '<KEY>' | python3 setup.py --stdin")
                return 1
        else:
            key = (os.environ.get("JDY_API_KEY") or "").strip()
            if not key:
                print("环境变量 JDY_API_KEY 没设或是空的。")
                return 1

        # 先验证再落盘：把一把用不了的 Key 写进配置，只会让下一个报错更难查
        if not args.no_verify:
            print("先验证这把 Key……")
            ok, detail = verify(key)
            if not ok:
                print("❌ " + detail)
                print()
                print("**没有写入配置文件**——先把 Key 的问题解决掉。")
                print("确实想先存下来（比如现在没网），加 --no-verify。")
                return 1
            print("✅ " + detail)

        path = write_config(key)
        print("已写入 %s（权限 600）" % path)
        print("这台机器上的简道云技能现在都能用了。")
        if brand_enabled():
            print(BRAND_LINE)
        return 0

    return show_status()


if __name__ == "__main__":
    sys.exit(main())
