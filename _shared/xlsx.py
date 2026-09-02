# -*- coding: utf-8 -*-
"""最小 xlsx 读写，仅用标准库。

为什么不用 openpyxl：三端 Agent 沙箱大概率禁止 pip 安装，带第三方依赖会把
"平台不支持"和"依赖装不上"两种失败混在一起。xlsx 本质是 zip + XML，
自己读写反而更可控。

支持范围（够 jdy-excel-bridge 用，不追求完备）：
  读：首个/指定工作表、共享字符串、内联字符串、公式结果、**日期序列号还原**
  写：单表、内联字符串、数字、日期写成文本（避免样式表复杂度）
不支持：合并单元格语义、图片、公式重算、多表联动。
"""
import datetime
import os
import re
import zipfile
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_Q = lambda tag: "{%s}%s" % (NS_MAIN, tag)

# Excel 内置的日期/时间数字格式 ID
BUILTIN_DATE_FMTS = set(range(14, 23)) | set(range(45, 48))
_DATE_CHARS = re.compile(r"[yYmMdDhHs]")
_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")
# Excel 的 1900 闰年 bug：它认为 1900 年是闰年，凭空多出 1900-02-29（序列号 60）。
# 于是序列号 60 之前和之后的基准差一天，必须分段换算。
# 锚点校验：1→1900-01-01、59→1900-02-28、61→1900-03-01、25569→1970-01-01。
_EPOCH = datetime.datetime(1899, 12, 30)          # 序列号 ≥ 60
_EPOCH_EARLY = datetime.datetime(1899, 12, 31)    # 序列号 < 60


class XlsxError(ValueError):
    pass


def _col_index(ref):
    """'AB12' → 27（0 基列号）。"""
    m = _CELL_REF.match(ref)
    if not m:
        raise XlsxError("无法解析单元格引用：%r" % ref)
    letters, idx = m.group(1), 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _col_letter(index):
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _serial_to_datetime(value):
    """Excel 日期序列号还原成 datetime。整数部分是天，小数部分是时间。

    序列号 60 是 Excel 虚构的 1900-02-29，现实中不存在；这里落到 1900-02-28，
    与主流表格工具的处理一致。
    """
    days = int(value)
    seconds = round((value - days) * 86400)
    base = _EPOCH_EARLY if days < 60 else _EPOCH
    return base + datetime.timedelta(days=days, seconds=seconds)


def _date_styles(zf):
    """解析 styles.xml，返回「样式索引 → 是否日期格式」。

    Excel 把日期存成数字，只有样式能区分 45000 是数字还是 2023-03-15。
    不解析样式就会把整列日期读成一堆五位数。
    """
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom = {}
    for numfmt in root.iter(_Q("numFmt")):
        code = numfmt.get("formatCode", "")
        # 去掉引号内的字面量再判断，避免 "mm" 这种文字被误判
        stripped = re.sub(r'"[^"]*"', "", code)
        custom[int(numfmt.get("numFmtId"))] = bool(_DATE_CHARS.search(stripped))
    date_style_ids = set()
    cell_xfs = root.find(_Q("cellXfs"))
    if cell_xfs is None:
        return date_style_ids
    for idx, xf in enumerate(cell_xfs.findall(_Q("xf"))):
        fmt_id = int(xf.get("numFmtId", 0))
        if fmt_id in BUILTIN_DATE_FMTS or custom.get(fmt_id):
            date_style_ids.add(idx)
    return date_style_ids


def _shared_strings(zf):
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root.findall(_Q("si")):
        out.append("".join(t.text or "" for t in si.iter(_Q("t"))))
    return out


def sheet_names(path):
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("xl/workbook.xml"))
        return [s.get("name") for s in root.iter(_Q("sheet"))]


def _sheet_path(zf, sheet):
    book = ET.fromstring(zf.read("xl/workbook.xml"))
    sheets = list(book.iter(_Q("sheet")))
    if not sheets:
        raise XlsxError("工作簿里没有工作表")
    target = sheets[0]
    if sheet is not None:
        for s in sheets:
            if s.get("name") == sheet:
                target = s
                break
        else:
            raise XlsxError("找不到工作表 %r，现有：%s"
                            % (sheet, "、".join(s.get("name") for s in sheets)))
    rid = target.get("{%s}id" % NS_REL_DOC)
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.iter("{%s}Relationship" % NS_PKG_REL):
        if rel.get("Id") == rid:
            t = rel.get("Target")
            return ("xl/" + t.lstrip("/")) if not t.startswith("xl/") else t
    raise XlsxError("解析不到工作表路径")


