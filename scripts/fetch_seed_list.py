# -*- coding: utf-8 -*-
r"""抓取世界种子清单 → bot/assets/seeds.json（序号冻结）

来源：
  https://terraria.wiki.gg/zh/wiki/世界种子        → 常规（特殊）世界种子 + 位标志表里的可输入代码
  https://terraria.wiki.gg/zh/wiki/秘密世界种子    → 37 条秘密世界种子（模板 {{/row|name=|seed=}}）

序号规则（冻结，提案引用它）：1..N = 常规，N+1.. = 秘密。
产出 seeds.json：{"generated_at":..., "regular":[{no,name,seed,desc}...], "secret":[...]}

注意：Terraria 的种子匹配会忽略大小写/空格/符号，所以常规种子直接用 wiki 上的名字即可（drunk / Not the Bees …）。
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://terraria.wiki.gg/zh/api.php"
UA = {"User-Agent": "ZSEBot/1.0 (QQ group bot)"}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot", "assets", "seeds.json")


def page_html(title: str) -> str:
    q = {"action": "parse", "page": title, "prop": "text", "format": "json"}
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(q), headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.loads(r.read().decode("utf-8"))
    return ((d.get("parse") or {}).get("text") or {}).get("*") or ""


def _clean(cell: str) -> str:
    t = re.sub(r"<sup[^>]*>.*?</sup>", "", cell, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def tables(htm: str):
    out = []
    for tbl in re.findall(r"<table[^>]*>.*?</table>", htm, re.S):
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
            cells = [_clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            rows.append(cells)
        out.append(rows)
    return out


# 同一颗种子的多种写法（wiki 的表里既写 Drunk world 也写 Drunk）
_ALIAS = {"drunk": "drunkworld", "getfixedboi": "zenith", "dontdigup": "remix"}

# 中文显示名：zh wiki 的页面标题与游戏内种子名都是英文（中文只在说明里），
# 这里用中文玩家通用的译名做展示名；seed 字段仍是可直接输入的英文名。
_CN_NAME = {
    "drunkworld": "醉酒世界",
    "notthebees": "蜜蜂世界",
    "fortheworthy": "你够格吗",
    "celebrationmk10": "十周年世界",
    "theconstant": "饥荒世界",
    "remix": "颠倒世界",
    "notraps": "无陷阱世界",
    "zenith": "天顶世界",
    "skyblock": "空岛世界",
}


def _key(name: str) -> str:
    k = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return _ALIAS.get(k, k)


def parse_regular(htm: str):
    """常规特殊种子：位标志表给可输入名称，选项表给中文描述"""
    tbs = tables(htm)
    # 描述按"规范化名"建表：选项表里写 Drunk / Celebration Mk 10，位标志表里写 Drunk world / Celebration MK 10
    desc_by_name = {}
    for row in (tbs[0] if tbs else []):
        if len(row) >= 3 and row[1] in ("随机", "常规", "秘密种子"):
            continue
        if len(row) >= 3 and (row[2] or "").strip():
            desc_by_name[_key(row[1])] = row[2]
    # 旧名 → 新名（位标志表用的是旧名，选项表用新名：Don't dig up→Remix、get fixed boi→Zenith）
    old_names = {}
    for row in (tbs[-1] if tbs else []):
        if len(row) >= 2 and row[0].strip().isdigit():
            k = _key(row[1].strip())
            if k in ("remix", "zenith"):
                old_names.setdefault(k, []).append(row[1].strip())
    out, seen = [], set()
    for row in (tbs[0] if tbs else []):
        if len(row) < 3 or row[1] in ("随机", "常规", "秘密种子"):
            continue
        name = row[1].strip()
        key = _key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({
            "name": _CN_NAME.get(key, name),      # 展示名（中文）
            "seed": name,                          # 可输入的种子名（游戏/wiki 原名）
            "en": name,
            "alias": old_names.get(key, []),       # 同一颗种子的旧名（如 Don't dig up）
            "desc": desc_by_name.get(key, "") or row[2],
        })
    return out


def parse_secret(htm: str):
    """秘密种子：形如  中文名（iname） ｜ seed ｜ 描述"""
    out = []
    for row in (tables(htm)[0] if tables(htm) else []):
        if len(row) < 3:
            continue
        name_cell, seed_cell, desc = row[0], row[1], row[2]
        seed = seed_cell.strip()
        if not seed or seed.startswith("种子"):
            continue
        nm = re.sub(r"（[^）]*）\s*$", "", name_cell).strip()
        if not nm:
            continue
        out.append({"name": nm, "seed": seed.lower(), "desc": desc})
    return out


def main() -> int:
    print("抓取 世界种子 …")
    reg = parse_regular(page_html("世界种子"))
    print("  常规（特殊）种子:", len(reg))
    time.sleep(1)
    print("抓取 秘密世界种子 …")
    sec = parse_secret(page_html("秘密世界种子"))
    print("  秘密种子:", len(sec))
    no = 0
    for it in reg:
        no += 1
        it["no"] = no
    for it in sec:
        no += 1
        it["no"] = no
    data = {"generated_at": int(time.time()), "source": "terraria.wiki.gg (CC BY-SA)",
            "regular": reg, "secret": sec}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("已写入", OUT, "共", no, "条")
    # 上传到服务器（机器人读 C:/bot/assets/seeds.json）
    try:
        import paramiko
        from deploy_config import load_deploy_config
        CFG = load_deploy_config()
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(CFG["host"], port=CFG["port"], username=CFG["user"],
                    key_filename=CFG["key"], timeout=25)
        try:
            sftp = cli.open_sftp()
            sftp.put(OUT, "C:/bot/assets/seeds.json")
            sftp.close()
            print("[ok] 已上传 C:/bot/assets/seeds.json")
        finally:
            cli.close()
    except Exception as e:
        print("[warn] 上传失败（可稍后手动上传）:", type(e).__name__, e)
    for it in reg:
        print("  %2d  %-18s seed=%-20s %s" % (it["no"], it["name"][:18], it["seed"][:20], it["desc"][:40]))
    for it in sec[:5]:
        print("  %2d  %-14s seed=%-28s %s" % (it["no"], it["name"][:14], it["seed"][:28], it["desc"][:36]))
    print("  … 秘密共", len(sec), "条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
