# -*- coding: utf-8 -*-
"""世界种子清单（序号冻结，供 种子列表 / 种子提案 使用）

数据由 scripts/fetch_seed_list.py 从 terraria.wiki.gg 抓取后落到 assets/seeds.json：
  regular: 11 条常规（特殊）世界种子，序号 1..11
  secret:  37 条秘密世界种子，序号 12..48

**序号一旦冻结不可变**：投票提案用序号引用种子，改了序号会让历史提案指错。
Terraria 匹配种子时忽略大小写/空格/符号，所以 seed 直接用 wiki 上的写法即可。
"""

import json
import os
import threading

DEFAULT_PATH = "C:/bot/assets/seeds.json"
_CANDIDATES = [
    os.environ.get("SEEDS_FILE") or "",
    DEFAULT_PATH,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "seeds.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "seeds.json"),
]
SECRET_PER_PAGE = 20        # 秘密种子每页条数（常规只有 11 条，独占第一页）


def _find_path() -> str:
    for p in _CANDIDATES:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return DEFAULT_PATH


class Seeds:
    def __init__(self, path: str = None):
        self.path = path or _find_path()
        self._lock = threading.Lock()
        self._data = None
        self._mtime = 0.0

    def _load(self) -> dict:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return self._data or {"regular": [], "secret": []}
        with self._lock:
            if self._data is not None and mtime == self._mtime:
                return self._data
            try:
                with open(self.path, encoding="utf-8", errors="ignore") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                d = {}
            if not isinstance(d, dict):
                d = {}
            d.setdefault("regular", [])
            d.setdefault("secret", [])
            self._data = d
            self._mtime = mtime
            return d

    def available(self) -> bool:
        d = self._load()
        return bool(d.get("regular") or d.get("secret"))

    def all(self) -> list:
        d = self._load()
        out = []
        for it in d.get("regular") or []:
            out.append(dict(it, category="常规"))
        for it in d.get("secret") or []:
            out.append(dict(it, category="秘密"))
        return out

    def by_no(self, no):
        try:
            no = int(no)
        except (TypeError, ValueError):
            return None
        for it in self.all():
            if int(it.get("no") or -1) == no:
                return it
        return None

    def resolve(self, nos):
        """把序号列表解析成条目：返回 (entries, err)。自动去重、校验范围"""
        entries, seen = [], set()
        for n in nos:
            n = int(n)
            if n in seen:
                continue
            seen.add(n)
            it = self.by_no(n)
            if it is None:
                return [], f"序号 {n} 不存在（合法范围 1–{len(self.all())}）"
            entries.append(it)
        if not entries:
            return [], "请至少给一个种子序号"
        return entries, None

    @staticmethod
    def label(entries) -> str:
        """组合的展示名：`醉酒世界 + 蜜蜂世界`"""
        return " + ".join(str(e.get("name") or e.get("seed") or "?") for e in entries)

    @staticmethod
    def seed_values(entries) -> list:
        """喂给插件/世界生成的种子字符串列表"""
        vals = []
        for e in entries:
            v = str(e.get("seed") or e.get("name") or "").strip()
            if v:
                vals.append(v)
        return vals

    def pages(self) -> list:
        """分页：第 1 页 = 常规（11 条），之后每页 SECRET_PER_PAGE 条秘密种子"""
        d = self._load()
        reg = [dict(it, category="常规") for it in (d.get("regular") or [])]
        sec = [dict(it, category="秘密") for it in (d.get("secret") or [])]
        out = []
        if reg:
            out.append(reg)
        for i in range(0, len(sec), SECRET_PER_PAGE):
            out.append(sec[i:i + SECRET_PER_PAGE])
        return out or [[]]
