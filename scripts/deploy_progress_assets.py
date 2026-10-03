# -*- coding: utf-8 -*-
r"""进度素材部署：打包 zip → 上传服务器 → 解压到 C:\bot\assets\progress

素材来源（本机，来自 CaiBotLite 仓库，GPL-3.0，不入库）：
  - boss/事件图标  <cai_assets_dir>/images/bosses/*.png        （28 张：18 boss + 入侵/事件）
  - 世界图标       <cai_assets_dir>/images/world_icon/*.png    （33 张：Icon*.png）
  - 锁定图标       <cai_assets_dir>/images/items/Item_5328.png → 存为 lock.png

以上目录在 deploy_config.json 中配置（不入库），见 deploy_config.example.json。

服务器目标目录结构（与 bot\progress_render.py 默认路径一致）：
  C:\bot\assets\progress\
    bosses\        （BOSSES_DIR，名字必须与 progress_render.BOSSES/EVENTS 的 key 一致）
    world_icon\    （WORLD_ICON_DIR）
    lock.png       （LOCK_ICON）
"""

import base64
import os
import zipfile

import paramiko

from deploy_config import load_deploy_config

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = load_deploy_config()

CAI_ASSETS = CFG["cai_assets_dir"]
ZIP_PATH = os.path.join(BASE, "_progress_assets.zip")
REMOTE_ZIP = "C:/bot/_progress_assets.zip"
REMOTE_DIR = "C:/bot/assets/progress"


def build_zip() -> tuple:
    """打包素材 zip，返回 (bosses, world_icons) 数量"""
    bosses_dir = os.path.join(CAI_ASSETS, "images", "bosses")
    icons_dir = os.path.join(CAI_ASSETS, "images", "world_icon")
    lock_src = os.path.join(CAI_ASSETS, "images", "items", "Item_5328.png")
    bosses = icons = 0
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for fn in os.listdir(bosses_dir):
            if fn.endswith(".png"):
                z.write(os.path.join(bosses_dir, fn), "bosses/" + fn)
                bosses += 1
        for fn in os.listdir(icons_dir):
            if fn.endswith(".png"):
                z.write(os.path.join(icons_dir, fn), "world_icon/" + fn)
                icons += 1
        z.write(lock_src, "lock.png")
    size_kb = os.path.getsize(ZIP_PATH) / 1024
    print(f"[打包] bosses={bosses} world_icon={icons} lock=1，zip {size_kb:.0f}KB")
    return bosses, icons


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
Write-Host ('bosses=' + (Get-ChildItem '{REMOTE_DIR}/bosses/*.png').Count)
Write-Host ('world_icon=' + (Get-ChildItem '{REMOTE_DIR}/world_icon/*.png').Count)
Get-Item '{REMOTE_DIR}/lock.png' | Select-Object Name, Length | Format-Table -AutoSize
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