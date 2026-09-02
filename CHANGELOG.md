# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

每个技能包在自己的 `SKILL.md` frontmatter 里带 `version`，
`dist/MANIFEST.txt` 记录每次发布的版本与校验和。

## [0.7.0] — 2026-09-01

**首个带版本号、可发布的版本。** 此前没有版本字段，用户装了 zip 也无从判断
手上是哪一版——豆包工作里躺着的那个 v0.1.0 探针就是这么来的。

### 新增

- **上腾讯 WorkBuddy 开放平台的两个渠道。**
  - **技能渠道**：11 个 `SKILL.md` 的 frontmatter 补齐上架必填字段
    `display_name` / `display_name_en` / `description_zh` / `description_en` /
    `category` / `author`。**`description` 一个字没动**——它是三端的触发依据，
    不是商店卡片文案，两者是两件东西。`tests/test_skill_format.py` 逐个校验
    六个字段存在非空、`description_zh` ≤ 60 字、`author` 恰好是 `aicliagent`。
  - **两种 zip 布局**：`build.py --dist DIR` 仍产出 `<name>.zip`（根是 `<name>/`，
    GitHub Release 与千问办公用）；新增 `--layout workbuddy` 产出
    `<name>-workbuddy.zip`（根是 `skills/<name>/`，WorkBuddy 技能渠道要的形状），
    `--layout both` 两种一起打，SHA256SUMS 与 MANIFEST.txt 把两种都收进去。
    **顶层目录差一级只在上传那一刻报错**，本地解压看着一模一样，
    所以 `tests/test_release.py` 对两种布局各断言一次包内路径。
  - **专家渠道**：新增 `experts/jdy-ops-expert`（运营／行政，`09-OperationsHR`，
    装 9 个技能）与 `experts/jdy-dev-expert`（集成开发，`02-Engineering`，
    装 4 个技能），CodeBuddy 插件格式。两个 Agent 的系统提示词里写死了
    非官方第三方身份与商标边界、**读可走官方「AI 连接」连接器／任何写入只走
    技能脚本**（明令禁止拿官方单条新增工具循环当批量导入——它静默存 null
    还返回成功）、写入先 dry-run 念计划、用户点头才 `--execute`、从不删除记录、
    没配 Key 引导跑 `hello-jdy` 的配置向导而不是让用户把 Key 贴进对话。
  - `build_experts.py`：校验 + 从 `skills/` 拷技能 + 打 zip + 写 SHA256SUMS。
    **技能目录不入库**（仓库里存两份一定会分叉），**zip 的根就是包根不套目录**
    （套了平台报「压缩包缺少 .codebuddy-plugin/plugin.json 文件」）。
    `tests/test_experts.py` 逐条断言官方规范，并做变异检查：中文展示描述改成
    39 字、tags 删成 2 个、`defaultInitPrompt` 与 `quickPrompts[0]` 不一致、
    frontmatter 里加 `tools`，四种改法各自必须红。
  - **暂不声明 `dependencies.connectors`**：简道云官方连接器的 ID 还不知道，
    猜错会让安装直接失败，比不声明更糟。两个专家的 README 里各留一行 TODO。
- **品牌署名。** 生成的文件（HTML 报告、周报 Markdown、数据字典、集成样例、
  修复建议表）末尾带一行 `aicliagent` 静态署名，`JDY_BRAND=0` 关闭；
  唯一来源 `_shared/brand.py`，`tests/test_brand.py` 守着每个落点。
  群机器人消息正文不带署名，`jdy-report/push.py` 推送前会把它摘掉。
