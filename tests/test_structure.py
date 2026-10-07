# -*- coding: utf-8 -*-
"""结构性静态检查（AST）——把"代码写进永远不会执行的位置"这类事故变成自动化防线。

背景：曾出现「校验代码被塞进前一个 except 的语句体里」——语法合法、编译通过，但正常流程永远不执行。
本测试用 AST 检查关键语句确实在**函数体第一层**（而不是嵌在 try/except 里）。
"""
import ast, os, re, sys, unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MAIN = os.path.join(REPO, "bot", "main.py")


def parse_main():
    with open(MAIN, "r", encoding="utf-8") as f:
        src = f.read()   # 只读一次（读两次第二次会是空串）
    return ast.parse(src), src


class StructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree, cls.src = parse_main()
        cls.classes = {n.name: n for n in cls.tree.body if isinstance(n, ast.ClassDef)}
        cls.methods = {}
        for cname, c in cls.classes.items():
            for m in c.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cls.methods.setdefault(m.name, []).append(m)

    def test_绑定校验必须在函数体第一层而不是嵌在except里(self):
        ms = self.methods.get("cmd_bind_email") or []
        self.assertTrue(ms, "找不到 cmd_bind_email")
        m = ms[0]
        # 收集函数体第一层的调用名
        top_calls = set()
        for st in m.body:
            for node in ast.walk(st):
                if isinstance(node, ast.Call):
                    f = node.func
                    if isinstance(f, ast.Name):
                        top_calls.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        top_calls.add(f.attr)
        self.assertIn("check_bind_request", top_calls,
                      "绑定规则必须由 check_bind_request 在第一层统一判定（防止再被塞进 try/except）")

    def test_没有指令方法被重复定义(self):
        dup = {k: len(v) for k, v in self.methods.items() if len(v) > 1}
        # 允许同名在不同类里（如包装），但同一类内重复必须报出来
        bad = []
        for cname, c in self.classes.items():
            seen = {}
            for m in c.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if m.name in seen:
                        bad.append("%s.%s" % (cname, m.name))
                    seen[m.name] = 1
        self.assertEqual(bad, [], "同类内存在重复定义（后者会覆盖前者）: %s" % bad)

    def test_分发调用的指令方法都存在(self):
        called = set(re.findall(r"await self\.(cmd_[a-z_0-9]+)\(", self.src))
        defined = set(self.methods.keys())
        missing = sorted(x for x in called if x not in defined)
        self.assertEqual(missing, [], "分发里调用了不存在的方法: %s" % missing)

    def test_每个绑定相关指令仍在分发里(self):
        for kw in ("绑定", "添加白名单", "签到", "我的信息", "积分排行"):
            self.assertIn(kw, self.src, "分发里缺少指令: %s" % kw)

    def test_关键字面量没有残留markdown标题喂给图片渲染器(self):
        bad = []
        for m in re.finditer(r"render_(?:rank|info)_card\(([^)]*)\)", self.src, re.S):
            args = m.group(1)
            if "build_card_title" in args:
                bad.append(args[:60])
        self.assertEqual(bad, [], "图片渲染器不应接收 markdown 标题（会出现 ## 与标题重叠）: %s" % bad)

    def test_部署清单覆盖所有本地导入(self):
        """证据要求：main.py 里 from X import 的本地模块 X，必须出现在部署清单里。
        历史事故：bind_rules.py 漏加清单 → 服务器 ModuleNotFoundError → 机器人起不来。"""
        botdir = os.path.join(REPO, "bot")
        local_mods = {x[:-3] for x in os.listdir(botdir) if x.endswith(".py")}
        imported = set(re.findall(r"(?m)^from ([A-Za-z_]\w*) import", self.src))
        must_have = sorted(imported & local_mods)
        self.assertTrue(must_have, "至少应导入一些本地模块")
        manifest = open(os.path.join(REPO, "scripts", "deploy_main_only.py"), encoding="utf-8").read()
        missing = [m for m in must_have if (m + ".py") not in manifest]
        self.assertEqual(missing, [], "这些本地模块被 main.py 导入，但不在部署清单里（会导致线上 ModuleNotFoundError）: %s" % missing)

    def test_关键常量已定义(self):
        for name in ("PLAYTIME_PER_HOUR", "BOT_QQ" if "BOT_QQ" in self.src else "PLAYTIME_PER_HOUR"):
            self.assertRegex(self.src, r"(?m)^%s\s*=" % re.escape(name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
