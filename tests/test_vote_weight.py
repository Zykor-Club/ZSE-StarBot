# -*- coding: utf-8 -*-
"""投票权重纯函数测试（证据：vote_store.py:52-63 `vote_weight`；阈值见 vote_store.ONLINE_THRESHOLD_MIN）"""
import os, sys, unittest

BOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
sys.path.insert(0, BOT)
from vote_store import vote_weight, ONLINE_THRESHOLD_MIN


class VoteWeightTest(unittest.TestCase):
    def test_快照为空时权重为1(self):
        self.assertEqual(vote_weight(None, "OID"), 1.0)
        self.assertEqual(vote_weight({}, "OID"), 1.0)

    def test_未达阈值为1(self):
        self.assertEqual(vote_weight({"OID": ONLINE_THRESHOLD_MIN - 1}, "OID"), 1.0)
        self.assertEqual(vote_weight({"OID": 0}, "OID"), 1.0)

    def test_达到或超过阈值为1_5(self):
        self.assertEqual(vote_weight({"OID": ONLINE_THRESHOLD_MIN}, "OID"), 1.5)
        self.assertEqual(vote_weight({"OID": ONLINE_THRESHOLD_MIN + 100}, "OID"), 1.5)

    def test_异常值不崩溃且按0处理(self):
        self.assertEqual(vote_weight({"OID": None}, "OID"), 1.0)
        self.assertEqual(vote_weight({"OID": "abc"}, "OID"), 1.0)
        self.assertEqual(vote_weight({"OID": "120"}, "OID"), 1.5 if 120 >= ONLINE_THRESHOLD_MIN else 1.0)

    def test_不存在的用户为1(self):
        self.assertEqual(vote_weight({"OTHER": 99999}, "OID"), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
