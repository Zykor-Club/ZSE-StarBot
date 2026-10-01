# -*- coding: utf-8 -*-
r"""查背包素材部署：生成 item_names.json → 打包 zip → 上传服务器 → 解压到 C:\bot\assets\lookbag

素材来源（本机）：
  - 物品图标   <cai_assets_dir>/images/items/Item_*.png            （来自 CaiBotLite 仓库，GPL-3.0）
  - buff 图标  <cai_assets_dir>/images/buffs/Buff_*.png            （来自 CaiBotLite 仓库，GPL-3.0）
  - 背景图     <bg_dir>/ *.jpg（自行准备，随机选取用）
  - 物品名     <cai_assets_dir>/terraria_data/item_id.json → 裁剪为 {netId: 名称}（文字降级用）

以上目录在 deploy_config.json 中配置（不入库），见 deploy_config.example.json。
CaiBotLite 素材可从 https://github.com/UnrealMultiple/TShockPlugin 的 src/CaiBotLite/assets 获取。

服务器目标目录结构：
  C:\bot\assets\lookbag\
    items\        （6213 张）
    buffs\        （389 张）
    backgrounds\  （9 张）
    item_names.json
"""

import base64
import json
import os
import zipfile

import paramiko

from deploy_config import load_deploy_config

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = load_deploy_config()

CAI_ASSETS = CFG["cai_assets_dir"]
BG_DIR = CFG["bg_dir"]
ZIP_PATH = os.path.join(BASE, "_lookbag_assets.zip")
REMOTE_ZIP = "C:/bot/_lookbag_assets.zip"
REMOTE_DIR = "C:/bot/assets/lookbag"


def build_item_names() -> dict:
    """item_id.json（3.8MB）→ {netId: 名称}（跳过空名）"""
    src = os.path.join(CAI_ASSETS, "terraria_data", "item_id.json")
    with open(src, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    out = {}
    for it in data:
        iid, nm = it.get("ItemId"), (it.get("Name") or "").strip()
        if isinstance(iid, int) and nm:
            out[str(iid)] = nm
    return out


def build_zip() -> tuple:
    """打包素材 zip，返回 (items, buffs, backgrounds) 数量"""
    names = build_item_names()
    items = buffs = bgs = 0
    items_dir = os.path.join(CAI_ASSETS, "images", "items")
    buffs_dir = os.path.join(CAI_ASSETS, "images", "buffs")
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("item_names.json", json.dumps(names, ensure_ascii=False))
        for fn in os.listdir(items_dir):
            # Item_*.png（物品图标）+ 其他素材（Trash.png、Inventory_Back*.png 等分区底图）
            if fn.endswith(".png"):
                z.write(os.path.join(items_dir, fn), "items/" + fn)
                items += 1
        for fn in os.listdir(buffs_dir):
            if fn.startswith("Buff") and fn.endswith(".png"):
                z.write(os.path.join(buffs_dir, fn), "buffs/" + fn)
                buffs += 1
        bg_files = sorted(f for f in os.listdir(BG_DIR) if f.lower().endswith((".jpg", ".jpeg")))
        # 只收 jpg/jpeg（背景图格式），避免把截图等 PNG 误打包进去
        for fn in bg_files:
            z.write(os.path.join(BG_DIR, fn), "backgrounds/" + fn)
            bgs += 1
    print("[打包] 背景图清单：", bg_files)
    size_mb = os.path.getsize(ZIP_PATH) / 1024 / 1024
    print(f"[打包] items={items} buffs={buffs} backgrounds={bgs}，zip {size_mb:.1f}MB")
    print(f"[打包] item_names.json 共 {len(names)} 条")
    return items, buffs, bgs


def deploy():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(CFG["host"], port=CFG["port"], username=CFG["user"],
                key_filename=CFG["key"], timeout=20)
    sftp = cli.open_sftp()
    sftp.put(ZIP_PATH, REMOTE_ZIP)
    sftp.close()
    print(f"[上传] {REMOTE_ZIP} OK")

    ps = rf"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Expand-Archive -Path '{REMOTE_ZIP}' -DestinationPath '{REMOTE_DIR}' -Force
Remove-Item '{REMOTE_ZIP}' -Force
Write-Host ('items=' + (Get-ChildItem '{REMOTE_DIR}/items/*.png').Count)
Write-Host ('buffs=' + (Get-ChildItem '{REMOTE_DIR}/buffs/*.png').Count)
Write-Host ('backgrounds=' + (Get-ChildItem '{REMOTE_DIR}/backgrounds/*').Count)
Get-Item '{REMOTE_DIR}/item_names.json' | Select-Object Length, LastWriteTime | Format-Table -AutoSize
"""
    enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    stdin, stdout, stderr = cli.exec_command(
        "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
        "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + enc, timeout=300)
    print("[服务器] 解压结果：")
    print(stdout.read().decode("utf-8", "replace"))
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        print("[stderr]", err[:500])
    cli.close()


if __name__ == "__main__":
    build_zip()
    deploy()