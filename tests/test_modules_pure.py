# -*- coding: utf-8 -*-
"""各模块纯函数/持久化测试：邮箱校验、原子写、种子库、状态存储、投票工具"""
import json, os, sys, tempfile, unittest

BOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
sys.path.insert(0, BOT)
from whitelist_mail import check_qq_email, check_name_ok
from permissions import atomic_write_json
from seeds import Seeds
from server_status_store import ServerStatusStore
from vote_store import generate_random_options


class PureHelpersTest(unittest.TestCase):
    def test_只接受纯数字qq邮箱(self):
        self.assertTrue(check_qq_email("1011819146@qq.com"))
        self.assertTrue(check_qq_email("3879157746@qq.com"))
        for bad in ("abc@qq.com", "1011819146@163.com", "1011819146", "@qq.com", "", "1011819146@qq"):
            self.assertFalse(check_qq_email(bad), bad)

    def test_玩家名校验返回布尔(self):
        self.assertIsInstance(check_name_ok("星梦"), bool)
        self.assertIsInstance(check_name_ok(""), bool)
        self.assertFalse(check_name_ok(""))

    def test_原子写json往返(self):
        p = os.path.join(tempfile.mkdtemp(), "a.json")
        atomic_write_json(p, {"k": "值", "n": 3})
        with open(p, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"k": "值", "n": 3})

    def test_种子库读取与分页(self):
        p = os.path.join(tempfile.mkdtemp(), "seeds.json")
        # 真实结构：{"regular": [...], "secret": [...]}
        data = {"regular": [{"no": 1, "name": "甲", "value": "1.1.1"}, {"no": 2, "name": "乙", "value": "2.2.2"}], "secret": [{"no": 3, "name": "丙", "value": "3.3.3"}]}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        s = Seeds(p)
        self.assertTrue(s.available(), "有 regular 种子时应可用")
        self.assertGreaterEqual(len(s.all()), 3)
        self.assertIsInstance(s.pages(), list)
        self.assertIsInstance(s.label(s.all()[:2]), str)
        self.assertEqual(s.by_no(1)["name"], "甲")
        self.assertEqual(s.by_no(1)["category"], "常规")
        self.assertIsNone(s.by_no(999))

    def test_状态存储可持久化(self):
        p = os.path.join(tempfile.mkdtemp(), "st.json")
        st = ServerStatusStore(p)
        st.observe("SRV1", True)
        st2 = ServerStatusStore(p)
        self.assertIsNotNone(st2.state_of("SRV1"))
        self.assertIsNone(st2.state_of("NEVER_SEEN"))

    def test_随机候选不重复且数量可控(self):
        # 真实契约：入参为种子字符串列表；返回 (options, err)
        seeds = [str(i) for i in range(1, 21)]
        opts, err = generate_random_options(seeds, option_count=6)
        self.assertIsNone(err)
        self.assertEqual(len(opts), 6)
        keys = [tuple(o.get("seeds") or []) for o in opts]
        self.assertEqual(len(set(keys)), 6, "候选不应重复")
        # 种子不足时的契约
        empty, err2 = generate_random_options([], option_count=6)
        self.assertEqual(empty, [])
        self.assertIsNotNone(err2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
