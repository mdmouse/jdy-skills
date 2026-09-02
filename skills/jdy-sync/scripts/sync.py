# -*- coding: utf-8 -*-
"""同步引擎的共用层：配置、ID 映射、拓扑排序、差异计划。

三个必须做对的地方：

1. **引用要按拓扑序同步**——被引用的表先同步，否则翻译引用时目标端还没有对应记录。
2. **ID 映射要持久化**——源端 data_id 与目标端 data_id 是两套编号，靠映射表打通；
   它同时也是断点续跑与增量同步的基础。
3. **引用要自校验**——关联数据（lookup）写入时接口**不校验引用是否存在**，
   写个不存在的 ID 照样入库、回读还"一致"，连回读比对都兜不住。
"""
import json
import os

import sources

from jdy_client import (ATTACHMENT_TYPES, COMPLEX_WRITE, LOOKUP_TYPES,   # noqa: F401
                        NOT_WRITABLE_TYPES, READ_ONLY_TYPES, UNVERIFIED_WRITE,
                        display_value, plan_code, writable_back)
#                       ^ UNVERIFIED_WRITE 在此转出：init_config 一直从这里 import


class SyncError(ValueError):
    pass


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config(path, parse_yaml):
    """读配置。相对路径一律按**配置文件所在目录**解析，不按当前工作目录。

    否则同一份配置换个目录跑，`id_map: ./idmap.json` 会在新目录另建一个空映射表，
    两份映射悄悄分裂。备份文件本来就是按配置目录落的，两套规则并存更容易出错。
    """
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        cfg = json.loads(text) if path.endswith(".json") else parse_yaml(text)
    except Exception as exc:
        raise SyncError("配置解析失败：%s" % exc)
    if not isinstance(cfg, dict):
        raise SyncError("配置顶层必须是映射")
    for key in ("source", "target", "tables"):
        if not cfg.get(key):
            raise SyncError("配置缺少必填项：%s" % key)
    src = cfg["source"]
    if not (src.get("app") or src.get("file")):
        raise SyncError("source 要么给 app（简道云应用），要么给 file"
                        "（CSV / JSONL / SQLite）")
    if src.get("app") and src.get("file"):
        raise SyncError("source 的 app 和 file 只能给一个")
    if not cfg["target"].get("app"):
        raise SyncError("target 必须是简道云应用（本工具不往外部文件写——"
                        "那不需要同步的这套保证，导出用 jdy-excel-bridge）")
    if not isinstance(cfg["tables"], list) or not cfg["tables"]:
        raise SyncError("tables 必须是非空列表")
    seen = set()
    for t in cfg["tables"]:
        for key in ("alias", "source_entry", "target_entry", "key"):
            if not t.get(key):
                raise SyncError("表配置缺少 %s：%s" % (key, json.dumps(t, ensure_ascii=False)))
        if t["alias"] in seen:
            raise SyncError("alias 重复：%s" % t["alias"])
        seen.add(t["alias"])
    if src.get("file"):
        # 平面文件只有一张表。两张表配同一个文件，原来是**各读一遍全部内容**、
        # 一声不吭——配置里认真写的 source_entry 被整个忽略了。
        # 要多表，就把 file 指向一个目录，让 source_entry 去点名文件。
        base = resolve_path(src["file"], os.path.dirname(os.path.abspath(path)))
        flat = (not os.path.isdir(base)
                and sources.SUFFIXES.get(os.path.splitext(base)[1].lower()) != "sqlite")
        if flat and len(cfg["tables"]) > 1:
            raise SyncError(
                "source.file 指向的是**一个平面文件**（%s），却配了 %d 张表——"
                "一个 CSV/JSONL 只有一张表，这样每张表读到的都是同一份数据。\n"
                "要同步多张表：把 file 改成一个**目录**，再让每张表的 source_entry "
                "点名目录里的文件名。（SQLite 不受此限，它本来就一个文件多张表。）"
                % (os.path.basename(base), len(cfg["tables"])))

    for t in cfg["tables"]:
        for field, alias in (t.get("refs") or {}).items():
            if alias not in seen:
                raise SyncError("表「%s」的引用字段「%s」指向未定义的 alias：%s"
                                % (t["alias"], field, alias))
            if src.get("file"):
                # 引用翻译要拿源端的 data_id 去查 ID 映射，而外部文件里没有 data_id
                raise SyncError(
                    "表「%s」声明了 refs，但源端是外部文件——外部文件里没有简道云的"
                    "记录 ID，引用翻译无从谈起。先把被引用的表同步进简道云，"
                    "再做应用到应用的同步。" % t["alias"])
    cfg["_base_dir"] = os.path.dirname(os.path.abspath(path))
    if src.get("file"):
        # 相对路径按**配置文件所在目录**解析，和 id_map 同一套规则——
        # 否则同一份配置换个目录跑就找不到源文件
        cfg["source"]["file"] = resolve_path(src["file"], cfg["_base_dir"])
    cfg["_id_map_path"] = resolve_path(cfg.get("id_map") or "idmap.json",
                                       cfg["_base_dir"])
    return cfg


