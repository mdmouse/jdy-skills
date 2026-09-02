#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把报表推到群机器人（企业微信 / 飞书 / 钉钉）。

**默认只预览，不发送。** 推送是对外动作、发出去收不回，必须显式 `--send`，
且要先把内容给用户看过、拿到同意。Webhook URL 里带密钥，日志一律掩码。

「怎么发」在内核 _shared/push.py——流程催办也要用它，而技能是各自单独安装的。
这里只留「该不该发」这道关卡。
"""
import argparse
import os
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 教训：label.py 的打标能力做完了却没进 description，
# 实测中 Agent 因此完全没触发本技能，自己从零写了脚本。
TRIGGERS = ("把报表发到群里", "定时报表")
import brand
from jdy_client import ask_yes, cli_main
from webhook import (FLAVORS, build_payload, check, detect, looks_ok, mask, post,
                     preview_text)


def main():
    ap = argparse.ArgumentParser(description="把报表推到群机器人（默认只预览）")
    ap.add_argument("markdown", help="报表 Markdown 文件")
    ap.add_argument("--webhook", help="群机器人 URL；缺省读环境变量 JDY_REPORT_WEBHOOK")
    ap.add_argument("--title", default="简道云报表", help="钉钉需要标题")
    ap.add_argument("--platform", choices=sorted(set(FLAVORS.values())),
                    help="强制指定消息体格式。默认按 URL 主机名自动判断；"
                         "webhook 走公司自建网关时主机名认不出来，用这个指定")
    ap.add_argument("--keep-tables", action="store_true",
                    help="保留 Markdown 表格原样。默认摊平成一行一条——"
                         "三家群机器人都不支持表格，原样推过去是一屏竖线")
    ap.add_argument("--send", action="store_true",
                    help="真的发送。不加只打印将要发送的内容")
    ap.add_argument("--yes", action="store_true",
                    help="已向用户取得发送同意（非交互环境配合 --send 时必须给）")
    ap.add_argument("--check", action="store_true",
                    help="只验 webhook 通不通，**不发任何消息**："
                         "只发 GET，群机器人会自己拒掉，群里不留痕")
    args = ap.parse_args()

    try:
        with open(args.markdown, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        sys.stderr.write("读不到报表文件：%s\n" % exc)
        return 2

    # 群消息正文**一个字的品牌都不加**——那是用户的工作群，不是我的版面。
    # 署名只留在生成的文件里，推送前在这里摘掉。预览和真发都走这一句，
    # 所以用户看到的和群里收到的仍然是同一份。
    content = brand.strip_md_footer(content)

    url = args.webhook or os.environ.get("JDY_REPORT_WEBHOOK")
    if not url:
        sys.stderr.write("缺少 webhook：用 --webhook 或设环境变量 JDY_REPORT_WEBHOOK\n")
        return 2
    if args.check:
        code, err = check(url)
        if err:
            sys.stderr.write("连不上 %s：%s\n" % (mask(url), err))
            return 2
        print("可达：%s（HTTP %s）—— 未发送任何消息" % (mask(url), code))
        return 0

    flavor = args.platform or detect(url)
    if not flavor:
        sys.stderr.write("认不出这个 webhook 属于哪家（支持企业微信/飞书/钉钉）：%s\n" % mask(url))
        return 2

    payload = build_payload(flavor, args.title, content, args.keep_tables)

    if not args.send:
        print("=" * 60)
        print("预览模式 —— 未发送任何消息")
        print("=" * 60)
        print("目标   ：%s（%s）" % (mask(url), flavor))
        # 预览必须是**真正会发出去的那份**。原来打的是文件原文，
        # 而发送前还要摊平表格——用户确认的和实际发出的不是同一个东西，
        # 那这道确认就白做了。
        shown = preview_text(payload)
        print("字数   ：%d（原文 %d，已按群机器人能显示的格式处理）"
              % (len(shown), len(content)))
        print("-" * 60)
        print(shown[:1500] + ("\n…（预览截断）" if len(shown) > 1500 else ""))
        print("-" * 60)
        print("\n把上面的内容给用户确认后，加 --send --yes 真正发送。")
        return 0

    if not args.yes:
        # 发进群是对外动作，收不回。非交互环境问不了用户，就不能默认同意——
        # 和导入、同步、审批同一条规矩。
        # 原来只判 `not sys.stdin.isatty()`：isatty() 谎报 True（Windows 的 NUL）
        # 时这道闸门整个失效，消息直接发出去。改成没 --yes 就必须真问一次。
        answered = ask_yes("确认把这份报表发进群？输入 yes：")
        if answered is None:
            sys.stderr.write(
                "拒绝发送：当前是非交互环境，无法当面向用户确认。\n"
                "请先去掉 --send 跑一次预览、把内容给用户看、取得明确同意，\n"
                "再加 --yes 重新执行。\n")
            return 4
        if not answered:
            print("已取消")
            return 0

    status, body = post(url, payload)
    if status is None:
        sys.stderr.write("发送失败（网络）：%s\n" % body)
        return 3
    ok = looks_ok(status, body)
    print("已发送到 %s（%s）→ HTTP %d" % (mask(url), flavor, status))
    print("响应：%s" % body[:300])
    if not ok:
        sys.stderr.write("\n⚠️ 响应不像成功——群机器人常见失败是"
                         "关键词不匹配、IP 白名单、加签校验，请核对机器人配置。\n")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
