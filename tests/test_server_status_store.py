# -*- coding: utf-8 -*-
"""服务器上/下线状态存储测试（证据：server_status_store.py 无自带自检，属覆盖缺口）

只断言"任何合理实现都必然成立"的性质，避免用测试固化我猜的语义：
  1) 未知 code → state_of 返回 None
  2) observe 之后重新加载仍能看到该 code（持久化）
  3) 高频抖动（200 次交替上/下线）不抛异常，且落盘仍是合法 JSON
  4) STABLE_SECONDS 常量存在且 > 0（防抖动窗口）
"""
import json, os, sys, tempfile, unittest

BOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
sys.path.insert(0, BOT)
from server_status_store import ServerStatusStore, STABLE_SECONDS


class ServerStatusTest(unittest.TestCase):
    def setUp(self):
        self.p = os.path.join(tempfile.mkdtemp(), "st.json")

    def test_未知code返回None(self):
        st = ServerStatusStore(self.p)
        self.assertIsNone(st.state_of("NEVER"))

    def test_observe后持久化(self):
        st = ServerStatusStore(self.p)
        st.observe("SRV1", True)
        st2 = ServerStatusStore(self.p)
        self.assertIsNotNone(st2.state_of("SRV1"), "observe 之后重新加载应能看到该服务器")

    def test_高频抖动不崩溃且落盘合法(self):
        st = ServerStatusStore(self.p, stable_seconds=1)
        t = 1000
        for i in range(200):
            st.observe("SRV1", bool(i % 2), now=t)
            t += 1
        self.assertIsNotNone(st.state_of("SRV1"))
        with open(self.p, "r", encoding="utf-8") as f:
            data = json.load(f)   # 落盘必须是合法 JSON
        self.assertIsInstance(data, dict)

    def test_防抖动窗口大于0(self):
        self.assertGreater(STABLE_SECONDS, 0)

    def test_多服务器互不影响(self):
        st = ServerStatusStore(self.p)
        st.observe("A", True)
        st.observe("B", False)
        self.assertIsNotNone(st.state_of("A"))
        self.assertIsNotNone(st.state_of("B"))
        self.assertIsNone(st.state_of("C"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
