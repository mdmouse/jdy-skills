# 上架材料

## 图标

| 文件 | 尺寸 | 用途 |
|---|---|---|
| `assets/logo.png` | 1250×1282 | **品牌插画**：帝王持剑骑北极熊，背后长城雪山 |
| `assets/icon-1024.png` 等四档 | 1024 / 512 / 256 / 128 | **商店图标**：从插画里裁的帝王胸像 |

### 为什么图标是裁出来的，不是整张插画

整张插画在小尺寸下不可用。实测把四种取景各自缩到 512/128/64/48 再放大来看：

* **整张插画** 到 48px 只剩「一团白上面一坨红」——剑、冕旒、长城、云纹全部消失；
* **帝王胸像满幅** 到 48px 冕旒、黑须、红袍三样都还在，是唯一扛得住的取景；
* 熊头也扛得住（黑鼻黑掌对比强），但一只白熊的辨识度不如戴冕旒的人；
* **留白是负收益**：给图标加 8%~16% 的边，等于把 30%~50% 的像素让给背景色，
  主体反而缩小、更难认。所以最终是满幅无留白。

裁切区域 `(470, 120, 780, 430)`，透明区用深靛蓝 `#172A46` 兜底。

### 用插画时注意

`logo.png` **不是镂空的**：全透明只占 0.1%（那是圆角抗锯齿），其余是一张
近白的卡片底 `#FBFCF6`。所以它在深色背景上会显示成一块白卡（贴纸感），
在浅色背景上边界会不明显。要压在深色横幅上的话，需要另做一版去底的。

素材是 512 色平涂插画，转成 256 色调色板后体积降了 83%，肉眼无差别。

图标本身没有商标风险：它是自有站点的标识，不是简道云/帆软的任何素材。

## 名称与描述里的硬约束

「简道云」是**帆软软件有限公司**的注册商标。上架时：

- ✅ 名称里可以出现「简道云」用于**说明兼容对象**，但必须让人一眼看出是第三方。
  建议形如「简道云技能族（第三方）」，或者干脆用自有品牌 + 副标题说明。
- ❌ 不要用简道云/帆软的官方 logo、配色、视觉素材。
- ❌ 不要出现「官方」「授权」「合作」「认证」这类字眼。
- ✅ 描述里第一屏就写明「非官方第三方项目，与帆软无隶属关系」，
  和 [SECURITY.md](../SECURITY.md) 开头那段一致。

以后要收费时，这一条会被审核方问到。

## 描述文案

每个技能的商店描述直接用它 `SKILL.md` frontmatter 里 `description` 的**第一段**
（首行那句人话）。后面的触发词和对 Agent 的行为约束是给模型看的，
放进商店卡片就是噪音，但**不要从 SKILL.md 里删掉**——技能能不能被正确触发全靠它们。

## 渠道现状

> 以下来自公开资料，**上架前需要各自再确认一次**，规则会变。

| 渠道 | 上传方式 | 付费 |
|---|---|---|
| 千问办公 Skill 广场 | 传 SKILL.md 或 GitHub 链接 | 未见公开说明 |
| WorkBuddy 技能市场 | 仅认证开发者，需联系管理员申请 | — |
| 扣子技能商店 | 需开通支付渠道并申请上架资质 | 支持付费技能 |

现阶段可走的路：免费版走 GitHub + 千问广场 + WorkBuddy 认证上架，
付费版走扣子商店 + 自有托管。

## 上架前还差什么

- [x] 一张像样的方形图标（`assets/icon-512.png`，48px 实测可读）
- [ ] 中文 Windows 真机跑一遍 `python3 tests/run_all.py`
- [ ] 每个渠道的具体规则再确认一次

## SkillHub（skillhub.cn，腾讯 WorkBuddy 官方推荐市场）逐技能上架表

> 一次提交 = 一个技能。上传方式选「选择文件夹」，直接选仓库里的 `skills/<slug>/`（已含 vendor 进去的内核）。
> 显示名称与一句话描述与 SKILL.md 首行一致；分类名以站点实际下拉为准，这里是建议。
> 全部标注「非官方第三方」；图标统一用 `assets/icon-512.png`；版本 0.7.0；许可证 Apache-2.0；
> 仓库地址 https://github.com/mdmouse/jdy-skills 。

