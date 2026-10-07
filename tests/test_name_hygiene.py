# -*- coding: utf-8 -*-
"""名字卫生检查（证据导向，专治今天踩过的两类事故）：

1) 使用了某个 `PERM_*` 常量但**没有导入它** —— 曾导致 help_content.py 在模块级抛 NameError，机器人启动即崩。
2) 引用了某个**模块级名字（全大写常量 / _ 开头的辅助函数）但全文件既未定义也未导入** ——
   曾导致 `_find_account_items is not defined`、`PLAYTIME_PER_HOUR` 缺失等运行期错误。
"""
import ast, builtins, os, sys, unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
BOT = os.path.join(REPO, "bot")


def module_files():
    return [os.path.join(BOT, f) for f in sorted(os.listdir(BOT)) if f.endswith(".py")]


class NameHygieneTest(unittest.TestCase):
    def test_PERM常量使用处必须已导入(self):
        bad = []
        for p in module_files():
            tree = ast.parse(open(p, encoding="utf-8").read())
            available = set()
            for n in ast.walk(tree):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:
                        available.add(a.asname or a.name.split(".")[0])
                # 本文件自己定义的常量也算可用（例如 permissions.py 自己定义 PERM_*）
                elif isinstance(n, ast.Assign):
                    for tg in n.targets:
                        if isinstance(tg, ast.Name):
                            available.add(tg.id)
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    available.add(n.target.id)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    available.add(n.name)
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and n.id.startswith("PERM_") and isinstance(n.ctx, ast.Load):
                    if n.id not in available:
                        bad.append("%s:%d 使用了 %s 但未导入" % (os.path.basename(p), n.lineno, n.id))
        self.assertEqual(bad, [], "未导入的权限常量（会导致 NameError）:\n  " + "\n  ".join(bad))

    def test_引用的模块级名字必须已定义或导入(self):
        allow = set(dir(builtins)) | {"self", "cls", "__name__", "__file__", "__doc__"}
        bad = []
        for p in module_files():
            src = open(p, encoding="utf-8").read()
            tree = ast.parse(src)
            defined = set()
            for n in ast.walk(tree):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:
                        defined.add(a.asname or a.name.split(".")[0])
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(n.name)
                elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    defined.add(n.id)
                elif isinstance(n, ast.arg):
                    defined.add(n.arg)
                elif isinstance(n, (ast.ExceptHandler,)) and n.name:
                    defined.add(n.name)
            # 只检查"像模块级符号"的名字，避免局部变量噪声
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    nm = n.id
                    looks_global = (nm.isupper() and len(nm) > 2) or nm.startswith("_")
                    if looks_global and nm not in defined and nm not in allow:
                        bad.append("%s:%d 引用 %s，但全文件未定义/未导入" % (os.path.basename(p), n.lineno, nm))
        self.assertEqual(bad, [], "可疑的未定义模块级引用（可能运行期 NameError）:\n  " + "\n  ".join(sorted(set(bad))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
