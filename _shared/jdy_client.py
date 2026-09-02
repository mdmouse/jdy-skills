# -*- coding: utf-8 -*-
"""简道云 API 共享内核。

设计依据全部来自 2026-08-27 的真实账号实测（见 references/write-behavior.md）：
  * 写入值必须 {"value": x} 包裹，子表单内层同样要包——少包一层静默丢数据
  * API 几乎不校验，脏值静默存 null 并返回 success，所以写入后必须回读比对
  * user 字段读回来是对象，写进去必须是 username 字符串（读写不对称）
  * linkdata 十种写法全部静默失败，判定为不可写
  * sn 流水号系统生成，且计数器可能与既有数据冲突
  * datetime 只认三种格式，且按 UTC 原样接收——源时区要显式指定后转 UTC

约束：仅用标准库（各端沙箱大概率禁 pip），兼容 Python 3.8。
"""

import hashlib
import json
import mimetypes
import os
import random
import re
import sys
import unicodedata
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import platform_env

API_ROOT = "https://api.jiandaoyun.com/api"
API_BASE = API_ROOT + "/v5"          # 数据接口的默认版本

# 简道云的接口版本是混的：数据接口 v5、流程读接口 v6、流程写接口 v1/v2。
# 所以 post() 允许传带版本前缀的路径（如 "/v6/workflow/task/list"），
# 不带前缀的沿用 v5，保持既有调用不变。
CONFIG_PATH = os.path.expanduser("~/.jdy/config.json")   # 默认位置，仅用于报错文案
ENV_KEY = "JDY_API_KEY"
# 缓存目录不再是常量：`~/.jdy` 在 WorkBuddy 沙箱里就不可写，另外两端未知。
# 交给 platform_env 在**运行时**找一个真能写的地方（见该模块开头的说明）。
# 不在导入期解析——探测会真的建目录写文件，导入不该有这种副作用。

MAX_BATCH = 100                     # batch_create 硬上限，超出报 17024
RETRY_CODES = {429, 500, 502, 503, 504}
RATE_LIMIT_CODE = 8303

# 实测确认的每端点频率上限（次/秒）。全局另有 50/秒，取较小者。
ENDPOINT_RATE = {
    "/app/list": 30,
    "/app/entry/list": 30,
    "/app/entry/widget/list": 30,
    "/app/entry/data/list": 30,
    "/app/entry/data/get": 30,
    "/app/entry/data/create": 20,
    "/app/entry/data/batch_create": 10,
    "/app/entry/data/update": 20,
    # 流程接口（注意版本各不相同）
    "/v6/workflow/task/list": 20,
    "/v6/workflow/instance/get": 30,
    "/v1/workflow/task/approve": 20,
    "/v1/workflow/task/reject": 20,
    "/v1/workflow/task/transfer": 20,
    "/v2/workflow/task/rollback": 20,
}
DEFAULT_RATE = 10                   # 未知端点从严

# 源数据时区默认北京时间。**不取机器本地时区**——否则同一份 Excel 在不同时区的
# 机器上导入会落成不同的值，且没有任何迹象。简道云是国产工具、界面按 +8 显示，
# 中文用户 Excel 里的时间默认就是北京时间。中国无夏令时，固定偏移即准确。
DEFAULT_TZ = timezone(timedelta(hours=8))

# 只读 / 不可写字段类型。**这是全项目唯一的一份**——
# jdy-devkit 的形状表原来自己另列了一份，多出一个 autonum，
# 而内核不认识它，于是 preflight/sync/clean 三条链路都会放这一列过去。
READ_ONLY_TYPES = {
    "sn",                # 流水号：系统生成，计数器还可能与既有数据冲突。
                         # 界面上的「自动编号」控件，API 返回的也是这个类型。
    "autonum",           # 保险起见留着：扫过本账号 74 张表单、25 种控件类型，
                         # 这个类型名一次都没出现过（很可能当初是凭印象写的）。
                         # 拦一个不存在的类型零成本，而两种错法不对称——
                         # 错拦一个能写的字段，用户在 skipped 清单里一眼能推翻；
                         # 错放一个系统字段，是静默写坏或静默丢。
}
NOT_WRITABLE_TYPES = {"linkdata"}   # 「选择数据」控件：官方明示不支持 API 写入
UNWRITABLE_REASON = {               # 给用户看的成因，别让调用方各写一句
    "linkdata": "「选择数据」控件，官方明示不支持 API 写入（实测十种写法全灭）",
    "sn": "流水号，系统生成",
    "autonum": "自动编号，按系统生成处理（实测中界面的自动编号返回的是 sn）",
}

# 「关联数据」控件（API type = lookup）**可以**直写 data_id 建立关系，
# 但接口不做引用完整性校验——写个不存在的 ID 照样静默入库。
# 中文名和 API 类型是反直觉的：选择数据=linkdata(死)，关联数据=lookup(活)。
LOOKUP_TYPES = {"lookup"}
_DATA_ID = re.compile(r"^[0-9a-f]{24}$")


class JdyError(Exception):
    """API 返回的业务错误。"""

    def __init__(self, code, msg, http_status=None, path=None):
        super(JdyError, self).__init__("[%s] %s (HTTP %s, %s)" % (code, msg, http_status, path))
        self.code = code
        self.msg = msg
        self.http_status = http_status
        self.path = path


class NotWritableField(Exception):
    """字段类型无法通过 API 写入——preflight 应当提前拦住。

    kind 区分成因：'unwritable'（接口不支持，如 linkdata）
    与 'system_generated'（系统生成，如 sn）。调用方按 kind 分类，
    不要去匹配报错文案——文案会变，分类逻辑不该跟着碎。
    """

    def __init__(self, message, kind="unwritable"):
        super(NotWritableField, self).__init__(message)
        self.kind = kind


class TokenBucket:
    """按端点限速。简道云超限返回 8303，退避成本远高于本地等待。"""

    def __init__(self, rate):
        self.rate = float(rate)
        self.capacity = float(rate)
        self.tokens = float(rate)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                time.sleep((1.0 - self.tokens) / self.rate)


# --------------------------------------------------------------------------
# 值编码：把 Python 值转成简道云要的写入格式
# --------------------------------------------------------------------------

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",          # Excel 最常见，简道云自己不认，必须归一
    "%Y.%m.%d", "%Y年%m月%d日",
    # `01/12/2025` 这类**不**放在这里：日/月谁在前无从判断，
    # 靠格式在列表里的先后顺序决定等于抛硬币。见 _parse_slash_date。
]

_SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")

# strptime 把格式串里的 Z 当**字面量**匹配，解析结果是 naive 的——
# 再按源时区（默认 +08:00）补时区，一个本来就是 UTC 的 "…Z" 就被平移了 8 小时。
# 导出→改→回导是最高频的工作流，这一条会让每走一轮时间列就整体倒退 8 小时。
_UTC_FORMATS = {"%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"}


def _parse_slash_date(text):
    """`01/12/2025` 这种两位数在前的斜杠日期。

    原来 `%d/%m/%Y` 排在 `%m/%d/%Y` 前面，于是 `01/12/2025` **恒**被读成 12 月 1 日——
    而中文用户写它多半指 1 月 12 日。谁在前是由格式列表的先后顺序决定的，
    等于抛硬币，且错了没有任何迹象：存进去是个合法日期，回读比对也通过。

    只在能判断时判断：有一位 >12 就它是"日"。两位都 ≤12 就报错让人改写法，
    绝不猜——猜错一次，整列日期就错了一年中的某几个月。
    """
    m = _SLASH_DATE.match(text)
    if m is None:
        return None
    a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if a > 12 and b <= 12:
        return datetime(year, b, a)          # 日/月/年
    if b > 12 and a <= 12:
        return datetime(year, a, b)          # 月/日/年
    if a > 12 and b > 12:
        raise ValueError("不是合法日期：%r" % text)
    raise ValueError(
        "%r 的日/月顺序无法判断：既可能是 %d 月 %d 日，也可能是 %d 月 %d 日。"
        "请把这一列改成 YYYY-MM-DD（或 YYYY/MM/DD）再导入——"
        "猜错一次整列就错了，而且写进去之后看不出来。" % (text, b, a, a, b))


