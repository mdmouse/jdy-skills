#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开仓库里不许出现真实 ID、本机路径、真手机号、密钥。

脱敏做一次一定会漏，做成闸门才不会回来——这个文件就是那道闸门。

**范围**：`PRIVATE_ONLY` 列出的路径不进公开仓（真机探针要真账号的 ID 才跑得动），
所以扫描跳过它们。这份名单同时也是打包时该排除什么的唯一事实来源——
写在两个地方就一定会不一致。

占位符约定：
  * 24 位十六进制 ID → `deadbeefdeadbeefdeadNNNN`（合法 ObjectId 形状，一眼是假的）
  * 本机 home 路径   → `/Users/<you>`
  * 手机号           → 工信部测试号段 138xxxx / 139xxxx（见 TEST_PHONES）

不依赖 pytest：`python3 tests/test_no_secrets.py`
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 不进公开仓的路径（前缀匹配）。改这里 = 改打包排除名单。
PRIVATE_ONLY = (
    "tests/real/",              # 真机探针：需要真账号的 app/entry ID 才有意义
    # 开发过程日志：排查叙事、审查修复清单、实验实录。已脱敏，但那是给自己看的
    # 过程记录，不是给用户看的文档——公开仓里只留 dev-notes / 兼容性表 / 观察哨。
    "docs/review-fixlist.md",
    "docs/dual-track-experiment.md",
    "docs/gui-test-findings.md",
    "docs/e2e-baselines.md",
    "docs/v1-v4-playbook.md",
    "docs/corrections/",
)

# 就地豁免。测试夹具里难免要写出"长得像真的"的值：在**它出现的那一行**
# 写上这个标记，该值在这个文件里就整体放过（同一个夹具常出现好几行，
# 逐行标记只会把测试改得满目疮痍）。
#
# **豁免只能写在现场，不能集中成一张名单**：名单会静静地长胖，
# 而写在值旁边的标记，下一个读到这行的人一眼就知道它为什么被放过。
PRAGMA = "脱敏例外"

PLACEHOLDER_ID = re.compile(r"\Adeadbeefdeadbeefdead\d{4}\Z")
OBJECT_ID = re.compile(r"\b[0-9a-f]{24}\b")
HOME_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\\\?Users\\\\?)([A-Za-z0-9._-]+)")
# 占位用户名。单字母的（/Users/x）在本仓库的文档与测试里就是占位符。
HOME_OK = {"<you>", "<user>", "you", "user", "runner", "root", "u", "x"}
# 工信部留给测试/文档的号段——这些出现在示例里是正常的
TEST_PHONES = {"13800138000", "13800000000", "13900009999", "13222222222"}
PHONE = re.compile(r"\b1[3-9]\d{9}\b")
# 简道云 API Key 是 64 位。长 base64/hex 字面量出现在 key/secret/token 附近就报。
SECRETISH = re.compile(
    r"(?i)(api[_-]?key|secret|token|password)\W{0,4}[\"']([A-Za-z0-9+/=_-]{32,})[\"']")


def tracked_public_files():
    """已跟踪 + 未跟踪但没被 gitignore 的文件。

    **`--others --exclude-standard` 不能省。** 只扫已跟踪的话，新文件要等到
    提交之后才第一次被检查——而"新加的文件"恰恰是最可能夹带东西的那类。
    这个闸门自己就栽过：它作为未跟踪文件加进来时扫不到自己，
    直到被提交才发现自己的检测样本没打豁免标记。
    """
    r = subprocess.run(["git", "ls-files", "-z", "--cached",
                        "--others", "--exclude-standard"], cwd=ROOT,
                       capture_output=True)
    # git 不在、或这儿根本不是仓库时，stdout 是空的——**那时绝不能返回空列表**。
    # 空列表会让下面的扫描一个文件都不看，然后报 OK：
    # 「没检查所以是绿的」和「查过了没问题」在报告里长得一模一样，含义正相反。
    # 导出公开仓时就撞见过：git init 之前跑这套测试，全绿，其实什么都没扫。
    if r.returncode != 0:
        raise RuntimeError(
            "git ls-files 失败（%s），闸门无法判断扫描范围：%s"
            % (r.returncode, r.stderr.decode("utf-8", "replace").strip()[:200]))
    out = []
    for path in r.stdout.decode("utf-8").split("\0"):
        if not path or any(path.startswith(p) for p in PRIVATE_ONLY):
            continue
        out.append(path)
    if not out:
        raise RuntimeError("闸门一个文件都没扫到——这不是「干净」，是没检查。")
    return out


