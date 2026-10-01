# -*- coding: utf-8 -*-
"""临时脚本：paramiko 连接远程服务器执行命令（部署机器人用）

SSH 连接信息读取自同目录 deploy_config.json（不入库），见 deploy_config.example.json
"""
import os
import sys

import paramiko

from deploy_config import load_deploy_config

CFG = load_deploy_config()
HOST = CFG["host"]
PORT = CFG["port"]
USER = CFG["user"]
# 认证走本机私钥（见 deploy_config.json 的 key），不再保存任何明文密码
KEY = CFG["key"]


def run(commands: list, timeout: int = 60) -> None:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key = paramiko.RSAKey.from_private_key_file(KEY)
    cli.connect(HOST, PORT, USER, pkey=key, timeout=15)
    for cmd in commands:
        print(f"\n$ {cmd}")
        stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if out.strip():
            print(out)
        if err.strip():
            print("[stderr]", err)
    cli.close()


def upload(local: str, remote: str) -> None:
    """SFTP 上传文件到服务器"""
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key = paramiko.RSAKey.from_private_key_file(KEY)
    cli.connect(HOST, PORT, USER, pkey=key, timeout=15)
    sftp = cli.open_sftp()
    sftp.put(local, remote)
    size = os.path.getsize(local)
    print(f"[upload] {local} -> {remote} ({size} bytes)")
    sftp.close()
    cli.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    if mode == "test":
        run(["whoami", "net user administrator | findstr 上次设置密码", "ver"])
    elif mode == "upload":
        upload(sys.argv[2], sys.argv[3])
    elif mode == "exec":
        # 用法: py ssh_remote.py exec "命令1; 命令2" [timeout]
        cmd = sys.argv[2]
        run([f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{cmd}"'],
            timeout=int(sys.argv[3]) if len(sys.argv) > 3 else 120)
    elif mode == "probe":
        # 探查 Python 环境
        run([
            "where python",
            "python --version",
            "where py",
            "py --version",
            "where git",
        ])