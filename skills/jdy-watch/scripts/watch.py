#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据哨兵：按规则定时巡检简道云，命中就推到群里。默认只预览，不发送。

为什么是本地轮询而不是 Webhook：简道云的数据推送要一个**公网可达的服务端**，
而这套技能跑在桌面 Agent 里——没有公网入口，但有定时任务。轮询正好补这个位。

代价也说清楚：轮询有间隔，不是实时；而且**改动检测只能靠本地比对**——
实测简道云的 filter 对 `updateTime`/`createTime` 这些系统字段一律**静默忽略**
（照常 200、返回整表），所以没有"只拉改过的行"这条路。

只读简道云数据，不写回任何东西。
"""
import argparse
import datetime
import os
import sys

import _bootstrap  # noqa: F401

# 本脚本对应的触发词。**加了新能力就要往这里补**，
# 并同步写进 SKILL.md 的 description——测试会核对两边一致。
# 代码里的触发词保持**领域中立**（tests/test_domain_neutral.py 扫的是代码字符串）；
# 「库存告警」「新单提醒」这类领域说法留在 SKILL.md 的 description 里——
# 那是文案不是引擎逻辑，而触发契约只要求 TRIGGERS ⊆ description。
TRIGGERS = ("数据哨兵", "盯着", "有变化就通知我", "定时巡检", "到了阈值提醒我",
            "监控某某表")
from jdy_client import (JdyClient, JdyError, ask_yes, build_filter, cli_main,
                        col_width, pad, resolve_app, resolve_entry)
from miniyaml import parse as parse_yaml
from rules import RuleError, State, evaluate, load_config, rule_kind
from webhook import (FLAVORS, build_payload, check, detect, looks_ok, mask, post,
                     preview_text)


def run_rule(client, rule, state, now):
    """跑一条规则。只读。"""
    app = resolve_app(client, rule["app"])
    entry = resolve_entry(client, app, rule["entry"])
    by_label, by_name = client.field_map(app, entry)
    data_filter = build_filter(rule.get("when"), by_label, by_name) if rule.get("when") else None
    rows = client.fetch_all(app, entry, data_filter=data_filter, limit=rule.get("limit"))
    result = evaluate(rule, rows, by_label, state, now=now)
    result["rule"] = rule
    return result


def render(results):
    """把命中拼成一条消息。群机器人不支持表格，写成一行一条。"""
    out = ["**数据哨兵**", ""]
    for r in results:
        if not r["hits"]:
            continue
        out.append("**%s**（%d 条）" % (r["rule"]["name"], len(r["hits"])))
        for hit in r["hits"][:10]:
            out.append("· %s" % (hit["text"] or hit["row_id"]))
        if len(r["hits"]) > 10:
            out.append("· …另有 %d 条" % (len(r["hits"]) - 10))
        out.append("")
    return "\n".join(out).rstrip()


def main():
    ap = argparse.ArgumentParser(description="数据哨兵：按规则巡检并推送（默认只预览）")
    ap.add_argument("config", help="规则文件（YAML 或 JSON）")
    ap.add_argument("--only", action="append", help="只跑指定规则名，可重复")
    ap.add_argument("--state", default=None,
                    help="去重状态文件；缺省自动找一个可写目录（优先 $JDY_HOME，"
                         "再 ~/.jdy，再宿主配置目录）")
    ap.add_argument("--webhook", help="群机器人 URL；缺省读规则文件的 webhook "
                                      "或环境变量 JDY_WATCH_WEBHOOK")
    ap.add_argument("--platform", choices=sorted(set(FLAVORS.values())),
                    help="强制指定消息体格式。默认按 URL 主机名判断")
    ap.add_argument("--title", default="数据哨兵", help="钉钉需要标题")
    ap.add_argument("--send", action="store_true", help="真的发送。不加只打印将要发的内容")
    ap.add_argument("--yes", action="store_true",
                    help="已向用户取得发送同意（非交互环境配合 --send 时必须给）")
    ap.add_argument("--check", action="store_true",
                    help="只验 webhook 通不通，**不发任何消息**")
    ap.add_argument("--dry-state", action="store_true",
                    help="不写去重状态。**只用于试规则**——正常巡检必须写，"
                         "否则每轮都把同样的东西再报一遍")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config, parse_yaml)
    except (RuleError, OSError) as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    url = (args.webhook or cfg.get("webhook")
           or os.environ.get("JDY_WATCH_WEBHOOK") or os.environ.get("JDY_REPORT_WEBHOOK"))
    if args.check:
        if not url:
            sys.stderr.write("缺少 webhook：规则文件里写 webhook，或用 --webhook，"
                             "或设环境变量 JDY_WATCH_WEBHOOK\n")
            return 2
        code, err = check(url)
        if err:
            sys.stderr.write("连不上 %s：%s\n" % (mask(url), err))
            return 2
        print("可达：%s（HTTP %s）—— 未发送任何消息" % (mask(url), code))
        return 0

    rules = cfg["rules"]
    if args.only:
        wanted = set(args.only)
        rules = [r for r in rules if r["name"] in wanted]
        if not rules:
            sys.stderr.write("--only 没有匹配到任何规则\n")
            return 2

    client = JdyClient()
    state = State(args.state or cfg.get("state") or None)
    now = datetime.datetime.now(datetime.timezone.utc)

    results, failed = [], []
    for rule in rules:
        try:
            results.append(run_rule(client, rule, state, now))
        except (JdyError, ValueError) as exc:
            # 一条规则挂了不该拖垮其余的——但必须报出来，
            # 静默跳过等于用户以为在盯着、其实没盯
            failed.append((rule["name"], str(exc)))

    print("=" * 70)
    print("数据哨兵　%d 条规则" % len(rules))
    print("=" * 70)
    if state.corrupt:
        # 哨兵最不该出的事不是报错，是**不响**。状态文件坏了就等于忘了"提醒到哪儿"，
        # 这一轮会把还命中的条目重报一遍——那是噪音，可以接受；
        # 但必须说出来，否则用户会以为这是新出的一批。
        print("\n⚠️ 去重状态文件读不成形状（%s）" % state.corrupt)
        print("   **这一轮可能把已经提醒过的再报一遍**——那是重复，不是新增。\n")
        # 坏文件挪没挪，要等 save() 之后才知道（--dry-state 或目录不可写就不会挪）。
        # 原来这句话在这里就打了，于是 --dry-state 下它是**空头承诺**：
        # 屏幕上说"已改名存到 .corrupt"，磁盘上什么都没发生。
        # 契约挪了位置，话术也得跟着挪。
    w = col_width([r["rule"]["name"] for r in results] + [f[0] for f in failed], 10)
    for r in results:
        note = ""
        if r["first_run"]:
            note = "　（首次运行：只建基准、不提醒）"
        elif r["suppressed"]:
            note = "　（%d 条被去重压住）" % len(r["suppressed"])
        print("  %s 扫 %d 行　命中 %d%s"
              % (pad(r["rule"]["name"], w), r["scanned"], len(r["hits"]), note))
    for name, why in failed:
        print("  %s ❌ %s" % (pad(name, w), why))

    if not args.dry_state:
        state.save()
        if state.kept_aside:
            print("\n坏掉的状态文件已改名存到 %s，新状态已重建。" % state.kept_aside)
        elif state.corrupt:
            print("\n⚠️ 新状态没能落盘，坏文件仍在 %s——下一轮还会报同样的损坏。"
                  % state.path)
        if state.readonly:
            print("\n⚠️ 去重状态写不进去（目录不可写）——**下一轮会把这些再报一遍**。"
                  "把 --state 指到可写位置，或设 JDY_HOME。")
        elif state.home is not None and state.home.note():
            # 降级到了别处也要说。存进了临时目录 = 下一轮等于没存，
            # 屏幕上不说这一句，用户就会以为哨兵记住了。
            print("\n%s" % state.home.note())
    else:
        print("\n（--dry-state：没有写去重状态，这一轮的命中下次还会再报）")
        if state.corrupt:
            print("　　坏掉的状态文件**原样留在** %s——没落盘就不动它，"
                  "下一轮还会报同样的损坏。" % state.path)

    total = sum(len(r["hits"]) for r in results)
    if state.corrupt and not args.dry_state:
        # 定时跑的时候没人看输出，退出码是唯一会被注意到的信号
        print("\n（因状态文件损坏，本次退出码为 3——定时任务请据此告警）")
    if not total:
        print("\n没有需要提醒的。")
        return 0 if not failed and not state.corrupt else 3

    message = render(results)
    if not url:
        print("\n（没配 webhook，只在这里汇总）")
        print("-" * 70)
        print(message)
        return 0 if not failed and not state.corrupt else 3

    flavor = args.platform or detect(url)
    if not flavor:
        sys.stderr.write("认不出这个 webhook 属于哪家（支持企业微信/飞书/钉钉）：%s\n"
                         % mask(url))
        return 2
    payload = build_payload(flavor, args.title, message)

    if not args.send:
        print("\n" + "=" * 70)
        print("预览模式 —— 未发送任何消息")
        print("=" * 70)
        print("目标   ：%s（%s）" % (mask(url), flavor))
        print("-" * 70)
        print(preview_text(payload))
        print("-" * 70)
        print("\n把上面这条给用户确认后，加 --send --yes 真正发送。")
        return 0 if not failed and not state.corrupt else 3

    if not args.yes:
        # 原来只判 `not sys.stdin.isatty()`：isatty() 谎报 True（Windows 的 NUL）
        # 时这道闸门整个失效，告警直接推进群。改成没 --yes 就必须真问一次。
        answered = ask_yes("确认把这条告警发进群？输入 yes：")
        if answered is None:
            sys.stderr.write(
                "拒绝发送：当前是非交互环境，无法当面向用户确认。\n"
                "请先去掉 --send 跑一次预览、把内容给用户看、取得明确同意，\n"
                "再加 --yes 重新执行。\n")
            return 4
        if not answered:
            print("已取消")
            return 0 if not failed and not state.corrupt else 3

    status, body = post(url, payload)
    if status is None:
        sys.stderr.write("发送失败（网络）：%s\n" % body)
        return 3
    print("已发送到 %s（%s）→ HTTP %d" % (mask(url), flavor, status))
    if not looks_ok(status, body):
        sys.stderr.write("\n⚠️ 响应不像成功：%s\n" % body[:200])
        return 3
    return 0 if not failed and not state.corrupt else 3


if __name__ == "__main__":
    sys.exit(cli_main(main))
