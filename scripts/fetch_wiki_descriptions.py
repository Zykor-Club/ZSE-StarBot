# -*- coding: utf-8 -*-
r"""从中文 Terraria Wiki 抓取"补充说明"，补全本地资料里没有 Description 的条目。

背景：本地图鉴数据（CaiBotLite assets/terraria_data/*.json）里物品仅 45%、生物 81%、增益 88%
带描述，弹幕/修饰语完全没有。用法：
  1) 读本地 JSON，收集 Description 为空的条目名
  2) 调 MediaWiki API（action=query&prop=extracts&exintro&explaintext，一次 50 个标题）拿首段导语
  3) 结果写入 bot/assets/lexicon_wiki.json，并上传到 C:/bot/assets/lexicon/terraria_data/wiki_desc.json

缓存：scripts/_wiki_cache.json 记录已抓过的名字，重复运行只补新增（想全量重抓就删掉它）。
版权：文本来自 terraria.wiki.gg（CC BY-SA），卡片页脚会标注来源。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

import paramiko

from deploy_config import load_deploy_config

API = "https://terraria.wiki.gg/zh/api.php"
UA = "ZSEBot/1.0 (QQ group bot; contact: 1011819146@qq.com)"
BATCH = 20                 # 每次请求的标题数
# 注意：prop=extracts 有 exlimit 限制（匿名用户最多 20 条/请求）。
# 之前一次塞 50 个标题、不传 exlimit，导致大量页面"有页面但拿不到导语"（漏抓）。
# 现在固定 20 条 + exlimit=max，并用"空结果也会重试"的策略兜底。
MAX_LEN = 420              # 单条说明最大长度（卡片一屏能容纳）
SLEEP = 1.0                # 请求间隔（礼貌抓取）
_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = (load_deploy_config().get("lexicon_source") or "").strip() or os.path.join(
    os.path.dirname(os.path.dirname(_HERE)), "reference-repos", "CaiBotLite", "assets")
OUT_LOCAL = os.path.join(os.path.dirname(_HERE), "bot", "assets", "lexicon_wiki.json")
CACHE = os.path.join(_HERE, "_wiki_cache.json")
REMOTE = "C:/bot/assets/lexicon/terraria_data/wiki_desc.json"
FILES = ("item_id.json", "npc_id.json", "npcx_id.json", "buff_id.json",
         "project_id.json", "prefix_id.json")


def load_local_names():
    """收集所有缺 Description 的条目名（去重、保持稳定顺序）"""
    names = []
    seen = set()
    data_dir = os.path.join(SOURCE, "terraria_data")
    for fn in FILES:
        try:
            rows = json.load(open(os.path.join(data_dir, fn), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in rows:
            nm = (r.get("Name") or "").strip()
            if not nm or (r.get("Description") or "").strip():
                continue
            if nm in seen:
                continue
            seen.add(nm)
            names.append(nm)
    return names


def api_extracts(titles):
    q = {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
         "exlimit": "max", "redirects": 1, "format": "json", "titles": "|".join(titles)}
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(q), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))


def clean(text):
    t = " ".join((text or "").split())
    if len(t) > MAX_LEN:
        t = t[:MAX_LEN].rstrip() + "…"
    return t


def main() -> int:
    names = load_local_names()
    cache = {}
    if os.path.exists(CACHE):
        try:
            cache = json.load(open(CACHE, encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    # 空结果也重试：此前因 exlimit 限制漏抓的条目（cache 里存的是空串）需要重新抓一遍
    todo = [n for n in names if not cache.get(n)]
    print("缺描述条目 %d 个，其中已缓存 %d，本次需抓 %d" % (len(names), len(cache), len(todo)))
    ok = miss = err = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            data = api_extracts(chunk)
        except Exception as e:
            err += len(chunk)
            print("  批次失败 (%s)，跳过 %d 个" % (type(e).__name__, len(chunk)))
            time.sleep(3)
            continue
        pages = (data.get("query") or {}).get("pages") or {}
        got = {}
        for _pid, p in pages.items():
            title = p.get("title") or ""
            ex = clean(p.get("extract"))
            if ex and "missing" not in p:
                got[title] = ex
        # 标题可能被规范化或重定向（铜表 → 表）：按 API 返回的映射落到原名上
        alias = {}
        for r in (data.get("query") or {}).get("normalized") or []:
            alias[r.get("from")] = r.get("to")
        for r in (data.get("query") or {}).get("redirects") or []:
            alias[r.get("from")] = r.get("to")
        for nm in chunk:
            title = alias.get(nm, nm)
            ex = got.get(title) or got.get(alias.get(title, title)) or ""
            cache[nm] = ex
            if ex:
                ok += 1
            else:
                miss += 1
        # 每批落盘：被抓断也能续跑
        try:
            json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        except OSError:
            pass
        print("  进度 %d/%d  命中 %d 未命中 %d" % (min(i + BATCH, len(todo)), len(todo), ok, miss), flush=True)
        time.sleep(SLEEP)
    # 保存（只保留非空，空串视为没有）
    final = {k: v for k, v in cache.items() if v}
    os.makedirs(os.path.dirname(OUT_LOCAL), exist_ok=True)
    json.dump(final, open(OUT_LOCAL, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print("抓到说明 %d 条（失败 %d），写入 %s" % (len(final), err, OUT_LOCAL))

    CFG = load_deploy_config()
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(CFG["host"], port=CFG["port"], username=CFG["user"],
                key_filename=CFG["key"], timeout=25)
    try:
        sftp = cli.open_sftp()
        sftp.put(OUT_LOCAL, REMOTE)
        sftp.close()
        print("[ok] 已上传", REMOTE)
    finally:
        cli.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