def resolve_path(path, base_dir):
    """相对路径按配置文件所在目录解析；`~` 与绝对路径原样。"""
    expanded = os.path.expanduser(str(path))
    if os.path.isabs(expanded):
        return expanded
    return os.path.normpath(os.path.join(base_dir, expanded))


def topo_sort(tables):
    """被引用的表排前面。存在环则报错——环意味着无法确定先同步谁。"""
    by_alias = {t["alias"]: t for t in tables}
    ordered, state = [], {}

    def visit(alias, trail):
        if state.get(alias) == "done":
            return
        if state.get(alias) == "visiting":
            raise SyncError("引用成环：%s。请打断环，或把其中一张表的引用字段留空后补"
                            % " → ".join(trail + [alias]))
        state[alias] = "visiting"
        for dep in set((by_alias[alias].get("refs") or {}).values()):
            visit(dep, trail + [alias])
        state[alias] = "done"
        ordered.append(by_alias[alias])

    for t in tables:
        visit(t["alias"], [])
    return ordered


# --------------------------------------------------------------------------
# ID 映射
# --------------------------------------------------------------------------

class IdMap(object):
    """源端 data_id → 目标端 data_id。断点续跑与增量同步都靠它。"""

    def __init__(self, path):
        self.path = os.path.expanduser(path)
        self.data = {}
        self.readonly = False
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.data = json.load(fh).get("map", {})
            except (OSError, ValueError):
                self.data = {}

    def get(self, alias, source_id):
        return self.data.get(alias, {}).get(source_id)

    def put(self, alias, source_id, target_id):
        self.data.setdefault(alias, {})[source_id] = target_id

    def count(self, alias):
        return len(self.data.get(alias, {}))

    def save(self):
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"map": self.data}, fh, ensure_ascii=False, indent=2)
            return True
        except OSError:
            self.readonly = True
            return False


# --------------------------------------------------------------------------
# 字段映射与取值
# --------------------------------------------------------------------------

# 同步场景下对"搬不动"的措辞。**只管怎么说，不管判不判得出来**——
# 判定一律走内核的 writable_back()，这张表查不到就用内核给的理由。
# 这样内核哪天多认一种类型，同步这边至少会拦住它、只是话说得笼统些；
# 而不是像原来那样各写各的判断，新类型直接漏过去。
SYNC_EXCLUSION = {
    "linkdata": "目标字段是「选择数据」，接口不支持写入——改用「关联数据」字段承载关系",
    "sn": "目标字段由系统生成，不可写入",
    "autonum": "目标字段由系统生成，不可写入",
}
# subform / image / upload 曾经也在这张表里，措辞是「同步暂不支持」。
# 现在支持了（见 sync_shape），措辞留着就成了假话，所以删掉——
# 它们改由 sync_shape 逐对判定，说不搬时给的是**这一对**的具体理由。


