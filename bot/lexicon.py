# -*- coding: utf-8 -*-
"""泰拉瑞亚图鉴检索（物品 / 生物 / 弹幕 / 增益 / 修饰语）

数据来自 CaiBotLite 的 assets（terraria_data/*.json + images/*），由
scripts/deploy_lexicon_assets.py 打包上传到 C:/bot/assets/lexicon/。
匹配规则（纯 stdlib，不引入 fuzzywuzzy）：ID 精确 → 名称/别名精确 → 前缀 → 包含 → 相似度(>=85)。
"""

import difflib
import json
import os
import re
import threading

KINDS = {
    "item":       {"label": "物品",   "title": "搜物品", "json": "item_id.json",    "id": "ItemId",
                   "img_dir": "items",       "img_tpl": "Item_{id}.png"},
    "npc":        {"label": "生物",   "title": "搜生物", "json": "npc_id.json",     "id": "NpcId",
                   "img_dir": "npcs",        "img_tpl": "NPC_{id}.png"},
    "projectile": {"label": "弹幕",   "title": "搜弹幕", "json": "project_id.json", "id": "ProjId",
                   "img_dir": "projectiles", "img_tpl": "Projectile_{id}.png"},
    "buff":       {"label": "增益",   "title": "搜增益", "json": "buff_id.json",    "id": "BuffId",
                   "img_dir": "buffs",       "img_tpl": "Buff_{id}.png"},
    "prefix":     {"label": "修饰语", "title": "搜修饰", "json": "prefix_id.json",  "id": "PrefixId",
                   "img_dir": None,          "img_tpl": None},
}

# 指令 / 中文别名 -> kind
COMMANDS = {
    "si": "item", "搜物品": "item",
    "sn": "npc", "搜生物": "npc",
    "sp": "projectile", "搜弹幕": "projectile",
    "sb": "buff", "搜增益": "buff",
    "sx": "prefix", "搜修饰": "prefix", "搜修饰语": "prefix",
}

_DEFAULT_DIR = "C:/bot/assets/lexicon"
_CANDIDATES = [
    os.environ.get("LEXICON_DIR") or "",
    _DEFAULT_DIR,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "lexicon"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "lexicon"),
]


def _find_base() -> str:
    for d in _CANDIDATES:
        if d and os.path.isdir(os.path.join(d, "terraria_data")):
            return os.path.abspath(d)
    return _DEFAULT_DIR


def money_text(mv) -> str:
    """MonetaryValue -> 中文货币串（铂/金/银/铜），全为 0 时返回空串"""
    if not isinstance(mv, dict):
        return ""
    parts = []
    for key, unit in (("Platinum", "铂"), ("Gold", "金"), ("Silver", "银"), ("Copper", "铜")):
        try:
            v = int(mv.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            parts.append(f"{v}{unit}")
    return "".join(parts)


_LOCALE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z0-9_]+$")


def is_locale_key(name) -> bool:
    """名字是不是"本地化键"（如 BuffName.MinecartLegacyUnused、ItemName.XXX）。

    这些是游戏里没有中文名的未使用遗留条目，展示出来只会是一串英文键，
    检索时直接过滤掉（否则搜 buff/物品 会被这类噪声刷屏）。
    """
    return bool(_LOCALE_KEY_RE.match(str(name or "").strip()))


def _aliases(item) -> list:
    alias = item.get("Alias") or []
    if isinstance(alias, str):
        alias = [alias]
    return [str(a) for a in alias if str(a).strip()]