| 顺序 | Slug | 显示名称 | 一句话描述 | 建议分类 | 标签 |
|---|---|---|---|---|---|
| 1 | `jdy-doc` | 简道云数据字典与结构体检 | 把简道云应用导成一份可交付的数据字典文件，并体检出导入与集成会踩的坑。只读。 | 数据处理 | 简道云,数据字典,低代码,集成 |
| 2 | `jdy-query` | 简道云查数与图表报告 | 按条件筛选、分组聚合简道云数据，产出自包含的 HTML 图表报告。只读。 | 数据分析 | 简道云,查数,报表,图表 |
| 3 | `jdy-excel-bridge` | 简道云 Excel 导入导出 | Excel 与简道云双向搬数据：预检、分批幂等写入、写后回读比对、修复建议表，附件也能进出。 | 数据处理 | 简道云,Excel,导入,导出,附件 |
| 4 | `jdy-report` | 简道云周报月报 | 按周期拉数、聚合、环比、趋势与 Top 榜，渲染成 Markdown，可推送企业微信／飞书／钉钉群。只读。 | 办公效率 | 简道云,周报,月报,群机器人 |
| 5 | `jdy-flow-ops` | 简道云审批流程运营 | 待办收件箱、流程积压扫描、批量同意／否决／回退／转交、按人催办。写操作默认 dry-run。 | 办公效率 | 简道云,审批,待办,流程,催办 |
| 6 | `jdy-clean` | 简道云数据清洗 | 扫填充率与格式不统一、查重只打标不删除、规范化只做不改语义的处理，写前备份、写后回读。 | 数据处理 | 简道云,数据清洗,查重,规范化 |
| 7 | `jdy-watch` | 简道云数据哨兵 | 按规则定时巡检简道云表单，阈值命中或有新记录就推到群里，自带去重与冷却。只读。 | 办公效率 | 简道云,告警,巡检,提醒 |
| 8 | `jdy-org` | 简道云通讯录管家 | 导出部门树与姓名↔成员编号对照表；建部门、调归属、加成员。不接任何删除接口。 | 办公效率 | 简道云,通讯录,组织架构 |
| 9 | `jdy-devkit` | 简道云集成开发加速器 | 给一张表单生成字段标识对照、可写形状、可直接跑的 curl／Python 样例与入参校验函数。只读。 | 开发工具 | 简道云,API,SDK,集成 |
| 10 | `jdy-sync` | 简道云跨应用同步（Beta） | 按业务键比对、按引用拓扑序同步、持久化 ID 映射保住表间关系，子表单与附件也能搬。默认 dry-run。 | 数据处理 | 简道云,数据同步,迁移 |
| 11 | `hello-jdy` | 简道云连接诊断 | 装了简道云技能却用不了时，一条命令告诉你断在哪一环，并引导配置 API Key。只读。 | 开发工具 | 简道云,诊断,配置 |

建议先发前 3 个（jdy-doc / jdy-query / jdy-excel-bridge）摸清审核口径，再批量发其余 8 个。

---

## 腾讯 WorkBuddy 开放平台（open.workbuddy.cn）

两个渠道，两种形状的包。**顶层目录差一级，本地一点都看不出来，只在上传那一刻报错。**

### 渠道一：技能

zip 内结构必须是 `skills/{skill-name}/SKILL.md`——**最外层有一级 `skills/`**，
和 GitHub Release／千问办公用的 `<name>/SKILL.md` 不是一回事。

```bash
python3 build.py --dist dist --layout workbuddy   # 产出 <name>-workbuddy.zip
python3 build.py --dist dist --layout both        # 两种布局一起打
```

SKILL.md frontmatter 字段：

| 字段 | 必填 | 说明 | 本项目怎么填 |
|---|---|---|---|
| `name` | 否 | 技能标识 | 与目录名一致 |
| `display_name` | 示例里有 | 展示名称 | 取本文末尾 SkillHub 表的「显示名称」 |
| `display_name_en` | 示例里有 | 英文展示名 | 自拟 |
| `description` | 是 | 用途与触发词 | **原样保留**——它是三端的触发依据，几百字，不是商店文案 |
| `description_zh` | 是 | 简短中文介绍 | 取 SkillHub 表的「一句话描述」，控制在 60 字内 |
| `description_en` | 是 | 简短英文介绍 | 自拟 |
| `category` | 示例里有 | 分类 | 见下表 |
| `version` | 是 | 版本号 | 0.7.0 |
| `author` | 是 | 合作方名称 | `aicliagent` |

> `description` 与 `description_zh` 是**两件东西**：前者给模型判断要不要触发，
> 带一长串用户话术；后者是商店卡片上那一行。把前者塞进卡片是噪音，
> 把后者当触发依据会让技能触发不了。两个都得有。

`tests/test_skill_format.py` 逐个技能校验这六个新字段存在且非空、
`description_zh` ≤ 60 字、`author` 恰好是 `aicliagent`、`category` 在枚举内。

#### category 取值

**官方没有给出完整枚举**，这里先用五个值，**以审核反馈为准**——被打回就照它给的改：

