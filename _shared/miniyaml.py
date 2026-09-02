# -*- coding: utf-8 -*-
"""YAML 子集解析器，仅用标准库。

为什么需要：报表定义是人反复手写手改的配置，YAML 的可读性明显强于 JSON；
但沙箱里装不了 PyYAML（V3 实测：有 Python 3.13，无 pip 通道）。
所以自己实现一个够用的子集——同一份代码也用来解析 SKILL.md 的 frontmatter。

**支持**：块映射、块序列、行内流式 `{a: b}` / `[a, b]`、注释、引号字符串、
`|` 与 `>` 块标量、标量类型推断（int / float / bool / null / str）、
`---` 文档分隔（只取第一篇）。

**不支持**（遇到会明确报错，不静默猜）：锚点与引用 `&` `*`、标签 `!!`、
多文档合并、复杂键 `? :`、多行流式集合。
报表定义用不到这些；真需要时报错比默默解析错强。
"""
import re

__all__ = ["parse", "yaml_quote", "YamlError"]

_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
_NULL = {"", "null", "~"}
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_UNSUPPORTED = re.compile(r"^(&\S|\*\S|!!)")


class YamlError(ValueError):
    pass


def _scalar(text):
    """把标量文本转成 Python 值。带引号的一律当字符串。"""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    low = text.lower()
    if low in _NULL:
        return None
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


# 冒号要成为「键与值的分界」，后面必须跟空白或行尾——这是 YAML 本身的规则。
# 「只要有冒号就当映射」会把 `- http://a.com` 切成 {"http": "//a.com"}：
# 一个 URL 就这么变成了一个键，而且不报错。
_KEY_COLON = re.compile(r":(?=\s|$)")


def split_key_value(text):
    """按「冒号 + 空白或行尾」切出 (键, 值)。不是映射就返回 None。"""
    if text[:1] in "\"'":
        quote = text[0]
        end = text.find(quote, 1)
        if end < 0:
            return None
        after = text[end + 1:]
        if not after.startswith(":") or (after[1:2] and not after[1:2].isspace()):
            return None
        return text[:end + 1], after[1:].strip()
    m = _KEY_COLON.search(text)
    if m is None:
        return None
    return text[:m.start()], text[m.start() + 1:].strip()


_RISKY_CHARS = ":#,{}[]&*!|>%@`\"'"


def yaml_quote(text):
    """把一个显示名安全地写成 YAML 标量（键或值都用它）。

    这是解析那一头的对偶问题：显示名带 `:`、`#`、逗号、引号或首尾空白时，
    不加引号写出去的配置**自己解析不回来**——生成器产出一份损坏的草稿，
    看着正常、一跑就错。字段显示名是用户在简道云界面里随手起的，
    「金额(元)：含税」这种名字很常见。

    也挡住"看起来像别的类型"的名字：叫「2026」的字段不加引号会解析成整数，
    叫「on」的会解析成 True，然后按显示名去查字段就查不到了。
    """
    text = str(text)
    risky = (not text or text != text.strip() or _scalar(text) != text
             or any(c in text for c in _RISKY_CHARS) or text[:1] in "-?")
    if not risky:
        return text
    # 本子集解析引号串时只剥外层，**不处理反斜杠转义**（`_strip_comment`
    # 与 `_split_flow` 也只按引号配对）。所以这里不能靠 \" 转义，
    # 只能挑一个正文里没出现过的引号；两种都出现就是这个子集表达不了的名字，
    # 报错好过写出一份自己解析不回来的配置。
    for q in '"\'':
        if q not in text:
            return q + text + q
    raise YamlError(
        "字段名「%s」同时含单引号和双引号，本 YAML 子集无法表达。"
        "请在简道云里给这个字段改名，或手工写这一行配置。" % text)


def _strip_comment(line):
    """去掉行尾注释，但引号内的 # 不算。"""
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _split_flow(body):
    """按顶层逗号切分流式集合内容，忽略嵌套括号与引号里的逗号。"""
    parts, buf, depth, quote = [], [], 0, None
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return [p.strip() for p in parts]


def _parse_flow(text):
    """解析行内 `{a: b, c: d}` 或 `[a, b]`。"""
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        out = {}
        for item in _split_flow(text[1:-1]):
            pair = split_key_value(item)
            if pair is None:
                raise YamlError("流式映射的项不是「键: 值」（冒号后要有空格）：%r" % item)
            k, v = pair
            out[_scalar(k)] = _parse_value(v)
        return out
    if text.startswith("[") and text.endswith("]"):
        return [_parse_value(i) for i in _split_flow(text[1:-1])]
    raise YamlError("不是合法的流式集合：%r" % text)