def parse_iso(value, assume_utc=True):
    """ISO 串 → datetime。带时区的按其自身，不带的按 UTC 补齐（assume_utc）。

    统一放内核是因为 naive/aware 混算会直接抛 TypeError：
    流程侧拿 create_time 减 now(utc)，只要接口返回的串不带 Z 就崩。
    看不懂的输入返回 None，让调用方决定怎么如实说明，而不是崩在减法上。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None and assume_utc:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_tz(spec):
    """'+08:00' / '-04:00' / 'utc' / 'local' → tzinfo。"""
    if spec is None:
        return DEFAULT_TZ
    if hasattr(spec, "utcoffset"):          # 已经是 tzinfo，直接用
        return spec
    text = str(spec).strip().lower()
    if text in ("utc", "z", "+00:00", "0"):
        return timezone.utc
    if text == "local":
        return datetime.now().astimezone().tzinfo
    m = re.match(r"^([+-])(\d{1,2}):?(\d{2})?$", text)
    if not m:
        raise ValueError("看不懂的时区：%r（用 +08:00 / -04:00 / utc / local）" % spec)
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))


def normalize_datetime(value, tz=None):
    """把各种日期写法归一成简道云接受的 ISO-UTC。

    实测：简道云只认 ISO-Z / 'Y-m-d' / 'Y-m-d H:M:S'，其余静默存 null；
    且不带时区的输入被当作 UTC 原样收下。

    tz 是**源数据**的时区，默认 +08:00（见 DEFAULT_TZ 的说明）。
    值自带时区时以其自身为准。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        dt = parse_iso(text, assume_utc=False)        # 先认自带时区的 ISO 串（含 "…Z"）
        if dt is None or dt.tzinfo is None:
            dt = _parse_slash_date(text)                 # 歧义就在这里抛，不往下猜
        if dt is None:
            for fmt in _DATE_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                except ValueError:
                    continue
                if fmt in _UTC_FORMATS:
                    dt = dt.replace(tzinfo=timezone.utc)   # 格式里的 Z 是真时区，不是字面量
                break
        if dt is None:
            raise ValueError("无法识别的日期格式：%r" % value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz if tz is not None else DEFAULT_TZ)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def to_number(value):
    """宽松取数：取不出来返回 None，**不抛**。用于聚合统计。

    与 `_coerce_number`（写入前的严格版，取不出来就抛）是同一套口径的两个入口。
    原来 jdy-query 直接 `float()`、jdy-report 另写一份（剥千分位逗号 + 拒 bool），
    于是同一列文本数字，两个技能求和给出不同答案——而两边都不报错。
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip().replace(",", "").replace("，", ""))
    except ValueError:
        return None


def _coerce_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("布尔值不能写入数字字段：%r" % value)
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace(",", "")
    try:
        return int(text) if re.match(r"^-?\d+$", text) else float(text)
    except ValueError:
        raise ValueError("无法解析为数字：%r" % value)


def _coerce_user(value):
    """成员字段只吃 username 字符串。传对象或显示名都会被静默丢弃。"""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        username = value.get("username")
        if not username:
            raise ValueError("成员字段需要 username，收到的对象里没有：%r" % value)
        return username
    text = str(value).strip()
    if not text.startswith("sys_") and not re.match(r"^[A-Za-z0-9_.\-]+$", text):
        raise ValueError("成员字段收到的疑似显示名而非 username：%r —— 需先查通讯录解析" % value)
    return text


def _coerce_lookup(value, widget):
    """关联数据字段只吃目标记录的 data_id。

    为什么必须在这里拦：接口**不校验引用是否存在**，写什么存什么。
    用户往这一列填「某某客户」这类业务名称时，会原样存成一个指向虚无的字符串——
    回读还"一致"，所以连回读比对都发现不了。这是本项目见过最隐蔽的一种脏数据。
    """
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        value = value.get("id") or value.get("_id") or ""
    text = str(value).strip()
    if not _DATA_ID.match(text):
        raise ValueError(
            "关联数据字段「%s」只接受目标记录的 data_id（24 位十六进制），收到 %r。"
            "接口不校验引用是否存在，直接写业务名称会存成一个指向虚无的引用——"
            "请先按业务键查出目标记录的 _id 再写入。" % (widget.get("label"), value))
    return text


def encode_value(widget, value, tz=None):
    """按字段类型把 Python 值编码成写入格式（不含 {"value": ...} 外壳）。"""
    wtype = widget.get("type")

    if wtype in NOT_WRITABLE_TYPES:
        raise NotWritableField(
            "字段「%s」类型 %s 无法通过 API 写入（实测十种写法全部静默失败）"
            % (widget.get("label"), wtype), kind="unwritable")
    if wtype in READ_ONLY_TYPES:
        raise NotWritableField("字段「%s」类型 %s 由系统生成，不可写入"
                               % (widget.get("label"), wtype), kind="system_generated")

    if value is None or value == "":
        return None
    if wtype == "datetime":
        return normalize_datetime(value, tz=tz)
    if wtype == "number":
        return _coerce_number(value)
    if wtype in LOOKUP_TYPES:
        return _coerce_lookup(value, widget)
    if wtype in ("user",):
        return _coerce_user(value)
    if wtype in ("usergroup",):
        seq = value if isinstance(value, (list, tuple)) else [value]
        return [_coerce_user(v) for v in seq]
    if wtype == "dept":
        # 实测：只认**裸 dept_no 整数**。完整对象、{"dept_no": n}、部门名字符串
        # 三种写法全部静默丢弃（接口回报成功、字段存成 null）。
        # 与 user 同一种不对称：写 ID，读回来是展开的对象。
        if isinstance(value, dict):
            no = value.get("dept_no")
            if no is None:
                raise ValueError("部门字段需要 dept_no，收到的对象里没有：%r" % value)
            return int(no)
        if isinstance(value, bool):
            raise ValueError("布尔值不能写入部门字段：%r" % value)
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if re.match(r"^\d+$", text):
            return int(text)
        raise ValueError(
            "部门字段只认部门编号 dept_no（整数），收到 %r。"
            "写部门名会被简道云静默丢弃——接口照样回报成功，字段却是空的。"
            "部门编号可以从这张表已有数据的该列里读出来。" % value)
    if wtype == "linkobject":
        # 实测：{"link_id": "<data_id>"} 可写，接口会自己补上 link_form 与目标记录
        # 的真实 name（写进去的 name 会被覆盖）。裸 data_id 字符串报 3005。
        if isinstance(value, dict):
            link_id = value.get("link_id") or value.get("id") or value.get("_id")
            if not link_id:
                raise ValueError("关联表单字段需要 link_id，收到的对象里没有：%r" % value)
        else:
            link_id = str(value).strip()
        if not _DATA_ID.match(str(link_id)):
            raise ValueError(
                "关联表单字段「%s」只接受目标记录的 data_id（24 位十六进制），收到 %r"
                % (widget.get("label"), value))
        return {"link_id": str(link_id)}
    if wtype in ATTACHMENT_TYPES:
        # 实测：写的是**上传返回的 key 组成的字符串列表**，`[{"key": k}]` 静默丢弃。
        # 读回来展开成 [{"name","size","mime","url"}]，url 还带过期戳——
        # 所以**读回来的值不能直接回灌**，附件要搬只能重新下载再上传。
        # 另外：写入请求必须带上取凭证时用的同一个 transaction_id。
        seq = value if isinstance(value, (list, tuple)) else [value]
        keys = []
        for item in seq:
            if isinstance(item, dict):
                k = item.get("key")
                if not k:
                    raise ValueError(
                        "附件字段「%s」收到的是读回来的形状（%s），里面只有带过期戳的 url，"
                        "没有 key——附件不能回灌，要先用 upload_files() 重新上传拿 key"
                        % (widget.get("label"), sorted(item)))
                keys.append(str(k))
            else:
                keys.append(str(item))
        return keys
    if wtype in ("checkboxgroup", "combocheck"):
        if isinstance(value, (list, tuple)):
            return list(value)
        # 只按半角逗号拆是不够的：读出来的多选是列表，display_value 用「、」拼，
        # Excel 里人手打的又常是「，」或「;」——一条链路上三种分隔符。
        # 中文分隔符不拆的话，"线上、线下" 会被当成**一个**选项名写进去。
        return [s for s in (p.strip() for p in re.split(r"[,，、;；]", str(value))) if s]
    if wtype == "address":
        # 实测：对象形式可写，拼接成的字符串会被静默丢弃
        if isinstance(value, dict):
            return value
        raise ValueError("地址字段需要 {province, city, district, detail} 对象；"
                         "拼接好的地址字符串会被简道云静默丢弃")
    if wtype == "phone":
        # 实测：{"phone": "138…"} 可写，纯字符串会被静默丢弃
        if isinstance(value, dict):
            if not value.get("phone"):
                raise ValueError("电话字段的对象里缺少 phone：%r" % value)
            return {"phone": str(value["phone"])}
        return {"phone": str(value).strip()}
    if wtype == "subform":
        raise ValueError("子表单请走 encode_row 的 subform 分支")
    if isinstance(value, (dict, list)):
        # 兜底分支原来对结构化值直接 str()，提交上去是一串 Python repr
        # （"{'name': 'mdmouse', ...}"）——接口收下、存成一坨没人看得懂的字符串。
        # 没有专门分支的结构化类型，宁可在这里拦住。
        raise ValueError(
            "字段「%s」（%s）收到的是%s，而这个类型没有已实测的写入形状——"
            "拒绝提交。硬写进去会存成一串 Python 字面量。"
            % (widget.get("label"), wtype, "对象" if isinstance(value, dict) else "列表"))
    return str(value) if not isinstance(value, str) else value


def encode_row(widgets_by_label, row, tz=None):
    """把 {显示名: 值} 编码成 batch_create 的一条 data。

    返回 (data, skipped)。skipped 是 [{"column","reason","kind"}]——它就是给用户看的
    「这些列导不进去」清单，必须在导入前呈现，而不是导完才发现是空的。
    kind 取值：unknown_column / unwritable / system_generated / bad_value
    """
    data = {}
    skipped = []
    for label, value in row.items():
        widget = widgets_by_label.get(label)
        if widget is None:
            skipped.append({"column": label, "kind": "unknown_column",
                            "reason": "表单中不存在该字段（简道云会静默忽略）"})
            continue
        try:
            if widget.get("type") == "subform":
                inner = {i["label"]: i for i in widget.get("items", [])}
                rows = value if isinstance(value, (list, tuple)) else []
                encoded_rows = []
                for sub_row in rows:
                    sub_data, sub_skipped = encode_row(inner, sub_row, tz=tz)
                    skipped.extend([dict(item, column="%s.%s" % (label, item["column"]))
                                    for item in sub_skipped])
                    encoded_rows.append(sub_data)
                if encoded_rows:
                    data[widget["name"]] = {"value": encoded_rows}
                continue
            encoded = encode_value(widget, value, tz=tz)
            if encoded is not None:
                data[widget["name"]] = {"value": encoded}     # 外壳在这里，且只在这里
        except NotWritableField as exc:
            skipped.append({"column": label, "kind": exc.kind, "reason": str(exc)})
        except ValueError as exc:
            skipped.append({"column": label, "kind": "bad_value", "reason": str(exc)})
    return data, skipped


def infer_lookup_target(client, app_id, entry_id, widget, sample=20):
    """推断某个关联数据字段指向哪张表单。

    `widget/list` 对 lookup 只返回 name/label/type，**不说明目标表**，
    而校验引用是否存在又非知道目标表不可。办法是反查：
    取该字段已有的几个引用 ID，拿到同应用各表单逐个 data/get，
    能查到的那张就是目标表。

    返回 entry_id；样本不足或查不到时返回 None（调用方要如实说明"无法校验引用"，
    不能假装校验过了）。
    """
    try:
        rows = client.fetch_all(app_id, entry_id, limit=sample, page_size=sample)
    except JdyError:
        return None
    ids = []
    for row in rows:
        v = row.get(widget["name"])
        if isinstance(v, dict):
            v = v.get("id")
        if isinstance(v, str) and v:
            ids.append(v)
        if len(ids) >= 3:
            break
    if not ids:
        return None
    for form in client.list_forms(app_id):
        if form["entry_id"] == entry_id:
            continue
        hit = 0
        for ref in ids:
            try:
                got = client.post("/app/entry/data/get",
                                  {"app_id": app_id, "entry_id": form["entry_id"],
                                   "data_id": ref})
            except JdyError:
                break
            if (got.get("data") or {}).get("_id") == ref:
                hit += 1
        if hit == len(ids):
            return form["entry_id"]
    return None


def locate_reference(client, data_id, app_ids=None, max_forms=200):
    """这个 data_id 到底在哪张表里？返回 (app_id, entry_id) 或 None。

    存在的理由是把两件**完全不同**的事分开：
      · 「反查不到目标表」——可能只是这一列的样本不够，或者指向别的应用；
      · 「这个引用在整个账号里都找不到」——目标记录已经被删了，
        这一列的关系**本身就是断的**，迁不迁移都一样。

    把两者混成一句"反查不到"，用户会以为是工具不行，然后去手工配一遍——
    而实际上他要面对的是一批已经断掉的引用。

    逐表 data/get 探测，命中即停；上界 max_forms，别在大账号上跑失控。
    """
    probed = 0
    for app in client.list_apps():
        if app_ids and app["app_id"] not in app_ids:
            continue
        for form in client.list_forms(app["app_id"]):
            if probed >= max_forms:
                return None
            probed += 1
            try:
                got = client.post("/app/entry/data/get",
                                  {"app_id": app["app_id"], "entry_id": form["entry_id"],
                                   "data_id": data_id})
            except JdyError:
                continue
            if (got.get("data") or {}).get("_id") == data_id:
                return app["app_id"], form["entry_id"]
    return None


def lookup_exists(client, app_id, target_entry_id, data_id):
    """目标记录是否真的存在。接口写入时不校验，只能自己查。"""
    if not target_entry_id:
        return None                      # 不知道目标表 → 无法判断，别谎称通过
    try:
        got = client.post("/app/entry/data/get",
                          {"app_id": app_id, "entry_id": target_entry_id,
                           "data_id": data_id})
    except JdyError:
        return False
    return (got.get("data") or {}).get("_id") == data_id


BACKUP_PREFIX = "backup_"


def backup_path(base_dir, entry_id, when=None):
    """备份文件名：`backup_<entry_id>_<YYYYmmdd-HHMMSS>.json`。

    三条写入链路原来三种命名（有的用 alias、有的用 Unix 时间戳整数），
    于是"有备份、没有恢复入口"——想恢复的人得先猜每个技能的文件名怎么排。
    统一之后 restore.py 能认出任何一个技能落下的备份。
    """
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return os.path.join(base_dir, "%s%s_%s.json" % (BACKUP_PREFIX, entry_id, stamp))


def load_backup(path):
    """读一份备份，校验形状。返回 (app_id, entry_id, rows)。"""
    with open(path, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    if not isinstance(blob, dict) or "data" not in blob:
        raise ValueError("%s 不像是本工具产出的备份（缺少 data）" % path)
    rows = blob.get("data") or []
    if not isinstance(rows, list):
        raise ValueError("%s 的 data 不是列表" % path)
    return blob.get("app_id"), blob.get("entry_id"), rows


EXPORT_ID_COLUMN = "_id"          # 导出时附带，导入时用来定位要更新哪条
EXPORT_TIME_COLUMN = "创建时间"    # 导出时附带的系统时间，导入时忽略
EXPORT_SYSTEM_COLUMNS = (EXPORT_ID_COLUMN, EXPORT_TIME_COLUMN)


def plan_code(parts):
    """由计划内容算出的短确认码。内容一变码就变，旧码立即失效——
    保证用户点头的那份计划，和真正执行的那份是同一个。"""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


CONFIRM_THRESHOLD = 50       # 改动超过这么多条就必须走确认码


def confirm_threshold(requested=None):
    """规模闸门的阈值：**只许调小，不许调大**。

    三个脚本原来把 `--confirm-threshold` 原样交给闸门，于是
    `--confirm-threshold 999999` 就等于把闸门拆了——一个安全机制
    不该由它自己的参数解除。想更保守可以调小，想放宽只能是人点头。
    """
    if requested is None:
        return CONFIRM_THRESHOLD
    try:
        return max(0, min(int(requested), CONFIRM_THRESHOLD))
    except (TypeError, ValueError):
        return CONFIRM_THRESHOLD


def ask_yes(prompt):
    """交互确认。True=用户输入 yes；False=输入了别的；None=问不了（不是 tty，
    或读到 EOF——Windows 的 NUL 设备 isatty() 会说 True，input() 却直接 EOFError）。
    None 必须由调用方当成"非交互"处理，绝不能当成同意。

    为什么不能各脚本自己写 `if sys.stdin.isatty(): input(...)`：
    POSIX 上 `stdin=DEVNULL` 的 isatty() 是 False，那条 else 分支走得到；
    **Windows 的 NUL 是字符设备，isatty() 返回 True**，于是同一份代码走进
    input()，第一次读就 EOFError，脚本带着 traceback 以退出码 1 死掉——
    该说的"拒绝写入：当前是非交互环境"一个字都没说出来，调用方也拿不到约定的 4。
    "问不了"和"问了但没答应"都不是同意，两者都由调用方落到拒绝/取消上。

    prompt 只有真要问人时才打印：非交互环境下 input() 根本不执行，
    stderr 上的拒绝文案不会被一段无人应答的提示语冲淡。
    """
    try:
        if not sys.stdin.isatty():
            return None
    except (AttributeError, ValueError, OSError):
        return None                      # stdin 是 None 或已关闭，同样是问不了
    try:
        answer = input(prompt)
    except EOFError:
        # Windows 的 NUL、被重定向到空文件的 stdin、以及管道被提前关掉，
        # 都在这里。**这一条捕获就是本函数存在的理由，别顺手删掉。**
        return None
    except (OSError, ValueError, UnicodeDecodeError):
        return None                      # stdin 读不动（已关闭 / 编码炸了）
    return answer.strip() == "yes"


def scale_gate(scale, code, given, threshold=None, detail_lines=(), what="写入"):
    """大批量写入前的二次确认。放行返回 None，需要确认则返回退出码 5。

    为什么两条写入链路都要有：Excel 批量导入是最容易一次写坏几百条的地方，
    偏偏它原来没有闸门，而更保守的同步反倒有。安全措施不能挑地方放。
    """
    threshold = confirm_threshold(threshold)      # 兜底：调用方漏夹紧也不至于失效
    if scale <= threshold or given == code:
        return None
    print("\n" + "=" * 68)
    print("⚠️ 本次要%s %d 条数据%s——需要先跟用户确认一次"
          % (what, scale, "，属于大批量" if scale > CONFIRM_THRESHOLD else ""))
    print("=" * 68)
    for line in detail_lines:
        print("   %s" % line)
    if given:
        print("\n给的码 %s 与当前计划不符——**数据在这期间变了**，请重新确认后用新码。"
              % given)
    print("\n请这样跟用户说：「这次要%s %d 条数据，确认执行吗？」" % (what, scale))
    print("得到同意后带上这个码重跑：  --confirm-code %s" % code)
    print("（码由本次计划内容算出，数据一变即失效。不要向用户提「阈值」或这个码"
          "——那是内部机制）")
    return 5


# --where 的筛选 DSL。**只有这一份**——jdy-query 与 jdy-excel-bridge 原来各写一遍，
# 两边的 method 清单还不一样（query 少了 verified/unverified/all，也不收 filter JSON），
# 于是同一句 `--where` 在两个技能里一个能跑、一个报"不支持的 method"。
FILTER_METHODS = ("empty", "not_empty", "eq", "ne", "in", "nin", "range", "like",
                  "gt", "lt", "verified", "unverified", "all")
FILTER_NO_VALUE = ("empty", "not_empty", "verified", "unverified")
# 每种 method 该收几个值。逗号是多值分隔符，所以 `金额:lt:1,000` 会被切成
# 两个值——而 lt 收两个值是没有意义的，接口那边只会更莫名其妙。
FILTER_ARITY = {"eq": 1, "ne": 1, "gt": 1, "lt": 1, "like": 1, "range": 2}


def resolve_filter_field(name, by_label, by_name):
    """--where 里的字段：显示名和字段标识都收。

    认不出来必须报错。简道云对不认识的字段**既不报错也不过滤**——
    直接返回全表。实测 `--where '姓名=张三'` 导出了全部 25 行，
    而调用方以为筛过了。这种"筛选静默失效"比报错危险得多。
    """
    if name in by_name:
        return name
    if name in by_label:
        return by_label[name]["name"]
    raise ValueError("表单里没有字段「%s」。可用的显示名：%s"
                     % (name, "、".join(list(by_label)[:12])))


def filter_value(widget, method, raw):
    """把条件里的值转成**这个字段类型该有的样子**。

    实测（2026-08-31）：简道云对**类型不匹配的 filter 静默忽略**——
    数字字段传 `["1000"]` 字符串，接口照常 200、把**整表**还给你。
    `订单总额:lt:1000` 于是等于"没有条件"：26 行一条不落地回来，最大值 16080，
    而调用方以为筛过了。

    这和"不认识的字段"是同一种事故的两个入口：resolve_filter_field 守住了
    字段名那一侧，值的类型这一侧一直没人守——所以 --where 里的数字比较
    从来就没真正生效过。
    """
    if method in FILTER_NO_VALUE:
        return raw
    want = FILTER_ARITY.get(method)
    if want is not None and len(raw) != want:
        raise ValueError(
            "%s 需要 %d 个值，收到 %d 个（%s）。**逗号是多值分隔符**——"
            "写 `1,000` 这种千分位会被切成两个值；数字直接写 1000。"
            % (method, want, len(raw), "、".join(str(r) for r in raw)))
    if widget is None:
        return raw
    wtype = widget.get("type")
    out = []
    for item in raw:
        if wtype == "number":
            value = to_number(item)
            if value is None:
                raise ValueError(
                    "字段「%s」是数字类型，条件值 %r 不是数字。"
                    "直接传字符串会被简道云**静默忽略**——返回整表，看着像没筛。"
                    % (widget.get("label"), item))
            out.append(value)
        elif wtype == "datetime":
            try:
                out.append(normalize_datetime(item))
            except ValueError as exc:
                raise ValueError("字段「%s」是日期类型：%s" % (widget.get("label"), exc))
        else:
            out.append(item)
    return out


def build_filter(spec, by_label, by_name):
    """--where 'field=值' / 'field:method:值'，或直接给一段 filter JSON。

    **spec 必须是字符串。** 命令行传进来的当然是，但哨兵的规则是从 YAML 读的——
    `when: 123` 或 `when: {field: x}` 拿到的就是 int / dict，`spec.lstrip()`
    当场抛 AttributeError。那个异常谁都接不住：调用方按规则 catch 的是
    (JdyError, ValueError)，cli_main 也只接 ValueError，于是**整个哨兵进程**
    因为一条规则写错一个字就死了，别的规则一条都没跑。
    """
    if not spec:
        return None
    if not isinstance(spec, str):
        raise ValueError(
            "筛选条件要写成字符串，收到 %s：%r\n"
            "写法是 `字段=值`、`字段:method:值`，或一整段 filter JSON。"
            % (type(spec).__name__, spec))
    if spec.lstrip().startswith("{"):
        raw = json.loads(spec)
        # 下面每一条都是"手写 JSON 时真会写错"的形状。原来一条都不查：
        # cond 写成对象就地裸崩 AttributeError（命令行工具甩 traceback），
        # value 写成裸字符串会被 filter_value 当序列**逐字拆开**
        # （"1000" → 四个值 1、0、0、0），method 拼错则直接发给接口——
        # 而简道云对不认识的 method 是**静默忽略**的，返回整表，看着像没筛。
        if not isinstance(raw, dict):
            raise ValueError("filter JSON 顶层要是对象：{\"cond\": [...]}")
        if "cond" not in raw:
            # 顶层直接写了一个条件、忘了外面那层 {"cond": [...]}。原来这种形状
            # **整体透传**：下面五道校验一道都走不到，字段名写错、method 拼错、
            # 值是裸字符串，全都原样发给接口——而这三样简道云都是静默忽略的。
            if raw.get("field") or raw.get("method"):
                raw = {"cond": [raw]}
            else:
                raise ValueError(
                    "看不懂这段 filter JSON：顶层既没有 cond，也不像一个条件。\n"
                    "写法：{\"cond\": [{\"field\": \"某字段\", "
                    "\"method\": \"eq\", \"value\": [\"某个值\"]}]}")
        if not isinstance(raw.get("rel", "and"), str) or \
                raw.get("rel", "and").lower() not in ("and", "or"):
            raise ValueError("filter 的 rel 只能是 and 或 or，收到 %r" % raw.get("rel"))
        conds = raw.get("cond", [])
        if isinstance(conds, dict):
            conds = [conds]                      # 只有一个条件时人常常忘了外面那层方括号
            raw["cond"] = conds
        if not isinstance(conds, list):
            raise ValueError("filter JSON 的 cond 要是条件列表，收到 %s"
                             % type(conds).__name__)
        for cond in conds:
            if not isinstance(cond, dict):
                raise ValueError("filter JSON 的每个条件要是对象，收到 %r" % (cond,))
            method = cond.get("method")
            if method not in FILTER_METHODS:
                raise ValueError(
                    "filter JSON 里不支持的 method：%r（可用：%s）。"
                    "简道云对不认识的 method **静默忽略**——会把整表还给你，"
                    "看着像筛过了。" % (method, "、".join(sorted(FILTER_METHODS))))
            name = resolve_filter_field(cond.get("field", ""), by_label, by_name)
            cond["field"] = name
            if "value" in cond:
                value = cond["value"]
                if not isinstance(value, (list, tuple)):
                    value = [value]              # 裸标量补成单元素列表，别被当序列拆
                cond["value"] = filter_value(by_name.get(name), method, list(value))
        return raw
    conds = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.count(":") >= 2:
            field, method, value = part.split(":", 2)
        elif "=" in part:
            field, value = part.split("=", 1)
            method = "eq"
        else:
            raise ValueError("看不懂的条件：%r（用 field=值 或 field:method:值）" % part)
        if method not in FILTER_METHODS:
            raise ValueError("不支持的 method：%s（可用：%s）"
                             % (method, "、".join(FILTER_METHODS)))
        name = resolve_filter_field(field.strip(), by_label, by_name)
        cond = {"field": name, "method": method}
        if method not in FILTER_NO_VALUE:
            cond["value"] = filter_value(by_name.get(name), method,
                                         [v.strip() for v in value.split(",")])
        conds.append(cond)
    return {"rel": "and", "cond": conds}


# --------------------------------------------------------------------------
# 聚合：分组与指标。**只有这一份**——jdy-report 与 jdy-query 原来各写一套，
# 于是同一句"按某列求和"在两个技能里可以给出不同的数：一边剥千分位逗号、
# 一边不剥；一边把 True 当 1、一边不当；一边四舍五入到 4 位、一边不舍。
# 数字口径分叉是最难发现的一类 bug——两边都不报错，用户拿到两个数，
# 无从知道该信哪个。
# --------------------------------------------------------------------------

AGGS = ("count", "sum", "avg", "max", "min", "distinct")
UNFILLED = "(未填)"          # 分组时空值的归属，两个技能同一个说法


def aggregate_rows(rows, agg, field=None, resolve=None):
    """算一个指标。返回数值；没有可用数值时返回 0。

    resolve(row, field) 取该行某字段的值——两个技能取值的路径不同
    （报表按显示名走配置，查数按字段映射走控件），所以取值方式由调用方给，
    **口径**留在这里。

    为什么"没有数值"返回 0 而不是 None：这些数字要进表格和图表，
    None 会在渲染处被各写一遍兜底。空集求和是 0 没有歧义；
    真正需要区分"没有数据"的地方（如派生指标的分母）由调用方自己判断行数。
    """
    if agg not in AGGS:
        raise ValueError("不支持的 agg：%r（可用：%s）" % (agg, "、".join(AGGS)))
    if agg == "count":
        return len(rows)
    if field is None:
        raise ValueError("agg=%s 必须同时给 field" % agg)
    get = resolve or (lambda row, f: row.get(f))
    if agg == "distinct":
        return len({get(r, field) for r in rows if get(r, field) is not None})
    nums = [n for n in (to_number(get(r, field)) for r in rows) if n is not None]
    if not nums:
        return 0
    if agg == "sum":
        return round(sum(nums), 4)
    if agg == "avg":
        return round(sum(nums) / len(nums), 4)
    if agg == "max":
        return max(nums)
    return min(nums)


def group_rows(rows, dimensions, resolve=None):
    """按维度分组，返回 [(维度值元组, 行列表)]，按维度值排序。

    空值归到「(未填)」而不是丢掉：丢掉会让各组之和小于总数，
    而看的人不会去做这道减法——他会以为那些行不存在。

    返回顺序按维度值排序（稳定、可复现）。要按数值排就在调用方排，
    排序意图是展示层的事，不该固化在引擎里。
    """
    if not dimensions:
        return [((), list(rows))]
    get = resolve or (lambda row, f: row.get(f))
    buckets = {}
    for row in rows:
        key = tuple(UNFILLED if get(row, d) in (None, "", [], {}) else str(get(row, d))
                    for d in dimensions)
        buckets.setdefault(key, []).append(row)
    return sorted(buckets.items(), key=lambda kv: kv[0])


# 写入格式尚未实测的类型：宁可排除，也不要写进去再静默丢。
# checkboxgroup/combocheck 在 references/field-types.md 里标着「待实测」，
# 而它那条链路上有三种分隔符（读出来是列表、display_value 用「、」拼、
# 人在 Excel 里打「，」），实测通过之前不该上路。
#
# **这份清单原来长在 jdy-sync 里**，于是后来新写的 restore.py 没享受到它的保护，
# 又踩了一遍同一个坑。凡是"把读回来的值写回去"的场景都要用它，放内核。
# 2026-08-31 逐类实测（tests/real/write_probe.py），以下五类已解锁并各自有编码分支：
#   checkboxgroup / combocheck  字符串列表；裸字符串与顿号串**静默丢弃**
#   dept                        写裸 dept_no 整数；对象与部门名全部静默丢弃
#   company                     纯字符串
#   linkobject                  {"link_id": data_id}；裸 data_id 串报 3005
#
# 剩下的分两种，**不要混成一个名单**——混了理由就说不准，用户看到的原因也是错的：
UNVERIFIED_WRITE = {          # ① 真的没实测过，形状未知
    "deptgroup",              #    账号里 74 张表一个样本都没有
    "signature",              #    未签是 {} 空对象，签了之后的形状没见过
}
COMPLEX_WRITE = {             # ② 实测**可写**，但值要重新组装，逐条链路得自己实现
    "subform",                #    见下
    "image", "upload",        #    要先把文件传上去拿 key，见 upload_files()
}
# image/upload 实测（2026-08-31）：写的是**上传后拿到的 key 组成的字符串列表**
# （`["<key>"]`；`[{"key": k}]` 静默丢弃），且写入请求必须带上取凭证时的
# 同一个 transaction_id。读回来展开成 [{"name","size","mime","url"}]，
# url 带过期戳——**读回来的值不能直接回灌**，搬附件只能重新下载再上传。
# 所以 encode_value 认得它的写入形状（导入这条路只要调用方先 upload_files 就通），
# 但同步/恢复要"搬文件"，那是一个功能不是一行解锁。
#
# subform 实测（2026-08-31）：`[{内层字段: {"value": v}}]` 双层包裹可写，
# **读回来的扁平形状（内层不包 value）写回去是静默丢弃的**——读写形状不一样。
# update 是**整表替换**（写 1 行会把原来的 2 行冲掉），不能只改其中一行。
# encode_row 的 subform 分支产出的正是能写的那个形状，所以**导入这条路是通的**；
# 但同步要把源表内层字段按显示名映射到目标表内层字段，那是一个功能不是一行解锁。


def writable_back(widget):
    """这个字段**读回来的值**能不能原样写回去？返回 (能不能, 说不能的理由)。

    这是本项目反复栽的那个坑的统一出口：简道云的读写是不对称的——
    成员读回来是 `{"name": "张三", "username": "sys_x"}`，写进去只认 username；
    地址读回来是对象，`display_value` 拼成的串写进去会被静默丢弃；
    部门读回来带 `dept_no`，写一个裸的"研发部"照样"成功"、存进去是空的。

    同步（sync_value）和清洗（NORMALIZABLE）各自为这件事做过防护，
    而新写的恢复（restore.py）没有——所以把判断收拢到这里，
    下一个写"把值写回去"的人不用重新想一遍。
    """
    wtype = widget.get("type")
    if wtype in NOT_WRITABLE_TYPES:
        return False, UNWRITABLE_REASON.get(wtype, "接口不支持写入")
    if wtype in READ_ONLY_TYPES:
        return False, UNWRITABLE_REASON.get(wtype, "系统生成，不可写入")
    if wtype in UNVERIFIED_WRITE:
        return False, "该类型（%s）的写入格式尚未实测——宁可不动，也不要写回去再静默丢" % wtype
    if wtype in COMPLEX_WRITE:
        return False, ("该类型（%s）实测可写，但读回来的形状**不是**能写回去的形状，"
                       "得重新组装；这条链路还没实现" % wtype)
    return True, None


def report_skipped(skipped_fields, limit=5):
    """把「编码阶段就没提交的字段」摆出来。

    这些字段**不在回读核对范围内**——回读比对只看提交过的字段，
    没提交的当然"没有不一致"。三条写入链路（清洗/同步/导入）原来都把它丢掉，
    照打"✅ 逐字段回读核对通过"，用户以为整表都处理过了。
    """
    if not skipped_fields:
        return
    print("⚠️ 未提交的字段 %d 处（编码阶段被拦下，根本没发出去）：" % len(skipped_fields))
    for s in skipped_fields[:limit]:
        print("   %s：%s" % (s.get("column"), (s.get("reason") or "")[:70]))
    if len(skipped_fields) > limit:
        print("   … 另有 %d 处" % (len(skipped_fields) - limit))
    print("**这些字段没有被写入，也不在回读核对范围内**——请如实告诉用户")


def cli_main(fn):
    """脚本入口统一包一层：把异常变成一句人话 + 退出码，而不是甩一屏 traceback。

    Agent 平台会把 stderr 原样贴给用户。traceback 里没有一句是用户能据以行动的，
    而且 Agent 看到它往往会绕开技能自己造轮子。
    """
    try:
        return fn()
    except TargetError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    except JdyError as exc:
        sys.stderr.write("简道云接口报错：%s（%s）\n" % (exc.msg, exc.path or exc.code))
        return 2
    except (OSError, IOError) as exc:
        sys.stderr.write("读写文件失败：%s\n" % exc)
        return 2
    except ValueError as exc:
        # 参数/配置里的值不合法。命令行工具甩 traceback 永远是错的输出，
        # 用户和 Agent 从里面读不出该改什么。
        sys.stderr.write("参数或配置有问题：%s\n" % exc)
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\n已中断。\n")
        return 130


WRITE_ALLOWLIST_ENV = "JDY_WRITE_ALLOWLIST"
# 会改动数据的接口。放在 post() 这个唯一出口上校验，绕不过去——
# 逐个方法打补丁的话，将来新加一个写接口就会漏掉。
WRITE_PATH = re.compile(r"/(create|update|delete|batch_create|batch_update|"
                        r"batch_delete|approve|reject|rollback|transfer|submit)\b")

# 流程写接口的 body 里只有 username/instance_id/task_id，**没有 app_id/entry_id**——
# post() 在这里无从判断目标表单，硬查只会拿 (None, None) 无条件拒绝掉所有流程操作。
# 这类请求改由调用方在拿到待办清单时逐条 check_workflow_writable()，
# 那时 task 里带着 app_id/form_id。**新增流程写接口时两边都要照顾到。**
WORKFLOW_PATH = re.compile(r"^/v\d+/workflow/")

# 通讯录写接口（/v5|v6/corp/...）的 body 里**同样没有 app_id/entry_id**。
# 这和流程接口是同一个结构性缺口，而上面那条豁免当初只照顾了流程——于是设了
# JDY_WRITE_ALLOWLIST 的账号上，check_writable 拿着 (None, None) 把**所有**
# 通讯录写入无条件拒掉：jdy-org 整个技能在最该用它的场合完全不能用。
# 它有自己的闸门 JDY_ORG_WRITE，同样安在 post() 这个唯一出口上——
# 这样绕开 apply.py 直接 client.post() 也拦得住（原来那道闸只在 apply.py 里）。
# **再有"不带表单信息的写接口"进来时，两道闸都要照顾到。**
CORP_PATH = re.compile(r"^/v\d+/corp/")
ORG_WRITE_ENV = "JDY_ORG_WRITE"


def write_allowlist():
    """只允许写入这些表单（entry_id 或 app_id，逗号分隔）。空 = 不限制。

    给两种人用：
      · 把 Agent 放在有真实业务数据的账号上的人——限定它只能动指定的表；
      · 做验证的人——所有写入实验都圈在一张废弃表里。
    后者是有代价换来的：一次规模闸门的验证里，因为没有这道闸，
    180 条测试数据被直接写进了业务表。
    """
    raw = os.environ.get(WRITE_ALLOWLIST_ENV, "")
    return {t.strip() for t in raw.replace(";", ",").split(",") if t.strip()}


def check_writable(app_id, entry_id):
    """写入前的白名单校验。不在名单里就抛，别等写完才发现。"""
    allowed = write_allowlist()
    if not allowed:
        return
    if entry_id in allowed or app_id in allowed:
        return
    raise TargetError(
        "%s 限定了可写入的目标，而 %s/%s 不在名单里——已拒绝写入。\n"
        "名单：%s\n"
        "要放开就改这个环境变量；要临时关闭就把它清空。"
        % (WRITE_ALLOWLIST_ENV, app_id, entry_id, "、".join(sorted(allowed))))


def check_workflow_writable(tasks):
    """流程批量操作前的白名单校验，逐条按待办自带的 app_id/form_id 查。

    流程写接口的 body 里没有表单信息，post() 那道统一关卡对它是瞎的，
    所以这道必须由调用方在发请求前显式调用。
    """
    for t in tasks:
        check_writable(t.get("app_id"), t.get("form_id"))


def is_data_id(text):
    """是不是一个合法的记录 ID（24 位十六进制）。"""
    return bool(_DATA_ID.match(str(text or "")))


def dwidth(text):
    """字符串在等宽终端里占几列。中日韩字符占两列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in str(text))