- **Windows 上的三处真问题**（GitHub Actions 首次在 Windows 跑测试才暴露）：
  `NUL` 设备的 `isatty()` 返回 True，原来 `if isatty(): input()` 式的确认在
  Agent 非交互环境下会 EOFError 崩掉（退出码 1 而非约定的 4）——现在 10 处确认
  统一走内核 `ask_yes()`，问不了一律拒绝；push / nudge / watch 三处发送闸门原来在
  isatty 谎报时会直接放行，现在没 `--yes` 就必须真问一次。`build.py --dist` 写出的
  `SHA256SUMS` 在 Windows 上是 CRLF，`shasum -c` 认不出文件名——改为强制 LF。
  `install_root` 判文件系统根时比较 `os.sep`，Windows 的根是 `D:\`，装在盘符根下
  会把盘符当宿主目录——改判 `dirname(root) == root`。
- **Windows 支持。** 中文 Windows 控制台默认 GBK，打印 `✅` / `⬜` 会抛
  `UnicodeEncodeError` 直接把脚本崩掉。现在所有入口在任何输出之前把
  stdout/stderr 钉成 UTF-8：10 份 `_bootstrap.py` 覆盖走内核的脚本，
  `probe.py` / `install.py` / `build.py` 三个独立入口各自带一份。
- `install.py` 的路径展开改用 `_expand()`（`expanduser` 不认 `%VAR%`），
  为将来需要 `%APPDATA%` 的端留好机制；`tests/test_windows.py` 守着
  「不许再加只在 macOS 上成立的路径」。
- `build.py --dist DIR`：vendor → 打 zip → 写 `SHA256SUMS`（标准两列，
  可直接 `shasum -a 256 -c`）与 `MANIFEST.txt`（名字／版本／摘要）。
  发布只走这一条路。
- `tests/run_all.py`：跨平台的测试入口。
  （`python3 -m unittest discover` 在本仓库跑不了——tests/ 没有 `__init__.py`。）
- GitHub Actions CI：Ubuntu + **Windows** × Python 3.9/3.13，外加一个
  `PYTHONIOENCODING=gbk` 的中文控制台专用 job，和一个脱敏闸门 job。
- `SECURITY.md`：非官方第三方声明、数据流向、密钥处理、漏洞报告方式。
- 三套新测试：`test_windows.py`（编码与 Windows 路径）、
  `test_no_secrets.py`（脱敏闸门）、`test_release.py`（发布产物）。

### 变更

- **`hello-jdy` 改写成「简道云连接诊断」**，面向"装了却用不了"的用户：
  十项检查各自失败了怎么修，去掉了 Sprint 0 / V1–V4 / 回填兼容性表这些
  内部项目脚手架。给用户看的报告里不再打印内部记账行。
- **11 个技能的 description 首行改成一句人话**——那是商店卡片上显示的内容。
  触发词与对 Agent 的行为约束保留，移到后面。
- 更正"C8 需要企业版及以上 API Key"：试用账号实测可以调通，
  与 README 和兼容性验证表的结论统一。
- README 加上非官方第三方声明。

### 移除

- **豆包工作不再是安装目标。** 它的 `.skills` 由客户端按服务端清单同步，
  复制进去的技能会被清掉（实测 11 个在 14 分钟后消失）——往一个会静默清空的
  目录里装东西，比不提供这个选项更糟。`install.py` 里已无该条目，
  `tests/test_windows.py` 守着它不再回来。
  **宿主识别保留**：在那一端跑诊断会认出它并当面说明不支持，
  而不是含糊地报"未识别的宿主"。

### 安全

- 轮换了 2026-08-26 建的那把曾经明文外泄的全权限 Key。
- 全仓脱敏：真实 app/entry ID 换成 `deadbeefdeadbeefdeadNNNN`，
  本机路径换成 `/Users/<you>`，抄自真实记录的手机号邮箱换成测试号段。
  `tests/test_no_secrets.py` 守着不让它们回来。
- 真机探针 `tests/real/` 不进公开仓（它需要真账号的 ID 才有意义），
  边界写在 `test_no_secrets.py` 的 `PRIVATE_ONLY`，是这条边界的唯一事实来源。
- 脱敏闸门改为同时扫**未跟踪但没被 gitignore 的文件**
  （`git ls-files --cached --others --exclude-standard`）。
  只扫已跟踪的话，新文件要等提交之后才第一次被检查——而新加的文件恰恰是最可能
  夹带东西的那类。这个闸门自己就栽过：它作为未跟踪文件加进来时扫不到自己。

### 已知限制

- **豆包工作本版本不支持**——只能走客户端「技能中心 → 导入本地技能」，且只能人在界面上做。
- `jdy-sync` 仍是 Beta。
- Windows：GitHub Actions 的 Windows 矩阵（Python 3.9 / 3.13）与 GBK 控制台 job 已全绿，
  但**没在中文 Windows 真机上跑过 acceptance**。
