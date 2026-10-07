# -*- coding: utf-8 -*-
"""绑定规则单元测试（纯函数，覆盖用户明确要求的四条规则）"""
import os, sys, unittest

BOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
sys.path.insert(0, BOT)
from bind_rules import normalize_email, is_valid_qq_email, check_bind_request

WL_EMPTY = {}
WL_MINE = {"G1": {"星梦": {"email": "1011819146@qq.com", "bind_openid": "OID_A"}}}
WL_OTHER = {"G1": {"别人": {"email": "5555555@qq.com", "bind_openid": "OID_B"}}}


class BindRulesTest(unittest.TestCase):
    def test_纯数字自动补qq邮箱(self):
        self.assertEqual(normalize_email("1011819146"), "1011819146@qq.com")
        self.assertEqual(normalize_email(" 3879157746 "), "3879157746@qq.com")

    def test_非纯数字不合法(self):
        for bad in ("abc123", "123", "1234567890123", "x@qq.com", "@qq.com", ""):
            self.assertFalse(is_valid_qq_email(normalize_email(bad)), bad)

    def test_格式非法被拒(self):
        ok, email, reason = check_bind_request("abc", "OID_A", "", WL_EMPTY, {})
        self.assertFalse(ok); self.assertIn("纯数字", reason)

    def test_机器人自己的qq被拒(self):
        ok, _e, reason = check_bind_request("4014747343", "OID_A", "", WL_EMPTY, {})
        self.assertFalse(ok); self.assertIn("机器人自己", reason)

    def test_邮箱已在白名单被拒(self):
        ok, _e, reason = check_bind_request("5555555", "OID_A", "", WL_OTHER, {})
        self.assertFalse(ok); self.assertIn("已经", reason)

    def test_身份取不到被拒(self):
        ok, _e, reason = check_bind_request("3879157746", "", "", WL_EMPTY, {})
        self.assertFalse(ok); self.assertIn("身份", reason)

    def test_本人已绑定则任何再绑定都被拒_核心需求(self):
        # 已绑定 1011819146 的人，去绑另一个全新号码 → 必须拒绝
        ok, _e, reason = check_bind_request("3879157746", "OID_A", "", WL_MINE, {})
        self.assertFalse(ok); self.assertIn("已经绑定过", reason)
        # union id 命中同样拒绝（记录里存的是哪种 id 都行）
        ok2, _e2, _r2 = check_bind_request("3879157746", "X", "OID_A", WL_MINE, {})
        self.assertFalse(ok2)

    def test_验证码记录兜底_记录无openid也能识别(self):
        wl = {"G1": {"星梦": {"email": "1011819146@qq.com", "bind_openid": ""}}}
        vu = {"OID_A": {"email": "1011819146@qq.com", "code": "1"}}
        ok, _e, reason = check_bind_request("3879157746", "OID_A", "", wl, vu)
        self.assertFalse(ok); self.assertIn("已经绑定过", reason)

    def test_全新用户全新号码允许(self):
        ok, email, reason = check_bind_request("3879157746", "OID_NEW", "", WL_EMPTY, {})
        self.assertTrue(ok); self.assertEqual(email, "3879157746@qq.com"); self.assertEqual(reason, "")

    def test_已绑定者换绑自己的邮箱也被拒(self):
        ok, _e, _r = check_bind_request("1011819146", "OID_A", "", WL_MINE, {})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