def col_width(texts, minimum=0):
    """一列的对齐宽度。**必须用它，不要写 max(len(...))**——
    len() 数的是字符数，中文占两个显示列，算出来的宽度对不齐。
    同一个坑我栽过两次（--list 的表单清单、jdy-clean 的填充率表）。
    """
    return max([dwidth(t) for t in texts] + [minimum])


def pad(text, width, right=False):
    """按**显示宽度**补空格，而不是按字符数。

    `"%-30s" % "客户档案"` 补的是 26 个空格（4 个字符），但这四个字占 8 列，
    于是这一行比「线索」那行宽 4 列——整张表是歪的。实测中 Agent 扫这种
    歪表时把行读串了，把 A 表的行数安到了 B 表头上。列宽必须按显示宽度算。
    """
    s = str(text)
    gap = " " * max(0, width - dwidth(s))
    return gap + s if right else s + gap


class TargetError(JdyError):
    """按名字定位应用/表单失败。不是 API 返回的错误，但挂在同一个异常族下，
    调用方现有的 `except JdyError` 无需改动。"""

    def __init__(self, msg):
        Exception.__init__(self, msg)
        self.code = "TARGET"
        self.msg = msg
        self.http_status = None
        self.path = None


class OrgWriteRefused(TargetError):
    """没开 JDY_ORG_WRITE 就写通讯录。

    挂在 TargetError 下，cli_main 会把它当"目标不对"原样打给用户，
    而不是套上"简道云接口报错"——这不是接口报的错，是我们自己拦的。
    """