LINK = re.compile(r"\]\(([^)#: ]+)(?:#[^)]*)?\)")


def private_links(files):
    """公开文件里指向 PRIVATE_ONLY 路径的 Markdown 相对链接。

    这些文件在私有仓里存在、在公开仓里不存在——链接在这边全绿、到那边全断，
    而断链是导出之后才看得见的。所以在这边就拦。
    """
    bad = []
    for path in files:
        if not path.endswith(".md"):
            continue
        base = os.path.dirname(path)
        for m in LINK.finditer(read(path)):
            target = os.path.normpath(os.path.join(base, m.group(1)))
            if any(target == p.rstrip("/") or target.startswith(p)
                   for p in PRIVATE_ONLY):
                bad.append("%s -> %s" % (path, m.group(1)))
    return bad


def read(path):
    """读一个文件的文本内容。二进制文件与已删除的路径返回 None。

    两条都是踩出来的：

    * **已删除但还在索引里的文件**——`git ls-files --cached` 照样列它，
      open 直接 FileNotFoundError，整个闸门报 error。删掉的文件泄不了密，跳过。
    * **二进制文件**——用 errors="ignore" 把 PNG 当文本读会得到八万多个乱码字符，
      迟早随机撞出一个 24 位十六进制，然后闸门指着一张图片说"这里有真实 ID"。
      按内容判定（前 8KB 有没有 NUL），不按扩展名——扩展名会漏。
    """
    full = os.path.join(ROOT, path)
    try:
        with open(full, "rb") as fh:
            head = fh.read(8192)
    except (OSError, IOError):
        return None
    if b"\x00" in head:
        return None
    try:
        with open(full, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except (OSError, IOError):
        return None


def scan(text):
    """返回这段文本里的问题清单。测试和自检共用同一套判定。

    带 PRAGMA 标记的行整行跳过，且该行上出现过的值在本文件内一律豁免。
    """
    kept, exempt = [], set()
    for line in text.splitlines():
        if PRAGMA in line:
            # 这一行声明的所有"像真的"的值，本文件内都放过
            exempt.update(OBJECT_ID.findall(line))
            exempt.update(PHONE.findall(line))
            exempt.update(v for _l, v in SECRETISH.findall(line))
            exempt.update(HOME_PATH.findall(line))
        else:
            kept.append(line)
    text = "\n".join(kept)
    hits = []
    for oid in sorted(set(OBJECT_ID.findall(text))):
        if oid in exempt: continue
        if not PLACEHOLDER_ID.match(oid):
            hits.append("真实 ID：%s" % oid)
    for who in sorted(set(HOME_PATH.findall(text))):
        if who in exempt: continue
        if who not in HOME_OK:
            hits.append("本机路径：.../%s" % who)
    for num in sorted(set(PHONE.findall(text))):
        if num in exempt: continue
        if num not in TEST_PHONES:
            hits.append("疑似真手机号：%s" % num)
    for _label, value in SECRETISH.findall(text):
        if value in exempt: continue
        hits.append("疑似密钥字面量：%s…（%d 位）" % (value[:8], len(value)))
    return hits


class DetectorIsSensitive(unittest.TestCase):
    """先证明这套检测真的看得见东西——否则下面全是空过。"""

    def test_catches_each_kind(self):
        cases = {
            "真实 ID": "app_id=6a8d7b931859df5dc7eef063",  # 脱敏例外：检测样本
            "本机路径": "/Users/mdmouse/.jdy/config.json",  # 脱敏例外：检测样本
            "疑似真手机号": "联系电话 13512345678",  # 脱敏例外：检测样本
            "疑似密钥字面量": 'api_key = "NTlxAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAc3bA"',  # 脱敏例外：检测样本
        }
        for kind, sample in cases.items():
            hits = scan(sample)
            self.assertTrue(any(h.startswith(kind) for h in hits),
                            "检测器漏了「%s」：%r -> %s" % (kind, sample, hits))

    def test_pragma_exempts_only_its_own_line(self):
        """豁免标记不能变成一关关全部——只放过写了标记的那一行。"""
        marked = "phone = '13512345678'  # %s：造的\nid=6a8d7b931859df5dc7eef063" % PRAGMA  # 脱敏例外：检测样本
        hits = scan(marked)
        self.assertFalse(any("手机号" in h for h in hits), "标记那行没被放过：%s" % hits)
        self.assertTrue(any("真实 ID" in h for h in hits), "标记把下一行也放过了：%s" % hits)

    def test_does_not_flag_the_placeholders(self):
        clean = ("id=deadbeefdeadbeefdead0001 路径 /Users/<you>/.jdy "
                 "电话 13800138000")
        self.assertEqual([], scan(clean), "占位符被误报了")


class PublicTreeIsClean(unittest.TestCase):

    def test_no_secrets_in_tracked_public_files(self):
        problems = []
        for path in tracked_public_files():
            text = read(path)
            if text is None:          # 二进制或已删除
                continue
            for hit in scan(text):
                problems.append("%s → %s" % (path, hit))
        self.assertEqual([], problems,
                         "公开仓里发现 %d 处未脱敏内容：\n  %s"
                         % (len(problems), "\n  ".join(problems)))

    def test_the_gate_actually_scanned_something(self):
        """扫描范围不能是空的。

        空范围下每一条断言都自动通过——那是最坏的一种绿。
        取个保守下限：这个仓库怎么也不止 50 个文件。
        """
        files = tracked_public_files()
        self.assertGreater(len(files), 50,
                           "闸门只扫到 %d 个文件，范围不对" % len(files))

    def test_deleted_and_binary_files_do_not_break_the_gate(self):
        """闸门不能被"索引里还在、磁盘上没了"或一张 PNG 打崩。

        前者在这次改图标时真的发生过：rm 掉两个已跟踪的图片还没 git add，
        闸门就 FileNotFoundError 整个报错。
        """
        import tempfile
        d = tempfile.mkdtemp()
        binp = os.path.join(d, "b.bin")
        with open(binp, "wb") as fh:
            fh.write(b"\x89PNG\x00\x00" + b"6a8d7b931859df5dc7eef063".ljust(200, b"\x00"))
        # 直接把绝对路径喂给 read()：它内部是 os.path.join(ROOT, path)，
        # join 遇到绝对路径会原样返回，所以这条路走得通。
        # **不能再用 os.path.relpath(binp, ROOT)**：Windows 的 runner 上 temp 在 C:、
        # 仓库在 D:，跨盘符没有相对路径可言，relpath 直接
        # `ValueError: path is on mount 'C:', start on mount 'D:'`——
        # 测试自己先炸了，闸门有没有被打崩根本没测到。
        self.assertIsNone(read(binp),
                          "带 NUL 的文件应当被当成二进制跳过")
        self.assertIsNone(read("这个路径根本不存在.md"),
                          "不存在的路径应当返回 None 而不是抛异常")

    def test_text_files_are_still_read(self):
        """跳过二进制不能顺手把文本也跳了——那就又是"没检查所以是绿的"。"""
        text = read("README.md")
        self.assertIsNotNone(text, "README.md 被误判成二进制了")
        self.assertIn("JDY-SKILLS", text)

    def test_public_files_do_not_link_to_private_ones(self):
        """公开文件不得链接到 PRIVATE_ONLY 路径——那种链接导出后必断。"""
        bad = private_links(tracked_public_files())
        self.assertEqual([], bad,
                         "这些链接指向不进公开仓的文件：\n  %s" % "\n  ".join(bad))

    def test_private_list_actually_matches_something(self):
        """PRIVATE_ONLY 写错路径的话，闸门会安静地把整棵树都扫进来。

        **公开仓里它一条都匹配不上是正常的**——那些路径本来就没被导出去。
        所以判据是"要么全中，要么全不中"：
          * 全中 → 私有开发仓，正常；
          * 全不中 → 公开导出仓，PRIVATE_ONLY 已经生效，跳过；
          * 一部分中一部分不中 → 没中的那条多半是打错了，红。
        """
        raw = [p for p in subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True).stdout.decode("utf-8").split("\0") if p]
        hit = [prefix for prefix in PRIVATE_ONLY
               if any(p.startswith(prefix) for p in raw)]
        if not hit:
            self.skipTest("一条 PRIVATE_ONLY 都没匹配上——这是公开导出仓，"
                          "那些路径本来就不该在")
        missed = [prefix for prefix in PRIVATE_ONLY if prefix not in hit]
        self.assertEqual([], missed,
                         "PRIVATE_ONLY 里这些路径一个文件都不匹配，多半是打错了："
                         "%s（其余 %s 是匹配上的）" % (missed, hit))


if __name__ == "__main__":
    unittest.main(verbosity=2)
