# -*- coding: utf-8 -*-
"""权限体系测试：等级顺序 / 权限判定 / 高级管理人员守卫（至少一名、不能取消自己）/ 开关 / 审计"""
import os, sys, tempfile, unittest

BOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
sys.path.insert(0, BOT)
from permissions import PermissionManager, RANK, OWNER, MASTER, ADMIN, MEMBER, PERM_ROLE_MANAGE, PERM_BROADCAST

G = "G_TEST"


class PermissionTest(unittest.TestCase):
    def setUp(self):
        self.pm = PermissionManager(os.path.join(tempfile.mkdtemp(), "perms.json"))

    def test_等级顺序(self):
        self.assertGreater(RANK[OWNER], RANK[MASTER])
        self.assertGreater(RANK[MASTER], RANK[ADMIN])
        self.assertGreater(RANK[ADMIN], RANK[MEMBER])

    def test_未授身份时无权限(self):
        self.assertFalse(self.pm.check(G, "NOBODY", PERM_BROADCAST))
        # 未授身份者默认是"普通群员"（rank=1），不是 0
        self.assertEqual(self.pm.rank_of(G, "NOBODY"), RANK[MEMBER])

    def test_高级管理员拥有全部权限(self):
        self.pm.set_first_owner(G, "OID_OWNER")
        self.assertTrue(self.pm.check(G, "OID_OWNER", PERM_BROADCAST))
        self.assertTrue(self.pm.is_owner(G, "OID_OWNER"))
        self.assertEqual(self.pm.role_of(G, "OID_OWNER"), OWNER)
        self.assertEqual(self.pm.rank_of(G, "OID_OWNER"), RANK[OWNER])

    def test_服主低于高级管理员(self):
        self.pm.set_first_owner(G, "OID_OWNER1")
        self.pm.add_role(G, "OID_MASTER", MASTER, "OID_OWNER1")
        self.assertEqual(self.pm.rank_of(G, "OID_MASTER"), RANK[MASTER])
        self.assertLess(self.pm.rank_of(G, "OID_MASTER"), self.pm.rank_of(G, "OID_OWNER1"))

    def test_不能取消最后一名高级管理员(self):
        self.pm.set_first_owner(G, "OID_ONLY")
        ok, msg = self.pm.remove_owner(G, "OID_ONLY", "OID_ONLY")
        self.assertFalse(ok, "必须拦住：至少保留一名高级管理员")
        self.assertTrue(self.pm.is_owner(G, "OID_ONLY"))

    def test_不能取消自己(self):
        self.pm.set_first_owner(G, "OID_A1")
        self.pm.add_owner(G, "OID_A2", "OID_A1")
        ok, msg = self.pm.remove_owner(G, "OID_A2", "OID_A2")   # 自己取消自己
        self.assertFalse(ok, "必须拦住：不能取消自己")
        self.assertTrue(self.pm.is_owner(G, "OID_A2"))

    def test_他人可以移除多余高级管理员(self):
        self.pm.set_first_owner(G, "OID_B1")
        self.pm.add_owner(G, "OID_B2", "OID_B1")
        ok, _msg = self.pm.remove_owner(G, "OID_B2", "OID_B1")
        self.assertTrue(ok)
        self.assertFalse(self.pm.is_owner(G, "OID_B2"))
        self.assertEqual(len(self.pm.owners_of(G)), 1)

    def test_身份移除后不再有权限(self):
        self.pm.set_first_owner(G, "OID_C1")
        self.pm.add_role(G, "OID_C2", ADMIN, "OID_C1")
        # 实战语义：设置身份只给高级管理员（OWNER）；管理员没有该权限
        self.assertFalse(self.pm.check(G, "OID_C2", PERM_ROLE_MANAGE))
        self.assertTrue(self.pm.check(G, "OID_C1", PERM_ROLE_MANAGE))
        self.pm.remove_member(G, "OID_C2")
        self.assertEqual(self.pm.rank_of(G, "OID_C2"), RANK[MEMBER])

    def test_群开关持久化(self):
        self.pm.set_first_owner(G, "OID_D1")
        self.assertTrue(self.pm.set_map_allowed(G, True, "OID_D1")[0])
        self.assertTrue(self.pm.map_allowed(G))
        p2 = PermissionManager(self.pm.path)
        self.assertTrue(p2.map_allowed(G), "重新加载后开关应保留")

    def test_审计有记录(self):
        self.pm.set_first_owner(G, "OID_E1")
        self.pm.add_role(G, "OID_E2", ADMIN, "OID_E1")
        self.assertGreaterEqual(len(self.pm.audit_log(G)), 1)

    def test_重置群清空所有身份(self):
        self.pm.set_first_owner(G, "OID_F1")
        self.pm.add_role(G, "OID_F2", ADMIN, "OID_F1")
        self.pm.reset_group(G)
        self.assertFalse(self.pm.is_owner(G, "OID_F1"))
        self.assertEqual(self.pm.rank_of(G, "OID_F2"), RANK[MEMBER])


if __name__ == "__main__":
    unittest.main(verbosity=2)