def _inner_map(src_w, dst_w):
    """子表单内层：按**显示名**把源内层字段对到目标内层字段（D2）。

    返回 (inner, excluded)：

      inner     [(源内层 name, 目标内层 label, 源内层 type)]，按源端字段顺序
      excluded  [("子表单名.内层名", 理由)]——**一条都不静默**

    v1 边界（D3）：内层的关联数据、附件、嵌套子表单一律不搬，各自写清理由。
    引用翻译与附件搬运**不下沉到内层**：内层引用要另一套 ID 映射、内层附件要另一轮
    上传凭证，一下沉范围就爆炸。宁可整列报出来让人知道，也不要搬一半。

    判定口径和外层保持一致：读用源端类型（成员读 username 那套不对称照样处理），
    写由目标端控件说了算（`writable_back`）。内外两套规矩会让人以为内层更严，
    实际只是没对齐。
    """
    src_items = [i for i in (src_w.get("items") or [])]
    dst_items = {i["label"]: i for i in (dst_w.get("items") or [])}
    inner, excluded = [], []
    for si in src_items:
        where = "%s.%s" % (src_w.get("label"), si.get("label"))
        di = dst_items.get(si.get("label"))
        if di is None:
            excluded.append((where, "目标子表单里没有同名的内层字段——"
                                    "内层是按显示名对应的，改名或补一个同名字段才能搬"))
            continue
        dtype = di.get("type")
        if dtype == "subform":
            excluded.append((where, "嵌套子表单：v1 不搬"))
            continue
        if dtype in ATTACHMENT_TYPES:
            excluded.append((where, "子表单内层的附件：v1 不搬——"
                                    "附件搬运要单独一轮上传凭证，不下沉到内层"))
            continue
        if dtype in LOOKUP_TYPES:
            excluded.append((where, "子表单内层的「关联数据」：v1 不搬——"
                                    "引用翻译要走 ID 映射，不下沉到内层。"
                                    "直接搬会把源端的 data_id 写成一个指向虚无的引用"))
            continue
        ok, why = writable_back(di)
        if not ok:
            excluded.append((where, SYNC_EXCLUSION.get(dtype, why)))
            continue
        inner.append((si["name"], si["label"], si.get("type")))
    return inner, excluded


def sync_shape(src_w, dst_w):
    """这一对字段能不能搬、要怎么搬。返回 (能不能, 说不能的理由, shape)。

    **只此一处判定**。草稿（init_config.blocked_fields）与计划（resolve_fields）
    都问它——否则又会变成"生成配置的人说能搬、执行的人说不能搬"，
    那正是 COMPLEX_WRITE 上一次分叉的原因。

    shape 只有子表单用得上：`{"inner": [...], "inner_excluded": [...]}`。
    """
    stype, dtype = src_w.get("type"), dst_w.get("type")

    # D9：只在同类之间搬。异类转换（把子表单摊平成文本、把附件写成文件名）
    # 是另一个功能，不该在同步里悄悄发生。
    if "subform" in (stype, dtype):
        if stype != dtype:
            return False, ("只在子表单之间搬（源 %s / 目标 %s）——"
                           "跨类转换是另一个功能" % (stype, dtype)), None
        inner, inner_excluded = _inner_map(src_w, dst_w)
        shape = {"inner": inner, "inner_excluded": inner_excluded}
        if not inner:
            return False, ("子表单的内层字段一个都对不上（按显示名对应）——"
                           "整列搬不过去"), shape
        return True, None, shape
    if stype in ATTACHMENT_TYPES or dtype in ATTACHMENT_TYPES:
        if not (stype in ATTACHMENT_TYPES and dtype in ATTACHMENT_TYPES):
            return False, ("只在附件字段之间搬（源 %s / 目标 %s）" % (stype, dtype)), None
        return True, None, None

    # 其余类型：能不能原样写回去，**由内核那一个判断说了算**。
    ok, why = writable_back(dst_w)
    if not ok:
        return False, SYNC_EXCLUSION.get(dtype, why), None
    return True, None, None


def target_shape(dst_w, shape):
    """目标端拿哪些内层字段来比——**和源端搬过去的那些一一对应**。

    不能拿目标子表单的全部内层字段：目标端多出来的内层列（公式算出来的小计、
    只在目标端维护的备注）每次回读都在、源端永远没有，于是每次都判"有变化"、
    每次整表重写——幂等就没了。只比我们自己搬过去的那些列。
    """
    if not shape or not shape.get("inner"):
        return None
    dst_items = {i["label"]: i for i in (dst_w.get("items") or [])}
    return {"inner": [(dst_items[label]["name"], label, dst_items[label].get("type"))
                      for _src_name, label, _t in shape["inner"] if label in dst_items]}


