# 定时生成周报

`build_report.py` 是个普通命令行脚本，任何定时器都能调。**它不依赖 MCP**——
这是有意的：豆包工作的自定义 MCP 仅本地模式可用、定时任务走云电脑，
依赖 MCP 的方案在那一端会失效。直连 REST 就没这个问题。

## 通用（macOS / Linux）

```bash
# 每周一 09:00 生成并推送
0 9 * * 1 cd /path/to/skill && \
  python3 scripts/build_report.py 周报.yaml --out /tmp/周报.md && \
  python3 scripts/push.py /tmp/周报.md --send
```

密钥从 `~/.jdy/config.json` 读；cron 环境变量少，**不要指望它继承 shell 里的
`JDY_API_KEY`**，用配置文件更稳。

## 各端的定时能力

| 端 | 定时任务 | 注意 |
|---|---|---|
| WorkBuddy | 客户端内置定时 | 沙箱写白名单不含 `~/.jdy`，报表输出请落在会话工作目录或临时目录 |
| 豆包工作 / 千问办公 | 客户端内置定时 | 沙箱白名单未实测。技能状态会自动找可写目录，但**报表输出文件由你指定路径**，写不进去会直接报错——先用 `hello-jdy` 探针看 C9 选中了哪儿 |
| 豆包工作 | 定时任务走**云电脑** | 自定义 MCP 仅本地模式可用 → 依赖 MCP 的方案在定时场景失效；本技能直连 REST，不受影响 |
| 千问办公 | 待实测（V4） | — |
| 系统 cron | 最可靠 | 与端无关，适合企业交付 |

## 复现历史报表

`--now YYYY-MM-DD` 可以把"现在"固定到某天，用来补跑上周的报表或核对口径：

```bash
python3 scripts/build_report.py 周报.yaml --now 2026-08-21 --out 上周补跑.md
```
