# -*- coding: utf-8 -*-
"""部署后自检（每次部署完跑一次）：
  1) 本地与远端关键文件哈希一致
  2) main.py 里 `from X import ...` 的每个模块，服务器上都必须存在（防"漏传文件 → 机器人起不来"）
  3) 机器人进程存活
  4) 机器人日志里没有 Traceback
用法：py scripts/verify_deploy.py
"""

import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paramiko
from deploy_config import load_deploy_config

BOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
REMOTE = "C:/bot"
CORE = ["main.py", "economy_store.py", "whitelist_users.py", "econ_render.py", "bind_rules.py",
        "permissions.py", "whitelist_mail.py", "vote_store.py", "seeds.py", "zse_server.py", "help_content.py"]


def main():
    cfg = load_deploy_config()
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(cfg["host"], port=cfg["port"], username=cfg["user"], key_filename=cfg["key"], timeout=25)
    sftp = cli.open_sftp()
    bad = []
    # ① 核心文件哈希
    for f in CORE:
        lp = os.path.join(BOT_DIR, f)
        if not os.path.exists(lp):
            print("  ⚠️ 本地缺少 %s（跳过）" % f)
            continue
        local = hashlib.sha256(open(lp, "rb").read()).hexdigest()
        try:
            remote = hashlib.sha256(sftp.open(REMOTE + "/" + f).read()).hexdigest()
        except IOError:
            bad.append("%s 远端不存在" % f)
            continue
        if local != remote:
            bad.append("%s 哈希不一致" % f)
    # ② main.py 导入的本地模块是否都在服务器上
    src = open(os.path.join(BOT_DIR, "main.py"), encoding="utf-8").read()
    mods = set()
    for m in re.finditer(r"(?m)^from ([a-zA-Z_][\w]*)(?: import|\.)", src):
        mods.add(m.group(1))
    local_files = {x[:-3] for x in os.listdir(BOT_DIR) if x.endswith(".py")}
    for mod in sorted(mods & local_files):
        try:
            sftp.stat(REMOTE + "/" + mod + ".py")
        except IOError:
            bad.append("远端缺少模块 %s.py（main.py 有导入它）" % mod)
    # ③ 进程
    _, o, _e = cli.exec_command('tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH', timeout=60)
    procs = o.read().decode("utf-8", "replace")
    nproc = procs.count("python.exe")
    if nproc == 0:
        bad.append("机器人进程不存在")
    # ④ Traceback
    log = sftp.open(REMOTE + "/server_bot_err.log").read().decode("utf-8", "replace")
    tb = log.count("Traceback")
    if tb:
        bad.append("日志里有 %d 处 Traceback" % tb)
    sftp.close(); cli.close()
    print("  进程数: %d | Traceback: %d | 检查文件: %d" % (nproc, tb, len(CORE)))
    if bad:
        print("❌ 自检未通过:")
        for b in bad:
            print("   - " + b)
        return 1
    print("✅ 部署自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