def resolve_fields(src_labels, dst_labels, explicit, ref_fields=()):
    """决定哪些字段要搬。返回 (mapping, excluded)。

    `fields` 是**白名单**：写了它就只搬列出的字段。这本身没问题，
    问题是"两边都有、却没被列出"的字段会**凭空消失**——既不搬也不报，
    计划里看不见，同步完才发现整列是空的。所以这里把它们显式记进 excluded。

    excluded 的成因：目标端不可写（选择数据/流水号）、写入格式未实测、
    目标端没有对应字段、**两边同名但没列进 fields**、
    **是关联数据但没在 refs 里声明**、子表单内层一个都对不上、异类字段（D9）。

    `src_labels` 要给 {显示名: widget}——判定要看**两边**的类型（D9），
    只有目标端那一半是不够的。
    """
    mapping, excluded = {}, []
    pairs = explicit.items() if explicit else [(l, l) for l in src_labels]
    if explicit:
        for label in src_labels:
            if label in explicit or label not in dst_labels:
                continue
            excluded.append((label, "两边同名但没写进 fields —— fields 是白名单，"
                                    "列了就只搬列出的；想搬它就补进去"))
    for src, dst in pairs:
        if src not in src_labels:
            excluded.append((src, "源表没有这个字段"))
            continue
        widget = dst_labels.get(dst)
        if widget is None:
            excluded.append((src, "目标表没有「%s」" % dst))
            continue
        # **能不能搬只有一处判定**（sync_shape，它内部再问内核的 writable_back）。
        # 这里原来把四组类型各自 if 了一遍——那是第二套实现：内核哪天多认一种
        # "不能原样写回去"的类型，同步这边照样会把它搬过去，然后静默丢。
        src_widget = src_labels[src] if isinstance(src_labels, dict) else widget
        ok, why, shape = sync_shape(src_widget, widget)
        if shape and shape.get("inner_excluded"):
            # 内层搬不动的那几个字段**照样要报出来**，哪怕整列是搬得动的：
            # 只报"这列搬了"会让人以为整块都搬了。
            excluded.extend(shape["inner_excluded"])
        if not ok:
            excluded.append((src, why))
            continue
        if widget["type"] in LOOKUP_TYPES and src not in ref_fields:
            # 源端的 data_id 拿到目标应用里毫无意义——它要么指向另一条记录，
            # 要么什么都不指。接口不校验引用，写进去照样"成功"，
            # 回读比对也发现不了（存的确实就是那个字符串）。
            # 唯一正确的搬法是在 refs 里声明它指向哪张表，走 ID 映射翻译。
            excluded.append((src, "目标字段是「关联数据」但没在 refs 里声明——"
                                  "直接搬会把源端的 data_id 原样写进目标应用，"
                                  "变成一个指向虚无的引用。要搬它就在 refs 里声明指向哪个 alias"))
            continue
        mapping[src] = dst
    return mapping, excluded


def table_shapes(src_by_label, dst_by_label, mapping):
    """映射定下来之后，把每对字段的 shape 算出来：源端一套、目标端一套。

    走的是 resolve_fields 用过的**同一个** sync_shape——两处各算各的迟早分叉。

    源端 shape 用来把源行翻成"目标端内层显示名"的形状；
    目标端 shape 用来把目标现值翻成同一种形状，好逐格比对（D1 的 canonical 化）。
    """
    src_shapes, dst_shapes = {}, {}
    for src, dst in mapping.items():
        _ok, _why, shape = sync_shape(src_by_label[src], dst_by_label[dst])
        if shape:
            src_shapes[src] = shape
            dst_shapes[dst] = target_shape(dst_by_label[dst], shape)
    return src_shapes, dst_shapes


