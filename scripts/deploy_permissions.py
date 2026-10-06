# -*- coding: utf-8 -*-
"""部署 bot 代码 + 插件 DLL，重启 TShock 与 QQBot

SSH 连接信息与插件输出目录读取自 deploy_config.json（不入库），见 deploy_config.example.json
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
files = [
    (os.path.join(BOT_DIR, "permissions.py"), "C:/bot/permissions.py"),
    (os.path.join(BOT_DIR, "whitelist_mail.py"), "C:/bot/whitelist_mail.py"),
    (os.path.join(BOT_DIR, "zse_server.py"), "C:/bot/zse_server.py"),
    (os.path.join(BOT_DIR, "main.py"), "C:/bot/main.py"),
    (os.path.join(BOT_DIR, "groups_registry.py"), "C:/bot/groups_registry.py"),
    (os.path.join(BOT_DIR, "config.yaml"), "C:/bot/config.yaml"),
    (os.path.join(BOT_DIR, "upload_media.py"), "C:/bot/upload_media.py"),
    (os.path.join(BOT_DIR, "vote_store.py"), "C:/bot/vote_store.py"),
    (os.path.join(BOT_DIR, "server_status_store.py"), "C:/bot/server_status_store.py"),
    (os.path.join(BOT_DIR, "vote_render.py"), "C:/bot/vote_render.py"),
    (os.path.join(BOT_DIR, "lookbag_render.py"), "C:/bot/lookbag_render.py"),
    (os.path.join(BOT_DIR, "progress_render.py"), "C:/bot/progress_render.py"),
    (os.path.join(BOT_DIR, "progress_notify_store.py"), "C:/bot/progress_notify_store.py"),
    (os.path.join(BOT_DIR, "progress_unlock_store.py"), "C:/bot/progress_unlock_store.py"),
    (os.path.join(BOT_DIR, "help_content.py"), "C:/bot/help_content.py"),
    (os.path.join(BOT_DIR, "rank_render.py"), "C:/bot/rank_render.py"),
    # 2026-10-05：补齐 github_monitor.py / card_render.py（AGENT.md §7.7 的清单缺口已修复），
    # 并新增图鉴模块
    (os.path.join(BOT_DIR, "lexicon.py"), "C:/bot/lexicon.py"),
    (os.path.join(BOT_DIR, "lexicon_render.py"), "C:/bot/lexicon_render.py"),
    (os.path.join(BOT_DIR, "seeds.py"), "C:/bot/seeds.py"),
    (os.path.join(BOT_DIR, "seed_render.py"), "C:/bot/seed_render.py"),
    (os.path.join(BOT_DIR, "economy_store.py"), "C:/bot/economy_store.py"),
    (os.path.join(BOT_DIR, "whitelist_users.py"), "C:/bot/whitelist_users.py"),
    (os.path.join(BOT_DIR, "econ_render.py"), "C:/bot/econ_render.py"),
    (os.path.join(BOT_DIR, "github_monitor.py"), "C:/bot/github_monitor.py"),
    (os.path.join(BOT_DIR, "card_render.py"), "C:/bot/card_render.py"),
]

# 插件 DLL（可选）：在 deploy_config.json 的 plugin_out_dir 中编译输出 starZSEbot.dll
plugin_dir = (CFG.get("plugin_out_dir") or "").strip()
plugin_dll = os.path.join(plugin_dir, "starZSEbot.dll") if plugin_dir else ""
if plugin_dll and os.path.exists(plugin_dll):
    files.append((plugin_dll, "C:/TShockServer/server/ServerPlugins/starZSEbot.dll"))
else:
    print("[提示] 未找到插件 DLL，跳过插件上传（仅部署机器人代码）：", plugin_dll or "plugin_out_dir 未配置")

for src, dst in files:
    sftp.put(src, dst)
    print("上传 OK:", dst.split("/")[-1])
sftp.close()


def run(script_cmd, secs=60):
    enc = base64.b64encode(script_cmd.encode("utf-16-le")).decode("ascii")
    cmd = ("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
           "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + enc)
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=secs)
    try:
        return stdout.read().decode("utf-8", "replace")
    except Exception:
        return "<timeout>"


# 重启 TShock（插件 DLL 需重启加载）
print(run(r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Get-Item 'C:\TShockServer\server\ServerPlugins\starZSEbot.dll' | Select-Object Name,Length,LastWriteTime | Format-Table -AutoSize
Get-Process 'TShock*' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine = 'cmd.exe /c "cd /d C:\TShockServer\server && TShock.Server.exe -lang 7 -config server.properties -port 7777"'
  CurrentDirectory = 'C:\TShockServer\server'
}
Write-Output ('启动 ReturnValue=' + $r.ReturnValue + ' PID=' + $r.ProcessId)
"""))

for i in range(15):
    time.sleep(4)
    if "UP" in run(r"if (Get-Process 'TShock.Server' -ErrorAction SilentlyContinue) { 'UP' } else { 'DOWN' }", 20):
        print(f"[{i}] TShock 已启动")
        break
    print(f"[{i}] DOWN")
time.sleep(10)
print(run(r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$log = Get-ChildItem 'C:\TShockServer\server\tshock\logs' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Output ('=== 最新日志: ' + $log.Name + ' ===')
Get-Content $log.FullName -Encoding UTF8 | Select-String -Pattern 'starZSEbot|Error|错误|Exception' | Select-Object -Last 15
""", 40))

# 重启机器人
print(run(r"""
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\bot\restart_bot.ps1
""", 60))
time.sleep(15)
print(run(r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Get-ChildItem 'C:\bot' -Filter '*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 2 Name,LastWriteTime | Format-Table -AutoSize
$log = Get-ChildItem 'C:\bot' -Filter '*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content $log.FullName -Encoding UTF8 -Tail 30
""", 40))
cli.close()
print("部署完成")