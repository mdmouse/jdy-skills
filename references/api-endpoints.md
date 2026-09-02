# 简道云开放 API 接口速查

> 复核日期 **2026-08-27**（对照官方开放平台文档逐条确认）。
> 平台迭代快，每两周巡检时重新核对本表——尤其是频率限制。

## 全局约定

| 项 | 值 |
|---|---|
| 基础地址 | `https://api.jiandaoyun.com/api` |
| 协议 | 仅 HTTPS，仅 POST，UTF-8 |
| Body | JSON（文件上传接口用 `form_data`） |
| 鉴权 | Header `Authorization: Bearer <API_KEY>` |
| 全局频率 | **50 次/秒**（各接口另有更严的单独限制，取两者较小值） |
| 错误响应 | HTTP **400** + `{"code": 8303, "msg": "超出请求频率限制"}` |
| 其他状态码 | `429` 超并发、`502` 网关异常 |
| 密钥 | 开放平台 > 密钥管理 > 创建 API KEY；单企业上限 500 个；**需企业版及以上** |

## 已确认接口

| 接口 | 地址（`/api` 之后） | 频率 | 关键参数 |
|---|---|---|---|
| 查询应用列表 | `/v5/app/list` | 30/s | `limit` 1~100（默认 100）、`skip` |
| 查询表单列表 | `/v5/app/entry/list` | 30/s | `app_id`、`limit` 1~100（默认 100）、`skip` |
| 查询表单字段 | `/v5/app/entry/widget/list` | 30/s | `app_id`、`entry_id` |
| 查询多条数据 | `/v5/app/entry/data/list` | 30/s | `app_id`、`entry_id`、`data_id`(游标)、`fields`、`filter`、`limit` 1~100（**默认 10**） |
| 新建单条数据 | `/v5/app/entry/data/create` | 20/s | `app_id`、`entry_id`、`data`、`transaction_id` |
| 新建多条数据 | `/v5/app/entry/data/batch_create` | **10/s** | `data_list` **≤100 条**、`transaction_id`、`data_creator`、`is_start_workflow` |

实测返回结构（2026-08-27 真实账号）：
- `app/list` → `{"apps":[{"name","app_id"}]}`
- `entry/list` → `{"forms":[{"name","app_id","entry_id"}]}`
- `widget/list` → `{"widgets":[{"name","widgetName","label","type"}]}`；`subform` 类型**额外带 `items` 数组**描述子表单内部字段
- `data/list` → `{"data":[{...}]}`，每条含系统字段 ＋ `_widget_xxx` 业务字段

**试用账号实测可调通 API** —— 调研结论"仅企业版可用"需修正：试用期即可开发验证，不必等付费。

## ⚠️ 三类写接口的 body 里没有表单信息

`app_id` / `entry_id` 是写入白名单（`JDY_WRITE_ALLOWLIST`）唯一的判据，
而下面这些写接口的 body 里根本没有它们：

| 接口族 | body 里有什么 | 归哪道闸管 |
|---|---|---|
| `/v{1,2}/workflow/task/*` | `task_id` / `instance_id` / `username` | 调用方拿待办自带的 `app_id`/`form_id` 逐条查 |
| `/v{5,6}/corp/*` | `dept_no` / `username` | `JDY_ORG_WRITE`（另一道开关） |

对它们**硬查表单白名单，拿到的是 `(None, None)`，等于无条件拒绝**。
流程接口踩过一次，通讯录接口后来又踩了同一脚——
**再加这类接口时，`post()` 里的三岔和它自己那道闸都要一起补。**

## 分页

`data/list` 的返回**恒按数据 ID 正序**。翻页把上一页最后一条的 `_id` 作为下一次的
`data_id` 游标传入——没有 offset 分页，全量拉取必须走游标。

## 审计日志接口：本账号用不了（2026-08-31 实测）

官方写明是**付费高级功能，旗舰版／独享版可用**。本试用账号实测：

