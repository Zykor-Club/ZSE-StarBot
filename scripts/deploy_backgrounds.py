# -*- coding: utf-8 -*-
r"""背景图部署：新横图补充到「查背包背景池」，竖图放到「排行榜专属目录」

来源（scripts/deploy_config.json 的 bg_new_dir）：
  <bg_new_dir>\*          横图 → 本机 <bg_dir>\bgnew_*.jpg  + 远端 C:/bot/assets/lookbag/backgrounds/
  <bg_new_dir>\竖屏\*     竖图 → 本机 <bg_dir>\rank\rank_*.jpg + 远端 C:/bot/assets/rank/backgrounds/

处理规则：长边 > MAX_SIDE 等比缩小，统一转 JPEG(q=90)。
  原图最大 7897×4375 / 30MB，直接当排行卡画布会让 PNG 到 10MB+、群里发送极慢。

只增不删：不清理远端已有背景；本机处理后的成品会留在 bg_dir（deploy_lookbag_assets 下次
打包时会自动把新增的 bgnew_*.jpg 一起带上，rank 子目录因无扩展名不会被误打包）。
"""

import base64
import os
import sys

import paramiko
from PIL import Image

from deploy_config import load_deploy_config

MAX_SIDE = 2048
QUALITY = 90
REMOTE_LOOKBAG_BG = "C:/bot/assets/lookbag/backgrounds"
REMOTE_RANK_BG = "C:/bot/assets/rank/backgrounds"
PORTRAIT_DIRNAME = "竖屏"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

CFG = load_deploy_config()
NEW_DIR = (CFG.get("bg_new_dir") or "").strip()
LOCAL_POOL = (CFG.get("bg_dir") or "").strip()


def convert(src: str, dst: str):
    """读取原图 → 长边压到 MAX_SIDE → 存 JPEG；返回 (原尺寸, 新尺寸)"""
    im = Image.open(src)
    orig = im.size
    im = im.convert("RGB")
    if max(im.size) > MAX_SIDE:
        r = MAX_SIDE / max(im.size)
        im = im.resize((max(1, round(im.width * r)), max(1, round(im.height * r))),
                       Image.LANCZOS)
    im.save(dst, "JPEG", quality=QUALITY, optimize=True)
    return orig, im.size


def collect(src_dir: str, dst_dir: str, prefix: str):
    out = []
    if not os.path.isdir(src_dir):
        print(f"[跳过] 目录不存在：{src_dir}")
        return out
    os.makedirs(dst_dir, exist_ok=True)
    for fn in sorted(os.listdir(src_dir)):
        if not fn.lower().endswith(IMG_EXT):
            continue
        src = os.path.join(src_dir, fn)
        name = f"{prefix}{os.path.splitext(fn)[0]}.jpg"
        dst = os.path.join(dst_dir, name)
        try:
            orig, now = convert(src, dst)
        except Exception as e:
            print(f"[失败] {fn}: {e}")
            continue
        out.append((dst, name, orig, now, os.path.getsize(dst)))
    return out


def ps_run(cli, body: str, timeout: int = 120) -> str:
    enc = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
    _, stdout, _ = cli.exec_command(
        "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
        "-NoProfile -EncodedCommand " + enc, timeout=timeout)
    return stdout.read().decode("utf-8", "replace")


def main() -> int:
    if not NEW_DIR or not os.path.isdir(NEW_DIR):
        print("请先在 scripts/deploy_config.json 配置 bg_new_dir（新背景图目录）")
        return 1
    if not LOCAL_POOL or not os.path.isdir(LOCAL_POOL):
        print("请先在 scripts/deploy_config.json 配置 bg_dir（本机原背景图目录）")
        return 1

    landscape = collect(NEW_DIR, LOCAL_POOL, "bgnew_")
    portrait = collect(os.path.join(NEW_DIR, PORTRAIT_DIRNAME),
                       os.path.join(LOCAL_POOL, "rank"), "rank_")

    def show(title, rows):
        print(f"\n{title}（{len(rows)} 张）")
        for _dst, name, orig, now, size in rows:
            print(f"  {name}  {orig[0]}x{orig[1]} → {now[0]}x{now[1]}  {size // 1024}KB")

    show("横图 → 补充到查背包背景池", landscape)
    show("竖图 → 排行榜专用", portrait)
    if not landscape and not portrait:
        print("没有可处理的图片")
        return 0

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(CFG["host"], port=CFG["port"], username=CFG["user"],
                key_filename=CFG["key"], timeout=20)
    ps_run(cli, f"New-Item -ItemType Directory -Force -Path '{REMOTE_LOOKBAG_BG}' | Out-Null; "
                f"New-Item -ItemType Directory -Force -Path '{REMOTE_RANK_BG}' | Out-Null; 'ok'")
    sftp = cli.open_sftp()
    for dst, name, *_ in landscape:
        sftp.put(dst, REMOTE_LOOKBAG_BG + "/" + name)
        print("[上传] 背景池", name)
    for dst, name, *_ in portrait:
        sftp.put(dst, REMOTE_RANK_BG + "/" + name)
        print("[上传] 排行榜", name)
    sftp.close()
    print("\n[服务器] 目录回显：")
    print(ps_run(cli,
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 f"'--- lookbag/backgrounds ---'; "
                 f"(Get-ChildItem '{REMOTE_LOOKBAG_BG}' -File).Count; "
                 f"'--- rank/backgrounds ---'; "
                 f"Get-ChildItem '{REMOTE_RANK_BG}' -File | ForEach-Object {{ $_.Name }}"))
    cli.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
