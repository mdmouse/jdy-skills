# 流程接口事实

> 2026-08-28 对真实账号实测确认。**接口版本是混的**，路径里必须带版本前缀。

## 接口清单

| 用途 | 路径 | 频率 | 关键参数 |
|---|---|---|---|
| 查询我的待办 | `/v6/workflow/task/list` | 20/s | `username`、`limit` ≤100、`task_id`(游标) |
| 查询流程实例 | `/v6/workflow/instance/get` | 30/s | `instance_id`（**同 data_id**）、`tasks_type` 1=返回节点 |
| 待办提交（同意） | `/v1/workflow/task/approve` | 20/s | `username`、`instance_id`、`task_id`、`comment` |
| 待办否决 | `/v1/workflow/task/reject` | 20/s | 同上，`comment` **是否必填取决于节点配置** |
| 待办转交 | `/v1/workflow/task/transfer` | 20/s | 同上 ＋ `transfer_username` |
| 待办回退 | `/v2/workflow/task/rollback` | 20/s | 同上 ＋ `flow_id`、`back_type`(1 正常流转/2 直达) |

读 v6、写 v1/v2、数据接口 v5 —— 四个版本并存，这不是笔误。

## 实测错误码

| code | message | 含义 | 怎么办 |
|---|---|---|---|
| `1010` | The user does not exist. | `username` 传了显示名而非成员编号 | 用 `sys_` 开头的编号 |
| `50016` | The instance_id is invalid. | 该数据行没有流程实例 | 不是故障，见下文「实例是什么时候产生的」 |
| `50040` | This node can't be returned. | 该节点没有配置允许回退 | 去流程设计里开启回退。**别调参数**——`back_type`／`flow_id` 只在回退已启用时生效 |
| `5049` | The Transfer feature hasn't been enabled at this node. | 该节点没有开启转交 | 同上，去流程设计里开启 |
| `5004` | No comment for approval. | 节点要求填审批意见 | 带 `comment`。**但并非所有节点都要求**——实测本账号的审批节点不带意见也能否决 |

**`50040` 与 `5049` 都是 HTTP 200 + `status:failure`**——实测证实了下面第 1 条坑的危害：
不做检查的话，这两个失败会被报成"成功"，而流程实例其实纹丝未动。

## 两个坑

**1. 写接口失败返回 HTTP 200。**

```json
{"status": "failure", "code": 1010, "message": "用户不存在"}
```

只看 HTTP 状态码会把失败当成功。内核已在 `post()` 里识别 `status == "failure"` 并抛错。

**2. `username` 必须是成员编号。** 传 `hao`、`mdmouse` 这类显示名一律报
`1010 The user does not exist`。实测有效格式为 `sys_` 开头（也见过 `jdy-` 开头）。

## 实例与节点结构（实测）

```json
{
  "instance_id": "…", "form_title": "…", "status": 0,
  "url": "https://www.jiandaoyun.com/workflow/process_instance/…",
  "creator": {"username": "sys_…", "name": "hao", "departments": [1]},
  "tasks": [
    {"task_id": "…", "flow_id": 0, "flow_name": "流程发起节点",
     "create_time": "2026-08-28T08:35:14.384Z", "finish_time": "2026-08-28T08:35:14.384Z",
     "status": 1,
     "create_action": "forward", "finish_action": "forward",
     "assignee": {...}, "creator": {...}},
    {"task_id": "…", "flow_id": 1, "flow_name": "审批节点",
     "create_time": "2026-08-28T08:35:14.385Z", "finish_time": null, "status": 0, ...}
  ]
}
```

- 实例 `status`：0 进行中 / 1 已完成 / 2 手动结束
- 节点 `status`：0 待处理 / 1 已完成
- **每个节点都有 `create_time` 与 `finish_time`** → 停留时长、瓶颈分析的数据基础
- 文档注明：子流程、插件节点、抄送节点**无法**通过 `instance/get` 获取

### 时间字段的真实格式（2026-08-31 实测）

`task/list` 的 `create_time`／`finish_time`，以及 `instance/get` 的
`create_time`／`update_time`／`finish_time`，实测都是 **ISO-8601 UTC 毫秒**：

```
"2026-08-28T08:35:14.385Z"      # 未完成时 finish_time 为 null
```

这份文档以前这几个位置写的是 `"…"` 占位符，于是没人知道它带不带时区。
**带不带时区不是格式细节**：不带时区的串解析出来是 naive datetime，
拿去和 `now(utc)` 相减（算"等了多久"）直接抛 `TypeError`——
一个只读操作以 traceback 收场。所以时间一律走内核 `parse_iso`，
它对不带时区的串按 UTC 补齐，看不懂的返回 `None` 而不是崩。

## 实例是什么时候产生的

**提交时才创建。** 流程配置之前就存在的数据行没有实例，查 `instance/get` 会报
`50016 The instance_id is invalid`——这不是故障。

API 写入默认也**不触发**流程，需要在 `batch_create` 里显式传 `is_start_workflow: true`。
实测：不传则无实例，传了则实例与首个待办同时生成。

## 写操作实测结论（2026-08-28）

在 `销售CRM / 缺货申请` 的「审批节点」上逐个验证：

| 操作 | 结果 | 数据层核对 |
|---|---|---|
| `approve` | ✅ 成功 | 实例 `status=1`／`result=1`，节点 `finish_action=forward` |
| `reject` | ✅ 成功（**未带 comment 也通过**） | 实例 `status=1`／`result=0`，节点 `finish_action=reject` |
| `rollback` | ❌ `50040` 节点未开启回退 | 实例仍 `status=0`，未受影响 |
| `transfer` | ❌ `5049` 节点未开启转交 | 实例仍 `status=0`，未受影响 |

后两个是**流程配置问题，不是代码问题**——关键在于失败被正确识别并如实报出，
而不是因为 HTTP 200 就当成功。审计日志四条齐全（2 成功 2 失败，含错误原因）。

## 尚未实测

加签（`/open/17368`）、撤回（`/open/17367`）、结束实例（`/open/16052`）、
激活实例（`/open/17366`）、流程日志（`/open/18793`）、抄送列表（`/open/22875`）、
审批意见（`/open/16050`）。账号只有 1 名成员，转交给他人无法验证。
