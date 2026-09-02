# -*- coding: utf-8 -*-
"""每种控件的**可写形状**：写进去要长什么样。

这是本技能的核心资产。简道云的坑在于「显示值 ≠ 可写值」——
读回来是一个对象，写进去却要另一种形状，写错了接口**照样返回成功**、
把字段静默存成 null。官方文档没有一张这样的对照表，
做集成的人只能一个个试出来。这里把实测结论固化下来。

纯数据 + 纯函数，不碰网络，所以能被完整测试。


不可写清单**不在这里定义**，从内核派生——这里原来自己列了一份，
多出一个 autonum，而内核不认识它，于是预检/同步/清洗三条链路都会放它过去：
同一个概念两份清单，迟早分叉。
"""
from jdy_client import (COMPLEX_WRITE, NOT_WRITABLE_TYPES, READ_ONLY_TYPES,
                        UNVERIFIED_WRITE, UNWRITABLE_REASON, writable_back)

# (示例值, 说明)。示例值是**真的能写进去**的形状，可以直接抄。
#
# ⚠️ 这张表里的每一条都必须与内核 encode_value() 的实测结论一致。
# 二期复查抓到过反例：`dept` 这里写着"部门名"，而实测只认**裸 dept_no 整数**，
# 写名字接口照样回成功、字段存成 null；附件这里写着读回来的 {name,url} 形状，
# 而实测要写**上传后的 key 字符串列表**。生成出来的集成代码一跑就是错的，
# 而 devkit 的整个价值就是"照着抄能跑"。
# 下面按类型分三块：能直接给形状的、要走一个流程的、还没实测过的。
WRITE_SHAPE = {
    "text":          ('"张三"', "普通字符串"),
    "textarea":      ('"多行\\n文本"', "普通字符串"),
    "number":        ("123.45", "数字；字符串会被转换，转不了就报错"),
    "datetime":      ('"2026-08-29T10:00:00.000Z"',
                      "ISO8601。**必须带时区**——`2026/08/29` 这类会被静默存成 null"),
    "radiogroup":    ('"选项A"', "单选：选项的**显示文本**，不是序号"),
    "combo":         ('"选项A"', "下拉单选：显示文本"),
    "checkboxgroup": ('["选项A", "选项B"]',
                      "多选：显示文本组成的**数组**。"
                      "裸字符串或用顿号拼起来的一串会被静默丢弃"),
    "combocheck":    ('["选项A", "选项B"]', "下拉多选：同上，必须是数组"),
    "user":          ('"zhangsan"', "成员：**username**，不是姓名，也不是读回来的那个对象"),
    "usergroup":     ('["zhangsan", "lisi"]', "多成员：username 数组"),
    "dept":          ("5",
                      "部门：**裸的 dept_no 整数**。"
                      "写部门名、写 {\"dept_no\": 5}、写读回来的完整对象——"
                      "三种都被静默丢弃（接口回成功、字段是空的）。"
                      "编号可以从这张表已有数据的该列里读出来，或用 jdy-org 导出通讯录"),
    "company":       ('"某某公司"', "企业：纯字符串"),
    "phone": ('{"phone": "13800000000"}',
              "**要包一层**。读回来是 {verified, phone}，"
              "写进去只给 {\"phone\": ...}；直接写裸字符串会静默丢弃"),
    "address": ('{"province": "广东省", "city": "深圳市",\n'
                '     "district": "南山区", "detail": "科技园1号"}',
                "**必须给对象**。写拼接好的字符串会静默丢弃"),
    "lookup": ('"deadbeefdeadbeefdead0006"',
               "关联数据：目标记录的 **data_id 裸串**。"
               "⚠️ 接口**不校验引用是否存在**——写个不存在的 ID 照样写得进去，"
               "回读也发现不了，因为读回来就是你写进去的那个 ID"),
    "linkobject": ('{"link_id": "deadbeefdeadbeefdead0006"}',
                   "关联表单：**要包一层** link_id。裸的 data_id 字符串报 3005；"
                   "写进去的 name 会被接口用目标记录的真实名字覆盖"),
    "image":     ('["9e0f1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b"]',
                  "附件类：写的是**上传后拿到的 key 组成的字符串列表**。"
                  "三步：取上传凭证 → 上传文件 → 把 key 写进来，"
                  "且写入请求必须带上取凭证时的**同一个 transaction_id**（1 小时有效）。"
                  "`[{\"key\": k}]` 和读回来的 `[{\"name\", \"url\"}]` 都会被静默丢弃"),
    "subform":   ('[{"_widget_子字段": {"value": "..."}}]',
                  "子表单：**双层包裹**（外层一行一个对象，内层每个字段再包 value）。"
                  "读回来是扁的（内层不包 value），**原样写回去是静默丢弃的**。"
                  "而且 update 是**整表替换**：写 1 行会把原来的 2 行冲掉"),
}
WRITE_SHAPE["upload"] = (WRITE_SHAPE["image"][0], WRITE_SHAPE["image"][1])

# 压根写不进去的（内核说了算）
NOT_WRITABLE = {t: UNWRITABLE_REASON.get(t, "系统生成或接口不支持写入")
                for t in sorted(NOT_WRITABLE_TYPES | READ_ONLY_TYPES)}


def shape_of(wtype):
    """返回 (可写?, 示例, 说明)。

    "没实测过"和"实测写不进去"是两件事，报出来的理由必须分得清——
    所以这两类都从内核的判断里取，不在本文件里另列一份名单。
    """
    if wtype in NOT_WRITABLE:
        return False, None, NOT_WRITABLE[wtype]
    if wtype in UNVERIFIED_WRITE:
        # 账号里一个样本都没有，形状不知道。**不要瞎给一个示例**——
        # devkit 的示例是给人直接抄进生产代码的，猜错了比不给更糟。
        return False, None, ("该类型（%s）的写入格式**尚未实测**，"
                             "本工具不猜形状。先在界面上填一条，"
                             "用 jdy-doc 读回来看看它长什么样，再动手写" % wtype)
    if wtype in WRITE_SHAPE:
        example, note = WRITE_SHAPE[wtype]
        if wtype in COMPLEX_WRITE:
            note += "　※ 这个类型不是填个值就行，照上面的流程走"
        return True, example, note
    return True, '"值"', "未收录的类型，写之前先用一条数据试写并回读核对"


def field_payload(widget):
    """单个字段在 data 里的完整写法：`{"_widget_x": {"value": ...}}`。"""
    writable, example, note = shape_of(widget["type"])
    if not writable:
        return None, note
    return '"%s": {"value": %s}' % (widget["name"], example), note
