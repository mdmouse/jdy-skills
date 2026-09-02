#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑齐 tests/ 下所有测试文件。

`python3 -m unittest discover` 在本仓库跑不了：tests/ 没有 __init__.py，
discover 会直接 "Start directory is not importable" 退出。而各测试文件是
**能独立执行**的设计（沙箱里未必有 pytest）。所以入口是逐个文件跑。

    python3 tests/run_all.py          # 全跑
    python3 tests/run_all.py -k win   # 只跑文件名含 win 的

tests/real/ 不在此列——那些要真账号和网络，由 tests/real/acceptance.sh 管。
"""
import os
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):          # Windows 中文控制台默认 GBK
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))


def _child_encoding():
    """子进程 stdio 的实际编码。

    各测试文件与技能脚本都把自己的 stdio 钉成 UTF-8；只有在显式设了
    PYTHONIOENCODING（CI 的 GBK 控制台 job）时子进程才会照它输出。
    `text=True` 不能用：它按**本进程**的 locale 解码（Windows runner 是 cp1252），
    子进程吐 UTF-8 时读线程直接 UnicodeDecodeError，stdout 变成 None，
    随后 `str + None` 把整个跑测试的入口炸掉——首次 Windows CI 就是这么红的。
    """
    enc = (os.environ.get("PYTHONIOENCODING") or "utf-8").split(":")[0].strip()
    return enc or "utf-8"


def _decode(data):
    """按子进程**实际**用的编码读，而不是按我们以为的那个。

    两种子进程并存，这是它们不对称的地方：
      · 多数测试文件不管编码，照 PYTHONIOENCODING（或平台默认）输出；
      · test_skill_format.py 和所有技能脚本（_bootstrap 的 _force_utf8_stdio）
        **无条件**把 stdio 钉成 UTF-8，看都不看 PYTHONIOENCODING。
    只按 PYTHONIOENCODING 解码，CI 的 gbk-console job 里第二类子进程一旦失败，
    打给人看的那段 tail 就是一片乱码——正是最需要看清楚的时候看不清。
    所以非 UTF-8 时先严格试一次 UTF-8：成了就是它，不成再按声明的编码读。
    """
    raw = data or b""
    enc = _child_encoding()
    if enc.lower().replace("-", "").replace("_", "") != "utf8":
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return raw.decode(enc, errors="replace")


def main():
    keep = None
    if len(sys.argv) > 2 and sys.argv[1] == "-k":
        keep = sys.argv[2]

    files = sorted(f for f in os.listdir(HERE)
                   if f.startswith("test_") and f.endswith(".py")
                   and (keep is None or keep in f))
    if not files:
        print("没有匹配的测试文件"); return 1

    # 子进程的 stdout 是管道，不是控制台：Windows 上 Python 会按 ANSI 代码页
    # （runner 上是 cp1252）编码，于是任何打中文的测试文件一 print 就
    # UnicodeEncodeError、以退出码 1 假红。测试文件自己也该钉 UTF-8
    # （test_skill_format.py 顶上那几行），这里再从环境上钉一次——**两层都要**：
    # 单独跑某个测试文件时没有这个 env，靠文件自己钉；而 unittest 打的
    # 中文用例文档字符串是在测试文件的代码之外写出去的，靠这个 env 兜住。
    # 用 setdefault 而不是直接赋值：CI 的 gbk-console job 显式设了 PYTHONIOENCODING=gbk
    # 来复现中文 Windows 控制台，覆盖掉它就等于把那个 job 测的东西删了。
    # 与 _child_encoding() 读的是同一个变量，两边保证一致。
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    failed = []
    for name in files:
        r = subprocess.run([sys.executable, os.path.join(HERE, name)],
                           capture_output=True, cwd=os.path.dirname(HERE), env=env)
        out = _decode(r.stdout) + _decode(r.stderr)
        mark = "OK  " if r.returncode == 0 else "FAIL"
        print("%s  %s" % (mark, name))
        if r.returncode != 0:
            failed.append(name)
            tail = out.strip().splitlines()[-25:]
            print("\n".join("        " + l for l in tail))
    print("-" * 60)
    print("%d 个文件，通过 %d，失败 %d" % (len(files), len(files) - len(failed), len(failed)))
    if failed:
        print("失败：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
