# -*- coding: utf-8 -*-
"""零依赖的 SVG 图表与自包含 HTML 报告。

为什么值得单独做：简道云**没有仪表盘 API**，想把查询结果给人看只能自己画。
而沙箱里没有 matplotlib、也装不了，Agent 每次都得手搓一遍 SVG——
搓出来的往往中文标签重叠、负值画穿、空数据直接崩。这里一次做对。

纯函数、不碰网络，所以能被完整测试。
"""
import datetime
import html

import brand
from jdy_client import dwidth

BAR_H = 26          # 每根条的高度
BAR_GAP = 8
LABEL_W = 150       # 左侧标签留宽（按显示宽度截断，中文占两列）
VALUE_W = 70
CHART_W = 720


def _esc(text):
    return html.escape(str(text), quote=True)


def truncate(text, width):
    """按显示宽度截断，超了补省略号。

    宽度一律走内核 dwidth（unicodedata 的东亚宽度）。这里原来有**两套**规则：
    一套按码点区间猜、一套"非 ASCII 就算两列"，彼此还对不上——
    于是同一个标签，判断"要不要截"和"截到哪"用的是两把尺子。
    """
    text = str(text)
    if dwidth(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = dwidth(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def bar_chart(pairs, title="", unit=""):
    """横向条形图。pairs 是 [(标签, 数值)]，已按调用方的顺序画。

    横向而不是纵向：中文标签放竖轴不会互相挤，也不用旋转文字。
    """
    pairs = [(str(k), float(v or 0)) for k, v in pairs]
    if not pairs:
        return ('<p class="empty">没有数据可画。</p>')
    top = max(abs(v) for _k, v in pairs) or 1.0
    height = len(pairs) * (BAR_H + BAR_GAP) + 24
    plot_w = CHART_W - LABEL_W - VALUE_W
    parts = ['<svg class="chart" viewBox="0 0 %d %d" width="100%%" '
             'role="img" aria-label="%s">' % (CHART_W, height, _esc(title or "图表"))]
    for i, (label, value) in enumerate(pairs):
        y = i * (BAR_H + BAR_GAP) + 12
        w = max(1.0, abs(value) / top * plot_w)
        parts.append(
            '<text x="%d" y="%d" class="lbl" text-anchor="end">%s</text>'
            % (LABEL_W - 10, y + BAR_H * 0.7, _esc(truncate(label, 18))))
        parts.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="3" '
                     'class="bar%s"/>' % (LABEL_W, y, w, BAR_H,
                                          " neg" if value < 0 else ""))
        parts.append('<text x="%.1f" y="%d" class="val">%s%s</text>'
                     % (LABEL_W + w + 8, y + BAR_H * 0.7, _fmt(value), _esc(unit)))
    parts.append("</svg>")
    return "".join(parts)


def _fmt(value):
    if value == int(value):
        return "{:,}".format(int(value))
    return "{:,.2f}".format(value)


def table(headers, rows, limit=200):
    """普通表格。行数超过 limit 就截断并**说出来**——不能默默少给。"""
    out = ["<table><thead><tr>"]
    out += ["<th>%s</th>" % _esc(h) for h in headers]
    out.append("</tr></thead><tbody>")
    for row in rows[:limit]:
        out.append("<tr>" + "".join(
            "<td>%s</td>" % _esc("" if c is None else c) for c in row) + "</tr>")
    out.append("</tbody></table>")
    if len(rows) > limit:
        out.append('<p class="note">共 %d 行，此处只列前 %d 行。'
                   '完整数据请用 --json-out 取。</p>' % (len(rows), limit))
    return "".join(out)


def page(title, blocks, subtitle="", generated_at=None):
    """自包含 HTML：样式内联、不引外部资源，双击就能看。"""
    stamp = (generated_at or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M")
    return """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title>
<style>
  :root{--ink:#1a1d24;--muted:#6b7280;--line:#e5e7eb;--bg:#fff;--bar:#2563eb;--neg:#dc2626}
  @media (prefers-color-scheme:dark){
    :root{--ink:#e8ecf3;--muted:#9aa4b2;--line:#2b3242;--bg:#12151c;--bar:#60a5fa;--neg:#f87171}}
  body{margin:0;padding:32px;background:var(--bg);color:var(--ink);
       font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:860px;margin:0 auto}
  h1{font-size:22px;margin:0 0 4px} .sub{color:var(--muted);margin:0 0 28px;font-size:13px}
  h2{font-size:16px;margin:32px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
  table{border-collapse:collapse;width:100%%;font-size:14px;display:block;overflow-x:auto}
  th,td{border-bottom:1px solid var(--line);padding:7px 10px;text-align:left;white-space:nowrap}
  th{color:var(--muted);font-weight:600}
  .chart{max-width:100%%;height:auto} .bar{fill:var(--bar)} .bar.neg{fill:var(--neg)}
  .lbl{font-size:13px;fill:var(--ink)} .val{font-size:13px;fill:var(--muted)}
  .note,.empty{color:var(--muted);font-size:13px}
</style></head><body><div class="wrap">
<h1>%s</h1><p class="sub">%s%s生成于 %s</p>
%s
%s
</div></body></html>
""" % (_esc(title), _esc(title), _esc(subtitle), "　·　" if subtitle else "",
       stamp, "\n".join(blocks), brand.html_footer())
