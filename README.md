<img src="assets/logo.png" width="120" align="right" alt="">

# JDY-SKILLS · 简道云 Agent 技能族

by [aicliagent](https://aicliagent.com)

> **非官方第三方项目。**「简道云」是帆软软件有限公司的注册商标，本项目与帆软
> 无隶属、无合作、无授权关系，只调用其公开开放 API。出问题请找本项目，
> 不要找简道云客服。详见 [SECURITY.md](SECURITY.md)。

**让 AI 助手直接干你在简道云里的活。** 导数据、出周报、清脏数据、批审批、
盯着表到点提醒——用大白话说，它去调简道云的 API 做。

**给谁用：** 天天在简道云里做表、导数、催审批的运营、行政、业务负责人。
不用会写代码。

**为什么不是官方那个：** 官方「AI 连接」只能读，**写入、批量、审批动作、
通讯录、附件、定时任务它一项都没有**——那些只能走这里。详见
[和官方「AI 连接」的分工](#和官方ai-连接的分工)。

11 个技能，零第三方依赖（不用 `pip install` 任何东西），Apache-2.0。

---

## 装

**1. 下载**：从 [Releases](../../releases) 拿最新的 zip，或者克隆本仓库。

**2. 装到你的 AI 客户端**：

```bash
python3 install.py --list     # 先看它认出了你机器上哪些客户端
python3 install.py            # 装到认出来的每一个
```

认不出你的客户端时：`python3 install.py --discover` 扫一遍本机，
或者 `python3 install.py --target '客户端名=/它的/skills目录'` 手工指定。

**3. 配一把 API Key**：

```bash
python3 skills/hello-jdy/scripts/setup.py
```

它会把**去哪儿点、点什么**一步步告诉你，拿到 Key 之后：

```bash
echo '你的KEY' | python3 skills/hello-jdy/scripts/setup.py --stdin
```

先验证能不能调通，通过了才写进 `~/.jdy/config.json`（权限 600）。
一把用不了的 Key 不会被存进去。

> 到简道云 **右上角头像 →「开放平台」→ 左侧「密钥管理」→「创建 API KEY」**。
> 建**最小权限**的：只勾要用的那个应用、只给用得到的接口、填 IP 白名单。
> 创建后**立刻复制**，弹窗关掉就再也看不到了。
> 官方文档写着"企业版及以上"，但试用账号实测可以调通，不必先付费。

**装不上或者用不了？** 跑一次诊断，它会告诉你断在哪一环：

```bash
python3 skills/hello-jdy/scripts/probe.py
```

## 五分钟跑通第一个

先看看你有哪些应用和表：

```bash
python3 skills/jdy-doc/scripts/export_dict.py --list
```

挑一个应用，把它的结构导成一份数据字典（能直接发给同事的 Markdown 文件）：

```bash
python3 skills/jdy-doc/scripts/export_dict.py --app 我的应用 --out 数据字典.md
```

把某张表导成 Excel（带 `_id` 列，改完还能导回去更新原记录）：

```bash
python3 skills/jdy-excel-bridge/scripts/export.py --app 我的应用 --entry 我的表单 --out 导出.xlsx
```

查一下数，顺手出张图（生成的 HTML 双击就能看，发给别人也不会坏）：

```bash
python3 skills/jdy-query/scripts/query.py --app 我的应用 --entry 我的表单 --group-by 职务 --top 8 --out 报告.html
```

跑通了就可以合上终端了——**日常用不着敲命令**，装好之后直接跟你的 AI 助手说
「把客户表导出来」「这周数据怎么样」就行。

## 11 个技能

| 技能 | 一句话 | 写数据吗 |
|---|---|---|
| [`hello-jdy`](skills/hello-jdy/) | **连接诊断**：装了却用不了时，告诉你断在哪一环；也负责引导配 Key | 只读 |
| [`jdy-doc`](skills/jdy-doc/) | **数据字典与体检**：把应用结构导成能交付的文件，扫出会让导入出错的问题 | 只读 |
| [`jdy-query`](skills/jdy-query/) | **查数与即席报告**：筛选、分组、聚合，出自包含 HTML 图表 | 只读 |
| [`jdy-report`](skills/jdy-report/) | **周报／月报**：按周期拉数聚合、环比、趋势，推到群机器人 | 只读 |
| [`jdy-excel-bridge`](skills/jdy-excel-bridge/) | **Excel ⇄ 简道云**：预检 → 分批幂等写入 → 回读比对 → 修复建议表，附件也能进出 | ✍️ 写 |
| [`jdy-clean`](skills/jdy-clean/) | **存量数据清洗**：查重、规范化、批量打标；去重只标记不删除 | ✍️ 写 |
| [`jdy-flow-ops`](skills/jdy-flow-ops/) | **流程运营**：待办收件箱、积压扫描、批量审批、按人催办 | ✍️ 写 |
| [`jdy-org`](skills/jdy-org/) | **通讯录与组织架构**：姓名↔成员编号权威对照表、建部门、调归属 | ✍️ 写（不接任何删除接口） |
| [`jdy-watch`](skills/jdy-watch/) | **数据哨兵**：按规则定时巡检，命中就推群，自带去重不刷屏 | 只读 |
| [`jdy-devkit`](skills/jdy-devkit/) | **集成开发加速器**：字段标识对照、可写形状、能直接跑的请求样例 | 只读 |
| [`jdy-sync`](skills/jdy-sync/) | **跨应用同步（Beta）**：按业务键增量同步，ID 映射保住表间关系 | ✍️ 写 |

每个技能的完整说明在它自己的 `SKILL.md` 里。

## 写数据这件事，我们是怎么小心的

简道云的写入接口**几乎不校验**：脏值静默存成 null，接口照样返回成功。
所以写入类技能一律：

- **默认 dry-run**，先给你看计划，你确认了才动手
- **写前整表备份**，写后**逐字段回读核对**——静默丢弃会被报出来，不会当成成功
- **`JDY_WRITE_ALLOWLIST`**：设了之后，目标不在名单里的写入直接拒绝
- `jdy-org` **一个删除接口都没接**，而且要另开 `JDY_ORG_WRITE=1`
- 去重只打标记，**从不删除任何记录**

**能改回去的只有还存在的记录。** 备份能还原被改写的值，但任何工具都变不回
已经被删掉的记录。

更多见 [SECURITY.md](SECURITY.md)。

## 和官方「AI 连接」的分工

简道云官方 2026-09-01 把原「MCP 服务」更名为「AI 连接」，12 项工具。
**这不是竞品关系，是分工关系：读走它，写走这里。**

| AI 连接**没有**的（12 项里一项都没有） | 只能走本项目 |
|---|---|
| 批量写入、更新、删除 | `jdy-excel-bridge` / `jdy-clean` |
| 审批动作（同意／否决／回退／转交）、催办 | `jdy-flow-ops` |
| 通讯录读写（建部门、调归属、加成员） | `jdy-org` |
| 附件进出 | `jdy-excel-bridge` |
| 跨应用同步、关系迁移 | `jdy-sync` |
| 定时巡检与阈值告警 | `jdy-watch` |
| 周期报表与推群 | `jdy-report` |

读的部分两边都能做，本项目还多给一点：`jdy-doc` 产出可交付的字典文件、
`jdy-query` 能分组聚合出 HTML 报告、`jdy-org` 给出姓名↔成员编号对照表。

⚠️ **两条轨的数据范围不同**：AI 连接是成员**个人视角**授权（看得到的＝该成员本人的权限），
本项目用的是**企业级 API Key**。同一个问题两边行数对不上，多半是这个原因，不是 bug。

## 支持哪些客户端

| 客户端 | 状态 |
|---|---|
| 腾讯 WorkBuddy | ✅ 端到端实跑验证通过 |
| 千问办公 QwenWork | ✅ 客户端内触发验证通过 |
| Claude Code | ✅ 作为基线 |
| **豆包工作** | ❌ **本版本不支持** |

**豆包工作本版本不支持**，`install.py` 里没有它的条目。不是路径没找对——
路径是对的；是它的技能目录由服务端按清单同步，复制进去的外来技能会被清掉
（实测 11 个在 14 分钟后消失）。往一个会静默清空的目录里装东西比不装更糟：
你看到"安装成功"，一刻钟后技能没了，而这两件事很难联系起来。
那一端唯一留得住的路是客户端「技能中心 → 导入本地技能」，只能人在界面上操作。
诊断脚本仍然认得出这一端，并会当面告诉你不支持。

Windows：编码与路径已处理并有 CI 覆盖，但**尚未在中文 Windows 真机上跑过完整验收**。

## 前置条件

- Python 3.9+（各端沙箱自带，不用另装）
- 一把简道云 API Key
- **不需要 `pip install` 任何东西**

## 开发

```bash
python3 build.py              # 把内核 vendor 进技能包（改完 _shared/ 必跑）
python3 tests/run_all.py      # 全部单元测试
python3 build.py --dist dist  # 发布：打 zip + 写 SHA256SUMS
```

设计取舍、平台实测记录、目录结构见 [docs/dev-notes.md](docs/dev-notes.md)。
版本变更见 [CHANGELOG.md](CHANGELOG.md)。上架材料与商标约束见
[docs/store-listing.md](docs/store-listing.md)。

## WorkBuddy 开放平台：技能与专家

腾讯 WorkBuddy 开放平台（open.workbuddy.cn）有两个渠道，要的是**两种不同形状的
压缩包**。两种都能从这个仓库构建出来，构建命令不同、产物也不同。

| 渠道 | 产物 | zip 内的第一层 | 怎么构建 |
|---|---|---|---|
| **技能** | `<name>-workbuddy.zip` × 11 | `skills/<name>/SKILL.md` | `python3 build.py --dist dist --layout workbuddy` |
| **专家** | `jdy-ops-expert.zip`、`jdy-dev-expert.zip` | `.codebuddy-plugin/plugin.json` | `python3 build_experts.py` |

GitHub Release 与千问办公用的还是原来那种 `<name>.zip`（第一层是 `<name>/`），
`python3 build.py --dist dist` 不变；`--layout both` 两种一起打，
SHA256SUMS 与 MANIFEST.txt 会把两种布局都收进去。

**顶层目录差一级，本地一点都看不出来。** 技能渠道要最外面多一级 `skills/`，
少了它上传时报"找不到 SKILL.md"；专家包反过来，zip 的根**就是**包内容，
外面多套一层目录，平台报「压缩包缺少 .codebuddy-plugin/plugin.json 文件」。
两种错都只在上传那一刻出现，所以 `tests/test_release.py` 和 `tests/test_experts.py`
各自盯着包内路径。

### 两个专家包

专家 = CodeBuddy 插件格式（`.codebuddy-plugin/plugin.json` + `agents/` +
`avatars/` + `skills/`）。源文件在 [`experts/`](experts/) 下，
**技能目录不入库**——构建时按 plugin.json 的 `skills` 声明从 `skills/` 拷进去，
免得仓库里存两份同名技能慢慢分叉。

| 专家 | 给谁 | 装了哪些技能 |
|---|---|---|
| [`jdy-ops-expert`](experts/jdy-ops-expert/) 简道云运营助手 | 天天在简道云里做表、导数、催审批的运营／行政／业务负责人 | `hello-jdy`、`jdy-doc`、`jdy-query`、`jdy-excel-bridge`、`jdy-report`、`jdy-flow-ops`、`jdy-clean`、`jdy-watch`、`jdy-org` |
| [`jdy-dev-expert`](experts/jdy-dev-expert/) 简道云集成开发助手 | 要写代码对接简道云、或做数据迁移的人 | `hello-jdy`、`jdy-doc`、`jdy-devkit`、`jdy-sync` |

两个专家的系统提示词里都写死了同一套规矩：**非官方第三方身份**、
**读可以走官方「AI 连接」连接器、任何写入只走技能脚本**、
写入先 dry-run 念计划、用户点头才 `--execute`、从不删除记录、
没配 Key 就引导跑 `hello-jdy` 的配置向导而不是让用户把 Key 贴进对话。

```bash
python3 build_experts.py --check   # 只校验 experts/，不产出文件
python3 build_experts.py           # 校验 + 构建到 dist/experts/
```

## 作者

本项目由 [aicliagent](https://aicliagent.com) 制作与维护。用得顺手、或者踩到坑，
都欢迎来说一声。

生成出来的文件（HTML 报告、周报 Markdown、数据字典、集成样例、修复建议表）
末尾会带一行署名。**它是写死在文件里的静态文字，不发起任何网络请求**
（见 [SECURITY.md](SECURITY.md)）。不想要就关掉：

```bash
export JDY_BRAND=0      # 也认 false / off；关掉后产物里不留那一行
```

推给群机器人的消息正文从来不带署名——那是你的工作群。

## 行业包与托管服务（规划中）

以下都**还不存在**，是接下来打算做的方向，先写在这里收集需求。想要哪一个、
或者有别的需要，来 [aicliagent.com](https://aicliagent.com) 说。

- **行业报表与巡检规则包** —— 现成的报表定义与哨兵规则，装上改两个字段就能用；
  引擎本身保持不认业务概念，行业差异全落在这些配置包里。
- **托管定时** —— `jdy-report` 和 `jdy-watch` 要按点跑，就需要一台不关机的机器；
  自己不想留一台的话，由我们来托管这份定时。
- **企业功能** —— 多人、权限、审计这类企业里才需要的部分。
- **实施支持** —— 落地、对接、排查的人工支持。

开源的这 11 个技能会一直是开源的，上面这些是另外的东西，不影响它们。

## License

Apache-2.0
