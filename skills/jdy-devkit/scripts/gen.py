#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给某张表单生成一套可直接用的集成代码。只读。

官方只有 demo 仓库、没有 SDK，做集成的人得自己摸字段标识、自己试写入形状。
这里一次性产出：字段对照表、能跑的请求样例（curl / Python）、
以及一个照着该表单类型生成的校验函数。

生成的代码**必须真能跑**——每个样例里的字段标识、可写形状都来自实际结构。
"""
import argparse
import json
import os
import sys

import _bootstrap  # noqa: F401
import brand
from jdy_client import (API_BASE, NOT_WRITABLE_TYPES, READ_ONLY_TYPES, JdyClient,
                        cli_main, col_width, describe_targets, pad, print_targets,
                        resolve_app, resolve_entry)
from shapes import shape_of

TRIGGERS = ("简道云 SDK", "字段标识对照", "怎么调简道云接口", "请求样例",
            "接入简道云", "字段 ID 是什么", "写入格式", "集成代码")


def _brand_comment(prefix="# "):
    """生成物头部的署名注释行（含换行）。关掉时是空串——不留空行。"""
    line = brand.comment(prefix)
    return line + "\n" if line else ""


def field_table(widgets):
    rows = []
    for w in widgets:
        writable, example, note = shape_of(w["type"])
        if w["type"] in NOT_WRITABLE_TYPES or w["type"] in READ_ONLY_TYPES:
            writable = False
        rows.append({"label": w["label"], "name": w["name"], "type": w["type"],
                     "writable": writable, "example": example, "note": note})
    return rows


def render_markdown(app_id, entry_id, form_name, rows):
    out = ["# %s · 字段对照与写入形状" % form_name, "",
           "- `app_id` = `%s`" % app_id,
           "- `entry_id` = `%s`" % entry_id, "",
           "读数据用**显示名**，写数据用**字段标识**（`_widget_...`）——",
           "这是简道云集成里最先踩的坑：两边不是一个东西。", "",
           "| 显示名 | 字段标识 | 类型 | 可写 | 写入形状 |",
           "|---|---|---|:--:|---|"]
    for r in rows:
        shape = r["example"] if r["writable"] else "—"
        out.append("| %s | `%s` | %s | %s | %s |"
                   % (r["label"], r["name"], r["type"],
                      "✅" if r["writable"] else "❌",
                      ("`%s`" % shape.replace("\n", " ")) if r["writable"] else "—"))
    out += ["", "## 逐字段说明", ""]
    for r in rows:
        out.append("- **%s**（`%s`，%s）：%s" % (r["label"], r["name"], r["type"], r["note"]))
    return "\n".join(out) + "\n"


def render_curl(app_id, entry_id, rows):
    writable = [r for r in rows if r["writable"]][:3]
    data = ",\n      ".join(
        '"%s": {"value": %s}' % (r["name"], r["example"].replace("\n", " "))
        for r in writable)
    return '''#!/usr/bin/env bash
%s# 简道云接口样例。字段标识与写入形状都取自该表单的实际结构，可直接跑。
# 密钥别写进脚本：export JDY_API_KEY=...
set -euo pipefail
: "${JDY_API_KEY:?请先 export JDY_API_KEY}"
APP=%s
ENTRY=%s
API=%s

# 1) 读一页数据（游标分页：把上一页最后一条的 _id 作为 data_id 传入取下一页）
curl -s -X POST "$API/app/entry/data/list" \\
  -H "Authorization: Bearer $JDY_API_KEY" -H "Content-Type: application/json" \\
  -d "{\\"app_id\\":\\"$APP\\",\\"entry_id\\":\\"$ENTRY\\",\\"limit\\":100}"

# 2) 新建一条。**注意每个字段都要包一层 {"value": ...}**
curl -s -X POST "$API/app/entry/data/create" \\
  -H "Authorization: Bearer $JDY_API_KEY" -H "Content-Type: application/json" \\
  -d '{"app_id":"'"$APP"'","entry_id":"'"$ENTRY"'","data":{
      %s
  }}'

# 3) 批量新建：data_list ≤100 条，带 transaction_id 可幂等重试
#    ⚠️ 接口几乎不校验：脏值会被静默存成 null 并返回成功。
#       写完一定要回读比对，别只看 HTTP 200。
''' % (_brand_comment(), app_id, entry_id, API_BASE, data)


def render_python(app_id, entry_id, rows):
    writable = [r for r in rows if r["writable"]][:3]
    fields = "\n".join('        "%s": {"value": %s},   # %s'
                       % (r["name"], r["example"].replace("\n", " "), r["label"])
                       for r in writable)
    return '''# -*- coding: utf-8 -*-
%s"""简道云接入样例。零第三方依赖，标准库即可跑。

