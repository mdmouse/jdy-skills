# -*- coding: utf-8 -*-
"""每个技能脚本的第一句 import：钉住输出编码，并定位共享内核。

1. **stdio 钉成 UTF-8**——Windows 中文控制台默认 GBK，打印 ✅ / ⬜ 会崩。
   必须在任何 print 之前，所以放在定位内核之前。
2. **定位共享内核**——安装后走 scripts/_shared/（由仓库根的 build.py vendor
   进来）；仓库内开发时回落到 ../../../_shared/。两条路径都找不到就明确报错，
   而不是留下一个 ImportError 让用户去猜。

本文件在 10 个技能里逐字节相同，由 tests/test_skill_format.py 把关。
改一处就要改全部——只改一半正是这个仓库反复出现的 bug 形状。
"""
import os
import sys


def _force_utf8_stdio():
    """把 stdout/stderr 钉成 UTF-8。

    Windows 中文控制台默认 GBK，打印 ✅ / ⬜ 这类符号会抛 UnicodeEncodeError
    把整个脚本崩掉——不是显示成乱码，是直接退出。三端主力用户在 Windows，
    所以这一句必须跑在任何 print 之前。

    宿主把 stdout 换成了非 TextIOWrapper 的对象（或 pythonw 下是 None）时
    reconfigure 不存在，静默跳过——不能因为修不了编码反而崩掉。
    """
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_stdio()

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = (
    os.path.join(_HERE, "_shared"),                                  # 安装后
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "_shared")),  # 仓库内开发
)

for _path in _CANDIDATES:
    if os.path.isdir(_path):
        if _path not in sys.path:
            sys.path.insert(0, _path)
        break
else:
    raise ImportError(
        "找不到共享内核 jdy_client。安装包内应有 scripts/_shared/——"
        "若从仓库直接运行，请先在仓库根执行 `python3 build.py`。\n已查找：\n  " +
        "\n  ".join(_CANDIDATES))
