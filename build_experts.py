#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 experts/ 下的专家包打成腾讯 WorkBuddy 开放平台「专家」渠道要的 zip。

专家包用的是 CodeBuddy 插件格式：plugin.json 放在 `.codebuddy-plugin/` 里，
其余目录（agents/、avatars/、skills/、README.md）都在包根。

**为什么要有这个脚本，而不是手工 zip：**

  1. `skills/` 不入库。专家声明它装哪几个技能，技能目录由这里从仓库的
     `skills/` 拷进去——库里存两份技能一定会分叉，而分叉在打包那一刻
     完全看不出来（zip 里两份看着都像真的）。
  2. 拷之前先跑一遍 vendor。技能包被单独复制走时 `_shared/` 不跟着走，
     内核必须已经 vendor 进 `scripts/_shared/`；漏了这一步，
     专家装上去之后第一次跑脚本才 ImportError。
  3. **zip 的根就是包根，不套目录。** 开放平台按 `.codebuddy-plugin/plugin.json`
     认包，多套一层目录它就报「压缩包缺少 .codebuddy-plugin/plugin.json 文件」。
     这个错只在上传那一刻出现，本地解压看着一切正常。

    python3 build_experts.py                  # 校验 + 构建到 dist/experts
    python3 build_experts.py --check          # 只校验 experts/，不产出任何文件
    python3 build_experts.py --out DIR        # 换个产物目录
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import zipfile


def _force_utf8_stdio():
    """把 stdout/stderr 钉成 UTF-8——理由见 skills/*/scripts/_bootstrap.py。"""
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_stdio()

ROOT = os.path.dirname(os.path.abspath(__file__))
EXPERTS = os.path.join(ROOT, "experts")
SKILLS = os.path.join(ROOT, "skills")

# 官方给的行业分类枚举。写错一个字，上传时才会被打回。
CATEGORY_IDS = {
    "01-ProductDesign", "02-Engineering", "03-GameSpatial", "04-DataAI",
    "05-MarketingGrowth", "06-ContentCreative", "07-SalesCommerce",
    "08-FinanceInvestment", "09-OperationsHR", "10-ProjectQuality",
    "11-SecurityCompliance", "12-IndustryConsultant", "13-TencentZone",
    "14-WorldWise", "15-Education",
}
KEBAB = re.compile(r"\A[a-z0-9][a-z0-9-]*[a-z0-9]\Z")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
SEMVER = re.compile(r"\A\d+\.\d+\.\d+\Z")

# 展示描述的中文字数是**硬区间**，不是建议：官方规范写死 40–50 字。
ZH_DESC_MIN, ZH_DESC_MAX = 40, 50
AVATAR_MAX_BYTES = 500 * 1024
AVATAR_SIDE = 512

# 系统提示词里必须写到的几处约束。少一处就意味着这个专家在别人的端上
# 可能去干我们明确说过不干的事——而那是装上之后才会发生的。
REQUIRED_PROMPT_MARKS = ("非官方", "dry-run", "--execute", "setup.py")

