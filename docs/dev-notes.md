# 开发记录

产品页在 [README.md](../README.md)。这里放的是**做这个项目时的取舍与实测记录**——
对使用者是噪音，对接手的人是全部理由。

---

## 产品边界与领域中立

### 只做通用引擎

**本仓库只做标准通用引擎，不做领域包。** 这是一条硬约束，由
[tests/test_domain_neutral.py](../tests/test_domain_neutral.py) 守着——
它扫描引擎代码的 token（跳过注释与文档字符串），一旦出现业务领域词汇就失败。

理由：领域逻辑一旦漏进引擎，PLM／ERP／WMS 就得各自分叉维护，
"一份技能包三端复用"立刻变成"每个行业一套"。

领域差异只能落在两个地方：**报表定义 YAML**（现在），以及**未来的可插拔领域包**（后续再考虑）。

### 不绑业务领域

技能认的是**简道云的字段类型**，不是业务概念——`datetime` 能当时间轴、`combo` 能当维度、
`number` 能求和、`linkdata` 写不进去。所以 PLM／ERP／WMS 与 CRM 一视同仁。

已零改动验证：**IT项目管理**（自动认出「项目关键里程碑」当维度、「预计变更人天」当指标、
「申请时间」当时间轴）、**物资管理**（体检正常报出流水号冲突风险）。

领域差异落在**报表定义**这一层，不在代码里。想要行业专用的指标口径，
写一份对应的报表定义即可，不需要改引擎——引擎不认识业务，也不该认识。

构建（把内核 vendor 进技能包，安装前必跑）与测试：

```bash
python3 build.py && for t in tests/test_*.py; do python3 "$t" || break; done
```

装到各端（先 `build.py`，安装是复制真实目录，改完要重装）：

```bash
python3 install.py --list                          # 看检测到哪些端
python3 install.py --discover                      # 装了新客户端却认不出时，扫一遍本机
python3 install.py --target '端名=/某处/skills'      # 名单里没有的端手工指定
python3 install.py                                 # 装全部技能到所有检测到的端
```

## 和官方「AI 连接」是什么关系

