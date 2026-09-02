---
name: jdy-devkit
description: |
  **要自己写代码调简道云 API 时，先用这个。** 官方只有 demo 仓库没有 SDK，
  字段标识和写入形状只能自己一个个试——这里把实测结论固化成可以直接抄的代码。
  给某张表单一次性生成：字段显示名↔字段标识对照表、
  每种控件的**可写形状**、能直接跑的请求样例（curl / Python，零第三方依赖）、
  以及照该表单类型生成的入参校验函数。官方只有 demo 仓库没有 SDK，
  字段标识和写入形状只能自己一个个试——这里把实测结论固化成可抄的代码。只读。
  触发词：简道云 SDK、字段标识对照、_widget_ 是什么、怎么调简道云接口、
  请求样例、接入简道云、简道云 API 怎么写、写入格式、集成代码、
  字段 ID 是什么、简道云开发。
version: 0.7.0
license: Apache-2.0
display_name: "简道云集成开发加速器"
display_name_en: "JDY Integration Devkit"
description_zh: "给一张表单生成字段标识对照、可写形状、可直接跑的 curl／Python 样例与入参校验函数。"
description_en: "For a given form, generates the label-to-field-id table, the writable payload shape per widget, runnable curl/Python samples with zero third-party dependencies, and an input validator."
category: development
author: aicliagent
---

# jdy-devkit 集成开发加速器

## 它解决什么

做简道云集成，第一天会连撞三堵墙：

1. **读用显示名、写用字段标识**（`_widget_1504835294651`），两边不是一个东西
2. **显示值 ≠ 可写值**——`phone` 读回来是 `{verified, phone}`，写进去要
   `{"phone": "..."}`；`address` 读回来是对象，写拼接好的字符串会被丢掉
3. **接口几乎不校验**——形状写错了照样返回 `success`，字段被静默存成 `null`

官方只有 demo 仓库、没有 SDK，这三条只能自己试出来。本技能把它们固化下来。

## 用法

```
python3 scripts/gen.py --list                       # 不知道表名就先列
python3 scripts/gen.py --app <应用名或ID> --entry <表单名或ID>          # 只看对照表
python3 scripts/gen.py --app <应用> --entry <表单> --out ./devkit       # 生成整套
```

产出四个文件：

| 文件 | 内容 |
|---|---|
| `fields.md` | 显示名 ↔ 字段标识 ↔ 类型 ↔ 可写 ↔ **写入形状**，逐字段说明 |
| `sample.sh` | curl 样例：游标分页读、单条新建。字段标识是真的，可直接跑 |
| `sample.py` | Python 样例：零第三方依赖，标准库即可跑 |
| `validate.py` | 照该表单类型生成的入参校验，写之前先跑一遍 |

**本技能自己**读密钥的方式和其他技能一样：环境变量 `JDY_API_KEY`
**或** `~/.jdy/config.json`，两者有其一即可，不必先 export。
**生成出来的代码**里不含任何密钥，只从环境变量读。

## 呈现建议

- 先说**这张表有几个字段、其中几个写不进去**——那几个才是坑
- `linkdata`（选择数据）、`sn`（流水号）**写不进去**，要在动手前就告诉用户，
  不是等他写完发现整列是空的
- `lookup`（关联数据）能写，但接口**不校验引用是否存在**——
  写个不存在的 ID 照样入库，回读也发现不了。这条值得单独提醒
- 生成的 `validate.py` 只做结构性校验（字段在不在、类型对不对），
  **业务规则生成不出来**，别让用户以为跑过就万无一失

## 安全边界

- 全程只读：只调 `app/list`、`entry/list`、`widget/list`
- 生成的样例中，写接口部分是**代码文本**，不会被本技能执行
- 本技能读 `JDY_API_KEY` 或 `~/.jdy/config.json`；生成物只从环境变量读，绝不含密钥
