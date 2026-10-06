# -*- coding: utf-8 -*-
"""白名单"以人为主体"的新表（参考 CaiBotLite 的 User/LoginUUID/LoginIP 模型）

**第 1 步：只加表 + 定期回填，不改任何进服判定逻辑** ——
现有判定仍读 whitelist.json（WhitelistStore），本模块只做"新结构的积累"，
等数据稳定后再走"双读灰度 → 切换"。

表结构：
  whitelist_user   (group_openid, openid, name) 主键 —— 一个人在一个群绑定的玩家名（可多名）
  whitelist_device (openid, uuid)      主键 —— **设备挂在"人"身上**（改名/多名字不用重验设备）
  whitelist_city   (openid, city)      主键 —— 常用城市也挂在人身上

回填是幂等 upsert：反复跑只会更新 updated_at，不产生重复。
"""

import os
import sqlite3
import threading
import time

DEFAULT_DB = "C:/bot/whitelist_users.db"

DDL = [
    """CREATE TABLE IF NOT EXISTS whitelist_user (
        group_openid TEXT    NOT NULL,
        openid       TEXT    NOT NULL,
        name         TEXT    NOT NULL,
        email        TEXT    NOT NULL DEFAULT '',
        bind_time    INTEGER NOT NULL DEFAULT 0,
        frozen       INTEGER NOT NULL DEFAULT 0,
        updated_at   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (group_openid, openid, name)
    )""",
    """CREATE TABLE IF NOT EXISTS whitelist_device (
        openid       TEXT    NOT NULL,
        uuid         TEXT    NOT NULL,
        platform     TEXT    NOT NULL DEFAULT '',
        first_seen   INTEGER NOT NULL DEFAULT 0,
        last_seen    INTEGER NOT NULL DEFAULT 0,
        src_group    TEXT    NOT NULL DEFAULT '',
        src_name     TEXT    NOT NULL DEFAULT '',
        updated_at   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (openid, uuid)
    )""",
    """CREATE TABLE IF NOT EXISTS whitelist_city (
        openid       TEXT    NOT NULL,
        city         TEXT    NOT NULL,
        ts           INTEGER NOT NULL DEFAULT 0,
        updated_at   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (openid, city)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wl_user_openid ON whitelist_user(openid)",
    "CREATE INDEX IF NOT EXISTS idx_wl_user_name ON whitelist_user(group_openid, name)",
]


def snapshot_items(d):
    """稳定的 items 列表（配合快照使用）"""
    return list((d or {}).items())


class WhitelistUsers:
    def __init__(self, path: str = ""):
        self.path = path or os.environ.get("WHITELIST_USERS_DB") or DEFAULT_DB
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            for sql in DDL:
                c.execute(sql)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def sync(self, data: dict, devices_of=None) -> dict:
        """把 whitelist.json 的全量数据回填到新表（幂等）。
        data: {group_openid: {name: record}}；devices_of(record) 用于取设备列表（兼容旧 uuid 字段）。"""
        now = int(time.time())
        users = devs = cities = skipped = 0
        # 深拷贝一层快照：白名单字典会被消息线程改动，直接迭代会抛"字典在迭代中被修改"
        snap = {g: dict(v or {}) for g, v in list((data or {}).items())}
        with self._lock, self._conn() as c:
            for gid, group in snapshot_items(snap):
                for name, rec in list((group or {}).items()):
                    rec = rec or {}
                    openid = (rec.get("bind_openid") or "").strip()
                    if not openid:
                        skipped += 1          # 老记录没记绑定人：等玩家登录认领后再回填
                        continue
                    c.execute(
                        "INSERT INTO whitelist_user(group_openid, openid, name, email, bind_time, frozen, updated_at) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_openid, openid, name) DO UPDATE SET "
                        "email=excluded.email, bind_time=excluded.bind_time, frozen=excluded.frozen, "
                        "updated_at=excluded.updated_at",
                        (gid, openid, name, rec.get("email") or "", int(rec.get("bind_time") or 0),
                         1 if rec.get("frozen") else 0, now))
                    users += 1
                    # 设备挂在"人"身上（用 store 的 _devices 兼容旧 uuid 字段）
                    dev_list = []
                    try:
                        dev_list = devices_of(rec) if devices_of else (rec.get("devices") or [])
                    except Exception:
                        dev_list = rec.get("devices") or []
                    for d in dev_list or []:
                        if not isinstance(d, dict) or not d.get("uuid"):
                            continue
                        c.execute(
                            "INSERT INTO whitelist_device(openid, uuid, platform, first_seen, last_seen, "
                            "src_group, src_name, updated_at) VALUES(?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(openid, uuid) DO UPDATE SET platform=excluded.platform, "
                            "first_seen=MIN(first_seen, excluded.first_seen), last_seen=MAX(last_seen, excluded.last_seen), "
                            "src_group=excluded.src_group, src_name=excluded.src_name, updated_at=excluded.updated_at",
                            (openid, str(d.get("uuid")), d.get("platform") or "",
                             int(d.get("first_seen") or 0), int(d.get("last_seen") or 0), gid, name, now))
                        devs += 1
                    for ct in (rec.get("cities") or []):
                        city = (ct or {}).get("city") if isinstance(ct, dict) else ct
                        if not city:
                            continue
                        c.execute(
                            "INSERT INTO whitelist_city(openid, city, ts, updated_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(openid, city) DO UPDATE SET ts=MAX(ts, excluded.ts), updated_at=excluded.updated_at",
                            (openid, str(city), int((ct or {}).get("ts") or 0) if isinstance(ct, dict) else 0, now))
                        cities += 1
        return {"users": users, "devices": devs, "cities": cities, "skipped_no_openid": skipped}

    # ── 查询（第 2 步"双读"会用；现在先提供便于对账） ──
    def names_of(self, openid: str) -> list:
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT name FROM whitelist_user WHERE openid=? ORDER BY updated_at DESC", (openid,))]

    def devices_of(self, openid: str) -> list:
        with self._conn() as c:
            return [dict(zip(["uuid", "platform", "first_seen", "last_seen"], r)) for r in c.execute(
                "SELECT uuid, platform, first_seen, last_seen FROM whitelist_device WHERE openid=?", (openid,))]

    def stats(self) -> dict:
        with self._conn() as c:
            q = lambda s: c.execute(s).fetchone()[0]
            return {"users": q("SELECT COUNT(*) FROM whitelist_user"),
                    "openids": q("SELECT COUNT(DISTINCT openid) FROM whitelist_user"),
                    "devices": q("SELECT COUNT(*) FROM whitelist_device"),
                    "cities": q("SELECT COUNT(*) FROM whitelist_city")}
