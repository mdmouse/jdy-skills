# -*- coding: utf-8 -*-
"""群机器人推送（企业微信 / 飞书 / 钉钉）。

**模块名叫 webhook 而不是 push**：jdy-report 的入口脚本就叫 push.py，
同名会让 `from push import …` 变成"谁先进 sys.path 谁赢"——实测直接撞出循环 import。
本仓库已经在 plan.py / apply.py 上栽过同一个坑。

**这里只管"怎么发"，不管"该不该发"。** 是否取得用户同意由调用方的 CLI 把关——
推送是对外动作、发出去收不回，每个入口都必须默认预览、显式 --send。

放进内核是因为它**不止报表要用**：流程催办也要往群里发，而技能是各自单独安装的，
jdy-flow-ops 引用不到 jdy-report 里的东西。同一份知识长在某个技能里，
下一个用得上它的人就享受不到——这个坑本项目已经踩过两次
（sync_value 之于 restore、UNVERIFIED_WRITE 之于新代码）。

约束：仅用标准库。
"""
import json
import re
import urllib.error
import urllib.request

__all__ = ["FLAVORS", "LIMIT", "build_payload", "check", "detect", "flatten_tables",
           "mask", "post", "preview_text"]


# 各家机器人的消息体不同，按 URL 主机名判断，别让用户自己选
FLAVORS = {
    "qyapi.weixin.qq.com": "wecom",
    "open.feishu.cn": "feishu",
    "open.larksuite.com": "feishu",
    "oapi.dingtalk.com": "dingtalk",
}
_SEP = re.compile(r"^[\s|:-]+$")     # 表格的分隔行
LIMIT = 4000            # 三家都有长度上限，取一个保守值


def detect(url):
    for host, flavor in FLAVORS.items():
        if host in url:
            return flavor
    return None


def _blur(text):
    return "%s…%s" % (text[:4], text[-4:]) if len(text) > 12 else "***"


def mask(url):
    """URL 里的密钥不能进日志。**三家的密钥不在同一个位置。**

    企微是 `?key=`、钉钉是 `?access_token=`——都在查询串上，原来只遮了这两种。
    而飞书/Lark 的密钥是**路径的最后一段**（`/open-apis/bot/v2/hook/<uuid>`），
    压根没有 `=`，于是一个字都没遮：nudge 每跑一次就把完整的群机器人地址
    明文打进 stdout，而这些输出会被 Agent 平台原样贴给用户、进日志、进工单。
    拿到这个 URL 的人可以直接往那个群发消息。

    查询串和路径是同一件事的两半，当初只顾了一半。
    """
    url = re.sub(r"(key|access_token)=([^&]+)",
                 lambda m: "%s=%s" % (m.group(1), _blur(m.group(2))), url)
    # 路径型：飞书/Lark 的密钥是 /hook/ 后面那一段。只遮**长得像凭证**的那种——
    # 把 /robot/send 里的 send 也遮掉，URL 就看不出是发给哪家的了，
    # 而遮蔽的目的是"还能认出目标、但拿不走凭证"。
    url = re.sub(r"(/(?:hook|hooks|robot|webhook|services)/)([A-Za-z0-9_-]{16,})"
                 r"(?=[/?#]|$)", lambda m: m.group(1) + _blur(m.group(2)), url)
    # 兜底：路径里任何一段长得像 UUID / 长随机串的都遮掉
    url = re.sub(r"/([0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}|[A-Za-z0-9_-]{32,})(?=[/?#]|$)",
                 lambda m: "/" + _blur(m.group(1)), url)
    return url


def flatten_tables(markdown):
    """把 GFM 表格摊平成一行一条。

    三家群机器人的 markdown **都不支持表格**（企微只认标题/加粗/链接/
    行内代码/引用/字体颜色，钉钉同理，飞书这边走的是纯文本）。
    报表是表格密集的，原样推过去就是一屏竖线——接口返回 200、
    脚本报"已发送"，而群里那条消息没法看。典型的"成功了但没用"。
    """
    out, table = [], []

    def flush():
        if not table:
            return
        header, rows = table[0], [r for r in table[1:] if not _SEP.match("|".join(r))]
        for row in rows:
            lead = row[0] if row else ""
            rest = ["%s %s" % (h, v) for h, v in zip(header[1:], row[1:])
                    if v and v != "—"]
            out.append("· %s%s" % (lead, ("：" + "，".join(rest)) if rest else ""))
        out.append("")
        del table[:]

    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped[1:-1].split("|")]
            table.append(cells)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out)


def build_payload(flavor, title, markdown, keep_tables=False):
    if not keep_tables:
        markdown = flatten_tables(markdown)
    if len(markdown) > LIMIT:
        markdown = markdown[:LIMIT] + "\n\n…（内容超长已截断，完整报表见附件/文件）"
    if flavor == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": markdown}}
    if flavor == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}}
    if flavor == "feishu":
        # 飞书群机器人的 markdown 支持有限，用富文本纯文本段最稳
        return {"msg_type": "text", "content": {"text": markdown}}
    raise ValueError("认不出的 webhook 类型")


def post(url, payload, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return None, str(exc.reason)

def preview_text(payload):
    """把要发出去的消息体还原成人能读的那段文字。

    预览必须是**真正会发出去的那份**：发送前还要摊平表格、截断超长，
    拿原文给用户看，那这道确认就白做了。
    """
    return (payload.get("markdown", {}).get("content")
            or payload.get("markdown", {}).get("text")
            or payload.get("content", {}).get("text", ""))


def looks_ok(status, body):
    """响应像不像成功。三家的成功标记不一样，且都可能 200 里带错误码。"""
    if status != 200:
        return False
    tight = body.replace(" ", "")
    if '"errcode":0' in tight or '"code":0' in tight:
        return True
    return "errcode" not in body and "code" not in body


def check(url, timeout=10):
    """只验通不通，**不发任何消息**：只发 GET，群机器人会自己拒掉，群里不留痕。

    存在的理由：不给一个"不发消息也能验通"的办法，调用方就会自己写一条
    「连通性探测」POST 过去——实测中就这么发生过，那是一条真消息，进了群。
    """
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            return resp.getcode(), None
    except urllib.error.HTTPError as exc:
        return exc.code, None                  # 机器人拒绝 GET 是正常的，说明可达
    except urllib.error.URLError as exc:
        return None, str(exc.reason)