def check_org_write():
    """通讯录写入的独立闸门。

    为什么不复用 JDY_WRITE_ALLOWLIST：那个按 app/entry 限定可写的**表单**，
    而通讯录接口的 body 里没有 app_id/entry_id，那道闸对它是瞎的。
    与其让一道看起来生效、实际不生效的闸门给人虚假的安全感，不如另设一道。
    """
    if os.environ.get(ORG_WRITE_ENV, "").strip() not in ("1", "true", "yes", "on"):
        raise OrgWriteRefused(
            "改通讯录要先显式开启：export %s=1\n"
            "这不是多此一举——通讯录动的是整个企业的组织架构，"
            "而 JDY_WRITE_ALLOWLIST 那道闸按表单限定，对通讯录接口是瞎的。"
            % ORG_WRITE_ENV)


class AmbiguousName(TargetError):
    """名字对上了多个目标——不猜，把候选摆出来让人选。"""

    def __init__(self, kind, name, candidates):
        self.kind, self.name, self.candidates = kind, name, candidates
        lines = ["「%s」对应到 %d 个%s，请改用 ID 指定：" % (name, len(candidates), kind)]
        w = max(dwidth(c["name"]) for c in candidates)
        lines += ["    %s  %s" % (pad(c["name"], w), c["id"]) for c in candidates]
        TargetError.__init__(self, "\n".join(lines))


