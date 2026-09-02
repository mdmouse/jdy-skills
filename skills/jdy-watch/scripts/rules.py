# -*- coding: utf-8 -*-
"""哨兵的规则引擎：配置、命中判定、去重状态。纯逻辑，不碰网络，所以能被完整测试。

三件必须做对的事：

1. **不刷屏。** 哨兵是定时跑的，同一条记录每轮都命中是常态（库存一直低于阈值）。
   报一次就得记住，除非过了冷却期。做不到这点的哨兵会被用户关掉，
   然后它就等于不存在。
2. **不漏报。** 去重状态存不下来时（沙箱只读），宁可**重复提醒**也不能静默跳过
   ——重复是噪音，漏掉是事故。
3. **"新增"要有可信的基准。** 第一次跑没有基准，此时把整表当成"新增"全推出去
   是灾难。第一次只记录、不提醒，并且说清楚。
"""
import datetime
import json
import os

import platform_env
from jdy_client import display_value, parse_iso

STATE_FILE = "watch-state.json"
KINDS = ("threshold", "new_rows")


class RuleError(ValueError):
    pass


def load_config(path, parse_yaml):
    """读规则文件并校验。看不懂的写法一律报错，绝不静默忽略——
    一条被忽略的规则 = 一个用户以为在盯着、其实没盯的指标。"""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        cfg = json.loads(text) if path.endswith(".json") else parse_yaml(text)
    except Exception as exc:
        raise RuleError("规则文件解析失败：%s" % exc)
    if not isinstance(cfg, dict):
        raise RuleError("规则文件顶层必须是映射")
    rules = cfg.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RuleError("rules 必须是非空列表")

    seen = set()
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise RuleError("第 %d 条规则不是映射" % (i + 1))
        for key in ("name", "app", "entry"):
            if not r.get(key):
                raise RuleError("第 %d 条规则缺少 %s" % (i + 1, key))
        if r["name"] in seen:
            # 去重状态是按规则名存的，重名会让两条规则互相吃掉对方的状态
            raise RuleError("规则名重复：%s —— 去重状态按名字存，重名会互相覆盖"
                            % r["name"])
        seen.add(r["name"])
        kind = rule_kind(r)
        if kind == "threshold" and not r.get("when"):
            raise RuleError("规则「%s」既没有 when 也没有 new_rows，"
                            "不知道要盯什么" % r["name"])
        if r.get("remind_after_hours") is not None:
            try:
                float(r["remind_after_hours"])
            except (TypeError, ValueError):
                raise RuleError("规则「%s」的 remind_after_hours 不是数字：%r"
                                % (r["name"], r["remind_after_hours"]))
    cfg["_base_dir"] = os.path.dirname(os.path.abspath(path))
    return cfg


def rule_kind(rule):
    """这条规则盯的是"当前满足条件"还是"新出现的记录"。"""
    return "new_rows" if rule.get("new_rows") else "threshold"


