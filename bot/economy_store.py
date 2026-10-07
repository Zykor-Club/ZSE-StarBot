# -*- coding: utf-8 -*-
"""喵币账本（机器人侧 SQLite，全联合体系 + 全服务器共用一份）

为什么放机器人侧：插件数据库是**每个服务器各一份**，放插件就做不到"全联合体系共用"；
机器人本来就是绑定/白名单/投票的唯一数据源，签到也在机器人端发生。

**主键选择（参考 CaiBotLite 的 User 模型）**：钱包按 **QQ 用户 openid** 一人一份，
玩家名只是展示字段 → 于是：
  · 同名不同人 → 各自一份钱包（不会互相花掉）；跨群/跨服同一个人 → 共用一份 ✅
  · 改名不掉分（行跟 openid 走）；绑了多个玩家名 = 同一份钱包（同一个人）✅
  · 签到幂等按 (openid, 自然日) → 一人一天一次，堵住"绑多名字重复领"

并发与结算要点（都靠 SQL 层保证，不做 read-modify-write）：
  1) 写事务用 BEGIN IMMEDIATE（SQLite 只允许一个写者）→ 先读后写也安全；
  2) 条件式 UPDATE：签到带 last_sign_date <> today，扣款带 balance >= amount → **余额永不为负**；
  3) ref 建 UNIQUE 索引 → 重复发放即使上层漏判也插不进去；
  4) 流水与余额同事务 → 不会"加了钱没流水"；
  5) 全程整数，无浮点。
"""

import os
import sqlite3
import threading
import time

DEFAULT_DB = "C:/bot/economy.db"
MAX_BALANCE = 1_000_000_000          # 余额上限
MAX_ADD = 10000                      # 单笔加分上限（防上层异常乱发）
SIGN_BASE = (15, 35)                 # 签到基础随机区间
SIGN_BONUS = (3, 9)                  # 连续签到额外随机（第 2 天起）
MILESTONES = {7: 20, 30: 100, 100: 300, 365: 500}   # 连续天数里程碑（一次性）

