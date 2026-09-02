# 简道云字段类型 ↔ 数据格式对照

> **证据来源：2026-08-27 对真实账号 6 个应用 / 56 张表单的 API 实测**（`widget/list` + `data/list`），
> 不是文档转抄。读取格式为实测确认；**写入格式标注「待实测」的尚未验证**，阶段 1 用测试表逐个验。
>
> **写入行为另见 [write-behavior.md](write-behavior.md)** —— 已实测 text/number/datetime 三类，
> 核心结论：值必须 `{"value": x}` 包裹，且格式不合法时**静默存 null 而不报错**。

## 一、最要命的三个类型：三种"关联"长得完全不一样

简道云有三种关联字段，API 返回结构**互不相同**，必须分别处理。这是 `jdy-excel-bridge` 的头号坑。

> ⚠️ **先钉死一个名词陷阱**：中文控件名与 API 类型是反直觉的。
> 「**选择数据**」= `linkdata`（**不可 API 写入**）；「**关联数据**」= `lookup`（**可以写**）。
> 搞反了会得出完全相反的架构结论。

| 类型 | 中文控件名 | 实测返回值 | 显示值 | 可 API 写入 |
|---|---|---|---|---|
| `linkdata` | 选择数据 | `{"id": "<data_id>"}` | 无 | **❌ 不可**（官方明示） |
| `linkobject` | 关联表单 | `{"link_form": "<entry_id>", "link_id": "<data_id>", "name": "示例客户"}` | 有 `name` | ✅ 写 `{"link_id": …}`；裸串报 3005 |
| `lookup` | 关联数据 | `"<data_id>"`（裸字符串） | 无 | **✅ 直写 data_id 建立关系** |

各自的导入导出影响：

- **`linkdata`**：导出是一串看不懂的 ID，且**导不回去**。含此类字段的表无法通过 API 保持关系
- **`linkobject`**：自带 `name` 与目标表单 ID，导出可读；**写入只给 `{"link_id": data_id}`**，
  接口自己补 `link_form` 并把 `name` 覆盖成目标记录的真名（写 ID、读回展开对象，与 `user` 同理）
- **`lookup`**：裸字符串，**和普通 `text` 长得一模一样**——只能靠 `widget/list` 的 `type` 区分。
  可以写，但**接口不校验引用是否存在**，详见 [write-behavior.md](write-behavior.md)

这三种在真实应用里经常同时出现，务必按 `type` 而非字段名判断。

> **表设计惯例**：`linkdata` 旁边常配一个 `text` 字段存显示值（实测见订单管理子表单里的「关联数据-主键」＝
> `"示例商品-饮料整箱装"`）。preflight 应当识别这种伴随字段并优先用它做人类可读输出。

## 二、全部实测类型

| type | 显示名 | 读取格式（实测） | 写入格式 |
|---|---|---|---|
| `text` | 单行文本 | `"字符串"` | 同 |
| `textarea` | 多行文本 | `"字符串"` | 同 |
| `number` | 数字 | `30`（JSON number） | 同 |
| `number`（百分比） | 百分比 | **小数** `0.75` 表示 75%（实测 CRM「赢率」27 条取值均在 0~1） | 同——阈值分层别拿 0~100 去比 |
| `datetime` | 日期时间 | `"1996-02-02T16:00:00.000Z"` ISO8601 **UTC** | 待实测（注意时区偏移） |
| `radiogroup` | 单选 | `"男"` | 同 |
| `combo` | 下拉单选 | `"本科"` | 同 |
| `checkboxgroup` | 复选 | `["销售"]` **数组** | ✅ 字符串数组；裸串与顿号串静默丢弃 |
| `combocheck` | 下拉复选 | `["了解客户需求"]` **数组** | ✅ 同上 |
| `user` | 成员单选 | `{"name":"hao","username":"sys_6a8d...","status":1,"type":0,"departments":[1]}` | **写入需 `username`，不是 `name`** —— 这是"成员重名"坑的根源 |
| `usergroup` | 成员多选 | `[{同上}]` 数组 | 同上，数组 |
| `dept` | 部门单选 | `{"name":"mdmouse","dept_no":1,"type":0,"parent_no":0,"status":1}` | ✅ **写裸 `dept_no` 整数**；对象与部门名全部静默丢弃 |
| `deptgroup` | 部门多选 | 数组 | 待实测（账号里没有样本） |
| `address` | 地址 | `{"province":"…","city":"…","district":"…","detail":""}` | **对象原样写回**；拼接好的地址串**静默丢弃** |
| `phone` | 电话 | `{"verified": false, "phone": "138…"}` | **`{"phone": "138…"}`**；纯字符串**静默丢弃** |
| `company` | 企业名称 | `"示例：无锡示例企业"` 纯字符串 | ✅ 纯字符串 |
| `sn` | 流水号 / **自动编号** | `"00007"` 字符串 | **系统生成，导入不可自造** —— 计划里"流水号冲突"坑的成因。界面上的「自动编号」控件 API 返回的也是 `sn`；扫过 74 张表单没有出现过 `autonum` 这个类型 |
| `image` | 图片 | `[{"name","size","mime","url"}]` 数组 | ✅ 先上传拿 key，写 `["<key>"]`；写入请求要带同一个 transaction_id |
| `upload` | 附件 | 同 `image` | ✅ 同 `image`：先上传拿 key，写 `["<key>"]` |
| `signature` | 签名 | 未签为 `{}` 空对象 | 待实测（签过的样本没见过） |
| `subform` | 子表单 | `[{"_id":"...", "_widget_xxx": ...}, ...]` 数组，**每行带 `_id`**，内层**不包** `value` | ✅ 写 `[{内层: {"value": v}}]` **双层包裹**；单层（即读回来的形状）静默丢弃；update 是**整表替换** |
| `lookup` | 关联数据 | `"<24位十六进制 data_id>"` | **直写目标记录 data_id**；接口不校验引用存在 |
| `linkdata` / `linkobject` | 选择数据 / 关联表单 | 见第一节 | `linkdata` 不可写 |
| `leads_pool` / `account_pool` / `sale_stage` | CRM 套件专有 | 未采样 | CRM 套件独有，通用技能可先不支持 |

