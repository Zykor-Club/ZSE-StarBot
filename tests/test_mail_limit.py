# -*- coding: utf-8 -*-
"""邮箱申请上限重置测试：封顶 → 重置 → 可再申请 → 落盘生效"""
import os, sys, tempfile, unittest

BOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
sys.path.insert(0, BOT)
import whitelist_mail as wm


class FakeSender:
    def send_code(self, email, code, group_name, bot_name):
        return True, "ok"


class MailLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(tempfile.mkdtemp(), "verify.json")
        self._old = wm.VERIFY_FILE
        wm.VERIFY_FILE = self.tmp          # 重定向持久化文件，避免污染真实数据
        self.mgr = wm.VerifyManager(FakeSender(), retry_cooldown=0, max_mail=5)

    def tearDown(self):
        wm.VERIFY_FILE = self._old

    def test_封顶后重置即可再申请且落盘(self):
        email = "1011819146@qq.com"
        for _ in range(5):
            ok, msg, code = self.mgr.request_code("OID", email, "G", "B")
            self.assertTrue(ok, msg)
        ok6, msg6, _ = self.mgr.request_code("OID", email, "G", "B")
        self.assertFalse(ok6, "第 6 次应被上限拦住")
        self.assertIn("5", msg6)
        r_ok, r_msg = self.mgr.reset_email_limit(email)
        self.assertTrue(r_ok, r_msg)
        ok7, msg7, _c = self.mgr.request_code("OID2", email, "G", "B")
        self.assertTrue(ok7, "重置后应可以重新申请: " + msg7)
        # 落盘验证：新实例重新加载后计数为 1（而不是 0 或 6）
        m2 = wm.VerifyManager(FakeSender(), retry_cooldown=0, max_mail=5)
        self.assertEqual(int(m2._verify_email[email]["sent_count"]), 1)

    def test_无记录时重置返回失败(self):
        ok, msg = self.mgr.reset_email_limit("1234567@qq.com")
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_空参数被拒(self):
        ok, _msg = self.mgr.reset_email_limit("")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