DDL = [
    """CREATE TABLE IF NOT EXISTS economy (
        openid         TEXT PRIMARY KEY,
        name           TEXT    NOT NULL DEFAULT '',
        balance        INTEGER NOT NULL DEFAULT 0,
        total_earned   INTEGER NOT NULL DEFAULT 0,
        total_spent    INTEGER NOT NULL DEFAULT 0,
        streak         INTEGER NOT NULL DEFAULT 0,
        last_sign_date TEXT    NOT NULL DEFAULT '',
        frozen_at      INTEGER NOT NULL DEFAULT 0,
        online_seconds INTEGER NOT NULL DEFAULT 0,
        cycle_world_id INTEGER NOT NULL DEFAULT 0,
        updated_at     INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS economy_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        openid        TEXT    NOT NULL,
        name          TEXT    NOT NULL DEFAULT '',
        delta         INTEGER NOT NULL,
        reason        TEXT    NOT NULL DEFAULT '',
        ref           TEXT    UNIQUE,
        balance_after INTEGER NOT NULL,
        ts            INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_economy_log_openid ON economy_log(openid)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_economy_sign_day ON economy_log(ref)",
    "CREATE INDEX IF NOT EXISTS idx_economy_balance ON economy(balance DESC)",
]


class Duplicate(Exception):
    """幂等命中：该 ref 已发放"""


class EconomyStore:
    def __init__(self, path: str = ""):
        self.path = path or os.environ.get("ECONOMY_DB") or DEFAULT_DB
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            for sql in DDL:
                c.execute(sql)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    # ── 基础读取 ──
    def get(self, openid: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM economy WHERE openid=?", (openid,)).fetchone()
            if row is None:
                return {"openid": openid, "balance": 0, "total_earned": 0, "total_spent": 0,
                        "streak": 0, "last_sign_date": "", "frozen_at": 0, "exists": False}
            d = dict(row)
            d["exists"] = True
            return d

    def _ensure(self, c, openid: str):
        c.execute("INSERT OR IGNORE INTO economy(openid, updated_at) VALUES(?, ?)",
                  (openid, int(time.time())))

    def today_rank(self, openid: str, day_start_ts: int) -> int:
        """今天第几个签到（按 UTC+8 自然日的流水顺序）"""
        with self._conn() as c:
            row = c.execute("SELECT id FROM economy_log WHERE openid=? AND reason LIKE 'sign:%' "
                            "AND ts >= ? ORDER BY id ASC LIMIT 1", (openid, int(day_start_ts))).fetchone()
            if row is None:
                return 0
            return int(c.execute("SELECT COUNT(*) FROM economy_log WHERE reason LIKE 'sign:%' "
                                 "AND ts >= ? AND id <= ?", (int(day_start_ts), row["id"])).fetchone()[0])

    def last_sign_ts(self, openid: str) -> int:
        with self._conn() as c:
            r = c.execute("SELECT ts FROM economy_log WHERE openid=? AND reason LIKE 'sign:%' "
                          "ORDER BY id DESC LIMIT 1", (openid,)).fetchone()
            return int(r["ts"]) if r else 0

    def last_play_hour(self, openid: str) -> int:
        """在线时长已结算到第几个小时（从流水 ref 推导；参数化 SQL，无需转义）"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT ref FROM economy_log WHERE openid = ? AND reason = ? AND ref LIKE ? "
                "ORDER BY id DESC LIMIT 60",
                (openid, "playtime", "playtime:%")).fetchall()
        best = 0
        for r in rows:
            try:
                best = max(best, int(str(r["ref"]).rsplit(":", 1)[-1]))
            except (TypeError, ValueError):
                continue
        return best

    def rank_of(self, openid: str, by: str = "balance") -> int:
        col = "total_earned" if by == "earned" else "balance"
        with self._conn() as c:
            row = c.execute(f"SELECT {col} AS v FROM economy WHERE openid=?", (openid,)).fetchone()
            if row is None:
                return 0
            higher = c.execute(f"SELECT COUNT(*) AS n FROM economy WHERE {col} > ?", (row["v"],)).fetchone()["n"]
            return int(higher) + 1

    def top(self, by: str = "balance", offset: int = 0, limit: int = 50) -> list:
        col = "total_earned" if by == "earned" else "balance"
        with self._conn() as c:
            rows = c.execute(
                f"SELECT openid, name, balance, total_earned, streak, frozen_at FROM economy "
                f"ORDER BY {col} DESC, updated_at ASC LIMIT ? OFFSET ?", (int(limit), int(offset))).fetchall()
            return [dict(r) for r in rows]

    def logs(self, openid: str, offset: int = 0, limit: int = 10) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT delta, reason, ref, balance_after, ts FROM economy_log WHERE openid=? "
                "ORDER BY id DESC LIMIT ? OFFSET ?", (openid, int(limit), int(offset))).fetchall()
            return [dict(r) for r in rows]

    # ── 记账（含流水，同事务） ──
    def _log(self, c, openid: str, delta: int, reason: str, ref, balance_after: int, name: str = ""):
        try:
            c.execute("INSERT INTO economy_log(openid, name, delta, reason, ref, balance_after, ts) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (openid, name, int(delta), reason, ref or None, int(balance_after), int(time.time())))
        except sqlite3.IntegrityError as e:
            raise Duplicate(str(e))

    def add(self, openid: str, amount: int, reason: str, ref: str = "", max_add: int = MAX_ADD, name: str = "") -> tuple:
        """加分。返回 (ok, 说明, 余额)。ref 非空时幂等。"""
        amount = int(amount)
        if amount <= 0:
            return False, "加分数额必须为正", self.get(openid)["balance"]
        if amount > max_add:
            return False, f"单笔加分超过上限（{max_add}）", self.get(openid)["balance"]
        with self._lock, self._conn() as c:
            try:
                c.execute("BEGIN IMMEDIATE")
                self._ensure(c, openid)
                row = c.execute("SELECT balance, frozen_at FROM economy WHERE openid=?", (openid,)).fetchone()
                if row["frozen_at"] > 0:
                    c.execute("ROLLBACK")
                    return False, "该账号已因退群冻结，暂时不能获得喵币", row["balance"]
                if row["balance"] + amount > MAX_BALANCE:
                    c.execute("ROLLBACK")
                    return False, "余额已达上限", row["balance"]
                cur = c.execute(
                    "UPDATE economy SET balance = balance + ?, total_earned = total_earned + ?, "
                    "updated_at = ? WHERE openid = ? AND frozen_at = 0",
                    (amount, amount, int(time.time()), openid))
                if cur.rowcount == 0:
                    c.execute("ROLLBACK")
                    return False, "写入失败（账号状态已变化）", row["balance"]
                after = c.execute("SELECT balance FROM economy WHERE openid=?", (openid,)).fetchone()["balance"]
                self._log(c, openid, amount, reason, ref, after)
                c.execute("COMMIT")
                return True, "ok", after
            except Duplicate:
                c.execute("ROLLBACK")
                return False, "该笔奖励已发放（幂等）", self.get(openid)["balance"]
            except sqlite3.Error as e:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return False, f"记账失败：{e}", 0

    def spend(self, openid: str, amount: int, reason: str, ref: str = "", name: str = "") -> tuple:
        """扣款。余额不足/冻结则拒绝；**不产生负数**。返回 (ok, 说明, 余额, 实际扣减)"""
        amount = int(amount)
        if amount <= 0:
            return False, "扣减数额必须为正", 0, 0
        with self._lock, self._conn() as c:
            try:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT balance, frozen_at FROM economy WHERE openid=?", (openid,)).fetchone()
                if row is None:
                    c.execute("ROLLBACK")
                    return False, "账号不存在", 0, 0
                if row["frozen_at"] > 0:
                    c.execute("ROLLBACK")
                    return False, "该账号已冻结", row["balance"], 0
                if row["balance"] < amount:
                    c.execute("ROLLBACK")
                    return False, f"喵币不足（当前 {row['balance']}）", row["balance"], 0
                cur = c.execute(
                    "UPDATE economy SET balance = balance - ?, total_spent = total_spent + ?, updated_at = ? "
                    "WHERE openid = ? AND frozen_at = 0 AND balance >= ?",
                    (amount, amount, int(time.time()), openid, amount))
                if cur.rowcount == 0:
                    c.execute("ROLLBACK")
                    return False, "扣减失败（并发或余额不足）", row["balance"], 0
                after = c.execute("SELECT balance FROM economy WHERE openid=?", (openid,)).fetchone()["balance"]
                self._log(c, openid, -amount, reason, ref, after)
                c.execute("COMMIT")
                return True, "ok", after, amount
            except Duplicate:
                c.execute("ROLLBACK")
                return False, "该笔扣减已处理（幂等）", self.get(openid)["balance"], 0
            except sqlite3.Error as e:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return False, f"记账失败：{e}", 0, 0

    def sign(self, openid: str, today: str, base: int, bonus_roll: int, name: str = "") -> tuple:
        """签到：**连续天数由本层按 last_sign_date 推导**；里程碑一次性。
        返回 (ok, 说明, 余额, 连续天数, 本次总额, 其中连续/里程碑奖励)"""
        base = int(base)
        if base <= 0 or base > 1000:
            return False, "签到数额异常", 0, 0, 0, 0
        with self._lock, self._conn() as c:
            try:
                c.execute("BEGIN IMMEDIATE")
                self._ensure(c, openid)
                row = c.execute("SELECT * FROM economy WHERE openid=?", (openid,)).fetchone()
                if row["frozen_at"] > 0:
                    c.execute("ROLLBACK")
                    return False, "该账号已因退群冻结，暂时不能签到", row["balance"], row["streak"], 0, 0
                last = row["last_sign_date"] or ""
                if last == today:
                    c.execute("ROLLBACK")
                    return False, "今天已经签到过了", row["balance"], row["streak"], 0, 0
                if last > today:                      # 字符串即 ISO 日期，可直接比较
                    c.execute("ROLLBACK")
                    return False, "日期异常（不能回退）", row["balance"], row["streak"], 0, 0
                streak = 1
                if last:
                    try:
                        from datetime import date
                        gap = (date.fromisoformat(today) - date.fromisoformat(last)).days
                    except ValueError:
                        gap = 99
                    # 日期由机器人自己计算，跨多天是正常行为（断签）→ 只把连续数重置为 1，不拒绝
                    if gap == 1:
                        streak = int(row["streak"]) + 1
                extra_bonus = int(bonus_roll) if streak >= 2 else 0
                milestone = MILESTONES.get(streak, 0)
                total = base + extra_bonus + milestone
                if row["balance"] + total > MAX_BALANCE:
                    c.execute("ROLLBACK")
                    return False, "余额已达上限", row["balance"], row["streak"], 0, 0
                cur = c.execute(
                    "UPDATE economy SET balance = balance + ?, total_earned = total_earned + ?, "
                    "name = ?, streak = ?, last_sign_date = ?, updated_at = ? "
                    "WHERE openid = ? AND last_sign_date = ? AND frozen_at = 0",
                    (total, total, name, streak, today, int(time.time()), openid, last))
                if cur.rowcount == 0:
                    c.execute("ROLLBACK")
                    return False, "重复的签到请求（并发保护）", row["balance"], row["streak"], 0, 0
                after = c.execute("SELECT balance FROM economy WHERE openid=?", (openid,)).fetchone()["balance"]
                self._log(c, openid, total, f"sign:streak={streak}", f"sign:{openid}:{today}", after, name)
                c.execute("COMMIT")
                return True, "ok", after, streak, total, extra_bonus + milestone
            except Duplicate:
                c.execute("ROLLBACK")
                return False, "今天已经签到过了（幂等）", self.get(openid)["balance"], 0, 0, 0
            except sqlite3.Error as e:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return False, f"签到失败：{e}", 0, 0, 0, 0

    # ── 冻结 / 清零 ──
    def freeze(self, openid: str, frozen: bool) -> bool:
        with self._lock, self._conn() as c:
            self._ensure(c, openid)
            cur = c.execute("UPDATE economy SET frozen_at = ?, updated_at = ? WHERE openid = ?",
                            (int(time.time()) if frozen else 0, int(time.time()), openid))
            return cur.rowcount > 0

    def reset_all(self) -> int:
        """把所有余额清零（保留账号行与全部流水）；返回受影响账号数"""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT openid, balance FROM economy WHERE balance > 0").fetchall()
            for r in rows:
                c.execute("UPDATE economy SET balance = 0, updated_at = ? WHERE openid = ?",
                          (int(time.time()), r["openid"]))
                self._log(c, r["openid"], -r["balance"], "admin_reset", None, 0)
            return len(rows)

    def clear_frozen(self, days: int = 7) -> int:
        """冻结超过 days 天 → 余额清零（保留账号行与全部流水，便于审计）"""
        limit = int(time.time()) - int(days) * 86400
        cleared = 0
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT openid, balance, frozen_at FROM economy "
                             "WHERE frozen_at > 0 AND frozen_at <= ? AND balance > 0", (limit,)).fetchall()
            for r in rows:
                c.execute("UPDATE economy SET balance = 0, updated_at = ? WHERE openid = ?",
                          (int(time.time()), r["openid"]))
                self._log(c, r["openid"], -r["balance"], "frozen_clear",
                          f"frozenclear:{r['openid']}:{r['frozen_at']}", 0)
                cleared += 1
        return cleared
