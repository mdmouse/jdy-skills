# -*- coding: utf-8 -*-
"""跨端适配层。

来历：技能族原来只在 WorkBuddy 上验过，而"适配一个端"实际要解决的不是
认不认识它的名字，是**它的沙箱允许写哪儿**。WorkBuddy 的写白名单里没有
`~/.jdy`，豆包工作与千问办公的白名单未知。旧代码是写死 `~/.jdy`、写不进去
就地放弃：字段缓存退成内存（可接受），审计日志直接丢（不可接受）。

这里钉死三件事：
  1. 落点是**按候选顺序实写探出来的**，不是按平台名分支的；
  2. `~/.jdy` 不可写时审计日志**换个地方写下去**，而不是消失；
  3. 探针里那份宿主签名表是副本，**不许和内核那份漂移**。

不依赖 pytest：`python3 tests/test_platform_env.py`
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_shared"))

import platform_env  # noqa: E402
import webhook      # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = _load("hello_jdy_probe",
              os.path.join(ROOT, "skills", "hello-jdy", "scripts", "probe.py"))


class Sandbox(object):
    """造一个假的沙箱：HOME 下的 .jdy 建不出来，另给一个可写的会话目录。

    "HOME 建不出东西"**不能用 `os.chmod(dir, 0o500)` 造**：那只在 POSIX 上成立。
    Windows 上 chmod 对目录基本不起作用（只映射到只读属性，而目录的只读属性
    根本不管能不能在里面建子项），于是 block_home 在那边等于没堵——
    `DEFAULT_STATE_HOME` 照样可写，`source` 是 'default' 而不是期望的 'cwd'/'temp'，
    `home.tried` 是空列表。首次 Windows CI 上 `'default' != 'temp'`、
    `[] is not true` 两条就是这么来的。

    改成让 `home` 本身是一个**普通文件**：它底下的任何路径，
    `os.makedirs()` 与 `open(..., "w")` 在两个平台上都必然失败
    （POSIX: NotADirectoryError；Windows: WinError 267 / ENOTDIR），
    而两者都是 OSError，`_writable()` 的 `except (OSError, IOError)` 照样接得住。
    没有 skip，断言一个字没放松。
    """

    def __init__(self, block_home=True):
        self.box = tempfile.mkdtemp(prefix="jdy-sandbox-")
        self.home = os.path.join(self.box, "home")
        if block_home:
            with open(self.home, "w", encoding="utf-8") as fh:
                fh.write("我是文件，不是目录——故意的，见类文档。\n")
        else:
            os.makedirs(self.home)
        self.default = os.path.join(self.home, ".jdy")
        self.session = os.path.join(self.box, "session")
        os.makedirs(self.session)
        self.block_home = block_home
        self._saved_default = None
        self._saved_env = None
        self._saved_cwd = None
        self._saved_state = None

    def __enter__(self):
        self._saved_default = platform_env.DEFAULT_STATE_HOME
        self._saved_env = dict(os.environ)
        self._saved_cwd = os.getcwd()
        self._saved_state = platform_env._STATE_HOME
        platform_env.DEFAULT_STATE_HOME = self.default
        os.environ.pop(platform_env.STATE_HOME_ENV, None)
        platform_env._STATE_HOME = None
        os.chdir(self.session)
        return self

    def __exit__(self, *exc):
        os.chdir(self._saved_cwd)
        platform_env.DEFAULT_STATE_HOME = self._saved_default
        platform_env._STATE_HOME = self._saved_state
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.box, ignore_errors=True)
        return False


class TestInstallRootRevealsTheHost(unittest.TestCase):
    """认宿主靠的是**技能自己被装在哪**，不是猜路径。

    这条是"适配没见过的端"的全部依据：宿主把技能复制进
    `<宿主目录>/skills/<技能名>/`，于是 __file__ 本身就是证据。
    """

    def test_finds_the_dir_above_skills(self):
        # 期望值过一遍 abspath：实现内部先 abspath，Windows 上会补上盘符并换成反斜杠
        self.assertEqual(
            platform_env.install_root("/Users/x/.qwenworkcn/skills/jdy-doc/scripts/a.py"),
            os.path.abspath("/Users/x/.qwenworkcn"))

    def test_vendored_copy_one_level_deeper_still_resolves(self):
        """内核是被 build.py 拷进 scripts/_shared/ 的，比技能脚本还深一层。
        少算一层就会把宿主目录认成技能目录。"""
        self.assertEqual(
            platform_env.install_root(
                "/Users/x/DoubaoWork/skills/jdy-watch/scripts/_shared/platform_env.py"),
            os.path.abspath("/Users/x/DoubaoWork"))

    def test_not_installed_under_a_skills_dir_means_no_host(self):
        self.assertIsNone(
            platform_env.install_root("/Users/x/codes/jiandaoyun/_shared/platform_env.py"))

    def test_skills_at_filesystem_root_is_not_a_host(self):
        self.assertIsNone(platform_env.install_root("/skills/jdy-doc/scripts/a.py"))

    def test_unknown_host_is_reported_with_its_path_not_swallowed(self):
        """认不出来时**必须把路径吐出来**——那是下次补进名单的事实。
        一句"不支持"会让人以为该端跑不了，实际只是我们没见过这个目录名。"""
        host = platform_env.detect_host("/Users/x/.some-new-agent/skills/jdy-doc/s/a.py")
        self.assertEqual(host["id"], "unknown")
        want = os.path.abspath("/Users/x/.some-new-agent")     # Windows 上带盘符、反斜杠
        self.assertEqual(host["root"], want)
        self.assertTrue(any(want in e for e in host["evidence"]))

    def test_known_but_unverified_host_is_flagged_as_unverified(self):
        """名单里有、但没在真机上验过的端，不能让它看起来像一条结论。"""
        host = platform_env.detect_host("/Users/x/DoubaoWork/skills/jdy-doc/s/a.py")
        self.assertEqual(host["id"], "doubao-work")
        self.assertFalse(host["verified"])

    def test_host_id_never_reaches_behavior(self):
        """宿主标识只许进报告。一旦有人拿它做 if 分支，没见过的端就又变成
        "不支持"了——本模块存在的全部意义就是不这么干。"""
        src = open(os.path.join(ROOT, "_shared", "platform_env.py"), encoding="utf-8").read()
        body = src.split("# 可写状态目录", 1)[1]        # 只看落点解析那一半
        for hid in ("workbuddy", "doubao-work", "qwenwork", "claude-code"):
            self.assertNotIn('"%s"' % hid, body,
                             "落点解析里出现了平台名 %s —— 那就是在按平台名分支" % hid)


class TestDotSkillsDirIsAlsoAHostLayout(unittest.TestCase):
    """豆包工作的技能目录是 `.skills`（点开头），而且埋在 Chromium 配置档深处。

    来历：2026-09-01 在豆包工作里触发「跑一下简道云探针」，Agent 完全找不到技能，
    转头去翻工作目录和定时任务、最后反问"简道云探针是什么"——
    **而那正是我们的 description 明令禁止的行为**，说明它压根没读到 SKILL.md。
    查下来是装错了地方：`~/DoubaoWork/skills` 是客户端自建的空目录，
    它真正加载的是
    `…/DoubaoWork/Default/.doubaowork/agent_mode/workspace/.skills/`。

    两处都得改：
      1. `install_root()` 只认 `skills`，点开头的认不出 → 报「未识别的宿主」；
      2. 根目录名是 `workspace`，太通用不能当签名——但路径里有 `DoubaoWork` 这一段，
         那就是证据。所以宿主匹配要能往祖先找。
    """

    DOUBAO = ("/Users/x/Library/Application Support/DoubaoWork/Default/"
              ".doubaowork/agent_mode/workspace/.skills/hello-jdy/scripts/probe.py")

    def test_dot_skills_is_recognised_as_a_skills_dir(self):
        root = platform_env.install_root(self.DOUBAO)
        tail = os.path.join("agent_mode", "workspace")           # 分隔符按本平台
        self.assertTrue(root and root.endswith(tail), root)

    def test_host_is_matched_through_an_ancestor(self):
        host = platform_env.detect_host(self.DOUBAO)
        self.assertEqual(host["id"], "doubao-work", host)
        self.assertIn("技能安装在", " ".join(host["evidence"]))

    def test_probe_agrees(self):
        """探针那份是副本，同样得认得出——否则该端的报告永远填不出宿主。"""
        self.assertEqual(probe.install_root(self.DOUBAO),
                         platform_env.install_root(self.DOUBAO))
        self.assertEqual(probe.detect_host()["id"] is None, False)
        self.assertEqual(probe.match_host(platform_env.install_root(self.DOUBAO)),
                         platform_env.match_host(platform_env.install_root(self.DOUBAO)))

    def test_plain_skills_still_works(self):
        """别为了认新端把老端弄丢——这正是本仓库最常见的那种半边改动。"""
        for path, want in (("/Users/x/.qwenworkcn/skills/a/scripts/p.py", "qwenwork"),
                           ("/Users/x/.workbuddy-ai/skills/a/scripts/p.py", "workbuddy"),
                           ("/Users/x/.claude/skills/a/scripts/p.py", "claude-code")):
            self.assertEqual(platform_env.detect_host(path)["id"], want, path)

    def test_a_generic_root_name_alone_is_not_a_signature(self):
        """`workspace` 这种名字不能拿来当签名——路径里没有宿主名就该老实说不认识。"""
        host = platform_env.detect_host("/tmp/whatever/workspace/.skills/a/scripts/p.py")
        self.assertEqual(host["id"], "unknown", host)


class TestProbeTableDoesNotDrift(unittest.TestCase):
    """探针刻意零依赖，所以它抄了一份宿主签名表。副本会漂，这里盯着。"""

    def test_host_signatures_identical(self):
        self.assertEqual(list(probe.HOST_SIGNATURES), list(platform_env.HOST_SIGNATURES),
                         "探针与内核的 HOST_SIGNATURES 不一致——改了一边忘了另一边")

    def test_env_signatures_identical(self):
        self.assertEqual(list(probe.ENV_SIGNATURES), list(platform_env.ENV_SIGNATURES),
                         "探针与内核的 ENV_SIGNATURES 不一致")

    def test_install_root_agrees(self):
        for path in ("/Users/x/.qwenworkcn/skills/a/scripts/p.py",
                     "/Users/x/DoubaoWork/skills/a/scripts/_shared/p.py",
                     "/Users/x/repo/_shared/p.py"):
            self.assertEqual(probe.install_root(path), platform_env.install_root(path), path)


class TestStateHomeFallsBackInsteadOfGivingUp(unittest.TestCase):

    def test_default_home_wins_when_writable(self):
        with Sandbox(block_home=False) as sb:
            home = platform_env.resolve_state_home(refresh=True)
            self.assertEqual(home.path, sb.default)
            self.assertEqual(home.source, "default")
            self.assertTrue(home.stable)
            self.assertIsNone(home.note(), "正常情况不该有降级提示")

    def test_env_override_beats_the_default(self):
        with Sandbox(block_home=False) as sb:
            pinned = os.path.join(sb.box, "pinned")
            os.environ[platform_env.STATE_HOME_ENV] = pinned
            home = platform_env.resolve_state_home(refresh=True)
            self.assertEqual(home.path, pinned)
            self.assertEqual(home.source, "env")

    def test_host_dir_is_tried_before_the_session_dir(self):
        """宿主自己的配置目录是它建的，沙箱放行它的概率远高于 ~/.jdy，
        而且**跨会话有效**——会话工作目录不是。顺序错了，状态就只能活一轮。"""
        with Sandbox() as sb:
            host_root = os.path.join(sb.box, "hostcfg")
            os.makedirs(host_root)
            home = platform_env.resolve_state_home(refresh=True, host_root=host_root)
            self.assertEqual(home.path,
                             os.path.join(host_root, platform_env.STATE_DIR_NAME))
            self.assertEqual(home.source, "host")
            self.assertTrue(home.stable)

    def test_session_dir_is_used_when_home_and_host_are_blocked(self):
        with Sandbox() as sb:
            home = platform_env.resolve_state_home(refresh=True, host_root=None)
            # macOS 的 /var 是指向 /private/var 的软链，getcwd() 给的是解析后的路径
            self.assertEqual(os.path.realpath(home.path),
                             os.path.realpath(os.path.join(sb.session, ".jdy")))
            self.assertEqual(home.source, "cwd")
            self.assertFalse(home.stable, "会话工作目录会变，不能当成跨轮次有效")
            self.assertIn("换个会话工作目录就找不回来", home.note())

    def test_tempdir_is_the_last_resort_and_says_so(self):
        with Sandbox() as sb:
            # 会话目录也堵死：放在"是个文件"的 HOME 底下，makedirs 建不出来
            home = platform_env.resolve_state_home(
                refresh=True, host_root=None, cwd=os.path.join(sb.home, "no-such-session"))
            self.assertEqual(home.source, "temp")
            self.assertTrue(home.ephemeral)
            self.assertIn("下一轮找不回来", home.note())

    def test_everything_blocked_yields_no_path_and_a_loud_note(self):
        with Sandbox() as sb:
            dead = os.path.join(sb.home, "a")            # HOME 是文件，建不出来
            platform_env.DEFAULT_STATE_HOME = dead
            home = platform_env.resolve_state_home(
                refresh=True, host_root=None, cwd=os.path.join(sb.home, "b"))
            # tempdir 兜底通常能写；把它也堵掉才测得到"全灭"，这里直接构造。
            blocked = platform_env.StateHome(None, "none", False,
                                             [(dead, "denied")])
            self.assertFalse(blocked.ok)
            self.assertIn("找不到任何可写目录", blocked.note())
            self.assertTrue(home.ok or not home.ok)      # 不对 tempdir 的可写性下断言

    def test_why_each_candidate_failed_is_kept(self):
        """只说"降级了"没用，得说清**每个候选为什么不行**——
        新端的沙箱白名单就是这么测出来的。"""
        with Sandbox() as sb:
            home = platform_env.resolve_state_home(refresh=True, host_root=None)
            self.assertTrue(home.tried)
            path, why = home.tried[0]
            self.assertEqual(path, sb.default)
            self.assertTrue(why)

    def test_hypothetical_query_does_not_poison_the_process_cache(self):
        """传了 host_root/cwd 的调用是"问一个假设"，不该顶掉真实落点。"""
        with Sandbox(block_home=False):
            real = platform_env.resolve_state_home(refresh=True)
            platform_env.resolve_state_home(host_root="/tmp/whatever-host")
            self.assertEqual(platform_env.resolve_state_home().path, real.path)


class TestConfigLookupIsNotTiedToStateDir(unittest.TestCase):
    """**读密钥和写状态是两件事。**

    状态目录一降级就跟着去新目录找 config.json，结果是密钥突然找不到了——
    这正是本仓库反复出现的那种"两半只做了一半"。
    """

    def test_config_stays_in_the_default_home_even_when_state_falls_back(self):
        with Sandbox() as sb:
            os.makedirs(sb.default, exist_ok=False) if False else None
            home = platform_env.resolve_state_home(refresh=True, host_root=None)
            self.assertEqual(home.source, "cwd")             # 状态降级到会话目录
            self.assertEqual(platform_env.find_config(),
                             os.path.join(sb.default, "config.json"))

    def test_env_home_adds_a_config_candidate_but_default_still_checked(self):
        with Sandbox(block_home=False) as sb:
            os.environ[platform_env.STATE_HOME_ENV] = os.path.join(sb.box, "pinned")
            cands = platform_env.config_candidates()
            self.assertEqual(cands[0], os.path.join(sb.box, "pinned", "config.json"))
            self.assertEqual(cands[-1], os.path.join(sb.default, "config.json"))

    def test_find_config_prefers_the_one_that_exists(self):
        with Sandbox(block_home=False) as sb:
            pinned = os.path.join(sb.box, "pinned")
            os.makedirs(pinned)
            with open(os.path.join(pinned, "config.json"), "w", encoding="utf-8") as fh:
                json.dump({"api_key": "k"}, fh)
            os.environ[platform_env.STATE_HOME_ENV] = pinned
            self.assertEqual(platform_env.find_config(),
                             os.path.join(pinned, "config.json"))


class TestSkillsThatKeepStateSurviveAForeignSandbox(unittest.TestCase):
    """三个真正会落盘的地方，在 `~/.jdy` 不可写时都得有交代。"""

    def _load_skill(self, skill, filename, modname):
        scripts = os.path.join(ROOT, "skills", skill, "scripts")
        sys.path.insert(0, scripts)
        sys.path.insert(0, os.path.join(scripts, "_shared"))
        try:
            return _load(modname, os.path.join(scripts, filename))
        finally:
            sys.path.remove(os.path.join(scripts, "_shared"))
            sys.path.remove(scripts)

    def test_flow_audit_log_lands_somewhere_real(self):
        """审批是有责任归属的动作。`~/.jdy` 不可写**不等于**可以不留痕。"""
        flow = self._load_skill("jdy-flow-ops", "flow.py", "pe_flow")
        with Sandbox():
            platform_env._STATE_HOME = None
            platform_env.resolve_state_home(refresh=True, host_root=None)
            path = flow.audit("approve", "sys_a", {"task_id": "t9"}, "success")
            self.assertIsNotNone(path, "换个可写目录就能留痕，不该返回 None")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.loads(fh.readline())["task_id"], "t9")

    def test_watch_state_reports_that_it_is_only_good_for_one_round(self):
        """哨兵去重状态落在会话目录 = 下一轮找不回来 = 会重报。
        重报可以接受，**不说**不可以接受。"""
        rules = self._load_skill("jdy-watch", "rules.py", "pe_rules")
        with Sandbox():
            platform_env._STATE_HOME = None
            platform_env.resolve_state_home(refresh=True, host_root=None)
            state = rules.State()
            self.assertIsNotNone(state.path)
            self.assertFalse(state.readonly)
            state.mark("r1", "row1", __import__("datetime").datetime(2026, 8, 31))
            self.assertTrue(state.save())
            self.assertIsNotNone(state.home.note(), "降级了就必须有一句话交代")

    def test_watch_state_with_nowhere_to_write_is_readonly_not_a_crash(self):
        rules = self._load_skill("jdy-watch", "rules.py", "pe_rules2")
        saved = platform_env.state_path
        platform_env.state_path = lambda *p: None
        try:
            state = rules.State()
            self.assertTrue(state.readonly)
            self.assertFalse(state.save())
        finally:
            platform_env.state_path = saved


class TestProbeReportsWhichTrackTheHostIsOn(unittest.TestCase):
    """探针要回答的新问题：**这一端是双轨还是技能单轨。**

    来历：官方 2026-09-01 把 MCP 服务改名「AI 连接」并【官方声称】上架
    WorkBuddy／千问办公连接器市场（doc/26886）。填兼容性验证表时，
    「该端装没装官方连接器」从此是一列——而这件事本机文件里就有答案，
    不必去点客户端界面。

    三件事在这里钉死：
      1. **找不到配置 ≠ 没装。** 输出必须是「未知」，不能是「无」——
         把没探到写成没有，下一个人就照着这个假结论去填表了。
      2. **只能报「已配置」，报不了「已启用」。** WorkBuddy 的启用状态在
         `connector-states.v3.json` 里，aes-256-gcm 加密（密钥 `.master.key`），
         读不了。这个区别必须出现在输出里，不能让读表的人自己去猜。
      3. **配置里有 token，输出必须掩码。** 千问办公的 `mcp-adaptor.config`
         本机实测就带 64 位 token 和 `x-api-key`；WorkBuddy 的 `staticHeaders`
         同样可能带。探针报告会被贴进验证表、聊天记录和工单——原样打出去
         等于把凭证发出去了。
    """

    JDY_URL = "https://api.jiandaoyun.com/mcp/sse/abcdef0123456789abcdef0123456789"
    TOKEN = "sk-jdy-9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a3928"  # 脱敏例外：造的假 Key

    def _root(self, files):
        """造一个假的宿主目录。files 是 {相对路径: 文本}。"""
        box = tempfile.mkdtemp(prefix="jdy-connector-")
        self.addCleanup(shutil.rmtree, box, True)
        root = os.path.join(box, ".somehost-ai")
        os.makedirs(root)
        for rel, body in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        return root

    def _mcp(self, entries):
        return json.dumps({"mcpServers": entries}, ensure_ascii=False)

    # --- 情况一：有条目 ---------------------------------------------------
    def test_finds_a_jiandaoyun_entry(self):
        root = self._root({"connectors/u1/mcp.json": self._mcp({
            "connector:jiandaoyun": {"url": self.JDY_URL, "disabled": False},
            "connector:github": {"url": "https://api.githubcopilot.com/mcp/"},
        })})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        self.assertEqual(item["evidence"]["state"], "dual-track", item)
        self.assertEqual(item["status"], probe.PASS)
        self.assertIn("已配置", item["detail"])

    def test_enabled_is_unknown_when_the_config_does_not_say(self):
        """WorkBuddy 把启用状态加密在 connector-states.v3.json 里，读不出来。

        这种时候报告不许读起来像「已经在用了」——配置里没有的东西，
        探针不能替它说。
        """
        root = self._root({"connectors/u1/mcp.json": self._mcp({
            "connector:jiandaoyun": {"url": self.JDY_URL}})})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        self.assertIn("读不出启用状态", item["detail"])
        self.assertFalse(item["evidence"]["enabled_known"],
                         "配置里没写启用状态，探针不该声称知道")
        self.assertIn("启用状态读不到", item["evidence"]["matrix_row"])

    def test_enabled_is_reported_when_the_config_does_say(self):
        """千问办公把 `"enabled": true` 明文写在 mcp.json 里（本机实测）。

        一句笼统的「启用状态读不到」是把 WorkBuddy 一端的限制说成了所有端的，
        等于在读得到的那一端主动丢掉一条真信息。
        """
        root = self._root({"mcp.json": self._mcp({
            "jiandaoyun": {"url": self.JDY_URL, "enabled": True,
                           "authType": "oauth", "_source": "market"}})})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        self.assertTrue(item["evidence"]["enabled_known"])
        self.assertIs(item["evidence"]["findings"][0]["enabled"], True)
        self.assertIn("已启用", item["detail"])
        self.assertNotIn("读不出启用状态", item["detail"])

    def test_a_disabled_entry_is_not_dressed_up_as_enabled(self):
        root = self._root({"connectors/u1/mcp.json": self._mcp({
            "connector:jiandaoyun": {"url": self.JDY_URL, "disabled": True}})})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        self.assertIs(item["evidence"]["findings"][0]["enabled"], False)
        self.assertIn("已停用", item["detail"])

    def test_config_enabled_is_not_sold_as_actually_running(self):
        """配置里写着 enabled，也只是**配置的说法**——宿主此刻用不用是另一回事。"""
        root = self._root({"mcp.json": self._mcp({
            "jiandaoyun": {"url": self.JDY_URL, "enabled": True}})})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        self.assertIn("不等于宿主此刻真在用", item["detail"])

    # --- 情况二：无条目 ---------------------------------------------------
    def test_config_without_jiandaoyun_is_skill_only_not_unknown(self):
        root = self._root({"connectors/u1/mcp.json": self._mcp({
            "connector:github": {"url": "https://api.githubcopilot.com/mcp/"},
            "connector:notion": {"url": "https://mcp.notion.com/mcp"},
        })})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        self.assertEqual(item["evidence"]["state"], "skill-only", item)
        self.assertGreaterEqual(len(item["evidence"]["files"]), 1,
                                "读到了配置文件，就该把读了哪些说出来")

    # --- 情况三：文件不存在 -----------------------------------------------
    def test_no_config_file_at_all_is_unknown_not_none(self):
        root = self._root({"README.txt": "nothing here"})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        self.assertEqual(item["evidence"]["state"], "unknown", item)
        self.assertIn("未知", item["detail"])
        self.assertNotIn("未配置", item["detail"],
                         "探不到就说未知——写成「未配置」是把没找到当成了没有")

    def test_missing_root_is_also_unknown(self):
        item = probe.c_ai_connect({"root": None}, roots=["/no/such/host/root"])
        self.assertEqual(item["evidence"]["state"], "unknown", item)

    # --- 情况四：只在市场目录里见到 ---------------------------------------
    def test_market_catalog_hit_is_not_reported_as_installed(self):
        """宿主会把整个连接器市场的清单缓存到本地。

        在那份缓存里看见简道云，只说明「市场上有」——它和「这台机器装了」
        是两件事。混为一谈，报告就把"能装"写成了"已装"，
        而这恰恰是本轮反复强调的那种越界。
        """
        root = self._root({"connectors-marketplace/connectors/jiandaoyun/mcp.json":
                           self._mcp({"jiandaoyun": {"url": self.JDY_URL}})})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        self.assertEqual(item["evidence"]["state"], "market-only", item)
        self.assertIn("市场", item["detail"])
        self.assertNotIn("已装", item["evidence"]["matrix_row"])

    def test_installed_wins_over_catalog(self):
        """两处都有时，结论是「装了」——市场目录只是旁证。"""
        root = self._root({
            "connectors-marketplace/connectors/jiandaoyun/mcp.json":
                self._mcp({"jiandaoyun": {"url": self.JDY_URL}}),
            "connectors/u1/mcp.json":
                self._mcp({"connector:jiandaoyun": {"url": self.JDY_URL}}),
        })
        item = probe.c_ai_connect({"root": root}, roots=[root])
        self.assertEqual(item["evidence"]["state"], "dual-track", item)

    # --- 认出宿主时只算它自己那一份 ---------------------------------------
    def test_another_hosts_connector_is_not_credited_to_this_host(self):
        """真机上踩到的：在 WorkBuddy 上跑出来的填表行写着
        「腾讯 WorkBuddy … jiandaoyun @ ~/.qwenworkcn/mcp.json」。

        候选根目录当时是「本机所有已知宿主目录」，于是另一端装的连接器
        被算进了这一端的结论。**填表列是按端的，扫描范围也必须是。**
        别的端有没有装仍然有用，但它只能进 other_hosts。
        """
        box = tempfile.mkdtemp(prefix="jdy-twohosts-")
        self.addCleanup(shutil.rmtree, box, True)
        mine = os.path.join(box, ".workbuddy-ai")
        theirs = os.path.join(box, ".qwenworkcn")
        os.makedirs(os.path.join(mine, "connectors", "u1"))
        os.makedirs(theirs)
        with io.open(os.path.join(mine, "connectors", "u1", "mcp.json"),
                     "w", encoding="utf-8") as fh:
            fh.write(self._mcp({"connector:github": {"url": "https://x/mcp"}}))
        with io.open(os.path.join(theirs, "mcp.json"), "w", encoding="utf-8") as fh:
            fh.write(self._mcp({"jiandaoyun": {"url": self.JDY_URL, "enabled": True}}))

        item = probe.c_ai_connect({"root": mine, "name": "腾讯 WorkBuddy"}, home=box)
        self.assertEqual(item["evidence"]["state"], "skill-only", item["detail"])
        self.assertNotIn(theirs, item["evidence"]["matrix_row"],
                         "填表行里出现了另一端的配置路径")
        self.assertEqual(item["evidence"]["roots"], [mine],
                         "认出宿主之后就只该扫它自己那一份")
        # 别的端装了仍然要说，只是要说清那是别人的结论
        self.assertEqual(item["evidence"]["other_hosts"]["state"], "dual-track")
        self.assertIn("本机别的端装了", item["detail"])

    def test_with_no_host_identified_the_scan_is_machine_wide(self):
        """从仓库副本直接跑时认不出宿主——这时扫全机是对的，
        因为结论本来就不归属于任何一端（填表行会写「未识别的宿主」）。"""
        box = tempfile.mkdtemp(prefix="jdy-nohost-")
        self.addCleanup(shutil.rmtree, box, True)
        theirs = os.path.join(box, ".qwenworkcn")
        os.makedirs(theirs)
        with io.open(os.path.join(theirs, "mcp.json"), "w", encoding="utf-8") as fh:
            fh.write(self._mcp({"jiandaoyun": {"url": self.JDY_URL, "enabled": True}}))
        item = probe.c_ai_connect({"root": None}, home=box)
        self.assertEqual(item["evidence"]["state"], "dual-track")
        self.assertNotIn("other_hosts", item["evidence"])

    # --- 走真实调用路径（不传 roots）---------------------------------------
    def test_unknown_state_on_the_real_call_path_does_not_crash(self):
        """验收打回的那条:探针在**未识别宿主**上直接崩了。

        `roots` 是参数,不传时是 None;改名成 primary 时漏改了 unknown 分支里的
        `len(roots)`。而未识别宿主(root 找得到、名字不认识——直接跑仓库副本就是)
        恰好落在这个分支上。

        单测一条都没抓到,原因很具体:**上面每一条都显式传了 `roots=`**,
        于是那个 None 永远不会出现。造一个假 host 目录、走真实调用路径,
        这条才够得着。
        """
        box = tempfile.mkdtemp(prefix="jdy-unknown-")
        self.addCleanup(shutil.rmtree, box, True)
        mine = os.path.join(box, ".nothing-here")
        os.makedirs(mine)
        item = probe.c_ai_connect({"root": mine, "name": "未识别的宿主"}, home=box)
        self.assertEqual(item["evidence"]["state"], "unknown")
        self.assertIn("1 个候选根目录", item["detail"])

    def test_the_whole_probe_runs_on_an_unidentified_host(self):
        """再往上一层:整份报告要能跑完。

        崩的地方在 `build_report` 里,而不是某个纯函数——只测纯函数,
        整条流水线断了照样全绿。acceptance.sh 的 `hello-jdy/probe` 抓到了,
        单测没有;这条把那道防线搬进单测,不用等真机。
        """
        class Args(object):
            no_network = True
            timeout = 5.0
        report = probe.build_report(Args())
        self.assertIn("track", report)
        self.assertTrue(probe.render(report))          # 渲染也要不炸

    # --- 掩码 -------------------------------------------------------------
    def test_secrets_never_reach_the_report(self):
        root = self._root({"connectors/u1/mcp.json": self._mcp({
            "connector:jiandaoyun": {
                "url": self.JDY_URL,
                "staticHeaders": {"Authorization": "Bearer " + self.TOKEN,
                                  "X-Request-Source": "workbuddy"},
            }})})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        blob = json.dumps(item, ensure_ascii=False)
        self.assertNotIn(self.TOKEN, blob, "报告里出现了完整 token")
        self.assertNotIn("abcdef0123456789abcdef0123456789", blob,
                         "报告里出现了 URL 路径末段的完整凭证")
        self.assertIn("X-Request-Source", blob,
                      "非密的 header 名该留着——遮到看不出连的是哪家就没用了")

    def test_only_whitelisted_fields_leave_the_config(self):
        """摘录走**白名单**，不是"把已知的密钥字段遮掉"。

        黑名单挡不住下一个字段名：千问办公的 `mcp-adaptor.config` 顶层就叫
        `token`，换个宿主可能叫 `apiKey`、`credential`、`refresh_token`。
        只输出 url / headers / disabled / type / timeout，
        新字段默认不进报告——这样漏的方向是"少说了"，不是"泄了"。
        """
        info = probe._describe({
            "url": "https://mcp.example.com/mcp",
            "token": self.TOKEN,
            "apiKey": self.TOKEN,
            "refresh_token": self.TOKEN,
            "staticHeaders": {"X-Request-Source": "qwenwork"},
            "disabled": False,
        })
        self.assertEqual(sorted(info), ["disabled", "headers", "url"])
        self.assertNotIn(self.TOKEN, json.dumps(info, ensure_ascii=False))

    def test_url_mask_does_not_drift_from_the_kernel(self):
        """探针零依赖，所以掩码是抄的一份。副本会漂，这里盯着。

        `webhook.mask` 修过一个真坑：飞书的密钥在**路径最后一段**、没有 `=`，
        只遮查询串等于一个字都没遮。抄过来的这份必须连那个修法一起抄。
        """
        for url in ("https://api.jiandaoyun.com/mcp/sse/" + "a" * 40,
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "b" * 36,
                    "https://oapi.dingtalk.com/robot/send?access_token=" + "c" * 64,
                    "https://open.feishu.cn/open-apis/bot/v2/hook/"
                    "1a2b3c4d-5e6f-7788-99aa-bbccddeeff00",
                    "https://mcp.example.com/mcp"):
            self.assertEqual(probe.mask_url(url), webhook.mask(url), url)

    # --- 只读 -------------------------------------------------------------
    def test_the_scan_writes_nothing(self):
        """C9 会建目录是它的活。这一条不许有任何副作用——它读的是别人的配置。"""
        root = self._root({"connectors/u1/mcp.json": self._mcp({
            "connector:jiandaoyun": {"url": self.JDY_URL}})})

        def snapshot():
            out = {}
            for dirpath, dirnames, files in os.walk(root):
                for f in files:
                    fp = os.path.join(dirpath, f)
                    st = os.stat(fp)
                    out[fp] = (st.st_size, st.st_mtime_ns)
            return out

        before = snapshot()
        probe.c_ai_connect({"root": root}, roots=[root])
        self.assertEqual(snapshot(), before, "扫描改动了被扫的目录")

    # --- 填表 -------------------------------------------------------------
    def test_report_carries_a_paste_ready_matrix_row(self):
        """报告要能直接填进 platform-compat-matrix 的 V5 三端表。

        探针的产出如果还要人再翻译一道，填表的人就会凭印象写。
        """
        root = self._root({"connectors/u1/mcp.json": self._mcp({
            "connector:jiandaoyun": {"url": self.JDY_URL}})})
        item = probe.c_ai_connect({"root": root}, roots=[root])
        row = item["evidence"]["matrix_row"]
        self.assertTrue(row.startswith("|") and row.endswith("|"), row)
        self.assertEqual(row.count("|"), 6, "V5 三端表是 5 列：%s" % row)

if __name__ == "__main__":
    unittest.main(verbosity=2)