def _pick(items, wanted, kind):
    """按名字选一个：精确 > 去空白精确 > 唯一包含。多个候选就抛出让人选。"""
    if _DATA_ID.match(str(wanted or "")):
        return str(wanted)                      # 已经是 ID，原样放行
    want = str(wanted or "").strip()
    if not want:
        raise TargetError("没有给%s" % kind)
    norm = lambda t: "".join(str(t).split()).lower()
    for pred in (lambda c: c["name"] == want,
                 lambda c: norm(c["name"]) == norm(want),
                 lambda c: norm(want) in norm(c["name"])):
        hits = [c for c in items if pred(c)]
        if len(hits) == 1:
            return hits[0]["id"]
        if len(hits) > 1:
            raise AmbiguousName(kind, want, hits)
    raise TargetError("找不到叫「%s」的%s。现有：%s"
                      % (want, kind, "、".join(c["name"] for c in items[:15])))


def resolve_app(client, wanted):
    """应用名或 app_id → app_id。Agent 手上往往只有名字，别逼它去猜 ID。"""
    return _pick([{"name": a["name"], "id": a["app_id"]} for a in client.list_apps()],
                 wanted, "应用")


def resolve_entry(client, app_id, wanted):
    """表单名或 entry_id → entry_id。"""
    return _pick([{"name": f["name"], "id": f["entry_id"]}
                  for f in client.list_forms(app_id)], wanted, "表单")