class State(object):
    """记住每条规则已经就哪些记录提醒过、什么时候提的。

    存不下来时**退化为"每次都提醒"而不是"不提醒"**：
    重复是噪音，漏掉是事故，两害相权取噪音。
    """

    def __init__(self, path=None):
        # 落点运行时定：~/.jdy 在 WorkBuddy 沙箱里不可写，另外两端未知。
        # `home` 留着是为了让调用方能把降级原因原话说给用户听——
        # 哨兵最忌讳的就是状态没存下还装作存下了。
        self.home = None if path else platform_env.resolve_state_home()
        if path:
            self.path = os.path.expanduser(path)
        else:
            self.path = platform_env.state_path(STATE_FILE)
        self.data = {}
        self.readonly = self.path is None
        self.corrupt = None       # 状态文件读不成形状时，写下原因
        self.kept_aside = None    # 坏文件真的挪走了才填——**没挪就别说挪了**
        self.first_run = set()
        if self.path and os.path.exists(self.path):
            self.corrupt = self._load()

    def _load(self):
        """读状态文件。返回 None 表示读好了，否则返回一句"哪里坏了"。

        原来这里只接 (OSError, ValueError)，**形状不对是接不住的**：
        文件顶层是个列表时 `json.load(fh).get(...)` 直接抛 AttributeError，
        命令行工具甩出 traceback。而更糟的是接住的那一半——损坏时静默 data={}，
        于是「新增行」类规则把这一轮当成**首次运行**：只建基准、一条都不提醒。
        哨兵最不该出的事就是这个：它没坏，它只是**不响了**。
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except OSError as exc:
            return "读不出来：%s" % exc
        except ValueError as exc:
            return "不是合法 JSON：%s" % exc
        if not isinstance(raw, dict):
            return "顶层应该是对象，实际是 %s" % type(raw).__name__
        rules = raw.get("rules", {})
        if not isinstance(rules, dict):
            return "rules 应该是对象，实际是 %s" % type(rules).__name__
        clean = {}
        for name, bucket in rules.items():
            if not isinstance(bucket, dict):
                return "规则「%s」下面应该是 {记录ID: 时间}，实际是 %s" % (
                    name, type(bucket).__name__)
            clean[name] = bucket
        self.data = clean
        return None

    def seen_before(self, rule_name):
        return rule_name in self.data

    def last_notified(self, rule_name, row_id):
        return (self.data.get(rule_name) or {}).get(row_id)

    def mark(self, rule_name, row_id, when):
        self.data.setdefault(rule_name, {})[row_id] = when.isoformat()

    def forget_missing(self, rule_name, alive_ids):
        """记录不再命中就忘掉它——否则状态文件只涨不减，
        而且这条记录下次再出问题时会被当成"已经提醒过"。"""
        bucket = self.data.get(rule_name)
        if bucket is None:
            return
        for row_id in [k for k in bucket if k not in alive_ids]:
            del bucket[row_id]

    def save(self):
        """写回状态。**坏文件要等这一步成功之后才挪走。**

        原来是一读到损坏就立刻改名挪开——于是这一轮如果没落盘（--dry-state、
        或者目录不可写），下一轮回来看到的是"根本没有状态文件"：
        corrupt 是 False、seen_before 是 False，新增型规则又一次被当成
        「首次运行」——只建基准、一条都不提醒，而且这次连那句警告都没有了。
        损坏的信号被"保全证据"这个动作自己销毁了。

        所以顺序反过来：新状态确实写下去了，才把坏文件挪到一边。
        写不下去就让它留着——下一轮照样会报"状态损坏"，照样不闭嘴。
        """
        if not self.path:
            self.readonly = True
            return False                    # 一个可写目录都没有，如实认账
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if self.corrupt and os.path.exists(self.path):
                # 先挪开再写：坏文件是唯一能看出"上次提醒到哪儿"的东西
                try:
                    os.replace(self.path, self.path + ".corrupt")
                    self.kept_aside = self.path + ".corrupt"
                except OSError:
                    pass
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"rules": self.data}, fh, ensure_ascii=False, indent=2)
            return True
        except OSError:
            self.readonly = True
            return False


def should_notify(rule, state, row_id, now):
    """这条命中该不该提醒。返回 (要不要提, 为什么不提)。"""
    last = state.last_notified(rule["name"], row_id)
    if last is None:
        return True, None
    cooldown = rule.get("remind_after_hours")
    if cooldown is None:
        return False, "已提醒过（没设 remind_after_hours，只提醒一次）"
    then = parse_iso(last)
    if then is None:
        return True, None                       # 状态里的时间读不懂，宁可重复提醒
    hours = (now - then).total_seconds() / 3600.0
    if hours >= float(cooldown):
        return True, None
    return False, "%.1f 小时前提醒过，冷却期 %s 小时" % (hours, cooldown)


def format_row(template, row, by_label):
    """把 `{字段显示名}` 换成这一行的值。

    认不出的字段**原样留着**而不是变成空——空出来看着像"这个字段是空的"，
    而实际是模板写错了字段名，两件事得能区分。
    """
    if not template:
        return None
    out = template
    for label, widget in by_label.items():
        token = "{%s}" % label
        if token in out:
            value = display_value(row.get(widget["name"]), widget["type"])
            out = out.replace(token, "" if value is None else str(value))
    return out


def evaluate(rule, rows, by_label, state, now=None):
    """算出这一轮要报什么。**纯函数**（除了读 state），不发任何消息。

    返回 {hits, suppressed, first_run, kind}：
      hits        这轮要提醒的行 [{row_id, text, row}]
      suppressed  命中了但被去重压住的条数（要报出来，否则用户以为哨兵瞎了）
      first_run   新增型规则的第一次运行：只建基准、不提醒
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    kind = rule_kind(rule)
    # 状态损坏时**不许**把这一轮当成首次运行。首次运行的含义是"只建基准、
    # 不提醒"，而损坏时那等于让哨兵闭嘴——按本模块的取舍（重复是噪音、
    # 漏掉是事故），这时宁可全报一遍。
    baseline_missing = (kind == "new_rows" and not state.seen_before(rule["name"])
                        and not getattr(state, "corrupt", None))

    hits, suppressed = [], []
    alive = set()
    for row in rows:
        row_id = row.get("_id")
        if not row_id:
            continue
        alive.add(row_id)
        if baseline_missing:
            state.mark(rule["name"], row_id, now)
            continue
        ok, why = should_notify(rule, state, row_id, now)
        if not ok:
            # 新增型里"见过的旧记录"**不是被压住的命中**，是本来就不该报的东西。
            # 报成"18 条被去重压住"会让人以为哨兵吞了 18 条要紧事。
            if kind == "threshold":
                suppressed.append((row_id, why))
            continue
        hits.append({"row_id": row_id, "row": row,
                     "text": format_row(rule.get("message"), row, by_label)})
        state.mark(rule["name"], row_id, now)

    if kind == "threshold":
        # 阈值型：不再命中的记录要从状态里清掉，否则它下次再出问题会被当成"提醒过"
        state.forget_missing(rule["name"], alive)
    return {"kind": kind, "hits": hits, "suppressed": suppressed,
            "first_run": baseline_missing, "scanned": len(rows)}
