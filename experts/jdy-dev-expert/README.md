# 简道云集成开发助手（第三方） · jdy-dev-expert

by [aicliagent](https://aicliagent.com) · hi@aicliagent.com

> **非官方第三方项目。**「简道云」是帆软软件有限公司的注册商标，本专家包与帆软
> 无隶属、无合作、无授权、无认证关系，只调用其公开开放 API。出问题请找
> [aicliagent](https://aicliagent.com)，不要找简道云客服。

给要写代码对接简道云、或者要把别的系统的数据搬进简道云的人用。官方只有 demo
仓库没有 SDK，字段标识和写入形状只能一个个试——这里把实测结论固化成可抄的代码。

## 装了哪些技能

| 技能 | 一句话 | 写数据吗 |
|---|---|---|
| `hello-jdy` | 连接诊断：装了却用不了时告诉你断在哪一环，也负责引导配 Key | 只读 |
| `jdy-doc` | 数据字典与结构体检：全量字段结构落成 Markdown，扫出导入会踩的坑 | 只读 |
| `jdy-devkit` | 集成开发加速器：字段标识对照、可写形状、能直接跑的 curl／Python 样例、入参校验函数 | 只读 |
| `jdy-sync` | 跨应用同步（Beta）：按业务键增量同步，ID 映射保住表间关系；源端也能是 CSV／JSONL／SQLite | ✍️ 写 |

技能目录**不在本目录下**：它们由 `build_experts.py` 从仓库的 `skills/` 拷进
构建产物，避免仓库里存两份同名技能慢慢分叉。

## 和官方「简道云 AI 连接」的分工

**读走它，写走这里。** 只读的结构探查装了官方连接器就可以走它；
批量写入、附件、跨应用同步、关系迁移它一项都没有。

⚠️ 主 Agent 的系统提示词里写死了一条：**不许拿官方连接器的「单条新增记录」
工具套个循环当批量导入或数据迁移用**——它脏值静默存成 null 还返回成功，
没有预检、没有备份、没有回读。搬一万条会报"全部成功"，而某几列一直是空的。

## 需要什么

- Python 3.9+（各端沙箱自带）
- 一把简道云 API Key。没配的话让助手带你跑：

  ```
  python3 skills/hello-jdy/scripts/setup.py
  ```

  它先验证能不能调通，通过了才写进本机配置文件（权限 600）。
  **不要把 Key 贴在对话里**，生成的样例代码也一律从环境变量或配置文件读。
- 不需要 `pip install` 任何东西；生成的样例代码同样零第三方依赖。

## 构建

在仓库根目录：

```
python3 build_experts.py --check
python3 build_experts.py
```

产物是 `dist/experts/jdy-dev-expert.zip`，**zip 的根就是包内容**
（第一层直接是 `.codebuddy-plugin/`、`agents/`、`avatars/`、`skills/`），
不套一层目录——套了开放平台会报「压缩包缺少 .codebuddy-plugin/plugin.json 文件」。

## TODO

- [ ] **声明官方连接器依赖。** plugin.json 支持
      `dependencies.connectors: ["连接器ID"]`，可以让这个专家在安装时自动带上
      官方「简道云 AI 连接」，只读那一半就能走连接器。**简道云官方连接器的 ID
      我们还不知道**，所以这一版**没有声明**——ID 猜错会让安装直接失败，
      比不声明更糟。拿到 ID 之后补上。

## License

Apache-2.0