def sync_value(raw, wtype, shape=None):
    """取**可写回**的值，而不是给人看的值。

    这两者对成员字段是不同的：读出来是 `{"name": "张三", "username": "sys_x"}`，
    `display_value` 给的是"张三"，但写入只认 `username`——直接搬显示名会被静默丢弃，
    而且"张三"能通过格式校验，连报错都没有。

    shape 是子表单专用的内层映射（见 sync_shape）；不给就搬不动子表单。
    """
    if raw in (None, "", [], {}):
        return None
    if wtype == "subform":
        return _subform_value(raw, shape)
    if wtype in ATTACHMENT_TYPES:
        # 读回来是 [{"name","size","mime","url"}]。**url 带过期戳、不能回灌**，
        # 但搬运要靠它去下载，所以原样留着；比对时只看 (name, size)（见 canonical）。
        # 真正写进目标表的是重新上传拿到的 key，那一步在 apply 里做。
        return [{"name": v.get("name"), "size": v.get("size"), "url": v.get("url")}
                for v in raw
                if isinstance(v, dict) and v.get("url")] or None
    if wtype == "user":
        return raw.get("username") if isinstance(raw, dict) else raw
    if wtype == "usergroup":
        return [u.get("username") for u in raw
                if isinstance(u, dict) and u.get("username")] or None
    if wtype in LOOKUP_TYPES:
        return raw.get("id") if isinstance(raw, dict) else raw
    if wtype in ("address", "phone"):
        # 这两个和成员字段同理：写入要对象，display_value 给的拼接串会被静默丢弃
        return raw if isinstance(raw, dict) else None
    if wtype in ("checkboxgroup", "combocheck"):
        # 多选读出来就是列表，写入端也收列表——中间不要经过 display_value。
        # 它会拼成「线上、线下」，而实测裸字符串与顿号串都会被**静默丢弃**。
        return list(raw) if isinstance(raw, (list, tuple)) else [raw]
    if wtype == "dept":
        # 实测只认裸 dept_no 整数，读回来却是展开的对象——和 user 同一种不对称
        return raw.get("dept_no") if isinstance(raw, dict) else raw
    if wtype == "linkobject":
        return {"link_id": raw.get("link_id")} if isinstance(raw, dict) else raw
    return display_value(raw, wtype)


def _subform_value(raw, shape):
    """子表单：读回来的扁平行 → `encode_row` 认的 `[{目标内层显示名: 值}]`。

    三件事必须做对：

    1. 内层值**递归**过 sync_value——内层的成员/部门同样是"读展开对象、写 ID"，
       不递归就会把"张三"搬过去再被静默丢。
    2. 每行的 `_id` 是**源端**子行的编号，不能带过去，也不能进比对
       （它每次重写都变，带上就永远判"有变化"）。
    3. **可搬内层全为空的子行直接丢掉**：实测提交 3 行（中间一行全空）回读只有 2 行，
       简道云自己会把全空子行吞掉。留着它，回读永远比提交少一行、永远判"没搬干净"，
       重跑一次就重写一次——幂等就没了。丢了不是不管：plan_table 会数出来报给用户，
       多半是那几行的数据都在被排除的内层字段里。
    """
    if not shape or not shape.get("inner"):
        return None
    rows = []
    for sub in raw:
        if not isinstance(sub, dict):
            continue
        out = {}
        for src_name, dst_label, inner_type in shape["inner"]:
            v = sync_value(sub.get(src_name), inner_type)
            if v not in (None, "", [], {}):
                out[dst_label] = v
        if out:
            rows.append(out)
    return rows or None


def row_values(row, by_label, labels, shapes=None):
    """按字段取可写回的值。shapes 给子表单用（label → shape）。"""
    out = {}
    for label in labels:
        w = by_label.get(label)
        if w is None:
            continue
        v = sync_value(row.get(w["name"]), w["type"], (shapes or {}).get(label))
        if v not in (None, "", [], {}):
            out[label] = v
    return out


