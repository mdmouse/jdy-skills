# -*- coding: utf-8 -*-
"""流程操作的共用层：成员编号解析、待办拉取、实例分析、审计日志。

接口版本是混的（读 v6、写 v1/v2），路径里都带版本前缀交给内核处理。
"""
import datetime
import json
import os

import platform_env
from jdy_client import JdyError, display_value, parse_iso

TASK_LIST = "/v6/workflow/task/list"
INSTANCE_GET = "/v6/workflow/instance/get"
APPROVE = "/v1/workflow/task/approve"
REJECT = "/v1/workflow/task/reject"
TRANSFER = "/v1/workflow/task/transfer"
ROLLBACK = "/v2/workflow/task/rollback"

DATA_GET = "/app/entry/data/get"          # v5，取单条业务数据

INSTANCE_STATUS = {0: "进行中", 1: "已完成", 2: "手动结束"}
# 审计日志与成员缓存**落在哪儿要运行时决定**：WorkBuddy 沙箱不放行 ~/.jdy，
# 另外两端未知。原来是写死 ~/.jdy，写不进去就 return None——于是每一次批量审批
# 都没有留痕，而同一时刻会话工作目录明明是可写的。审批是有责任归属的动作，
# "沙箱不让写"不该等于"不留痕"，只该等于"换个地方留"。


def audit_path():
    """审计日志的落点。一个可写目录都没有时返回 None。"""
    return platform_env.state_path("flow-audit.jsonl")


def user_cache_path():
    """成员编号缓存的落点。同上。"""
    return platform_env.state_path("cache", "usernames.json")


class FlowError(ValueError):
    pass


# --------------------------------------------------------------------------
# 成员编号
# --------------------------------------------------------------------------

def discover_users(client, limit_forms=40, refresh=False):
    """从各表单的数据里反查「姓名 → 成员编号」。

    简道云没有可用的通讯录查询接口，只能拿业务数据当索引：
    每行的 creator/updater 以及成员字段里都带着 name 与 username。
    因此它只覆盖出现过的人，是**辅助**而非权威。
    """
    cache = user_cache_path()
    if not refresh and cache and os.path.exists(cache):
        try:
            with open(cache, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            return {k: list(v) for k, v in cached.get("users", {}).items()}
        except (OSError, ValueError):
            pass

    index, scanned = {}, 0
    for app in client.list_apps():
        for form in client.list_forms(app["app_id"]):
            if scanned >= limit_forms:
                break
            scanned += 1
            try:
                widgets = client.widgets(app["app_id"], form["entry_id"])
                rows = client.fetch_all(app["app_id"], form["entry_id"],
                                        limit=20, page_size=20)
            except JdyError:
                continue
            user_fields = [w for w in widgets if w.get("type") in ("user", "usergroup")]
            for row in rows:
                holders = [row.get("creator"), row.get("updater")]
                for w in user_fields:
                    v = row.get(w["name"])
                    holders.extend(v if isinstance(v, list) else [v])
                for item in holders:
                    if isinstance(item, dict) and item.get("username") and item.get("name"):
                        index.setdefault(item["name"], set()).add(item["username"])
    index = {k: sorted(v) for k, v in index.items()}
    try:
        if not cache:
            return index                            # 哪儿都写不了就纯内存用
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"users": index}, fh, ensure_ascii=False)
    except OSError:
        pass                                        # 沙箱不可写就纯内存用
    return index


def resolve_username(client, who, refresh=False):
    """把用户说的「张三」变成接口要的成员编号。

    接口只认 `sys_xxx` 这种成员编号，传显示名会报 1010 用户不存在。
    重名时列出候选交用户裁决，绝不替他挑一个——挑错就是把别人的待办批了。
    """
    if not who:
        raise FlowError("没有指定成员。用 --user 传成员编号或姓名，"
                        "或在 ~/.jdy/config.json 里加一行 \"username\"")
    text = str(who).strip()
    if text.startswith("sys_") or text.startswith("jdy-"):
        return text
    index = discover_users(client, refresh=refresh)
    candidates = index.get(text)
    if not candidates:
        raise FlowError("在业务数据里查不到「%s」的成员编号。已知的有：%s\n"
                        "（该索引只覆盖在数据里出现过的人；也可以直接传 sys_ 开头的编号）"
                        % (text, "、".join(sorted(index)) or "无"))
    if len(candidates) > 1:
        raise FlowError("「%s」对应多个成员编号：%s —— 请直接指定其中一个"
                        % (text, "、".join(candidates)))
    return candidates[0]


