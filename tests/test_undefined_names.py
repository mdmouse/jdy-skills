# -*- coding: utf-8 -*-
"""静态检查：函数里用到的名字必须真的定义过。

存在的理由：两次事故都是「批量替换悄悄没匹配上」——
`.replace()` 找不到目标不会报错，于是用到常量的那两行改了、
import 那行没改，`export.py` 变成一跑就 NameError。
226 个单元测试全绿，因为没有一个测试真的调用过 export.py 的主流程。

编译检查也拦不住：NameError 是运行时的。所以这里做一次作用域分析，
把「读了但从没定义过」的名字挑出来。它不需要执行代码，也就不需要网络和密钥。
"""
import ast
import builtins
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILTINS = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__spec__",
                                 "__package__", "__builtins__"}


def _sources():
    """所有一手源码。跳过 skills/*/scripts/_shared/（build.py 拷进去的副本）。"""
    for base in [os.path.join(ROOT, "_shared")] + [
            os.path.join(ROOT, "skills", d, "scripts")
            for d in sorted(os.listdir(os.path.join(ROOT, "skills")))]:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                yield os.path.join(base, name)


SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _walk_scope(node):
    """本作用域内的节点。遇到嵌套函数/类就停在它本身，不往里走——
    否则方法体里的 self 会被当成外层的名字（第一版就栽在这）。"""
    def walk(n):
        yield n
        if isinstance(n, SCOPE):
            return
        for c in ast.iter_child_nodes(n):
            for x in walk(c):
                yield x
    for child in ast.iter_child_nodes(node):
        for x in walk(child):
            yield x


def _bound_names(node):
    """本作用域里被绑定的名字：形参、赋值、for、with、except、import、
    嵌套函数/类名、global/nonlocal。

    不考虑先后顺序——Python 的规则就是「函数里任何地方赋值即为局部」。
    """
    names = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = node.args
        for arg in list(getattr(a, "posonlyargs", [])) + list(a.args) + list(a.kwonlyargs):
            names.add(arg.arg)
        for extra in (a.vararg, a.kwarg):
            if extra:
                names.add(extra.arg)
    for sub in _walk_scope(node):
        if isinstance(sub, SCOPE):
            if not isinstance(sub, ast.Lambda):
                names.add(sub.name)           # 嵌套函数/类：只绑定它的名字
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            names.update(sub.names)
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            names.add(sub.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
    return names


def _scopes(node, parent_names, out):
    """逐层进入作用域，把 Load 的名字对着作用域链核对。"""
    here = parent_names | _bound_names(node)
    for sub in _walk_scope(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            if sub.id not in here and sub.id not in BUILTINS:
                out.append((sub.lineno, sub.id))
    for sub in _walk_scope(node):
        if isinstance(sub, SCOPE):
            _scopes(sub, here, out)


class TestNoUndefinedNames(unittest.TestCase):

    def test_every_name_is_defined(self):
        problems = []
        for path in _sources():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            found = []
            _scopes(tree, BUILTINS, found)
            for lineno, name in found:
                problems.append("%s:%d  用到了未定义的名字 %s"
                                % (os.path.relpath(path, ROOT), lineno, name))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_catches_the_export_bug(self):
        """自检：这个检查真的能抓到那次的 bug 形状吗"""
        src = ("from jdy_client import JdyClient\n"
               "def main():\n"
               "    return {EXPORT_ID_COLUMN: 1}\n")
        found = []
        _scopes(ast.parse(src), BUILTINS, found)
        self.assertEqual([n for _, n in found], ["EXPORT_ID_COLUMN"])


class TestEveryTestFileActuallyRuns(unittest.TestCase):
    """测试文件必须真的会跑。

    README 教的跑法是 `for t in tests/test_*.py; do python3 "$t"; done`，
    而这个跑法有两个安静的失效方式，两个都真的发生过：

      · 文件里根本没有 `unittest.main()` —— 跑起来零输出、退出码 0，看着全绿；
        本仓库 14 个测试文件里有 6 个是这样，包括写入白名单和规模闸门的测试。
      · `unittest.main()` 写在文件中间 —— 它下面的测试类还没被定义，
        永远不会被收集；另有 5 个文件是这样。

    两种情况下"测试通过"都只是**没有测试**。
    """

    def test_runner_exists_and_is_last(self):
        problems = []
        for name in sorted(os.listdir(os.path.join(ROOT, "tests"))):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            path = os.path.join(ROOT, "tests", name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            runner = [n.lineno for n in tree.body
                      if isinstance(n, ast.If) and ast.dump(n.test).find("__main__") >= 0]
            classes = [n.lineno for n in tree.body if isinstance(n, ast.ClassDef)]
            if not runner:
                problems.append("%s 没有 `if __name__ == \"__main__\": unittest.main()`"
                                "——直接跑它是零输出、退出码 0" % name)
                continue
            after = [ln for ln in classes if ln > runner[-1]]
            if after:
                problems.append("%s 的 unittest.main() 在第 %d 行，后面还有 %d 个测试类"
                                "（第 %s 行）永远不会被收集"
                                % (name, runner[-1], len(after),
                                   "、".join(str(ln) for ln in after)))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