简道云官方 2026-09-01 把原「MCP 服务」更名为 **「AI 连接」**，工具从 4 项扩到 12 项，
并【官方声称】上架了 WorkBuddy、千问办公的连接器市场（来源
[doc/26886](https://hc.jiandaoyun.com/doc/26886)、[open/25090](https://hc.jiandaoyun.com/open/25090)；
本仓库尚未实测其市场安装，实测结论见[兼容性验证表 V5 节](platform-compat-matrix.md)）。

**这不是竞品关系，是分工关系：读走它，写走这里。**

| AI 连接（12 项工具）有什么 | 本仓库对应的技能 |
|---|---|
| 列应用 / 列表单 / **列字段**（`member_app_*`） | `jdy-doc`（还产出可交付的数据字典文件与集成体检） |
| 查数据列表 / 查单条（`member_data_*`、`member_managed_data_*`） | `jdy-query`（还能分组聚合、出自包含 HTML 报告） |
| 取用户信息（`member_user_info`） | `jdy-org`（还给出姓名↔成员编号的权威对照表） |
| **列**待办（`member_workflow_task_list`，只列不办） | `jdy-flow-ops`（还能积压扫描与批量审批） |
| 单条新增（`member_data_create`，需管理员审核） | `jdy-excel-bridge`（预检 → 分批幂等 → 回读比对 → 修复建议表） |

| AI 连接**没有**的（12 项里一项都没有） | 只能走技能轨 |
|---|---|
| 批量写入、更新、删除 | `jdy-excel-bridge` / `jdy-clean` |
| 审批动作（同意／否决／回退／转交）、催办 | `jdy-flow-ops` |
| 通讯录读写（建部门、调归属、加成员） | `jdy-org` |
| 附件进出 | `jdy-excel-bridge` |
| 跨应用同步、关系迁移 | `jdy-sync` |
| 定时巡检与阈值告警 | `jdy-watch` |
| 周期报表与推群 | `jdy-report` |

两条轨的**凭证与数据范围也不同**：AI 连接是成员**个人视角**授权（看得到的数据＝该成员本人的权限），
技能轨是**企业级 API Key**。同一个问题两边行数对不上，多半是这个原因，不是 bug。

**豆包工作不在官方任何清单里**（doc/26886 的安装提示词点名的是 WorkBuddy / 悟帆 AI / Codex）
→ 该端**技能轨是简道云能力的唯一通路**。

> 写入类技能的 SKILL.md 正文里都有一段「与官方简道云 AI 连接的分工」，
> 点名禁止把 `member_data_create` 循环起来当批量导入用——它与 REST 写接口同源，
> 脏值静默存 null 照样返回成功，而那条路上没有预检、备份、回读和写入白名单。
> 这段话由 [tests/test_cli_contract.py](../tests/test_cli_contract.py) 按代码派生的写入技能名单逐个把关，
> 新增写入技能却忘了写，测试会红。

## 目录

```
├── skills/                     # 11 个技能包，每个自带 SKILL.md 与 version
│   ├── hello-jdy/              # 连接诊断 + 引导配 Key。**仅标准库，不 vendor 内核**
│   ├── jdy-doc/                # 数据字典 + 结构体检
│   ├── jdy-query/              # 查数 + 自包含 HTML 报告
│   ├── jdy-report/             # init_config / build_report / push
│   ├── jdy-excel-bridge/       # preflight / import_data / export（含附件）
│   ├── jdy-clean/              # scan / plan / label / apply / restore
│   ├── jdy-flow-ops/           # inbox / backlog / act / nudge（催办）
│   ├── jdy-org/                # dump / apply —— 部门树·成员编号·组织架构
│   ├── jdy-watch/              # 规则巡检 + 命中推群（数据哨兵）
│   ├── jdy-devkit/             # 字段标识对照 / 可写形状 / 请求样例
│   └── jdy-sync/               # plan / apply（Beta，含子表单与附件）
├── _shared/                    # 内核。由 build.py vendor 进各技能的 scripts/_shared/
│   ├── jdy_client.py           # 限流·游标分页·值编码·回读比对·备份·时区·写入白名单
│   ├── xlsx.py                 # 最小 xlsx 读写，仅标准库
│   ├── miniyaml.py             # YAML 子集解析，仅标准库
│   ├── platform_env.py         # 宿主识别 / 状态目录落点 / 配置查找
│   └── webhook.py              # 群机器人推送（报表 / 催办 / 哨兵共用）
├── build.py                    # vendor 内核（--check 校验，--dist 发布）
├── install.py                  # 装到各端 / 打 zip / --discover 扫本机
├── references/                 # 知识库——护城河本体，全部基于真实账号实测
│   ├── api-endpoints.md        # 接口·限流·分页·filter DSL
│   ├── field-types.md          # 字段类型 ↔ 数据格式（三种关联的区别）
│   └── write-behavior.md       # 写入行为与失败归因（静默失败目录）
├── docs/                       # 开发记录（本文件所在）
└── tests/
    ├── run_all.py              # 跨平台测试入口（discover 在本仓库跑不了）
    ├── test_windows.py         # GBK 控制台 + Windows 路径
    ├── test_no_secrets.py      # 脱敏闸门（PRIVATE_ONLY 是公开/私有边界的唯一来源）
    ├── test_release.py         # 发布产物：包完整·校验和可核·版本对得上
    └── real/                   # 会真的写数据的验证脚本 + 变异检查（**不进公开仓**）
```

## 状态

| 技能 | 状态 |
|---|---|
| `hello-jdy` | ✅ 平台探针，WorkBuddy／Claude Code 验证通过 |
| `jdy-doc` | ✅ 数据字典 + 结构体检 |
| `jdy-excel-bridge` | ✅ 预检 → 写入 → 回读比对 → 修复建议表 |
| `jdy-report` | ✅ 周期聚合 / 趋势 / 跨板块比率 |
| `jdy-flow-ops` | ✅ 待办 / 积压 / 批量处理 / 催办，写后核对 |
| `jdy-watch` | ✅ 规则巡检 + 命中推群，自带去重；只读 |
| `jdy-org` | ✅ 通讯录导出（姓名↔成员编号权威表）＋组织架构改动；**不接任何删除接口**，新增成员占用户数单独确认 |
| `jdy-sync` | 🅱️ Beta — 拓扑序 / ID 映射 / 引用翻译 / 子表单与附件搬运，已实测跨应用同步与幂等 |

平台验证：**腾讯 WorkBuddy** 已完成读、写、成功、失败四条路径的端到端实测；
**千问办公**已在客户端内触发验证通过；**豆包工作不通过**——`.skills` 由服务端
按清单同步，复制进去的外来技能会被清掉，只剩「技能中心 → 导入本地技能」这条
人工路径（详见 [兼容性验证表](platform-compat-matrix.md) V1）。
**Windows** 有 CI 覆盖（含 GBK 控制台 job），但没在中文 Windows 真机上跑过验收。

技能不按平台名分支。宿主是从**技能自己被装在哪**反推出来的，
可写目录是按候选顺序**实写**探出来的——这样对还没见过的端同样成立。
认不出宿主时它会把安装路径报出来，那就是下次要回填进名单的事实。
详见 `_shared/platform_env.py` 与兼容性验证表末尾的「三端适配的做法」。

## 三处对计划的有意偏离

都源自同一个实测事实：**各端沙箱有 Python 3.13，但没有 pip 通道**（V3 已验证）。

| 计划原文 | 实际做法 | 原因 |
|---|---|---|
| 用 openpyxl 读写 xlsx | 自实现 `_shared/xlsx.py` | 带第三方依赖会把"平台不支持"和"依赖装不上"混成一团 |
| 用 pandas 聚合 | 自实现 `aggregate.py` | 同上；报表要的 group-by 本来也不复杂 |
| YAML 报表定义 | 自实现 `_shared/miniyaml.py`（YAML 子集） | 装不了 PyYAML，但配置的可读性值得保留 |

三个自实现件都有独立测试，且**不支持的特性一律报错、绝不静默猜**。

## 安全约定（写进每个 SKILL.md）

- 密钥只从环境变量 `JDY_API_KEY` 或 `~/.jdy/config.json` 读取；**技能目录与仓库零密钥**
- **`JDY_HOME`**：把状态目录（字段缓存 / 审计日志 / 哨兵去重）钉在一个固定位置。
  不设也能跑——技能会自己找一个可写目录并打印落点；但沙箱把 `~/.jdy` 挡住时，
  自动落点可能只在本次会话有效，要跨轮次保留就设它
- **可选的写入白名单**：设 `JDY_WRITE_ALLOWLIST=<entry_id 或 app_id，逗号分隔>`，
  之后任何写接口只要目标不在名单里就直接拒绝。校验做在唯一的 HTTP 出口上，绕不过去。
  把 Agent 放在有真实业务数据的账号上时建议开；做验证时用它把写入实验圈在一张废弃表里
- 写操作默认 dry-run ＋ 用户确认；删除/覆盖前强制备份导出
- 子表单整体提交；批量写幂等可重试（`transaction_id`）
- 大表操作先报预估耗时，支持断点续跑

---

## 平台实测记录

- [兼容性验证表](platform-compat-matrix.md) —— 各端逐项实测结论
- [官方动态观察哨](ecosystem-watch.md) —— 官方 AI 连接 / Jander 的变化与应对

验证手册、双轨实验实录、审查修复清单这些**过程记录留在私有开发仓**，
不随公开仓发布——它们是排查叙事，不是给用户看的文档。

## 发布流程

```bash
python3 build.py --dist dist            # vendor + 打 zip + 写 SHA256SUMS/MANIFEST
cd dist && shasum -a 256 -c SHA256SUMS  # 核对
```

每个技能在自己的 `SKILL.md` frontmatter 里带 `version`，
`tests/test_skill_format.py` 强制它存在且是 x.y.z，
`tests/test_release.py` 盯着产物本身（包完整、校验和真能核、版本对得上、
包里没有未脱敏内容）。发完打 tag，更新 [CHANGELOG.md](../CHANGELOG.md)。

**不要手工 `install.py --zip` 发版**——那样打出来的包没有校验和、
也不保证 vendor 是新的。`dist/` 是 gitignore 的产物目录，没人盯着它：
08-27 手工打的那个 v0.1.0 探针就在豆包工作的「我的技能」里安安静静躺了好几天。
