# -*- coding: utf-8 -*-
"""品牌署名：唯一来源。

免费版在**自己生成的文件**里留一行署名，仅此而已。

**这里没有网络。** 全模块只有常量和纯函数：没有请求、没有统计像素、
没有运行时去拉远程内容。署名是写死在产物里的静态文字，
所以 SECURITY.md 的「没有任何回传给作者的通道」继续成立——
往这个文件里加 import urllib 就等于把那句话变成假话。

**不进推送消息。** 群机器人的消息正文（jdy-report / jdy-watch /
jdy-flow-ops）一个字都不加：那条消息发进的是用户的工作群，不是我的版面。

关掉：`JDY_BRAND=0`（也认 false / off，不区分大小写）。
关掉时每个便利函数返回空串，调用方据此**整行不输出**，不留多余空行。
"""
import os

NAME = "aicliagent"
URL = "https://aicliagent.com"
LINE = "由 %s 生成 · %s" % (NAME, URL)

# 认作"关掉"的值。只此三个，写全在这里——散在各处判断一定会长歪。
OFF_VALUES = ("0", "false", "off")


def enabled():
    """默认开。`JDY_BRAND` 是 0 / false / off（不区分大小写、忽略首尾空白）时关。

    其余任何值都算开——包括拼错的。宁可多一行署名，也不要让一个笔误
    静默地把它关掉，然后没人知道为什么产物里没有。
    """
    return str(os.environ.get("JDY_BRAND", "")).strip().lower() not in OFF_VALUES


def md_footer():
    """Markdown 一行（斜体）。关掉时返回空串。

    调用方约定：`if foot: out += ["", foot]`——空串时连那个空行也别加。
    """
    return "_%s_" % LINE if enabled() else ""


def html_footer():
    """HTML 一行。样式内联，链接可点。关掉时返回空串。

    样式写在 style 属性里而不是靠页面的 CSS 类：产物是自包含单文件，
    署名行不该依赖调用方恰好定义了某个 class。
    """
    if not enabled():
        return ""
    return ('<p style="margin:36px 0 0;font-size:12px;color:#9aa4b2;'
            'text-align:right">由 <a href="%s" style="color:inherit">%s</a>'
            ' 生成</p>' % (URL, NAME))


def comment(prefix="# "):
    """源码/脚本的头注释一行，prefix 是该语言的注释符。关掉时返回空串。"""
    return prefix + LINE if enabled() else ""


def strip_md_footer(text):
    """把 md_footer() 那一行（连同它上面的分隔线）从文本末尾摘掉。

    **为什么必须有这个函数**：署名落在周报 Markdown 文件里，而那份文件正是
    `jdy-report/push.py` 推给群机器人的正文。加了署名却不摘，等于把广告发进
    用户的工作群——一件事的两半只照顾了一半，另一半是静默的。

    按 `LINE` 常量匹配，**不看 `enabled()`**：报表可能是开着署名生成的，
    推送时环境变量早就不是同一个了；认得出这一行就摘掉，与开关无关。
    """
    lines = str(text).split("\n")

    def _drop_blanks():
        while lines and not lines[-1].strip():
            lines.pop()

    _drop_blanks()
    if not (lines and LINE in lines[-1]):
        return text
    lines.pop()
    _drop_blanks()
    tail = lines[-1].strip() if lines else ""
    if len(tail) >= 3 and set(tail) == {"-"}:        # md_footer 上面那条分隔线
        lines.pop()
        _drop_blanks()
    return "\n".join(lines) + "\n"