| 请求 | 响应 |
|---|---|
| `POST /v1/audit_log/type_definitions` | ❌ HTTP 403 |
| `POST /v1/audit_log/list`（login / platform / app_builder / kms 四个 domain） | ❌ `8302 Do not have permission for the API calls.` |

→ **不要基于它做技能**。做了也只有旗舰版用户能用，而且没法在开发账号上验证。
真要做，只能做成「先探一次权限、有才启用」的可选能力。

协议本身记在这里，将来有旗舰版账号可直接用（`tests/real/audit_probe.py` 可复跑）：

```
POST /v1/audit_log/list   30 次/秒
{ "domain": "app_builder",                    # 一次只能查一个范围
  "time_range": {"start": "...Z", "end": "...Z"},   # 跨度 ≤ 31 天
  "event_types": [...], "limit": 200, "cursor": "...",
  "filters": {"actor_ids": [], "app_ids": [], "entry_ids": []} }
→ { has_more, cursor, items[{event_id, event_time, event_type, domain, tenant,
                             actor{type,id,name,ip,user_agent,geo},
                             event{category,action,outcome,severity},
                             resource{type,id,name,parent_id,parent_type}, detail}] }
```

`filters` 支持哪些字段随 domain 变，传了该 domain 不支持的字段会**报参数错误**
（这点比数据接口好——数据接口是静默忽略）。分页按事件时间倒序。

## 文件接口（2026-08-31 实测打通）

| 用途 | 路径 | 频率 | 关键参数 |
|---|---|---|---|
| 取上传凭证 | `/v5/app/entry/file/get_upload_token` | 20/s | `app_id`、`entry_id`、`transaction_id`；一次返回 **100** 组 `{url, token}` |
| 上传文件 | 上一步返回的 `url`（七牛 `upload.qiniup.com`） | 20/s | `multipart/form-data`：`token` + `file`（**file 放最后**，要给对 mime）→ `{"key"}` |

拿到的 `key` 填进附件/图片控件，形状是 **`["<key>"]` 字符串列表**
（`[{"key": k}]` 静默丢弃），且**写入请求必须带上取凭证时的同一个
`transaction_id`**。凭证与事务的有效期都是 1 小时。

一个 token 只能传一个文件，不允许覆盖。详见 write-behavior.md 四之六。

## filter DSL

```json
{"rel": "and", "cond": [{"field": "_widget_1", "method": "eq", "value": ["x"]}]}
```

- `rel`：`and` | `or`
- `method`：`empty` `not_empty` `eq` `ne` `in` `nin` `range` `like` `gt` `lt` `verified` `unverified` `all`

### ⚠️ 认不出的 `method` 也是静默忽略（2026-08-31 实测）

23 行的表，按同一个文本字段筛同一个值：

| method | 返回 |
|---|---|
| `eq` | 2 行 ✅ |
| `contains` | **23 行**（整表） |
| `包含` | **23 行** |
| `nonsense` | **23 行** |

→ 和"字段名写错""值的类型不对"是**同一种事故的第三个入口**：
接口 200、条件被整个丢掉、返回整表，而调用方以为筛过了。
所以 method 必须在客户端就校验，不能指望接口报错。

### ⚠️ 系统字段一律进不了 filter（2026-08-31 实测）

`_id` `createTime` `updateTime` `updater` 全部**不能**作为 `filter` 的 `field`。
接口 **200 正常返回，条件被静默忽略**——返回的是整表的前 N 条。

| 请求（11 行的表） | 响应 | 实际返回 |
|---|---|---|
| `{"field": "_id", "method": "in", "value": [id1, id2]}` | 200 | ❌ 前 10 条 |
| `{"field": "_id", "method": "eq", "value": [id1]}` | 200 | ❌ 前 10 条 |
| `{"field": "updateTime", "method": "gt", "value": ["2030-01-01T00:00:00.000Z"]}` | 200 | ❌ 11 条（未来时间，正确答案是 0 条） |
| `{"field": "createTime", "method": "gt", "value": ["2030-…"]}` | 200 | ❌ 11 条 |
| `{"field": "updater", "method": "gt", "value": ["2030-…"]}` | 200 | ❌ 11 条 |

