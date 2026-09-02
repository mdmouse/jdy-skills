# -*- coding: utf-8 -*-
"""xlsx 读写测试。

关键在于**不能只测自己写的文件读回来对不对**——那只证明自洽。
真正要验的是能否读懂 Excel/WPS 产出的结构：共享字符串表、日期序列号 + 样式、
稀疏单元格。所以这里手工拼出 Excel 风格的 xlsx 作为夹具。

    python3 tests/test_xlsx.py
"""
import datetime
import os
import sys
import tempfile
import shutil
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_shared"))

from xlsx import XlsxError, read_rows, read_table, sheet_names, write_sheet  # noqa: E402

CT = """<?xml version="1.0"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
</Types>"""
ROOT_RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
BOOK_RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
BOOK = """<?xml version="1.0"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets></workbook>"""
# 样式 0 = 常规，1 = 内置日期格式 14，2 = 自定义 yyyy-mm-dd，3 = 自定义 "件"（非日期）
STYLES = """<?xml version="1.0"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2">
  <numFmt numFmtId="176" formatCode="yyyy&quot;年&quot;mm&quot;月&quot;dd&quot;日&quot;"/>
  <numFmt numFmtId="177" formatCode="0&quot;md&quot;"/>
</numFmts>
<cellXfs count="4">
  <xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="176"/><xf numFmtId="177"/>
</cellXfs></styleSheet>"""
SHARED = """<?xml version="1.0"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="4" uniqueCount="4">
<si><t>客户名称</t></si><si><t>下单日期</t></si><si><t>数量</t></si><si><t>示例客户A</t></si>
</sst>"""
# B2=46261 内置日期格式（=2026-08-27）；C3 跳过 B 列（稀疏）；D 列自定义非日期格式必须仍读成数字
SHEET = """<?xml version="1.0"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c><c r="D1" t="inlineStr"><is><t>备注</t></is></c></row>
<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2" s="1"><v>46261</v></c><c r="C2"><v>12</v></c><c r="D2" s="3"><v>7</v></c></row>
<row r="3"><c r="A3" t="inlineStr"><is><t>内联客户</t></is></c><c r="C3" s="2"><v>46261.5</v></c></row>
<row r="4"/>
</sheetData></worksheet>"""


def make_excel_style_xlsx(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CT)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", BOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", BOOK_RELS)
        zf.writestr("xl/styles.xml", STYLES)
        zf.writestr("xl/sharedStrings.xml", SHARED)
        zf.writestr("xl/worksheets/sheet1.xml", SHEET)
    return path


class TestReadExcelStyle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = make_excel_style_xlsx(os.path.join(cls.tmp, "excel.xlsx"))

    def test_sheet_name(self):
        self.assertEqual(sheet_names(self.path), ["数据"])

    def test_shared_strings_resolved(self):
        rows = read_rows(self.path)
        self.assertEqual(rows[0][:3], ["客户名称", "下单日期", "数量"])
        self.assertEqual(rows[1][0], "示例客户A")

    def test_inline_string(self):
        rows = read_rows(self.path)
        self.assertEqual(rows[0][3], "备注")
        self.assertEqual(rows[2][0], "内联客户")

    def test_builtin_date_serial_becomes_datetime(self):
        """不解析样式的话，整列日期会读成 46266 这种五位数。"""
        rows = read_rows(self.path)
        self.assertIsInstance(rows[1][1], datetime.datetime)
        self.assertEqual(rows[1][1].date(), datetime.date(2026, 8, 27))

    def test_custom_date_format_detected(self):
        rows = read_rows(self.path)
        cell = rows[2][2]
        self.assertIsInstance(cell, datetime.datetime)
        self.assertEqual(cell.hour, 12)                       # .5 天 = 12:00

    def test_custom_nondate_format_stays_number(self):
        """自定义格式 0"md" 含 m 和 d，但都在引号里——不能误判成日期。"""
        rows = read_rows(self.path)
        self.assertEqual(rows[1][3], 7)

    def test_sparse_cells_padded(self):
        rows = read_rows(self.path)
        self.assertIsNone(rows[2][1])                          # B3 缺失

    def test_read_table_skips_blank_rows(self):
        headers, records = read_table(self.path)
        self.assertEqual(headers, ["客户名称", "下单日期", "数量", "备注"])
        self.assertEqual(len(records), 2)                      # 第 4 行全空，跳过
        self.assertEqual(records[0]["数量"], 12)


class TestSerialEpoch(unittest.TestCase):
    """Excel 1900 闰年 bug 的分段换算，用公认锚点钉死。"""

    def test_known_anchors(self):
        from xlsx import _serial_to_datetime as conv
        cases = [(1, (1900, 1, 1)), (59, (1900, 2, 28)), (61, (1900, 3, 1)),
                 (25569, (1970, 1, 1)), (45000, (2023, 3, 15)), (46261, (2026, 8, 27))]
        for serial, expect in cases:
            self.assertEqual(conv(serial).date(), datetime.date(*expect),
                             "序列号 %d 换算错误" % serial)

    def test_phantom_leap_day_does_not_crash(self):
        """序列号 60 是 Excel 虚构的 1900-02-29，不能让程序炸掉。"""
        from xlsx import _serial_to_datetime as conv
        self.assertEqual(conv(60).date(), datetime.date(1900, 2, 28))

    def test_fractional_day_is_time(self):
        from xlsx import _serial_to_datetime as conv
        self.assertEqual(conv(46261.75).hour, 18)


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "out.xlsx")

    def test_write_then_read(self):
        headers = ["名称", "数量", "日期"]
        rows = [{"名称": "甲", "数量": 3, "日期": datetime.date(2026, 8, 27)},
                {"名称": "乙", "数量": 4.5, "日期": None}]
        write_sheet(self.path, headers, rows)
        self.assertTrue(zipfile.is_zipfile(self.path))
        got_headers, records = read_table(self.path)
        self.assertEqual(got_headers, headers)
        self.assertEqual(records[0]["名称"], "甲")
        self.assertEqual(records[0]["数量"], 3)
        self.assertEqual(records[0]["日期"], "2026-08-27")
        self.assertEqual(records[1]["数量"], 4.5)

    def test_special_characters_escaped(self):
        write_sheet(self.path, ["列"], [{"列": '<a>&"引号" \x07控制符'}])
        _, records = read_table(self.path)
        self.assertEqual(records[0]["列"], '<a>&"引号" 控制符')

    def test_many_columns_use_correct_letters(self):
        headers = ["c%d" % i for i in range(30)]              # 跨过 Z 到 AD
        write_sheet(self.path, headers, [{h: i for i, h in enumerate(headers)}])
        got, records = read_table(self.path)
        self.assertEqual(got, headers)
        self.assertEqual(records[0]["c29"], 29)