def default_username(client):
    """配置文件里若写了 username 就用它，否则返回 None。"""
    try:
        with open(platform_env.find_config(), "r", encoding="utf-8") as fh:
            return (json.load(fh).get("username") or "").strip() or None
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------

def iter_tasks(client, username, page_size=100):
    """游标翻页拉全部待办。"""
    cursor = None
    while True:
        body = {"username": username, "limit": min(page_size, 100)}
        if cursor:
            body["task_id"] = cursor
        resp = client.post(TASK_LIST, body)
        tasks = resp.get("tasks", [])
        for t in tasks:
            yield t
        if not resp.get("has_more") or not tasks:
            return
        cursor = tasks[-1]["task_id"]


def get_instance(client, instance_id, with_tasks=True):
    return client.post(INSTANCE_GET,
                       {"instance_id": instance_id, "tasks_type": 1 if with_tasks else 0})


def parse_time(value):
    """走内核 parse_iso：不带时区的串按 UTC 补齐。

    原来这里直接 fromisoformat，接口返回不带 Z 的串时拿到的是 naive datetime，
    到 stuck_hours 里和 now(utc) 相减就抛一个裸 TypeError——
    "查看待办等了多久"这种只读操作不该以 traceback 收场。
    """
    return parse_iso(value)


def stuck_hours(task, now=None):
    """待办已停留多少小时。已完成的返回实际耗时。"""
    start = parse_time(task.get("create_time"))
    if start is None:
        return None
    end = parse_time(task.get("finish_time")) or now or datetime.datetime.now(
        datetime.timezone.utc)
    return round((end - start).total_seconds() / 3600.0, 1)


def humanize(hours):
    if hours is None:
        return "未知"
    if hours < 1:
        return "%d 分钟" % int(hours * 60)
    if hours < 48:
        return "%.1f 小时" % hours
    return "%.1f 天" % (hours / 24.0)


def task_content(client, task, max_fields=6):
    """取待办对应的业务数据内容。

    为什么必须做：`task/list` 只返回元数据（表单名／节点／人／时间），
    **一个业务字段都没有**。同一张表单的多条待办在调用方眼里长得一模一样。
    而人提审批永远是按内容说的——"把那条五万的批了"、"退回缺料的那几条"，
    没有内容就没法把人话对应到具体待办。

    instance_id 等同 data_id，待办里又带 app_id／form_id，所以能取回原始记录。
    """
    try:
        resp = client.post(DATA_GET, {"app_id": task["app_id"],
                                      "entry_id": task["form_id"],
                                      "data_id": task["instance_id"]})
    except (JdyError, KeyError):
        return {}
    row = resp.get("data") or {}
    try:
        by_label, _ = client.field_map(task["app_id"], task["form_id"])
    except JdyError:
        return {}
    out = {}
    for label, widget in by_label.items():
        value = display_value(row.get(widget["name"]), widget["type"])
        if value not in (None, "", [], {}):
            out[label] = value
        if len(out) >= max_fields:
            break
    return out


def content_text(content):
    """把内容压成一行，供关键词匹配与展示。"""
    return "　".join("%s=%s" % (k, v) for k, v in content.items())


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------

def audit(action, username, task, result, comment=None):
    """每次写操作都留痕。审批是有责任归属的动作，出了事要查得到谁批的。"""
    record = {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action": action,
        "operator": username,
        "task_id": task.get("task_id"),
        "instance_id": task.get("instance_id"),
        "form_title": task.get("form_title"),
        "node": task.get("flow_name"),
        "comment": comment,
        "result": result,
    }
    path = audit_path()
    if not path:
        return None                                 # 一个可写目录都没有，调用方要说出来
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path
    except OSError:
        return None                                 # 沙箱不可写时不阻断操作，但要告知调用方