只有**控件字段**（`_widget_…`，或用显示名解析成它）能筛。

### ⚠️ 值的类型不对，条件同样被静默忽略（2026-08-31 实测）

| 请求（26 行的表，`订单总额` 是 number） | 返回 |
|---|---|
| `{"method":"lt","value":["1000"]}` 字符串 | ❌ **26 行**，最大值 16080——条件被忽略 |
| `{"method":"lt","value":[1000]}` 数字 | ✅ 1 行，最大值 40 |

也就是说：**数字字段的条件值必须是数字**，传字符串等于没筛。
日期同理，要传归一化后的 ISO-UTC 串。

这和"不认识的字段"是同一种事故的两个入口——一个在字段名那侧、一个在值的类型这侧。
`build_filter` 现在按字段类型转换并在转不出来时报错。

两个直接后果：

1. **回读核对不能靠 `_id` 筛**。那样会把别人的行当成自己刚写的行来比对，
   而且比对"通过"。所以 `fetch_rows_by_id` 走「扫 + 逐条 get」。
2. **没有 `updateTime` 增量拉取这条路**。想只拉"上次同步之后改过的"，
   接口层面做不到，只能全量拉回来在本地比。

新增筛选功能时务必记得：这个接口对看不懂的条件**不报错**——
"筛选静默失效"在这里是默认行为，不是意外。

### `fields` 投影（同日实测）

传一个**真实控件标识**才生效，能显著缩小响应；`fields: []` 与 `fields: ["_id"]`
同样被**静默忽略**（返回整行）。

| `fields` | 返回字段 | 两行的字节数 |
|---|---|---|
| 不传 | 全部 33 列 | 3418 |
| `[]` | 全部 33 列（**被忽略**） | 3418 |
| `["_id"]` | 全部 33 列（**被忽略**） | 3418 |
| `["_widget_1504835294344"]` | `_id` + 该列 + `appId` + `entryId` | 486 |

投影结果里 `_id` / `appId` / `entryId` 总是带着，不用也不能去掉。

## 字段类型

**见 [field-types.md](field-types.md)** —— 基于真实账号 56 张表单实测，比文档列表完整得多。

官方文档的类型列表**不全**，实测多出这些关键类型：`linkdata` `linkobject` `lookup`（三种关联，
返回结构互不相同）、`sn`（流水号）、`signature`、`company`、`phone`、`leads_pool`/`account_pool`/`sale_stage`（CRM 套件专有）。
另有实测未见的文档类型：`location`（本账号无样本）。

系统字段实测比文档多三个：`_id` `appId` `entryId` `creator` **`updater`** **`deleter`**
`createTime` `updateTime` **`deleteTime`**。

## 写入行为的坑（来自调研，待 Sprint 0/阶段 1 实测复验）

- `batch_create` **不触发**重复值校验与必填校验——API 写入绕过表单校验。
- `transaction_id` 1 小时内幂等：同 ID 重复提交会**覆盖**前次数据，是重试的正确姿势。
- 子表单必须**整体提交**，不能单独改一行。
- API 写入**不回推 webhook**（用于 `jdy-sync` 时是天然防循环）。
- 无表单结构创建、无仪表盘、无催办接口。
  「无表单结构创建」2026-09-01 又核了一遍（扫全账号 6 个应用 78 张表）：**建表、加字段
  只能人在界面做**。所以凡是需要一张新测试表的验证（比如 sync 三期的 W0 要两张同构表），
  工具这边无论如何都做不了，得先请人建——排期时把它当成外部依赖，别当成一步实现。

## 待补（阶段 1 前补齐）

修改单条/多条、删除单条、查询单条、文件上传、流程接口（v6 `workflow/task/list`、
`approve`/`rollback`/`transfer`）、错误码全表。
