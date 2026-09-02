---
name: jdy-report
description: |
  **出简道云的周报／月报／日报。** 按周期拉数、聚合、按维度拆分与 Top 榜、
  与上期环比、看时间趋势，渲染成 Markdown，可推送到企业微信／飞书／钉钉群。只读。
  用户说「这周数据怎么样」「出个周报／月报／日报」「和上周比怎么样」「帮我算个达成率」
  「按周拆一下趋势」「Top 排行」「把报表发到群里」时，用这个技能。
  **他没给数字、也没说数据在哪时，不要当算术题答，更不要说"工作区是空的"**——
  先列出他简道云里的候选表单让他选，再拉数。
  做的事：按周期拉数，本地聚合计数／求和／均值／去重、按维度拆分与 Top 榜、
  与上期环比、按天／周／月的时间趋势，渲染成 Markdown；
  可推送到企业微信／飞书／钉钉群机器人。只读，不修改简道云数据。
  适用于任何简道云应用，不含业务领域预设。需要简道云 API Key。
  另外这些说法也走本技能：生成简道云周报、做个月报、出个日报、数据汇总、分组统计、
  这周的数据怎么样、按周拆分、按月趋势、环比、同比、算个比率、定时报表。
version: 0.7.0
license: Apache-2.0
display_name: "简道云周报月报"
display_name_en: "JDY Periodic Reports"
description_zh: "按周期拉数、聚合、环比、趋势与 Top 榜，渲染成 Markdown，可推送企业微信／飞书／钉钉群。只读。"
description_en: "Pulls data per period and renders Markdown reports with aggregates, period-over-period deltas, trends and Top-N rankings; can push to WeCom, Feishu or DingTalk group bots. Read-only."
category: office
author: aicliagent
---

# jdy-report 周报／月报

## 为什么在本地聚合

不占简道云 AI 点数、不受界面 200 条限制、口径完全可控。数据用服务端 filter
按周期筛好再拉，所以不需要全量拖表。

## 能做什么

| 需求 | 配置项 |
|---|---|
| 本期总计（计数/求和/均值/最大最小/去重） | `metrics` |
| 与上期对比、环比 | `period.compare: previous` |
| 按字段拆分 + Top 榜 | `dimensions` ＋ `top` |
| **按天/周/月看趋势** | **`trend: day｜week｜month`** |
| 指定统计区间 | `period.range`，含 `custom` 自定义起止 |
| 用业务日期而非录入时间做时间轴 | `period.field` 填该日期字段的显示名 |
| **不同板块用不同的时间轴** | **板块内 `period_field`**（各表的业务日期字段名常常不同） |
| **比率：回款率／良率／达成率／周转率** | **`agg: ratio`**，分子分母可**跨板块**引用（`板块.指标`） |

**要"最近 30 天按周拆分"这类诉求，直接在板块里加 `trend: week` 即可，不需要另写脚本。**
时间桶按区间预生成、空桶保留为 0，所以不同指标的序列天然等长，不会错位。

**要算比率**（分子分母常在两张不同的表，如回款率＝回款单÷销售订单），
用 `agg: ratio` 并跨板块引用，不要自己算——分母为 0 的期间会被正确标成 `—` 而不是 0 或 ∞。

**本技能不含任何业务领域预设。** 它认的是简道云的字段类型（`datetime` 能当时间轴、
`combo` 能当维度、`number` 能求和），所以 PLM／ERP／WMS 与 CRM 一视同仁——
已在项目管理、物资管理应用上零改动验证过。

## 三步

```
python3 scripts/init_config.py --app <app_id> --out 周报.yaml   # 生成配置骨架
python3 scripts/build_report.py 周报.yaml --out 周报.md          # 生成报表
python3 scripts/push.py 周报.md --webhook <群机器人URL>          # 预览（不发）
python3 scripts/push.py 周报.md --webhook <URL> --check           # 只验通不通，不发消息
python3 scripts/push.py 周报.md --webhook <URL> --send --yes      # 确认后真发
```

配置写法见 `references/report-config.md`。定时任务见 `references/scheduling.md`。

## 关键口径（讲给用户听时要说清）

- **区间左闭右开** `[start, end)`。简道云的 `range` 是闭区间，直接用会让相邻两期
  重复计数——脚本已减 1 毫秒处理，但跟用户对数时要说明这个口径
- **周期按北京时间（+08:00）切**，不受跑脚本的机器时区影响
- **上期为 0 时不给百分比**，只给绝对增量。除零算出的"+∞%"是噪音不是信息
- **Top 榜会标出"共 N 组"**，让用户知道有没有被截断
- 维度值为空的行归入 **`(未填)`** 分组，不静默丢弃

## 推送

**推送是对外动作，发出去收不回。**

规矩不是关于 `--send` 这个参数的，是关于**那个 webhook**：
**任何发往 webhook 的 POST 都会在群里冒出一条消息**——包括你自己写的
「连通性探测」「测试一下通不通」。实测中就发生过：Agent 绕开本脚本
直接 POST 了一条「连通性探测」进群，然后才反应过来那是条真消息。

- 要验地址通不通，用 `python3 scripts/push.py 报表.md --webhook <URL> --check`，
  它只发 GET，群里不留痕
- 要发正文，先把预览给用户看、拿到明确同意，再 `--send --yes`
- **不要自己写 curl / urllib 去 POST 那个地址**

`--send` 在非交互环境下不给 `--yes` 会直接拒绝执行。
Webhook URL 里带密钥，脚本日志已掩码，你在对话里也不要回显完整 URL。

群机器人发送失败最常见的三个原因：关键词不匹配、IP 白名单、加签校验——
响应不像成功时脚本会提示核对这三项。

## 呈现建议

- 先讲结论再讲数字："本周跟进量涨了 3 成，主要来自渠道商" 比直接甩表格有用
- 环比异常（涨跌超过 50%）要点出来，并提醒可能是数据录入延迟而非真实波动
- 板块无数据时不要沉默——那通常意味着周期字段选错了，要主动提示

## 安全边界

只调用只读接口（`app/list`、`entry/list`、`widget/list`、`data/list`）。
密钥读 `JDY_API_KEY` 或 `~/.jdy/config.json`，绝不写进技能目录。
