# -*- coding: utf-8 -*-
r"""图鉴素材部署：把 CaiBotLite 的图鉴数据与图标打包上传到 C:/bot/assets/lexicon/

来源（scripts/deploy_config.json 的 lexicon_source，留空则用工作区内的参考仓库）：
  reference-repos\CaiBotLite\assets
    terraria_data\*.json                  → C:/bot/assets/lexicon/terraria_data/
    images\{items,npcs,projectiles,buffs}  → C:/bot/assets/lexicon/images/<同名>/

约 8400 个小文件 / ~14MB：逐个 SFTP 上传会有数千次往返，所以打成 zip 上传后在服务器解压
（优先 Windows 自带 tar，失败回退 Expand-Archive）。

只增不删：不清理远端已有素材。
"""

import base64
import os
import sys
import zipfile

import paramiko

from deploy_config import load_deploy_config

CFG = load_deploy_config()
_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = (CFG.get("lexicon_source") or "").strip() or os.path.join(
    os.path.dirname(os.path.dirname(_HERE)), "reference-repos", "CaiBotLite", "assets")
ZIP_LOCAL = os.path.join(_HERE, "_lexicon_assets.zip")
REMOTE_ZIP = "C:/bot/assets/lexicon_assets.zip"
REMOTE_DIR = "C:/bot/assets/lexicon"
IMG_SUBS = ("items", "npcs", "projectiles", "buffs")
IMG_EXT = (".png", ".webp", ".jpg", ".jpeg")


WIKI_LOCAL = os.path.join(os.path.dirname(_HERE), "bot", "assets", "lexicon_wiki.json")


def build_zip():
    data_dir = os.path.join(SOURCE, "terraria_data")
    img_root = os.path.join(SOURCE, "images")
    n = 0
    with zipfile.ZipFile(ZIP_LOCAL, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        if os.path.isdir(data_dir):
            for name in sorted(os.listdir(data_dir)):
                if name.lower().endswith(".json"):
                    z.write(os.path.join(data_dir, name), "terraria_data/" + name)
                    n += 1
        # 本地抓好的 Wiki 补充说明（scripts/fetch_wiki_descriptions.py 产出）也一起带上
        if os.path.isfile(WIKI_LOCAL):
            z.write(WIKI_LOCAL, "terraria_data/wiki_desc.json")
            n += 1
        for sub in IMG_SUBS:
            d = os.path.join(img_root, sub)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.lower().endswith(IMG_EXT):
                    z.write(os.path.join(d, name), "images/%s/%s" % (sub, name))
                    n += 1
    return n, os.path.getsize(ZIP_LOCAL)


def main() -> int:
    if not os.path.isdir(SOURCE):
        print("找不到素材目录:", SOURCE)
        return 1
    n, size = build_zip()
    print("打包完成: %d 个文件 / %.1f MB" % (n, size / 1048576))

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(CFG["host"], port=CFG["port"], username=CFG["user"],
                key_filename=CFG["key"], timeout=25)
    try:
        sftp = cli.open_sftp()
        sftp.put(ZIP_LOCAL, REMOTE_ZIP)
        sftp.close()
        print("[ok] zip 已上传，服务器解压中…")

        body = (
            '$ErrorActionPreference = "Continue"; '
            "New-Item -ItemType Directory -Force -Path '%s' | Out-Null; "
            "tar -xf '%s' -C '%s' 2>$null; "
            "if (-not (Test-Path '%s/terraria_data/item_id.json')) { "
            "  Expand-Archive -Path '%s' -DestinationPath '%s' -Force }; "
            "'data=' + (Get-ChildItem '%s/terraria_data' -Filter *.json).Count + "
            "' items=' + (Get-ChildItem '%s/images/items' -Filter *.png).Count + "
            "' npcs=' + (Get-ChildItem '%s/images/npcs' -Filter *.png).Count + "
            "' proj=' + (Get-ChildItem '%s/images/projectiles' -Filter *.png).Count + "
            "' buffs=' + (Get-ChildItem '%s/images/buffs' -Filter *.png).Count"
        ) % (REMOTE_DIR, REMOTE_ZIP, REMOTE_DIR, REMOTE_DIR, REMOTE_ZIP, REMOTE_DIR,
             REMOTE_DIR, REMOTE_DIR, REMOTE_DIR, REMOTE_DIR, REMOTE_DIR)
        enc = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
        _, out, err = cli.exec_command(
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
            "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + enc, timeout=900)
        print(out.read().decode("utf-8", "replace").strip())
        e = err.read().decode("utf-8", "replace").strip()
        if e:
            print("[stderr]", e[:300])
        cli.exec_command('cmd /c del "%s"' % REMOTE_ZIP, timeout=60)
    finally:
        cli.close()
    if os.path.exists(ZIP_LOCAL):
        os.remove(ZIP_LOCAL)
    print("图鉴素材部署完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
