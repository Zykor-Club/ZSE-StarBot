# -*- coding: utf-8 -*-
"""
多群联合：群注册表模块（星状 zone 模型）

为每个群（按 32 位大写十六进制 openid）分配一个短整数群ID（join_id），
并维护"总群（主群）— 子群"的星状联合关系。

约定：
  - 主动发起「绑定联合群」且双方均未联合的群成为总群（master_gid=null, 编号1）；
    之后与总群建立联合的群都是子群（master_gid=总群openid，编号=加入顺序）。
  - 一个子群不能发起联合；目标群必须是独立群（或总群吸纳独立群）。
  - 数据归属：联合区内所有数据（白名单/身份/权限）都以总群为准（effective_gid 概念）。

数据存 groups_registry.json：
{
  "next_id": 1,
  "groups": {
    "<群openid>": {
      "join_id": 1,
      "master_gid": null,     // 子群指向总群；总群/独立群为 null
      "joined_at": 0,         // 加入联合时间戳（总群/独立群为 0）
      "join_order": 0         // 总群=1；子群=2,3,...；独立群=0
    }
  }
}
"""

import json
import os
import tempfile
import time

_BASE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_FILE = os.path.join(_BASE, "groups_registry.json")


def atomic_write_json(path: str, data: dict):
    """写临时文件后原子替换，避免进程崩溃导致 JSON 半写损坏"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class GroupRegistry:
    """群注册表：群ID分配 + 星状联合（总群/子群）"""

    def __init__(self, path: str = REGISTRY_FILE):
        self.path = path
        self._data: dict = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._data = {"next_id": 1, "groups": {}}
        # 旧格式兼容：老 linked 数组清空视为独立群
        for ent in (self._data.get("groups", {}) or {}).values():
            ent.pop("linked", None)
            ent.pop("name", None)
            ent.setdefault("master_gid", None)
            ent.setdefault("joined_at", 0)
            ent.setdefault("join_order", 0)

    def _save(self):
        try:
            atomic_write_json(self.path, self._data)
        except OSError as e:
            print(f"[groups_registry] 保存 groups_registry.json 失败: {e}")

    # ────────────── 群ID 分配 / 反查 ──────────────
    def get_or_assign(self, gid: str):
        """gid 已有则返回其记录；没有则分配 join_id = next_id 再自增，保存后返回记录。gid 为空返回 None"""
        if not gid:
            return None
        groups = self._data.setdefault("groups", {})
        ent = groups.get(gid)
        if ent is not None:
            return ent
        jid = self._data.get("next_id", 1)
        self._data["next_id"] = jid + 1
        ent = {
            "join_id": jid,
            "master_gid": None,
            "joined_at": 0,
            "join_order": 0,
        }
        groups[gid] = ent
        self._save()
        return ent

    def join_id_of(self, gid: str) -> int | None:
        ent = (self._data.get("groups", {}) or {}).get(gid)
        return None if ent is None else ent.get("join_id")

    def resolve_by_join_id(self, jid: int) -> str | None:
        for ogid, ent in (self._data.get("groups", {}) or {}).items():
            if ent.get("join_id") == jid:
                return ogid
        return None

    def all_gids(self) -> list:
        return list((self._data.get("groups", {}) or {}).keys())

    # ────────────── 联合关系（星状 zone） ──────────────
    def _ent(self, gid: str) -> dict | None:
        return (self._data.get("groups", {}) or {}).get(gid)

    def is_registered(self, gid: str) -> bool:
        return gid in (self._data.get("groups", {}) or {})

    def master_gid(self, gid: str) -> str | None:
        """返回 gid 的总群 openid（自己是总群/独立群返回 None）"""
        ent = self._ent(gid)
        return (ent or {}).get("master_gid") or None

    def effective_gid(self, gid: str) -> str:
        """数据归属群：联合区内一律用总群；独立群/总群返回自身"""
        return self.master_gid(gid) or gid

    def zone_gids(self, gid: str) -> set:
        """返回 gid 所在联合区的全部群 openid 集合（含自己与总群）；独立群 = {自己}"""
        master = self.effective_gid(gid)
        out = {master}
        for ogid, ent in (self._data.get("groups", {}) or {}).items():
            if (ent or {}).get("master_gid") == master:
                out.add(ogid)
        return out

    def zone_info(self, gid: str) -> dict:
        """联合区信息：{master_gid, master_join_id, joined_at, join_order, members:[{gid,join_id,joined_at,join_order}]}
        members 含总群与全部子群；独立群 members=[自己], join_order=0, joined_at=0"""
        master = self.effective_gid(gid)
        members = []
        for ogid, ent in (self._data.get("groups", {}) or {}).items():
            if ogid == master or (ent or {}).get("master_gid") == master:
                members.append({
                    "gid": ogid,
                    "join_id": ent.get("join_id"),
                    "joined_at": ent.get("joined_at", 0),
                    "join_order": ent.get("join_order", 0),
                })
        ent = self._ent(gid) or {}
        return {
            "master_gid": master,
            "master_join_id": self.join_id_of(master),
            "joined_at": ent.get("joined_at", 0),
            "join_order": ent.get("join_order", 0),
            "members": members,
        }

    def bind(self, a: str, b: str) -> tuple:
        """建立联合：a 为发起群。
        规则：
          - a == b 拒绝
          - a 已是子群 → 拒绝（子群不能发起联合）
          - b 已属于某联合区（总群或子群）→ 拒绝（目标已是联合群）
          - a 独立（可与 b 独立）→ a 成为总群，b 成为 a 的子群（编号=2）
          - a 已是总群（无 master_gid 但有自己的子群）且 b 独立 → b 成为 a 的子群
        返回 (ok, msg)。
        """
        if a == b:
            return False, "不能联合自己喵"
        if not self.is_registered(a) or not self.is_registered(b):
            return False, "群不存在喵"
        ea = self._ent(a)
        eb = self._ent(b)
        if ea["master_gid"]:
            return False, "本群已是子群，无法发起联合喵"
        if eb["master_gid"]:
            return False, "目标群已是其他联合区的子群喵"
        # b 已是总群（有自己的子群）→ 拒绝，避免两个总群互吞
        if any((e or {}).get("master_gid") == b for e in self._data.get("groups", {}).values()):
            return False, "目标群已是一个联合区的总群喵，可在目标群中邀请本群加入"
        # 计算本群区当前子群数（加入顺序）
        existing = [e for e in self._data.get("groups", {}).values() if (e or {}).get("master_gid") == a]
        order = max([(e.get("join_order") or 0) for e in existing] + [1]) + 1
        eb["master_gid"] = a
        eb["joined_at"] = int(time.time())
        eb["join_order"] = order
        if not ea["joined_at"]:
            ea["joined_at"] = int(time.time())
            ea["join_order"] = 1
        self._save()
        return True, f"联合成功喵！本群为总群，目标群为第 {order} 个子群"

    def unbind(self, gid: str) -> tuple:
        """解除联合：
          - 子群调用 → 自己脱离联合区（恢复独立）
          - 总群调用 → 解散整个联合区（所有子群恢复独立）
        返回 (ok, msg, affected_gids)。
        """
        ent = self._ent(gid)
        if ent is None:
            return False, "群不存在喵", []
        master = ent["master_gid"]
        affected = []
        if not master:
            # 总群：解散全部子群
            for ogid, e in (self._data.get("groups", {}) or {}).items():
                if (e or {}).get("master_gid") == gid:
                    e["master_gid"] = None
                    e["joined_at"] = 0
                    e["join_order"] = 0
                    affected.append(ogid)
            if not affected:
                return False, "本群没有子群喵", []
            self._save()
            return True, f"已解散联合区，{len(affected)} 个子群恢复独立喵", affected
        else:
            # 子群：脱离
            ent["master_gid"] = None
            ent["joined_at"] = 0
            ent["join_order"] = 0
            self._save()
            return True, "本群已解除联合，恢复独立喵", [gid]