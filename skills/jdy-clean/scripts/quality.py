# -*- coding: utf-8 -*-
"""数据质量的度量。纯函数，不碰网络——所以能被完整测试。

刻意不认识任何业务概念：它只看**值的形状**，不看值的含义。
"手机号该是 11 位"这种判断属于领域知识，不属于通用引擎。
引擎只能说"这一列里 90% 的值长这样，剩下 10% 长那样"，
剩下的判断交给人或 Agent。
"""
import re
import unicodedata

_DIGIT = re.compile(r"[0-9]")
_ALPHA = re.compile(r"[A-Za-z]")
_HANZI = re.compile(r"[一-鿿]")
_SPACE_EDGE = re.compile(r"^\s|\s$")
# 只认全角**字母数字**。全角标点（：，。！？）在中文里是正确写法，
# 把「示例：王猛」改成「示例:王猛」是破坏数据，不是规范化。
_FULLWIDTH_ALNUM = re.compile(r"[Ａ-Ｚａ-ｚ０-９]")
_IDEO_SPACE = "　"


def shape(value, cap=40):
    """把一个值压成"形状"：数字→9，字母→a，汉字→中，其余原样保留。

    `138-0000-0000` 与 `13800000000` 形状不同，一眼能看出同一列里混着两种写法。
    这是判断"格式不统一"最省事又不需要懂业务的办法。
    """
    text = "" if value is None else str(value)
    out = []
    for ch in text[:cap]:
        if _DIGIT.match(ch):
            out.append("9")
        elif _ALPHA.match(ch):
            out.append("a")
        elif _HANZI.match(ch):
            out.append("中")
        else:
            out.append(ch)
    # 连续同类压缩：9999 → 9{4}，否则形状会碎成一堆长短不一的串
    packed, i = [], 0
    while i < len(out):
        j = i
        while j < len(out) and out[j] == out[i]:
            j += 1
        packed.append(out[i] if j - i == 1 else "%s{%d}" % (out[i], j - i))
        i = j
    return "".join(packed) + ("…" if len(text) > cap else "")


def issues_of(value):
    """一个值本身的毛病。只报**客观可见**的，不猜意图。"""
    if value is None:
        return []
    text = str(value)
    if not text:
        return []
    found = []
    if _SPACE_EDGE.search(text):
        found.append("首尾空白")
    if "  " in text:
        found.append("连续空格")
    if _FULLWIDTH_ALNUM.search(text):
        found.append("含全角字母数字")
    if _IDEO_SPACE in text:
        found.append("含全角空格")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        found.append("含控制字符")
    return found


def normalized(value):
    """规范化：去首尾空白、压缩连续空格、全角转半角、去控制字符。

    **不改变语义**——不猜格式、不补零、不改大小写。
    那些属于领域规则，引擎不该替用户决定。
    """
    if value is None:
        return None
    text = "".join(ch for ch in str(value)
                   if unicodedata.category(ch) != "Cc" or ch in "\t\n")
    text = "".join(chr(ord(ch) - 0xFEE0) if _FULLWIDTH_ALNUM.match(ch)
                   else (" " if ch == _IDEO_SPACE else ch)
                   for ch in text)
    return re.sub(r"[ \t]+", " ", text).strip()


def column_profile(values):
    """一列的画像：填充率、唯一度、形状分布、逐值毛病计数。"""
    total = len(values)
    filled = [v for v in values if v not in (None, "", [], {})]
    shapes, samples, flaws = {}, {}, {}
    for v in filled:
        sig = shape(v)
        shapes[sig] = shapes.get(sig, 0) + 1
        # 每种形状留一个**真实样例**。只给形状是不够的：
        # `中{4}-9{2}×150` 看不出那是「闸门测试-01」，读的人得另外去捞值，
        # 捞完还可能只看见零头、漏掉占 87% 的大头——实测就这么漏过一次。
        samples.setdefault(sig, str(v)[:30])
        for kind in issues_of(v):
            flaws[kind] = flaws.get(kind, 0) + 1
    return {
        "total": total,
        "filled": len(filled),
        "fill_rate": (len(filled) / float(total)) if total else 0.0,
        "distinct": len(set(str(v) for v in filled)),
        "uniqueness": (len(set(str(v) for v in filled)) / float(len(filled)))
                      if filled else 0.0,
        "shapes": sorted(shapes.items(), key=lambda kv: -kv[1]),
        "samples": samples,
        "flaws": sorted(flaws.items(), key=lambda kv: -kv[1]),
    }


def duplicate_groups(rows, key_fn, min_size=2):
    """按 key_fn 分组，返回重复组。空键不算重复——那是缺失，不是重复。"""
    buckets = {}
    for row in rows:
        k = key_fn(row)
        if k in (None, "", [], {}):
            continue
        buckets.setdefault(str(k), []).append(row)
    return {k: v for k, v in buckets.items() if len(v) >= min_size}