def _num(value):
    """比对前把数值归一：接口时而给 7、时而给 7.0，字符串化之后就成了"有变化"。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical(value, wtype=None):
    """把值压成**能拿来比对**的形式。

    ⚠️ 子表单**绝不能**拿 display_value 比：它对子表单只给"N 行子表单"，
    于是"行数相同、内容全变了"会被判成无变化，整列悄悄不同步，
    而计划、执行、回读三处都显示一切正常。这是本期最大的陷阱。

    附件只比 (name, size)：url 每次读回来都带新的过期戳，比它等于每次都判有变化。
    代价是"内容变了但文件名和大小都没变"检测不到——写进 v1 边界，不假装能测。
    """
    if value in (None, "", [], {}):
        return ""
    if wtype in ATTACHMENT_TYPES:
        return json.dumps([[v.get("name"), _num(v.get("size"))]
                           for v in value if isinstance(v, dict)], ensure_ascii=False)
    if wtype == "subform":
        return json.dumps([{k: _num(v) for k, v in row.items()}
                           for row in value if isinstance(row, dict)],
                          ensure_ascii=False, sort_keys=True)
    return str(value)


def verify_complex(client, app_id, entry_id, checks, dst_by_label):
    """写完之后**按新类型的口径**再核对一遍。返回不一致清单。

    为什么内核那一关不够：`verify_written` / `update` 的回读只问一句
    "提交了值，写进去是不是空的"。子表单内层映射错了、附件搬串了，
    字段都**不是空的**——那一关照样过，然后报"逐字段回读核对通过"。

    checks: [(data_id, 业务键, {目标显示名: 期望值})]

    比什么由**期望值自己**说了算：期望里出现过的内层显示名就是我们写过的那些列，
    目标端多出来的内层列（公式算的小计之类）不参与比对，否则永远判不一致。
    """
    mism = []
    checks = [c for c in checks if c[0] and c[2]]
    if not checks:
        return mism
    rows = client.fetch_rows_by_id(app_id, entry_id, [c[0] for c in checks])
    for data_id, key, expected in checks:
        row = rows.get(data_id)
        if row is None:
            mism.append({"key": key, "field": "（整行）", "expected": "写入的记录",
                         "actual": "回读不到 %s" % data_id})
            continue
        shapes = {}
        for label, want in expected.items():
            w = dst_by_label.get(label) or {}
            if w.get("type") != "subform":
                continue
            items = {i["label"]: i for i in (w.get("items") or [])}
            used = [l for l in {k for r in (want or []) if isinstance(r, dict) for k in r}
                    if l in items]
            shapes[label] = {"inner": [(items[l]["name"], l, items[l].get("type"))
                                       for l in sorted(used)]}
        actual = row_values(row, dst_by_label, list(expected), shapes)
        for label, want in expected.items():
            wtype = (dst_by_label.get(label) or {}).get("type")
            got, exp = canonical(actual.get(label), wtype), canonical(want, wtype)
            if got != exp:
                mism.append({"key": key, "field": label,
                             "expected": exp[:120], "actual": got[:120]})
    return mism


def business_key(row, by_label, key_label):
    w = by_label.get(key_label)
    if w is None:
        raise SyncError("业务键字段「%s」在表里不存在" % key_label)
    v = display_value(row.get(w["name"]), w["type"])
    return None if v in (None, "", [], {}) else str(v)


def plan_fingerprint(cfg, plans):
    """把一次计划压成一个短指纹。

    大批量写入要求先拿到这个码再执行。**它拦不住 Agent**——Agent 读得到输出，
    自然也能把码传回来。它保证的是另一件事：**被确认的计划和被执行的计划是同一个**。
    源数据在两次调用之间变了（多了几百条、业务键对不上了），指纹就变，
    旧码失效，强制重新规划再确认。

    防的是"确认的和执行的不是一回事"，不是防作弊。

    算法走内核 plan_code：原来这里自己 sha256 一遍再 .upper()，
    于是同一个项目里出现了两种大小写的确认码，调用方抄错一种就白跑一次。
    """
    return plan_code({
        "target": cfg["target"]["app"],
        "tables": [{"alias": p["alias"],
                    "creates": len(p["creates"]), "updates": len(p["updates"]),
                    "keys": sorted(c["key"] for c in p["creates"])[:200]}
                   for p in sorted(plans, key=lambda x: x["alias"])],
    })


# --------------------------------------------------------------------------
# 差异计划
# --------------------------------------------------------------------------

def translate_refs(row, src_by_label, table, id_map):
    """把源端的引用值翻译成目标端的 data_id。

    返回 (translated, unresolved)。翻译不出来的**不写**——
    宁可留空让人看见，也不要写一个指向虚无的引用：那种脏数据回读比对都发现不了。
    """
    translated, unresolved = {}, []
    for field, alias in (table.get("refs") or {}).items():
        w = src_by_label.get(field)
        if w is None:
            unresolved.append((field, "源表没有这个字段"))
            continue
        raw = row.get(w["name"])
        if isinstance(raw, dict):
            raw = raw.get("id")
        if not raw:
            continue
        target_id = id_map.get(alias, str(raw))
        if target_id:
            translated[field] = target_id
        else:
            unresolved.append((field, "被引用记录（%s / %s）尚未同步到目标端"
                               % (alias, raw)))
    return translated, unresolved


def source_label(cfg):
    """源端在报告里怎么称呼。外部源要**明说是哪个文件**——
    同一份配置里换了源文件，计划看起来会一模一样。"""
    if cfg["source"].get("file"):
        return "文件 %s" % os.path.basename(cfg["source"]["file"])
    return "应用 %s" % cfg["source"]["app"]


def read_source(client, cfg, table):
    """取源端的 (by_label, rows)。源可以是简道云应用，也可以是外部文件。

    两边返回同一种形状，所以下游（业务键、取值、差异比对）一行都不用改。
    """
    if cfg["source"].get("file"):
        by_label, rows = sources.read(cfg["source"]["file"], table["source_entry"])
        rows = sources.stamp_ids(rows, by_label, table["key"])
        limit = table.get("limit")
        return by_label, (rows[:limit] if limit else rows)
    src_app = cfg["source"]["app"]
    by_label, _ = client.field_map(src_app, table["source_entry"])
    return by_label, client.fetch_all(src_app, table["source_entry"],
                                      limit=table.get("limit"))


def plan_table(client, cfg, table, id_map):
    """算出这张表要做什么。只读，不写任何数据。

    **副作用（有意为之）**：按业务键匹配上的记录会立刻写进内存里的 id_map。
    因为按拓扑序规划时，后面的表要靠它翻译引用——而匹配关系在规划阶段就已确定，
    不必等到写入。只在新增时记映射是错的：第二次同步时全是"无变化"，
    映射表空空如也，所有引用都会翻译失败。
    """
    dst_app = cfg["target"]["app"]
    src_by_label, src_rows = read_source(client, cfg, table)
    dst_by_label, _ = client.field_map(dst_app, table["target_entry"])

    ref_fields = set((table.get("refs") or {}).keys())
    mapping, excluded = resolve_fields(src_by_label, dst_by_label, table.get("fields"),
                                       ref_fields)
    # 引用字段单独走翻译，不参与普通字段映射
    for f in ref_fields:
        mapping.pop(f, None)
    src_shapes, dst_shapes = table_shapes(src_by_label, dst_by_label, mapping)
    # 目标端类型：比对要按类型走（子表单/附件各有各的 canonical 化）
    dst_types = {d: dst_by_label[d]["type"] for d in mapping.values()
                 if d in dst_by_label}

    dst_key = (table.get("fields") or {}).get(table["key"], table["key"])
    # limit：先拿几条试水。同步是批量改动，全量跑之前先小范围验一遍口径对不对
    dst_rows = client.fetch_all(dst_app, table["target_entry"])

    # 业务键重复 = 整个同步模型的前提被打破。
    # 原来这里 setdefault「取第一条」，另一条从此永远不会被更新，两边越漂越远，
    # 而且**一句提示都没有**。键要是不唯一，"按键匹配"就不是匹配，是猜。
    dst_index, dst_by_id, dst_dupes = {}, {}, {}
    for r in dst_rows:
        dst_by_id[r["_id"]] = r
        k = business_key(r, dst_by_label, dst_key)
        if k is None:
            continue
        if k in dst_index:
            dst_dupes.setdefault(k, [dst_index[k]["_id"]]).append(r["_id"])
        else:
            dst_index[k] = r

    src_dupes = {}
    seen_src = {}
    for r in src_rows:
        k = business_key(r, src_by_label, table["key"])
        if k is None:
            continue
        if k in seen_src:
            src_dupes.setdefault(k, [seen_src[k]]).append(r["_id"])
        else:
            seen_src[k] = r["_id"]

    creates, updates, skips, problems, matched = [], [], [], [], []
    for k, ids in sorted(dst_dupes.items()):
        problems.append({
            "kind": "duplicate_key_target", "source_id": None,
            "detail": "目标端业务键「%s」= %s 有 %d 条记录（%s）——"
                      "只有第一条会被更新，其余永远同步不到。"
                      "先在目标端去重，或换一个真正唯一的业务键"
                      % (table["key"], k, len(ids), "、".join(ids[:4]))})
    for k, ids in sorted(src_dupes.items()):
        problems.append({
            "kind": "duplicate_key_source", "source_id": None,
            "detail": "源端业务键「%s」= %s 有 %d 条记录（%s）——"
                      "它们会依次写向同一条目标记录，后一条覆盖前一条"
                      % (table["key"], k, len(ids), "、".join(ids[:4]))})
    for row in src_rows:
        key = business_key(row, src_by_label, table["key"])
        if key is None:
            problems.append({"kind": "missing_key", "source_id": row["_id"],
                             "detail": "业务键「%s」为空，无法判断目标端是否已存在" % table["key"]})
            continue
        values = {mapping[s]: v for s, v in
                  row_values(row, src_by_label, list(mapping), src_shapes).items()}
        # 全空子行搬不过去（简道云自己会吞掉），**丢了就要说**——
        # 多半是那几行的数据都在被排除的内层字段里（内层附件、内层关联数据）。
        for s_label in src_shapes:
            w = src_by_label.get(s_label)
            if not w or w["type"] != "subform":
                continue
            raw_n = len([r for r in (row.get(w["name"]) or []) if isinstance(r, dict)])
            kept = len(values.get(mapping[s_label]) or [])
            if raw_n > kept:
                problems.append({
                    "kind": "subform_empty_rows", "source_id": row["_id"],
                    "detail": "子表单「%s」有 %d 行的可搬内层字段全为空，搬不过去"
                              "（简道云会丢弃全空子行，实测提交 3 行回读 2 行）——"
                              "常见成因是这几行的数据都在被排除的内层字段里"
                              % (s_label, raw_n - kept)})
        refs, unresolved = translate_refs(row, src_by_label, table, id_map)
        for field, why in unresolved:
            problems.append({"kind": "unresolved_ref", "source_id": row["_id"],
                             "detail": "引用字段「%s」：%s" % (field, why)})
        for field, target_id in refs.items():
            values[(table.get("fields") or {}).get(field, field)] = target_id

        # 先认 ID 映射，再退回业务键。
        #
        # 只按业务键匹配会有个很难发现的坑：目标端那条记录的业务键被人改过之后
        # （在简道云界面里改、或用 Excel 回写改），下次同步就认不出它，
        # 于是**新建一条重复记录**，接口不会拦。实测中就这样多出了一条联系人。
        # ID 映射表本来就是为了"业务键变了也知道是同一条"而存在的，
        # 建了、存了，却唯独没在匹配这一步用上。
        mapped_id = id_map.get(table["alias"], row["_id"])
        existing = dst_by_id.get(mapped_id) if mapped_id else None
        matched_by = "id_map"
        if existing is None:
            if mapped_id:
                # 映射指向的记录已不在目标端（被删了），这条映射作废
                problems.append({
                    "kind": "stale_mapping", "source_id": row["_id"],
                    "detail": "ID 映射指向的目标记录 %s 已不存在，改按业务键重新匹配"
                              % mapped_id})
            existing = dst_index.get(key)
            matched_by = "key"
        if existing is None:
            creates.append({"key": key, "source_id": row["_id"], "values": values})
            continue
        # 匹配上就立刻登记映射——更新与无变化同样要登记
        id_map.put(table["alias"], row["_id"], existing["_id"])
        matched.append((row["_id"], existing["_id"]))
        current = row_values(existing, dst_by_label, list(values), dst_shapes)
        # ⚠️ 比对按**类型**走：子表单要 canonical 化整体比（拿 display_value 比会因为
        # 它只给"N 行子表单"，把"行数相同、内容全变"判成无变化）；附件只比 (name,size)。
        diff = {k: v for k, v in values.items()
                if canonical(current.get(k), dst_types.get(k))
                != canonical(v, dst_types.get(k))}
        if diff:
            updates.append({"key": key, "source_id": row["_id"],
                            "target_id": existing["_id"], "values": values, "diff": diff})
        else:
            skips.append(key)

    # 附件要真的过一遍本地磁盘（下载→重传），大表可能是几个 G、几十分钟。
    # **计划阶段就把总量摆出来**（D8），别让用户点了 --execute 才发现（写到一半更糟）。
    attach_fields = sorted(d for d in mapping.values()
                           if dst_types.get(d) in ATTACHMENT_TYPES)
    files = size = 0
    for item in creates + updates:
        for label in attach_fields:
            for f in (item["values"].get(label) or []):
                files += 1
                size += int(f.get("size") or 0)

    return {"alias": table["alias"], "key": table["key"],
            "limited": table.get("limit"),
            "source_rows": len(src_rows), "target_rows": len(dst_rows),
            "mapped_fields": mapping, "excluded": excluded,
            "ref_fields": sorted(ref_fields),
            # 这两张名单**要进计划快照**：apply 照快照执行时不再重算映射，
            # 但它必须知道哪些列的值是"附件原值、要先搬文件"、哪些是子表单。
            "attachment_fields": attach_fields,
            "subform_fields": sorted(d for d in mapping.values()
                                     if dst_types.get(d) == "subform"),
            "attachments": {"files": files, "bytes": size},
            "creates": creates, "updates": updates, "skips": skips,
            "problems": problems, "matched": len(matched)}
