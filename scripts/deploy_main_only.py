# -*- coding: utf-8 -*-
"""仅重传机器人代码并重启 QQBot（插件 DLL 未变，无需重启 TShock）

SSH 连接信息读取自 deploy_config.json（不入库），见 deploy_config.example.json
"""
import base64
import os
import time

import paramiko

from deploy_config import load_deploy_config

BASE = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.join(BASE, "..", "bot")  # 机器人代码目录（对应服务器 C:\bot）
CFG = load_deploy_config()

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(CFG["host"], port=CFG["port"], username=CFG["user"],
            key_filename=CFG["key"], timeout=20)

sftp = cli.open_sftp()
for f in ["main.py", "permissions.py", "whitelist_mail.py", "zse_server.py", "groups_registry.py",
          "config.yaml", "lookbag_render.py", "upload_media.py", "vote_store.py", "server_status_store.py", "vote_render.py",
          "progress_render.py", "progress_notify_store.py", "progress_unlock_store.py",
          "github_monitor.py", "card_render.py", "help_content.py", "rank_render.py",
          "lexicon.py", "lexicon_render.py", "seeds.py", "seed_render.py", "economy_store.py", "whitelist_users.py", "econ_render.py"]:
    sftp.put(os.path.join(BOT_DIR, f), "C:/bot/" + f)
    print("上传 OK:", f)
sftp.close()


def run(script_cmd, secs=60):
    enc = base64.b64encode(script_cmd.encode("utf-16-le")).decode("ascii")
    cmd = ("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
           "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + enc)
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=secs)
    return stdout.read().decode("utf-8", "replace")


print(run(r"powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\bot\restart_bot.ps1", 60))
time.sleep(15)
print(run(r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId,CreationDate | Format-Table -AutoSize
$log = Get-ChildItem 'C:\bot\server_bot_err.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($log) { Get-Content $log.FullName -Encoding UTF8 -Tail 12 }
""", 40))
cli.close()
print("机器人已重启")