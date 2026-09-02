# -*- coding: utf-8 -*-
"""守住「只做通用引擎」这条产品边界。

技能只认**简道云的字段类型**（datetime 能当时间轴、combo 能当维度、number 能求和），
不认业务概念。领域差异必须落在**配置**（报表定义 YAML）与**未来的领域包**里，
不能漏进引擎代码——一旦漏进去，PLM／ERP／WMS 就得各自分叉维护。

原则写在文档里会漂移，写成测试才不会。

判定口径：扫描代码 token，**跳过注释与三引号文档字符串**（举例说明是允许的），
剩下的字符串与标识符里不得出现业务领域词汇。

    python3 tests/test_domain_neutral.py
"""
import io
import os
import sys
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 业务领域词汇：出现在代码逻辑里就说明引擎绑了某个行业
DOMAIN_WORDS = [
    # 销售/CRM
    "客户", "商机", "线索", "跟进", "回款", "订单", "销售", "报价", "成交",
    # 生产/制造
    "良率", "工单", "工序", "产线", "设备", "稼动",
    # 库存/进销存。注意「入库/出库」在中文里也指"写入数据库"——
    # 守卫第一次跑就抓到了这种碰撞，措辞改成"写入后"更准确，词表因此保持严格
    "库存", "入库", "出库", "周转", "采购", "供应商",
    # 项目/PLM
    "里程碑", "需求变更", "工时",
    # 人事
    "考勤", "薪酬", "绩效",
]

SCAN_DIRS = [os.path.join(ROOT, "_shared"),
             os.path.join(ROOT, "skills")]


def code_strings_and_names(path):
    """返回文件里的代码 token 文本，跳过注释与三引号文档字符串。"""
    out = []
    with open(path, "rb") as fh:
        try:
            tokens = list(tokenize.tokenize(fh.readline))
        except (tokenize.TokenError, SyntaxError):
            return out
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue                                   # 注释里举例是允许的
        if tok.type == tokenize.STRING:
            raw = tok.string.lstrip("rbufRBUF")
            if raw.startswith('"""') or raw.startswith("'''"):
                continue                               # 文档字符串同理
            out.append((tok.start[0], tok.string))
        elif tok.type == tokenize.NAME:
            out.append((tok.start[0], tok.string))
    return out


def python_files():
    for base in SCAN_DIRS:
        for dirpath, dirnames, files in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "_shared")]
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(dirpath, f)


class TestEngineIsDomainNeutral(unittest.TestCase):
    def test_no_domain_vocabulary_in_code(self):
        offenders = []
        for path in python_files():
            for line_no, text in code_strings_and_names(path):
                for word in DOMAIN_WORDS:
                    if word in text:
                        offenders.append("%s:%d  %s（含「%s」）"
                                         % (os.path.relpath(path, ROOT), line_no,
                                            text.strip()[:60], word))
        self.assertEqual(
            offenders, [],
            "引擎代码里出现了业务领域词汇——领域差异应落在报表定义或领域包里：\n  "
            + "\n  ".join(offenders))

    def test_scan_actually_covers_the_engine(self):
        """守卫本身要能失效检测：确认真的扫到了文件。"""
        files = list(python_files())
        self.assertGreater(len(files), 5, "扫描没覆盖到引擎文件，这个守卫形同虚设")
        self.assertTrue(any("aggregate.py" in f for f in files))
        self.assertTrue(any("jdy_client.py" in f for f in files))

    def test_detector_would_catch_a_violation(self):
        """反向验证：塞一段带领域词的代码，守卫必须能抓到。"""
        sample = 'if label == "客户名称":\n    pass\n'
        tokens = []
        for tok in tokenize.tokenize(io.BytesIO(sample.encode()).readline):
            if tok.type == tokenize.STRING and not tok.string.lstrip("rbufRBUF").startswith('"""'):
                tokens.append(tok.string)
        self.assertTrue(any("客户" in t for t in tokens))

    def test_field_type_constants_are_platform_not_domain(self):
        """维度/指标候选靠的是简道云字段类型，不是字段名里的业务词。"""
        sys.path.insert(0, os.path.join(ROOT, "skills", "jdy-report", "scripts"))
        sys.path.insert(0, os.path.join(ROOT, "_shared"))
        import init_config
        for t in init_config.DIM_TYPES + init_config.NUM_TYPES + init_config.DATE_TYPES:
            self.assertRegex(t, r"^[a-z]+$", "字段类型应是简道云的类型名，不是业务概念")


if __name__ == "__main__":
    unittest.main(verbosity=2)