def read_rows(path, sheet=None):
    """返回 list[list]，**第 0 行是表头**（与 write_sheet 的 rows 形状对齐）。

    返回 [[cell, ...], ...]，空单元格补 None，日期还原成 datetime。"""
    if not os.path.exists(path):
        raise XlsxError("文件不存在：%s" % path)
    if not zipfile.is_zipfile(path):
        raise XlsxError("不是有效的 xlsx（可能是 .xls 老格式或 CSV 改了扩展名）：%s" % path)
    with zipfile.ZipFile(path) as zf:
        strings = _shared_strings(zf)
        date_styles = _date_styles(zf)
        root = ET.fromstring(zf.read(_sheet_path(zf, sheet)))
        rows = []
        for row in root.iter(_Q("row")):
            cells = {}
            for c in row.findall(_Q("c")):
                ref = c.get("r")
                col = _col_index(ref) if ref else len(cells)
                ctype = c.get("t")
                if ctype == "inlineStr":
                    is_el = c.find(_Q("is"))
                    value = "".join(t.text or "" for t in is_el.iter(_Q("t"))) if is_el is not None else None
                else:
                    v = c.find(_Q("v"))
                    raw = v.text if v is not None else None
                    if raw is None:
                        value = None
                    elif ctype == "s":
                        value = strings[int(raw)] if int(raw) < len(strings) else None
                    elif ctype == "b":
                        value = raw == "1"
                    elif ctype in ("str", "e"):
                        value = raw
                    else:
                        try:
                            num = float(raw)
                        except ValueError:
                            value = raw
                        else:
                            style = int(c.get("s", -1))
                            if style in date_styles and num > 0:
                                value = _serial_to_datetime(num)
                            else:
                                value = int(num) if num.is_integer() else num
                cells[col] = value
            rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
    return rows


def read_table(path, sheet=None, header_row=0):
    """返回**元组** (headers, list[dict])——不是 list[dict]，别直接当列表用。

    返回 (headers, [dict, ...])。表头去空白，重名列自动加后缀。"""
    rows = read_rows(path, sheet)
    if len(rows) <= header_row:
        raise XlsxError("表格为空或没有第 %d 行表头" % (header_row + 1))
    raw_headers = [("" if h is None else str(h).strip()) for h in rows[header_row]]
    headers, seen = [], {}
    for h in raw_headers:
        name = h or "未命名列"
        if name in seen:
            seen[name] += 1
            name = "%s_%d" % (name, seen[name])
        else:
            seen[name] = 0
        headers.append(name)
    out = []
    for row in rows[header_row + 1:]:
        if all(c is None or c == "" for c in row):
            continue                                    # 跳过全空行
        out.append({headers[i]: (row[i] if i < len(row) else None)
                    for i in range(len(headers))})
    return headers, out


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------

_CTYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_BOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""


def _escape(text):
    out = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    # XML 1.0 不允许的控制字符会让 Excel 直接拒绝打开文件
    return "".join(ch for ch in out if ch in "\t\n\r" or ord(ch) >= 32)


def _cell_xml(ref, value):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        value = "是" if value else "否"
    elif isinstance(value, (datetime.datetime, datetime.date)):
        value = value.isoformat()
    if isinstance(value, (int, float)):
        return '<c r="%s"><v>%s</v></c>' % (ref, value)
    return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (
        ref, _escape(str(value)))


def write_sheet(path, headers, rows, sheet_name="Sheet1"):
    """写单表 xlsx。rows 是 [dict] 或 [list]，按 headers 顺序取值。"""
    body = []
    body.append("<row r=\"1\">%s</row>" % "".join(
        _cell_xml("%s1" % _col_letter(i), h) for i, h in enumerate(headers)))
    for r, row in enumerate(rows, start=2):
        values = [row.get(h) for h in headers] if isinstance(row, dict) else list(row)
        body.append("<row r=\"%d\">%s</row>" % (r, "".join(
            _cell_xml("%s%d" % (_col_letter(i), r), v) for i, v in enumerate(values))))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (NS_MAIN, "".join(body)))
    book_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="%s" xmlns:r="%s"><sheets>'
        '<sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>'
        % (NS_MAIN, NS_REL_DOC, _escape(sheet_name)))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CTYPES)
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("xl/workbook.xml", book_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", _BOOK_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return path