class Lexicon:
    """图鉴数据与检索。dataset 懒加载并缓存（JSON 只在首次用时读）。"""

    def __init__(self, base_dir: str = None):
        self.base = base_dir or _find_base()
        self._data = {}
        self._wiki = None
        self._wiki_mtime = 0.0
        self._lock = threading.Lock()

    def available(self) -> bool:
        return os.path.isdir(os.path.join(self.base, "terraria_data"))

    def dataset(self, kind: str) -> list:
        meta = KINDS.get(kind)
        if not meta:
            return []
        with self._lock:
            if kind in self._data:
                return self._data[kind]
            rows = []
            path = os.path.join(self.base, "terraria_data", meta["json"])
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    rows = list(json.load(f) or [])
            except (OSError, ValueError) as e:
                print(f"[lexicon] 读取 {path} 失败: {e}")
                rows = []
            # 过滤本地化键条目（未使用遗留项）与无名字条目
            rows = [r for r in rows if isinstance(r, dict) and (r.get("Name") or "").strip()
                    and not is_locale_key(r.get("Name"))]
            if kind == "npc":
                # 与 CaiBotLite 一致：把自定义 NPC（npcx_id.json）并入生物库
                try:
                    with open(os.path.join(self.base, "terraria_data", "npcx_id.json"),
                              encoding="utf-8", errors="ignore") as f2:
                        rows += list(json.load(f2) or [])
                except (OSError, ValueError):
                    pass
            self._data[kind] = rows
            return rows

    @staticmethod
    def _names(item) -> list:
        name = str(item.get("Name") or "")
        return [n for n in ([name] + _aliases(item)) if n]

    def search(self, kind: str, query: str, limit: int = 12):
        """返回 (matches, total)：matches 按匹配度降序并截断到 limit"""
        meta = KINDS.get(kind)
        q = str(query or "").strip()
        if not meta or not q:
            return [], 0
        rows = self.dataset(kind)
        id_field = meta["id"]
        # ① ID 精确命中
        try:
            qid = int(q)
            for it in rows:
                if it.get(id_field) == qid:
                    return [it], 1
        except (TypeError, ValueError):
            pass
        ql = q.lower()
        scored = []
        for it in rows:
            best = 0
            for nm in self._names(it):
                nl = nm.lower()
                if nm == q or nl == ql:
                    best = 1000
                    break
                if nl.startswith(ql):
                    best = max(best, 900)
                elif ql in nl:
                    best = max(best, 800)
                else:
                    r = difflib.SequenceMatcher(None, ql, nl).ratio() * 100.0
                    if r >= 85:
                        best = max(best, r)
            if best > 0:
                scored.append((best, it))
        scored.sort(key=lambda x: -x[0])
        return [it for _s, it in scored[:max(1, int(limit))]], len(scored)

    def image_path(self, kind: str, item) -> str:
        meta = KINDS.get(kind) or {}
        if not meta.get("img_dir"):
            return ""
        try:
            iid = int(item.get(meta["id"]))
        except (TypeError, ValueError):
            return ""
        p = os.path.join(self.base, "images", meta["img_dir"], meta["img_tpl"].format(id=iid))
        return p if os.path.isfile(p) else ""

    def attributes(self, kind: str, item) -> list:
        """属性行 [(标签, 值)]，口径对齐 CaiBotLite（暴击率用正确语义）"""
        out = []
        if kind == "item":
            if item.get("MaxStack") is not None:
                out.append(("最大堆叠", str(item.get("MaxStack"))))
            if item.get("WeaponType"):
                out.append(("武器类型", str(item.get("WeaponType"))))
            if item.get("Damage") not in (None, -1):
                out.append(("伤害", str(item.get("Damage"))))
            if item.get("Crit"):
                out.append(("暴击率", f"{item.get('Crit')}%"))
            if item.get("Shoot"):
                sn = item.get("ShootName") or ""
                out.append(("发射", f"{sn} ({item.get('Shoot')})" if sn else str(item.get("Shoot"))))
            if item.get("Mana"):
                out.append(("消耗魔力", f"{item.get('Mana')} 点"))
            for key, label in (("Pick", "镐力"), ("Axe", "斧力"), ("Hammer", "锤力")):
                if item.get(key):
                    out.append((label, f"+{item.get(key)}%"))
            if item.get("HealLife"):
                out.append(("恢复生命", f"{item.get('HealLife')} 点"))
            if item.get("HealMana"):
                out.append(("恢复魔力", f"{item.get('HealMana')} 点"))
            if item.get("BuffType"):
                bn = item.get("BuffName") or ""
                out.append(("增益", f"{bn} ({item.get('BuffType')})" if bn else str(item.get("BuffType"))))
            if item.get("CreateTile") not in (None, -1):
                out.append(("物块 ID", str(item.get("CreateTile"))))
            if item.get("CreateWall") not in (None, -1):
                out.append(("墙 ID", str(item.get("CreateWall"))))
            out.append(("价值", money_text(item.get("MonetaryValue")) or "无价"))
        elif kind == "npc":
            if item.get("LifeMax") is not None:
                out.append(("生命值", str(item.get("LifeMax"))))
            if item.get("Damage") not in (None, -1):
                out.append(("伤害", str(item.get("Damage"))))
            out.append(("价值", money_text(item.get("MonetaryValue")) or "无价"))
        elif kind == "projectile":
            if item.get("AiStyle") is not None:
                out.append(("AI 类型", str(item.get("AiStyle"))))
            out.append(("阵营", "友方" if item.get("Friendly") else "敌方"))
        al = _aliases(item)
        if al:
            out.append(("别名", "、".join(al)))
        return out

    def _wiki_map(self) -> dict:
        """本地资料里没有描述时用的补充说明（由 scripts/fetch_wiki_descriptions.py 抓取）"""
        path = os.path.join(self.base, "terraria_data", "wiki_desc.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return self._wiki or {}
        with self._lock:
            # 按 mtime 热加载：重新抓取/上传 wiki_desc.json 后无需重启机器人；
            # 空结果不缓存（文件可能在机器人启动后才上传）
            if self._wiki and mtime == self._wiki_mtime:
                return self._wiki
            data = {}
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    data = d
            except (OSError, ValueError):
                data = {}
            self._wiki = data
            self._wiki_mtime = mtime
            return data

    def description_with_source(self, item) -> tuple:
        """返回 (说明文本, 来源)：data=本地资料 / wiki=补充抓取 / ""=都没有"""
        d = str(item.get("Description") or "").strip()
        if d:
            return d, "data"
        nm = str(item.get("Name") or "").strip()
        if nm:
            w = self._wiki_map().get(nm)
            if w:
                return str(w), "wiki"
        return "", ""

    def description(self, item) -> str:
        return self.description_with_source(item)[0]

    def summary(self, kind: str, item) -> str:
        """列表卡每行的一行摘要"""
        if kind == "item":
            bits = []
            if item.get("Damage") not in (None, -1):
                bits.append(f"伤害 {item.get('Damage')}")
            if item.get("HealLife"):
                bits.append(f"回血 {item.get('HealLife')}")
            if item.get("MaxStack") is not None:
                bits.append(f"堆叠 {item.get('MaxStack')}")
            if item.get("WeaponType"):
                bits.append(str(item.get("WeaponType")))
            return " · ".join(bits)
        if kind == "npc":
            bits = []
            if item.get("Damage") not in (None, -1):
                bits.append(f"伤害 {item.get('Damage')}")
            if item.get("LifeMax") is not None:
                bits.append(f"生命 {item.get('LifeMax')}")
            return " · ".join(bits)
        if kind == "projectile":
            return f"AI {item.get('AiStyle')} · {'友方' if item.get('Friendly') else '敌方'}"
        if kind == "buff":
            return str(item.get("Description") or "").replace("\n", " ")[:40]
        al = _aliases(item)
        return ("别名：" + "、".join(al)) if al else ""
