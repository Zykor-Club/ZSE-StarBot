# -*- coding: utf-8 -*-
"""种子投票引擎（持久化 + 计票 + 获胜判定）

仅负责数据与规则，不涉及 QQ / 网络。votes.json 路径由调用方传入。

持久化：JSON 文件 {vote_id: record}，tmp 写入 + os.replace 原子替换，
threading.Lock 保护并发读写。时间统一以 ISO 字符串落盘，比较时转回 epoch。

record 字段见 create_vote；user_votes 每人最多 2 个不同编号。
"""

import json
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime

# ── 常量 ──
MAX_VOTES_PER_USER = 2          # 每人最多持有的不同编号数
ONLINE_THRESHOLD_MIN = 3000     # 在线满 50 小时（分钟）→ 每票 1.5 分

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _iso(now=None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _epoch(iso) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _circle_no(no) -> str:
    try:
        n = int(no)
    except (TypeError, ValueError):
        return str(no)
    if 1 <= n <= len(_CIRCLED):
        return _CIRCLED[n - 1]
    return str(n)


def vote_weight(snapshot, openid) -> float:
    """该 openid 的每票权重：在线满 50 小时 → 1.5，否则 1.0"""
    if not snapshot:
        return 1.0
    minutes = snapshot.get(openid)
    if minutes is None and not isinstance(openid, str):
        minutes = snapshot.get(str(openid))
    try:
        minutes = float(minutes or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    return 1.5 if minutes >= ONLINE_THRESHOLD_MIN else 1.0


def _norm_option(opt: dict, no: int) -> dict:
    seeds = [str(s).strip() for s in (opt.get("seeds") or []) if str(s).strip()]
    name = str(opt.get("name") or "").strip() or "+".join(seeds)
    return {
        "no": int(opt.get("no") or no),
        "name": name,
        "seeds": seeds,
        "proposer": str(opt.get("proposer") or ""),
        "proposer_openid": str(opt.get("proposer_openid") or ""),
        # 创建时间：满员时按它顶掉最旧的一条（同一批创建用 no 兜底）
        "created_ts": float(opt.get("created_ts") or 0) or time.time(),
        "by_bot": bool(opt.get("by_bot")),
    }


def generate_random_options(seed_list, min_seeds=None, max_seeds=None, option_count=6):
    """从 seed_list 随机生成 option_count 个候选组合（每候选随机 2~4 个种子，组合去重）。

    返回 (options, err)：成功 err=None；seed_list 为空或 <2 时返回 ([], 提示)。
    """
    seeds = [str(s).strip() for s in (seed_list or []) if str(s).strip()]
    if len(seeds) < 2:
        return [], "可用种子不足 2 个，无法生成候选"
    lo = 2 if min_seeds is None else int(min_seeds)
    hi = 4 if max_seeds is None else int(max_seeds)
    lo = max(1, min(lo, len(seeds)))
    hi = max(1, min(hi, len(seeds)))
    if hi < lo:
        hi = lo
    index = {s: i for i, s in enumerate(seeds)}
    seen = set()
    options = []
    attempts = 0
    while len(options) < option_count and attempts < option_count * 40:
        attempts += 1
        k = random.randint(lo, hi)
        combo = tuple(sorted(random.sample(seeds, k), key=lambda s: index[s]))
        if combo in seen:
            continue
        seen.add(combo)
        options.append({
            "no": len(options) + 1,
            "name": "+".join(combo),
            "seeds": list(combo),
            "proposer": "机器人随机",
            "by_bot": True,
        })
    return options, None


def parse_candidates(text, proposer):
    """解析玩家提案：全角/半角分号分隔候选，`+` 连接段内种子。

    返回 (options, err)。候选 <2 或有空段 → err。
    """
    raw = str(text or "")
    segments = [seg.strip() for seg in re.split(r"[；;]", raw)]
    parsed = []
    for seg in segments:
        if not seg:
            continue
        seeds = [s.strip() for s in seg.split("+")]
        seeds = [s for s in seeds if s]
        if not seeds:
            return [], "存在空的候选，请用 + 连接种子"
        parsed.append(seeds)
    if len(parsed) < 2:
        return [], "至少需要 2 个候选，请用；分隔"
    options = []
    for i, seeds in enumerate(parsed):
        options.append({
            "no": i + 1,
            "name": "+".join(seeds),
            "seeds": seeds,
            "proposer": str(proposer or ""),
            "by_bot": False,
        })
    return options, None


def parse_server_index(args_text):
    """取首个空白分隔 token：纯数字 → (int, 剩余文本)；否则 (None, 原文)"""
    raw = str(args_text or "")
    stripped = raw.strip()
    parts = stripped.split(None, 1)
    if parts and parts[0].isdigit():
        rest = parts[1].strip() if len(parts) > 1 else ""
        return int(parts[0]), rest
    return None, stripped


class VoteStore:
    """种子投票存储与规则引擎（线程安全）"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._read()

    # ── 持久化 ──
    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_locked(self):
        tmp = self.path + ".tmp"
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self._data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ── 查询 ──
    def has_active(self, server_code) -> bool:
        with self._lock:
            return any(r.get("server_code") == server_code and r.get("status") == "open"
                       for r in self._data.values())

    def get_vote(self, vote_id):
        with self._lock:
            return self._data.get(vote_id)

    def get_active(self, server_code):
        with self._lock:
            for r in self._data.values():
                if r.get("server_code") == server_code and r.get("status") == "open":
                    return r
            return None

    def active_votes_for_gid(self, gid) -> list:
        g = str(gid)
        with self._lock:
            return [r for r in self._data.values()
                    if r.get("status") == "open" and g in [str(z) for z in (r.get("zone_gids") or [])]]

    def latest_vote_for_gid(self, gid, server_code=None) -> dict | None:
        """最近一场投票：server_code 指定服务器，否则按 gid 是否在 zone_gids 里筛选本群可见。
        进行中优先；没有进行中的则取创建时间最新的一场（含已结束）；无则 None"""
        g = str(gid)
        with self._lock:
            if server_code:
                pool = [r for r in self._data.values() if r.get("server_code") == server_code]
            else:
                pool = [r for r in self._data.values()
                        if g in [str(z) for z in (r.get("zone_gids") or [])]]
        if not pool:
            return None
        open_pool = [r for r in pool if r.get("status") == "open"]
        pool = open_pool or pool
        return max(pool, key=lambda r: str(r.get("created_at") or ""))

    # ── 创建 ──
    def create_vote(self, server_code, origin_gid, zone_gids, options,
                    snapshot=None, deadline_hours=24, update_interval_minutes=60,
                    title="下个档玩什么", now=None, max_options: int = 6) -> str:
        ts = time.time() if now is None else float(now)
        vote_id = f"{server_code}-{int(ts)}"
        with self._lock:
            # 极端情况下同一秒内"结束→重建"会撞 ID：撞车时追加随机后缀，避免覆盖旧记录
            if vote_id in self._data:
                vote_id = f"{vote_id}-{uuid.uuid4().hex[:6]}"
            if any(r.get("server_code") == server_code and r.get("status") == "open"
                   for r in self._data.values()):
                raise ValueError("该服务器已有进行中的投票")
            opts = [_norm_option(o, i + 1) for i, o in enumerate(options or [])]
            record = {
                "vote_id": vote_id,
                "server_code": server_code,
                "origin_gid": str(origin_gid),
                "zone_gids": [str(z) for z in (zone_gids or [])],
                "title": title or "下个档玩什么",
                "created_at": _iso(ts),
                "deadline": _iso(ts + float(deadline_hours) * 3600),
                "update_interval_minutes": int(update_interval_minutes),
                "last_update_at": _iso(ts),
                "status": "open",
                "closed_at": None,
                "used": False,
                "winner_no": None,
                "tie_random": False,
                "options": opts,
                # 选项上限（固定 6）：满员后新提案顶掉最旧的一条；卡片据此提示剩余空位
                "max_options": max(2, int(max_options or 6)),
                "snapshot": dict(snapshot or {}),
                "user_votes": {},
            }
            self._data[vote_id] = record
            self._write_locked()
        return vote_id

    # ── 投票切换 ──
    def toggle_vote(self, vote_id, openid, no) -> tuple:
        key = str(openid)
        with self._lock:
            r = self._data.get(vote_id)
            if not r:
                return False, "投票不存在"
            if r.get("status") != "open":
                return False, "投票已结束，无法投票"
            try:
                n = int(no)
            except (TypeError, ValueError):
                return False, "编号不存在"
            opt = next((o for o in r.get("options") or [] if int(o["no"]) == n), None)
            if opt is None:
                return False, "编号不存在"
            uv = r.setdefault("user_votes", {})
            mine = list(uv.get(key) or [])
            circ = _circle_no(n)
            name = opt.get("name") or ""
            if n in mine:
                mine.remove(n)
                uv[key] = mine
                self._write_locked()
                return True, f"已取消对 {circ} {name} 的投票"
            if len(mine) >= MAX_VOTES_PER_USER:
                return False, f"每人最多可投 {MAX_VOTES_PER_USER} 票，不能投更多了"
            mine.append(n)
            uv[key] = mine
            self._write_locked()
            return True, f"已投给 {circ} {name}"

    # ── 提案：追加 / 顶替 / 撤回 / 删除（仅进行中的投票） ──
    def _remove_option_locked(self, r, no: int) -> int:
        """删除某编号选项：**归还其票数**（从各用户投票列表移除 → 额度自动恢复），
        并把其后编号整体前移（票号同步重映射）。返回归还的票数。"""
        opts = r.get("options") or []
        try:
            no = int(no)
        except (TypeError, ValueError):
            return 0
        if not any(int(o.get("no") or -1) == no for o in opts):
            return 0
        uv = r.get("user_votes") or {}
        refunded = 0
        for key, nos in list(uv.items()):
            new = []
            for n in nos or []:
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                if n == no:
                    refunded += 1          # 归还：移除该票，用户可再投给别人
                    continue
                new.append(n - 1 if n > no else n)
            uv[key] = new
        r["options"] = [dict(o, no=(int(o["no"]) - 1 if int(o["no"]) > no else int(o["no"])))
                        for o in opts if int(o.get("no") or -1) != no]
        return refunded

    def add_option(self, vote_id, name, seeds, proposer_openid="", proposer_name="",
                   max_options: int = 6, per_user: int = 2):
        """追加一个提案选项。返回 (ok, msg, info)。

        规则：① 组合不能与现有选项重复；② 同一人最多 per_user 条在场；
        ③ 满 max_options 时**顶掉最旧的一条**（其票数归还，其余编号前移），新提案成为最后一位。
        """
        with self._lock:
            r = self._data.get(vote_id)
            if not r:
                return False, "投票不存在", {}
            if r.get("status") != "open":
                return False, "投票已结束，无法提案", {}
            opts = r.setdefault("options", [])
            norm = tuple(sorted(str(x).strip().lower() for x in (seeds or []) if str(x).strip()))
            if not norm:
                return False, "种子组合为空", {}
            for o in opts:
                cur = tuple(sorted(str(x).strip().lower() for x in (o.get("seeds") or [])))
                if cur == norm:
                    return False, f"已存在相同提案（{_circle_no(o.get('no'))} {o.get('name')}）", {}
            openid = str(proposer_openid or "")
            if openid:
                mine = sum(1 for o in opts if str(o.get("proposer_openid") or "") == openid)
                if mine >= int(per_user):
                    return False, f"你最多只能有 {per_user} 条在场提案，先撤回一条吧", {}
            info = {"replaced": "", "refunded": 0, "no": 0}
            if len(opts) >= int(max_options):
                oldest = min(opts, key=lambda o: (float(o.get("created_ts") or 0), int(o.get("no") or 0)))
                info["refunded"] = self._remove_option_locked(r, int(oldest["no"]))
                info["replaced"] = oldest.get("name") or ""
                info["replaced_by_bot"] = bool(oldest.get("by_bot"))
                # 重要：_remove_option_locked 是**整体替换** r["options"]（编号前移），
                # 这里必须重新取引用，否则新提案会被追加到已废弃的旧列表上（曾导致提案丢失）
                opts = r.get("options") or []
            no = (max([int(o["no"]) for o in opts]) + 1) if opts else 1
            opts.append({
                "no": no, "name": name, "seeds": list(seeds or []),
                "proposer": str(proposer_name or ""), "proposer_openid": openid,
                "created_ts": time.time(), "by_bot": False,
            })
            opts.sort(key=lambda o: int(o["no"]))
            info["no"] = no
            self._write_locked()
            return True, f"已提案：{_circle_no(no)} {name}", info

    def remove_option(self, vote_id, no, openid="", admin: bool = False):
        """撤回/删除提案：本人可撤自己的，管理员可删任意（含机器人随机项）。
        返回 (ok, msg, info)；info 含 refunded（归还票数）与 name。"""
        with self._lock:
            r = self._data.get(vote_id)
            if not r:
                return False, "投票不存在", {}
            if r.get("status") != "open":
                return False, "投票已结束，无法修改提案", {}
            try:
                n = int(no)
            except (TypeError, ValueError):
                return False, "编号不存在", {}
            opt = next((o for o in r.get("options") or [] if int(o.get("no") or -1) == n), None)
            if opt is None:
                return False, f"编号 {n} 不存在", {}
            if not admin and str(opt.get("proposer_openid") or "") != str(openid or ""):
                return False, "只能撤回自己提出的提案（管理员可用 删除提案）", {}
            name = opt.get("name") or ""
            by_bot = bool(opt.get("by_bot"))
            refunded = self._remove_option_locked(r, n)
            self._write_locked()
            return True, ("已移除机器人随机提案：" if by_bot else "已移除提案：") + f"{_circle_no(n)} {name}", \
                {"refunded": refunded, "name": name, "by_bot": by_bot}

    # ── 计票 ──
    def _tally_locked(self, r) -> dict:
        opts = r.get("options") or []
        snapshot = r.get("snapshot") or {}
        user_votes = r.get("user_votes") or {}
        counts = {int(o["no"]): 0 for o in opts}
        scores = {int(o["no"]): 0.0 for o in opts}
        for openid, nos in user_votes.items():
            w = vote_weight(snapshot, openid)
            for n in nos or []:
                if int(n) in counts:
                    counts[int(n)] += 1
                    scores[int(n)] += w
        total = sum(scores.values())
        result = []
        for o in sorted(opts, key=lambda x: int(x["no"])):
            n = int(o["no"])
            sc = scores[n]
            pct = round(sc / total * 100, 1) if total > 0 else 0.0
            result.append({
                "no": n,
                "name": o.get("name") or "",
                "proposer": o.get("proposer") or "",
                "by_bot": bool(o.get("by_bot")),
                "score": sc,
                "votes": counts[n],
                "percent": pct,
            })
        return {"total_score": float(total), "options": result}

    def tally(self, vote_id) -> dict:
        with self._lock:
            r = self._data.get(vote_id)
            if not r:
                return {"total_score": 0.0, "options": []}
            return self._tally_locked(r)

    def compute_winner(self, vote_id):
        with self._lock:
            r = self._data.get(vote_id)
            if not r:
                return None
            t = self._tally_locked(r)
        opts = t["options"]
        if not opts:
            return None
        # 0 票保护：没有任何有效投票时不产生获胜组合
        if sum(o["votes"] for o in opts) <= 0:
            return None
        best = max(o["score"] for o in opts)
        cand = [o for o in opts if abs(o["score"] - best) < 1e-9]
        tie = False
        if len(cand) > 1:
            best_votes = max(o["votes"] for o in cand)
            cand2 = [o for o in cand if o["votes"] == best_votes]
            if len(cand2) > 1:
                cand = cand2
                tie = True
            else:
                cand = cand2
        w = random.choice(cand)
        return {"no": w["no"], "name": w["name"], "score": w["score"],
                "votes": w["votes"], "tie_random": tie}

    def finish_vote(self, vote_id, now=None):
        with self._lock:
            r = self._data.get(vote_id)
            if not r:
                return None
            if r.get("status") == "closed":
                return self._winner_from_record_locked(r)
            r["status"] = "closed"
            r["closed_at"] = _iso(now)
            # 结果卡发布标记：先置 False，发卡成功后才置 True。
            # 若机器人正好在 finish 与发卡之间重启，调度会据此外补发（见 unpublished_closed）。
            r["result_published"] = False
            t = self._tally_locked(r)
            winner = self._pick_winner(t["options"])
            if winner:
                r["winner_no"] = winner["no"]
                r["tie_random"] = winner["tie_random"]
            self._write_locked()
            return winner

    @staticmethod
    def _pick_winner(opts):
        if not opts:
            return None
        # 0 票保护：没有任何有效投票时不产生获胜组合
        if sum(o["votes"] for o in opts) <= 0:
            return None
        best = max(o["score"] for o in opts)
        cand = [o for o in opts if abs(o["score"] - best) < 1e-9]
        tie = False
        if len(cand) > 1:
            best_votes = max(o["votes"] for o in cand)
            cand2 = [o for o in cand if o["votes"] == best_votes]
            if len(cand2) > 1:
                tie = True
            cand = cand2
        w = random.choice(cand)
        return {"no": w["no"], "name": w["name"], "score": w["score"],
                "votes": w["votes"], "tie_random": tie}

    def _winner_from_record_locked(self, r):
        wn = r.get("winner_no")
        if wn is None:
            return None
        t = self._tally_locked(r)
        o = next((x for x in t["options"] if x["no"] == int(wn)), None)
        if not o:
            return None
        return {"no": o["no"], "name": o["name"], "score": o["score"],
                "votes": o["votes"], "tie_random": bool(r.get("tie_random"))}

    def mark_result_published(self, vote_id) -> bool:
        """标记该投票的结果卡已发出（补发判断用）"""
        with self._lock:
            r = self._data.get(vote_id)
            if not r:
                return False
            r["result_published"] = True
            self._write_locked()
            return True

    def unpublished_closed(self, min_age: float = 0.0, now=None) -> list:
        """已 closed 但结果卡还没发出去的投票（用于崩溃/重启后补发）。

        min_age>0 时只返回"结束已超过 min_age 秒"的记录，避免与正在进行的发布抢跑。

        只认**显式标记为 False** 的记录：升级前就已 closed 的老记录没有该字段，
        会被视为已发布，避免升级后把历史投票结果又播一遍。
        """
        ts = time.time() if now is None else float(now)
        with self._lock:
            out = []
            for r in self._data.values():
                if r.get("status") != "closed" or r.get("result_published") is not False:
                    continue
                # min_age：跳过"刚结束"的投票——它可能正被结束投票指令/同一轮调度发布中，
                # 否则会出现"结果卡发两轮"（每个群多一张卡）
                if min_age and ts - _epoch(r.get("closed_at")) < float(min_age):
                    continue
                out.append({"vote_id": r.get("vote_id"), "server_code": r.get("server_code")})
            return out

    # ── 结果领取 ──
    def _pending_locked(self, server_code):
        cands = [r for r in self._data.values()
                 if r.get("server_code") == server_code and r.get("status") == "closed"
                 and not r.get("used")]
        if not cands:
            return None
        return max(cands, key=lambda r: _epoch(r.get("closed_at")))

    def pending_result(self, server_code):
        with self._lock:
            r = self._pending_locked(server_code)
            if not r:
                return None
            return {"vote_id": r["vote_id"],
                    "winner": self._winner_from_record_locked(r),
                    "closed_at": r.get("closed_at")}

    def mark_result_used(self, server_code) -> bool:
        with self._lock:
            r = self._pending_locked(server_code)
            if not r:
                return False
            r["used"] = True
            self._write_locked()
            return True

    # ── 定时任务 ──
    def due_updates(self, now=None) -> list:
        ts = time.time() if now is None else float(now)
        with self._lock:
            out = []
            for r in self._data.values():
                if r.get("status") != "open":
                    continue
                interval = float(r.get("update_interval_minutes") or 0) * 60
                if ts - _epoch(r.get("last_update_at")) >= interval:
                    out.append(r)
            return out

    def mark_update_sent(self, vote_id, now=None):
        with self._lock:
            r = self._data.get(vote_id)
            if r:
                r["last_update_at"] = _iso(now)
                self._write_locked()

    def due_closes(self, now=None) -> list:
        ts = time.time() if now is None else float(now)
        with self._lock:
            return [r for r in self._data.values()
                    if r.get("status") == "open" and ts >= _epoch(r.get("deadline"))]

    # ── 清理 ──
    def remove_server(self, server_code) -> int:
        """删除服务器时清理该服务器的全部投票记录（含进行中/已结束）。返回删除条数"""
        code = str(server_code or "")
        if not code:
            return 0
        with self._lock:
            ids = [k for k, r in self._data.items() if r.get("server_code") == code]
            if not ids:
                return 0
            for k in ids:
                self._data.pop(k, None)
            self._write_locked()
            return len(ids)


# ─────────────────────────── 自测 ───────────────────────────
def _selftest():
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="vote_store_test_")
    path = os.path.join(tmpdir, "votes.json")
    S = "SRV"

    def opts(*names):
        return [{"no": i + 1, "name": n, "seeds": n.split("+"),
                 "proposer": "玩家" + str(i + 1), "by_bot": False}
                for i, n in enumerate(names)]

    # 1. 创建 / 持久化重载恢复
    store = VoteStore(path)
    assert store.has_active(S) is False
    vid = store.create_vote(S, "g_origin", ["g1", "g2"], opts("a+b", "c+d", "e+f"),
                            snapshot={"u1": 4000, "u2": 100}, deadline_hours=1,
                            update_interval_minutes=1, now=1000000)
    assert vid == f"{S}-1000000", vid
    assert store.has_active(S) is True
    # 已有活动投票 → raise
    try:
        store.create_vote(S, "g_origin", ["g1"], opts("x+y", "z+w"), now=1000001)
        raise AssertionError("应抛 ValueError")
    except ValueError:
        pass
    # 重载
    store2 = VoteStore(path)
    v = store2.get_vote(vid)
    assert v and v["title"] == "下个档玩什么" and v["status"] == "open"
    assert v["options"][0]["name"] == "a+b" and v["zone_gids"] == ["g1", "g2"]

    # 2. has_active / active_votes_for_gid
    assert store2.get_active(S)["vote_id"] == vid
    assert len(store2.active_votes_for_gid("g1")) == 1
    assert len(store2.active_votes_for_gid("g2")) == 1
    assert store2.active_votes_for_gid("nope") == []
    # 同一 openid 跨群（g1/g2 都命中同一 record）→ 去重
    assert store2.active_votes_for_gid("g1")[0]["vote_id"] == store2.active_votes_for_gid("g2")[0]["vote_id"]

    # 3. toggle 语义
    ok, msg = store2.toggle_vote(vid, "u1", 1)
    assert ok and "已投给" in msg, msg
    ok, msg = store2.toggle_vote(vid, "u1", 2)
    assert ok and "已投给" in msg, msg
    ok, msg = store2.toggle_vote(vid, "u1", 3)          # 超出 2 票
    assert ok is False and "2" in msg, msg
    ok, msg = store2.toggle_vote(vid, "u1", 1)          # 取消
    assert ok and "已取消" in msg, msg
    ok, msg = store2.toggle_vote(vid, "u1", 99)         # 编号不存在
    assert ok is False and "编号不存在" in msg, msg
    assert store2.get_vote(vid)["user_votes"]["u1"] == [2]
    store2.toggle_vote(vid, "u1", 2)                    # 清空 u1，便于后续计票
    assert store2.get_vote(vid)["user_votes"]["u1"] == []

    # 4. 权重 1.5 / 1.0
    assert vote_weight({"u1": 3000}, "u1") == 1.5
    assert vote_weight({"u1": 2999}, "u1") == 1.0
    assert vote_weight({}, "u1") == 1.0

    # 5. 计票 + 获胜（三档：总分→票数→平票随机）
    #    u1 权重 1.5
    store2.toggle_vote(vid, "u1", 1)      # u1(1.5) -> opt1
    store2.toggle_vote(vid, "u2", 1)      # u2(1.0) -> opt1
    store2.toggle_vote(vid, "u3", 1)      # u3(1.0) -> opt1
    store2.toggle_vote(vid, "u4", 2)      # u4(1.0) -> opt2
    t = store2.tally(vid)
    assert abs(t["total_score"] - 4.5) < 1e-9, t
    o1 = next(x for x in t["options"] if x["no"] == 1)
    o2 = next(x for x in t["options"] if x["no"] == 2)
    assert o1["votes"] == 3 and abs(o1["score"] - 3.5) < 1e-9, o1
    assert o2["votes"] == 1 and abs(o2["score"] - 1.0) < 1e-9, o2
    assert abs(o1["percent"] - 77.8) < 0.05, o1
    w = store2.compute_winner(vid)
    assert w["no"] == 1 and not w["tie_random"], w

    # 平票构造：新建服务器，两选项各 1 票 1.0 → 随机 tie_random
    S2 = "SRV2"
    vid2 = store2.create_vote(S2, "g", ["g"], opts("p+q", "r+s"), now=2000000,
                              deadline_hours=1, update_interval_minutes=1)
    store2.toggle_vote(vid2, "a", 1)
    store2.toggle_vote(vid2, "b", 2)
    t2 = store2.tally(vid2)
    assert t2["options"][0]["score"] == t2["options"][1]["score"] == 1.0, t2
    random.seed(7)
    w2 = store2.compute_winner(vid2)
    assert w2["tie_random"] is True and w2["no"] in (1, 2), w2
    # 票数档：总分并列（各 1.0）不触发随机 → 去重后唯一
    #   改为总分并列但票数不同：opt1 两票 0.5? 权重只有 1/1.5 → 用 u(u1 1.5) vs 两票 1.0
    S3 = "SRV3"
    vid3 = store2.create_vote(S3, "g", ["g"], opts("m+n", "o+p"), now=3000000,
                              deadline_hours=1, update_interval_minutes=1)
    store2.toggle_vote(vid3, "h1", 1)             # 1.5
    store2.toggle_vote(vid3, "h2", 1)             # 1.5
    store2.toggle_vote(vid3, "n1", 2)             # 1.0
    store2.toggle_vote(vid3, "n2", 2)             # 1.0
    store2.toggle_vote(vid3, "n3", 2)             # 1.0
    store2.get_vote(vid3)["snapshot"] = {"h1": 5000, "h2": 5000,
                                         "n1": 10, "n2": 10, "n3": 10}
    store2._write_locked()
    t3 = store2.tally(vid3)
    assert abs(t3["options"][0]["score"] - t3["options"][1]["score"]) < 1e-9, t3
    assert t3["options"][0]["score"] == 3.0 and t3["options"][1]["votes"] == 3, t3
    w3 = store2.compute_winner(vid3)
    assert w3["no"] == 2 and w3["tie_random"] is False, w3   # 总分并列→票数多者胜

    # 6. 随机生成 / 指定解析 / 服务器索引
    gen, err = generate_random_options(["a", "b", "c", "d", "e"], option_count=6)
    assert err is None and len(gen) == 6, (gen, err)
    assert all(2 <= len(o["seeds"]) <= 4 and o["by_bot"] and o["proposer"] == "机器人随机" for o in gen)
    assert len({o["name"] for o in gen}) == len(gen), "组合应去重"
    assert generate_random_options(["a"])[1] is not None
    assert generate_random_options([])[1] is not None
    pc, err = parse_candidates("饥荒+下雨；颠倒世界;醉酒", "星梦")
    assert err is None and len(pc) == 3, (pc, err)
    assert pc[0]["name"] == "饥荒+下雨" and pc[0]["proposer"] == "星梦" and pc[0]["by_bot"] is False
    assert pc[1]["seeds"] == ["颠倒世界"] and pc[2]["seeds"] == ["醉酒"]
    assert parse_candidates("只有一个", "x")[1] is not None
    assert parse_server_index("1 饥荒+下雨") == (1, "饥荒+下雨")
    assert parse_server_index("多 行  文本") == (None, "多 行  文本")

    # 7. finish / pending_result / mark_result_used
    win = store2.finish_vote(vid, now=1000100)
    assert win and win["no"] == 1, win
    vv = store2.get_vote(vid)
    assert vv["status"] == "closed" and vv["winner_no"] == 1 and vv["closed_at"]
    # 已 closed 再 finish → 返回现有 winner
    assert store2.finish_vote(vid)["no"] == 1
    assert store2.has_active(S) is False
    # toggle 已截止
    ok, msg = store2.toggle_vote(vid, "zzz", 1)
    assert ok is False and "已结束" in msg, msg
    store2.finish_vote(vid2, now=2000100)
    # pending_result（SRV 与 SRV2 都 closed；SRV2 未 used）
    pr = store2.pending_result(S2)
    assert pr and pr["vote_id"] == vid2 and pr["winner"], pr
    assert store2.pending_result(S) is not None
    assert store2.mark_result_used(S2) is True
    assert store2.pending_result(S2) is None
    assert store2.mark_result_used(S2) is False

    # 7.1 latest_vote_for_gid（进行中优先 / 已结束兜底 / 无则 None）
    assert store2.latest_vote_for_gid("g1")["vote_id"] == vid        # 本群仅一场且已结束 → 兜底返回
    assert store2.latest_vote_for_gid("g")["vote_id"] == vid3        # 本群含进行中 → 优先进行中
    assert store2.latest_vote_for_gid("nope") is None
    assert store2.latest_vote_for_gid("g", server_code=S)["vote_id"] == vid
    assert store2.latest_vote_for_gid("g", server_code=S2)["vote_id"] == vid2
    assert store2.latest_vote_for_gid("g", server_code="NOPE") is None

    # 8. due_updates / due_closes（用仍 open 的 vid3；vid 已 closed 不应再出现）
    due = store2.due_updates(now=3000060)          # interval=1min=60s
    assert any(r["vote_id"] == vid3 for r in due), due
    assert all(r["vote_id"] != vid3 for r in store2.due_updates(now=3000030))
    store2.mark_update_sent(vid3, now=3000060)
    assert all(r["vote_id"] != vid3 for r in store2.due_updates(now=3000100))
    # deadline = 3000000 + 3600 = 3003600
    assert any(r["vote_id"] == vid3 for r in store2.due_closes(now=3003600))
    assert all(r["vote_id"] != vid3 for r in store2.due_closes(now=3003000))
    # 已 closed 不再出现在 due 列表
    assert all(r["vote_id"] != vid for r in store2.due_updates(now=9999999))
    assert all(r["vote_id"] != vid for r in store2.due_closes(now=9999999))

    print(f"vote_store 自测全部通过（临时目录：{tmpdir}）")


if __name__ == "__main__":
    _selftest()