def _parse_value(text):
    text = text.strip()
    if _UNSUPPORTED.match(text):
        # `b: *base` 这类别名出现在值的位置——行首检查看不到它，
        # 漏掉就会静默变成字符串 "*base"，配置就错得无声无息
        raise YamlError("不支持的 YAML 特性（锚点/引用/标签）：%r" % text)
    if text[:1] in "{[":
        return _parse_flow(text)
    return _scalar(text)


class _Reader(object):
    def __init__(self, text):
        raw = text.split("\n")
        # 只取第一篇文档
        if raw and raw[0].strip() == "---":
            raw = raw[1:]
        for i, line in enumerate(raw):
            if line.strip() in ("---", "..."):
                raw = raw[:i]
                break
        self.lines = raw
        self.i = 0

    def peek(self):
        """返回下一条有内容的行 (缩进, 文本)，跳过空行与整行注释。"""
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            stripped = _strip_comment(raw)
            if not stripped.strip():
                self.i += 1
                continue
            return len(raw) - len(raw.lstrip(" ")), stripped.strip(), raw
        return None, None, None

    def take_block_scalar(self, indent, style):
        """读 `|` / `>` 之后的缩进块。

        块内容的缩进由**首个非空行**决定（YAML 的规则），不能按父级缩进硬算——
        写成 indent+1 会把内容多剥或少剥一格。
        """
        chunk, block_indent = [], None
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if not raw.strip():
                chunk.append("")
                self.i += 1
                continue
            cur = len(raw) - len(raw.lstrip(" "))
            if cur <= indent:
                break
            if block_indent is None:
                block_indent = cur
            chunk.append(raw[block_indent:] if len(raw) > block_indent else "")
            self.i += 1
        while chunk and not chunk[-1]:
            chunk.pop()
        if style.startswith("|"):
            text = "\n".join(chunk)
        else:
            text = " ".join(c.strip() for c in chunk if c.strip())
        return text.strip("\n") if style.endswith("-") else text


def _parse_block(reader, indent):
    """解析缩进 >= indent 的一段块结构，返回 dict 或 list。"""
    cur_indent, text, raw = reader.peek()
    if text is None or cur_indent < indent:
        return None
    if text.startswith("- "):
        return _parse_list(reader, cur_indent)
    return _parse_map(reader, cur_indent)


def _parse_list(reader, indent):
    items = []
    while True:
        cur, text, raw = reader.peek()
        if text is None or cur < indent or not text.startswith("- "):
            if text is not None and cur == indent and not text.startswith("- ") and items:
                break
            if text is None or cur != indent:
                break
        rest = text[2:].strip()
        reader.i += 1
        if not rest:
            items.append(_parse_block(reader, indent + 1))
        elif rest[:1] in "{[":
            items.append(_parse_flow(rest))
        elif split_key_value(rest) is not None:
            # `- key: value` —— 列表项本身是个映射，把它当成从这一列开始的块
            key, value = split_key_value(rest)
            entry = {}
            _assign(reader, entry, key.strip(), value, indent + 3, indent + 2)
            nested = _parse_map(reader, indent + 2, into=entry)
            items.append(nested)
        else:
            items.append(_scalar(rest))
    return items


def _assign(reader, target, key, value, child_indent, key_indent):
    """key_indent 是键本身所在的列。块标量靠它判断在哪结束——
    用 child_indent 做相对偏移在列表项里会算错（`- key:` 的键实际缩进是 indent+2）。"""
    key = _scalar(key)
    if value in ("|", ">", "|-", ">-", "|+", ">+"):
        target[key] = reader.take_block_scalar(key_indent, value)
    elif value == "":
        nested = _parse_block(reader, child_indent)
        target[key] = {} if nested is None else nested
    else:
        target[key] = _parse_value(value)


def _parse_map(reader, indent, into=None):
    out = {} if into is None else into
    while True:
        cur, text, raw = reader.peek()
        if text is None or cur < indent:
            break
        if cur > indent:
            raise YamlError("意外的缩进（第 %d 行）：%r" % (reader.i + 1, raw))
        if text.startswith("- "):
            break
        if _UNSUPPORTED.match(text):
            raise YamlError("不支持的 YAML 特性（锚点/引用/标签）：%r" % text)
        pair = split_key_value(text)
        if pair is None:
            raise YamlError("第 %d 行不是合法的键值对（冒号后要有空格或换行）：%r"
                            % (reader.i + 1, text))
        key, value = pair
        reader.i += 1
        _assign(reader, out, key.strip(), value, indent + 1, indent)
    return out


def parse(text):
    """解析 YAML 子集，返回 dict / list / 标量。"""
    if not text or not text.strip():
        return None
    reader = _Reader(text)
    cur, first, _ = reader.peek()
    if first is None:
        return None
    if first[:1] in "{[":
        return _parse_flow(first)
    result = _parse_block(reader, cur)
    cur2, leftover, raw2 = reader.peek()
    if leftover is not None:
        raise YamlError("解析后仍有未消费的内容（第 %d 行）：%r" % (reader.i + 1, raw2))
    return result
