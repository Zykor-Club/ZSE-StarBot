# -*- coding: utf-8 -*-
"""经济账本单元测试：签到/连续/里程碑/幂等/并发/扣款不为负/冻结/清零/在线奖励"""
import os, sys, tempfile, threading, time, unittest

BOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot")
sys.path.insert(0, os.path.abspath(BOT))
os.environ.setdefault("ECONOMY_DB", os.path.join(tempfile.mkdtemp(), "econ_test.db"))
from economy_store import EconomyStore, MILESTONES, MAX_ADD


class EconomyTest(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "e.db")
        self.st = EconomyStore(self.db)

    def test_首次签到给基础分且连续为1(self):
        ok, msg, bal, streak, amt, extra = self.st.sign("O1", "2026-01-01", 20, 5, "甲")
        self.assertTrue(ok); self.assertEqual(streak, 1)
        self.assertEqual(amt, 20); self.assertEqual(extra, 0); self.assertEqual(bal, 20)

    def test_同日重复签到被拒且余额不变(self):
        self.st.sign("O1", "2026-01-01", 20, 5, "甲")
        ok, msg, bal, *_ = self.st.sign("O1", "2026-01-01", 30, 9, "甲")
        self.assertFalse(ok); self.assertIn("已经签到", msg); self.assertEqual(bal, 20)

    def test_连续第二天含连续奖励(self):
        self.st.sign("O1", "2026-01-01", 20, 5, "甲")
        ok, _, bal, streak, amt, extra = self.st.sign("O1", "2026-01-02", 20, 5, "甲")
        self.assertTrue(ok); self.assertEqual(streak, 2); self.assertEqual(extra, 5); self.assertEqual(amt, 25)

    def test_跳签多天连续重置为1(self):
        self.st.sign("O1", "2026-01-01", 20, 5, "甲")
        ok, _, _, streak, amt, extra = self.st.sign("O1", "2026-01-09", 20, 5, "甲")
        self.assertTrue(ok); self.assertEqual(streak, 1); self.assertEqual(extra, 0)

    def test_日期回退被拒(self):
        self.st.sign("O1", "2026-01-10", 20, 5, "甲")
        ok, msg, *_ = self.st.sign("O1", "2026-01-09", 20, 5, "甲")
        self.assertFalse(ok); self.assertIn("不能回退", msg)

    def test_里程碑第7天一次性(self):
        tot = 0
        for i in range(7):
            ok, _, _, streak, amt, extra = self.st.sign("O1", "2026-02-%02d" % (i + 1), 20, 5, "甲")
            tot += amt
        self.assertEqual(streak, 7); self.assertEqual(extra, MILESTONES[7] + 5)
        # 第 8 天不再给里程碑
        ok, _, _, streak8, amt8, extra8 = self.st.sign("O1", "2026-02-08", 20, 5, "甲")
        self.assertEqual(streak8, 8); self.assertEqual(extra8, 5)

    def test_并发签到只成功一次(self):
        res = []
        def w():
            res.append(self.st.sign("OC", "2026-03-01", 20, 5, "乙")[0])
        ts = [threading.Thread(target=w) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sum(1 for x in res if x), 1)

    def test_扣款不会为负(self):
        self.st.add("O2", 10, "grant", "g1", 1000, "甲")
        ok, msg, bal, spent = self.st.spend("O2", 50, "buy")
        self.assertFalse(ok); self.assertEqual(bal, 10); self.assertEqual(spent, 0)
        ok2, _, bal2, spent2 = self.st.spend("O2", 10, "buy")
        self.assertTrue(ok2); self.assertEqual(bal2, 0); self.assertEqual(spent2, 10)

    def test_幂等ref只发一次(self):
        a = self.st.add("O3", 5, "grant", "same-ref", 1000, "甲")
        b = self.st.add("O3", 5, "grant", "same-ref", 1000, "甲")
        self.assertTrue(a[0]); self.assertFalse(b[0]); self.assertEqual(self.st.get("O3")["balance"], 5)

    def test_单笔加分上限(self):
        ok, msg, _ = self.st.add("O4", MAX_ADD + 1, "grant", "", MAX_ADD, "甲")
        self.assertFalse(ok); self.assertIn("上限", msg)

    def test_冻结期间不能赚也不能花(self):
        self.st.add("O5", 10, "grant", "x1", 1000, "甲")
        self.st.freeze("O5", True)
        self.assertFalse(self.st.sign("O5", "2026-04-01", 20, 5, "甲")[0])
        self.assertFalse(self.st.add("O5", 5, "grant", "x2", 1000, "甲")[0])
        self.assertFalse(self.st.spend("O5", 1, "buy")[0])
        self.st.freeze("O5", False)
        self.assertTrue(self.st.add("O5", 5, "grant", "x3", 1000, "甲")[0])

    def test_冻结超期清零但保留流水(self):
        self.st.add("O6", 30, "grant", "y1", 1000, "甲")
        self.st.freeze("O6", True)
        with self.st._conn() as c:
            c.execute("UPDATE economy SET frozen_at=? WHERE openid=?", (int(time.time()) - 8 * 86400, "O6"))
        n = self.st.clear_frozen(7)
        self.assertEqual(n, 1)
        self.assertEqual(self.st.get("O6")["balance"], 0)
        self.assertGreaterEqual(len(self.st.logs("O6")), 2)

    def test_在线奖励按整小时幂等(self):
        for h in (1, 2, 3):
            self.assertTrue(self.st.add("O7", 3, "playtime", "playtime:O7:%d" % h, 1000, "甲")[0])
        self.assertFalse(self.st.add("O7", 3, "playtime", "playtime:O7:3", 1000, "甲")[0])
        self.assertEqual(self.st.last_play_hour("O7"), 3)
        self.assertEqual(self.st.get("O7")["balance"], 9)
        self.assertEqual(self.st.last_play_hour("NONE"), 0)

    def test_今日签到序与实际一致(self):
        day0 = int(time.time()) - 3600
        self.st.sign("OA", "2026-05-01", 20, 5, "甲")
        self.st.sign("OB", "2026-05-01", 20, 5, "乙")
        self.st.sign("OD", "2026-05-01", 20, 5, "丙")
        self.assertEqual(self.st.today_rank("OA", day0), 1)
        self.assertEqual(self.st.today_rank("OD", day0), 3)

    def test_重置经济只清余额保留流水(self):
        self.st.add("O8", 20, "grant", "z1", 1000, "甲")
        self.st.reset_all()
        self.assertEqual(self.st.get("O8")["balance"], 0)
        self.assertEqual(self.st.get("O8")["total_earned"], 20)
        self.assertGreaterEqual(len(self.st.logs("O8")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