def describe_targets(client, app_id=None, with_counts=True, limit=300):
    """列出可操作的目标：不给 app_id 就列应用，给了就列该应用的表单。

    存在的理由很实际：脚本都要 app_id / entry_id，而这恰恰是用户和 Agent
    最不知道的东西。没有一个正经的发现入口，Agent 就会去翻缓存目录、
    或自己写脚本猜——实测中它因此漏掉了一张明明存在的表。

    行数只用来帮人挑表，所以：
      · 只投影**一个**字段。整行拉下来纯属浪费——实测 33 列的表，
        投影后同样两行从 3418 字节降到 486 字节。
        （注意 `fields: []` 和 `fields: ["_id"]` 都会被接口**静默忽略**，
         必须给一个真实的控件标识，见 references/api-endpoints.md）
      · 数到 limit 就停，并标记 capped——原来在 300 处封顶却不说，
        一张 5 万行的表和一张 300 行的表在清单里长得一模一样，
        看的人据此判断规模就判断错了。
    """
    if not app_id:
        return [{"kind": "app", "name": a["name"], "id": a["app_id"]}
                for a in client.list_apps()]
    out = []
    for f in client.list_forms(app_id):
        item = {"kind": "form", "name": f["name"], "id": f["entry_id"]}
        if with_counts:
            try:
                widgets = [w for w in client.widgets(app_id, f["entry_id"])
                           if w.get("type") != "subform"]
                fields = [widgets[0]["name"]] if widgets else None
                rows = client.fetch_all(app_id, f["entry_id"], limit=limit, fields=fields)
                item["rows"] = len(rows)
                item["capped"] = len(rows) >= limit
            except JdyError:
                item["rows"] = None
        out.append(item)
    return out


def print_targets(items, header):
    print(header)
    if not items:
        print("  （空）")
        return
    w = max(dwidth(it["name"]) for it in items)
    for it in items:
        rows = ""
        if it.get("rows") is not None:
            # 封顶时标 "300+"：不标的话，5 万行的表和 300 行的表看着一样大
            rows = "  %s 行" % pad("%d%s" % (it["rows"], "+" if it.get("capped") else ""),
                                   5, right=True)
        elif "rows" in it:
            rows = "  行数未知"
        print("  %s  %s%s" % (pad(it["name"], w), it["id"], rows))


def display_value(value, wtype):
    """把 API 返回值压成人能看懂的单值。

    导出、分组、报表都要用同一套口径——三处各写一遍迟早会不一致。
    """
    if value is None or value == "" or value == [] or value == {}:
        return None
    if wtype in ("user", "dept"):
        return value.get("name") if isinstance(value, dict) else str(value)
    if wtype in ("usergroup", "deptgroup"):
        return "、".join(v.get("name", "") for v in value if isinstance(v, dict))
    if wtype == "address" and isinstance(value, dict):
        return "".join(filter(None, [value.get("province"), value.get("city"),
                                     value.get("district"), value.get("detail")]))
    if wtype == "linkobject" and isinstance(value, dict):
        return value.get("name") or value.get("link_id")
    if wtype == "linkdata" and isinstance(value, dict):
        return value.get("id")                       # 只有 ID，没有显示值
    if wtype in ("image", "upload") and isinstance(value, list):
        # 附件 URL 带 e= 过期戳（实测约 15 天），只给文件名
        return " | ".join(f.get("name", "") for f in value)
    if wtype in ("checkboxgroup", "combocheck") and isinstance(value, list):
        return "、".join(str(v) for v in value)
    if wtype == "subform" and isinstance(value, list):
        return "%d 行子表单" % len(value)
    if wtype == "phone" and isinstance(value, dict):
        return value.get("phone")                    # {"verified": bool, "phone": "…"}
    if isinstance(value, dict) and "name" in value:
        # 通用形状兜底：简道云的结构化控件几乎都带一个 name（签名、公海池、
        # 线索池、销售阶段…）。按形状认而不是逐个枚举控件类型——
        # 平台加新控件时不用改代码，也不会把某个业务模板写死进通用引擎。
        return value.get("name")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


# --------------------------------------------------------------------------
# 附件：取凭证 → 上传 → 把 key 写进控件（2026-08-31 实测打通）
# --------------------------------------------------------------------------

ATTACHMENT_TYPES = {"image", "upload"}


def _multipart(fields, filename, content, mime):
    """手搓 multipart/form-data。本项目不引第三方依赖，所以没有 requests 可用。

    **file 必须是最后一个字段**——官方文档明写的要求。
    """
    boundary = "----jdy%032x" % random.getrandbits(128)
    out = []
    for key, value in fields.items():
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                    % (boundary, key, value)).encode("utf-8"))
    out.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                'Content-Type: %s\r\n\r\n' % (boundary, filename, mime)).encode("utf-8"))
    out.append(content)
    out.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(out), "multipart/form-data; boundary=%s" % boundary


def new_transaction_id():
    """事务 ID。附件必须和写入请求共用同一个，否则接口不认这个文件。"""
    return "jdy-%032x" % random.getrandbits(128)


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

