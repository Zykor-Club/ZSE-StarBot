# -*- coding: utf-8 -*-
"""进度提醒订阅存储（每群 × 每服务器 × 每 boss 独立）

仅负责数据与规则，不涉及 QQ / 网络。文件路径由调用方传入。

持久化：JSON 文件 {gid: [ {server_code, boss_key, boss_name, server_name, created_by, created_at}, ... ]}，
tmp 写入 + os.replace 原子替换，threading.Lock 保护并发读写。

语义：
  - 「server_code」= 服务器绑定码（全局唯一、稳定，与 votes.json 的 server_code 一致）；
  - 同一群内 (server_code, boss_key) 唯一（重复设置直接提示，不重复添加）；
  - 播报目标 = 所有订阅了 (server_code, boss_key) 的群（每群每服独立，互不影响）。
"""

import json
import os
import tempfile
import threading
import time
from datetime import datetime

MAX_PER_GROUP = 200     # 单群提醒条数上限（防刷）


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ProgressNotifyStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    # ───────────────────────── 持久化 ─────────────────────────
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._data = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._data = {}
        # 结构自愈：值必须是列表，条目必须是 dict
        for gid in list(self._data.keys()):
            entries = self._data[gid]
            if not isinstance(entries, list):
                self._data[gid] = []
                continue
            self._data[gid] = [e for e in entries if isinstance(e, dict)]

    def _save_locked(self):
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            print(f"[progress_notify] 保存 {self.path} 失败: {e}")

    # ───────────────────────── 增删查 ─────────────────────────
    def add(self, gid: str, server_code: str, boss_key: str, boss_name: str = "",
            server_name: str = "", created_by: str = "") -> tuple:
        """新增一条提醒。返回 (ok, msg)"""
        gid = gid or "nogroup"
        if not server_code or not boss_key:
            return False, "服务器标识或 boss 无效"
        with self._lock:
            entries = self._data.setdefault(gid, [])
            if any(e.get("server_code") == server_code and e.get("boss_key") == boss_key
                   for e in entries):
                return False, "该提醒已存在（同一服务器同一 boss 只需设置一次）"
            if len(entries) >= MAX_PER_GROUP:
                return False, f"本群提醒数量已达上限（{MAX_PER_GROUP} 条），请先取消部分提醒"
            entries.append({
                "server_code": server_code,
                "boss_key": boss_key,
                "boss_name": boss_name or boss_key,
                "server_name": server_name or "",
                "created_by": created_by or "",
                "created_at": _now_iso(),
            })
            self._save_locked()
        return True, "已设置"

    def remove(self, gid: str, server_code: str, boss_key: str) -> tuple:
        """取消一条提醒。返回 (ok, msg)"""
        gid = gid or "nogroup"
        with self._lock:
            entries = self._data.get(gid) or []
            target = next((e for e in entries
                           if e.get("server_code") == server_code and e.get("boss_key") == boss_key),
                          None)
            if target is None:
                return False, "没有找到该提醒（可用「进度提醒列表」查看）"
            entries.remove(target)
            if not entries:
                self._data.pop(gid, None)
            self._save_locked()
        return True, "已取消"

    def list_for(self, gid: str) -> list:
        """本群全部提醒（拷贝，按设置时间排序）"""
        return [dict(e) for e in (self._data.get(gid or "nogroup") or [])]

    def find(self, gid: str, server_code: str, boss_key: str):
        """本群某条提醒；找不到返回 None"""
        for e in (self._data.get(gid or "nogroup") or []):
            if e.get("server_code") == server_code and e.get("boss_key") == boss_key:
                return dict(e)
        return None

    def subscribers_for(self, server_code: str, boss_key: str) -> list:
        """所有订阅了 (server_code, boss_key) 的群 openid 列表"""
        out = []
        if not server_code or not boss_key:
            return out
        for gid, entries in self._data.items():
            if any(e.get("server_code") == server_code and e.get("boss_key") == boss_key
                   for e in entries):
                out.append(gid)
        return sorted(out)

    def remove_server(self, server_code: str) -> int:
        """服务器被删除时清理其全部提醒（跨群）。返回清理条数"""
        if not server_code:
            return 0
        cnt = 0
        with self._lock:
            for gid in list(self._data.keys()):
                entries = self._data.get(gid) or []
                kept = [e for e in entries if e.get("server_code") != server_code]
                cnt += len(entries) - len(kept)
                if kept:
                    self._data[gid] = kept
                else:
                    self._data.pop(gid, None)
            if cnt:
                self._save_locked()
        return cnt


# ─────────────────────────── 自测 ───────────────────────────
if __name__ == "__main__":
    fd, tmp_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(tmp_path)
    st = ProgressNotifyStore(tmp_path)
    assert st.add("g1", "123456", "Moon Lord", "月亮领主", "梦幻服", "u1")[0]
    assert not st.add("g1", "123456", "Moon Lord", "月亮领主")[0]  # 重复
    assert st.add("g1", "123456", "The Twins", "双子魔眼", "梦幻服", "u1")[0]
    assert st.add("g2", "123456", "Moon Lord", "月亮领主", "梦幻服", "u2")[0]
    assert len(st.list_for("g1")) == 2
    assert st.find("g1", "123456", "The Twins")["boss_name"] == "双子魔眼"
    assert st.subscribers_for("123456", "Moon Lord") == ["g1", "g2"]
    assert st.subscribers_for("123456", "Plantera") == []
    assert st.remove("g1", "123456", "The Twins")[0]
    assert not st.remove("g1", "123456", "The Twins")[0]
    # 重载（模拟重启）后数据仍在
    st2 = ProgressNotifyStore(tmp_path)
    assert len(st2.list_for("g1")) == 1
    assert st2.remove_server("123456") == 2
    assert st2.list_for("g1") == [] and st2.list_for("g2") == []
    os.unlink(tmp_path)
    print("progress_notify_store 自测通过")