| category | 用在哪些技能 |
|---|---|
| `data` | `jdy-doc`、`jdy-query`、`jdy-excel-bridge`、`jdy-clean`、`jdy-sync` |
| `office` | `jdy-report`、`jdy-flow-ops`、`jdy-org` |
| `automation` | `jdy-watch` |
| `development` | `jdy-devkit`、`hello-jdy` |
| `writing` | （暂未使用） |

### 渠道二：专家

专家包 = CodeBuddy 插件格式。**只有 plugin.json 放在 `.codebuddy-plugin/` 里，
其余目录都在包根**；**zip 的根就是包根，不要再套一层目录**——套了会报
「压缩包缺少 .codebuddy-plugin/plugin.json 文件」。

```
<expert>/
├── .codebuddy-plugin/plugin.json
├── agents/<agentName>.md
├── avatars/expert.png
├── skills/<skill-name>/SKILL.md     # 构建时从仓库 skills/ 拷进来，不入库
└── README.md
```

```bash
python3 build_experts.py --check   # 只校验
python3 build_experts.py           # 校验 + 构建到 dist/experts/
```

#### plugin.json 字段

| 字段 | 必填 | 约束 |
|---|---|---|
| `name` | ✅ | 小写字母 + 连字符（kebab-case） |
| `expertType` | ✅ | 固定 `"agent"` |
| `version` | ✅ | 语义化版本 |
| `description` | ✅ | **英文**简短描述 |
| `author` | ✅ | `{name, email}` |
| `agents` | ✅ | 路径数组，如 `["./agents/my-expert.md"]` |
| `agentName` | ✅ | 主 Agent 名 = `agents/` 下文件名去掉 `.md` |
| `skills` | 否 | 技能目录路径数组，如 `["./skills/jdy-doc"]` |
| `displayName` | ✅ | `{en, zh}` |
| `profession` | ✅ | `{en, zh}` 职业头衔 |
| `displayDescription` | ✅ | `{en, zh}`，**中文字数必须在 40–50 之间** |
| `avatar` | ✅ | 相对路径 `avatars/expert.png` |
| `categoryId` | ✅ | 见下 |
| `defaultInitPrompt` | ✅ | `{en, zh}`，**必须与 `quickPrompts[0]` 一致（中英都要）** |
| `plugin` | ✅ | 与 `name` 相同 |
| `tags` | ✅ | `{en, zh}[]`，**固定 3 个** |
| `quickPrompts` | ✅ | `{en, zh}[]`，**固定 3 个** |
| `homepage` / `license` / `keywords` | 否 | — |
| `dependencies` | 否 | `{"mcpServers": …, "connectors": ["连接器ID"]}` |

**本版本不声明 `dependencies.connectors`**：简道云官方连接器的 ID 我们还不知道，
猜错会让安装直接失败，比不声明更糟。两个专家的 README 里各留了一行 TODO。

`categoryId` 枚举：`01-ProductDesign`、`02-Engineering`、`03-GameSpatial`、
`04-DataAI`、`05-MarketingGrowth`、`06-ContentCreative`、`07-SalesCommerce`、
`08-FinanceInvestment`、`09-OperationsHR`、`10-ProjectQuality`、
`11-SecurityCompliance`、`12-IndustryConsultant`、`13-TencentZone`、
`14-WorldWise`、`15-Education`。
本项目：`jdy-ops-expert` → `09-OperationsHR`，`jdy-dev-expert` → `02-Engineering`。

#### agents/<name>.md

YAML frontmatter + 正文（正文就是系统提示词）。frontmatter：
`name` ✅（与文件名一致）、`description` ✅（**英文**，AI 用它判断何时激活）、
`displayName` ✅ `{en, zh}`、`profession` ✅ `{en, zh}`、`maxTurns` 否（默认 50）、
`skills` 否（预加载的技能名）。

⚠️ **开发者不可自行添加 `tools` 字段**——工具权限由平台统一分配。

#### 头像

PNG、512×512、单张 ≤ 500KB，放 `avatars/`。本项目两个专家都用
`assets/icon-512.png`（512×512、57KB），构建时原样带进包里。

#### 谁在盯着这些

`tests/test_experts.py`：上面每一条 ✅ 约束逐个断言，加上构建产物的包内路径
（第一层必须直接是 `.codebuddy-plugin/plugin.json`），
再加**变异检查**——把中文描述改成 39 字、把 tags 删成 2 个、
把 `defaultInitPrompt` 改得与 `quickPrompts[0]` 不一致，各自必须红。
判据本身来自 `build_experts.validate()`，测试与打包共用同一份，不另抄。