class JdyClient:
    def __init__(self, api_key=None, config_path=None, cache_dir=None,
                 timeout=30, max_retries=4, user_agent="jdy-skills/0.1"):
        # 密钥配置由 platform_env 按候选顺序找（$JDY_HOME 优先，再 ~/.jdy）。
        # 显式传路径仍然照办——调用方指定了就不该被搜索覆盖。
        self.config_path = config_path or platform_env.find_config()
        self.api_key = api_key or self._load_key(self.config_path)
        if not self.api_key:
            raise JdyError("NO_KEY",
                           "未找到密钥：请设置环境变量 %s 或写入 %s" % (ENV_KEY, self.config_path))
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        # 状态目录运行时探测。cache_dir 为 None = 一个能写的地方都没有，
        # 此时**只用内存缓存**，而不是拿 None 去拼路径炸在半路。
        self.state_home = platform_env.resolve_state_home()
        self.cache_dir = cache_dir or (
            os.path.join(self.state_home.path, "cache") if self.state_home.ok else None)
        self._buckets = {}
        self._bucket_lock = threading.Lock()
        self._widget_cache = {}

    @staticmethod
    def _load_key(config_path):
        key = os.environ.get(ENV_KEY)
        if key:
            return key.strip()
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            return (cfg.get("api_key") or cfg.get("apiKey") or "").strip() or None
        except Exception:
            return None

    def _bucket(self, path):
        with self._bucket_lock:
            if path not in self._buckets:
                self._buckets[path] = TokenBucket(ENDPOINT_RATE.get(path, DEFAULT_RATE))
            return self._buckets[path]

    @staticmethod
    def _url(path):
        """带版本前缀的路径原样拼到 /api 下，否则默认 v5。"""
        return API_ROOT + path if path.startswith("/v") else API_BASE + path

    def post(self, path, body):
        """带限流与退避重试的 POST。业务错误抛 JdyError。

        path 可带版本前缀（"/v6/workflow/task/list"）或不带（"/app/list"，默认 v5）。
        """
        if WRITE_PATH.search(path):
            # **闸门只看 path，不看 body 长什么样。** 原来整段挂在
            # `isinstance(body, dict)` 下面：body 传成列表，刚焊死的通讯录闸门
            # 连同表单白名单一起整体绕过去——而"默认拒绝"的闸门被一个形状判断
            # 连坐掉，是最糟的那种失效，它悄无声息。
            if CORP_PATH.match(path):
                check_org_write()
            elif not WORKFLOW_PATH.match(path):
                if not isinstance(body, dict):
                    raise TargetError(
                        "写请求的 body 不是对象（%s），无从判断要写哪张表——已拒绝。\n"
                        "%s 靠 body 里的 app_id/entry_id 把关，拿不到就不放行。"
                        % (type(body).__name__, WRITE_ALLOWLIST_ENV))
                check_writable(body.get("app_id"), body.get("entry_id"))
        self._bucket(path).take()
        payload = json.dumps(body).encode("utf-8")
        attempt = 0
        while True:
            req = urllib.request.Request(
                self._url(path), data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + self.api_key,
                         "User-Agent": self.user_agent},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    parsed = json.loads(resp.read().decode("utf-8"))
                # 流程写接口失败时返回 HTTP 200 + {"status":"failure"}，
                # 只看 HTTP 码会把失败当成功
                if isinstance(parsed, dict) and parsed.get("status") == "failure":
                    raise JdyError(parsed.get("code"), parsed.get("message", "操作失败"),
                                   200, path)
                return parsed
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = {}
                code = parsed.get("code")
                retryable = exc.code in RETRY_CODES or code == RATE_LIMIT_CODE
                if retryable and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(30.0, (2 ** attempt) * 0.5 + random.random() * 0.3))
                    self._bucket(path).take()
                    continue
                raise JdyError(code or exc.code, parsed.get("msg", raw[:200]), exc.code, path)
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(30.0, (2 ** attempt) * 0.5))
                    continue
                raise JdyError("NETWORK", "连不上 %s：%s" % (self._url(path), exc.reason),
                               None, path)

    # ---- 元数据 ----------------------------------------------------------

    def list_apps(self):
        """游标翻页，照 list_forms 的写法。

        原来固定 limit 100 不翻页：第 101 个应用在任何技能里都不存在——
        `--list` 看不到、按名字也解析不出来，而且报的是"找不到这个应用"，
        像是名字写错了。
        """
        out, skip = [], 0
        while True:
            page = self.post("/app/list", {"limit": 100, "skip": skip}).get("apps", [])
            out.extend(page)
            if len(page) < 100:
                return out
            skip += 100

    def list_forms(self, app_id):
        out, skip = [], 0
        while True:
            page = self.post("/app/entry/list",
                             {"app_id": app_id, "limit": 100, "skip": skip}).get("forms", [])
            out.extend(page)
            if len(page) < 100:
                return out
            skip += 100

    def widgets(self, app_id, entry_id, refresh=False):
        """字段结构，带本地 JSON 缓存。子表单的 items 一并缓存。"""
        ck = "%s.%s" % (app_id, entry_id)
        if not refresh and ck in self._widget_cache:
            return self._widget_cache[ck]
        # cache_dir 为 None：这台机器上一个可写目录都没找到，只走内存缓存。
        cache_file = os.path.join(self.cache_dir, "widgets_%s.json" % ck) if self.cache_dir else None
        if cache_file and not refresh and os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                if time.time() - cached.get("_fetched_at", 0) < 86400:
                    self._widget_cache[ck] = cached["widgets"]
                    return cached["widgets"]
            except Exception:
                pass
        ws = self.post("/app/entry/widget/list",
                       {"app_id": app_id, "entry_id": entry_id}).get("widgets", [])
        self._widget_cache[ck] = ws
        if not cache_file:
            return ws
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump({"_fetched_at": time.time(), "widgets": ws}, fh, ensure_ascii=False)
        except OSError:
            pass                                    # 沙箱不可写就纯内存缓存，不该因此失败
        return ws

    def field_map(self, app_id, entry_id, refresh=False):
        """返回 (by_label, by_name)。映射必须走 label ↔ name，name 是 _widget_ 固定 ID。"""
        ws = self.widgets(app_id, entry_id, refresh=refresh)
        return ({w["label"]: w for w in ws}, {w["name"]: w for w in ws})

    def field_map_including(self, app_id, entry_id, labels):
        """要用到 labels 里这些字段——**少一个就刷新一次缓存再看**。

        字段结构在本地缓存 24 小时。而"刚在界面上加了个字段，马上让 Agent 用它"
        恰恰是最常见的场景（关系迁移就是为它写的）：缓存里没有那个字段，
        工具会一口咬定"这张表没有这一列"，人对着界面上明明有的字段干瞪眼。
        缓存是为了省请求，不该省出一个假答案。
        """
        by_label, by_name = self.field_map(app_id, entry_id)
        want = [l for l in labels if l]
        if want and any(l not in by_label for l in want):
            by_label, by_name = self.field_map(app_id, entry_id, refresh=True)
        return by_label, by_name

    # ---- 读 --------------------------------------------------------------

    def iter_data(self, app_id, entry_id, fields=None, data_filter=None,
                  page_size=100, limit=None, progress=None):
        """data_id 游标全量拉取。简道云没有 offset 分页，只能游标。"""
        cursor, fetched = None, 0
        while True:
            body = {"app_id": app_id, "entry_id": entry_id, "limit": min(page_size, MAX_BATCH)}
            if cursor:
                body["data_id"] = cursor
            if fields:
                body["fields"] = fields
            if data_filter:
                body["filter"] = data_filter
            page = self.post("/app/entry/data/list", body).get("data", [])
            if not page:
                return
            for row in page:
                yield row
                fetched += 1
                if limit and fetched >= limit:
                    return
            if progress:
                progress(fetched)
            if len(page) < body["limit"]:
                return
            cursor = page[-1]["_id"]

    def fetch_all(self, app_id, entry_id, **kwargs):
        return list(self.iter_data(app_id, entry_id, **kwargs))

    def get_row(self, app_id, entry_id, data_id):
        """按 data_id 取一条；不存在返回 None。"""
        try:
            row = self.post("/app/entry/data/get",
                            {"app_id": app_id, "entry_id": entry_id,
                             "data_id": data_id}).get("data")
        except JdyError:
            return None
        return row or None

    def fetch_rows_by_id(self, app_id, entry_id, ids):
        """按 data_id 取一批记录。代价跟着 len(ids) 走，**不跟着表的大小走**。

        为什么不能用 filter 的 `in`：实测 `_id` 进 filter DSL **被静默忽略**——
        接口照常 200 返回，但返回的是整表的前 N 条，不是你要的那几条
        （2026-08-31 实测，见 references/api-endpoints.md）。
        拿它做回读核对，等于把别人的行当成自己写的行来核对，而且"通过"。

        策略：先顺着扫，扫到「再扫下去就比逐条 get 还贵」时切成逐条 get。
        代价因此不超过两种办法里较优者的两倍，且两头都有上界——
        原来是无条件全表扫：50 万行的表核对 300 条，要拉 5000 页。
        """
        wanted = {str(i) for i in ids if i}
        if not wanted:
            return {}
        found, seen, budget = {}, 0, max(1, len(wanted)) * MAX_BATCH
        scanned_whole_table = True
        for row in self.iter_data(app_id, entry_id, page_size=MAX_BATCH):
            seen += 1
            if row["_id"] in wanted:
                found[row["_id"]] = row
                if len(found) == len(wanted):
                    return found
            if seen >= budget:
                scanned_whole_table = False
                break
        if scanned_whole_table:
            return found                    # 整表扫完了，没找到的就是真不存在
        for did in sorted(wanted - set(found)):
            row = self.get_row(app_id, entry_id, did)
            if row is not None:
                found[did] = row
        return found

    def estimate_seconds(self, row_count, path="/app/entry/data/list", page_size=100):
        """大表操作先报预估耗时——限流下这个数字可能很难看，用户有权提前知道。"""
        rate = ENDPOINT_RATE.get(path, DEFAULT_RATE)
        return (row_count / float(page_size)) / float(rate)

    def backup(self, app_id, entry_id, out_path):
        """写操作前的强制备份。返回落地行数。"""
        rows = self.fetch_all(app_id, entry_id)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({"app_id": app_id, "entry_id": entry_id,
                       "exported_at": datetime.now(timezone.utc).isoformat(),
                       "count": len(rows), "data": rows}, fh, ensure_ascii=False, indent=2)
        return len(rows)

    # ---- 附件 ------------------------------------------------------------

    def upload_tokens(self, app_id, entry_id, transaction_id, need=1):
        """取文件上传凭证。一次返回 100 组 {url, token}（官方固定）。"""
        resp = self.post("/app/entry/file/get_upload_token",
                         {"app_id": app_id, "entry_id": entry_id,
                          "transaction_id": transaction_id})
        pairs = resp.get("token_and_url_list") or []
        if len(pairs) < need:
            raise JdyError("NO_TOKEN", "只拿到 %d 个上传凭证，不够 %d 个文件用"
                           % (len(pairs), need))
        return pairs

    def upload_file(self, url, token, path, filename=None, mime=None):
        """把一个本地文件传到凭证给出的地址，返回 key。

        一个 token 只能传一个文件，不允许覆盖（官方明示）。
        """
        with open(path, "rb") as fh:
            content = fh.read()
        filename = filename or os.path.basename(path)
        mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body, ctype = _multipart({"token": token}, filename, content, mime)
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise JdyError("UPLOAD", "上传 %s 失败：%s"
                           % (filename, exc.read().decode("utf-8", "replace")[:160]),
                           exc.code, url)
        except urllib.error.URLError as exc:
            raise JdyError("NETWORK", "连不上上传地址：%s" % exc.reason, None, url)
        key = parsed.get("key")
        if not key:
            raise JdyError("UPLOAD", "上传响应里没有 key：%s" % json.dumps(parsed)[:160])
        return key

    def upload_files(self, app_id, entry_id, paths, transaction_id, names=None):
        """批量上传，返回 [key]。

        **这些 key 只能配合同一个 transaction_id 的写入请求使用**，
        且凭证与事务都只有 1 小时有效期（官方明示）——所以上传和写入要挨着做，
        别把 key 存起来隔天再用。

        names：显式指定每个文件传上去之后叫什么，不给就用本地文件名。
        搬运场景必须给——本地文件名可能已被 download_file 改过（见 copy_attachments）。
        """
        paths = list(paths)
        if not paths:
            return []
        names = list(names) if names else [None] * len(paths)
        if len(names) != len(paths):
            raise ValueError("names 有 %d 个但要传 %d 个文件——对不上就会张冠李戴"
                             % (len(names), len(paths)))
        pairs = self.upload_tokens(app_id, entry_id, transaction_id, need=len(paths))
        return [self.upload_file(p["url"], p["token"], path, filename=name)
                for p, path, name in zip(pairs, paths, names)]

    def upload_pool(self, app_id, entry_id, transaction_id, need):
        """一次取够 need 个上传凭证，返回 [{url, token}]。

        取凭证是**一次请求返回 100 组**（官方固定）。原来每个单元格调一次
        upload_files，50 行就是 50 次请求、白取 5000 组只用 50 组——
        限流是 20/s，行数一多就自己把自己卡住。这里按需一次取够，
        调用方从池子里顺序领。
        """
        pool = []
        while len(pool) < need:
            got = self.upload_tokens(app_id, entry_id, transaction_id, need=1)
            if not got:
                raise JdyError("NO_TOKEN", "取不到上传凭证")
            pool.extend(got)                     # 一次就给 100 组，通常一轮够
        return pool[:need]

    def download_file(self, url, dest_dir, filename=None):
        """把一个附件下载到本地，返回落地路径。

        附件 url 带 `e=` 过期戳（实测约 15 天）——**导出的表里放 url 是没用的**，
        用户过两周点开全是死链。要么当场下载，要么只留文件名。

        同名文件不覆盖：加 `-2`、`-3` 后缀。附件的 name 是用户上传时的原名，
        一张表里重名太正常了，覆盖掉就等于悄悄丢文件。
        """
        filename = filename or (urllib.parse.unquote(
            urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]) or "attachment")
        filename = re.sub(r'[/\\:*?"<>|]', "_", filename).strip() or "attachment"
        base, ext = os.path.splitext(filename)
        path, i = os.path.join(dest_dir, filename), 1
        while os.path.exists(path):
            i += 1
            path = os.path.join(dest_dir, "%s-%d%s" % (base, i, ext))
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as resp:
                content = resp.read()
        except urllib.error.HTTPError as exc:
            raise JdyError("DOWNLOAD", "下载附件失败（url 可能已过期）：HTTP %s" % exc.code,
                           exc.code, url)
        except urllib.error.URLError as exc:
            raise JdyError("NETWORK", "连不上附件地址：%s" % exc.reason, None, url)
        os.makedirs(dest_dir, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def copy_attachments(self, values, app_id, entry_id, transaction_id, workdir=None):
        """把**读回来的**附件值搬到另一张表，返回可写入的 [key]。

        为什么不能直接搬值：读回来只有带过期戳的 url，写入要的是重新上传得到的
        key——附件是全项目唯一一个"搬运"必须真的过一遍本地磁盘的字段类型。

        workdir 不给就用临时目录（用完即弃）。

        **文件名要显式带过去**（2026-09-01 首次真机调用抓到）：一格里两个同名附件，
        download_file 为了不覆盖会把第二个存成 `x-2.jpg`，而上传默认拿本地文件名当
        文件名——于是搬过去的附件叫 `x-2.jpg`，和源端的 `x.jpg` 对不上。
        对同步是致命的：按文件名比对永远判「有差异」，每次重跑都重传一遍，幂等直接破了。
        下载半为防覆盖改了名，上传半却当它没改——本仓库那个「两半不对称」的老形状。
        """
        items = [v for v in (values or []) if isinstance(v, dict) and v.get("url")]
        if not items:
            return []
        tmp = workdir or tempfile.mkdtemp(prefix="jdy-attach-")
        paths = [self.download_file(v["url"], tmp, v.get("name")) for v in items]
        return self.upload_files(app_id, entry_id, paths, transaction_id,
                                 names=[v.get("name") for v in items])

    # ---- 写 --------------------------------------------------------------

    def batch_create(self, app_id, entry_id, rows, dry_run=True, verify=True,
                     tz=None, progress=None, transaction_id=None):
        """分块幂等写入 + 回读比对。

        rows: [{显示名: 值}]。dry_run=True（默认）只做编码与预检，不发写请求。

        为什么必须 verify：简道云对脏值静默存 null 并返回 success，
        响应里没有失败行的概念。不回读就等于交付脏数据。
        """
        by_label, _ = self.field_map(app_id, entry_id)
        encoded, submitted_rows, skipped_report, empty_rows = [], [], [], []
        for idx, row in enumerate(rows):
            data, skipped = encode_row(by_label, row, tz=tz)
            if skipped:
                skipped_report.append({"row": idx, "skipped": skipped})
            if not data:
                # 一个字段都没编出来（整行空，或所有列都被跳过）。空 data 会被 3005 拒绝，
                # 必须**在这里**剔除而不是提交前再滤：留到那时，提交序列比 encoded 短，
                # 回读比对与调用方的 ID 映射就整体错位——第 5 条的值挂到第 4 条名下，
                # 而且两边都"成功"，没有任何迹象。
                empty_rows.append(idx)
                continue
            encoded.append(data)
            submitted_rows.append(idx)

        report = {
            "total_rows": len(rows),
            "chunks": (len(encoded) + MAX_BATCH - 1) // MAX_BATCH,
            "skipped": skipped_report,
            # 和 update() 返回的 skipped 同一个形状，好让调用方两条路径用同一段代码。
            # 教训：三处 update 路径都记得报"未提交的字段"了，两处 batch_create 分支
            # 却忘了——因为一个是现成的列表，另一个埋在嵌套结构里，长得不像同一件事。
            "not_submitted": [dict(item, row=entry["row"])
                              for entry in skipped_report for item in entry["skipped"]],
            "empty_rows": empty_rows,          # 一个可写字段都没有、未提交的行号（rows 的下标）
            "submitted_rows": submitted_rows,  # 实际提交的行号，与 created_ids 同序
            "unwritable_columns": sorted({
                item["column"].split(".")[0] for entry in skipped_report
                for item in entry["skipped"]
                if item["kind"] in ("unwritable", "system_generated")}),
            "bad_value_columns": sorted({
                item["column"] for entry in skipped_report
                for item in entry["skipped"] if item["kind"] == "bad_value"}),
            "unknown_columns": sorted({
                item["column"] for entry in skipped_report
                for item in entry["skipped"] if item["kind"] == "unknown_column"}),
            "estimated_seconds": round(self.estimate_seconds(
                len(encoded), "/app/entry/data/batch_create", MAX_BATCH), 1),
            "dry_run": dry_run,
            "created_ids": [],
            "verification": None,
        }
        if dry_run or not encoded:
            return report

        # transaction_id 曾经被当成死参数删掉过——它当时确实没人用，
        # 但它不是多余，是**功能还没做**：附件上传的凭证绑定在 transaction_id 上，
        # 只有同一个 transaction_id 的写入请求才能用那些文件。
        #
        # 而分块时每块必须用**不同**的事务号（相同事务号会互相覆盖，见
        # write-behavior.md 三），于是带附件就只能一批装得下。这条限制说出来，
        # 不要默默按块拆——那会让后面几块的附件全部失效，而接口照样回报成功。
        if transaction_id and len(encoded) > MAX_BATCH:
            raise ValueError(
                "带附件的写入一次最多 %d 行：附件绑定在一个 transaction_id 上，"
                "而分块时每块要用不同的事务号（相同的会互相覆盖）。"
                "请把行数拆到 %d 以内分次调用，每次自己取一次上传凭证。"
                % (MAX_BATCH, MAX_BATCH))
        prefix = "jdy-%d-%04d" % (int(time.time()), random.randint(0, 9999))
        # 对齐是**按块**判断的：某一块部分成功，只让那一块失去核对资格，
        # 别把已经对得上的块也一起放弃——200 行里第二块少认了 60 条，
        # 不该连带让第一块那 100 条也变成"无法核对"。
        aligned_ids, aligned_data, unverifiable = [], [], 0
        for i in range(0, len(encoded), MAX_BATCH):
            chunk = encoded[i:i + MAX_BATCH]        # 空行已在编码阶段剔除，这里不再过滤
            resp = self.post("/app/entry/data/batch_create", {
                "app_id": app_id, "entry_id": entry_id, "data_list": chunk,
                "transaction_id": transaction_id or "%s-%d" % (prefix, i // MAX_BATCH)})
            # 覆盖场景下 success_ids 为空但 success_count 非零，两者要分开看
            ids = resp.get("success_ids", [])
            report["created_ids"].extend(ids)
            report.setdefault("success_count", 0)
            report["success_count"] += resp.get("success_count", 0)
            if len(ids) == len(chunk):
                aligned_ids.extend(ids)
                aligned_data.extend(chunk)
            else:
                unverifiable += len(chunk)          # 这一块谁对谁不可知
            if progress:
                progress(min(i + MAX_BATCH, len(encoded)), len(encoded))

        if verify and aligned_ids:
            report["verification"] = self.verify_written(
                app_id, entry_id, aligned_ids, aligned_data)
        elif verify and unverifiable:
            report["verification"] = {
                "checked": 0, "missing_rows": [], "silently_dropped": [],
                "aligned": False, "clean": False,
                "reason": "%d 行所在的批次返回的 ID 数与提交数对不上，"
                          "无法确定对应关系——本次未做逐字段核对" % unverifiable}
        if report.get("verification") is not None and unverifiable:
            report["verification"]["unverified_rows"] = unverifiable
            report["verification"]["clean"] = False   # 有没核对的行就不算干净
        return report

    def update(self, app_id, entry_id, data_id, row, tz=None, verify=True,
               transaction_id=None):
        """按显示名更新单条记录。返回 (ok, skipped, mismatches)。

        实测要点：更新是**局部**的——未提及的字段不会被清空，这正是同步需要的；
        但脏值的处理和新增一样**静默丢弃**（`2026/08/27` → null），
        所以同样必须回读核对。
        """
        by_label, by_name = self.field_map(app_id, entry_id)
        data, skipped = encode_row(by_label, row, tz=tz)
        if not data:
            return False, skipped, []
        body = {"app_id": app_id, "entry_id": entry_id, "data_id": data_id, "data": data}
        if transaction_id:
            body["transaction_id"] = transaction_id      # 附件必须与上传时同号
        self.post("/app/entry/data/update", body)
        mismatches = []
        if verify:
            back = self.post("/app/entry/data/get",
                             {"app_id": app_id, "entry_id": entry_id,
                              "data_id": data_id}).get("data", {})
            for name, wrapped in data.items():
                if self._is_empty(back.get(name)):
                    widget = by_name.get(name, {})
                    mismatches.append({
                        "field": widget.get("label", name), "type": widget.get("type"),
                        "submitted": wrapped.get("value"),
                        "reason": "提交了值但写入后为空——被简道云静默丢弃"})
        return True, skipped, mismatches

    def verify_written(self, app_id, entry_id, created_ids, submitted):
        """回读比对：把实际入库值和提交值逐字段对照，找出静默丢失的字段。

        created_ids 与 submitted 必须**同序等长**。对不上就说明接口只认了一部分
        （分块内部分成功），此时哪个 ID 对应哪一行是不可知的——宁可如实说"核对不了"，
        也不能按前缀硬对，那样报出来的"丢失字段"会指向错的行。
        """
        if len(created_ids) != len(submitted):
            return {"checked": 0, "missing_rows": [], "silently_dropped": [],
                    "aligned": False, "clean": False,
                    "reason": "接口返回 %d 个 ID，但提交了 %d 行——无法确定对应关系，"
                              "本次未做逐字段核对，请在简道云界面抽查"
                              % (len(created_ids), len(submitted))}
        wanted = set(created_ids)
        stored = self.fetch_rows_by_id(app_id, entry_id, wanted)
        _, by_name = self.field_map(app_id, entry_id)
        mismatches, missing = [], sorted(wanted - set(stored))
        for did, data in zip(created_ids, submitted):
            row = stored.get(did)
            if row is None:
                continue
            for name, wrapped in data.items():
                widget = by_name.get(name, {})
                actual = row.get(name)
                if self._is_empty(actual):
                    mismatches.append({
                        "data_id": did, "field": widget.get("label", name),
                        "type": widget.get("type"), "submitted": wrapped.get("value"),
                        "stored": actual, "reason": "提交了值但写入后为空——被简道云静默丢弃"})
        return {"checked": len(created_ids), "missing_rows": missing,
                "silently_dropped": mismatches, "aligned": True,
                "clean": not mismatches and not missing}

    @staticmethod
    def _is_empty(value):
        return value is None or value == "" or value == [] or value == {}
