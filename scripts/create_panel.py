# -*- coding: utf-8 -*-
"""创建 QQ 机器人集团令面板（/v2/panels），作用于 config.yaml 中已配置的监控群（specific）。一次性脚本。

凭证与群列表均从 ../bot/config.yaml 读取，避免把 AppSecret 写进代码。
"""
import json
import os
import urllib.request

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "..", "bot", "config.yaml")
with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
    CONFIG = yaml.safe_load(fp)

APPID = str(CONFIG["appid"])
SECRET = CONFIG["secret"]

# 目标群（config.yaml 现有监控群）
GROUPS = [g["group_openid"] for g in CONFIG.get("groups", []) if g.get("group_openid")]


def post_json(url, body, headers=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8")
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get_access_token():
    """botpy 同款：POST https://bots.qq.com/app/getAppAccessToken (Body {appId, clientSecret})"""
    url = "https://bots.qq.com/app/getAppAccessToken"
    code, raw = post_json(url, {"appId": APPID, "clientSecret": SECRET})
    if code == 200:
        return json.loads(raw)["access_token"]
    raise RuntimeError(f"获取 token 失败: {code} {raw}")


PANEL_ITEMS = [
    {"type": "command", "name": "在线", "desc": "查看各服务器在线玩家与推图进度"},
    {"type": "command", "name": "服务器列表", "desc": "查看已接入服务器与在线状态"},
    {"type": "command", "name": "群信息", "desc": "群ID、联合群与白名单统计"},
    {"type": "command", "name": "绑定邮箱", "desc": "获取白名单绑定验证码"},
    {"type": "command", "name": "帮助", "desc": "查看机器人功能引导"},
]

body = {
    "scope": "group",
    "target_type": "specific",
    "group_openids": GROUPS,
    "panel": {
        "items": PANEL_ITEMS,
        "remark": "ZSE联合体 服务器管理指令面板（创建于 2026-10-01）",
    },
}

token = get_access_token()
print("token ok:", token[:12] + "...")
url = "https://api.sgroup.qq.com/v2/panels"
code, raw = post_json(url, body, headers={"Authorization": f"QQBot {token}"})
print("HTTP", code)
print(raw)
if code == 200:
    print("panel_id:", json.loads(raw).get("panel_id"))