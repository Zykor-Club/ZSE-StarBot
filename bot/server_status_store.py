# -*- coding: utf-8 -*-
"""服务器上/下线状态跟踪（供变化时通知）

只做「状态机 + 持久化」，不涉及 QQ。落盘结构：{code: {"online": bool, "since": ts,
"pending": str, "pending_since": ts}}

两个刻意的设计：
  · pending：TShock 重启、网络抖动都会造成瞬时断开，必须连续 stable 秒保持同一状态才认定切换，
    否则群里会被上线/掉线通知刷屏；
  · 首次观测不通知：机器人重启后所有服务器都会短暂显示离线，此时通知等于全体误报。
"""

import json
import os
import threading
import time

STABLE_SECONDS = 120        # 状态需稳定的时长（秒）
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_status.json")


def atomic_write_json(path: str, data: dict):
    """tmp + os.replace 原子替换（与其它存储模块同款）"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class ServerStatusStore:
    def __init__(self, path: str = None, stable_seconds: int = STABLE_SECONDS):
        self.path = path or _PATH
        self.stable = max(1, int(stable_seconds))
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_locked(self):
        try:
            atomic_write_json(self.path, self._data)
        except OSError as e:
            print(f"[server_status] 保存失败: {e}")

    def state_of(self, code: str):
        """当前已知状态：True 在线 / False 离线 / None 尚未观测过"""
        with self._lock:
            st = self._data.get(code or "")
            return None if not st else bool(st.get("online"))

    def observe(self, code: str, online: bool, now=None):
        """观测一次在线状态。

        返回 (event, prev_since)：event 为 "online"/"offline" 表示状态已稳定切换、需要通知；
        prev_since 是切换前那一状态的起始时间（用于算在线/掉线时长）。无需通知返回 None。
        """
        code = str(code or "")
        if not code:
            return None
        now = int(time.time() if now is None else now)
        online = bool(online)
        with self._lock:
            st = self._data.get(code)
            if st is None:
                self._data[code] = {"online": online, "since": now, "seen": now}
                self._save_locked()
                return None                     # 首次观测不通知
            if bool(st.get("online")) == online:
                st["seen"] = now
                st.pop("pending", None)
                st.pop("pending_since", None)
                return None
            want = "online" if online else "offline"
            if st.get("pending") != want:
                st["pending"] = want
                st["pending_since"] = now
                self._save_locked()
                return None
            if now - int(st.get("pending_since") or now) < self.stable:
                return None
            prev_since = int(st.get("since") or now)
            st["online"] = online
            st["since"] = now
            st.pop("pending", None)
            st.pop("pending_since", None)
            self._save_locked()
            return want, prev_since
