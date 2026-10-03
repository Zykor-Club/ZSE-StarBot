# -*- coding: utf-8 -*-
"""进度解锁提醒订阅存储（每群 × 每服务器 × 每 boss 独立）

与 progress_notify_store 的差异：
  - 覆盖式：同一 (server_code, boss_key) 重复设置时更新提醒分钟数，不报重复；
  - 额外维护运行时状态：last_ts（最近一次同步到的绝对解锁时间戳）、
    fired_ts（已提醒过的解锁时间戳，幂等键）、last_sync（最近同步时间）。
    last_ts 变化（世界重置 → 新解锁时间）后自动重新武装；
    fired_ts == last_ts 表示本轮已提醒过，不重复发送。
  - 服务器离线时保留 last_ts（按最后一次同步的时间照发）；
    boss 已不在锁定表（已解锁）时也保留旧 last_ts，窗口已过自然不再触发。

持久化：JSON 文件 {gid: [ {...}, ... ]}，tmp 写入 + os.replace 原子替换，
threading.Lock 保护并发读写。
"""

import json
import os
import tempfile
import threading
import time
from datetime import datetime

MAX_PER_GROUP = 200     # 单群提醒条数上限（防刷）
MIN_MINUTES = 1
MAX_MINUTES = 1440


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ProgressUnlockStore:
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
            print(f"[progress_unlock] 保存 {self.path} 失败: {e}")

    # ───────────────────────── 增删查 ─────────────────────────
    def add(self, gid: str, server_code: str, boss_key: str, boss_name: str = "",
            server_name: str = "", minutes: int = 30, created_by: str = "") -> tuple:
        """新增或覆盖一条提醒（同服务器同 boss 唯一）。返回 (ok, msg)"""
        gid = gid or "nogroup"
        if not server_code or not boss_key:
            return False, "服务器标识或 boss 无效"
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return False, "提醒分钟数无效"
        if not (MIN_MINUTES <= minutes <= MAX_MINUTES):
            return False, f"分钟数需在 {MIN_MINUTES}~{MAX_MINUTES} 之间"
        with self._lock:
            entries = self._data.setdefault(gid, [])
            target = next((e for e in entries
                           if e.get("server_code") == server_code and e.get("boss_key") == boss_key),
                          None)
            if target is not None:
                # 覆盖：保留 last_ts / fired_ts 状态（避免已提醒过的被重复触发）
                target["minutes"] = minutes
                target["boss_name"] = boss_name or target.get("boss_name") or boss_key
                target["server_name"] = server_name or target.get("server_name") or ""
                target["created_by"] = created_by or target.get("created_by") or ""
                target["updated_at"] = _now_iso()
                self._save_locked()
                return True, "已更新"
            if len(entries) >= MAX_PER_GROUP:
                return False, f"本群提醒数量已达上限（{MAX_PER_GROUP} 条），请先取消部分提醒"
            entries.append({
                "server_code": server_code,
                "boss_key": boss_key,
                "boss_name": boss_name or boss_key,
                "server_name": server_name or "",
                "minutes": minutes,
                "created_by": created_by or "",
                "created_at": _now_iso(),
                "last_ts": 0,
                "fired_ts": 0,
                "last_sync": 0,
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
                return False, "没有找到该提醒（可用「进度解锁提醒列表」查看）"
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

    def all_entries(self) -> list:
        """全部提醒 [(gid, entry拷贝)]，供后台轮询判定触发"""
        out = []
        with self._lock:
            for gid, entries in self._data.items():
                for e in entries:
                    out.append((gid, dict(e)))
        return out

    def server_codes(self) -> list:
        """所有存在订阅的服务器标识（去重，供轮询同步用）"""
        codes = set()
        with self._lock:
            for entries in self._data.values():
                for e in entries:
                    code = e.get("server_code")
                    if code:
                        codes.add(code)
        return sorted(codes)

    def sync_server(self, server_code: str, ts_map: dict):
        """服务器在线时同步：更新该服务器所有提醒的 last_ts / last_sync。

        boss 不在锁定表（已解锁/未锁）时保留旧 last_ts——窗口已过，不会补发。
        """
        if not server_code or not isinstance(ts_map, dict):
            return
        now = int(time.time())
        with self._lock:
            changed = False
            for entries in self._data.values():
                for e in entries:
                    if e.get("server_code") != server_code:
                        continue
                    e["last_sync"] = now
                    changed = True
                    try:
                        ts = int(ts_map.get(e.get("boss_key")))
                    except (TypeError, ValueError):
                        ts = 0
                    if ts > 0 and ts != int(e.get("last_ts") or 0):
                        e["last_ts"] = ts
            if changed:
                self._save_locked()

    def mark_fired(self, gid: str, server_code: str, boss_key: str, ts: int):
        """标记某条提醒已针对该解锁时间戳发送过（幂等键）"""
        gid = gid or "nogroup"
        with self._lock:
            for e in (self._data.get(gid) or []):
                if e.get("server_code") == server_code and e.get("boss_key") == boss_key:
                    e["fired_ts"] = int(ts)
                    self._save_locked()
                    return

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
    st = ProgressUnlockStore(tmp_path)
    assert st.add("g1", "123456", "Moon Lord", "月亮领主", "梦幻服", 30, "u1")[0]
    assert st.add("g1", "123456", "Moon Lord", minutes=60)[0]  # 覆盖式：更新分钟数
    e = st.find("g1", "123456", "Moon Lord")
    assert e["minutes"] == 60 and e["boss_name"] == "月亮领主" and e["server_name"] == "梦幻服"
    assert not st.add("g1", "123456", "Golem", minutes=0)[0]      # 分钟范围校验
    assert not st.add("g1", "123456", "Golem", minutes=1441)[0]
    assert st.add("g1", "123456", "The Twins", "双子魔眼", "梦幻服", 15)[0]
    assert st.add("g2", "123456", "Moon Lord", "月亮领主", "梦幻服", 30)[0]
    assert st.server_codes() == ["123456"]
    assert [g for g, _ in st.all_entries()] == ["g1", "g1", "g2"]
    # 同步：更新 last_ts；boss 不在表时保留旧值（错过窗口不补发）
    st.sync_server("123456", {"Moon Lord": 1000, "The Twins": 2000})
    assert st.find("g1", "123456", "Moon Lord")["last_ts"] == 1000
    assert st.find("g1", "123456", "Moon Lord")["last_sync"] > 0
    st.sync_server("123456", {"Moon Lord": 9000})
    assert st.find("g1", "123456", "Moon Lord")["last_ts"] == 9000
    assert st.find("g1", "123456", "The Twins")["last_ts"] == 2000
    # 幂等标记：fired_ts 记录已提醒的解锁时间戳
    st.mark_fired("g1", "123456", "Moon Lord", 9000)
    assert st.find("g1", "123456", "Moon Lord")["fired_ts"] == 9000
    # 解锁时间变化（世界重置）→ last_ts ≠ fired_ts，自动重新武装
    st.sync_server("123456", {"Moon Lord": 20000})
    e = st.find("g1", "123456", "Moon Lord")
    assert e["last_ts"] == 20000 and e["fired_ts"] == 9000
    # 重载（模拟重启）后状态仍在
    st2 = ProgressUnlockStore(tmp_path)
    assert st2.find("g1", "123456", "Moon Lord")["fired_ts"] == 9000
    assert st2.find("g1", "123456", "Moon Lord")["last_ts"] == 20000
    assert st2.remove("g1", "123456", "The Twins")[0]
    assert not st2.remove("g1", "123456", "The Twins")[0]
    # g1 剩 Moon Lord、g2 剩 Moon Lord（Twins 已先删）→ 跨群清理共 2 条
    assert st2.remove_server("123456") == 2
    assert st2.all_entries() == [] and st2.server_codes() == []
    os.unlink(tmp_path)
    print("progress_unlock_store 自测通过")