## 三、子表单结构可以完整拿到

`widget/list` 对 `subform` 会返回 `items` 数组，描述内部字段：

```json
{"name":"_widget_1504854132443","label":"订单明细","type":"subform",
 "items":[{"name":"_widget_1566976937263","label":"关联数据","type":"linkdata"},
          {"name":"_widget_1409210537263","label":"关联数据-主键","type":"text"},
          {"name":"_widget_1566976937682","label":"单价","type":"number"},
          {"name":"_widget_1504855928911","label":"数量","type":"number"},
          {"name":"_widget_1504855928943","label":"金额","type":"number"}]}
```

→ `jdy-doc` 能导出完整含子表单的数据字典；`jdy-excel-bridge` 能对子表单列做映射校验。

## 四、附件 URL 会过期 ⚠️

`image` / `upload` 返回的 `url` 形如：

```
https://files.jiandaoyun.com/FvX5nSIM...?attname=xxx.png&e=1789181999&token=IAM-0WcXoIs...
```

`e=` 是**过期时间戳**（实测样本 `1789181999` ≈ 2026-09-11，约 15 天）。
→ 导出的 Excel 里直接塞这个链接，**十几天后全部失效**。`jdy-excel-bridge` 的导出必须
要么当场下载附件到本地，要么明确告知用户链接有效期。

## 四之二、"显示值 ≠ 可写值"是个通用陷阱

到目前为止，**所有对象型字段都遵循同一条规律**：读出来是对象，人看到的是其中一段，
但写回去必须给对象——给显示串一律**静默丢弃**。

| 类型 | 读到的 | 人看到的 | 写回去要给 |
|---|---|---|---|
| `user` | `{name, username, …}` | 张三 | **`username`** 字符串 |
| `phone` | `{verified, phone}` | 138… | **`{"phone": "138…"}`** |
| `address` | `{province, city, district, detail}` | 江苏省无锡市… | **原对象** |
| `lookup` | `"<data_id>"` | 一串 ID | data_id（且**不校验引用存在**） |

所以任何"读出来再写回去"的流程（导出改完再导入、跨应用同步）都不能用显示值当中转。
内核里 `display_value()` 是给人看的，`sync_value()` 才是可写的——两者有意分开。

## 五、字段标识与系统字段

- 字段 key 是 `_widget_<13位时间戳>` 的固定 ID；`widget/list` 里 `name` 与 `widgetName` 实测同值，
  `label` 才是用户看到的显示名 → **映射必须走 `label` ↔ `name`**
- 设了**别名**后所有 API 改用别名作字段名（本账号未使用别名，待补实测）
- 系统字段（文档只列了一半，实测还有）：
  `_id` `appId` `entryId` `creator` `updater` `deleter` `createTime` `updateTime` `deleteTime`
  —— `creator`/`updater`/`deleter` 是完整成员对象，不是字符串

## 六、怎么找到自己的测试床

不必手搭测试应用——简道云自带的**示例应用**通常已经覆盖了全部坑型。
用 `jdy-doc` 一条命令就能找出哪些表单含哪些类型：

```bash
python3 skills/jdy-doc/scripts/export_dict.py --app <你的app_id> --out 字典.md
```

字典里「可 API 写入」那一列会直接标出 ❌ 的字段。要找特定坑型，看这几种组合：

| 想验的坑型 | 找什么样的表单 |
|---|---|
| 子表单整体提交 | 含 `subform` 的表单（订单、申请类常有） |
| 三种关联的差异 | 同时含 `linkdata` 与 `linkobject` 的应用 |
| 成员名→username | 含 `user` / `usergroup` 的表单 |
| 流水号冲突 | 含 `sn` 的表单 |
| 附件 URL 过期 | 含 `image` / `upload` 的表单 |

⚠️ **写操作请另建一张废弃表**，不要拿有真实数据的表做导入实验。
本仓库的所有写入实测都是在专门新建的空表上做的。
