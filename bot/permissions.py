# -*- coding: utf-8 -*-
"""
群权限系统（starZSEbot 机器人侧）

四身份：
  owner  高级管理员（可多名，互相添加；默认首任 = 把机器人拉进群的那个用户；拥有全部权限）
  master 服主（可添加服务器、删除自己添加的服务器、远程指令等）
  admin  管理员（可远程指令、开关"允许成员获取地图"等）
  member 普通群员（默认身份，基础功能可用）

命令权限矩阵见 MIN_ROLE，删除服务器的"归属"校验在调用处单独判断。
持久化文件：permissions.json（按群 openid 隔离），并附带简版审计日志。
"""

import json
import os
import tempfile
import time

_BASE = os.path.dirname(os.path.abspath(__file__))
PERMS_FILE = os.path.join(_BASE, "permissions.json")


def atomic_write_json(path: str, data: dict):
    """写临时文件后原子替换，避免进程崩溃导致 JSON 半写损坏"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# ───────────────────────── 身份常量 ─────────────────────────
OWNER = "owner"      # 高级管理员（可多名）
MASTER = "master"    # 服主
ADMIN = "admin"      # 管理员
MEMBER = "member"    # 普通群员

ROLE_LABEL = {
    OWNER: "高级管理员",
    MASTER: "服主",
    ADMIN: "管理员",
    MEMBER: "普通群员",
}

RANK = {OWNER: 4, MASTER: 3, ADMIN: 2, MEMBER: 1}


def role_label(role: str) -> str:
    return ROLE_LABEL.get(role, role or "未知")


# ───────────────────────── 命令权限矩阵（取所需最低身份） ─────────────────────────
PERM_MAP_FETCH = "map_fetch"      # 获取地图（member 也可用，但需群开关 allow_member_map）
PERM_MAP_TOGGLE = "map_toggle"    # 允许成员获取地图（开关）
PERM_ONLINE_SHOW = "online_show"  # 允许查看在线玩家（开关）
PERM_ADD_SERVER = "add_server"    # 添加服务器
PERM_DEL_SERVER = "del_server"    # 删除服务器
PERM_EXEC = "exec"                # 远程指令 / 远程执行
PERM_ROLE_MANAGE = "role_manage"  # 设置/取消身份（任一高级管理员可用）
PERM_BROADCAST = "broadcast"      # 公告广播（向联合区所有群发公告）
PERM_VOTE_MANAGE = "vote_manage"  # 种子投票 / 结束投票
PERM_VOTE_PUSH = "vote_push"      # 推送投票（向联合区所有群推卡）
PERM_RESET = "reset"              # 重置（导出存档 → 触发重置 → 推送存档）
PERM_PROGRESS_NOTIFY = "progress_notify"  # 进度提醒（设置/取消/列表）
PERM_SAY_ALL = "say_all"          # 全服喊话（向所有在线服务器广播）
PERM_STATUS_NOTIFY = "status_notify"  # 服务器上/下线通知（开关）
PERM_VOTE_PROPOSAL_DEL = "vote_proposal_del"  # 删除提案（管理员及以上）
PERM_BACKUP = "backup"  # 存档备份（管理员及以上）

MIN_ROLE = {
    PERM_MAP_FETCH: MEMBER,        # member 直接通过，但群未开放时在 check 内拦截
    PERM_MAP_TOGGLE: ADMIN,
    PERM_ONLINE_SHOW: ADMIN,
    PERM_STATUS_NOTIFY: ADMIN,
    PERM_VOTE_PROPOSAL_DEL: ADMIN,
    PERM_BACKUP: ADMIN,
    PERM_ADD_SERVER: MASTER,
    PERM_DEL_SERVER: MASTER,       # 归属（是否本人添加）在校验处单独判断
    PERM_EXEC: ADMIN,
    PERM_ROLE_MANAGE: OWNER,
    PERM_BROADCAST: MASTER,
    PERM_VOTE_MANAGE: MASTER,
    PERM_VOTE_PUSH: ADMIN,
    PERM_RESET: MASTER,
    PERM_PROGRESS_NOTIFY: ADMIN,
    PERM_SAY_ALL: ADMIN,
}

PERM_NEED_LABEL = {
    PERM_MAP_FETCH: "普通群员（需群开启「允许成员获取地图」）或管理员及以上",
    PERM_MAP_TOGGLE: "管理员及以上",
    PERM_ONLINE_SHOW: "管理员及以上",
    PERM_ADD_SERVER: "服主及以上",
    PERM_DEL_SERVER: "服主及以上（服主只能删除自己添加的服务器）",
    PERM_EXEC: "管理员及以上",
    PERM_ROLE_MANAGE: "高级管理员",
    PERM_BROADCAST: "服主及以上",
    PERM_VOTE_MANAGE: "服主及以上",
    PERM_VOTE_PUSH: "管理员及以上",
    PERM_RESET: "服主及以上",
    PERM_PROGRESS_NOTIFY: "管理员及以上",
    PERM_SAY_ALL: "管理员及以上",
}

# 身份昵称 -> 身份 key（设置身份命令用）
ROLE_ALIAS = {
    "owner": OWNER, "高级管理员": OWNER,
    "master": MASTER, "服主": MASTER,
    "admin": ADMIN, "管理员": ADMIN,
}


class PermissionManager:
    """按群（group_openid）维护四身份与群开关，数据持久化到 permissions.json。

    数据结构：
    {
      "<群openid>": {
        "owners": ["openid", ...],       # 高级管理员（可多名）
        "masters": ["openid", ...],
        "admins":  ["openid", ...],
        "config": {"allow_member_map": false, "show_online_players": true, "notify_server_status": true},  # 群开关 dict
        "created_at": 时间戳,
        "audit": [ {"ts": .., "who": "..", "what": ".."}, ... ]  # 最近 100 条
      }
    }
    """

    def __init__(self, path: str = PERMS_FILE):
        self.path = path
        self._data: dict = {}
        self._load()

    # ────────────── 持久化 ──────────────
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._data = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._data = {}
        # 旧格式迁移：owner 单值 -> owners 列表；顶层 map_allowed -> config dict
        for g in self._data.values():
            old = g.get("owner")
            if old and not g.get("owners"):
                g["owners"] = [old]
            g.pop("owner", None)
            cfg = g.get("config")
            if isinstance(cfg, dict):
                # 保留老字段兼容
                if "map_allowed" in g:
                    cfg.setdefault("allow_member_map", g.pop("map_allowed"))
            else:
                cfg = {}
                if "map_allowed" in g:
                    cfg["allow_member_map"] = g.pop("map_allowed")
                g["config"] = cfg

    def _save(self):
        try:
            atomic_write_json(self.path, self._data)
        except OSError as e:
            print(f"[permissions] 保存 permissions.json 失败: {e}")

    def group(self, gid: str) -> dict:
        """取（不存在则创建）某群的身份配置"""
        gid = gid or "nogroup"
        g = self._data.get(gid)
        if g is None:
            g = {"owners": [], "masters": [], "admins": [],
                 "config": {"allow_member_map": False, "show_online_players": True, "notify_server_status": True},
                 "created_at": int(time.time()), "audit": []}
            self._data[gid] = g
        return g

    # ────────────── 审计 ──────────────
    def _audit(self, gid: str, who: str, what: str):
        g = self.group(gid)
        g.setdefault("audit", [])
        g["audit"].append({"ts": int(time.time()), "who": who or "", "what": what})
        if len(g["audit"]) > 100:
            g["audit"] = g["audit"][-100:]

    def audit_log(self, gid: str) -> list:
        return list(self.group(gid).get("audit", []))

    # ────────────── 身份查询 ──────────────
    def owners_of(self, gid: str) -> list:
        return list(self._data.get(gid or "nogroup", {}).get("owners", []) or [])

    def is_owner(self, gid: str, openid: str) -> bool:
        return bool(openid) and openid in self.owners_of(gid)

    def role_of(self, gid: str, openid: str) -> str:
        """返回 openid 的身份：owner / master / admin / member"""
        if not openid:
            return MEMBER
        g = self._data.get(gid or "nogroup")
        if not g:
            return MEMBER
        if openid in (g.get("owners") or []):
            return OWNER
        if openid in (g.get("masters") or []):
            return MASTER
        if openid in (g.get("admins") or []):
            return ADMIN
        return MEMBER

    def rank_of(self, gid: str, openid: str) -> int:
        return RANK.get(self.role_of(gid, openid), RANK[MEMBER])

    def members_of_role(self, gid: str, role: str) -> list:
        g = self.group(gid)
        if role == OWNER:
            return self.owners_of(gid)
        return list(g.get("masters" if role == MASTER else "admins", []) or [])

    # ────────────── 权限判定 ──────────────
    def check(self, gid: str, openid: str, perm: str) -> bool:
        """是否允许 openid 执行 perm 命令。member 权限需群开关 map_allowed 开放。"""
        role = self.role_of(gid, openid)
        need = MIN_ROLE.get(perm, OWNER)
        if RANK.get(role, 1) < RANK.get(need, 4):
            return False
        # 普通群员获取地图受开关限制
        if perm == PERM_MAP_FETCH and role == MEMBER:
            return bool(self.map_allowed(gid))
        return True

    def can(self, gid: str, openid: str, perm: str) -> bool:
        return self.check(gid, openid, perm)

    # ────────────── 身份变更 ──────────────
    def set_first_owner(self, gid: str, openid: str, who: str = "", note: str = "设置高级管理员"):
        """追加一名高级管理员（机器人被拉入群 / config 种子 / 退群接管 / 无管理员追授）"""
        if not openid:
            return False
        g = self.group(gid)
        owners = g.setdefault("owners", [])
        if openid in owners:
            return True
        # 新 owner 不再占 master/admin 位
        g["masters"] = [x for x in g.get("masters", []) if x != openid]
        g["admins"] = [x for x in g.get("admins", []) if x != openid]
        owners.append(openid)
        self._audit(gid, who or openid, f"{note}: + {openid}")
        self._save()
        return True

    def add_owner(self, gid: str, openid: str, operator: str = "") -> tuple[bool, str]:
        """高级管理员互相添加新的高级管理员。返回 (ok, 提示)。"""
        if not openid:
            return False, "目标无效"
        g = self.group(gid)
        owners = g.setdefault("owners", [])
        if openid in owners:
            return False, "该用户已是高级管理员"
        g["masters"] = [x for x in g.get("masters", []) if x != openid]
        g["admins"] = [x for x in g.get("admins", []) if x != openid]
        owners.append(openid)
        self._audit(gid, operator, f"添加 {openid} 为高级管理员")
        self._save()
        return True, "已将用户设置为高级管理员"

    def remove_owner(self, gid: str, openid: str, operator: str = "") -> tuple[bool, str]:
        """取消某人为高级管理员（至少保留一名）。返回 (ok, 提示)。"""
        g = self.group(gid)
        owners = g.setdefault("owners", [])
        if openid not in owners:
            return False, "该用户不是高级管理员"
        if len(owners) <= 1:
            return False, "至少保留一名高级管理员，可先让其他高级管理员添加新成员后再取消"
        owners.remove(openid)
        self._audit(gid, operator, f"取消 {openid} 的高级管理员")
        self._save()
        return True, "已取消其高级管理员"

    def add_role(self, gid: str, openid: str, role: str, operator: str = "") -> tuple[bool, str]:
        """添加 master/admin 身份。返回 (ok, 提示)。"""
        if role not in (MASTER, ADMIN):
            return False, "未知身份，仅支持：高级管理员(owner)、服主(master)、管理员(admin)"
        g = self.group(gid)
        key = "masters" if role == MASTER else "admins"
        if openid in g.get("owners", []):
            return False, f"该用户已是{role_label(OWNER)}，无需再分配"
        other = "admins" if role == MASTER else "masters"
        if openid in g.get(key, []):
            return False, f"该用户已是{role_label(role)}"
        # 跨身份：先移除另一身份列表
        if openid in g.get(other, []):
            g[other].remove(openid)
        g.setdefault(key, []).append(openid)
        self._audit(gid, operator, f"设置 {openid} 为 {role}")
        self._save()
        return True, f"已将用户设置为{role_label(role)}"

    def remove_role(self, gid: str, openid: str, role: str = "", operator: str = "") -> tuple[bool, str]:
        """取消 master/admin 身份（role 留空表示取消全部非高级管理身份）"""
        if role and role not in (MASTER, ADMIN):
            return False, "未知身份，仅支持：服主(master)、管理员(admin)"
        g = self.group(gid)
        if openid in g.get("owners", []):
            return False, "高级管理员请使用取消其高级管理员身份的方式处理"
        changed = False
        roles = ((MASTER, "masters"), (ADMIN, "admins"))
        for rname, key in roles:
            if role and rname != role:
                continue
            if openid in g.get(key, []):
                g[key].remove(openid)
                changed = True
        if not changed:
            return False, ("该用户没有被设置此身份" if role else "该用户没有任何管理身份")
        self._audit(gid, operator, f"取消 {openid} 的{role_label(role) if role else '管理身份'}")
        self._save()
        return True, f"已取消其{role_label(role) if role else '管理身份'}"

    def remove_member(self, gid: str, openid: str):
        """用户退群：从全部身份列表中移除（服务端角色由 main.py 处理接管/冻结）"""
        if not openid:
            return
        g = self._data.get(gid or "nogroup")
        if not g:
            return
        changed = False
        for key in ("owners", "masters", "admins"):
            if openid in g.get(key, []):
                g[key].remove(openid)
                changed = True
        if changed:
            self._audit(gid, openid, "退群，移除身份")
            self._save()

    def reset_group(self, gid: str, openid: str = "", note: str = "机器人被重新拉入群，身份重置") -> None:
        """把某群身份重置为「只有拉入者一名高级管理员」。

        语义对齐 CaiBotLite（event/add_robot.py：重新拉入时 admins 只留拉入者、parent_open_id 置空）：
        机器人被踢后又被重新拉进群，视为该群数据作废，由拉入者重新开始。
        """
        g = self.group(gid)
        g["owners"] = [openid] if openid else []
        g["masters"] = []
        g["admins"] = []
        self._audit(gid, openid, note)
        self._save()

    # ────────────── 群开关（config dict） ──────────────
    def _cfg(self, gid: str) -> dict:
        g = self.group(gid)
        if not isinstance(g.get("config"), dict):
            g["config"] = {"allow_member_map": False, "show_online_players": True, "notify_server_status": True}
        return g["config"]

    def map_allowed(self, gid: str) -> bool:
        return bool(self._cfg(gid).get("allow_member_map", False))

    def set_map_allowed(self, gid: str, flag: bool, operator: str = "") -> tuple[bool, str]:
        self._cfg(gid)["allow_member_map"] = bool(flag)
        self._audit(gid, operator, f"允许成员获取地图 {'开' if flag else '关'}")
        self._save()
        return True, "已开启" if flag else "已关闭"

    def show_online_players(self, gid: str) -> bool:
        return bool(self._cfg(gid).get("show_online_players", True))

    def notify_server_status(self, gid: str) -> bool:
        """服务器上/下线是否在本群通知（默认开；可在群里用「服务器通知 关」关闭）"""
        return bool(self._cfg(gid).get("notify_server_status", True))

    def set_notify_server_status(self, gid: str, flag: bool, operator: str = "") -> tuple[bool, str]:
        self._cfg(gid)["notify_server_status"] = bool(flag)
        self._audit(gid, operator, f"服务器上/下线通知 {'开' if flag else '关'}")
        self._save()
        return True, "已开启" if flag else "已关闭"

    def set_show_online_players(self, gid: str, flag: bool, operator: str = "") -> tuple[bool, str]:
        self._cfg(gid)["show_online_players"] = bool(flag)
        self._audit(gid, operator, f"允许查看在线玩家 {'开' if flag else '关'}")
        self._save()
        return True, "已开启" if flag else "已关闭"

    # ────────────── 汇总展示 ──────────────
    def summary(self, gid: str) -> str:
        """群内身份配置汇总文本（权限查询用）"""
        owners = self.owners_of(gid)
        lines = [f"高级管理员：{'、'.join(owners) if owners else '（未设置）'}"]
        lines.append(f"服主：{'、'.join(self.members_of_role(gid, MASTER)) or '（无）'}")
        lines.append(f"管理员：{'、'.join(self.members_of_role(gid, ADMIN)) or '（无）'}")
        lines.append(f"允许成员获取地图：{'已开启' if self.map_allowed(gid) else '已关闭'}")
        lines.append(f"允许查看在线玩家：{'已开启' if self.show_online_players(gid) else '已关闭'}")
        lines.append(f"服务器上/下线通知：{'已开启' if self.notify_server_status(gid) else '已关闭'}")
        return "\n".join(lines)