class TestDuplicateHeaders(unittest.TestCase):
    def test_duplicate_headers_disambiguated(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "dup.xlsx")
        write_sheet(path, ["名称", "名称", "名称"], [])
        headers, _ = read_table(path)
        self.assertEqual(headers, ["名称", "名称_1", "名称_2"])


class TestErrors(unittest.TestCase):
    def test_non_xlsx_gives_clear_error(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "fake.xlsx")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("名称,数量\n甲,1\n")                       # CSV 改了扩展名
        with self.assertRaises(XlsxError) as ctx:
            read_rows(path)
        self.assertIn("CSV", str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(XlsxError):
            read_rows("/nonexistent/nope.xlsx")




class TestExcelStyleDateSerials(unittest.TestCase):
    """真 Excel 存的日期是**数值序列号 + 日期样式**，不是字符串。

    我们自己的写入器把日期写成 ISO 字符串，所以「写出去再读回来」永远测不到
    这条路——自洽而已。这里手工拼出 Excel 的产物（numFmtId=14 内置日期格式，
    单元格是纯数字）来验读取器。
    """

    NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

    def _build(self, path, serials):
        rows = "".join('<row r="%d"><c r="A%d" s="1"><v>%d</v></c></row>' % (i, i, s)
                       for i, s in enumerate(serials, start=2))
        sheet = ('<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>'
                 '<row r="1"><c r="A1" t="inlineStr"><is><t>日期</t></is></c></row>'
                 '%s</sheetData></worksheet>' % (self.NS, rows))
        styles = ('<?xml version="1.0"?><styleSheet xmlns="%s">'
                  '<cellXfs count="2"><xf numFmtId="0" xfId="0"/>'
                  '<xf numFmtId="14" applyNumberFormat="1" xfId="0"/></cellXfs>'
                  '</styleSheet>' % self.NS)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("[Content_Types].xml",
                       '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.'
                       'org/package/2006/content-types"><Default Extension="xml" '
                       'ContentType="application/xml"/><Default Extension="rels" '
                       'ContentType="application/vnd.openxmlformats-package.relationships'
                       '+xml"/></Types>')
            z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="%s">'
                       '<Relationship Id="rId1" Type="%s/officeDocument" '
                       'Target="xl/workbook.xml"/></Relationships>' % (self.PKG, self.REL))
            z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="%s" '
                       'xmlns:r="%s"><sheets><sheet name="Sheet1" sheetId="1" '
                       'r:id="rId1"/></sheets></workbook>' % (self.NS, self.REL))
            z.writestr("xl/_rels/workbook.xml.rels",
                       '<?xml version="1.0"?><Relationships xmlns="%s">'
                       '<Relationship Id="rId1" Type="%s/worksheet" '
                       'Target="worksheets/sheet1.xml"/><Relationship Id="rId2" '
                       'Type="%s/styles" Target="styles.xml"/></Relationships>'
                       % (self.PKG, self.REL, self.REL))
            z.writestr("xl/styles.xml", styles)
            z.writestr("xl/worksheets/sheet1.xml", sheet)

    def _read(self, serials):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "dates.xlsx")
        self._build(path, serials)
        _, recs = read_table(path)
        shutil.rmtree(tmp)
        return [r["日期"] for r in recs]

    def test_ordinary_date(self):
        # 46266 是 Excel 里的 2026-09-01
        self.assertEqual(self._read([46266])[0],
                         datetime.datetime(2026, 9, 1))

    def test_1900_leap_year_boundary(self):
        # Excel 认为 1900 是闰年（并不是）。序列号 60 是那个不存在的 2/29，
        # 所以 <60 与 >60 必须用不同的纪元，否则早期日期会整体差一天。
        got = self._read([59, 61])
        self.assertEqual(got[0], datetime.datetime(1900, 2, 28))
        self.assertEqual(got[1], datetime.datetime(1900, 3, 1))

    def test_plain_number_without_date_style_stays_number(self):
        # 没有日期样式的数字不能被当成日期——否则金额、数量会变成日期
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "n.xlsx")
        self._build(path, [])
        with zipfile.ZipFile(path, "a") as z:
            z.writestr("xl/worksheets/sheet1.xml",
                       '<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>'
                       '<row r="1"><c r="A1" t="inlineStr"><is><t>数量</t></is></c></row>'
                       '<row r="2"><c r="A2"><v>46266</v></c></row>'
                       '</sheetData></worksheet>' % self.NS)
        _, recs = read_table(path)
        shutil.rmtree(tmp)
        self.assertEqual(recs[0]["数量"], 46266)


if __name__ == "__main__":
    unittest.main(verbosity=2)
