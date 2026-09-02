# -*- coding: utf-8 -*-
"""通讯录的读与写。纯逻辑 + 薄接口层，写入的闸门在 apply.py。

**这个技能和别的不一样，动的不是业务数据，是组织架构。** 三条后果说在前面：

1. **加一个成员会占用一个用户数**（官方明示：新建成员自动激活）。
   那是**计费后果**，不是数据后果——改错一行数据能改回来，多买一个坐席不能。
2. `JDY_WRITE_ALLOWLIST` **管不住这里**。那个白名单按 app_id/entry_id 限定，
   而通讯录接口的 body 里根本没有这两样——和流程写接口同一个结构性缺口，
   但这里的影响面是整个企业。所以另设一道显式开关 `JDY_ORG_WRITE`。
3. **本模块不接任何删除接口。** 官方有删除成员/批量删除/删除部门，这里一个都不连。
   删错一个部门，它下面的人和权限一起没了，而本项目的规矩是从不删数据。
   要删请到界面上删。

读接口（都只读）：
    /v6/corp/department/list        {dept_no, has_child}   10/s   根部门 dept_no=1
    /v5/corp/department/user/list   {dept_no, has_child}   10/s
    /v5/corp/user/get               {username}             30/s
写接口：
    /v6/corp/department/create      {name, parent_no, dept_no?}   20/s
    /v6/corp/department/update      {dept_no, name?, parent_no?, seq?}
    /v5/corp/user/create            {name, username?, departments?}  ← 占用用户数
    /v5/corp/user/update            {username, name?, departments?}
"""
import json

ROOT_DEPT = 1

# 写入闸门**在内核里**（jdy_client.post 上），这里只是转出，让本技能的调用方
# 有个就近的名字可用。原来这道闸是本文件自己实现、且只在 apply.py 里调用一次——
# 谁绕开 apply.py 直接 client.post("/v5/corp/user/create") 就完全不设防。
# 现在它安在 post() 这个唯一出口上，绕不过去。
from jdy_client import ORG_WRITE_ENV, OrgWriteRefused, check_org_write  # noqa: F401


class OrgError(ValueError):
    pass


def existing_dept_nos(current):
    """哪些部门编号算「已经存在」。**根部门 1 永远存在。**

    这个判断有两处用得着：算计划时分「新建/修改」，执行时选调哪个接口。
    原来两边各写各的，执行那半漏了根部门——于是"改根部门名"被 classify 归进
    「修改」、执行时却按「新建」发了出去。同一个概念只留一个出处。
    """
    return {d.get("dept_no") for d in current["departments"]} | {ROOT_DEPT}


def existing_usernames(current):
    return {m.get("username") for m in current["members"]}


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------

def fetch_departments(client, dept_no=ROOT_DEPT):
    """递归取部门。根部门自己不在返回里（它就是 dept_no=1）。"""
    resp = client.post("/v6/corp/department/list",
                       {"dept_no": dept_no, "has_child": True})
    return resp.get("departments") or resp.get("depts") or []


def fetch_members(client, dept_no=ROOT_DEPT):
    resp = client.post("/v5/corp/department/user/list",
                       {"dept_no": dept_no, "has_child": True})
    return resp.get("users") or []


def fetch_managers(client, dept_no):
    """部门主管。**接口可能不可用**（本账号实测 403），那就如实返回 None，
    而不是当成"没有主管"——那两件事不一样。"""
    from jdy_client import JdyError
    try:
        resp = client.post("/v6/corp/department/manager/list", {"dept_no": dept_no})
    except JdyError:
        return None
    return resp.get("managers") or []


def snapshot(client):
    """整棵通讯录。写之前拿它做备份，也用来做前后比对。"""
    return {"departments": fetch_departments(client),
            "members": fetch_members(client)}


def tree_lines(departments, members, root_name="（根部门）"):
    """把部门树画成缩进的几行，人能一眼看出层级。"""
    by_parent = {}
    for d in departments:
        by_parent.setdefault(d.get("parent_no"), []).append(d)
    heads = {}
    for m in members:
        for no in (m.get("departments") or []):
            heads.setdefault(no, []).append(m)

    out = []

    def walk(no, name, depth):
        who = heads.get(no) or []
        out.append("%s%s（编号 %s）　%d 人%s"
                   % ("  " * depth, name, no, len(who),
                      "：" + "、".join(x.get("name", "?") for x in who[:6]) if who else ""))
        for child in sorted(by_parent.get(no) or [], key=lambda d: d.get("seq") or 0):
            walk(child.get("dept_no"), child.get("name", "?"), depth + 1)

    walk(ROOT_DEPT, root_name, 0)
    # 挂在树外的部门（父部门不在返回里）也要露出来，不能悄悄漏掉
    known = {ROOT_DEPT} | {d.get("dept_no") for d in departments}
    for d in departments:
        if d.get("parent_no") not in known:
            out.append("（父部门 %s 不在本次返回里）%s（编号 %s）"
                       % (d.get("parent_no"), d.get("name", "?"), d.get("dept_no")))
    return out


# --------------------------------------------------------------------------
# 写：先算计划，再执行
# --------------------------------------------------------------------------

def load_plan(path, parse_yaml):
    """读改动计划并校验。看不懂的一律报错。"""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        plan = json.loads(text) if path.endswith(".json") else parse_yaml(text)
    except Exception as exc:
        raise OrgError("计划解析失败：%s" % exc)
    if not isinstance(plan, dict):
        raise OrgError("计划顶层必须是映射")
    depts = plan.get("departments") or []
    members = plan.get("members") or []
    if not depts and not members:
        raise OrgError("计划里既没有 departments 也没有 members，没有要做的事")
    for d in depts:
        if not isinstance(d, dict) or not (d.get("name") or d.get("dept_no")):
            raise OrgError("部门条目至少要有 name（新建）或 dept_no（修改）：%r" % d)
    for m in members:
        if not isinstance(m, dict):
            raise OrgError("成员条目不是映射：%r" % m)
        if not m.get("username") and not m.get("name"):
            raise OrgError("成员条目要有 username（修改）或 name（新建）：%r" % m)
        if "departments" in m and not isinstance(m["departments"], list):
            raise OrgError("成员「%s」的 departments 要是编号列表"
                           % (m.get("username") or m.get("name")))
    return plan


def classify(plan, current):
    """把计划分成「新建部门/改部门/新建成员/改成员」四堆，并标出**要花钱的那一堆**。

    新建成员会占用用户数，所以它必须被单独数出来、单独说——
    混在"共 12 项改动"里报，人是看不见那笔账的。
    """
    have_depts = existing_dept_nos(current)
    have_users = existing_usernames(current)
    out = {"dept_create": [], "dept_update": [], "member_create": [], "member_update": []}
    for d in plan.get("departments") or []:
        if d.get("dept_no") and d["dept_no"] in have_depts:
            out["dept_update"].append(d)
        else:
            out["dept_create"].append(d)
    for m in plan.get("members") or []:
        if m.get("username") and m["username"] in have_users:
            out["member_update"].append(m)
        else:
            out["member_create"].append(m)
    return out


def describe(buckets):
    lines = []
    for key, label in (("dept_create", "新建部门"), ("dept_update", "修改部门"),
                       ("member_update", "修改成员"), ("member_create", "新增成员")):
        items = buckets[key]
        if not items:
            continue
        lines.append("%s %d 项" % (label, len(items)))
        for it in items[:5]:
            lines.append("    %s" % json.dumps(it, ensure_ascii=False)[:70])
        if len(items) > 5:
            lines.append("    … 另有 %d 项" % (len(items) - 5))
    return lines