EXCLUDE_DIRS = {"__pycache__", ".git"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", ".gitkeep"}


def expert_names():
    if not os.path.isdir(EXPERTS):
        return []
    return sorted(d for d in os.listdir(EXPERTS)
                  if os.path.isdir(os.path.join(EXPERTS, d))
                  and not d.startswith("."))


def plugin_path(name):
    return os.path.join(EXPERTS, name, ".codebuddy-plugin", "plugin.json")


def load_plugin(name):
    with open(plugin_path(name), encoding="utf-8") as fh:
        return json.load(fh)


def png_size(path):
    """只用标准库读 PNG 的宽高。返回 (w, h)，不是 PNG 就返回 None。"""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def _i18n(obj, field, where, errors):
    """{en, zh} 双语字段：两边都得有，且都不能是空串。"""
    val = obj.get(field)
    if not isinstance(val, dict):
        errors.append("%s：%s 必须是 {en, zh} 对象" % (where, field))
        return None
    for lang in ("en", "zh"):
        if not str(val.get(lang) or "").strip():
            errors.append("%s：%s.%s 为空" % (where, field, lang))
    return val


def validate(name):
    """校验一个专家包，返回问题清单（空列表 = 通过）。

    这份判据和 tests/test_experts.py 是同一份——测试导入这里，而不是
    另抄一遍。抄一遍的下场是两边慢慢分叉，然后打包通过、上传被拒。
    """
    errors = []
    d = os.path.join(EXPERTS, name)
    pj = plugin_path(name)
    if not os.path.isfile(pj):
        return ["%s：缺少 .codebuddy-plugin/plugin.json" % name]
    try:
        p = load_plugin(name)
    except (ValueError, OSError) as exc:
        return ["%s：plugin.json 解析失败：%s" % (name, exc)]

    w = name          # 报错前缀

    # —— 基础字段 ——
    for field in ("name", "version", "description"):
        if not str(p.get(field) or "").strip():
            errors.append("%s：plugin.json 缺少必填字段 %s" % (w, field))
    if p.get("name") != name:
        errors.append("%s：plugin.json name=%r 与目录名不一致" % (w, p.get("name")))
    if p.get("name") and not KEBAB.match(str(p["name"])):
        errors.append("%s：name=%r 不是 kebab-case" % (w, p["name"]))
    if p.get("version") and not SEMVER.match(str(p["version"])):
        errors.append("%s：version=%r 不是语义化版本 x.y.z" % (w, p["version"]))
    # description 是**英文**简短描述——官方规范这么写的，中文会被打回
    if re.search(r"[一-鿿]", str(p.get("description") or "")):
        errors.append("%s：description 必须是英文，实际含中文" % w)
    author = p.get("author")
    if not isinstance(author, dict) or not author.get("name") or not author.get("email"):
        errors.append("%s：author 必须是 {name, email}" % w)

    if p.get("expertType") != "agent":
        errors.append("%s：expertType 必须是 'agent'，实际 %r" % (w, p.get("expertType")))
    if p.get("plugin") != p.get("name"):
        errors.append("%s：plugin=%r 必须与 name=%r 相同"
                      % (w, p.get("plugin"), p.get("name")))

    # —— agents 与 agentName ——
    agents = p.get("agents")
    if not isinstance(agents, list) or not agents:
        errors.append("%s：agents 必须是非空路径数组" % w)
        agents = []
    for rel in agents:
        target = os.path.join(d, str(rel)[2:] if str(rel).startswith("./") else str(rel))
        if not os.path.isfile(target):
            errors.append("%s：agents 里的 %s 不存在" % (w, rel))
    agent_name = p.get("agentName")
    if not agent_name:
        errors.append("%s：缺少 agentName" % w)
    else:
        md = os.path.join(d, "agents", "%s.md" % agent_name)
        if not os.path.isfile(md):
            errors.append("%s：agentName=%r 没有对应的 agents/%s.md"
                          % (w, agent_name, agent_name))

    # —— 展示字段 ——
    _i18n(p, "displayName", w, errors)
    _i18n(p, "profession", w, errors)
    dd = _i18n(p, "displayDescription", w, errors)
    dip = _i18n(p, "defaultInitPrompt", w, errors)
    if dd:
        zh = str(dd.get("zh") or "")
        if zh and not (ZH_DESC_MIN <= len(zh) <= ZH_DESC_MAX):
            errors.append("%s：displayDescription.zh 有 %d 字，官方要求 %d–%d 字"
                          % (w, len(zh), ZH_DESC_MIN, ZH_DESC_MAX))
    if p.get("categoryId") not in CATEGORY_IDS:
        errors.append("%s：categoryId=%r 不在官方枚举内" % (w, p.get("categoryId")))

    tags = p.get("tags")
    if not isinstance(tags, list) or len(tags) != 3:
        errors.append("%s：tags 必须**恰好 3 个**，实际 %s"
                      % (w, len(tags) if isinstance(tags, list) else "非数组"))
    else:
        for i, tag in enumerate(tags):
            if not isinstance(tag, dict) or not tag.get("en") or not tag.get("zh"):
                errors.append("%s：tags[%d] 必须是 {en, zh}" % (w, i))

    qp = p.get("quickPrompts")
    if not isinstance(qp, list) or len(qp) != 3:
        errors.append("%s：quickPrompts 必须**恰好 3 个**，实际 %s"
                      % (w, len(qp) if isinstance(qp, list) else "非数组"))
    else:
        for i, one in enumerate(qp):
            if not isinstance(one, dict) or not one.get("en") or not one.get("zh"):
                errors.append("%s：quickPrompts[%d] 必须是 {en, zh}" % (w, i))
        # defaultInitPrompt 与 quickPrompts[0] 必须一致——**两种语言都要**。
        # 只对上中文那半，市场上英文界面的首句和第一个推荐提示词会对不上。
        if dip and isinstance(qp[0], dict):
            for lang in ("zh", "en"):
                if dip.get(lang) != qp[0].get(lang):
                    errors.append(
                        "%s：defaultInitPrompt.%s 与 quickPrompts[0].%s 不一致"
                        % (w, lang, lang))

    # —— 头像 ——
    avatar = str(p.get("avatar") or "")
    if not avatar:
        errors.append("%s：缺少 avatar" % w)
    else:
        ap = os.path.join(d, avatar)
        if not os.path.isfile(ap):
            errors.append("%s：头像文件不存在：%s" % (w, avatar))
        else:
            size = png_size(ap)
            if size is None:
                errors.append("%s：头像不是 PNG：%s" % (w, avatar))
            elif size != (AVATAR_SIDE, AVATAR_SIDE):
                errors.append("%s：头像是 %dx%d，要求 %d×%d"
                              % (w, size[0], size[1], AVATAR_SIDE, AVATAR_SIDE))
            nbytes = os.path.getsize(ap)
            if nbytes > AVATAR_MAX_BYTES:
                errors.append("%s：头像 %.0f KB，超过 %d KB 上限"
                              % (w, nbytes / 1024.0, AVATAR_MAX_BYTES // 1024))

    # —— 声明的技能必须在仓库里真的存在 ——
    for rel in p.get("skills") or []:
        rel = str(rel)
        stem = rel[2:] if rel.startswith("./") else rel
        if not stem.startswith("skills/"):
            errors.append("%s：skills 路径要写成 ./skills/<技能名>，实际 %r" % (w, rel))
            continue
        skill = stem[len("skills/"):]
        if not os.path.isfile(os.path.join(SKILLS, skill, "SKILL.md")):
            errors.append("%s：声明的技能 %s 在仓库 skills/ 下不存在" % (w, skill))

    # —— 结构 ——
    for forbidden in ("hooks", "commands"):
        if os.path.exists(os.path.join(d, forbidden)):
            errors.append("%s：不许有 %s/ 目录" % (w, forbidden))
    for sub in ("agents", "skills", "avatars", "bin"):
        if os.path.exists(os.path.join(d, ".codebuddy-plugin", sub)):
            errors.append("%s：%s/ 必须在包根，不能放进 .codebuddy-plugin/" % (w, sub))
    if not os.path.isfile(os.path.join(d, "README.md")):
        errors.append("%s：缺少 README.md" % w)

    errors += validate_agent_mds(name)
    return errors


def validate_agent_mds(name):
    """agents/ 下每个 md 的 frontmatter 与系统提示词。"""
    errors = []
    d = os.path.join(EXPERTS, name, "agents")
    if not os.path.isdir(d):
        return ["%s：缺少 agents/ 目录" % name]
    mds = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    if not mds:
        errors.append("%s：agents/ 下没有 md" % name)
    for f in mds:
        w = "%s/agents/%s" % (name, f)
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            text = fh.read()
        m = FRONTMATTER.match(text)
        if not m:
            errors.append("%s：开头必须是 --- 包裹的 YAML frontmatter" % w)
            continue
        fm, body = m.group(1), text[m.end():]
        # **开发者不可自行添加 tools**：工具权限由平台统一分配。
        if re.search(r"^tools:", fm, re.M):
            errors.append("%s：frontmatter 不许出现 tools 字段" % w)
        for key in ("name", "description", "displayName", "profession"):
            if not re.search(r"^%s:" % key, fm, re.M):
                errors.append("%s：frontmatter 缺少 %s" % (w, key))
        mname = re.search(r'^name:\s*"?([^"\n]+?)"?\s*$', fm, re.M)
        if mname and mname.group(1) != f[:-3]:
            errors.append("%s：frontmatter name=%r 与文件名不一致"
                          % (w, mname.group(1)))
        mdesc = re.search(r'^description:\s*"?(.+?)"?\s*$', fm, re.M)
        if mdesc and re.search(r"[一-鿿]", mdesc.group(1)):
            errors.append("%s：description 必须是英文（AI 用它判断何时激活）" % w)
        for mark in REQUIRED_PROMPT_MARKS:
            if mark not in body:
                errors.append("%s：系统提示词里必须写到「%s」" % (w, mark))
    return errors


def stage(name, out_dir):
    """把一个专家包摊到 <out_dir>/<name>/，返回那个目录。"""
    src = os.path.join(EXPERTS, name)
    dst = os.path.join(out_dir, name)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)

    def ignore(_dirpath, entries):
        return [e for e in entries if e in EXCLUDE_DIRS or e in EXCLUDE_FILES]

    shutil.copytree(os.path.join(src, ".codebuddy-plugin"),
                    os.path.join(dst, ".codebuddy-plugin"), ignore=ignore)
    for sub in ("agents", "avatars"):
        shutil.copytree(os.path.join(src, sub), os.path.join(dst, sub), ignore=ignore)
    shutil.copy2(os.path.join(src, "README.md"), os.path.join(dst, "README.md"))

    # skills/ 不入库：从仓库的 skills/ 拷进来，保证两边不会分叉
    for rel in load_plugin(name).get("skills") or []:
        rel = str(rel)
        skill = (rel[2:] if rel.startswith("./") else rel)[len("skills/"):]
        shutil.copytree(os.path.join(SKILLS, skill),
                        os.path.join(dst, "skills", skill), ignore=ignore)
    return dst


def zip_package(staged, out_zip, prefix=""):
    """打 zip。默认**根就是包内容，不套一层目录**；`prefix="<name>/"` 时套一层。

    两种布局都出，原因是两边证据相反：开放平台上传曾报「压缩包缺少
    .codebuddy-plugin/plugin.json 文件」，而 WorkBuddy 自带的官方
    `package_expert.py` 打出来的 zip 却是套着 `<name>/` 的。哪种被收以实际
    上传为准——`<name>.zip` 不套，`<name>-wrapped.zip` 套。
    """
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, files in os.walk(staged):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for f in sorted(files):
                if f in EXCLUDE_FILES or f.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, f)
                zf.write(full, prefix + os.path.relpath(full, staged).replace(os.sep, "/"))
    return out_zip


def build(out_dir, names):
    os.makedirs(out_dir, exist_ok=True)
    sums = []
    for name in names:
        staged = stage(name, out_dir)
        for fname, prefix in (("%s.zip" % name, ""), ("%s-wrapped.zip" % name, name + "/")):
            zp = os.path.join(out_dir, fname)
            zip_package(staged, zp, prefix)
            digest = hashlib.sha256(open(zp, "rb").read()).hexdigest()
            sums.append("%s  %s" % (digest, fname))
            print("  %s（%.1f KB）" % (zp, os.path.getsize(zp) / 1024.0))
    # newline="\n" 不能省：Windows 上默认写出 CRLF，`shasum -c` 会把 \r
    # 当成文件名的一部分，于是每一行都报 No such file。理由同 build.py。
    with open(os.path.join(out_dir, "SHA256SUMS"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(sums) + "\n")
    print("已写 %s/SHA256SUMS（%d 个专家包）" % (out_dir, len(sums)))
    print("核对：cd %s && shasum -a 256 -c SHA256SUMS" % out_dir)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="把 experts/ 下的专家包打成 WorkBuddy 开放平台要的 zip")
    ap.add_argument("names", nargs="*", help="专家名，缺省为全部")
    ap.add_argument("--out", metavar="DIR", default=os.path.join("dist", "experts"),
                    help="产物目录，默认 dist/experts")
    ap.add_argument("--check", action="store_true",
                    help="只校验 experts/ 下的专家包，不产出任何文件")
    args = ap.parse_args()

    names = args.names or expert_names()
    if not names:
        print("experts/ 下没有专家包")
        return 1
    unknown = [n for n in names if n not in expert_names()]
    if unknown:
        print("没有这些专家：%s" % "、".join(unknown))
        return 1

    problems = []
    for name in names:
        problems += validate(name)
    if problems:
        print("FAIL（%d 项）" % len(problems))
        for p in problems:
            print("  - " + p)
        return 1
    print("OK — %d 个专家包校验通过：%s" % (len(names), "、".join(names)))
    if args.check:
        return 0

    # 技能目录要带着 vendor 好的内核一起进包，否则装上之后第一次跑就 ImportError
    sys.path.insert(0, ROOT)
    import build as build_skills
    if build_skills.vendor(check=False) != 0:
        return 1

    print("构建 %d 个专家包到 %s/" % (len(names), args.out))
    return build(args.out, names)


if __name__ == "__main__":
    sys.exit(main())