字段标识与写入形状取自该表单实际结构。密钥从环境变量读，别写进代码。
"""
import json
import os
import urllib.request

API = "%s"
APP = "%s"
ENTRY = "%s"
KEY = os.environ["JDY_API_KEY"]


def call(path, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def iter_all():
    """游标分页。简道云没有 offset，只能拿上一页最后一条的 _id 往下翻。"""
    cursor = None
    while True:
        body = {"app_id": APP, "entry_id": ENTRY, "limit": 100}
        if cursor:
            body["data_id"] = cursor
        page = call("/app/entry/data/list", body).get("data", [])
        if not page:
            return
        for row in page:
            yield row
        cursor = page[-1]["_id"]


def create_one():
    data = {
%s
    }
    return call("/app/entry/data/create",
                {"app_id": APP, "entry_id": ENTRY, "data": data})


if __name__ == "__main__":
    print("共 %%d 行" %% sum(1 for _ in iter_all()))
    # 写入前请先读一遍 fields.md 的「写入形状」列。
    # 接口几乎不校验：脏值静默存 null 并返回成功，务必写后回读比对。
''' % (_brand_comment(), API_BASE, app_id, entry_id, fields)


def render_validator(rows):
    checks = []
    for r in rows:
        if not r["writable"]:
            checks.append('    if %r in row:\n'
                          '        problems.append("%s 写不进去：%s")'
                          % (r["label"], r["label"], r["note"]))
    body = "\n".join(checks) or "    pass"
    types = {r["label"]: r["type"] for r in rows if r["writable"]}
    return '''# -*- coding: utf-8 -*-
%s"""照该表单结构生成的入参校验。写之前先跑一遍，省得写完才发现被静默丢弃。

这里只做**结构性**校验（字段存不存在、能不能写、类型对不对），
不做业务规则——那是你的领域知识，生成不出来。
"""

FIELD_TYPES = %s

NEEDS_DICT = {"phone", "address"}
NEEDS_LIST = {"checkboxgroup", "combocheck", "usergroup", "deptgroup",
              "image", "upload", "subform"}
# 关联数据能写，但接口**不校验引用是否存在**：写个不存在的 ID 照样成功，
# 回读也发现不了（读回来就是你写进去的那个 ID）。这一条自动校验不了，
# 只能提醒——所以它出现在 warnings 里而不是 problems 里。
LOOKUP_TYPES = {"lookup"}


def validate(row, warnings=None):
    """row 是 {显示名: 值}。返回问题列表，空列表表示可以写。

    warnings 传一个 list 进来，可以额外收到"能写但有风险"的提醒。
    """
    problems = []
    if warnings is None:
        warnings = []
%s
    for label, value in row.items():
        wtype = FIELD_TYPES.get(label)
        if wtype is None:
            problems.append("表单里没有「%%s」这一列——写了会被静默忽略" %% label)
            continue
        if value is None:
            continue
        if wtype in NEEDS_DICT and not isinstance(value, dict):
            problems.append("「%%s」是 %%s，要给对象不是字符串，"
                            "给错会被静默存成 null" %% (label, wtype))
        if wtype in NEEDS_LIST and not isinstance(value, (list, tuple)):
            problems.append("「%%s」是 %%s，要给数组" %% (label, wtype))
        if wtype == "number":
            try:
                float(value)
            except (TypeError, ValueError):
                problems.append("「%%s」要能转成数字，当前是 %%r" %% (label, value))
        if wtype in LOOKUP_TYPES:
            warnings.append("「%%s」是关联数据：接口**不校验引用是否存在**，"
                            "写个不存在的 data_id 照样成功、回读也发现不了。"
                            "写前先确认目标记录真的在" %% label)
        if wtype == "datetime" and isinstance(value, str) and "T" not in value:
            problems.append("「%%s」要 ISO8601 带时区（2026-08-29T10:00:00.000Z），"
                            "当前是 %%r —— 格式不对会被静默存成 null" %% (label, value))
    return problems
''' % (_brand_comment(), json.dumps(types, ensure_ascii=False, indent=4), body)


def main():
    ap = argparse.ArgumentParser(description="生成简道云集成代码（只读）")
    ap.add_argument("--app", help="应用名或 ID；不确定就先 --list")
    ap.add_argument("--entry", help="表单名或 ID")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="列出应用；配合 --app 则列出该应用的表单")
    ap.add_argument("--out", help="输出目录，缺省只打印对照表")
    args = ap.parse_args()

    client = JdyClient()
    if args.do_list or not (args.app and args.entry):
        aid = resolve_app(client, args.app) if args.app else None
        print_targets(describe_targets(client, aid),
                      "应用：" if not aid else "该应用下的表单：")
        print("\n用法：gen.py --app <应用> --entry <表单> --out 输出目录")
        return 0
    args.app = resolve_app(client, args.app)
    args.entry = resolve_entry(client, args.app, args.entry)
    form_name = next((f["name"] for f in client.list_forms(args.app)
                      if f["entry_id"] == args.entry), args.entry)

    widgets = client.widgets(args.app, args.entry)
    rows = field_table(widgets)
    w = col_width([r["label"] for r in rows], 6)

    print("=" * 74)
    print("%s　%d 个字段（可写 %d）"
          % (form_name, len(rows), sum(1 for r in rows if r["writable"])))
    print("=" * 74)
    for r in rows:
        print("  %s %-26s %-12s %s"
              % (pad(r["label"], w), r["name"], r["type"],
                 r["example"].replace("\n", " ")[:28] if r["writable"] else "❌ " + r["note"][:26]))

    if not args.out:
        print("\n加 --out <目录> 生成对照表、请求样例与校验代码。")
        return 0

    os.makedirs(args.out, exist_ok=True)
    files = {
        "fields.md": render_markdown(args.app, args.entry, form_name, rows),
        "sample.sh": render_curl(args.app, args.entry, rows),
        "sample.py": render_python(args.app, args.entry, rows),
        "validate.py": render_validator(rows),
    }
    print()
    for name, text in files.items():
        path = os.path.join(args.out, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("  %s（%d 字节）" % (path, len(text.encode("utf-8"))))
    print("\nsample.sh / sample.py 里的字段标识与写入形状都取自实际结构，可直接跑。")
    print("validate.py 只做结构性校验，业务规则得你自己加。")
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(main))
