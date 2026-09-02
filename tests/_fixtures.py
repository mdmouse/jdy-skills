# -*- coding: utf-8 -*-
"""测试夹具。**不是测试文件**（run_all.py 只收 test_*.py），别在这里写断言。

这里只住一件事：怎么造一条「任何操作系统上都写不进去」的路径。

原来各测试文件是这么造的：`/proc/不可能写得进去/x.json`。在 Linux/macOS 上
它确实写不进（/proc 是内核的伪文件系统 / 根目录不可写），可**在 Windows 上
它只是相对当前盘符的 `D:\\proc\\不可能写得进去\\x.json`**——makedirs 建得出来、
文件写得下去。于是所有「写不进去时应当降级/如实返回 None」的断言，在 Windows 上
测的其实是成功路径，一声不响地绿着。

`os.chmod(dir, 0o500)` 是同一个坑的另一种写法：Windows 上 chmod 对目录**不起作用**
（它只映射到只读属性，而目录的只读属性根本不管建不建得了子项），只读目录照样能写。

改成拿一个**普通文件当父目录**：
  · POSIX   → NotADirectoryError（errno ENOTDIR）
  · Windows → WinError 267「目录名称无效」/ ENOTDIR
两者都是 OSError 的子类，被测代码里 `except OSError` 一律接得住；
`os.makedirs()` 与 `open(..., "w")` 在两个平台上都必然失败，没有例外。
"""
import os
import tempfile


def blocker_file(prefix="jdy-blocked-"):
    """建一个普通文件，返回它的路径。它专门用来当"假父目录"。"""
    box = tempfile.mkdtemp(prefix=prefix)
    path = os.path.join(box, "blocker")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("我是一个普通文件，不是目录——测试故意这么造的。\n")
    return path


def unwritable_path(*parts):
    """一条任何系统上都写不进去的路径：`<某个普通文件>/<parts...>`。

    不传 parts 时返回的就是那个文件本身的一个子路径占位（"child"），
    保证返回值永远处在"文件底下"，而不是那个文件自己（后者是可写的）。
    """
    return os.path.join(blocker_file(), *(parts or ("child",)))
