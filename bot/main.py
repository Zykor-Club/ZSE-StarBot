# -*- coding: utf-8 -*-
"""
QQ 群入群申请审核机器人
依据 QQ 机器人开放平台 API v2 最新文档（2026-09）开发

功能：
  1. 轮询拉取群入群申请  GET  /v2/groups/{group_openid}/join_request_list
  2. 发现新申请 -> 在群内发送 "Markdown + 批准/拒绝按钮" 审核卡片
  3. 群管理员点击按钮 -> 审批  POST /v2/groups/{group_openid}/approval_join_request/{member_openid}

运行前提（缺一不可，详见 docs/官方接口速查.md 与 README.md）：
  - 机器人已加入目标群，且是【群管理员】（入群申请接口硬性要求）
  - 已申请 Markdown / 按钮权限（按钮自定义需"内邀开通"）
  - 群里开启接收机器人主动消息
"""

import asyncio
import io
import json
import os
import random
import re
import tempfile
import time
from collections import deque
from datetime import datetime

import aiohttp
import yaml

import botpy
from botpy import logging
from botpy.http import Route
from botpy.interaction import Interaction
from botpy.manage import GroupManageEvent
from botpy.message import GroupMessage
from botpy.types.inline import Keyboard, KeyboardRow, Button, RenderData, Action, Permission
from botpy.types.message import MarkdownPayload, KeyboardPayload

import github_monitor
from card_render import render_card
from zse_server import (ZseServer, decode_map_png, decode_archive_zip, decode_compressed_b64,
                        make_ssl_context)

RANK_PAGE_SIZE = 10   # 排行卡每页条数（图片与翻页按钮共用，必须一致）
LEX_LIMIT = 12               # 图鉴搜索结果一次展示条数
VOTE_MAX_OPTIONS = 6         # 投票卡固定 6 项（满员后新提案顶掉最旧的一条）
VOTE_RANDOM_OPTIONS = 3      # /种子投票 只随机 3 条，其余 3 个位置留给玩家提案
VOTE_PROPOSALS_PER_USER = 2  # 每人同时最多在场提案数
_LEX_CMDS: list = []         # 懒初始化：本常量区在 import lexicon 之前，不能在此直接引用模块


def _lexicon_match(raw):
    """识别图鉴子指令（si/sn/sp/sb/sx 与中文别名），返回命中的指令词，未命中返回 None。

    2 字母指令必须跟分隔符（空格/全角空格/冒号），避免把 sin 这类普通单词误判成 si。
    """
    if not _LEX_CMDS:
        _LEX_CMDS.extend(sorted(lexicon.COMMANDS.keys(), key=len, reverse=True))
    low = str(raw or "").lower()
    for cmd in _LEX_CMDS:
        c = cmd.lower()
        if not low.startswith(c):
            continue
        if c.isascii():
            nxt = low[len(c):len(c) + 1]
            if low == c or nxt in (" ", "　", ":", "："):
                return cmd
        else:
            return cmd
    return None


_PING_LAST: dict = {}        # (host, port) -> 上次 ping 时间戳（5 分钟冷却）
_PING_COOLDOWN = 300


def _md_fence_safe(value) -> str:
    """代码围栏（```）内文本：只处理能打破围栏的反引号，**保留换行与原字符**
    （远程指令输出是多行的，不能像卡片正文那样压平/替换尖括号）"""
    s = str(value or "")
    return s.replace("```", "'''").replace("`", "'")


def _md_safe(value) -> str:
    """外部字符串进 markdown 卡片前转义：防止注入 <qqbot-at-user> 等标签或破坏排版。

    覆盖来源：插件列表的作者/描述（第三方插件元数据）、服务器自报名、世界名、
    排行榜标题、远程指令输出等——这些都不是我们可控的文本。
    """
    s = str(value or "")
    for ch, rep in (("<", "["), (">", "]"), ("`", "'"), ("\n", " "), ("\r", " ")):
        s = s.replace(ch, rep)
    return s
from whitelist_mail import (ChangeStore, MailSender, PendingStore, VerifyManager, WhitelistStore,
                            check_name_ok, check_qq_email)
from groups_registry import GroupRegistry
import help_content
from rank_render import render_rank_card
import lexicon
from lexicon import Lexicon
from lexicon_render import render_lexicon_card
from server_status_store import ServerStatusStore
from bind_rules import check_bind_request, normalize_email, is_valid_qq_email
from economy_store import EconomyStore, SIGN_BASE, SIGN_BONUS, MAX_ADD

# 在线奖励：每满 1 小时发放的喵币数
def _find_account_items(obj, depth: int = 0):
    """在回包里递归找出"账号-秒数"列表：兼容任意外层结构与字段名（account/name）"""
    if depth > 4:
        return None
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and ("account" in obj[0] or "name" in obj[0]):
            return obj
        for x in obj:
            r = _find_account_items(x, depth + 1)
            if r:
                return r
        return None
    if isinstance(obj, dict):
        for k in ("items", "data", "payload", "list", "rows"):
            if k in obj:
                r = _find_account_items(obj[k], depth + 1)
                if r:
                    return r
        for v in obj.values():
            r = _find_account_items(v, depth + 1)
            if r:
                return r
    return None


PLAYTIME_PER_HOUR = 3
PLAYTIME_PER_CYCLE_MAX = 12
from whitelist_users import WhitelistUsers
from econ_render import render_info_card, render_rank_card
from seeds import Seeds
from seed_render import render_seed_list_card
from permissions import (
    PERM_MAIL_RESET,
    PermissionManager,
    OWNER, MASTER, ADMIN, MEMBER, RANK,
    ROLE_ALIAS, PERM_NEED_LABEL, role_label,
    PERM_ADD_SERVER, PERM_DEL_SERVER, PERM_EXEC,
    PERM_MAP_FETCH, PERM_MAP_TOGGLE, PERM_ONLINE_SHOW, PERM_ROLE_MANAGE,
    PERM_BROADCAST, PERM_VOTE_MANAGE, PERM_VOTE_PUSH, PERM_RESET, PERM_VOTE_PROPOSAL_DEL,
PERM_BACKUP, PERM_WORLD_SETTINGS, PERM_BACKUP_RESTORE, PERM_ECON_ADMIN,
    PERM_PROGRESS_NOTIFY, PERM_SAY_ALL, PERM_STATUS_NOTIFY, 
)
from github_monitor import (
    get_repo_stats, get_latest_pulls, get_latest_issues, get_org_repos, get_repo_stargazers,
)
from upload_media import send_group_image, send_group_file
from vote_store import (
    VoteStore, generate_random_options, parse_candidates, parse_server_index,
)
from vote_render import render_vote_card
from progress_notify_store import ProgressNotifyStore
from progress_unlock_store import ProgressUnlockStore
from progress_render import (render_progress_card, render_notify_card, render_unlock_card,
                             format_unlock_time, resolve_boss, boss_cn, BOSSES)

_log = logging.get_logger()

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

# ────────────────── SDK 补丁（自动扩展官方 SDK，不需要改 SDK 源码）──────────────────
# 当机器人在开放平台开启"接收所有消息"（全量模式）时，群里每条消息以
# GROUP_MESSAGE_CREATE 事件推送，而官方 SDK 只解析 GROUP_AT_MESSAGE_CREATE。
# 这里按 SDK 原生的 "parse_* 方法自动收集" 机制补一个解析器，让全量群消息也能收到。
import botpy.connection as _botpy_connection


def _parse_group_message_create(self, payload):
    # 注意：SDK 的 gateway 是同步调用 parse_* 解析器的（见 gateway.py on_message），
    # 所以这里不能用 async def，否则 coroutine 永远不会被 await。
    _message = GroupMessage(self.api, payload.get("id", None), payload.get("d", {}))
    self._dispatch("group_message_create", _message)


_botpy_connection.ConnectionState.parse_group_message_create = _parse_group_message_create


def _strip_at_marks(text: str) -> str:
    """去掉消息里的 @ 标记（<@openid> / <qqbot-at-user .../> / [@名字] 等形态），只留指令本体。
    这样"@机器人 在线"也能正常命中指令（QQ 群聊 @ 机器人的 content 会带这些标记）。"""
    if not text:
        return ""
    t = re.sub(r"<qqbot-at-user[^>]*/?>", "", text)
    t = re.sub(r"<@[^>]+>", "", t)
    t = re.sub(r"\[@[^\]]*]", "", t)
    return t.strip()


def _fmt_left(sec: int) -> str:
    """剩余秒数 → 人性化文案（天/小时/分钟），用于限流提示"""
    sec = max(0, int(sec))
    if sec >= 86400:
        return f"{sec // 86400} 天 {sec % 86400 // 3600} 小时"
    if sec >= 3600:
        return f"{sec // 3600} 小时 {sec % 3600 // 60} 分钟"
    return f"{sec // 60} 分钟"


class GroupMemberEvent:
    """群成员加入/退出事件（官方 GROUP_MEMBER_ADD / GROUP_MEMBER_REMOVE，INTENT 1<<24）
    字段：timestamp、group_openid、member_openid（官方事件不带 username）"""

    __slots__ = ("event_id", "timestamp", "group_openid", "member_openid")

    def __init__(self, event_id, data):
        self.event_id = event_id
        self.timestamp = data.get("timestamp", None)
        self.group_openid = data.get("group_openid", None)
        self.member_openid = data.get("member_openid", None)


def _parse_group_member_remove(self, payload):
    self._dispatch("group_member_remove", GroupMemberEvent(payload.get("id", None), payload.get("d", {})))


def _parse_group_member_add(self, payload):
    self._dispatch("group_member_add", GroupMemberEvent(payload.get("id", None), payload.get("d", {})))


_botpy_connection.ConnectionState.parse_group_member_remove = _parse_group_member_remove
_botpy_connection.ConnectionState.parse_group_member_add = _parse_group_member_add


def load_config(path: str = CONFIG_PATH) -> dict:
    """读取配置文件"""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class GroupReviewClient(botpy.Client):
    """入群申请审核机器人客户端"""

    def __init__(self, groups, poll_interval: int = 20, apply_title: str = "入群申请", github_cfg: dict = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 监控的群：{群名: 群 openid}
        self.groups: dict = {g["name"]: g["group_openid"] for g in groups if g.get("group_openid")}
        self.poll_interval = poll_interval
        # 申请卡片标题里的组织名（"꧁༺ {apply_title} 入群申请 ༻꧂"）
        self.apply_title = apply_title
        # GitHub 推送配置（owner/repo/org/轮询间隔/自动推送开关）
        self.github_cfg: dict = github_cfg or {}
        if self.github_cfg.get("token"):
            github_monitor.GH_TOKEN = self.github_cfg["token"].strip()
        # 已处理过的申请 id，避免重复发卡；deque 限长防止内存膨胀
        self._handled: deque = deque(maxlen=10000)
        # 入群申请防刷屏：{群|用户名|理由 -> 上次成功转发审核卡的时间}，1 分钟内相同申请不再转发
        # 注意：仅"发过卡"才计时拦截，新申请首次出现立即发卡，不会被 60 秒窗口拖慢
        self._apply_last_sent: dict = {}
        # 入群申请轮询跳过表：{群openid: 跳过到的时间戳}（机器人非群管理员时报 11703，暂停避免日志刷屏）
        self._join_poll_skip: dict = {}
        # 入群引导卡待补发：{群openid: 上次尝试时间}（加群时主动消息权限未开则标记，该群能通信后补发）
        self._guide_pending: dict = {}
        # 登录请求卡推送防刷屏：{群|玩家名 -> 上次推送时间}，5 分钟内同一玩家只推一次
        self._login_push_last: dict = {}
        # 查背包回包短缓存：{(服务器序号, 玩家名): (时间, rec, payload)}，60 秒内复用
        self._lookbag_cache: dict = {}
        # 群成员信息缓存：{group_openid:member_openid -> {username, avatar,...}}
        self._member_cache: dict = {}
        # GitHub 已推送的最大 PR / Issue 编号：{full_name: max_number}
        self._gh_last_pr: dict = {}
        self._gh_last_issue: dict = {}
        # star 监控基线：{full_name: {"count": int, "latest": 最近一次 star 时间}}
        self._gh_last_star: dict = {}
        self._polling = False
        # starZSEbot 服务端（TShock 插件长连接）
        _cfg = load_config()
        self.zse_port = int(_cfg.get("zse_server_port", 13140))
        # 插件通道 TLS：指向含 fullchain.pem + privkey.pem 的目录则启用 wss，留空=明文 ws
        self.zse_tls_dir = (cfg.get("zse_tls_dir") or "").strip()
        self.whitelist_store = WhitelistStore()
        self.lexicon_store = Lexicon()
        self.seeds = Seeds()
        # 喵币账本（机器人侧 SQLite：全联合体系 + 全服务器共用一份）
        self.economy = EconomyStore()
        self._econ_clear_ts = 0.0
        # 白名单"以人为主体"的新表（第 1 步：只积累数据 + 定期回填，不改进服判定）
        self.whitelist_users = WhitelistUsers()
        self._wl_sync_ts = 0.0
        # 累计在线时长缓存 {玩家名: 秒}：各服上报后按账号取最大（一个人不可能同时在两个服玩）
        self._playtime = {}
        self._playtime_ts = 0.0
        self.status_store = ServerStatusStore()
        # 投票结果卡发布锁：结束投票指令与 60 秒调度可能同时想发布同一场投票，
        # 不加锁会出现"结果卡发两轮"（每个群多一张卡）
        self._vote_pub_lock = asyncio.Lock()
        # 指令去重：开启「接收所有消息」后，一条 @ 指令可能同时从
        # on_group_at_message_create 与 on_group_message_create 两个事件到达 → 必须只处理一次
        # （普通指令的第二次回复会被 QQ 判重挡掉，但主动消息类指令会对所有群多发一轮）
        self._handled_msgs = {}
        # 首杀播报幂等表：同一场首杀只播报一次（插件重发/WS 重连重放时不再多播一轮）
        self._notify_fired = {}
        # 设备登录校验配置（IP 跨市级变动；离线库缺失/解析失败自动跳过城市校验）
        dev_cfg = _cfg.get("device_check", {}) or {}
        self.whitelist_store.configure_device_check(
            enabled=bool(dev_cfg.get("enabled", True)),
            xdb_path=str(dev_cfg.get("ip2region_xdb", "") or ""),
            city_max=int(dev_cfg.get("city_max", 3)),
        )
        # 白名单变更事务（改名限流 / 邮箱改绑超时回滚）
        self.changes = ChangeStore()
        self.whitelist_store.attach_changes(self.changes)
        self.pending_store = PendingStore()
        self.zse_server = ZseServer(whitelist=self.whitelist_store, pending=self.pending_store)
        # 权限系统：四身份（owner/master/admin/member）+ 群开关
        self.perms = PermissionManager()
        # 多群联合：群注册表（群ID分配 + 群间联合关系）
        self.registry = GroupRegistry()
        self.zse_server.registry = self.registry
        # 种子投票引擎：持久化到 bot/votes.json（与代码同目录）
        self.votes = VoteStore(os.path.join(os.path.dirname(os.path.abspath(__file__)), "votes.json"))
        # 进度提醒订阅：持久化到 bot/progress_notifications.json（每群 × 每服 × 每 boss 独立）
        self.progress_notifies = ProgressNotifyStore(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress_notifications.json"))
        # 进度解锁提醒订阅：持久化到 bot/progress_unlocks.json（每群 × 每服 × 每 boss 独立，覆盖式）
        self.progress_unlocks = ProgressUnlockStore(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress_unlocks.json"))
        # 插件首杀推送 → 播报到订阅群（回调由 zse_server 的 WS 循环触发）
        self.zse_server.on_progress_notify = self._on_progress_notify_push
        # 插件判定 need_login → 推送带「登录/拒绝」按钮的请求卡（回调由 zse_server 的 WS 循环触发）
        self.zse_server.on_need_login = self._on_need_login_push
        # 投票配置段（旧配置缺失时全部走默认值）
        self._vote_config: dict = _cfg.get("vote", {}) or {}
        self._appid = str(_cfg.get("appid", "") or "")
        # 迁移种子：config.yaml 群条目可显式指定 owner_openid（机器人已在群里、没有 add-robot 事件时用）
        for g in (_cfg.get("groups") or []):
            ogid = g.get("group_openid") or ""
            oid = (g.get("owner_openid") or "").strip()
            if ogid and oid and not self.perms.owners_of(ogid):
                self.perms.set_first_owner(ogid, oid, who="config", note="config.yaml 指定的高级管理员")
                _log.info("已从 config.yaml 设置群 %s 的高级管理员", ogid)
        # 白名单发送配置（SMTP）
        smtp_cfg = _cfg.get("smtp", {}) or {}
        mail_sender = MailSender(
            smtp_host=smtp_cfg.get("host", "smtp.qq.com"),
            smtp_port=int(smtp_cfg.get("port", 465)),
            username=smtp_cfg.get("username", ""),
            auth_code=smtp_cfg.get("auth_code", ""),
            from_name=smtp_cfg.get("from_name", "ZSE联合体"),
        )
        self.mail = VerifyManager(
            sender=mail_sender,
            code_ttl=int(smtp_cfg.get("code_ttl", 300)),
            retry_cooldown=int(smtp_cfg.get("retry_cooldown", 240)),
            max_mail=int(smtp_cfg.get("max_mail", 5)),
        )
        self.bot_name = _cfg.get("bot_name", "猫娘小梦")
        # 本机器人 openid：从"被 @ 的事件"/mentions 运行时学习；也可在 config 固化（群消息里 @ 本机器人即 <@我们的openid>）
        self._self_openid = ""
        self._bot_openid = str(_cfg.get("bot_openid", "") or "").strip().upper()

    # ───────────────────── 就不需要自己封装：直接复用 SDK 网络层 ─────────────────────
    async def get_group_join_request_list(self, group_openid: str, limit: int = 50, cursor: str = ""):
        """
        拉取入群申请列表
        文档: GET /v2/groups/{group_openid}/join_request_list
        """
        route = Route("GET", "/v2/groups/{group_openid}/join_request_list", group_openid=group_openid)
        return await self.http.request(route, params={"limit": limit, "cursor": cursor})

    async def approval_group_join_request(
        self,
        group_openid: str,
        member_openid: str,
        *,
        op: str,
        join_request_id: str = None,
        reject_reason: str = None,
        add_to_member_blacklist: bool = False,
    ):
        """
        审批入群申请
        文档: POST /v2/groups/{group_openid}/approval_join_request/{member_openid}
        op: approve=通过, decline=拒绝
        """
        payload = {"op": op}
        if join_request_id:
            payload["join_request_id"] = join_request_id
        if reject_reason:
            payload["reject_reason"] = reject_reason
        if add_to_member_blacklist:
            payload["add_to_member_blacklist"] = True
        route = Route(
            "POST",
            "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
            group_openid=group_openid,
            member_openid=member_openid,
        )
        return await self.http.request(route, json=payload)

    async def get_group_member(self, group_openid: str, member_openid: str):
        """
        获取群成员详情（含 username/avatar）
        文档: GET /v2/groups/{group_openid}/members/{member_openid}
        注意：官方该接口需要开通"群成员获取"权限，未开通会报 11253，
        调用方应容忍失败（返回 None），降级展示 openid。结果做内存缓存避免高频请求。
        """
        if not member_openid:
            return None
        key = f"{group_openid}:{member_openid}"
        if key in self._member_cache:
            return self._member_cache[key]
        try:
            route = Route(
                "GET",
                "/v2/groups/{group_openid}/members/{member_openid}",
                group_openid=group_openid,
                member_openid=member_openid,
            )
            data = await self.http.request(route)
        except Exception as e:
            _log.warning("获取群成员信息失败(可能未开通接口权限 11253)：%s", e)
            data = None
        self._member_cache[key] = data
        if len(self._member_cache) > 2000:  # 防止缓存无限膨胀
            self._member_cache.clear()
        return data

    # ───────────────────────── 事件回调 ─────────────────────────
    async def on_ready(self):
        """WebSocket 连接就绪后，启动轮询任务与 starZSEbot 服务端"""
        _log.info("机器人已上线，当前监控群：%s", list(self.groups.keys()))
        if not self._polling:
            self._polling = True
            asyncio.create_task(self.poll_join_requests())
        if self.github_cfg.get("repo") and self.github_cfg.get("owner"):
            asyncio.create_task(self.github_poll())
            _log.info("GitHub 监控已启动：%s/%s（组织 %s）",
                      self.github_cfg.get("owner"), self.github_cfg.get("repo"), self.github_cfg.get("org"))
        if not getattr(self, "_zse_started", False):
            self._zse_started = True
            asyncio.create_task(self._start_zse_server())
        # 种子投票后台调度（更新卡 / 自动截止），独立于命令处理
        if not getattr(self, "_vote_started", False):
            self._vote_started = True
            asyncio.create_task(self.vote_scheduler())
        # 进度解锁提醒后台轮询（每 60 秒同步锁表并判定触发窗口），独立于命令处理
        if not getattr(self, "_unlock_started", False):
            self._unlock_started = True
            asyncio.create_task(self.progress_unlock_scheduler())

    async def _start_zse_server(self):
        """启动 aiohttp 服务端，监听 TShock 插件连接"""
        from aiohttp import web
        runner = web.AppRunner(self.zse_server.build_app())
        await runner.setup()
        ctx = None
        tls_dir = getattr(self, "zse_tls_dir", "") or ""
        if tls_dir:
            cert = os.path.join(tls_dir, "fullchain.pem")
            key = os.path.join(tls_dir, "privkey.pem")
            if os.path.exists(cert) and os.path.exists(key):
                ctx = make_ssl_context(cert, key)
            else:
                _log.warning("配置了 zse_tls_dir 但缺少 fullchain.pem/privkey.pem，本次仍以明文 ws 提供：%s", tls_dir)
        site = web.TCPSite(runner, "0.0.0.0", self.zse_port, ssl_context=ctx)
        await site.start()
        _log.info("starZSEbot 服务端已监听 %s 端口（%s）", self.zse_port, "wss/TLS" if ctx else "ws 明文")

    async def on_group_add_robot(self, event: GroupManageEvent):
        """机器人被拉进群：自动加入轮询列表（方便获取 group_openid）。
        按 QQ 官方事件，op_member_openid = 把机器人拉进群的用户，
        默认将其设为该群的高级管理员（CaiBotLite 同款逻辑：拉入者必为管理员，不清除其他已设身份），
        并发送入群引导卡；若发送失败（主动消息权限未开）则标记待补发。"""
        gid = event.group_openid
        _log.info("机器人被添加到群聊 group_openid=%s", gid)
        if gid not in self.groups.values():
            self.groups[f"群({gid[:8]}...)"] = gid
            _log.warning("已将 %s 加入轮询列表（这个 openid 建议复制到 config.yaml 固化）", gid)
        op = getattr(event, "op_member_openid", None) or ""
        self.registry.get_or_assign(gid)
        if self.registry.consume_kicked(gid):
            # 该群被移出过机器人 → 数据作废重建（对齐 CaiBotLite event/add_robot.py：
            # 重新拉入时 admins 只留拉入者、parent_open_id 置空；我们额外清空该群服务器绑定）
            unlinked = self.registry.unlink(gid)
            self.perms.reset_group(gid, op)
            n_srv = await self.zse_server.unregister_all(gid)
            _log.warning("机器人被重新拉入群 %s：身份已重置（拉入者 %s…）、解除联合 %d 个群、清空服务器 %d 台",
                         gid[:8], (op or "")[:8], len(unlinked), n_srv)
        else:
            # 首次加入：身份归总群（_eff_gid）；独立/总群时即 gid 自身
            eff = self._eff_gid(gid)
            if op and not self.perms.is_owner(eff, op):
                self.perms.set_first_owner(eff, op, who=op, note="机器人被拉入群，自动设为高级管理员")
                _log.info("已将添加机器人的用户设为群 %s 的高级管理员", eff)
        try:
            # 以"事件被动回复"发送引导卡（event_id），不依赖群主开启主动发言权限（CaiBotLite 同款做法）
            await self._send_add_robot_guide(
                gid, op,
                reply_to={"event_id": getattr(event, "event_id", None)},
            )
        except Exception as e:
            _log.error("入群引导卡发送异常: %s", e)
            self._guide_pending[gid] = time.time()

    async def _send_add_robot_guide(self, gid: str, op: str,
                                    reply_to: dict = None):
        """机器人入群引导卡：欢迎 + 高级管理员确认 + 权限设置引导 + 帮助/设置按钮。
        reply_to 传 msg_id/event_id 使发送为被动回复（免主动消息权限）；为空则走主动消息。"""
        at_tag = f'<qqbot-at-user id="{op}" />' if op else ""
        avatar = self._openid_avatar_md(op) if op else ""
        lines = ["## Ciallo！ 欢迎使用猫娘机器人~🎉", ""]
        if op:
            lines.append(f"> 已将{at_tag} {avatar} 设为本群**高级管理员**喵!")
        else:
            lines.append("> 欢迎使用本机器人喵！")
        lines += [
            "- 您可以使用 `设置身份 <玩家名> <身份>` 来添加管理员喵！",
            "",
            "────────────",
            "> 在使用本机器人前，请群主为本机器人**设置可获取的信息范围**喵！",
        ]
        markdown = MarkdownPayload(content="\n".join(lines))
        # 帮助 / 关于按钮（所有人都可点）
        keyboard = KeyboardPayload(
            content=Keyboard(
                rows=[
                    KeyboardRow(
                        buttons=[
                            Button(
                                id="help",
                                render_data=RenderData(label="帮助", style=1),
                                action=Action(
                                    type=1,
                                    permission=Permission(type=2),
                                    data=json.dumps({"cmd": "help"}, ensure_ascii=False),
                                ),
                            ),
                            Button(
                                id="about",
                                render_data=RenderData(label="关于", style=1),
                                action=Action(
                                    type=1,
                                    permission=Permission(type=2),
                                    data=json.dumps({"cmd": "about"}, ensure_ascii=False),
                                ),
                            ),
                        ]
                    )
                ]
            )
        )
        reply_to = {k: v for k, v in (reply_to or {}).items() if v}
        try:
            await self.api.post_group_message(
                group_openid=gid, msg_type=2, markdown=markdown, keyboard=keyboard, **reply_to
            )
            _log.info("已发送入群引导卡")
            self._guide_pending.pop(gid, None)
        except Exception as e:
            _log.warning("入群引导卡发送失败(可能未开主动消息)：%s，尝试纯文本", e)
            plain = self._markdown_to_plain(
                "\n".join([ln for ln in lines if ln]), at_tag,
                f"@{self._disp(gid, op)}" if op else "")
            try:
                await self.api.post_group_message(
                    group_openid=gid, msg_type=0,
                    content=plain,
                    **reply_to
                )
                _log.info("已完成入群引导纯文本降级")
                self._guide_pending.pop(gid, None)
            except Exception as e2:
                _log.error("入群引导纯文本发送也失败: %s", e2)
                self._guide_pending[gid] = time.time()  # 标记待补发：该群能通信后再发

    async def on_group_del_robot(self, event: GroupManageEvent):
        _log.info("机器人被移出群聊 group_openid=%s", event.group_openid)
        # 打标记：重新拉入时按「数据作废重建」处理（语义对齐 CaiBotLite event/add_robot.py）
        try:
            self.registry.mark_kicked(event.group_openid)
        except Exception as e:
            _log.warning("记录被移出标记失败: %s", e)

    async def on_group_at_message_create(self, message):
        """群里 @机器人 的消息事件（未开全量接收时触发）：自动接入 + 指令分发"""
        # 学习本机器人 openid：被 @ 时原始内容里的 <@!openid> / <@openid> 就是自己
        raw = getattr(message, "content", "") or ""
        m = re.search(r"<@!?([0-9A-Fa-f]{32})>", raw)
        if m:
            self._self_openid = m.group(1)
            _log.debug("学习到本机器人 openid=%s", self._self_openid)
        await self._capture_group(message)
        await self._dispatch_command(message, is_at_event=True)

    async def on_group_message_create(self, message):
        """
        群全量消息事件（机器人管理端开启"接收所有消息"后，群里每条消息都走这里）。
        两种事件都能拿到 group_openid（API v2 用 openid 定位群，群号不是 openid）。
        @机器人 的消息在开启全量接收后也走本事件，所以指令分发必须同时挂在这里。
        """
        await self._capture_group(message)
        await self._dispatch_command(message)

    def _at_targets_other(self, message) -> bool:
        """判断群消息 @ 的是否为别人/其它机器人（返回 True 则本机器人不回应）。
        优先级：① @ 标记里的 openid（<@openid> 形式）对比本机器人 openid（config 固化或运行时学习）
            ② mentions 段（含 bot 标志）→ 学到后转 ①
            ③ 文本形态 @机器人名。
        全量消息里 @ 本机器人是 <@openid> 标记（非文本名、mentions 可能为空），所以必须有本机器人 openid 才能精确判断。"""
        raw = getattr(message, "content", "") or ""
        if not raw or "@" not in raw:
            return False
        targets = set(re.findall(r"<@!?([0-9A-Fa-f]{32})>", raw))
        mentions = getattr(message, "mentions", None) or []
        for m in mentions:
            for key in ("id", "member_openid"):
                mid = getattr(m, key, None) or ""
                if mid:
                    targets.add(mid.upper())
        # 学到本机器人 openid（mention 的 bot 且昵称=机器人名，或标记 openid == config 的 bot_openid）
        if not self._self_openid and self._bot_openid and self._bot_openid.upper() in targets:
            self._self_openid = self._bot_openid
            _log.info("已按 config 固化本机器人 openid=%s", self._self_openid)
        if self._self_openid and self._self_openid.upper() in targets:
            return False  # @ 了我们
        for m in mentions:
            if not getattr(m, "bot", None):
                continue
            if self.bot_name and self.bot_name in (getattr(m, "username", None) or ""):
                mid = getattr(m, "id", None) or getattr(m, "member_openid", None) or ""
                if mid and not self._self_openid:
                    self._self_openid = mid.upper()
                    _log.info("从 mentions 学到本机器人 openid=%s", self._self_openid)
                return False
        if self.bot_name and f"@{self.bot_name}" in raw:
            return False  # 文本形态 @了我们
        _log.info("跳过非本机器人的 @ 消息: %s", raw[:50])
        return True

    async def _dispatch_command(self, message, is_at_event: bool = False):
        """指令分发：TShock 服务器管理（starZSEbot 协议）+ 权限管理 + GitHub"""
        # 同一条消息可能被"@事件"和"全量消息事件"各推一次 → 按消息 id 去重
        mid = getattr(message, "id", None) or ""
        if mid:
            now = time.time()
            if mid in self._handled_msgs:
                _log.info("忽略重复分发的同一条消息: %s…", str(mid)[:8])
                return
            self._handled_msgs[mid] = now
            if len(self._handled_msgs) > 256:      # 只保留最近 5 分钟，防止无限增长
                for k in [k for k, t in self._handled_msgs.items() if now - t > 300]:
                    self._handled_msgs.pop(k, None)
        raw = (message.content or "").strip()
        # @ 的是别人（含其它机器人）而非本机器人 → 不回应（仅事件级 @ 我们、或纯指令才回应）
        if not is_at_event and self._at_targets_other(message):
            return
        text = _strip_at_marks(raw).lstrip("/")
        low = text.lower()
        try:
            gid = message.group_openid
        except AttributeError:
            gid = None
        user_openid = self._user_openid(message)

        # ── 权限管理 ──
        if low.startswith("设置高级管理员"):  # 本群无高级管理员时的一次性追授（也用于退群接管后）
            await self.cmd_bootstrap_owner(message, text, gid, user_openid)
            return
        if low.startswith("设置身份"):  # 设置身份 <玩家名> <高级管理员|master|admin>
            if not await self._perm_ok(message, gid, user_openid, PERM_ROLE_MANAGE, "设置身份"):
                return
            await self.cmd_set_role(message, text, gid, user_openid)
            return
        if low.startswith("取消身份"):  # 取消身份 <玩家名> [角色]
            if not await self._perm_ok(message, gid, user_openid, PERM_ROLE_MANAGE, "取消身份"):
                return
            await self.cmd_remove_role(message, text, gid, user_openid)
            return
        if low in ("权限查询", "身份查询", "查看权限"):
            await self.cmd_perm_query(message, gid, user_openid)
            return
        if low in ("群信息", "群资料"):  # 群信息：任何群员可查
            await self.cmd_group_info(message, gid)
            return
        if low.startswith(("帮助", "help")):  # 帮助 [分类]：帮助卡片（带分类按钮）/ 该分类指令清单
            await self.cmd_help(message, text, gid, user_openid)
            return
        if low in ("关于", "about"):  # 关于：机器人信息（任何群员可查，/关于 亦可）
            await self.cmd_about(message, gid, user_openid)
            return
        if low.startswith("绑定联合群"):  # 绑定联合群 <群ID>：双方高级管理员确认后互相关联
            await self.cmd_link_group(message, text, gid, user_openid)
            return
        if low.startswith("解除联合群"):  # 解除联合群 <群ID>
            await self.cmd_unlink_group(message, text, gid, user_openid)
            return
        if low.startswith("允许成员获取地图"):  # 允许成员获取地图 [开|关]（admin 及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_MAP_TOGGLE, "允许成员获取地图"):
                return
            await self.cmd_map_toggle(message, text, gid, user_openid)
            return
        if low.startswith("服务器通知"):  # 服务器通知 [开|关]（admin 及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_STATUS_NOTIFY, "服务器通知"):
                return
            await self.cmd_server_status_notify(message, text, gid, user_openid)
            return
        if low.startswith("允许查看在线玩家"):  # 允许查看在线玩家 [开|关]（admin 及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_ONLINE_SHOW, "允许查看在线玩家"):
                return
            await self.cmd_online_show(message, text, gid, user_openid)
            return

        # ── TShock 服务器管理（starZSEbot 协议）──
        if low.startswith("添加服务器"):  # 添加服务器 <ip/域名> <端口> <绑定码>
            if not await self._perm_ok(message, gid, user_openid, PERM_ADD_SERVER, "添加服务器"):
                return
            await self.cmd_add_server(message, text, gid, user_openid)
            return
        if low in ("在线", "服务器在线"):
            await self.cmd_online(message, gid)
            return
        if low.startswith("删除服务器"):  # 删除服务器 <序号>
            if not await self._perm_ok(message, gid, user_openid, PERM_DEL_SERVER, "删除服务器"):
                return
            await self.cmd_del_server(message, text, gid, user_openid)
            return
        if low in ("服务器列表", "服务器", "列表"):
            await self.cmd_server_list(message, gid)
            return
        if low.startswith("ping"):  # ping <域名/ip> <端口>
            await self.cmd_ping(message, text)
            return
        if low.startswith("获取地图"):  # 获取地图 <服务器序列号>
            if not self.perms.check(self._eff_gid(gid), user_openid, PERM_MAP_FETCH):
                await self._reply_markdown(
                    message,
                    "\n".join([
                        self.build_card_title("世界地图"), "",
                        "**❌ 群内未开放普通成员获取地图喵**", "",
                        "> 可由 管理员及以上 发送 `允许成员获取地图 开` 开放",
                    ]),
                )
                return
            await self.cmd_fetch_map(message, text, gid)
            return
        if low.startswith(("查背包", "查看背包", "查询背包")) or low == "背包":
            # 查背包 <服务器序号> [玩家名]：任何人可查（宽松，与 CaiBotLite 一致）
            await self.cmd_lookbag(message, text, gid, user_openid)
            return
        if low.startswith(("自踢", "自提", "自体")):  # 自踢：断开自己（所有人可用，需已绑定白名单）
            await self.cmd_self_kick(message, text, gid, user_openid)
            return
        if low.startswith("插件列表"):  # 插件列表 <序号>：该服务器已加载的插件
            await self.cmd_plugin_list(message, text, gid, user_openid)
            return
        if low.startswith("排行"):  # 排行 <序号> <项目> [参数] [页码]（所有人可用）
            await self.cmd_rank(message, text, gid, user_openid)
            return
        _lex = _lexicon_match(low)
        if _lex:  # 图鉴搜索：si/sn/sp/sb/sx（搜物品/搜生物/搜弹幕/搜增益/搜修饰）
            await self.cmd_lexicon(message, text, lexicon.COMMANDS[_lex], gid)
            return
        if low.startswith(("下载小地图文件", "下载小地图")):  # 下载小地图 <序号>：.map 文件
            if not self.perms.check(self._eff_gid(gid), user_openid, PERM_MAP_FETCH):
                await self._reply_markdown(message, "\n".join([
                    self.build_card_title("下载小地图"), "",
                    "**❌ 群内未开放普通成员获取地图喵**", "",
                    "> 可由 管理员及以上 发送 `允许成员获取地图 开` 开放",
                ]))
                return
            await self.cmd_download_map(message, text, gid)
            return
        if low.startswith(("下载地图", "下载世界文件", "下载存档")):  # 下载地图 <序号>：.wld 世界存档
            if not self.perms.check(self._eff_gid(gid), user_openid, PERM_MAP_FETCH):
                await self._reply_markdown(message, "\n".join([
                    self.build_card_title("下载地图"), "",
                    "**❌ 群内未开放普通成员获取地图喵**", "",
                    "> 可由 管理员及以上 发送 `允许成员获取地图 开` 开放",
                ]))
                return
            await self.cmd_download_world(message, text, gid)
            return
        if low.startswith(("远程指令", "远程执行")):  # 远程指令 <序号|all|*> <指令>
            if not await self._perm_ok(message, gid, user_openid, PERM_EXEC, "远程指令"):
                return
            await self.cmd_exec(message, text, gid)
            return
        if low.startswith("全服喊话"):  # 全服喊话 <内容>：对所有在线服务器广播（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_SAY_ALL, "全服喊话"):
                return
            await self.cmd_say_all(message, text, gid)
            return
        if low.startswith("广播"):  # 广播 <内容>：向联合区所有群发公告（服主+）
            if not await self._perm_ok(message, gid, user_openid, PERM_BROADCAST, "ZSE 联合广播"):
                return
            await self.cmd_broadcast(message, text, gid)
            return
        if low.startswith("喊话"):  # 喊话 <服务器序列号> <内容>：对指定服务器广播
            await self.cmd_say(message, text, gid)
            return

        # ── 种子投票 / 投票 / 结束投票 / 重置（AutoResetPlus 接入，见 spec add-seed-vote-reset）──
        # 分发顺序证据：「种子投票」「结束投票」先于「投票」判断；且二者均不以"投票"开头，
        # startswith("投票") 不会把「种子投票」「结束投票」误落到「投票」处理器。
        if low.startswith("世界设置"):  # 世界设置 <序号> [难度/大小/邪恶 值…]（改设置需管理员+）
            await self.cmd_world_settings(message, text, gid, user_openid)
            return
        if low.startswith("签到"):  # 签到 [玩家名]（需已绑定白名单）
            await self.cmd_sign(message, text, gid, user_openid)
            return
        if low.startswith(("我的信息", "我的积分", "我的喵币")):
            await self.cmd_my_points(message, text, gid, user_openid)
            return
        if low.startswith(("积分排行", "签到排行", "喵币排行")):
            _log.info("DISPATCH_RANK_MATCH")
            await self.cmd_points_rank(message, text, gid, user_openid)
            return
        if low.startswith(("发币", "扣币")):
            if not await self._perm_ok(message, gid, user_openid, PERM_ECON_ADMIN, "喵币管理"):
                return
            await self.cmd_econ_grant(message, text, gid, user_openid)
            return
        if low.startswith("重置经济"):
            if not await self._perm_ok(message, gid, user_openid, PERM_ECON_ADMIN, "重置经济"):
                return
            await self.cmd_econ_reset(message, text, gid, user_openid)
            return
        if low.startswith("备份列表") or low.startswith("备份 列表"):  # 备份列表 <序号>
            await self.cmd_backup_list(message, text, gid)
            return
        if low.startswith("回退备份") or low.startswith("还原备份"):  # 回退备份 <序号> <备份编号>（服主及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_BACKUP_RESTORE, "回退备份"):
                return
            await self.cmd_backup_restore(message, text, gid)
            return
        if low.startswith("备份") and not low.startswith("备份状态"):  # 备份 [发送] <序号>（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_BACKUP, "备份"):
                return
            await self.cmd_backup(message, text, gid)
            return
        if low.startswith("种子列表"):  # 种子列表 [页码]（所有人可用）
            await self.cmd_seed_list(message, text, gid)
            return
        if low.startswith("种子提案"):  # 种子提案 <服务器序号> <序号+序号…>（所有人可用）
            await self.cmd_seed_propose(message, text, gid, user_openid)
            return
        if low.startswith("撤回提案"):  # 撤回提案 <服务器序号> <编号>（提案人本人）
            await self.cmd_seed_withdraw(message, text, gid, user_openid)
            return
        if low.startswith("删除提案"):  # 删除提案 <服务器序号> <编号>（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_VOTE_PROPOSAL_DEL, "删除提案"):
                return
            await self.cmd_seed_delete(message, text, gid, user_openid)
            return
        if low.startswith("种子投票"):  # 种子投票 [序号] [候选…]（服主及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_VOTE_MANAGE, "种子投票"):
                return
            await self.cmd_seed_vote(message, text, gid, user_openid)
            return
        if low.startswith("结束投票"):  # 结束投票 [序号]（服主及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_VOTE_MANAGE, "结束投票"):
                return
            await self.cmd_end_vote(message, text, gid)
            return
        if low.startswith("推送投票"):  # 推送投票 [序号]：手动把进行中投票卡推送到联合区所有群（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_VOTE_PUSH, "推送投票"):
                return
            await self.cmd_push_vote(message, text, gid)
            return
        if low.startswith("查看投票"):  # 查看投票 <序号>：把指定服务器的投票卡发到本群（所有人可用，成功不回复确认）
            await self.cmd_view_vote(message, text, gid)
            return
        if low.startswith("投票"):  # 投票 <编号>（所有群员）
            await self.cmd_vote(message, text, gid, user_openid)
            return
        if low.startswith("重置"):  # 重置 [序号]（服主及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_RESET, "重置"):
                return
            await self.cmd_reset(message, text, gid)
            return

        # ── 进度查询 / 进度提醒 / 进度解锁提醒（查询所有人可用，提醒类管理员及以上）──
        # 分发顺序证据：「进度解锁提醒列表」必须先于「进度解锁提醒」判断（前者以后者为前缀）。
        if low.startswith("进度解锁提醒列表"):  # 进度解锁提醒列表：本群解锁前提醒（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_PROGRESS_NOTIFY, "进度解锁提醒列表"):
                return
            await self.cmd_progress_unlock_list(message, text, gid)
            return
        if low.startswith("取消进度解锁提醒"):  # 取消进度解锁提醒 <序号> <boss名>（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_PROGRESS_NOTIFY, "取消进度解锁提醒"):
                return
            await self.cmd_progress_unlock_cancel(message, text, gid)
            return
        if low.startswith("进度解锁提醒"):  # 进度解锁提醒 <序号> <boss名> <分钟>：解锁前提醒本群（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_PROGRESS_NOTIFY, "进度解锁提醒"):
                return
            await self.cmd_progress_unlock(message, text, gid, user_openid)
            return
        # ── 进度提醒（Boss 首杀播报；管理员及以上）──
        # 分发顺序证据：「进度提醒列表」必须先于「进度提醒」判断（前者以后者为前缀）。
        if low.startswith("进度提醒列表"):  # 进度提醒列表：查看本群已设定的提醒（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_PROGRESS_NOTIFY, "进度提醒列表"):
                return
            await self.cmd_progress_notify_list(message, text, gid)
            return
        if low.startswith("取消进度提醒"):  # 取消进度提醒 <序号> <boss名>（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_PROGRESS_NOTIFY, "取消进度提醒"):
                return
            await self.cmd_progress_cancel(message, text, gid)
            return
        if low.startswith("取消"):  # 取消 <玩家名>：撤销本人的待批准设备登录请求（所有人可用）
            await self.cmd_cancel_login(message, text, gid)
            return
        if low.startswith("进度提醒"):  # 进度提醒 <序号> <boss名>：该 boss 首杀时播报到本群（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_PROGRESS_NOTIFY, "进度提醒"):
                return
            await self.cmd_progress_notify(message, text, gid, user_openid)
            return
        if low.startswith("进度查询"):  # 进度查询 <序号>：把服务器进度卡发到本群（所有人可用，成功不回复确认）
            await self.cmd_progress_query(message, text, gid, user_openid)
            return

        # 绑定 <QQ号>：与"绑定邮箱"同一实现（免输入 @ 符号，避开平台过滤）
        if low.startswith("绑定"):
            await self.cmd_bind_email(message, text, gid)
            return
            return
        if low.startswith("添加白名单"):  # 添加白名单 <进服玩家名> <验证码>
            await self.cmd_add_whitelist(message, text, gid)
            return
        if low.startswith("修改白名单"):  # 修改白名单 <新玩家名>：改名（不迁移存档；48 小时限一次；需重新确认登录）
            await self.cmd_rename_whitelist(message, text, gid)
            return
        if low.startswith(("邮箱改绑", "改绑邮箱")):  # 邮箱改绑 <新邮箱>：改绑绑定邮箱（7 天限一次；24 小时内完成，否则回滚）
            await self.cmd_change_email(message, text, gid)
            return
        if low.startswith("邮箱上限重置"):  # 邮箱上限重置 <QQ号>（管理员及以上）
            if not await self._perm_ok(message, gid, user_openid, PERM_MAIL_RESET, "邮箱上限重置"):
                return
            await self.cmd_mail_limit_reset(message, text, gid, user_openid)
            return
        if low.startswith("登录"):  # 登录 [玩家名]：批准换设备登录
            await self.cmd_login(message, text, gid)
            return
        if low.startswith("清空设备"):  # 清空设备 [玩家名]：清空本人已登录设备（下次进服重新登录）；联合区共用
            await self.cmd_clear_devices(message, text, gid)
            return
        if low.startswith(("玩家查询", "信息查询")):  # 已并入「我的信息」
            await self.cmd_my_points(message, text, gid, user_openid)
            return

        # ── GitHub ──
        if text in ("仓库", "repo", "github", "状态", "star", "数据", "查看仓库"):
            await self.reply_repo_overview(message)
        elif low in ("/pr list", "/pr", "pr list", "拉取请求列表", "pr", "pull", "拉取", "更新", "mr"):
            await self.reply_gh_activity(message, kind="pr")
        elif low in ("/issue list", "/issue", "issue list", "议题列表", "issue", "问题", "议题"):
            await self.reply_gh_activity(message, kind="issue")

    async def _capture_group(self, message):
        """从任意群消息捕获 group_openid 并接入监控"""
        group_openid = message.group_openid
        _log.info("收到群消息 group_openid=%s content=%s", group_openid, message.content)
        # 补发入群引导卡：加群时主动消息权限未开导致卡片没发出去的群，能在通信后自动补发一次（用 msg_id 被动回复）
        last_guide = self._guide_pending.get(group_openid)
        if last_guide is not None and time.time() - last_guide > 60:
            self._guide_pending[group_openid] = time.time()
            try:
                await self._send_add_robot_guide(
                    group_openid, "",
                    reply_to={"msg_id": getattr(message, "id", None)},
                )
            except Exception as e:
                _log.error("补发入群引导卡异常: %s", e)
        if group_openid and group_openid not in self.groups.values():
            self.groups[f"群({group_openid[:8]}...)"] = group_openid
            _log.warning("!!! 已自动监控群，可将 group_openid 固化到 config.yaml: %s", group_openid)
            # 立刻检查一次该群现有的入群申请
            try:
                await self.check_one_group("自动捕获群", group_openid)
            except Exception as e:
                _log.exception("首次检查群申请失败: %s", e)

    async def on_group_msg_reject(self, event: GroupManageEvent):
        """群里关闭了机器人主动消息，会导致审核卡片发不出去"""
        _log.warning("群 %s 关闭了机器人主动消息！请在机器人群资料页重新开启", event.group_openid)

    async def on_group_member_add(self, event: GroupMemberEvent):
        """群成员入群事件（官方 GROUP_MEMBER_ADD，INTENT 1<<24）：发送欢迎卡片"""
        group_openid = event.group_openid
        member_openid = event.member_openid or ""
        _log.info("新成员入群 group=%s member=%s", group_openid, member_openid)
        if not group_openid:
            return
        # 重新入群：自动解冻其退群时被冻结的白名单
        unfrozen = self.whitelist_store.unfreeze_by_openid(self._eff_gid(group_openid), member_openid)
        if unfrozen:
            _log.info("已自动解冻 %s 的 %s 条白名单（重新入群）", member_openid, unfrozen)
        # 喵币：回群解冻（放在白名单解冻之后，确保归属判定可用）
        self.economy.freeze(member_openid, False)
        # 群聊 @ 用户：最新格式 <qqbot-at-user id="" />（旧格式 <@userid> 即将弃用）
        at_tag = f'<qqbot-at-user id="{member_openid}" />' if member_openid else "@新成员"
        welcome_lines = [
            self.build_card_title("入群欢迎"),
            "",
            at_tag,
            "🎉欢迎加入ZSE联合体喵!",
            "发送 帮助 查看更多喵!",
        ]
        if unfrozen:
            welcome_lines.append(f"> 已自动解冻 {unfrozen} 条白名单喵（重新入群）")
        markdown = MarkdownPayload(content="\n".join(welcome_lines))
        # 帮助/关于按钮（所有人都可点，功能后续完善）
        keyboard = KeyboardPayload(
            content=Keyboard(
                rows=[
                    KeyboardRow(
                        buttons=[
                            Button(
                                id="help",
                                render_data=RenderData(label="帮助", style=1),
                                action=Action(
                                    type=1,
                                    permission=Permission(type=2),
                                    data=json.dumps({"cmd": "help"}, ensure_ascii=False),
                                ),
                            ),
                            Button(
                                id="about",
                                render_data=RenderData(label="关于", style=1),
                                action=Action(
                                    type=1,
                                    permission=Permission(type=2),
                                    data=json.dumps({"cmd": "about"}, ensure_ascii=False),
                                ),
                            ),
                        ]
                    )
                ]
            )
        )
        try:
            await self.api.post_group_message(
                group_openid=group_openid, msg_type=2, markdown=markdown, keyboard=keyboard
            )
            _log.info("已发送入群欢迎卡片")
        except Exception as e:
            _log.warning("入群欢迎卡片发送失败：%s，降级纯文本", e)
            try:
                await self.api.post_group_message(
                    group_openid=group_openid,
                    msg_type=0,
                    content=self._markdown_to_plain(
                        "\n".join(welcome_lines), at_tag,
                        f"@{self._disp(group_openid, member_openid)}" if member_openid else "@新成员"),
                )
            except Exception as e2:
                _log.error("入群欢迎纯文本发送也失败: %s", e2)

    async def on_group_member_remove(self, event: GroupMemberEvent):
        """群成员退群事件（官方 GROUP_MEMBER_REMOVE，INTENT 1<<24）
        - 若其绑定过白名单 → 自动冻结（被冻结玩家不能进服，重新入群自动解冻）
        - 若身负身份 → 移除；若有高级管理员退群 → 按 master→admin 顺序自动接任为高级管理员"""
        _log.info("群成员退出群聊 group=%s member=%s", event.group_openid, event.member_openid)
        group_openid = event.group_openid
        if not group_openid:
            return
        mid = event.member_openid or ""
        # 喵币：钱包按 openid 一人一份 → 直接冻结这个人
        self.economy.freeze(mid, True)
        frozen_cnt = self.whitelist_store.freeze_by_openid(self._eff_gid(group_openid), mid)
        if frozen_cnt:
            _log.info("已冻结 %s 的 %s 条白名单（退群）", mid, frozen_cnt)
        # 身份清理 + 高级管理员退群接管
        was_owner = bool(mid) and self.perms.is_owner(self._eff_gid(group_openid), mid)
        self.perms.remove_member(self._eff_gid(group_openid), mid)
        takeover_line = ""
        if was_owner:
            new_owner = None
            candidates = (self.perms.members_of_role(self._eff_gid(group_openid), MASTER)
                          or self.perms.members_of_role(self._eff_gid(group_openid), ADMIN))
            if candidates:
                new_owner = candidates[0]
                self.perms.set_first_owner(self._eff_gid(group_openid), new_owner, who=mid, note="高级管理员退群，自动接任")
                takeover_line = f"高级管理员退群，已由 `{self._disp(self._eff_gid(group_openid), new_owner)}` 自动接任为高级管理员"
            else:
                takeover_line = "高级管理员退群且无其他管理身份，本群暂无高级管理员（可用 `设置高级管理员 <玩家名>` 追授）"
        # 尝试获取成员资料（昵称/头像）：官方接口需"群成员获取"权限，未开通时这里返回 None；头像改用按 openid 直连 qlogo（无需该权限）
        info = await self.get_group_member(group_openid, mid)
        username = (info or {}).get("username") or ""
        avatar_url = (info or {}).get("avatar") or ""
        at_tag = f'<qqbot-at-user id="{mid}" />' if mid else ""
        avatar_md = self._openid_avatar_md(mid) or (f"![头像 #20px #20px]({avatar_url})" if avatar_url else "")

        user_line = "用户: " + (f"{at_tag} {avatar_md}" if avatar_md else (at_tag or username or "未知")).strip()
        frozen_line = f"> 已自动冻结其白名单（{frozen_cnt} 条，重新入群自动解冻，冻结期间无法进服）" if frozen_cnt else ""
        markdown_lines = [self.build_card_title("退群事件")]
        markdown_lines.append(user_line)
        markdown_lines.append("该成员已离开本群聊喵...")
        if frozen_line:
            markdown_lines.append(frozen_line)
        if takeover_line:
            markdown_lines.append(f"> {takeover_line}")
        markdown_lines.append("---")
        markdown_lines.append(f"> 用户id：{mid}")
        markdown = MarkdownPayload(content="\n".join(markdown_lines))
        plain_user = at_tag or username or mid
        try:
            await self.api.post_group_message(group_openid=group_openid, msg_type=2, markdown=markdown)
            _log.info("已发送退群通知")
        except Exception as e:
            _log.warning("退群卡片发送失败(可能无权限)：%s，降级纯文本", e)
            try:
                await self.api.post_group_message(
                    group_openid=group_openid,
                    msg_type=0,
                    content=(
                        f"{self.build_card_title('退群事件')}\n"
                        f"用户: {plain_user}\n"
                        f"该成员已离开本群聊喵...\n"
                        + (f"> 已自动冻结其白名单（{frozen_cnt} 条，重新入群自动解冻）\n" if frozen_cnt else "")
                        + (f"> {takeover_line}\n" if takeover_line else "")
                        + f"---\n> 用户id：{mid}"
                    ),
                )
            except Exception as e2:
                _log.error("退群纯文本发送也失败: %s", e2)

    # ───────────────────────── 统一模板 ─────────────────────────
    def build_card_title(self, event_name: str) -> str:
        """所有发言模板统一标题：꧁༺ {apply_title} 【事件名】 ༻꧂"""
        return f"## ꧁༺ {self.apply_title} {event_name} ༻꧂"

    def _about_card(self, gid: str, user_openid: str) -> str:
        """关于卡片：指令（关于）与入群指引卡"关于"按钮共用同一模板。
        信息区的 GroupID / 用户id 按当前群与当前用户动态填充；"Powered By"用粗斜体，分割线用浅灰细线(***)"""
        return "\n".join([
            "## ꧁༺ 关于 ༻꧂",
            "",
            "### 🚀Zykor StarBot",
            "- 🌟开发者:星梦",
            "- 🗿贡献者:",
            "大肥鱼🐳 v4.1falsh[吃白饭和减少工作量]",
            "CaibotLite[抄爽了🥰]",
            "帕秋莉Bot[模板参考与功能借鉴:)]",
            "七青[背景图提供]",
            "小触须团子十号[背景图提供]",
            "",
            "***Powered By Zykor-Club***",
            "",
            "***",
            "> 信息",
            f"> GroupID:{gid or '未知'}",
            f"> 用户id:{user_openid or '未知'}",
        ])

    # ───────────────────────── 帮助指令（分类卡片 + 分类按钮） ─────────────────────────
    def _help_rank(self, gid, user_openid: str) -> int:
        """当前用户在（联合区有效群）中的身份等级"""
        return self.perms.rank_of(self._eff_gid(gid), user_openid or "")

    def _help_index_card(self, gid, user_openid: str) -> str:
        return help_content.render_index(self._help_rank(gid, user_openid), self.build_card_title)

    def _help_category_card(self, key: str, gid, user_openid: str) -> str:
        return help_content.render_category(key, self._help_rank(gid, user_openid), self.build_card_title)

    @staticmethod
    def _help_keyboard(rank: int = None) -> KeyboardPayload:
        """分类按钮：type=2「指令按钮」，点击即发送 `帮助 <分类>`（无需回调处理，对齐 CaiBotLite）
        传 rank 时隐藏该身份整类都无权限的分类按钮"""
        rows = []
        for cats in help_content.keyboard_layout(rank):
            rows.append(KeyboardRow(buttons=[
                Button(
                    id=f"help_{c['key']}",
                    render_data=RenderData(label=f"{c['emoji']} {c['title']}", style=1),
                    action=Action(
                        type=2,
                        permission=Permission(type=2),
                        data=f"帮助 {c['title']}",
                    ),
                )
                for c in cats
            ]))
        return KeyboardPayload(content=Keyboard(rows=rows))

    async def _reply_markdown_kb(self, message, content: str, keyboard=None):
        """Markdown（可带键盘）发送；失败降级纯文本（命令标签转纯命令）"""
        uid = self._user_openid(message)
        content = self._insert_executor_at(content, uid)
        kwargs = {
            "group_openid": message.group_openid, "msg_type": 2,
            "msg_id": message.id,
            "markdown": MarkdownPayload(content=content),
        }
        if keyboard:
            kwargs["keyboard"] = keyboard
        try:
            await self.api.post_group_message(**kwargs)
        except Exception as e:
            _log.warning("卡片(带键盘)发送失败，降级纯文本: %s", e)
            at_tag = f'<qqbot-at-user id="{uid}" />' if uid else ""
            await self._reply_text(message, self._markdown_to_plain(
                content, at_tag, f"@{self._disp(message.group_openid, uid)}" if uid else ""))

    async def _reply_help(self, message, content: str, with_keyboard: bool = True, rank: int = None):
        """帮助卡片发送；with_keyboard=False 时只发 markdown（分类卡不带切换按钮）"""
        await self._reply_markdown_kb(
            message, content, self._help_keyboard(rank) if with_keyboard else None)

    async def cmd_help(self, message, text: str, gid, user_openid: str = ""):
        """帮助 [分类]：无参数=帮助卡片（带分类按钮）；带分类=该分类指令清单"""
        parts = (text or "").split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        rank = self._help_rank(gid, user_openid)
        if not arg:
            await self._reply_help(message, self._help_index_card(gid, user_openid),
                                   rank=rank)
            return
        cat = help_content.find_category(arg)
        if cat is None:
            await self._reply_help(
                message,
                "\n".join([
                    self.build_card_title("帮助"),
                    "",
                    f"### ⚠️ 没有找到分类「{arg}」喵",
                    "可用分类：" + "、".join(
                        f"{c['emoji']}{c['title']}" for c in help_content.CATEGORIES),
                ]),
                rank=rank,
            )
            return
        # 分类卡不带分类切换按钮（只有主卡带）
        await self._reply_help(message, self._help_category_card(cat["key"], gid, user_openid),
                               with_keyboard=False)

    # ───────────────────────── GitHub 消息推送 ─────────────────────────
    async def github_poll(self):
        """定时检测仓库新 Issue / PR，发现新动态主动推送到所有监控群"""
        interval = int(self.github_cfg.get("poll_interval_minutes", 10) or 10) * 60
        _log.info("GitHub 轮询启动，间隔 %s 分钟", interval // 60)
        fails = 0
        # 先跑一次建立基线（不推送），再进入循环；首跑失败不能让整个轮询任务退出。
        # 网络类失败（GitHub 从服务器常不可达）只记一行并指数退避，不打整页堆栈刷屏。
        try:
            await self.check_github(push_initial=False)
        except (asyncio.TimeoutError, OSError) as e:
            fails = 1
            _log.warning("GitHub 查询失败（首次，%s）：%s", type(e).__name__, e)
        except Exception as e:
            fails = 1
            _log.exception("GitHub 轮询首跑失败（后续按周期重试）: %s", e)
        while True:
            delay = interval if not fails else min(interval * (2 ** min(fails, 3)), interval * 8)
            await asyncio.sleep(delay)
            try:
                await self.check_github(push_initial=True)
                if fails:
                    _log.info("GitHub 轮询已恢复正常")
                fails = 0
            except (asyncio.TimeoutError, OSError) as e:
                fails += 1
                back = min(interval * (2 ** min(fails, 3)), interval * 8)
                _log.warning("GitHub 查询失败（连续 %d 次，%s），%d 秒后重试：%s",
                             fails, type(e).__name__, back, e)
            except Exception as e:
                fails += 1
                _log.exception("GitHub 轮询出错: %s", e)

    async def check_github(self, push_initial: bool = True):
        """拉取最新 PR / Issue / star，对比上次快照，有新内容则推送"""
        owner = self.github_cfg.get("owner")
        repo = self.github_cfg.get("repo")
        if not owner or not repo:
            return
        full = f"{owner}/{repo}"
        async with aiohttp.ClientSession() as s:
            stats = await get_repo_stats(s, owner, repo)
            pulls = await get_latest_pulls(s, owner, repo, 5)
            issues = await get_latest_issues(s, owner, repo, 5)

        push = push_initial and bool(self.github_cfg.get("auto_push", True))

        # ── 新 PR ──
        max_pr = max((p["number"] for p in pulls), default=0)
        base_pr = self._gh_last_pr.get(full)
        if push and base_pr is not None and max_pr > base_pr:
            for p in pulls:
                if p["number"] > base_pr:
                    await self.push_gh_card("新的拉取请求", p, full)
        self._gh_last_pr[full] = max_pr

        # ── 新 Issue ──
        max_issue = max((i["number"] for i in issues), default=0)
        base_issue = self._gh_last_issue.get(full)
        if push and base_issue is not None and max_issue > base_issue:
            for i in issues:
                if i["number"] > base_issue:
                    await self.push_gh_card("新的Issue", i, full)
        self._gh_last_issue[full] = max_issue

        # ── 新 Star ──
        await self.check_star(push, full, stats.get("stars", 0))

    async def check_star(self, push: bool, full: str, count: int):
        """
        检测 star 增量并推送。
        count 增加说明有新 star；若要显示 star 用户名字，需配置 github.token
        （2026-07 起 stargazers 列表接口强制鉴权），否则显示为"未知用户"。
        """
        base = self._gh_last_star.get(full)
        new_name = None
        if count > 0 and base is not None and count > base["count"]:
            # 尝试拿最新的 star 用户（无 token 会失败，静默降级）
            owner, repo = full.split("/", 1)
            try:
                async with aiohttp.ClientSession() as s:
                    stars = await get_repo_stargazers(s, owner, repo, 10)
                # 接口按时间正序，取最后一条（最新）且非 base 已有
                if stars:
                    new_name = stars[-1]["login"]
            except Exception as e:
                _log.warning("获取 star 用户名失败（无 Token？）：%s", e)
            if push:
                await self.push_star_card(full, count, new_name)
        self._gh_last_star[full] = {"count": count}

    async def push_star_card(self, full_name: str, count: int, username=None):
        """推送"新增 Star"卡片图：仓库新增来自【xxx】的star喵！当前共【N】颗"""
        who = username or "未知用户"
        rows = [
            ("来 源", f"@{who}"),
            ("仓库", full_name),
            ("Star 数", f"{count} 颗"),
        ]
        image = self._render_card_bytes("新的Star", rows, subtitle=full_name)
        for group_name, group_openid in self.groups.items():
            try:
                await send_group_image(self, group_openid, image)
                _log.info("[%s] 已推送新Star卡片: %s → %s", group_name, who, count)
            except Exception as e:
                _log.warning("[%s] 推送Star失败: %s", group_name, e)

    async def push_gh_card(self, title: str, item: dict, full_name: str):
        """把单条 PR / Issue 渲染成卡片图并推送到所有监控群"""
        kind = "PR" if "merged_at" in item else "Issue"
        state_desc = {
            "open": "开启中",
            "closed": "已关闭",
            "merged": "已合并",
        }.get("merged" if item.get("merged_at") else item.get("state"), item.get("state"))
        rows = [
            ("序号", f"#{item['number']}"),
            ("发起者", item.get("user", "?")),
            ("仓库", full_name),
            ("标题", item.get("title", "")),
            ("状态", state_desc),
        ]
        image = self._render_card_bytes(title, rows, subtitle=full_name)
        for group_name, group_openid in self.groups.items():
            try:
                await send_group_image(self, group_openid, image)
                _log.info("[%s] 已推送%s卡片: #%s", group_name, kind, item["number"])
            except Exception as e:
                _log.warning("[%s] 推送%s失败: %s", group_name, kind, e)

    async def reply_repo_overview(self, message):
        """@机器人 发"仓库/状态"等：生成仓库综述图（star/fork/issue + 组织仓库）"""
        owner = self.github_cfg.get("owner")
        repo = self.github_cfg.get("repo")
        if not owner or not repo:
            await self.api.post_group_message(
                group_openid=message.group_openid, msg_type=0, msg_id=message.id,
                content="GitHub 监控未在 config.yaml 中配置 (owner/repo)",
            )
            return
        try:
            async with aiohttp.ClientSession() as s:
                stats = await get_repo_stats(s, owner, repo)
                pulls = await get_latest_pulls(s, owner, repo, 3)
                issues = await get_latest_issues(s, owner, repo, 3)
                org_repos = await get_org_repos(s, self.github_cfg.get("org")) if self.github_cfg.get("org") else []
        except Exception as e:
            _log.exception("拉取仓库数据失败")
            await self.api.post_group_message(
                group_openid=message.group_openid, msg_type=0, msg_id=message.id,
                content=f"⚠️ 获取 GitHub 数据失败：{e}",
            )
            return

        rows = [
            ("仓库", stats["full_name"]),
            ("描述", stats["description"] or "无"),
            ("Star", str(stats["stars"])),
            ("Fork", str(stats["forks"])),
            ("Issue", str(stats["open_issues"])),
            ("语言", stats["language"]),
            ("最近推送", (stats["pushed_at"] or "")[:10]),
        ]
        if org_repos:
            org_line = "、".join(f"{r['name']}(★{r['stars']})" for r in org_repos[:5])
            rows.append(("组织仓库", org_line))
        if pulls:
            rows.append(("最新PR", f"#{pulls[0]['number']} {pulls[0]['title']} @{pulls[0]['user']}"))
        if issues:
            rows.append(("最新Issue", f"#{issues[0]['number']} {issues[0]['title']} @{issues[0]['user']}"))

        image = self._render_card_bytes("仓库动态总览", rows, subtitle=stats["full_name"])
        await send_group_image(self, message.group_openid, image)

    async def reply_gh_activity(self, message, kind: str):
        """@机器人 发"PR/Issue"：最新列表卡片图"""
        owner = self.github_cfg.get("owner")
        repo = self.github_cfg.get("repo")
        if not owner or not repo:
            await self.api.post_group_message(
                group_openid=message.group_openid, msg_type=0, msg_id=message.id,
                content="GitHub 监控未在 config.yaml 中配置 (owner/repo)",
            )
            return
        try:
            async with aiohttp.ClientSession() as s:
                items = (
                    await get_latest_pulls(s, owner, repo, 5)
                    if kind == "pr"
                    else await get_latest_issues(s, owner, repo, 5)
                )
        except Exception as e:
            _log.exception("拉取 %s 失败", kind)
            await self.api.post_group_message(
                group_openid=message.group_openid, msg_type=0, msg_id=message.id,
                content=f"⚠️ 获取 GitHub 数据失败：{e}",
            )
            return
        title = "最新拉取请求" if kind == "pr" else "最新Issue"
        rows = []
        for it in items:
            state = "已合并" if it.get("merged_at") else ("开启" if it["state"] == "open" else "已关闭")
            rows.append((f"#{it['number']} {state}", f"{it['title']}\n@{it['user']}"))
        if not rows:
            rows.append(("暂无", "还没有任何记录"))
        image = self._render_card_bytes(title, rows, subtitle=f"{owner}/{repo}")
        await send_group_image(self, message.group_openid, image)

    def _render_card_bytes(self, title: str, rows, subtitle: str = None) -> bytes:
        """渲染卡片图为 PNG bytes（临时文件中转）"""
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            render_card(title, rows, tmp, subtitle=subtitle)
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # ───────────────────────── 通用回复工具 ─────────────────────────
    async def _reply_text(self, message, content: str):
        await self.api.post_group_message(
            group_openid=message.group_openid, msg_type=0, msg_id=message.id, content=content
        )

    async def _reply_markdown(self, message, content: str):
        """优先 Markdown 卡片发送（msg_type=2），失败自动降级为纯文本；卡片统一 @ 执行指令的用户（放在标题下方）"""
        await self._post_markdown(message, content, at_executor=True)

    def _insert_executor_at(self, content: str, uid: str) -> str:
        """把 @ 执行人插入到卡片标题（首行 ## 标题）下方；无 openid 或内容已含该 @ 则原样返回"""
        at = f'<qqbot-at-user id="{uid}" />'
        if not uid or f'id="{uid}"' in content:
            return content
        lines = content.split("\n")
        first = lines[0] if lines else ""
        if first.strip().startswith("##"):
            lines = [first, "", at] + lines[1:]
        else:
            lines = [at] + lines
        return "\n".join(lines)

    @staticmethod
    def _markdown_to_plain(content: str, at_tag: str = "", at_text: str = "") -> str:
        """Markdown → 纯文本降级：去掉行首标题标记（#/##/###）、图片语法、反引号与粗体；
        at 标签转为 @ 文本；命令标签（qqbot-cmd-input / cmd-enter）转为纯命令文本"""
        plain = re.sub(r"^#{1,6} ", "", content, flags=re.M)
        plain = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", plain)
        plain = re.sub(r"<qqbot-cmd-input\b[^>]*/>",
                       lambda m: (re.search(r'text="([^"]*)"', m.group(0)) or [None, ""])[1], plain)
        plain = re.sub(r"<qqbot-cmd-enter\b[^>]*/>", "", plain)
        plain = plain.replace("`", "").replace("**", "")
        if at_tag:
            plain = plain.replace(at_tag, at_text or "")
        return plain

    async def _post_markdown(self, message, content: str, at_executor: bool = True):
        """Markdown 发送（msg_type=2），失败自动降级纯文本；at_executor=True 时在标题下插入 @ 执行人"""
        uid = self._user_openid(message) if at_executor else ""
        if at_executor:
            content = self._insert_executor_at(content, uid)
        try:
            markdown = MarkdownPayload(content=content)
            await self.api.post_group_message(
                group_openid=message.group_openid,
                msg_type=2,
                msg_id=message.id,
                markdown=markdown,
            )
        except Exception as e:
            _log.warning("Markdown 发送失败，降级纯文本: %s", e)
            at_tag = f'<qqbot-at-user id="{uid}" />' if uid else ""
            await self._reply_text(
                message, self._markdown_to_plain(
                    content, at_tag, f"@{self._disp(message.group_openid, uid)}" if uid else ""))

    async def _reply_at_then_card(self, message, target_openid: str, card_content: str):
        """在 Markdown 卡片内嵌真实 @（群@新协议 <qqbot-at-user id="openid"/>，markdown 消息支持），再发送卡片。

        <@userid> 旧联盟格式已弃用不解析；新格式 id 为群成员 member_openid。
        target_openid 为空时原样发送（卡片首行保留 @名字 文本）。"""
        if target_openid:
            at_tag = f'<qqbot-at-user id="{target_openid}" />'
            card_content = re.sub(r"@\S+", at_tag, card_content, count=1)
        await self._reply_markdown(message, card_content)

    # ───────────────────────── TShock 服务器管理（starZSEbot 协议）─────────────────────────
    async def cmd_add_server(self, message, text: str, gid, user_openid: str = ""):
        """添加服务器 <ip/域名> <端口> <绑定码>：登记绑定码，等待插件认领（记录添加者归属）"""
        parts = text.split()
        if len(parts) < 4 or not (parts[2].isdigit() and 1 <= int(parts[2]) <= 65535) \
                or not (parts[3].isdigit() and len(parts[3]) == 6):
            await self._reply_markdown(
                message,
                "## ꧁༺ 服务器添加 ༻꧂\n\n"
                "**缺少参数或格式错误喵...**\n\n"
                "格式：`添加服务器 <ip/域名> <端口> <六位数绑定码>`\n\n"
                "> 请按照以上格式重新添加一个服务器喵",
            )
            return
        ip, port_s, code = parts[1], parts[2], parts[3]
        ok, msg = await self.zse_server.register(gid, ip, int(port_s), code, added_by=user_openid)
        if ok:
            other_ids = self._zone_other_ids(gid)
            await self._reply_markdown(
                message,
                "## ꧁༺ 服务器添加 ༻꧂\n\n"
                "**执行添加成功喵!**\n\n"
                f"请确认您添加的服务器绑定码为: `{code}`\n"
                + (f"> 联合区内 {other_ids} 将自动可见该服务器喵\n" if other_ids else "")
                + "\n> 如果服务器不对，请删除已添加的服务器并重新添加喵!",
            )
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 服务器添加 ༻꧂\n\n"
                f"**❌ {msg}**",
            )

    # 中文序号：数量不多够用
    _CN_NUM = ["零", "壹", "贰", "叁", "肆", "伍", "陆", "柒", "捌", "玖", "拾"]

    @classmethod
    def _cn_seq(cls, n: int) -> str:
        return cls._CN_NUM[n] if 0 <= n < len(cls._CN_NUM) else str(n)

    async def cmd_online(self, message, gid):
        """在线：向本群所有已连接插件发 player_list + progress 请求，按模板汇总显示"""
        servers = self.zse_server.visible_records(gid)
        if not servers:
            await self._reply_markdown(
                message,
                "## ꧁༺ 服务器在线状态 ༻꧂\n\n"
                "**本群还没添加服务器喵...**\n\n"
                "格式：`添加服务器 <ip/域名> <端口> <六位数绑定码>`\n"
                "> 添加后可发送 `在线` 查看服务器状态",
            )
            return
        results = await self.zse_server.query_online(gid)
        blocks = ["## ꧁༺ 服务器在线状态 ༻꧂", ""]
        for idx, (rec, status, info) in enumerate(results, 1):
            cn = self._cn_seq(idx)
            name = rec.get("server_name") or "未知名称"
            if status == "ok":
                proc = info.get("process") or {}
                if isinstance(proc, dict) and proc:
                    done = sum(1 for v in proc.values() if v)
                    grade = "已毕业" if done == len(proc) else f"进度 {done}/{len(proc)}"
                else:
                    grade = info.get("world_icon") or "未知进度"
                blocks.append(f"### ☬{cn} ⚡{name} 「{grade}」")
                current = info.get("current_online") or 0
                players = info.get("player_list") or []
                if current == 0 or not players:
                    blocks.append("服务器没落了喵...")
                elif not self.perms.show_online_players(self._eff_gid(gid)):
                    # 群设置了"不显示在线玩家"：只显示在线数，隐藏名单（隐私/防刷屏）
                    blocks.append(f"（{current} 人在线，玩家列表已隐藏）")
                else:
                    # 玩家名带头像（仅白名单内玩家能反查到 QQ 显示头像；未启用白名单进服的玩家不显示）
                    chips = [f"{self._avatar_md(self._eff_gid(gid), p)}{p}" for p in players]
                    blocks.append("，".join(chips))
            elif status == "timeout":
                blocks.append(f"### ☬{cn} ⚡{name}")
                blocks.append("⚠️服务器被超时了喵...")
            else:  # offline
                blocks.append(f"### ☬{cn} ⚡{name}")
                blocks.append("🔴 链接不到服务器喵，可能是服务器在火星喵...")
            blocks.append("")
        await self._reply_markdown(message, "\n".join(blocks).rstrip("\n"))

    async def cmd_del_server(self, message, text: str, gid, user_openid: str = ""):
        """删除服务器 <序号>：通知插件解绑并移除登记。
        序号 = “服务器列表”里显示的号；其它群的服务器不能删（提示归属群）；
        删除时，成功卡会说明联合区内哪些群将不再可见。
        归属校验：owner 可删任意；master 只能删自己添加的（added_by==本人 openid）。"""
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await self._reply_markdown(
                message,
                "## ꧁༺ 删除服务器 ༻꧂\n\n"
                "**参数格式错误喵...**\n\n"
                "格式：`删除服务器 <序号>`\n\n"
                "> 序号请在“服务器列表”中查看喵",
            )
            return
        seq = int(parts[1])
        rec = self.zse_server.record_by_seq(gid, seq)
        if rec is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ 删除服务器 ༻꧂\n\n"
                f"**❌ 没有序号 {seq} 的服务器喵**",
            )
            return
        # 其它群的服务器不能在本群删除（仅归属群可操作，提示归属群）
        owner_gid = rec.get("owner_gid") or gid
        if owner_gid != gid:
            oid = self.registry.join_id_of(owner_gid) or (owner_gid[:8] + "…")
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 删除服务器 ༻꧂",
                    "",
                    f"**❌ 该服务器属于群ID `{oid}`，仅归属群可删除喵**",
                    "",
                    "> 如需移除，请到归属群执行删除（删除后联合区内都将不再可见）",
                ]),
            )
            return
        role = self.perms.role_of(self._eff_gid(gid), user_openid)
        if role != OWNER and rec.get("added_by") != user_openid:
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 删除服务器 ༻꧂",
                    "",
                    "**❌ 您只能删除自己添加的服务器喵**",
                    "",
                    f"> 服务器：`{rec.get('server_name') or rec.get('ip')}:{rec.get('port')}`",
                    "> 如需删除他人添加的服务器，请联系高级管理员处理",
                ]),
            )
            return
        # 同联合区其它群（网状可见）：删除后这些群都将看不到
        others_ids = self._zone_other_ids(gid)
        ok, msg = await self.zse_server.unregister(gid, rec.get("seq"))
        if ok:
            # 该服务器已不存在 → 联合区内所有群的进度提醒/解锁提醒一并清理（避免僵尸订阅）
            cleaned = self.progress_notifies.remove_server(rec.get("code") or "")
            cleaned_u = self.progress_unlocks.remove_server(rec.get("code") or "")
            cleaned_v = self.votes.remove_server(rec.get("code") or "")
            extra = ""
            if others_ids:
                extra = f"\n\n> 联合区内 {others_ids} 也能看到该服务器，删除后这些群都将不再可见喵"
            if cleaned or cleaned_u or cleaned_v:
                extra += (f"\n> 已同步清理该服务器的进度提醒 {cleaned} 条、"
                          f"进度解锁提醒 {cleaned_u} 条、种子投票 {cleaned_v} 场（联合区内所有群）")
            await self._reply_markdown(
                message,
                "## ꧁༺ 删除服务器 ༻꧂\n\n"
                "💾 **已从本群踢出一个服务器喵！**" + extra,
            )
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 删除服务器 ༻꧂\n\n"
                f"**❌ {msg}**",
            )

    async def cmd_server_list(self, message, gid):
        """服务器列表：按模板展示登记的所有服务器"""
        servers = self.zse_server.visible_records(gid)
        if not servers:
            await self._reply_markdown(
                message,
                "## ꧁༺ 服务器列表 ༻꧂\n\n"
                "**本群还没添加服务器喵...**\n\n"
                "格式：`添加服务器 <ip/域名> <端口> <六位数绑定码>`\n"
                "> 添加后可发送 `服务器列表` 查看",
            )
            return
        blocks = ["## ꧁༺ 服务器列表 ༻꧂", ""]
        eff = self._eff_gid(gid)
        for idx, rec in enumerate(servers, 1):
            cn = self._cn_seq(idx)
            name = rec.get("server_name") or "未认领"
            ver = rec.get("version") or ""
            ver_tag = f" 【{ver}】" if ver else ""
            if rec.get("bound") and rec.get("online"):
                state = "🟢 在线"
            else:
                state = "🔴 离线"
            blocks.append(f"### ✵{cn}✵ **{name}**{ver_tag}｜{state}")
            blocks.append(f"- »地址: `{rec['ip']}`")
            blocks.append(f"- »端口: `{rec['port']}`")
            # 添加者：按 openid 直连 qlogo 头像（不要求绑过白名单）；名字按总群白名单反查，查不到用 openid 前缀
            adder = rec.get("added_by") or ""
            add_name = self.whitelist_store.find_by_openid(eff, adder) if adder else ""
            add_disp = add_name or (f"{adder[:8]}…" if adder else "未知用户")
            av_add = self._openid_avatar_md(adder)
            # 其它群的服务器（同联合区网状可见）：标注来源群
            src_gid = rec.get("rec_shared_by") or ""
            if src_gid:
                jid = self.registry.join_id_of(src_gid)
                jid_disp = f"群id：{jid}" if jid else f"群id：{src_gid[:8]}…"
                owner_line = f"> 本服务器由来自{jid_disp} 的 {av_add}{add_disp} 添加"
            else:
                # QQ 渲染器在"文字紧贴图片"时会强制图片换行，头像放行首可内联（同广播卡写法）
                owner_line = f"> {av_add} 本服务器由 {add_disp} 添加" if av_add else f"> 本服务器由 {add_disp} 添加"
            blocks.append(owner_line)
            wl = rec.get("whitelist")
            if wl is True:
                blocks.append("> 本服务器已启用白名单喵")
            elif wl is False:
                blocks.append("> 本服务器已关闭白名单喵...")
            blocks.append("")
        await self._reply_markdown(message, "\n".join(blocks).rstrip("\n"))

    async def cmd_ping(self, message, text: str):
        """ping <域名/ip> [端口]：TCP 连接测速"""
        parts = text.split()
        if len(parts) < 2:
            await self._reply_markdown(
                message,
                self.build_card_title("Ping服务器") + "\n\n"
                "**缺少参数喵...**\n\n"
                "格式：`ping <域名/ip> <端口>`\n"
                "例：`ping <服务器IP> 7777`",
            )
            return
        host = parts[1]
        port = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 7777
        # 冷却：同一 ip:port 5 分钟内只测一次（防被当成内网端口扫描器刷）
        now = time.time()
        last = _PING_LAST.get((host, port))
        if last and now - last < _PING_COOLDOWN:
            await self._reply_markdown(message, "\n".join([
                self.build_card_title("Ping服务器"), "",
                f"**⏳ 这个地址刚测过喵...**",
                f"> 请 {int(_PING_COOLDOWN - (now - last))} 秒后再试",
            ]))
            return
        _PING_LAST[(host, port)] = now
        ok, ms = await self.zse_server.ping(host, port)
        if ok:
            await self._reply_markdown(
                message,
                self.build_card_title("Ping服务器") + "\n\n"
                "💞本喵成功连接上了服务器，但被服务器一脚踢飞了喵....\n\n"
                f"本喵带回来了该服务器数据: **{ms} ms**",
            )
        else:
            await self._reply_markdown(
                message,
                self.build_card_title("Ping服务器") + "\n\n"
                "**🔴本喵连接不上服务器喵...**",
            )

    # ───────────────────────── 白名单绑定（邮箱验证）─────────────────────────
    def _group_name(self, gid: str) -> str:
        """群 openid 反查群名"""
        for name, ogid in self.groups.items():
            if ogid == gid:
                return name
        return "本群"

    async def cmd_fetch_map(self, message, text: str, gid):
        """获取地图 <服务器序列号>：直接向指定服务器插件请求生成并推送群图片（无中转提示）"""
        gid = gid or (getattr(message, "group_openid", None) or "")
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await self._reply_markdown(
                message,
                "## ꧁༺ 世界地图 ༻꧂\n\n"
                "**缺少参数或序号不对喵...**\n\n"
                "格式：`获取地图 <服务器序列号>`\n"
                "例：`获取地图 1`",
            )
            return
        seq = int(parts[1])
        try:
            results = await self.zse_server.fetch_maps(gid, seq=seq, timeout=120.0)
        except Exception as e:
            _log.exception("地图请求异常: %s", e)
            await self._reply_markdown(
                message, "## ꧁༺ 世界地图 ༻꧂\n\n" f"**❌ 请求失败：{e}**"
            )
            return
        if not results:
            await self._reply_markdown(
                message,
                "## ꧁༺ 世界地图 ༻꧂\n\n"
                f"**❌ 找不到序列号 {seq} 的服务器喵**",
            )
            return
        rec, status, info = results[0]
        name = rec.get("server_name") or (f"{rec.get('ip', '')}:{rec.get('port', '')}")
        if status == "offline":
            await self._reply_markdown(
                message,
                "## ꧁༺ 世界地图 ༻꧂\n\n"
                f"**❌ `{name}` 已离线，无法获取地图喵**",
            )
            return
        if status == "timeout":
            await self._reply_markdown(
                message,
                "## ꧁༺ 世界地图 ༻꧂\n\n"
                f"**❌ `{name}` 生成超时（服务器繁忙）喵**",
            )
            return
        compressed = (info or {}).get("base64")
        if not compressed:
            await self._reply_markdown(
                message,
                "## ꧁༺ 世界地图 ༻꧂\n\n"
                "**❌ 服务器未返回地图数据喵**",
            )
            return
        try:
            png = decode_map_png(compressed)
            # 带 msg_id 按被动回复发送（群未开"主动发言"权限时也能发图）
            await send_group_image(self, gid, png, filename="world-map.png", msg_id=message.id)
        except Exception as e:
            _log.exception("地图解压/上传失败: %s", e)
            await self._reply_markdown(
                message,
                "## ꧁༺ 世界地图 ༻꧂\n\n"
                f"**❌ 处理失败：{e}**",
            )

    async def cmd_lookbag(self, message, text: str, gid, user_openid: str = ""):
        """查背包 <服务器序号> [玩家名]：请求插件读取玩家背包（在线直读 / 离线读 SSC 角色库）并渲染成图。
        省略玩家名 = 查自己（按白名单绑定反查）；渲染或上传失败自动降级为文字卡。"""
        gid = gid or (getattr(message, "group_openid", None) or "")
        eff = self._eff_gid(gid)
        rest = ""
        for prefix in ("查背包", "查看背包", "查询背包", "背包"):
            if text.startswith(prefix):
                rest = text[len(prefix):]
                break
        seg = rest.split(None, 1)
        if not seg or not seg[0].isdigit():
            await self._reply_markdown(
                message,
                "## ꧁༺ 查询背包 ༻꧂\n\n"
                "**参数不完整喵...**\n\n"
                "格式：`查背包 <服务器序号> [玩家名]`\n"
                "例：`查背包 1`（查自己）、`查背包 1 星梦`",
            )
            return
        seq = int(seg[0])
        player = seg[1].strip() if len(seg) > 1 else ""
        if not player:  # 省略玩家名 = 查自己
            player = self.whitelist_store.find_by_openid(eff, user_openid) or ""
            if not player:
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 查询背包 ༻꧂\n\n"
                    "**❌ 您还未绑定白名单，无法反查玩家名喵**\n\n"
                    "> 可发 `绑定 <QQ号>` 按提示绑定，或指定玩家名：`查背包 <序号> <玩家名>`",
                )
                return
        elif not check_name_ok(player):
            await self._reply_markdown(
                message,
                "## ꧁༺ 查询背包 ༻꧂\n\n"
                "**❌ 玩家名不合法喵**（1~15 位，仅汉字/字母/数字/空格）",
            )
            return

        # 60 秒内同服同玩家重复查询复用上次回包（省一次 WS 往返，图片仍按当前时间重渲染）
        now = time.time()
        cache_key = (gid, seq, player)  # 带群维度：避免不同群同序号/同玩家复用串服
        cached = self._lookbag_cache.get(cache_key)
        if cached and now - cached[0] < 60:
            rec, payload = cached[1], cached[2]
        else:
            try:
                rec, status, payload = await self.zse_server.fetch_lookbag(gid, seq, player)
            except Exception as e:
                _log.exception("背包查询异常: %s", e)
                await self._reply_markdown(message, "## ꧁༺ 查询背包 ༻꧂\n\n" f"**❌ 请求失败：{e}**")
                return
            if status == "notfound":
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 查询背包 ༻꧂\n\n"
                    f"**❌ 找不到序列号 {seq} 的服务器喵**",
                )
                return
            if status == "offline":
                sname = self._server_name_plain(gid, seq) or f"序号 {seq}"
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 查询背包 ༻꧂\n\n"
                    f"**❌ `{sname}` 已离线，无法查询喵**",
                )
                return
            if status == "timeout":
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 查询背包 ༻꧂\n\n"
                    "**❌ 查询超时（服务器繁忙）喵**",
                )
                return
            self._lookbag_cache[cache_key] = (now, rec, payload)

        # exist 语义：true=有数据；false=有账号但无角色数据（SSC 未开也走这里）；0=账号不存在
        exist = payload.get("exist")
        if exist is not True:
            if exist is False and payload.get("ssc") is False:
                reason = "**❌ 当前服务器未启用 SSC，无法查询离线玩家喵**"
            elif exist is False:
                reason = "**❌ 查不到该玩家的角色数据喵**"
            else:
                reason = f"**❌ 该服务器没有名为 `{player}` 的玩家喵**"
            await self._reply_markdown(message, "## ꧁༺ 查询背包 ༻꧂\n\n" + reason)
            return

        sname = rec.get("server_name") or f"{rec.get('ip', '')}:{rec.get('port', '')}"
        querier = self._disp(eff, user_openid) if user_openid else ""
        png = None
        try:
            from lookbag_render import render_lookbag_image
            png = render_lookbag_image(payload, server_name=sname, querier=querier)
        except Exception as e:
            _log.exception("背包图渲染失败: %s", e)
        if png:
            try:
                # 带 msg_id 按被动回复发送（群未开"主动发言"权限时也能发图）
                await send_group_image(self, gid, png, filename="lookbag.png", msg_id=message.id)
                return
            except Exception as e:
                _log.exception("背包图上传失败: %s", e)
        # 降级文字卡（渲染或上传失败）
        try:
            from lookbag_render import build_text_summary
            summary = build_text_summary(payload, server_name=sname)
        except Exception as e:
            _log.exception("背包文字降级失败: %s", e)
            summary = "**❌ 背包图渲染失败，文字降级也不可用喵...**"
        await self._reply_markdown(message, "## ꧁༺ 查询背包 ༻꧂\n\n" + summary)

    # ───────────────────────── 自踢 / 插件列表 / 排行 / 下载文件 ─────────────────────────
    def _self_player_name(self, gid, user_openid: str) -> str:
        """按 openid 反查白名单玩家名（与「喊话」同一套解析）"""
        eff = self._eff_gid(gid)
        return (self.whitelist_store.find_by_openid(eff, user_openid)
                or self.whitelist_store.claim_single(eff, user_openid) or "")

    async def cmd_self_kick(self, message, text: str, gid, user_openid: str = ""):
        """自踢：把白名单绑定的角色名断开发到联合区内所有在线服务器（所有人可用）"""
        gid = gid or (getattr(message, "group_openid", None) or "")
        name = self._self_player_name(gid, user_openid)
        if not name:
            await self._reply_markdown(message, "\n".join([
                self.build_card_title("自踢"), "",
                "**❌ 您还未绑定白名单，无法自踢喵...**", "",
                "> 先发 `绑定 <QQ号>` 按提示添加白名单",
            ]))
            return
        sent = await self.zse_server.send_self_kick(gid, name)
        await self._reply_at_then_card(message, user_openid, "\n".join([
            self.build_card_title("自踢"), "",
            f"### ✅ 已请求断开 `{name}` 的连接",
            f"> 已发送到 **{sent}** 个在线服务器｜仅当角色在线时才会被踢出",
        ]))

    @staticmethod
    def _plugin_rows(plugins) -> list:
        """插件列表 -> 展示行（`- 名称 | 作者 | 描述 | v版本`），按名称排序。

        插件回包字段是 PascalCase（C# PluginInfo 的字段名），这里兼容小写写法。
        """
        def field(p, key):
            return _md_safe(p.get(key) or p.get(key.lower()) or "")

        return [
            f"- {field(p, 'Name')} | {field(p, 'Author')} | {field(p, 'Description')} | v{field(p, 'Version')}"
            for p in sorted(plugins or [], key=lambda p: field(p, "Name").lower())
        ]

    async def cmd_plugin_list(self, message, text: str, gid, user_openid: str = ""):
        """插件列表 <序号>：列出该服务器已加载的插件（名称 | 作者 | 描述 | 版本）"""
        gid = gid or (getattr(message, "group_openid", None) or "")
        rest = text[len("插件列表"):].lstrip("：: \t").strip()
        toks = rest.split(None, 1)
        if not toks or not toks[0].isdigit():
            await self._reply_markdown(message, "\n".join([
                self.build_card_title("插件列表"), "",
                "**❌ 参数不完整喵...**", "", "格式：`插件列表 <服务器序号>`",
            ]))
            return
        seq = int(toks[0])
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "\n".join([
                self.build_card_title("插件列表"), "",
                f"**❌ 找不到序号 {seq} 的服务器喵...**",
            ]))
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "\n".join([
                self.build_card_title("插件列表"), "", "**❌ 该服务器标识无效喵...**",
            ]))
            return
        ok, data = await self.zse_server.request_plugin_list(server_code, timeout=15)
        if not ok or not isinstance(data, dict):
            await self._reply_markdown(message, "\n".join([
                self.build_card_title("插件列表"), "",
                "**❌ 插件列表获取失败喵...**", f"> 原因：{self._reason_of(data)}",
            ]))
            return
        plugins = data.get("plugins") or []
        rows = self._plugin_rows(plugins)
        kind = "MOD" if data.get("is_mod") else "插件"
        sname = rec.get("server_name") or f"服务器 {seq}"
        await self._reply_markdown(message, "\n".join([
            self.build_card_title(f"{kind}列表"), "",
            f"### 💾 {sname}｜共 {len(plugins)} 个{kind}",
            "",
            "\n".join(rows) if rows else "> 该服务器没有加载任何插件",
        ]))

    @staticmethod
    def _rank_table(rank_lines: dict, page: int, page_size: int = 10):
        """排行榜 -> markdown 表格；返回 (表格, 当前页, 总页数)"""
        items = list(rank_lines.items())
        total_pages = max(1, (len(items) + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        chunk = items[start:start + page_size]
        rows = [f"| {start + i} | {nm} | {val} |" for i, (nm, val) in enumerate(chunk, start=1)]
        if len(rows) < 3:  # 与 CaiBotLite 一致：不足 3 行补空行，表格不塌
            rows += ["| - | - | - |"] * (3 - len(rows))
        table = "\n".join(["| 排名 | 名字 | 项目 |", "| :--: | --- | --- |"] + rows)
        return table, page, total_pages

    @staticmethod
    def _rank_args(toks: list):
        """解析「排行 <序号> <项目> ...」里 <项目> 之后的参数与页码。

        返回 (arg, page)：末尾是纯数字且参数不止一个 token 时，把末尾当页码——
        这样含空格的参数（如 boss 名 `Moon Lord`、`Skeletron Prime`）不会被切碎。
        """
        extra = toks[2:]
        if len(extra) >= 2 and extra[-1].isdigit():
            return " ".join(extra[:-1]), int(extra[-1])
        return " ".join(extra), 1

    @staticmethod
    def _rank_keyboard(seq: int, rank_type: str, arg: str, page: int, total_pages: int):
        """翻页按钮（type=2 指令按钮，点击即发送 `排行 <序号> <项目> [参数] <页码>`）"""
        if total_pages <= 1:
            return None
        base = f"排行 {seq} {rank_type}" + (f" {arg}" if arg else "")
        rows = [KeyboardRow(buttons=[
            Button(id="rank_prev", render_data=RenderData(label="⬅️ 上一页", style=1),
                   action=Action(type=2, permission=Permission(type=2),
                                 data=f"{base} {max(1, page - 1)}")),
            Button(id="rank_next", render_data=RenderData(label="下一页 ➡️", style=1),
                   action=Action(type=2, permission=Permission(type=2),
                                 data=f"{base} {min(total_pages, page + 1)}")),
        ])]
        return KeyboardPayload(content=Keyboard(rows=rows))

    async def cmd_rank(self, message, text: str, gid, user_openid: str = ""):
        """排行 <序号> <项目> [参数] [页码]：拉取插件排行榜并以 markdown 表格分页展示（所有人可用）"""
        gid = gid or (getattr(message, "group_openid", None) or "")
        title = self.build_card_title("排行")
        rest = text[len("排行"):].lstrip("：: \t").strip()
        toks = rest.split()
        if not toks or not toks[0].isdigit():
            await self._reply_markdown(message, "\n".join([
                title, "",
                "**❌ 参数不完整喵...**", "",
                "格式：`排行 <服务器序号> <项目> [参数] [页码]`",
                "> 项目：`死亡` / `在线` / `钓鱼` / `金币`（需参数：货币名）/ `boss`（需参数：boss 名）",
            ]))
            return
        seq = int(toks[0])
        rank_type = toks[1] if len(toks) > 1 else ""
        extra = toks[2:]
        if not rank_type:
            await self._reply_markdown(message, "\n".join([
                title, "", "**❌ 请带上排行项目喵**", "",
                "> 例：`排行 1 死亡`、`排行 1 boss 克苏鲁之眼`、`排行 1 金币 幻影币`",
            ]))
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ 找不到序号 {seq} 的服务器喵...**"]))
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "\n".join([title, "", "**❌ 该服务器标识无效喵...**"]))
            return
        arg, want_page = self._rank_args(toks)
        ok, data = await self.zse_server.request_rank(server_code, rank_type, arg, timeout=20)
        if not ok or not isinstance(data, dict):
            await self._reply_markdown(message, "\n".join([
                title, "", "**❌ 排行榜获取失败喵...**", f"> 原因：{self._reason_of(data)}"]))
            return
        # 无效项目 → 回显该服务器支持的排行类型
        if not data.get("rank_type_support"):
            types = "、".join(str(x) for x in (data.get("support_rank_types") or []))
            await self._reply_markdown(message, "\n".join([
                title, "", "**❌ 无效的排行项目喵...**", "",
                f"> 该服务器支持：{types or '（无）'}",
            ]))
            return
        if data.get("need_arg"):
            page = want_page
            if not data.get("arg_support"):
                sargs = "、".join(str(x) for x in (data.get("support_args") or []))
                await self._reply_markdown(message, "\n".join([
                    title, "", f"**⚠️ {data.get('message') or '需要参数喵'}**",
                    f"> 支持参数：{sargs or '（无）'}",
                ]))
                return
        else:
            page = int(extra[0]) if extra and extra[0].isdigit() else 1
            arg = ""
        rank = data.get("rank") or {}
        lines = rank.get("rank_lines") or {}
        title_txt = _md_safe(rank.get("title") or rank_type)
        if not lines:
            await self._reply_markdown(message, "\n".join([title, "", "> 该排行榜暂无数据喵"]))
            return
        # ① 优先出图片卡（竖屏背景）
        png = None
        try:
            querier = self.whitelist_store.find_by_openid(self._eff_gid(gid), user_openid) or "群友"
            png, page, total_pages = render_rank_card(
                lines, page=page, page_size=RANK_PAGE_SIZE, title=title_txt,
                server_name=rec.get("server_name") or f"服务器 {seq}", querier=querier,
                bg_dir=self._rank_bg_dir(),
            )
        except Exception as e:
            _log.exception("排行卡渲染失败: %s", e)
            png = None
        if png:
            try:
                await send_group_image(self, gid, png, filename="rank.png",
                                       msg_id=getattr(message, "id", None))
                # 图片消息本身挂不了键盘：仅当多页时补发一条只含翻页按钮的卡片
                kb = self._rank_keyboard(seq, rank_type, arg, page, total_pages)
                if kb:
                    await self._reply_markdown_kb(
                        message,
                        "\n".join([
                            self.build_card_title(f"排行 · {title_txt}"), "",
                            f"> 第 **{page}** / **{total_pages}** 页｜共 {len(lines)} 条｜点下方按钮翻页",
                        ]),
                        kb)
                return
            except Exception as e:
                _log.warning("排行图片发送失败，降级 markdown 表格: %s", e)
        # ② 降级：markdown 表格 + 翻页按钮
        table, page, total_pages = self._rank_table(lines, page, page_size=RANK_PAGE_SIZE)
        content = "\n".join([
            self.build_card_title(f"排行 · {title_txt}"), "",
            table, "",
            f"> 第 **{page}** / **{total_pages}** 页｜共 {len(lines)} 条｜服务器 `{rec.get('server_name') or seq}`",
        ])
        await self._reply_markdown_kb(
            message, content, self._rank_keyboard(seq, rank_type, arg, page, total_pages))

    async def _download_file(self, message, text: str, gid, kind: str, prefixes: tuple):
        """下载地图(world)/下载小地图(map) 公共实现：取文件 -> 群文件发送（被动优先，失败转主动）"""
        gid = gid or (getattr(message, "group_openid", None) or "")
        label = "下载地图" if kind == "world" else "下载小地图"
        rest = text
        for p in prefixes:
            if rest.startswith(p):
                rest = rest[len(p):]
                break
        toks = rest.lstrip("：: \t").strip().split(None, 1)
        if not toks or not toks[0].isdigit():
            await self._reply_markdown(message, "\n".join([
                self.build_card_title(label), "",
                "**❌ 参数不完整喵...**", "", f"格式：`{label} <服务器序号>`",
            ]))
            return
        seq = int(toks[0])
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "\n".join([
                self.build_card_title(label), "", f"**❌ 找不到序号 {seq} 的服务器喵...**"]))
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "\n".join([
                self.build_card_title(label), "", "**❌ 该服务器标识无效喵...**"]))
            return
        if kind == "world":
            ok, data = await self.zse_server.request_world_file(server_code, timeout=180)
            fallback = f"世界{seq}.wld"
        else:
            ok, data = await self.zse_server.request_map_file(server_code, timeout=180)
            fallback = f"地图{seq}.map"
        if not ok or not isinstance(data, dict) or not data.get("base64"):
            reason = self._reason_of(data)
            if isinstance(data, dict) and data.get("error"):
                reason = str(data.get("error"))
            if kind == "map":
                reason += "（小地图需要服务器安装 GenerateMap 插件）"
            await self._reply_markdown(message, "\n".join([
                self.build_card_title(label), "", "**❌ 文件获取失败喵...**", f"> 原因：{reason}"]))
            return
        try:
            blob = decode_compressed_b64(data["base64"])
        except Exception as e:
            _log.exception("文件解压失败: %s", e)
            await self._reply_markdown(message, "\n".join([
                self.build_card_title(label), "", f"**❌ 文件解压失败喵...**", f"> 原因：{e}"]))
            return
        fname = data.get("name") or fallback
        ok_send = False
        try:
            await send_group_file(self, gid, blob, fname, file_type=4,
                                  msg_id=getattr(message, "id", None))
            ok_send = True
        except Exception as e:
            _log.warning("群文件被动发送失败(%s)，改主动发送重试: %s", fname, e)
            try:
                await send_group_file(self, gid, blob, fname, file_type=4)
                ok_send = True
            except Exception as e2:
                _log.warning("群文件主动发送也失败: %s", e2)
                await self._reply_markdown(message, "\n".join([
                    self.build_card_title(label), "",
                    f"**⚠️ 文件发送失败喵...**（{len(blob) / 1048576:.2f} MB）",
                    f"> 原因：{e2}"]))
        if ok_send:
            await self._reply_markdown(message, "\n".join([
                self.build_card_title(label), "",
                f"### ✅ 已发送 `{fname}`",
                f"> 大小 **{len(blob) / 1048576:.2f} MB**｜服务器 `{rec.get('server_name') or seq}`",
            ]))

    async def cmd_download_world(self, message, text: str, gid):
        """下载地图 <序号>：以群文件发送服务器当前世界存档（.wld）"""
        await self._download_file(message, text, gid, "world",
                                  ("下载世界文件", "下载存档", "下载地图"))

    async def cmd_download_map(self, message, text: str, gid):
        """下载小地图 <序号>：以群文件发送 GenerateMap 生成的小地图（.map）"""
        await self._download_file(message, text, gid, "map",
                                  ("下载小地图文件", "下载小地图"))

    async def cmd_say(self, message, text: str, gid):
        """喊话 <服务器序列号> <内容>：向指定服务器广播（= 服内 /say），任何人可发"""
        gid = gid or (getattr(message, "group_openid", None) or "")
        parts = text.split(None, 2)
        if len(parts) < 3 or not parts[1].isdigit():
            await self._reply_markdown(
                message,
                "## ꧁༺ 服内广播 ༻꧂\n\n"
                "参数不完整喵...\n\n"
                "请按照正确的格式：`喊话 <服务器序列号> <内容>` 重新输入喵",
            )
            return
        seq = int(parts[1])
        content = parts[2].strip()
        user_openid = getattr(message.author, "member_openid", None) or ""
        nm = (self.whitelist_store.find_by_openid(self._eff_gid(gid), user_openid)
              or self.whitelist_store.claim_single(self._eff_gid(gid), user_openid)
              or "")
        say = f"【{nm}】说：{content}" if nm else content
        sname = self._server_name_plain(gid, seq) or "未知服务器"
        try:
            ok = await self.zse_server.send_say(gid, seq, say)
        except Exception as e:
            _log.exception("喊话发送异常: %s", e)
            await self._reply_at_then_card(
                message, user_openid,
                "## ꧁༺ ZSE 广播功能 ༻꧂\n\n"
                f"@{nm or '群友'}\n\n"
                f"服务器：`{sname}`\n"
                f"执行内容：`{say}`\n"
                f"返回内容：\n\n```\n发送失败：{e}\n```",
            )
            return
        if not ok:
            await self._reply_at_then_card(
                message, user_openid,
                "## ꧁༺ ZSE 广播功能 ༻꧂\n\n"
                f"@{nm or '群友'}\n\n"
                f"服务器：`{sname}`\n"
                f"执行内容：`{say}`\n"
                f"返回内容：\n\n```\n发送失败（服务器离线或不存在）\n```",
            )
            return
        await self._reply_at_then_card(
            message, user_openid,
            "## ꧁༺ ZSE 广播功能 ༻꧂\n\n"
            f"@{nm or '群友'}\n\n"
            f"服务器：`{sname}`\n"
            f"执行内容：`{say}`\n"
            f"返回内容：\n\n```\n服务器返回了个棍母喵...\n```",
        )

    async def cmd_say_all(self, message, text: str, gid):
        """全服喊话 <内容>：对所有在线服务器广播（管理员及以上，分发处 _perm_ok 校验）"""
        gid = gid or (getattr(message, "group_openid", None) or "")
        parts = text.split(None, 1)
        if len(parts) < 2:
            await self._reply_markdown(
                message,
                "## ꧁༺ 全服广播 ༻꧂\n\n"
                "参数不完整喵...\n\n"
                "请按照正确的格式：`全服喊话 <内容>` 重新输入喵",
            )
            return
        content = parts[1].strip()
        user_openid = getattr(message.author, "member_openid", None) or ""
        nm = (self.whitelist_store.find_by_openid(self._eff_gid(gid), user_openid)
              or self.whitelist_store.claim_single(self._eff_gid(gid), user_openid)
              or "")
        say = f"来自【{nm}】的【全服广播】：{content}" if nm else f"【全服广播】：{content}"
        ok_cnt = 0
        offline = []
        recs = self.zse_server.visible_records(gid)
        for idx, rec in enumerate(recs, 1):
            try:
                if await self.zse_server.send_say(gid, idx, say):
                    ok_cnt += 1
                else:
                    offline.append(f"`{idx}`{rec.get('server_name') or ''}")
            except Exception as e:
                _log.exception("全服喊话发送异常: %s", e)
        if ok_cnt > 0:
            names = "、".join(
                f"`{idx}`{r.get('server_name') or ''}"
                for idx, r in enumerate(recs, 1)
            )
            msg = (f"服务器：`{names}`\n"
                   f"执行内容：`{say}`\n"
                   f"返回内容：\n\n```\n已向 {ok_cnt} 个服务器广播"
                   + ("\n离线未送达：" + "、".join(offline) if offline else "")
                   + "\n```")
        else:
            msg = f"服务器：`--`\n执行内容：`{say}`\n返回内容：\n\n```\n发送失败（没有在线服务器）\n```"
        await self._reply_at_then_card(
            message, user_openid,
            "## ꧁༺ ZSE 全服广播 ༻꧂\n\n"
            f"@{nm or '群友'}\n\n" + msg,
        )

    async def cmd_about(self, message, gid, user_openid: str = ""):
        """关于：机器人信息卡（所有群员可用；与入群卡片"关于"按钮共用模板）"""
        await self._reply_markdown(
            message,
            self._about_card(gid, user_openid or self._user_openid(message)),
        )

    async def cmd_broadcast(self, message, text: str, gid):
        """广播 <内容>：向本群所在联合区的全部群发送公告（服主+）。
        主动消息，需群主开启「机器人主动在群聊内发言」才可送达；失败名单会汇总提示。"""
        content = text[len("广播"):].lstrip("：: \t").strip()
        if not content:
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE 联合广播 ༻꧂\n\n"
                "**缺少内容喵...**\n\n"
                "格式：`广播 <内容>`\n"
                "例：`广播 今晚 20:00 服务器维护`",
            )
            return
        user_openid = self._user_openid(message)
        nm = self.whitelist_store.find_by_openid(self._eff_gid(gid), user_openid) or "管理员"
        av = self._avatar_md(self._eff_gid(gid), nm) if nm != "管理员" else ""
        # QQ markdown 渲染器在"文字紧贴图片"（如图片前是【）时会把图片强制换行，
        # 所以头像放行首（在线/引导卡已验证该写法可内联），后跟"来自【玩家名】"
        from_line = f"> {av} 来自【{nm}】" if av else f"> 来自【{nm}】"
        card = "\n".join([
            "## ꧁༺ ZSE 联合广播 ༻꧂",
            "",
            content,
            "",
            "***",
            from_line,
        ])
        # 群发给联合区内所有群，但跳过发起广播的本群（发起人已在自己群看到结果卡）
        targets = sorted((self.registry.zone_gids(gid) or {gid}) - {gid})
        if not targets:
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE 联合广播 ༻꧂\n\n"
                "当前联合区内没有其它群喵，无需群发...",
            )
            return
        ok_cnt, failed = 0, []
        for tg in targets:
            try:
                await self.api.post_group_message(group_openid=tg, msg_type=2, markdown=MarkdownPayload(content=card))
                ok_cnt += 1
            except Exception as e:
                _log.warning("公告发送失败 group=%s: %s", tg, e)
                failed.append(tg[:8])
        result = f"✅ 公告已发送至 **{ok_cnt}** 个群" if ok_cnt else "❌ 发送失败（可能未开启主动发言权限）"
        if failed:
            result += f"\n> 失败群：{'、'.join(failed)}"
        await self._reply_markdown(message, "## ꧁༺ ZSE 联合广播 ༻꧂\n\n" + result)

    # ───────────────────────── 种子投票 / 投票 / 结束投票 / 重置 ─────────────────────────
    def _vote_option_count(self) -> int:
        try:
            return int(self._vote_config.get("option_count", 6) or 6)
        except (TypeError, ValueError):
            return 6

    def _vote_update_minutes(self) -> int:
        try:
            return int(self._vote_config.get("update_interval_minutes", 60) or 60)
        except (TypeError, ValueError):
            return 60

    def _vote_deadline_hours(self) -> float:
        try:
            return float(self._vote_config.get("deadline_hours", 24) or 24)
        except (TypeError, ValueError):
            return 24.0

    def _vote_bg_dir(self):
        """投票卡背景图目录；配置留空返回 None（渲染模块自动复用查背包素材目录）"""
        d = (self._vote_config.get("bg_dir") or "").strip()
        return d or None

    def _vote_server(self, gid: str, seq: int):
        """按展示序号取服务器记录（沿用 record_by_seq 的「序号→服务器记录」解析）"""
        return self.zse_server.record_by_seq(gid, seq)

    @staticmethod
    def _vote_server_code(rec) -> str:
        """服务器标识：登记记录的绑定码 code（全局唯一、稳定，作为 votes.json 的 server_code）"""
        return (rec or {}).get("code") or ""

    def _vote_target_gids(self, vote) -> list:
        """投票卡/存档的推送目标群：按投票发起群「实时」计算联合区（成员可随时增减）；
        发起群已不存在时回退创建时快照，避免推送目标丢失。"""
        origin = (vote or {}).get("origin_gid") or ""
        if origin and self.registry.is_registered(origin):
            zone = sorted(self.registry.zone_gids(origin) or set())
            if zone:
                return zone
        return list((vote or {}).get("zone_gids") or [])

    def _build_vote_snapshot(self, gid: str, online_minutes: dict) -> dict:
        """用白名单绑定（玩家名→bind_openid）与插件 online_minutes（玩家名→分钟）反查，
        生成 {openid: 分钟} 快照；未绑定/查不到者不写入（其权重按 1 分计）。"""
        snap = {}
        eff = self._eff_gid(gid)
        for name, minutes in (online_minutes or {}).items():
            rec = self.whitelist_store.get_record(eff, name)
            oid = (rec or {}).get("bind_openid")
            if oid:
                snap[oid] = minutes
        return snap

    async def _fetch_vote_config(self, server_code: str):
        """best-effort 拉取插件种子配置（auto_reset get_config）；不可用返回 None"""
        ok, data = await self.zse_server.request_auto_reset(server_code, "get_config", timeout=15)
        return data if (ok and isinstance(data, dict)) else None

    @staticmethod
    def _reason_of(data) -> str:
        if isinstance(data, dict):
            return str(data.get("error") or "未知错误")
        return str(data or "未知错误")

    def _render_vote_png(self, vote, tally, status: str = "open") -> bytes:
        """渲染投票卡为 JPEG bytes（临时文件中转；JPEG 比 PNG 小约 5 倍）"""
        fd, tmp = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            render_vote_card(tmp, vote, tally, status=status, bg_dir=self._vote_bg_dir())
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    async def _publish_vote_card(self, gids, vote, tally, status: str):
        """把投票卡（主动消息图片）发布到指定群列表；返回 (成功数, 失败群openid列表)"""
        try:
            png = self._render_vote_png(vote, tally, status)
        except Exception as e:
            _log.exception("投票卡渲染失败: %s", e)
            return 0, list(gids or [])
        ok_cnt, failed = 0, []
        for g in (gids or []):
            try:
                await send_group_image(self, g, png, filename="vote.jpg")
                ok_cnt += 1
            except Exception as e:
                _log.warning("投票卡发布失败 group=%s: %s", (g or "")[:8], e)
                failed.append(g)
        return ok_cnt, failed

    @staticmethod
    def _vote_failed_line(failed) -> str:
        if not failed:
            return ""
        return f"\n> 失败群：{'、'.join((f or '')[:8] for f in failed)}（可能未开启主动发言权限）"

    @staticmethod
    def _fmt_vote_time(iso) -> str:
        try:
            dt = datetime.fromisoformat(str(iso))
        except (ValueError, TypeError):
            return str(iso or "-")
        return f"{dt.month}月{dt.day}日 {dt:%H:%M}"

    async def cmd_seed_vote(self, message, text: str, gid, user_openid: str):
        """种子投票 [序号] [候选…]：发起投票（无候选则读插件种子列表随机生成）"""
        rest = text[len("种子投票"):].lstrip("：: \t").strip()
        seq, cand_text = parse_server_index(rest)
        server_seq = seq or 1
        rec = self._vote_server(gid, server_seq)
        if rec is None:
            await self._reply_markdown(message, "## ꧁༺ 种子投票 ༻꧂\n\n"
                                       f"**❌ 找不到序号 {server_seq} 的服务器喵...**")
            return
        server_code = self._vote_server_code(rec)
        if not server_code or self.votes.has_active(server_code):
            await self._reply_markdown(message, "## ꧁༺ 种子投票 ༻꧂\n\n"
                                       "**❌ 该服务器已有进行中的投票喵...**\n\n"
                                       "> 可先发送 `结束投票` 提前截止，再发起新的投票")
            return
        # 拉取插件配置（随机生成必需；指定候选时仅用于在线时长快照，失败可降级）
        cfg = await self._fetch_vote_config(server_code)
        snapshot = self._build_vote_snapshot(gid, (cfg or {}).get("online_minutes") or {})
        if cand_text:
            proposer = self.whitelist_store.find_by_openid(self._eff_gid(gid), user_openid) or "群友"
            options, err = parse_candidates(cand_text, proposer)
        else:
            if not cfg or not cfg.get("installed"):
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 种子投票 ༻꧂\n\n"
                    "**❌ 未检测到 AutoResetPlus 插件，无法随机生成候选喵...**\n\n"
                    "可手动指定候选：`种子投票 十周年+醉酒；饥荒+下雨`\n"
                    "（多个候选用 `；` 分隔，组合内的种子用 `+` 连接）",
                )
                return
            options, err = generate_random_options(
                cfg.get("seed_list"), cfg.get("min"), cfg.get("max"),
                option_count=min(VOTE_RANDOM_OPTIONS, self._vote_option_count()))
        if err or not options:
            await self._reply_markdown(message, "## ꧁༺ 种子投票 ༻꧂\n\n"
                                       f"**❌ 候选解析失败：{err or '没有可用候选'}喵...**")
            return
        zone_gids = sorted(self.registry.zone_gids(gid) or {gid})
        try:
            vote_id = self.votes.create_vote(
                server_code, origin_gid=gid, zone_gids=zone_gids, options=options,
                snapshot=snapshot, deadline_hours=self._vote_deadline_hours(),
                max_options=self._vote_max_options(),
                update_interval_minutes=self._vote_update_minutes(),
                title="下个档玩什么")
        except ValueError as e:
            await self._reply_markdown(message, f"## ꧁༺ 种子投票 ༻꧂\n\n**❌ {e}喵...**")
            return
        vote = self.votes.get_vote(vote_id)
        # 卡片仅发本群预览（发起不推群）；各群推送由「推送投票」手动触发
        await self._publish_vote_card([gid], vote, self.votes.tally(vote_id), "open")
        lines = ["## ꧁༺ 种子投票 ༻꧂", "",
                 "**✅ 投票已发起（卡片仅本群预览，尚未推送到各群）**", ""]
        for o in (vote.get("options") or []):
            lines.append(f"- {o.get('no')}. `{o.get('name')}` —— {o.get('proposer') or '群友'}")
        lines.append("")
        lines.append(f"- 截止时间：{self._fmt_vote_time(vote.get('deadline'))}")
        lines.append("- 参与方式：发送 `投票 <编号>`（再次发送取消，每人最多 2 票）")
        lines.append("- 推送到各群：发送 `推送投票 <序号>`（管理员及以上）")
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_vote(self, message, text: str, gid, user_openid: str):
        """投票 <编号>：投票/取消（切换语义，所有群员）。所有错误分支零副作用。"""
        rest = text[len("投票"):].lstrip("：: \t").strip()
        active = self.votes.active_votes_for_gid(gid)
        if not active:
            await self._reply_markdown(message, "## ꧁༺ 投票 ༻꧂\n\n**当前没有进行中的投票喵...**")
            return
        if len(active) > 1:
            await self._reply_markdown(
                message,
                "## ꧁༺ 投票 ༻꧂\n\n"
                "**当前有多个服务器的投票进行中，请联系服主喵...**")
            return
        if not rest or not rest.isdigit():
            await self._reply_markdown(
                message,
                "## ꧁༺ 投票 ༻꧂\n\n"
                "**缺少投票编号喵...**\n\n"
                "格式：`投票 <编号>`（编号见投票卡，例：`投票 1`）",
            )
            return
        ok, msg = self.votes.toggle_vote(active[0].get("vote_id"), user_openid, int(rest))
        await self._reply_markdown(
            message, "## ꧁༺ 投票 ༻꧂\n\n" + (f"✅ {msg}" if ok else f"**❌ {msg}**"))

    async def cmd_end_vote(self, message, text: str, gid):
        """结束投票 [序号]：提前截止并公布结果（服主及以上）"""
        rest = text[len("结束投票"):].lstrip("：: \t").strip()
        seq, _ = parse_server_index(rest)
        server_seq = seq or 1
        rec = self._vote_server(gid, server_seq)
        if rec is None:
            await self._reply_markdown(message, "## ꧁༺ 结束投票 ༻꧂\n\n"
                                       f"**❌ 找不到序号 {server_seq} 的服务器喵...**")
            return
        server_code = self._vote_server_code(rec)
        vote = self.votes.get_active(server_code) if server_code else None
        if not vote:
            await self._reply_markdown(message, "## ꧁༺ 结束投票 ༻꧂\n\n"
                                       "**该服务器当前没有进行中的投票喵...**")
            return
        vote_id = vote["vote_id"]
        # 整个"截止 → 发结果卡 → 标记已发布"必须在锁内完成，
        # 否则 60 秒调度会在发卡途中看到"未发布"而补发一轮（每个群多一张卡）
        async with self._vote_pub_lock:
            winner = self.votes.finish_vote(vote_id)
            vote = self.votes.get_vote(vote_id)
            tally = self.votes.tally(vote_id)
            # 推送目标：按发起群实时计算联合区（避免用创建时快照导致新增/退出群漏发/多发）
            zone_gids = self._vote_target_gids(vote)
            ok_cnt, failed = await self._publish_vote_card(zone_gids, vote, tally, "closed")
            self.votes.mark_result_published(vote_id)
        lines = ["## ꧁༺ 结束投票 ༻꧂", "",
                 f"**✅ 投票已结束**（结果卡已发布至 {ok_cnt}/{len(zone_gids)} 个群）", ""]
        if winner:
            lines.append(f"- 获胜组合：`{winner.get('name')}`")
            lines.append(f"- 分数：{float(winner.get('score') or 0):g} 分 · 票数：{winner.get('votes')} 票")
            if winner.get("tie_random"):
                lines.append("> 最高分与票数均并列，已平分随机选取")
        else:
            lines.append("> 没有任何有效投票，未产生获胜组合")
        lines.append(self._vote_failed_line(failed))
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_push_vote(self, message, text: str, gid):
        """推送投票 [序号]：把进行中投票卡推送到其联合区全部群（管理员及以上）。

        带序号 → 该序号服务器的进行中投票；不带序号 → 当前群可见的最新进行中投票。
        不调用 mark_update_sent：不影响 60 分钟自动重发计时。
        """
        rest = text[len("推送投票"):].lstrip("：: \t").strip()
        seq, _ = parse_server_index(rest)
        vote = None
        scope = ""
        if seq:
            rec = self._vote_server(gid, seq)
            if rec is None:
                await self._reply_markdown(message, "## ꧁༺ 推送投票 ༻꧂\n\n"
                                           f"**❌ 找不到序号 {seq} 的服务器喵...**")
                return
            server_code = self._vote_server_code(rec)
            vote = self.votes.get_active(server_code) if server_code else None
            scope = f"序号 {seq} 服务器"
        else:
            actives = self.votes.active_votes_for_gid(gid)
            if actives:
                vote = max(actives, key=lambda v: str(v.get("created_at") or ""))
                scope = "最新进行中"
        if not vote:
            await self._reply_markdown(message, "## ꧁༺ 推送投票 ༻꧂\n\n"
                                       "**当前没有进行中的投票喵...**\n\n"
                                       "> 可先发送 `种子投票` 发起，再推送")
            return
        tally = self.votes.tally(vote["vote_id"])
        # 推送目标：实时计算联合区（推送时联合区可能已有变动）
        gids = self._vote_target_gids(vote)
        ok_cnt, failed = await self._publish_vote_card(gids, vote, tally, "open")
        lines = ["## ꧁༺ 推送投票 ༻꧂", "",
                 f"**✅ 投票卡已推送至 {ok_cnt}/{len(gids)} 个群**（{scope}）", "",
                 "- 参与方式：发送 `投票 <编号>`（再次发送取消，每人最多 2 票）",
                 self._vote_failed_line(failed)]
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_view_vote(self, message, text: str, gid):
        """查看投票 <序号>：把指定服务器的当前投票卡发到本群（所有人可用）。

        进行中优先，没有则显示最近已结束的一场；发送成功不回复确认消息。
        """
        rest = text[len("查看投票"):].lstrip("：: \t").strip()
        seq, _ = parse_server_index(rest)
        if not seq:
            await self._reply_markdown(message, "## ꧁༺ 查看投票 ༻꧂\n\n"
                                       "**❌ 请带上服务器序号喵：`查看投票 <序号>`**")
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "## ꧁༺ 查看投票 ༻꧂\n\n"
                                       f"**❌ 找不到序号 {seq} 的服务器喵...**")
            return
        server_code = self._vote_server_code(rec)
        vote = self.votes.latest_vote_for_gid(gid, server_code=server_code) if server_code else None
        if not vote:
            await self._reply_markdown(message, "## ꧁༺ 查看投票 ༻꧂\n\n"
                                       f"**序号 {seq} 服务器当前没有可查看的投票喵...**\n\n"
                                       "> 可先发送 `种子投票` 发起一场")
            return
        tally = self.votes.tally(vote["vote_id"])
        status = vote.get("status") or "open"
        ok_cnt, _ = await self._publish_vote_card([gid], vote, tally, status)
        if not ok_cnt:
            await self._reply_markdown(message, "## ꧁༺ 查看投票 ༻꧂\n\n"
                                       "**⚠️ 投票卡发送失败喵...**（可能未开启主动发言权限）")

    # ───────────────────────── 进度查询 / 进度提醒 ─────────────────────────
    def _progress_bg_dir(self):
        """进度卡背景图目录；未配置返回 None（渲染模块自动复用查背包背景）"""
        d = (self._vote_config.get("bg_dir") or "").strip()
        return d or None

    def _rank_bg_dir(self):
        """排行卡背景目录；未配置返回 None（渲染模块自动用 assets/rank/backgrounds）"""
        return (self._vote_config.get("rank_bg_dir") or "").strip() or None

    def _progress_tip(self, seq: int, boss_key: str) -> str:
        """统一生成取消提示（中文名 + 英文 key 两种都能用）"""
        return f"取消：`取消进度提醒 {seq} {boss_cn(boss_key)}`（或英文 `{boss_key}`）"

    async def cmd_progress_query(self, message, text: str, gid, user_openid: str = ""):
        """进度查询 <序号>：拉取插件 boss 进度并渲染为图片卡发到本群（所有人可用）。

        发送成功不回复确认消息（与「查看投票」交互一致）。
        """
        rest = text[len("进度查询"):].lstrip("：: \t").strip()
        seq, _ = parse_server_index(rest)
        if not seq:
            await self._reply_markdown(message, "## ꧁༺ 进度查询 ༻꧂\n\n"
                                       "**❌ 请带上服务器序号喵：`进度查询 <序号>`**")
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "## ꧁༺ 进度查询 ༻꧂\n\n"
                                       f"**❌ 找不到序号 {seq} 的服务器喵...**")
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "## ꧁༺ 进度查询 ༻꧂\n\n**❌ 该服务器标识无效喵...**")
            return
        ok, data = await self.zse_server.request_progress(server_code, timeout=15)
        if not ok or not isinstance(data, dict):
            await self._reply_markdown(message, "## ꧁༺ 进度查询 ༻꧂\n\n"
                                       f"**❌ 进度查询失败喵...**\n\n> 原因：{self._reason_of(data)}")
            return
        if data.get("is_text"):
            # 插件回退为文本（协议允许）→ 原样转发，避免渲染出误导性的空卡
            await self._reply_markdown(message, "## ꧁༺ 进度查询 ༻꧂\n\n"
                                       f"{data.get('text') or '插件返回为空'}")
            return
        querier = self.whitelist_store.find_by_openid(self._eff_gid(gid), user_openid) or "群友"
        try:
            png = render_progress_card(
                data,
                server_name=rec.get("server_name") or "",
                querier=querier,
                bg_dir=self._progress_bg_dir(),
            )
            await send_group_image(self, gid, png, filename="progress.png")
        except Exception as e:
            _log.exception("进度卡渲染/发送失败: %s", e)
            await self._reply_markdown(message, "## ꧁༺ 进度查询 ༻꧂\n\n"
                                       f"**⚠️ 进度卡渲染或发送失败喵...**\n\n> 原因：{e}")

    async def cmd_progress_notify(self, message, text: str, gid, user_openid: str = ""):
        """进度提醒 <序号> <boss名>：设定该服务器某 boss 首次被击杀时播报到本群（管理员及以上）。

        每群 × 每服务器 × 每 boss 独立；仅播报本世界首杀，重置（新世界）后重新生效。
        """
        rest = text[len("进度提醒"):].lstrip("：: \t").strip()
        seq, tail = parse_server_index(rest)
        if not seq:
            await self._reply_markdown(message, "## ꧁༺ 进度提醒 ༻꧂\n\n"
                                       "**❌ 请带上服务器序号喵：`进度提醒 <序号> <boss名>`**")
            return
        boss_key = resolve_boss(tail)
        if not boss_key:
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 进度提醒 ༻꧂", "",
                    f"**❌ 无法识别的 boss 名：`{tail or '（空）'}`**", "",
                    "> 支持中文名或英文 key，可选：",
                    "> " + "、".join(cn for _k, cn in BOSSES),
                ]),
            )
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "## ꧁༺ 进度提醒 ༻꧂\n\n"
                                       f"**❌ 找不到序号 {seq} 的服务器喵...**")
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "## ꧁༺ 进度提醒 ༻꧂\n\n**❌ 该服务器标识无效喵...**")
            return
        ok, msg = self.progress_notifies.add(
            gid, server_code, boss_key, boss_cn(boss_key),
            server_name=rec.get("server_name") or "",
            created_by=user_openid,
        )
        if not ok:
            await self._reply_markdown(message, "## ꧁༺ 进度提醒 ༻꧂\n\n"
                                       f"**❌ {msg}**\n\n> 可发送 `进度提醒列表` 查看本群已设定的提醒")
            return
        await self._reply_markdown(
            message,
            "\n".join([
                "## ꧁༺ 进度提醒 ༻꧂", "",
                f"**✅ 已设定：序号 {seq} 服务器 · {boss_cn(boss_key)} 首杀播报到本群喵！**", "",
                f"- 服务器：`{rec.get('server_name') or rec.get('ip') or server_code}`",
                "- 播报时机：该 boss **本世界首次**被击杀时（播报击杀时间与击杀玩家）",
                "- 直到服务器重置（新世界）前，后续击杀不再重复播报；重置后重新生效",
                "- 本设置仅对本群本服务器生效，不影响其它群",
                "",
                f"> {self._progress_tip(seq, boss_key)}",
            ]),
        )

    async def cmd_progress_notify_list(self, message, text: str, gid):
        """进度提醒列表：本群全部提醒（标注服务器当前展示序号；不可见/已删除时警示）"""
        entries = self.progress_notifies.list_for(gid)
        lines = ["## ꧁༺ 进度提醒列表 ༻꧂", ""]
        if not entries:
            lines += [
                "**本群还没有设定任何进度提醒喵...**", "",
                "> 设定：`进度提醒 <服务器序号> <boss名>`（例：`进度提醒 1 月亮领主`）",
            ]
            await self._reply_markdown(message, "\n".join(lines))
            return
        seq_by_code = {}
        for i, r in enumerate(self.zse_server.visible_records(gid) or []):
            code = (r or {}).get("code") or ""
            if code:
                seq_by_code[code] = i + 1
        for i, e in enumerate(entries):
            code = e.get("server_code") or ""
            seq = seq_by_code.get(code)
            target = f"序号 {seq}" if seq else "⚠️ 服务器当前不可见或已删除"
            lines.append(
                f"{i + 1}. {e.get('boss_name') or e.get('boss_key')}"
                f" —— `{target}`（{e.get('server_name') or code[:8]}）"
            )
        lines += ["", "> 取消：`取消进度提醒 <服务器序号> <boss名>`"]
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_progress_cancel(self, message, text: str, gid):
        """取消进度提醒 <序号> <boss名>：删除本群的一条提醒（管理员及以上）"""
        rest = text[len("取消进度提醒"):].lstrip("：: \t").strip()
        seq, tail = parse_server_index(rest)
        if not seq:
            await self._reply_markdown(message, "## ꧁༺ 取消进度提醒 ༻꧂\n\n"
                                       "**❌ 请带上服务器序号喵：`取消进度提醒 <序号> <boss名>`**")
            return
        boss_key = resolve_boss(tail)
        if not boss_key:
            await self._reply_markdown(message, "## ꧁༺ 取消进度提醒 ༻꧂\n\n"
                                       f"**❌ 无法识别的 boss 名：`{tail or '（空）'}`**\n\n"
                                       "> 可发送 `进度提醒列表` 查看本群已设定的提醒")
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "## ꧁༺ 取消进度提醒 ༻꧂\n\n"
                                       f"**❌ 找不到序号 {seq} 的服务器喵...**")
            return
        ok, msg = self.progress_notifies.remove(gid, self._vote_server_code(rec), boss_key)
        if not ok:
            await self._reply_markdown(message, "## ꧁༺ 取消进度提醒 ༻꧂\n\n**❌ " + msg + "**")
            return
        await self._reply_markdown(message, "## ꧁༺ 取消进度提醒 ༻꧂\n\n"
                                   f"**✅ 已取消：序号 {seq} 服务器 · {boss_cn(boss_key)} 的首杀播报喵**")

    async def _on_progress_notify_push(self, rec: dict, payload: dict):
        """插件首杀推送 → 渲染播报卡并发送到所有订阅群（由 zse_server 的 WS 循环回调）。

        订阅关系为「每群 × 每服务器 × 每 boss」独立；无订阅群时静默忽略。
        """
        server_code = (rec or {}).get("code") or ""
        payload = payload or {}
        boss_key = payload.get("boss_key") or ""
        if not server_code or not boss_key:
            return
        # 幂等：以「服务器 + boss + 击杀时间 + 世界名」为键，同一场首杀只播一轮
        fire_key = "|".join([server_code, boss_key,
                             str(payload.get("kill_time") or ""),
                             str(payload.get("world_name") or "")])
        now = time.time()
        if self._notify_fired.get(fire_key):
            _log.info("首杀推送重复，已忽略 boss=%s server=%s", boss_key, server_code[:8])
            return
        self._notify_fired[fire_key] = now
        if len(self._notify_fired) > 800:      # 只保留 24 小时，防止无限增长
            for k in [k for k, t in self._notify_fired.items() if now - t > 86400]:
                self._notify_fired.pop(k, None)
        gids = self.progress_notifies.subscribers_for(server_code, boss_key)
        if not gids:
            _log.info("首杀推送无任何群订阅，忽略 boss=%s server=%s", boss_key, server_code[:8])
            return
        players = payload.get("players")
        if isinstance(players, str):
            players = [players] if players else []
        try:
            png = render_notify_card(
                boss_key,
                players=players or [],
                kill_time=payload.get("kill_time") or "",
                world_name=payload.get("world_name") or "",
                server_name=(rec or {}).get("server_name") or "",
                bg_dir=self._progress_bg_dir(),
            )
        except Exception as e:
            _log.exception("首杀播报卡渲染失败 boss=%s: %s", boss_key, e)
            return
        ok_cnt = 0
        for g in gids:
            try:
                await send_group_image(self, g, png, filename="progress_notify.png")
                ok_cnt += 1
            except Exception as e:
                _log.warning("首杀播报发送失败 group=%s: %s", (g or "")[:8], e)
        _log.info("首杀播报完成 boss=%s 成功 %s/%s 群", boss_key, ok_cnt, len(gids))

    # ───────────────────────── 进度解锁提醒 ─────────────────────────
    def _progress_unlock_tip(self, seq: int, boss_key: str) -> str:
        """统一生成取消提示（中文名 + 英文 key 两种都能用）"""
        return f"取消：`取消进度解锁提醒 {seq} {boss_cn(boss_key)}`（或英文 `{boss_key}`）"

    async def cmd_progress_unlock(self, message, text: str, gid, user_openid: str = ""):
        """进度解锁提醒 <序号> <boss名> <分钟>：该 boss 解锁前 N 分钟提醒本群（管理员及以上）。

        仅支持装有 BossLock / ProgressControls 且当前处于锁定状态的 boss（设定时实时校验）；
        每群 × 每服务器 × 每 boss 独立，重复设置覆盖旧分钟数。
        """
        rest = text[len("进度解锁提醒"):].lstrip("：: \t").strip()
        seq, tail = parse_server_index(rest)
        if not seq:
            await self._reply_markdown(message, "## ꧁༺ 进度解锁提醒 ༻꧂\n\n"
                                       "**❌ 请带上服务器序号喵：`进度解锁提醒 <序号> <boss名> <分钟>`**")
            return
        parts = tail.rsplit(None, 1)
        if len(parts) < 2 or not parts[-1].isdigit():
            await self._reply_markdown(message, "## ꧁༺ 进度解锁提醒 ༻꧂\n\n"
                                       "**❌ 格式：`进度解锁提醒 <序号> <boss名> <分钟>`**\n\n"
                                       "> 例：`进度解锁提醒 1 月亮领主 30`（解锁前 30 分钟提醒）")
            return
        minutes = int(parts[-1])
        if not (1 <= minutes <= 1440):
            await self._reply_markdown(message, "## ꧁༺ 进度解锁提醒 ༻꧂\n\n"
                                       "**❌ 分钟数需在 1~1440 之间喵**")
            return
        boss_key = resolve_boss(parts[0])
        if not boss_key:
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 进度解锁提醒 ༻꧂", "",
                    f"**❌ 无法识别的 boss 名：`{parts[0] or '（空）'}`**", "",
                    "> 支持中文名或英文 key，可选：",
                    "> " + "、".join(cn for _k, cn in BOSSES),
                ]),
            )
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "## ꧁༺ 进度解锁提醒 ༻꧂\n\n"
                                       f"**❌ 找不到序号 {seq} 的服务器喵...**")
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "## ꧁༺ 进度解锁提醒 ༻꧂\n\n**❌ 该服务器标识无效喵...**")
            return
        # 设定时实时校验：服务器在线 + 该 boss 当前处于锁定状态（解锁时间戳来自锁插件）
        ok, data = await self.zse_server.request_progress(server_code, timeout=10)
        if not ok or not isinstance(data, dict) or data.get("is_text"):
            await self._reply_markdown(message, "## ꧁༺ 进度解锁提醒 ༻꧂\n\n"
                                       "**❌ 无法确认锁定状态喵...**\n\n"
                                       f"> 原因：{self._reason_of(data)}\n"
                                       "> 解锁提醒需要服务器在线（要先能取到解锁时间）")
            return
        ts_map = data.get("boss_lock_ts")
        if not isinstance(ts_map, dict):
            await self._reply_markdown(message, "## ꧁༺ 进度解锁提醒 ༻꧂\n\n"
                                       "**❌ 该服务器插件版本不支持解锁提醒喵...**\n\n"
                                       "> 需要新版 starZSEbot 插件（进度包携带解锁时间戳）")
            return
        try:
            unlock_ts = int(ts_map.get(boss_key) or 0)
        except (TypeError, ValueError):
            unlock_ts = 0
        if unlock_ts <= 0:
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 进度解锁提醒 ༻꧂", "",
                    f"**❌ {boss_cn(boss_key)} 当前没有处于锁定状态喵...**", "",
                    "> 解锁提醒仅支持 BossLock / ProgressControls 当前锁定中的 boss",
                    f"> 可发送 `进度查询 {seq}` 查看各 boss 锁定情况",
                ]),
            )
            return
        ok2, msg = self.progress_unlocks.add(
            gid, server_code, boss_key, boss_cn(boss_key),
            server_name=rec.get("server_name") or "",
            minutes=minutes, created_by=user_openid,
        )
        if not ok2:
            await self._reply_markdown(message, "## ꧁༺ 进度解锁提醒 ༻꧂\n\n"
                                       f"**❌ {msg}**\n\n> 可发送 `进度解锁提醒列表` 查看本群已设定的提醒")
            return
        action = "已更新（已覆盖旧分钟数）" if msg == "已更新" else "已设定"
        await self._reply_markdown(
            message,
            "\n".join([
                "## ꧁༺ 进度解锁提醒 ༻꧂", "",
                f"**✅ {action}：序号 {seq} 服务器 · {boss_cn(boss_key)} 解锁前 {minutes} 分钟提醒本群喵！**", "",
                f"- 服务器：`{rec.get('server_name') or rec.get('ip') or server_code}`",
                f"- 当前解锁时间：{format_unlock_time(unlock_ts)}",
                f"- 触发时机：解锁前 {minutes} 分钟推送一张卡片图（仅一次）",
                "- 世界重置（解锁时间变化）后自动重新生效；服务器离线时按最后一次同步时间照发",
                "- 本设置仅对本群本服务器生效，不影响其它群",
                "",
                f"> {self._progress_unlock_tip(seq, boss_key)}",
            ]),
        )

    async def cmd_progress_unlock_list(self, message, text: str, gid):
        """进度解锁提醒列表：本群全部解锁提醒（标注服务器序号 / 最近同步的解锁时间与状态）"""
        entries = self.progress_unlocks.list_for(gid)
        lines = ["## ꧁༺ 进度解锁提醒列表 ༻꧂", ""]
        if not entries:
            lines += [
                "**本群还没有设定任何进度解锁提醒喵...**", "",
                "> 设定：`进度解锁提醒 <服务器序号> <boss名> <分钟>`",
                "> 例：`进度解锁提醒 1 月亮领主 30`（解锁前 30 分钟提醒）",
            ]
            await self._reply_markdown(message, "\n".join(lines))
            return
        seq_by_code = {}
        for i, r in enumerate(self.zse_server.visible_records(gid) or []):
            code = (r or {}).get("code") or ""
            if code:
                seq_by_code[code] = i + 1
        for i, e in enumerate(entries):
            code = e.get("server_code") or ""
            seq = seq_by_code.get(code)
            target = f"序号 {seq}" if seq else "⚠️ 服务器当前不可见或已删除"
            ts = int(e.get("last_ts") or 0)
            time_text = format_unlock_time(ts) if ts > 0 else "等待同步"
            state = "已提醒" if (ts > 0 and int(e.get("fired_ts") or 0) == ts) else "待触发"
            lines.append(
                f"{i + 1}. {e.get('boss_name') or e.get('boss_key')}"
                f" —— `{target}`（{e.get('server_name') or code[:8]}）"
                f" · 解锁前 {e.get('minutes')} 分钟 · {time_text} · {state}"
            )
        lines += ["", "> 取消：`取消进度解锁提醒 <服务器序号> <boss名>`"]
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_progress_unlock_cancel(self, message, text: str, gid):
        """取消进度解锁提醒 <序号> <boss名>：删除本群的一条解锁提醒（管理员及以上）"""
        rest = text[len("取消进度解锁提醒"):].lstrip("：: \t").strip()
        seq, tail = parse_server_index(rest)
        if not seq:
            await self._reply_markdown(message, "## ꧁༺ 取消进度解锁提醒 ༻꧂\n\n"
                                       "**❌ 请带上服务器序号喵：`取消进度解锁提醒 <序号> <boss名>`**")
            return
        boss_key = resolve_boss(tail)
        if not boss_key:
            await self._reply_markdown(message, "## ꧁༺ 取消进度解锁提醒 ༻꧂\n\n"
                                       f"**❌ 无法识别的 boss 名：`{tail or '（空）'}`**\n\n"
                                       "> 可发送 `进度解锁提醒列表` 查看本群已设定的提醒")
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "## ꧁༺ 取消进度解锁提醒 ༻꧂\n\n"
                                       f"**❌ 找不到序号 {seq} 的服务器喵...**")
            return
        ok, msg = self.progress_unlocks.remove(gid, self._vote_server_code(rec), boss_key)
        if not ok:
            await self._reply_markdown(message, "## ꧁༺ 取消进度解锁提醒 ༻꧂\n\n**❌ " + msg + "**")
            return
        await self._reply_markdown(message, "## ꧁༺ 取消进度解锁提醒 ༻꧂\n\n"
                                   f"**✅ 已取消：序号 {seq} 服务器 · {boss_cn(boss_key)} 的解锁前提醒喵**")

    # ───────────────────────── 进度解锁提醒后台调度 ─────────────────────────
    async def progress_unlock_scheduler(self):
        """后台轮询（每 60 秒）：同步各服务器解锁时间 → 判定触发窗口 → 推送卡片图。

        计时完全在机器人侧（不依赖游戏主循环，空服也不受影响）。
        """
        _log.info("进度解锁提醒轮询已启动")
        while True:
            try:
                await self._progress_unlock_tick()
            except Exception as e:
                _log.exception("进度解锁提醒轮询出错: %s", e)
            await asyncio.sleep(60)

    async def _progress_unlock_tick(self):
        """单轮判定：① 在线服务器同步 last_ts；② 对进入触发窗口的提醒发卡片并标记。"""
        codes = self.progress_unlocks.server_codes()
        if not codes:
            return
        for code in codes:
            ok, data = await self.zse_server.request_progress(code, timeout=10)
            if ok and isinstance(data, dict) and not data.get("is_text"):
                ts_map = data.get("boss_lock_ts")
                if isinstance(ts_map, dict) and ts_map:
                    self.progress_unlocks.sync_server(code, ts_map)
        now = time.time()
        for gid, entry in self.progress_unlocks.all_entries():
            ts = int(entry.get("last_ts") or 0)
            if ts <= 0 or now >= ts:
                continue  # 无解锁时间 / 已过解锁点：不补发（错过整个窗口不发）
            if int(entry.get("fired_ts") or 0) == ts:
                continue  # 本轮已提醒过（幂等：解锁时间变化后自动重新武装）
            minutes = int(entry.get("minutes") or 0)
            if now < ts - minutes * 60:
                continue  # 未进入触发窗口
            boss_key = entry.get("boss_key") or ""
            # 先标记再发送：发送异常也不重复刷屏（宁可少发不重复）
            self.progress_unlocks.mark_fired(gid, entry.get("server_code") or "", boss_key, ts)
            mins_left = max(1, int(round((ts - now) / 60)))
            try:
                png = render_unlock_card(
                    boss_key,
                    minutes_left=mins_left,
                    unlock_ts=ts,
                    server_name=entry.get("server_name") or "",
                    bg_dir=self._progress_bg_dir(),
                )
                await send_group_image(self, gid, png, filename="progress_unlock.png")
                _log.info("进度解锁提醒已发送 group=%s boss=%s 剩余 %s 分钟",
                          (gid or "")[:8], boss_key, mins_left)
            except Exception as e:
                _log.warning("进度解锁提醒发送失败 group=%s boss=%s: %s",
                             (gid or "")[:8], boss_key, e)

    async def cmd_reset(self, message, text: str, gid):
        """重置 [序号]：① 导出存档 → ② 写入投票种子（有未使用结果时）→ ③ 触发重置 → ④ 推送存档 zip"""
        rest = text[len("重置"):].lstrip("：: \t").strip()
        seq, _ = parse_server_index(rest)
        if rest and (seq is None or seq < 1):
            await self._reply_markdown(message, "## ꧁༺ 重置 ༻꧂\n\n"
                                       "**❌ 序号无效喵...**\n\n> 格式：`重置 <序号>`，例：`重置 1`")
            return
        server_seq = seq or 1
        rec = self._vote_server(gid, server_seq)
        if rec is None:
            await self._reply_markdown(message, "## ꧁༺ 重置 ༻꧂\n\n"
                                       f"**❌ 找不到序号 {server_seq} 的服务器喵...**")
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "## ꧁༺ 重置 ༻꧂\n\n**❌ 该服务器标识无效喵...**")
            return
        title = "## ꧁༺ 重置 ༻꧂\n\n"
        # ① 导出存档（失败/异常 → 中止重置，可重试）
        ok, data = await self.zse_server.request_archive_export(server_code, timeout=300)
        if not ok or not isinstance(data, dict) or data.get("error") or not data.get("base64"):
            await self._reply_markdown(message, title +
                f"**❌ 存档导出失败，已中止重置喵...**\n\n> 原因：{self._reason_of(data)}\n> 可稍后重试")
            return
        zip_name = data.get("name") or "archive.zip"
        # ①.5 强制结束该服务器进行中的投票并结算（重置会重建世界，票不该继续投）
        closed_note = ""
        try:
            closed_vote = await self._force_close_vote(server_code)
            if closed_vote:
                wno = closed_vote.get("winner_no")
                wopt = next((o for o in (closed_vote.get("options") or [])
                             if int(o.get("no") or -1) == int(wno or -1)), None)
                closed_note = "\n- 已强制结算进行中的投票" + (
                    f"（获胜：{wopt.get('name')}）" if wopt else "（本场无有效投票）")
        except Exception as e:
            _log.warning("重置时强制结算投票失败: %s", e)
        # ② 有「已结束且未使用」的投票结果 → set_seed 写入获胜种子 + 标记已使用；无则走插件预设
        pending = self.votes.pending_result(server_code)
        pending_vote = self.votes.get_vote(pending["vote_id"]) if pending else None
        seed_source = "插件预设/随机"
        if pending and pending.get("winner"):
            winner = pending["winner"]
            opt = next((o for o in (pending_vote or {}).get("options") or []
                        if int(o.get("no")) == int(winner.get("no"))), None)
            if opt:
                seed_str = "|".join(opt.get("seeds") or [])
                ok2, d2 = await self.zse_server.request_auto_reset(
                    server_code, "set_seed", seed=seed_str, timeout=15)
                if not ok2 or (isinstance(d2, dict) and (d2.get("error") or d2.get("ok") is False)):
                    await self._reply_markdown(message, title +
                        f"**❌ 写入投票种子失败，已中止重置喵...**\n\n> 原因：{self._reason_of(d2)}\n"
                        f"> 存档 zip 已保留在服务器本地（{zip_name}）")
                    return
                self.votes.mark_result_used(server_code)
                seed_source = f"投票获胜（{opt.get('name')}）"
        # ③ 触发重置
        ok3, d3 = await self.zse_server.request_auto_reset(server_code, "do_reset", timeout=15)
        if not ok3 or (isinstance(d3, dict) and (d3.get("error") or d3.get("ok") is False)):
            await self._reply_markdown(message, title +
                f"**❌ 触发重置失败喵...**\n\n> 原因：{self._reason_of(d3)}\n"
                f"> 存档 zip 已保留在服务器本地（{zip_name}）")
            return
        # ④ 解码 zip 到临时文件并推送到该投票/服务器联合区所有群（实时计算）
        if pending_vote:
            targets = self._vote_target_gids(pending_vote)
        else:
            targets = sorted(self.registry.zone_gids(gid) or {gid})
        ok_cnt, failed, zip_path = 0, [], None
        try:
            fd, zip_path = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            with open(zip_path, "wb") as f:
                f.write(decode_archive_zip(data["base64"]))
            for g in targets:
                try:
                    await send_group_file(self, g, zip_path, zip_name, file_type=4)
                    ok_cnt += 1
                except Exception as e:
                    _log.warning("存档 zip 推送失败 group=%s: %s", (g or "")[:8], e)
                    failed.append(g)
        except Exception as e:
            _log.exception("存档 zip 解码/推送失败: %s", e)
            await self._reply_markdown(message, title +
                f"**⚠️ 重置已触发，但存档 zip 推送失败喵...**\n\n> 原因：{e}\n"
                f"> 存档 zip 已保留在服务器本地（{zip_name}）")
            return
        finally:
            if zip_path:
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
        await self._reply_markdown(message, title +
            f"**✅ 重置已触发**\n\n"
            f"- 种子来源：{seed_source}\n"
            f"- 存档推送：成功 {ok_cnt}/{len(targets)} 个群\n"
            f"- 存档文件：`{zip_name}`" + closed_note + self._vote_failed_line(failed))

    # ───────────────────────── 喵币（签到 / 积分 / 排行 / 账单） ─────────────────────────
    def _econ_bound_openids(self, gid) -> set:
        """**主群**已绑定白名单的 openid 集合（榜单过滤；与签到资格口径一致）"""
        out = set()
        for rec in (self.whitelist_store._data.get(self._eff_gid(gid), {}) or {}).values():
            oid = (rec or {}).get("bind_openid") or ""
            if oid:
                out.add(oid)
        return out

    def _econ_openid_of_name(self, gid, name: str) -> str:
        """按玩家名反查绑定人 openid（本联合体系内）"""
        for sg in sorted(self.registry.zone_gids(gid)):
            rec = self.whitelist_store.get_record(sg, name)
            if rec and rec.get("bind_openid"):
                return rec["bind_openid"]
        return ""

    def _econ_all_my_names(self, user_openid: str) -> list:
        """在全部已知群里找该 openid 绑定的玩家名（查看类用）"""
        out = []
        for sg, recs in list((self.whitelist_store._data or {}).items()):
            for nm, rec in (recs or {}).items():
                if (rec or {}).get("bind_openid") == user_openid and nm not in out:
                    out.append(nm)
        return out
    def _econ_bound_names(self, gid) -> list:
        """本联合体系内已绑定白名单的玩家名（积分排行过滤用）"""
        names = []
        for sg in sorted(self.registry.zone_gids(gid)):
            for nm in (self.whitelist_store._data.get(sg, {}) or {}):
                if nm not in names:
                    names.append(nm)
        return names

    def _econ_my_names(self, gid, user_openid: str) -> list:
        """某个 openid 在**联合体主群**绑定的玩家名（与"能否进服"口径一致）。
        必须真的是本人绑定：老记录 bind_openid 为空时 _login_authorized 会放行任何人，
        签到不能沿用该兜底，否则任何人都能拿别人的名字签到。"""
        eff = self._eff_gid(gid)
        mine = []
        for nm, rec in (self.whitelist_store._data.get(eff, {}) or {}).items():
            if nm in mine or not (rec or {}).get("bind_openid"):
                continue
            if self._login_authorized(eff, nm, user_openid, gid):
                mine.append(nm)
        return mine

    async def _econ_avatar_bytes(self, openid: str, size: int = 640) -> bytes:
        """下载 QQ 头像（qlogo 直连，无需额外接口权限）；带内存缓存，失败返回空（卡片回落首字）"""
        if not self._appid or not openid:
            return b""
        cache = getattr(self, "_avatar_cache", None)
        if cache is None:
            cache = self._avatar_cache = {}
        if openid in cache:
            return cache[openid]
        data = b""
        # qlogo 的 qqapp 接口只对特定尺寸有效（实测 640 可用，200/100 返回 400）→ 逐个尝试
        for _sz in (640, 0, 100):
            url = "https://q.qlogo.cn/qqapp/" + str(self._appid) + "/" + openid + "/" + str(_sz)
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status == 200:
                            data = await r.read()
                            break
            except Exception as e:
                _log.debug("头像下载失败 %s(size=%s): %s", openid[:8], _sz, e)
        if data:
            cache[openid] = data
        return data

    def _econ_email(self, gid, name: str) -> str:
        rec = self.whitelist_store.get_record(self._eff_gid(gid), name) or {}
        return rec.get("email") or "（未记录）"

    def _playtime_map(self) -> dict:
        """在线时长：内存 → 文件缓存 → **直读同机 TShock SQLite**（三级，命令路径当场可得，不依赖调度/WS）"""
        if self._playtime:
            return self._playtime
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playtime_cache.json")
        try:
            import json as _json
            with open(_p, "r", encoding="utf-8") as f:
                d = _json.load(f)
            if isinstance(d, dict) and d:
                self._playtime = {str(k): int(v or 0) for k, v in d.items()}
                return self._playtime
        except Exception:
            pass
        # 直读插件写入的表（机器人与插件同机，最可靠）
        try:
            import sqlite3 as _sq
            _dbp = "C:/TShockServer/server/tshock/tshock.sqlite"
            if os.path.exists(_dbp):
                _c = _sq.connect(_dbp)
                d = {str(r[0]): int(r[1] or 0) for r in _c.execute("SELECT account_name, seconds FROM zse_playtime")}
                _c.close()
                if d:
                    _log.info("playtime read from sqlite: %d accounts", len(d))
                    self._playtime = d
                    return d
        except Exception as e:
            _log.warning("playtime sqlite read failed: %s", e)
        return {}

    def _econ_playtime_text(self, name: str) -> str:
        sec = int(self._playtime_map().get(name) or 0)
        if sec <= 0:
            return "统计中（需在服内累计）"
        h, rem = divmod(sec, 3600)
        mnt = rem // 60
        return (str(h) + " 小时 " + str(mnt) + " 分") if h else (str(mnt) + " 分")

    def _econ_info_rows(self, gid, name: str, earned: int, sign_state: str, streak: int,
                        sign_ts: int, today_rank: int, rec: dict | None = None, extra: list | None = None) -> list:
        rec = rec if rec is not None else (self.whitelist_store.get_record(self._eff_gid(gid), name) or {})

        def _fmt(ts):
            return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "（暂无）"

        ts_txt = time.strftime("%m-%d %H:%M:%S", time.localtime(sign_ts)) if sign_ts else "（今天还没签到）"
        rank_txt = ("今天第 " + str(today_rank) + " 位签到") if today_rank else "—"
        rows = [
            ("玩家名", name),
            ("QQ邮箱", rec.get("email") or "（未记录）"),
            ("签到情况", sign_state),
            ("总签到积分", str(earned)),
            ("签到时间", ts_txt),
            ("签到排名", rank_txt),
            ("连签天数", str(streak) + " 天"),
            ("总在线时长", self._econ_playtime_text(name)),
            ("在线奖励", "每满 1 小时 +" + str(PLAYTIME_PER_HOUR) + "（已结算 " + str(self.economy.last_play_hour(rec.get("bind_openid") or "")) + " 小时）"),
            ("绑定时间", _fmt(rec.get("bind_time"))),
            ("最后进服", _fmt(rec.get("last_join_time"))),
        ]
        if extra:
            rows[3:3] = list(extra)
        if rec.get("frozen"):
            rows.append(("白名单", "已冻结（重新入群自动解冻）"))
        return rows
    # ── 从 wip 分支补齐：卡片渲染依赖的辅助方法 ──
    def _econ_day_start(self) -> int:
        """北京时间今天 00:00 的时间戳（用于统计今日签到顺序）"""
        now = time.time() + 8 * 3600
        return int(now - (now % 86400) - 8 * 3600)

    def _bg_dir_safe(self):
        """背景目录兜底：取不到就返回 None（渲染器会用纯色背景，绝不让整卡失败）"""
        try:
            return self._vote_bg_dir()
        except Exception as e:
            _log.warning("背景目录获取失败，改用纯色: %s", e)
            return None

    def _settle_playtime(self):
        """在线时长结算：每满 1 小时 +PLAYTIME_PER_HOUR 喵币。
        幂等键 = (openid, 累计整小时数)：重启、重复执行、多服汇总都不会多发。"""
        if not self._playtime:
            return
        # 玩家名 → openid（一个人多个角色则时长相加；同一名字在多群只计一次）
        by_oid = {}
        seen = set()
        for recs in (self.whitelist_store._data or {}).values():
            for nm, rec in (recs or {}).items():
                oid = (rec or {}).get("bind_openid") or ""
                if not oid or (oid, nm) in seen:
                    continue
                sec = int(self._playtime_map().get(nm) or 0)
                if sec <= 0:
                    continue
                seen.add((oid, nm))
                cur = by_oid.setdefault(oid, [0, nm])
                cur[0] += sec
                if not cur[1]:
                    cur[1] = nm
        paid = 0
        for oid, (sec, nm) in by_oid.items():
            hours = sec // 3600
            if hours <= 0:
                continue
            done = self.economy.last_play_hour(oid)
            todo = min(hours - done, PLAYTIME_PER_CYCLE_MAX)
            for h in range(done + 1, done + 1 + todo):
                ok, _msg, _bal = self.economy.add(oid, PLAYTIME_PER_HOUR, "playtime",
                                                 "playtime:" + oid + ":" + str(h), 1000, nm)
                if ok:
                    paid += PLAYTIME_PER_HOUR
        if paid:
            _log.info("在线时长结算：本轮发放 %s 喵币", paid)

    def _econ_today(self) -> str:
        """机器人侧按北京时间（UTC+8）算自然日"""
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))

    def _econ_pick(self, gid, user_openid: str, want: str, strict: bool = True):
        """选定要操作的玩家名；返回 (name, err)"""
        names = self._econ_my_names(gid, user_openid)
        if want:
            if want in names:
                return want, ""
            return "", (f"`{want}` 不是你绑定的玩家名喵\n"
                        f"> 你绑定的是：{('、'.join(names) if names else '（无）')}")
        if not names:
            return "", ("还没有绑定白名单喵，签到需要先绑定\n"
                        "> `绑定 <QQ号>` → 邮箱里收到的验证码 → `添加白名单 <玩家名> <验证码>`")
        if len(names) > 1:
            return "", (f"你绑定了多个玩家名，请指定一个：\n> {('、'.join(names))}\n"
                        "> 用法：`签到 <玩家名>`")
        return names[0], ""

    async def cmd_sign(self, message, text: str, gid, user_openid: str = ""):
        """签到 [玩家名]：需已绑定白名单；基础 15~35 + 连续奖励 3~9（第 2 天起）+ 里程碑"""
        try:
            self._settle_playtime()   # 在线奖励：每满 1 小时 +PLAYTIME_PER_HOUR（幂等）
        except Exception as _e:
            _log.warning("在线奖励结算失败: %s", _e)
        title = self.build_card_title("签到")
        rest = text[len("签到"):].lstrip("：: \t").strip()
        want = rest.split()[0] if rest.split() else ""
        name, err = self._econ_pick(gid, user_openid, want)
        if err:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ {err}**"]))
            return
        today = self._econ_today()
        base = random.randint(SIGN_BASE[0], SIGN_BASE[1])
        bonus = random.randint(SIGN_BONUS[0], SIGN_BONUS[1])
        ok, msg, bal, streak, amt, extra = self.economy.sign(user_openid, today, base, bonus, name)
        if not ok:
            row = self.economy.get(user_openid)
            await self._reply_markdown(message, "\n".join([
                title, "", f"**{msg}**", "",
                f"> 余额：**{row['balance']}** 喵币｜连续 **{row['streak']}** 天",
            ]))
            return
        # 图片卡（渲染/发送失败则回落到下面的文字卡）
        try:
            _send_gid = gid or (getattr(message, "group_openid", None) or "")
            _state = "本次 +" + str(amt) + " 喵币" + (("（含连续/里程碑奖励 +" + str(extra) + "）") if extra else "")
            _rows = self._econ_info_rows(
                gid, name, int(self.economy.get(user_openid).get("total_earned") or 0), _state, streak,
                self.economy.last_sign_ts(user_openid), self.economy.today_rank(user_openid, self._econ_day_start()))
            _png = render_info_card(
                name, _rows,
                avatar_bytes=await self._econ_avatar_bytes(user_openid),
                banner="签到成功  ·  +" + str(amt) + " 喵币",
                subtitle=self._econ_email(gid, name) + " · " + today,
                badges=[("连续签到第 " + str(streak) + " 天", "gold"),
                        (str(self.economy.get(user_openid).get("total_earned") or 0) + " 喵币", "blue"),
                        ("已签到", "green")],
                footer="Generated by ZSE StarBot", bg_dir=self._vote_bg_dir())
            if _png:
                await send_group_image(self, _send_gid, _png, filename="sign.jpg",
                                       msg_id=getattr(message, "id", None))
                return
        except Exception as e:
            _log.warning("签到卡渲染/发送失败，回落文字卡: %s", e)
        lines = [title, "", f"**🐾 签到成功！本次 +{amt} 喵币**", ""]
        if extra:
            lines.append(f"- 其中连续/里程碑奖励：**+{extra}**")
        lines += [f"- 连续签到：**{streak}** 天",
                  f"- 当前余额：**{bal}** 喵币",
                  f"- 余额排名：第 **{self.economy.rank_of(user_openid)}** 名",
                  "", f"> 自然日按北京时间算：{today}"]
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_my_points(self, message, text: str, gid, user_openid: str = ""):
        """我的信息 [玩家名]：不带参数=查自己；带参数=查该玩家（= 原「玩家查询」，联合体总群口径）"""
        try:
            self._settle_playtime()   # 在线奖励：每满 1 小时 +PLAYTIME_PER_HOUR（幂等）
        except Exception as _e:
            _log.warning("在线奖励结算失败: %s", _e)
        title = self.build_card_title("我的信息")
        _mp = next((p for p in ("我的信息", "我的积分", "我的喵币", "玩家查询", "信息查询") if text.startswith(p)), "我的信息")
        rest = text[len(_mp):].lstrip("：: \t").strip()
        want = rest.split()[0] if rest.split() else ""
        eff = self._eff_gid(gid)
        if not want:
            _names = self._econ_my_names(gid, user_openid) or self._econ_all_my_names(user_openid)
            if not _names:
                await self._reply_markdown(message, "\n".join([
                    title, "",
                    "**还没有绑定白名单喵**",
                    "> `绑定 <QQ号>` → 邮箱里收到的验证码 → `添加白名单 <玩家名> <验证码>`",
                ]))
                return
            name = _names[0]   # 钱包按人（openid）算，多个名字取第一个即可
            target_oid = user_openid
            rec = self.whitelist_store.get_record(eff, name) or {}
        else:
            name = want
            rec = self.whitelist_store.get_record(eff, name)
            if rec is None:
                await self._reply_markdown(message, "\n".join([
                    title, "", f"**❌ 未找到 `{name}` 的绑定记录喵**",
                    "> 请确认玩家名正确，且已在总群绑定白名单",
                ]))
                return
            target_oid = rec.get("bind_openid") or ""
        row = self.economy.get(target_oid or "")
        mine = (not want) or (target_oid == user_openid)
        # 图片卡（失败回落文字）
        try:
            _send_gid = gid or (getattr(message, "group_openid", None) or "")
            _today = self._econ_today()
            _signed = bool(row.get("last_sign_date")) and row.get("last_sign_date") == _today
            _state = ("今天已签到（" + str(row.get("last_sign_date") or "") + "）" if _signed else "今天还没签到")
            _extra = [("喵币余额", str(row.get("balance") or 0)), ("累计消费", str(row.get("total_spent") or 0))]
            _rows = self._econ_info_rows(
                gid, name, int(row.get("total_earned") or 0), _state, int(row.get("streak") or 0),
                self.economy.last_sign_ts(target_oid or ""),
                self.economy.today_rank(target_oid or "", self._econ_day_start()) if target_oid else 0,
                rec=rec, extra=_extra)
            _png = render_info_card(
                name, _rows,
                avatar_bytes=await self._econ_avatar_bytes(target_oid or ""),
                banner=("今日已签到" if _signed else "今天还没签到"),
                subtitle=(rec.get("email") or "未绑定邮箱") + " · " + _today,
                badges=[("连续签到第 " + str(int(row.get("streak") or 0)) + " 天", "gold"),
                        (str(row.get("balance") or 0) + " 喵币", "blue"),
                        ("已签到" if _signed else "未签到", "green" if _signed else "plain")],
                footer="Generated by ZSE StarBot", bg_dir=self._vote_bg_dir())
            if _png:
                await send_group_image(self, _send_gid, _png, filename="myinfo.jpg",
                                       msg_id=getattr(message, "id", None))
                return
        except Exception as e:
            _log.warning("我的信息卡渲染/发送失败，回落文字卡: %s", e)
        lines = [title, "", f"**{name}**", "",
                 f"- 喵币余额：**{row['balance']}**",
                 f"- 累计获取：{row['total_earned']}｜累计消费：{row['total_spent']}"]
        if row["last_sign_date"]:
            lines.append(f"- 连续签到：**{row['streak']}** 天（上次 {row['last_sign_date']}）")
        else:
            lines.append("- 连续签到：还没签到过")
        lines.append(f"- 绑定邮箱：{rec.get('email') or '（未绑定邮箱）'}")
        lines.append(f"- 绑定时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(rec.get('bind_time') or 0)) if rec.get('bind_time') else '（暂无）'}")
        lines.append(f"- 最后进服：{time.strftime('%Y-%m-%d %H:%M', time.localtime(rec.get('last_join_time') or 0)) if rec.get('last_join_time') else '（暂无）'}")
        await self._reply_markdown(message, "\n".join(lines))
    async def cmd_points_rank(self, message, text: str, gid, user_openid: str = ""):
        _log.info("RANK_ENTER")
        """积分排行 [累计|在线] [页码]：本联合体系（主群口径）内已绑定玩家的榜单"""
        title = self.build_card_title("积分排行")
        _rp = next((p for p in ("积分排行", "签到排行", "喵币排行") if text.startswith(p)), "积分排行")
        rest = text[len(_rp):].lstrip("：: \t").strip()
        by_play = ("在线" in rest) or ("时长" in rest)
        by = "earned" if ("累计" in rest or "earned" in rest.lower()) else "balance"
        toks = [t for t in rest.split() if t.isdigit()]
        page = max(1, int(toks[0]) if toks else 1)
        per = 20
        my_name = (self._econ_my_names(gid, user_openid) or [""])[0]
        if by_play:
            # 在线时长榜：名字做归一化匹配（去空格/忽略大小写），避免白名单名与账号名细微差异导致"明明有时长却显示无数据"
            _norm = lambda x: (x or "").strip().casefold()
            bound_names = {_norm(x) for x in self._econ_bound_names(gid)}
            _all = sorted(((nm, int(s or 0)) for nm, s in self._playtime_map().items()), key=lambda x: -x[1])
            ranked = [(nm, s) for nm, s in _all if _norm(nm) in bound_names]
            _log.info("RANK_PLAY all=%d bound=%d ranked=%d", len(_all), len(bound_names), len(ranked))
            if not ranked and _all:
                ranked = _all   # 兜底：对不上就先展示全部（至少有数据可看），便于定位
            sub = "在线时长榜"
            value_of = lambda x: self._econ_playtime_text(x[0])
        else:
            bound = set(self._econ_bound_openids(gid))
            rows_all = [r for r in self.economy.top(by, 0, 200) if r["openid"] in bound]
            ranked = [(r.get("name") or "?", int((r.get("total_earned") if by == "earned" else r.get("balance")) or 0))
                      for r in rows_all]
            sub = "累计获取榜" if by == "earned" else "余额榜"
            value_of = lambda x: str(x[1])
            my_rank = next((i + 1 for i, r in enumerate(rows_all) if r["openid"] == user_openid), 0)
        if by_play:
            my_rank = next((i + 1 for i, x in enumerate(ranked) if my_name and x[0] == my_name), 0)
        total = len(ranked)
        if total == 0:
            await self._reply_markdown(message, "\n".join([
                title, "",
                ("**在线时长还没统计数据喵**（需有人在服内玩满 1 分钟）" if by_play else "**本群还没有可展示的喵币榜单喵...**"),
            ]))
            return
        page_items = ranked[(page - 1) * per: page * per]
        if not page_items:
            await self._reply_markdown(message, "\n".join([title, "", f"**第 {page} 页是空的喵**（共 {total} 人）"]))
            return
        items = [((page - 1) * per + i + 1, x[0], value_of(x)) for i, x in enumerate(page_items)]
        total_pages = max(1, (total + per - 1) // per)
        rank_txt = ("你的排名：第 " + str(my_rank) + " 名") if my_rank else "你还没上榜"
        try:
            _send_gid = gid or (getattr(message, "group_openid", None) or "")
            _png = render_rank_card(items, title=(sub if by_play else ("累计获取榜" if by == "earned" else "积分排行")),
                                    subtitle=(sub + " · " + rank_txt), page=page, total_pages=total_pages,
                                    value_label=("时长" if by_play else ("喵币·累计" if by == "earned" else "喵币·余额")),
                                    footer="Generated by ZSE StarBot · 翻页：积分排行 " + str(page + 1),
                                    bg_dir=self._bg_dir_safe())
            if _png:
                _log.info("RANK_IMG ok bytes=%d", len(_png or b""))
                try:
                    await asyncio.wait_for(send_group_image(self, _send_gid, _png, filename="econ_rank.jpg",
                                                          msg_id=getattr(message, "id", None)), timeout=25)
                except Exception as e1:
                    _log.warning("带参数发图失败(%s)，改用朴素发法: %s", "econ_rank", e1)
                    await asyncio.wait_for(send_group_image(self, _send_gid, _png), timeout=25)
                return
        except Exception as e:
            _log.warning("积分排行卡渲染/发送失败，回落文字卡: %s", e)
        lines = [title, "", f"**{sub}**（共 {total} 人 · 第 {page}/{total_pages} 页）", f"> {rank_txt}", ""]
        medal = ["🥇", "🥈", "🥉"]
        for rank, nm, val in items:
            tag = medal[rank - 1] if rank <= 3 else f"`{rank:>2}`"
            lines.append(f"- {tag} **{nm}**　{val}")
        lines.append("")
        lines.append("> 翻页：`积分排行 " + str(page + 1) + "`" + ("｜切换：`积分排行`" if by_play else "｜切换：`积分排行 在线`"))
        await self._reply_markdown(message, "\n".join(lines))

    async def vote_scheduler(self):
        """后台循环：约 60 秒一轮，扫描 due_updates（重发 open 卡）/ due_closes（自动截止发结果卡）。
        循环内异常必须捕获，不能中断整个调度。"""
        _log.info("种子投票调度已启动（间隔 60 秒）")
        while True:
            await asyncio.sleep(60)
            try:
                await self._vote_tick()
            except Exception as e:
                _log.exception("种子投票调度出错: %s", e)
            # 累计在线时长汇总（各服上报 → 按账号取最大，永不重置）
            if time.time() - self._playtime_ts > 300:
                self._playtime_ts = time.time()
                try:
                    # 枚举所有已登记服务器：兼容 dict-of-list / dict-of-dict / list 等存放形状（不再只依赖 all_records）
                    _recs = []
                    _raw = getattr(self.zse_server, "_data", None) or {}
                    _pool = list(_raw.values()) if isinstance(_raw, dict) else list(_raw)
                    for _v in _pool:
                        if isinstance(_v, dict) and _v.get("code"):
                            _recs.append(_v)
                        elif isinstance(_v, dict):
                            _recs.extend(x for x in _v.values() if isinstance(x, dict) and x.get("code"))
                        elif isinstance(_v, list):
                            _recs.extend(x for x in _v if isinstance(x, dict) and x.get("code"))
                    _recs.extend(r for r in (self.zse_server.all_records() or []) if isinstance(r, dict) and r.get("code"))
                    _seen = set()
                    _recs = [r for r in _recs if not (r.get("code") in _seen or _seen.add(r.get("code")))]
                    _log.info("在线时长：待查询服务器 %d 台", len(_recs))
                    _pt = {}
                    for _rec in _recs:
                        _code = _rec.get("code")
                        if not _code:
                            continue
                        _ok, _d = await self.zse_server.request_playtime(_code, timeout=15)
                        if not _ok:
                            _log.warning("在线时长请求失败(%s): %s", _code[:8], _d)
                        # 回包结构做宽容处理：递归查找"账号-秒数"列表
                        _items = _find_account_items(_d) if isinstance(_d, (dict, list)) else None
                        if not isinstance(_items, list):
                            _items = []
                            _log.warning("在线时长回包未找到账号列表(%s): %s", _code[:8], str(_d)[:200])
                        for _it in _items:
                            _acc = (_it or {}).get("account") or (_it or {}).get("name")
                            _sec = int((_it or {}).get("seconds") or (_it or {}).get("sec") or 0)
                            if _acc:
                                _pt[_acc] = max(_pt.get(_acc, 0), _sec)
                    if not _pt:
                        # 兜底：WS 没取到就直读同机 TShock 的 SQLite（插件写入的 zse_playtime 表）
                        try:
                            import sqlite3 as _sq
                            _dbp = "C:/TShockServer/server/tshock/tshock.sqlite"
                            if os.path.exists(_dbp):
                                _c = _sq.connect(_dbp)
                                for _r in _c.execute("SELECT account_name, seconds FROM zse_playtime"):
                                    _nm2 = str(_r[0])
                                    _pt[_nm2] = max(_pt.get(_nm2, 0), int(_r[1] or 0))
                                _c.close()
                                _log.info("playtime fallback(sqlite): %d accounts", len(_pt))
                        except Exception as e:
                            _log.warning("在线时长兜底读取失败: %s", e)
                    if _pt:
                        try:
                            _k0 = next(iter(_pt))
                            _log.info("playtime cache: keys=%d firstKeyLen=%d firstSec=%d", len(_pt), len(str(_k0)), int(_pt[_k0]))
                        except Exception:
                            pass
                        _log.info("在线时长汇总：%d 个账号（取最大）", len(_pt))
                        self._playtime = _pt
                        try:
                            import json as _json
                            _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playtime_cache.json")
                            with open(_p, "w", encoding="utf-8") as f:
                                _json.dump(_pt, f, ensure_ascii=False)
                            _log.info("playtime cache saved: %d entries", len(_pt))
                        except Exception as e:
                            _log.warning("在线时长缓存落盘失败: %s", e)
                        self._settle_playtime()
                except Exception as e:
                    _log.warning("在线时长汇总失败: %s", e)
            # 白名单新表回填（第 1 步：只写不读，每 5 分钟一次，幂等 upsert）
            if time.time() - self._wl_sync_ts > 300:
                self._wl_sync_ts = time.time()
                try:
                    _st = self.whitelist_users.sync(
                        self.whitelist_store._data,
                        devices_of=self.whitelist_store._devices)
                    _log.info("白名单新表回填：用户 %s、设备 %s、城市 %s、无绑定人跳过 %s",
                              _st["users"], _st["devices"], _st["cities"], _st["skipped_no_openid"])
                except Exception as e:
                    _log.warning("白名单新表回填失败: %s", e)
            # 喵币：冻结超过 7 天的账号清零（每 6 小时扫一次，条件天然幂等）
            if time.time() - self._econ_clear_ts > 6 * 3600:
                self._econ_clear_ts = time.time()
                try:
                    _n = self.economy.clear_frozen(7)
                    if _n:
                        _log.info("喵币：已清零 %d 个冻结超过 7 天的账号", _n)
                except Exception as e:
                    _log.warning("喵币冻结清零失败: %s", e)
            try:
                await self._server_status_tick()
            except Exception as e:
                _log.exception("服务器状态通知出错: %s", e)

    @staticmethod

    async def cmd_econ_grant(self, message, text: str, gid, user_openid: str = ""):
        """发币 <玩家名> <数量> [原因] / 扣币 <玩家名> <数量> [原因]（高级管理员）"""
        is_add = text.startswith("发币")
        label = "发币" if is_add else "扣币"
        title = self.build_card_title(label)
        parts = text[len(label):].lstrip("：: \t").split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await self._reply_markdown(message, "\n".join([
                title, "", f"**格式：** `{label} <玩家名> <数量> [原因]`", "",
                f"> 例：`{label} 星梦 100 活动奖励`",
            ]))
            return
        target, amount = parts[0], int(parts[1])
        reason = " ".join(parts[2:]) or ("admin_grant" if is_add else "admin_deduct")
        if amount <= 0:
            await self._reply_markdown(message, "\n".join([title, "", "**数量必须为正喵**"]))
            return
        bound = set(self._econ_bound_names(gid))
        if target not in bound:
            await self._reply_markdown(message, "\n".join([
                title, "", f"**❌ `{target}` 不在本联合体系的白名单里喵**",
                "> 为避免打错名字发错人，只能对本联合体系内已绑定的玩家操作",
            ]))
            return
        if is_add:
            ok, msg, bal = self.economy.add(target, amount, reason, "", MAX_ADD)
        else:
            ok, msg, bal, _ = self.economy.spend(target, amount, reason, "")
        if not ok:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ {msg}**"]))
            return
        _log.warning("[喵币%d] %s %s %d（原因：%s）→ 余额 %s",
                     1 if is_add else 2, self._disp(gid, user_openid), label, amount, reason, bal)
        await self._reply_markdown(message, "\n".join([
            title, "", f"**✅ 已{label}**", "",
            f"- 玩家：**{target}**", f"- 数量：{amount}",
            f"- 原因：{reason}", f"- 变动后余额：**{bal}**",
        ]))

    async def cmd_econ_reset(self, message, text: str, gid, user_openid: str = ""):
        """重置经济 确认：把所有喵币余额清零（保留流水，高级管理员）"""
        title = self.build_card_title("重置经济")
        if "确认" not in text:
            await self._reply_markdown(message, "\n".join([
                title, "", "**⚠️ 会把所有人的喵币余额清零（流水保留）**", "",
                "> 确认请发送：`重置经济 确认`",
            ]))
            return
        n = self.economy.reset_all()
        _log.warning("[喵币] %s 执行了重置经济，影响 %s 个账号", self._disp(gid, user_openid), n)
        await self._reply_markdown(message, "\n".join([
            title, "", f"**✅ 已清零 {n} 个账号的喵币**", "> 历史流水保留，可用 `账单` 查看",
        ]))

    async def cmd_backup_list(self, message, text: str, gid, user_openid: str = ""):
        """备份列表 [序号]：列出服务器上的备份（编号/时间/大小），编号供 回退备份 使用"""
        title = self.build_card_title("备份列表")
        rest = text[len("备份列表"):].lstrip("：: \t").strip()
        if rest.startswith("列表"):
            rest = rest[len("列表"):].strip()
        seq, _tail = parse_server_index(rest)
        seq = seq or 1
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ 找不到序号 {seq} 的服务器喵...**"]))
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "\n".join([title, "", "**❌ 该服务器标识无效喵...**"]))
            return
        ok, data = await self.zse_server.request_archive_export(
            server_code, action="list", timeout=60)
        if not ok or not isinstance(data, dict) or data.get("error"):
            await self._reply_markdown(message, "\n".join([
                title, "", "**❌ 读取备份列表失败喵...**", "",
                f"> 原因：{self._reason_of(data)}",
                "> 若提示不支持的包类型，请让服主更新 starZSEbot 插件",
            ]))
            return
        items = data.get("items") or []
        _log.info("备份列表请求 server=%s 返回 keys=%s items=%d",
                  (server_code or "")[:8], list((data or {}).keys()), len(items))
        if not items:
            await self._reply_markdown(message, "\n".join([
                title, "", "**还没有任何备份喵...**", "",
                "> 插件会每 30 分钟自动备份一次（配置项：自动备份间隔小时 / 备份保留份数）",
            ]))
            return
        lines = [title, "", f"**共 {len(items)} 份备份**（新 → 旧）", ""]
        for it in items[:25]:
            size = float(it.get("size") or 0) / 1024 / 1024
            lines.append(f"- `{int(it.get('no') or 0):>2}`  {it.get('time')}  ·  {size:.1f} MB")
        if len(items) > 25:
            lines.append(f"> 仅显示最近 25 份（共 {len(items)} 份）")
        lines += ["", f"> 回退：`回退备份 {seq} <编号>`（服主+，会把备份里的玩家存档导入覆盖）",
                  "> 自动备份：默认每 30 分钟一次"]
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_backup_restore(self, message, text: str, gid, user_openid: str = ""):
        """回退备份 <序号> <备份编号>：把该备份里的玩家存档重新导入覆盖（服主及以上）"""
        title = self.build_card_title("回退备份")
        cmd = "回退备份" if "回退备份" in text else "还原备份"
        rest = text[len(cmd):].lstrip("：: \t").strip()
        seq, tail = parse_server_index(rest)
        toks = (tail or "").split()
        no = int(toks[0]) if toks and toks[0].isdigit() else None
        if seq is None or no is None:
            await self._reply_markdown(message, "\n".join([
                title, "", "**格式：** `回退备份 <服务器序号> <备份编号>`", "",
                "> 备份编号见 `备份列表 <序号>`",
                "> 例：`回退备份 1 3`（把第 3 份备份里的玩家存档导入覆盖）",
            ]))
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ 找不到序号 {seq} 的服务器喵...**"]))
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "\n".join([title, "", "**❌ 该服务器标识无效喵...**"]))
            return
        ok, data = await self.zse_server.request_archive_export(
            server_code, action="list", timeout=60)
        items = (data or {}).get("items") if isinstance(data, dict) else None
        if not ok or not items:
            await self._reply_markdown(message, "\n".join([
                title, "", "**❌ 读取备份列表失败喵...**",
                f"> 原因：{self._reason_of(data)}",
            ]))
            return
        target = next((it for it in items if int(it.get("no") or -1) == no), None)
        if target is None:
            await self._reply_markdown(message, "\n".join([
                title, "", f"**❌ 没有编号 {no} 的备份喵...**",
                f"> 当前共 {len(items)} 份，用 `备份列表 {seq}` 查看编号",
            ]))
            return
        ok2, d2 = await self.zse_server.request_archive_export(
            server_code, action="restore", file=target.get("name") or "", timeout=300)
        if not ok2 or not isinstance(d2, dict) or d2.get("error"):
            await self._reply_markdown(message, "\n".join([
                title, "", "**❌ 回退失败喵...**", "",
                f"> 备份：`{target.get('name')}`",
                f"> 原因：{self._reason_of(d2)}",
            ]))
            return
        restored = d2.get("restored") or []
        skipped = d2.get("skipped") or []
        lines = [title, "", "**✅ 回退完成**", "",
                 f"- 备份：`{d2.get('file') or target.get('name')}`  （{target.get('time')}）",
                 f"- 成功导入：**{len(restored)}** 个玩家存档"]
        if restored:
            show = "、".join(str(x) for x in restored[:10])
            lines.append(f"  > {show}" + ("…" if len(restored) > 10 else ""))
        lines.append(f"- 跳过：{len(skipped)} 个" + (f"（{'、'.join(str(x) for x in skipped[:6])}…）" if skipped else ""))
        lines += ["", "> 相关在线玩家已被踢下线；重新登录后即为备份里的存档",
                  "> 若数据看着没变，请确认没有别人随后又保存了角色"]
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_world_settings(self, message, text: str, gid, user_openid: str = ""):
        """世界设置 <序号> [难度 经典|专家|大师|旅行] [大小 小|中|大] [邪恶 腐化|猩红]

        · 只给序号 → 展示当前世界参数 + 下次重置将使用的设置（所有人可看）
        · 带参数 → 保存设置（管理员及以上），**重置生成新世界时生效**（种子投票出来的世界也按它）
        · 值与"跟随"（或 默认/清除）→ 恢复为跟随当前世界
        """
        title = self.build_card_title("世界设置")
        rest = text[len("世界设置"):].lstrip("：: \t").strip()
        seq, tail = parse_server_index(rest)
        if seq is None:
            await self._reply_markdown(message, "\n".join([
                title, "", "**格式：** `世界设置 <服务器序号> [难度] [世界大小] [邪恶]`", "",
                "> 查看：`世界设置 1`",
                "> 修改：`世界设置 1 大师 大 猩红`（管理员+，顺序＝难度/大小/邪恶，只写前几项也行）",
                "> 取值：难度 `经典/专家/大师/旅行`；大小 `小/中/大`；邪恶 `腐化/猩红`",
                "> 恢复跟随当前世界：对应位置填 `跟随`",
            ]))
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ 找不到序号 {seq} 的服务器喵...**"]))
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "\n".join([title, "", "**❌ 该服务器标识无效喵...**"]))
            return
        toks = (tail or "").split()
        keymap = {"难度": "difficulty", "大小": "size", "世界大小": "size", "尺寸": "size",
                  "邪恶": "evil", "环境": "evil", "邪恶环境": "evil", "邪恶地形": "evil"}
        pairs = {}
        if toks:
            if toks[0] in keymap:
                # 键值式（也支持）：世界设置 1 难度 大师 大小 大 邪恶 猩红
                i = 0
                while i < len(toks):
                    key = keymap.get(toks[i])
                    if key is None or i + 1 >= len(toks):
                        await self._reply_markdown(message, "\n".join([
                            title, "", "**❌ 参数格式不对喵...**", "",
                            "> 格式：`世界设置 <序号> [难度] [世界大小] [邪恶]`",
                            "> 例：`世界设置 1 大师 大 猩红`",
                        ]))
                        return
                    val = toks[i + 1]
                    pairs[key] = "" if val in ("跟随", "默认", "清除", "不变", "默认值") else val
                    i += 2
            else:
                # 位置式（推荐）：世界设置 1 大师 大 猩红 = 难度 / 世界大小 / 邪恶（可只写前几项）
                for key, val in zip(("difficulty", "size", "evil"), toks[:3]):
                    pairs[key] = "" if val in ("跟随", "默认", "清除", "不变", "默认值", "-") else val
                if len(toks) > 3:
                    await self._reply_markdown(message, "\n".join([
                        title, "", "**❌ 参数太多了喵...**", "",
                        "> `世界设置 <序号> [难度] [世界大小] [邪恶]`",
                        "> 例：`世界设置 1 大师 大 猩红`（只写前两项也行）",
                    ]))
                    return
            if not await self._perm_ok(message, gid, user_openid, PERM_WORLD_SETTINGS, "世界设置"):
                return
            await self._reply_markdown(message, "\n".join([
                title, "", "**❌ 读取/保存世界设置失败喵...**", "",
                f"> 原因：{self._reason_of(data)}",
                "> 若提示不支持的包类型，请让服主更新 starZSEbot 插件",
            ]))
            return
        if data.get("error"):
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ {data.get('error')}**"]))
            return
        lines = [title, ""]
        if pairs:
            lines += ["**✅ 地图设置已保存（下次重置生成新世界时生效）**", ""]
        lines += [
            f"**当前世界**：{data.get('world_name') or '—'}",
            f"- 难度：**{data.get('difficulty') or '—'}**",
            f"- 世界大小：**{data.get('size') or '—'}**（{data.get('max_x') or '?'}×{data.get('max_y') or '?'}）",
            f"- 邪恶环境：**{data.get('evil') or '—'}**",
        ]
        seed = str(data.get("text_seed") or data.get("seed") or "").strip()
        if seed:
            lines.append(f"- 当前种子：`{_md_safe(seed)}`")
        lines.append(f"- 困难模式：{'是' if data.get('hardmode') else '否'}")
        lines += ["", "**下次重置将使用**（种子投票获胜时也一样，只有种子由投票决定）"]
        for label, key in (("难度", "set_difficulty"), ("世界大小", "set_size"), ("邪恶环境", "set_evil")):
            val = str(data.get(key) or "").strip()
            lines.append(f"- {label}：**{_md_safe(val)}**" if val else f"- {label}：跟随当前世界")
        lines += ["", "> 修改：`世界设置 %d 大师 大 猩红`（顺序＝难度/大小/邪恶）" % seq,
                  "> 恢复跟随：对应位置填 `跟随`"]
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_backup(self, message, text: str, gid, user_openid: str = ""):
        """备份 [发送] <序号>：把存档打包备份到服务器（加"发送"则额外把 zip 推到本群）"""
        title = self.build_card_title("备份")
        rest = text[len("备份"):].lstrip("：: \t").strip()
        toks = rest.split()
        send_to_group = bool(toks) and toks[0] in ("发送", "发", "群里", "发到群里")
        if send_to_group:
            toks = toks[1:]
        seq = int(toks[0]) if toks and toks[0].isdigit() else None
        if seq is None:
            await self._reply_markdown(message, "\n".join([
                title, "", "**格式：** `备份 [发送] <服务器序号>`", "",
                "> `备份 1`：只备份到服务器（推荐，不刷群、快）",
                "> `备份 发送 1`：备份并把 zip 发到本群（存档大时会很慢）",
            ]))
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ 找不到序号 {seq} 的服务器喵...**"]))
            return
        server_code = self._vote_server_code(rec)
        if not server_code:
            await self._reply_markdown(message, "\n".join([title, "", "**❌ 该服务器标识无效喵...**"]))
            return
        ok, data = await self.zse_server.request_archive_export(
            server_code, timeout=300, action="" if send_to_group else "backup")
        if not ok or not isinstance(data, dict) or data.get("error") or not data.get("name"):
            await self._reply_markdown(message, "\n".join([
                title, "", "**❌ 备份失败喵...**", "",
                f"> 原因：{self._reason_of(data)}",
                "> 若提示插件版本不支持，请让服主更新 starZSEbot 插件",
            ]))
            return
        name = data.get("name")
        size = int(data.get("size") or 0)
        lines = [title, "", "**✅ 存档已备份到服务器**", "",
                 f"- 文件：`{name}`",
                 f"- 大小：{size / 1024 / 1024:.1f} MB" if size else "- 大小：—",
                 "- 位置：`tshock/starZSEBot/Exports/`（按配置自动只保留最近若干份）"]
        pushed = None
        if send_to_group:
            b64 = data.get("base64") or ""
            if b64:
                fd, zip_path = tempfile.mkstemp(suffix=".zip")
                os.close(fd)
                try:
                    with open(zip_path, "wb") as f:
                        f.write(decode_archive_zip(b64))
                    await send_group_file(self, gid, zip_path, name, file_type=4)
                    pushed = True
                except Exception as e:
                    _log.warning("备份 zip 推送失败: %s", e)
                    pushed = False
                finally:
                    try:
                        os.remove(zip_path)
                    except OSError:
                        pass
        if send_to_group:
            lines.append(f"- 已发送到本群：{'是' if pushed else '失败（存档已备份在服务器）'}")
        await self._reply_markdown(message, "\n".join(lines))

    async def _force_close_vote(self, server_code: str):
        """强制结束该服务器进行中的投票并结算（发结果卡到联合区）。

        用于 /重置：重置会重建世界，投票结果随即被本次重置采用，
        所以先把进行中的投票结算掉（`pending_result` 才能取到获胜组合）。
        返回被结束的投票记录；没有进行中的投票则返回 None。
        """
        if not server_code:
            return None
        vote = self.votes.get_active(server_code)
        if not vote:
            return None
        vote_id = vote.get("vote_id")
        async with self._vote_pub_lock:
            vote = self.votes.get_vote(vote_id)
            if not vote or vote.get("status") != "open":
                return None        # 已被 结束投票/调度 结算
            self.votes.finish_vote(vote_id)
            vote = self.votes.get_vote(vote_id)
            try:
                tally = self.votes.tally(vote_id)
                await self._publish_vote_card(self._vote_target_gids(vote), vote, tally, "closed")
            except Exception as e:
                _log.warning("重置时发布投票结果卡失败: %s", e)
            self.votes.mark_result_published(vote_id)
        _log.info("重置触发：已强制结算进行中的投票 %s", vote_id)
        return vote

    # ───────────────────────── 种子投票后台调度 ─────────────────────────
    def _fmt_secs(sec) -> str:
        try:
            sec = max(0, int(sec))
        except (TypeError, ValueError):
            sec = 0
        d, rem = divmod(sec, 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        if d:
            return f"{d} 天 {h} 小时"
        if h:
            return f"{h} 小时 {m} 分"
        return f"{m} 分钟"

    async def _server_status_tick(self):
        """服务器上/下线通知（60 秒一轮）。

        只在「状态稳定切换」时播报：插件崩溃、TShock 重启、网络抖动都不会立刻刷群。
        每个群可用「服务器通知 关」关闭。
        """
        now = int(time.time())
        for rec in self.zse_server.all_records():
            code = rec.get("code") or ""
            if not code:
                continue
            gid = self._eff_gid(rec.get("owner_gid") or "")
            if not gid or not self.perms.notify_server_status(gid):
                continue
            ev = self.status_store.observe(code, self.zse_server.is_alive(rec), now)
            if not ev:
                continue
            event, prev_since = ev
            name = _md_safe(rec.get("server_name") or f"服务器 {rec.get('seq')}")
            addr = f"{rec.get('ip') or ''}:{rec.get('port') or ''}"
            elapsed = self._fmt_secs(now - int(prev_since or now))
            if event == "offline":
                title = "服务器掉线"
                body = f"**{name}** 已离线"
                extra = f"> 掉线前已在线：{elapsed}" if prev_since else "> 无法确定已在线时长"
            else:
                title = "服务器上线"
                body = f"**{name}** 已恢复在线"
                extra = f"> 掉线时长：{elapsed}" if prev_since else "> 首次观测到在线"
            try:
                await self.api.post_group_message(
                    group_openid=gid, msg_type=2,
                    markdown=MarkdownPayload(content="\n".join([
                        self.build_card_title(title), "",
                        body,
                        f"> 地址：`{addr}`",
                        extra, "",
                        "> 可用 `服务器通知 关` 关闭本群通知",
                    ])))
            except Exception as e:
                _log.warning("服务器状态通知发送失败 group=%s: %s", gid, e)

    async def _vote_tick(self):
        # 先算出本轮要截止的投票：这些不再发"更新卡"，否则同一时刻会先来一张投票卡、再来一张结果卡
        closing = {r.get("vote_id") for r in self.votes.due_closes()}
        for r in self.votes.due_updates():
            if r.get("vote_id") in closing:
                continue
            vote = self.votes.get_vote(r.get("vote_id"))
            if not vote or vote.get("status") != "open":
                continue
            tally = self.votes.tally(r.get("vote_id"))
            await self._publish_vote_card(self._vote_target_gids(vote), vote, tally, "open")
            self.votes.mark_update_sent(r.get("vote_id"))

        for r in self.votes.due_closes():
            # 与「结束投票」指令互斥：两边同时发布会让每个群多收一张结果卡
            async with self._vote_pub_lock:
                # finish 幂等：加锁后再确认一次仍为 open
                vote = self.votes.get_vote(r.get("vote_id"))
                if not vote or vote.get("status") != "open":
                    continue
                self.votes.finish_vote(r.get("vote_id"))
                vote = self.votes.get_vote(r.get("vote_id"))
                tally = self.votes.tally(r.get("vote_id"))
                await self._publish_vote_card(self._vote_target_gids(vote), vote, tally, "closed")
                self.votes.mark_result_published(r.get("vote_id"))

        # 结果卡补发：机器人若正好在 finish_vote 与发卡之间崩溃/重启，
        # 该投票已是 closed（due_closes 不会再返回它）但结果卡从未发出 → 这里补发，成功才标记。
        # min_age=90：刚结束的投票可能正在被「结束投票」指令或本轮调度发布，跳过它避免重复发卡。
        for r in self.votes.unpublished_closed(min_age=90):
            async with self._vote_pub_lock:
                vote = self.votes.get_vote(r.get("vote_id"))
                if not vote:
                    self.votes.mark_result_published(r.get("vote_id"))
                    continue
                try:
                    tally = self.votes.tally(r.get("vote_id"))
                    await self._publish_vote_card(self._vote_target_gids(vote), vote, tally, "closed")
                    self.votes.mark_result_published(r.get("vote_id"))
                    _log.warning("已补发漏掉的投票结果卡: %s", r.get("vote_id"))
                except Exception as e:
                    # 发卡失败就下轮再试（不标记，保持「未发布」状态）
                    _log.warning("补发投票结果卡失败（下轮重试）: %s", e)

    # ───────────────────────── 种子列表 / 提案 ─────────────────────────
    def _vote_max_options(self) -> int:
        return max(2, self._vote_option_count())

    def _seed_parse_nos(self, expr):
        """解析 `1+2+3`（也支持 , ，、 空格 分割）→ (nos, err)"""
        nos, bad = [], []
        for part in re.split(r"[+＋,，、\s]+", str(expr or "").strip()):
            if not part:
                continue
            if part.isdigit():
                nos.append(int(part))
            else:
                bad.append(part)
        if bad:
            return [], f"种子序号必须是数字：{'、'.join(bad[:3])}"
        if not nos:
            return [], "请给出种子序号，例如 `1+2+3`"
        return nos, None

    async def _republish_vote_card(self, vote) -> int:
        """提案变动后立刻把投票卡重发到联合区（不等下一个更新周期），并标记已更新"""
        if not vote:
            return 0
        try:
            tally = self.votes.tally(vote["vote_id"])
            ok_cnt, _failed = await self._publish_vote_card(
                self._vote_target_gids(vote), vote, tally, "open")
            self.votes.mark_update_sent(vote["vote_id"])
            return ok_cnt
        except Exception as e:
            _log.warning("提案后重发投票卡失败: %s", e)
            return 0

    async def cmd_seed_list(self, message, text: str, gid: str = ""):
        """种子列表 [页码]：图片卡展示常规/秘密世界种子（序号供 种子提案 引用，所有人可用）"""
        send_gid = gid or (getattr(message, "group_openid", None) or "")
        title = self.build_card_title("种子列表")
        if not self.seeds.available():
            await self._reply_markdown(message, "\n".join([
                title, "", "**❌ 种子数据未部署喵...**",
                "> 请让服主运行 `scripts/fetch_seed_list.py`",
            ]))
            return
        toks = text[len("种子列表"):].lstrip("：: \t").split()
        page = int(toks[0]) if toks and toks[0].isdigit() else 1
        pages = self.seeds.pages()
        total = max(1, len(pages))
        page = max(1, min(page, total))
        rows = pages[page - 1]
        subtitle = ((rows[0].get("category") or "") + "世界种子") if rows else ""
        footer = "starZSEbot · 种子数据来自 terraria.wiki.gg（CC BY-SA）"
        if total > 1:
            footer += f" ｜ 翻页：种子列表 {page % total + 1}"
        png = None
        try:
            png = render_seed_list_card(rows, page=page, total_pages=total, title="种子列表",
                                        subtitle=subtitle, footer=footer,
                                        bg_dir=self._vote_bg_dir())
        except Exception as e:
            _log.exception("种子列表卡渲染失败: %s", e)
        if png:
            try:
                await send_group_image(self, send_gid, png, filename="seeds.jpg",
                                       msg_id=getattr(message, "id", None))
                return
            except Exception as e:
                _log.warning("种子列表卡发送失败，降级文本: %s", e)
        lines = [title, "", f"> 第 {page} / {total} 页 · {subtitle}"]
        for it in rows:
            lines.append(f"- `{int(it.get('no') or 0):02d}` **{_md_safe(it.get('name'))}**"
                         f"（输入 `{_md_safe(it.get('seed'))}`）")
        if total > 1:
            lines.append(f"> 翻页：`种子列表 {page % total + 1}`")
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_seed_propose(self, message, text: str, gid, user_openid: str = ""):
        """种子提案 <服务器序号> <序号+序号…>：给该服务器进行中的投票追加候选（所有人可用）"""
        title = self.build_card_title("种子提案")
        seq, rest = parse_server_index(text[len("种子提案"):])
        if seq is None or not str(rest or "").strip():
            await self._reply_markdown(message, "\n".join([
                title, "", "**格式：** `种子提案 <服务器序号> <种子序号+…>`", "",
                "> 序号见 `种子列表`，例：`种子提案 1 1+3+15`",
                f"> 每人最多 {VOTE_PROPOSALS_PER_USER} 条在场；满 {VOTE_MAX_OPTIONS} 项后新提案顶掉最旧的（票会归还）",
            ]))
            return
        if not self.seeds.available():
            await self._reply_markdown(message, "\n".join([title, "", "**❌ 种子数据未部署喵...**"]))
            return
        nos, err = self._seed_parse_nos(rest)
        if err:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ {err}**"]))
            return
        entries, err = self.seeds.resolve(nos)
        if err:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ {err}**"]))
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ 找不到序号 {seq} 的服务器喵...**"]))
            return
        server_code = self._vote_server_code(rec)
        vote = self.votes.get_active(server_code) if server_code else None
        if not vote:
            await self._reply_markdown(message, "\n".join([
                title, "", "**该服务器当前没有进行中的投票喵...**",
                "> 可由服主用 `种子投票` 发起后再提案",
            ]))
            return
        name = self.seeds.label(entries)
        seed_vals = self.seeds.seed_values(entries)
        ok, msg, info = self.votes.add_option(
            vote["vote_id"], name, seed_vals,
            proposer_openid=user_openid, proposer_name=self._disp(gid, user_openid),
            max_options=self._vote_max_options(), per_user=VOTE_PROPOSALS_PER_USER)
        if not ok:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ {msg}**"]))
            return
        lines = [title, "", f"✅ {msg}", "", f"> 组合：`{_md_safe(name)}`",
                 f"> 输入种子：`{_md_safe(' + '.join(seed_vals))}`"]
        if info.get("replaced"):
            lines.append(f"> ♻️ 已顶替最旧的「{_md_safe(info.get('replaced'))}」提案"
                         f"（归还 {int(info.get('refunded') or 0)} 张票，编号已前移）")
        await self._reply_markdown(message, "\n".join(lines))
        await self._republish_vote_card(self.votes.get_vote(vote["vote_id"]))

    async def cmd_seed_withdraw(self, message, text: str, gid, user_openid: str = ""):
        """撤回提案 <服务器序号> <投票卡编号>：撤回自己提出的提案（票归还给投票人）"""
        await self._seed_remove(message, text, gid, user_openid, "撤回提案", admin=False)

    async def cmd_seed_delete(self, message, text: str, gid, user_openid: str = ""):
        """删除提案 <服务器序号> <投票卡编号>：管理员删除任意提案（票归还给投票人）"""
        await self._seed_remove(message, text, gid, user_openid, "删除提案", admin=True)

    async def _seed_remove(self, message, text, gid, user_openid, cmd_name: str, admin: bool):
        title = self.build_card_title(cmd_name)
        seq, rest = parse_server_index(text[len(cmd_name):])
        toks = (rest or "").split()
        no = int(toks[0]) if toks and toks[0].isdigit() else None
        if seq is None or no is None:
            await self._reply_markdown(message, "\n".join([
                title, "", f"**格式：** `{cmd_name} <服务器序号> <投票卡编号>`", "",
                f"> 编号见投票卡，例：`{cmd_name} 1 4`",
                "> 只能撤回自己提出的提案" if not admin else "> 管理员及以上可删除任意提案（含机器人随机项）",
            ]))
            return
        rec = self._vote_server(gid, seq)
        if rec is None:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ 找不到序号 {seq} 的服务器喵...**"]))
            return
        server_code = self._vote_server_code(rec)
        vote = self.votes.get_active(server_code) if server_code else None
        if not vote:
            await self._reply_markdown(message, "\n".join([title, "", "**该服务器当前没有进行中的投票喵...**"]))
            return
        ok, msg, info = self.votes.remove_option(vote["vote_id"], no,
                                                 openid=user_openid, admin=admin)
        if not ok:
            await self._reply_markdown(message, "\n".join([title, "", f"**❌ {msg}**"]))
            return
        lines = [title, "", f"✅ {msg}", "",
                 f"> ♻️ 已归还 **{int(info.get('refunded') or 0)}** 张票（投票人可重新投给其他提案）",
                 "> 其余编号已自动前移，请以新的投票卡为准"]
        await self._reply_markdown(message, "\n".join(lines))
        await self._republish_vote_card(self.votes.get_vote(vote["vote_id"]))

    async def cmd_lexicon(self, message, text: str, kind: str, gid: str = ""):
        """图鉴搜索：si/sn/sp/sb/sx（也支持 搜物品/搜生物/搜弹幕/搜增益/搜修饰）

        命中 1 条 → 详情卡（图标 + 属性 + 说明）；多条 → 列表卡（图标 + 名称 + ID + 摘要）。
        卡片渲染失败时降级为纯文本，功能不中断。
        """
        meta = lexicon.KINDS.get(kind) or {}
        label = meta.get("label") or "图鉴"
        title = meta.get("title") or label
        cmd = _lexicon_match(text) or title
        query = text[len(cmd):].lstrip("：: \t　").strip() if text.lower().startswith(cmd.lower()) else text.strip()
        if not query:
            await self._reply_markdown(message, "\n".join([
                self.build_card_title(f"图鉴 · {label}"), "",
                f"**格式：** `{title} <名字|ID>`",
                f"> 例：`{title} 天顶剑`、`{title} 4956`",
                "> 支持中文名、英文名、别名与数字 ID",
            ]))
            return
        store = self.lexicon_store
        if not store.available():
            await self._reply_markdown(message, "\n".join([
                self.build_card_title(f"图鉴 · {label}"), "",
                "**❌ 图鉴数据尚未部署喵...**",
                "> 请让服主运行 `scripts/deploy_lexicon_assets.py`",
            ]))
            return
        items, total = store.search(kind, query, limit=LEX_LIMIT)
        if not items:
            await self._reply_markdown(message, "\n".join([
                self.build_card_title(f"图鉴 · {label}"), "",
                f"**未找到与「{_md_safe(query)}」相关的条目喵...**",
                "> 可以试试英文名、别名或数字 ID",
            ]))
            return
        # 注意：这里必须用「发消息的那个群」的 openid。被动回复的 msg_id 与所在群绑定，
        # 用 _eff_gid()（联合区总群）去发图片会被 QQ 判定「msg_id 无效或越权」(40034024) → 发图失败降级文本。
        send_gid = gid or (getattr(message, "group_openid", None) or "")
        png = None
        try:
            if total <= 1:
                it = items[0]
                desc_txt, desc_src = store.description_with_source(it)
                # 资料 + 说明：同一张图里上下两个面板
                # （务必把 desc_txt 传进来！曾经这里残留 desc="" 导致"有堆叠开关但没内容"→ 说明面板不画）
                png = render_lexicon_card(label, query, "single",
                                          item=dict(it, _id=it.get(meta.get("id"))),
                                          attrs=store.attributes(kind, it),
                                          desc=desc_txt,
                                          icon_path=store.image_path(kind, it),
                                          empty_desc_hint=("资料库中暂无该条目的说明"
                                                           if not desc_txt and kind in ("item", "npc", "buff")
                                                           else ""),
                                          # 资料与说明分成上下两个独立面板（视觉上是两张卡）：
                                          # 被动回复的 msg_id 只能用一次，第二条消息必然走「主动消息」，
                                          # 而主动消息的内容审核会拦我们的卡片（实测 40034006 消息内容违规）
                                          stack_desc=True,
                                          footer=("starZSEbot · 图鉴（说明来自 terraria.wiki.gg）"
                                                  if desc_src == "wiki" else ""),
                                          bg_dir=self._vote_bg_dir())
            else:
                rows = [{"name": it.get("Name"), "id": it.get(meta.get("id")),
                         "icon": store.image_path(kind, it),
                         "summary": store.summary(kind, it)} for it in items]
                png = render_lexicon_card(label, query, "list", rows=rows, total=total,
                                          bg_dir=self._vote_bg_dir())
        except Exception as e:
            _log.exception("图鉴卡渲染失败: %s", e)
            png = None
        if png:
            # 落盘最近一张卡：出问题时可直接下载核对（"我这边看不到说明"这类问题一眼定位）
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "last_lexicon.jpg"), "wb") as f:
                    f.write(png)
                if total <= 1:
                    _log.info("图鉴卡已渲染: %s desc_src=%s desc_len=%d bytes=%d",
                              _md_safe(items[0].get("Name") or ""),
                              store.description_with_source(items[0])[1] or "无",
                              len(store.description(items[0])), len(png))
            except OSError as e:
                _log.warning("图鉴卡落盘失败: %s", e)
            try:
                await send_group_image(self, send_gid, png, filename="lexicon.png",
                                       msg_id=getattr(message, "id", None))
                # 发送成功就必须结束：被动回复的 msg_id 只能用一次，
                # 再发一条会被 QQ 判重（40054005 消息已去重，请勿重复 msgseq）
                return
            except Exception as e:
                _log.warning("图鉴卡发送失败，降级文本: %s", e)
        # 降级：纯文本
        lines = [self.build_card_title(f"图鉴 · {label}"), ""]
        if total <= 1:
            it = items[0]
            lines.append(f"**{_md_safe(it.get('Name'))}**（ID {it.get(meta.get('id'))}）")
            lines += [f"- {_md_safe(k)}：{_md_safe(v)}" for k, v in store.attributes(kind, it)]
            d = store.description(it)
            if d:
                lines += ["", *[f"> {_md_safe(x)}" for x in d.splitlines()[:8]]]
        else:
            lines.append(f"找到 **{total}** 条匹配（显示前 {len(items)} 条）：")
            lines += [f"- {_md_safe(it.get('Name'))}（ID {it.get(meta.get('id'))}）" for it in items]
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_exec(self, message, text: str, gid):
        """远程指令 <序号|all|*> <指令>：bot 以超管权限执行并返回服务器结果"""
        gid = gid or (getattr(message, "group_openid", None) or "")
        prefix = "远程指令" if text.startswith("远程指令") else "远程执行"
        rest = text[len(prefix):].lstrip()
        seg = rest.split(None, 1)
        if len(seg) < 2:
            await self._reply_markdown(
                message,
                "## ꧁༺ 远程执行 ༻꧂\n\n"
                "参数不完整喵...\n\n"
                "请按照正确的格式：`远程指令 <服务器序列号> <指令>` 重新输入喵",
            )
            return
        target = seg[0]
        command = seg[1].strip()
        recs = self.zse_server.visible_records(gid)
        if target in ("all", "*"):
            seqs = list(range(1, len(recs) + 1))
        elif target.isdigit():
            seqs = [int(target)]
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 远程执行 ༻꧂\n\n"
                "参数错误了喵...\n\n"
                "请按照正确的格式：`远程指令 <服务器序列号> <指令>` 重新输入喵",
            )
            return
        if not seqs:
            await self._reply_markdown(message, "## ꧁༺ 远程执行 ༻꧂\n\n" "**❌ 没有可执行的服务器喵**")
            return
        user_openid = getattr(message.author, "member_openid", None) or ""
        nm = self.whitelist_store.find_by_openid(self._eff_gid(gid), user_openid) or "群友"
        blocks = []
        for seq in seqs:
            sname = self._server_name_plain(gid, seq) or "未知服务器"
            ok, out = await self.zse_server.exec_command(gid, seq, command, user_openid, gid)
            if ok and out not in ("（无输出）", ""):
                result = _md_fence_safe(out[:1000])   # 保留多行格式，只防围栏逃逸
            else:
                result = "服务器返回了个棍母喵..."
            blocks.append(
                f"@{nm}\n\n"
                f"服务器：`{sname}`\n"
                f"执行内容：`{command}`\n"
                f"返回内容：\n\n"
                f"```\n{result}\n```"
            )
        sep = "\n\n────────────\n\n"
        await self._reply_at_then_card(
            message, user_openid,
            "## ꧁༺ ZSE 远程命令 ༻꧂\n\n" + sep.join(blocks),
        )

    def _server_name_plain(self, gid: str, seq: int) -> str:
        """按展示序号取服务器显示名（纯名，无括号；其它群的服务器同样能取到）"""
        rec = self.zse_server.record_by_seq(gid, seq)
        return _md_safe((rec or {}).get("server_name") or "")

    def _zone_other_ids(self, gid: str) -> str:
        """同联合区其它群的显示串（“群ID 1、群ID 3”）；没有则空串"""
        others = [g for g in (self.registry.zone_gids(gid) or set()) if g and g != gid]
        others.sort(key=lambda g: (self.registry.join_id_of(g) or 10 ** 9, g))
        return "、".join(f"群ID {self.registry.join_id_of(g) or (g[:8] + '…')}" for g in others)

    async def cmd_mail_limit_reset(self, message, text: str, gid, user_openid: str = ""):
        """邮箱上限重置 <QQ号>：清零该邮箱的申请计数与冷却（管理员及以上）"""
        title = self.build_card_title("邮箱上限重置")
        rest = text[len("邮箱上限重置"):].lstrip("：: \t").strip()
        if not rest:
            await self._reply_markdown(message, "\n".join([title, "", "**格式：** `邮箱上限重置 <QQ号>`"]))
            return
        email = normalize_email(rest)
        if not is_valid_qq_email(email):
            await self._reply_markdown(message, "\n".join([title, "", "**❌ 请提供纯数字 QQ 号（5~12 位）**"]))
            return
        ok, msg = self.mail.reset_email_limit(email)
        _log.warning("[邮箱上限重置] %s 对 %s：%s", self._disp(gid, user_openid), email, msg)
        await self._reply_markdown(message, "\n".join([
            title, "",
            ("✅ **" + msg + "**" if ok else ("❌ " + msg)),
            "> 该邮箱现在可以重新申请验证码了",
        ]))

    async def cmd_bind_email(self, message, text: str, gid):
        """绑定 <QQ号>：向 <QQ号>@qq.com 发 6 位验证码。规则集中在 bind_rules.check_bind_request（可单元测试）"""
        _bp = "绑定" if text.startswith("绑定") else "绑定"
        rest = text[len(_bp):].lstrip("：: \t")
        try:
            user_openid = message.author.member_openid or ""
        except AttributeError:
            user_openid = ""
        try:
            union_openid = getattr(getattr(message, "author", None), "union_openid", "") or ""
        except Exception:
            union_openid = ""
        try:
            import json as _json
            _vp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify.json")
            with open(_vp, "r", encoding="utf-8") as _f:
                _verify_users = (_json.load(_f) or {}).get("users") or {}
        except Exception:
            _verify_users = {}
        allowed, email, reason = check_bind_request(rest, user_openid, union_openid,
                                                  getattr(self.whitelist_store, "_data", None) or {},
                                                  _verify_users)
        _log.info("BIND_CHECK ok=%s oid_len=%d reason_len=%d", allowed, len(user_openid or ""), len(reason or ""))
        if not allowed:
            await self._reply_markdown(message, "## ꧁༺ 白名单验证 ༻꧂\n\n"
                "**❌ " + reason + "**\n\n"
                "> 首次绑定：`绑定 <你的QQ号>`（例 `绑定 1011819146`）\n"
                "> 换绑：`邮箱改绑 <新QQ号>`")
            return
        ok, msg, _code = self.mail.request_code(user_openid, email, self._group_name(self._eff_gid(gid)), self.bot_name)
        if ok:
            await self._reply_markdown(message, "## ꧁༺ 白名单验证 ༻꧂\n\n"
                f"✅ **验证码已发送到 `{email}`**\n\n"
                "请查收邮件，然后发送：\n"
                f"`添加白名单 <进服玩家名> <验证码>`\n\n"
                "> 验证码5分钟有效，若过期请重新申请喵")
        else:
            await self._reply_markdown(message, "## ꧁༺ 白名单验证 ༻꧂\n\n" + f"**❌ {msg}**")

    async def cmd_add_whitelist(self, message, text: str, gid):
        """添加白名单 <进服玩家名> <验证码>：按申请人 QQ 校验验证码并绑定白名单"""
        parts = text.split()
        if len(parts) < 3:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单绑定 ༻꧂\n\n"
                "**缺少参数喵...**\n\n"
                "格式：`添加白名单 <进服玩家名> <验证码>`\n"
                "例：`添加白名单 星梦 1234`",
            )
            return
        player_name = parts[1].strip()
        code = parts[2]
        if not check_name_ok(player_name):
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单绑定 ༻꧂\n\n"
                "**玩家名不合法喵...**\n\n"
                "> 要求：长度 1~15，仅限汉字、字母、数字、空格\n"
                "> 不能包含换行、引号或其它特殊符号喵",
            )
            return
        try:
            user_openid = message.author.member_openid or ""
        except AttributeError:
            user_openid = ""

        ok, msg, email = self.mail.verify_code(user_openid, code)
        if not ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单绑定 ༻꧂\n\n"
                f"**❌ {msg}**\n\n"
                "请先发送 `绑定 <QQ号>` 获取验证码",
            )
            return

        # 验证通过：先清理过期改绑事务（过期退回原白名单），再写入白名单
        self._sweep_changes()
        eff = self._eff_gid(gid)
        # 同名校验：名字已被他人绑定时拒绝覆盖（本人、或使用同一绑定邮箱的账号可覆盖）
        existing = self.whitelist_store.get_record(eff, player_name)
        exist_openid = (existing or {}).get("bind_openid") or ""
        if existing and exist_openid and exist_openid != user_openid:
            same_person = bool(email) and (existing.get("email") or "").lower() == email.lower()
            if not same_person:
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 白名单绑定 ༻꧂\n\n"
                    f"**`{player_name}` 已被其他玩家绑定喵...**\n\n"
                    "> 请不要使用他人已在用的玩家名\n"
                    "> 如需使用该名字，请联系管理员处理",
                )
                return
        self.whitelist_store.add(eff, player_name, email, bind_openid=user_openid)
        # 邮箱改绑完成判定：本人有进行中的改绑、新邮箱一致、且在同一联合区 → 改绑成功
        ch = self.changes.get_active(user_openid) if user_openid else None
        change_done = False
        if ch and (ch.get("new_email") or "").lower() == (email or "").lower() and ch.get("gid") == eff:
            self.changes.complete(user_openid)
            change_done = True
        lines = [
            "## ꧁༺ 白名单绑定 ༻꧂", "",
            "✅ **绑定成功喵！**", "",
            f"- 进服玩家名：`{player_name}`",
            f"- 绑定邮箱：`{email}`", "",
        ]
        if change_done:
            lines += [
                "🔁 **邮箱改绑已完成**：新邮箱已生效，原白名单已作废，现在进服用本名字即可喵",
                "",
            ]
        elif ch:
            lines += [
                f"> ⚠️ 您有待完成的邮箱改绑（新邮箱：`{ch.get('new_email')}`），"
                "本次使用的邮箱与之不一致，改绑尚未完成",
                "",
            ]
        lines.append("> 现在可以用这个名字进入服务器啦，首次进服会自动登记设备")
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_rename_whitelist(self, message, text: str, gid):
        """修改白名单 <新玩家名>：修改本人绑定的进服玩家名。
        规则：无需重绑邮箱（邮箱/绑定人保留）；名字校验同添加白名单；
        不迁移游戏存档；原名登录记录清除（新名字须重新确认登录）；
        48 小时内限一次；新名字被占用则拒绝。"""
        text = text.lstrip("/").strip()
        rest = text[len("修改白名单"):] if text.startswith("修改白名单") else ""
        new_name = rest.lstrip("：: \t").strip()
        if not new_name:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单改名 ༻꧂\n\n"
                "**缺少参数喵...**\n\n"
                "格式：`修改白名单 <新玩家名>`\n"
                "例：`修改白名单 星梦`",
            )
            return
        if not check_name_ok(new_name):
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单改名 ༻꧂\n\n"
                "**玩家名不合法喵...**\n\n"
                "> 要求：长度 1~15，仅限汉字、字母、数字、空格\n"
                "> 不能包含换行、引号或其它特殊符号喵",
            )
            return
        user_openid = self._user_openid(message)
        eff = self._eff_gid(gid)
        old_name = self.whitelist_store.find_by_openid(eff, user_openid) if user_openid else ""
        if not old_name:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单改名 ༻꧂\n\n"
                "**没有找到您绑定的白名单喵...**\n\n"
                "> 请先发送 `添加白名单 <玩家名> <验证码>` 完成绑定",
            )
            return
        if new_name == old_name:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单改名 ༻꧂\n\n"
                "**新玩家名和当前名字一样喵，无需修改**",
            )
            return
        allowed, left = self.changes.rename_allowed(user_openid)
        if not allowed:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单改名 ༻꧂\n\n"
                "**修改太频繁喵...**\n\n"
                f"> 玩家名每 48 小时只能修改一次，剩余约 {_fmt_left(left)}\n"
                "> 如需紧急处理请联系管理员",
            )
            return
        ok, msg = self.whitelist_store.rename(eff, old_name, new_name)
        if not ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单改名 ༻꧂\n\n"
                f"**❌ {msg}**",
            )
            return
        old_email = (self.whitelist_store.get_record(eff, new_name) or {}).get("email") or "（未绑定）"
        self.changes.mark_rename(user_openid)
        self.pending_store.pop(eff, old_name)  # 旧名字的待批准登录记录一并作废
        await self._reply_markdown(
            message,
            "## ꧁༺ 白名单改名 ༻꧂\n\n"
            "✅ **修改成功喵！**\n\n"
            f"- 原玩家名：`{old_name}`\n"
            f"- 新玩家名：`{new_name}`\n"
            f"- 绑定邮箱：`{old_email}`（无需重绑，保持不变）\n\n"
            "> ⚠️ 游戏存档不会迁移：原存档仍在原玩家名下，新名字进服会是全新角色\n"
            "> 原名登录记录已清除，新名字需重新确认登录后进服：\n"
            f"> 1️⃣ 用新名字进服一次（会提示未授权设备）\n"
            f"> 2️⃣ 在本群发送 `登录 {new_name}` 批准\n"
            f"> 3️⃣ 再次进服即可\n\n"
            "> 玩家名每 48 小时只能修改一次喵",
        )

    async def cmd_change_email(self, message, text: str, gid):
        """邮箱改绑 <新邮箱>（兼容 改绑邮箱 <新邮箱>）：改绑本人白名单的绑定邮箱。
        规则：原白名单立即作废；24 小时内用新邮箱重新走「绑定 → 添加白名单」
        才算改绑成功（成功后才真正生效）；超时未完成自动恢复原白名单；7 天限一次。"""
        text = text.lstrip("/").strip()
        rest = ""
        for kw in ("邮箱改绑", "改绑邮箱"):
            if text.startswith(kw):
                rest = text[len(kw):]
                break
        new_email = rest.lstrip("：: \t").strip().lower()
        if not check_qq_email(new_email):
            await self._reply_markdown(
                message,
                "## ꧁༺ 邮箱改绑 ༻꧂\n\n"
                "**❌ 只支持 QQ 邮箱，且必须是「QQ号@qq.com」形式喵**\n\n"
                "格式：`邮箱改绑 <QQ号>@qq.com`（也支持 `改绑邮箱 <QQ号>@qq.com`）\n"
                "例：`邮箱改绑 1011819146@qq.com`",
            )
            return
        user_openid = self._user_openid(message)
        eff = self._eff_gid(gid)
        old_name = self.whitelist_store.find_by_openid(eff, user_openid) if user_openid else ""
        if not old_name:
            await self._reply_markdown(
                message,
                "## ꧁༺ 邮箱改绑 ༻꧂\n\n"
                "**没有找到您绑定的白名单喵...**\n\n"
                "> 请先发送 `添加白名单 <玩家名> <验证码>` 完成绑定",
            )
            return
        rec = self.whitelist_store.get_record(eff, old_name) or {}
        old_email = (rec.get("email") or "").lower()
        if new_email == old_email:
            await self._reply_markdown(
                message,
                "## ꧁༺ 邮箱改绑 ༻꧂\n\n"
                "**新邮箱与当前绑定邮箱相同喵，无需改绑**",
            )
            return
        self._sweep_changes()  # 先清理过期事务，避免"卡住"误判
        ch = self.changes.get_active(user_openid)
        if ch:
            await self._reply_markdown(
                message,
                "## ꧁༺ 邮箱改绑 ༻꧂\n\n"
                "**您已有一个进行中的邮箱改绑喵**\n\n"
                f"> 请先用 `{ch.get('new_email')}` 完成「绑定 → 添加白名单」\n"
                "> 超过 24 小时未完成会自动恢复原白名单",
            )
            return
        allowed, left = self.changes.email_allowed(user_openid)
        if not allowed:
            await self._reply_markdown(
                message,
                "## ꧁༺ 邮箱改绑 ༻꧂\n\n"
                "**改绑太频繁喵...**\n\n"
                f"> 邮箱每 7 天只能改绑一次，剩余约 {_fmt_left(left)}\n"
                "> 如需紧急处理请联系管理员",
            )
            return
        # 事务开始：备份原记录（超时回滚用）→ 原白名单立即作废
        self.changes.start_email_change(user_openid, eff, old_name, old_email, new_email, dict(rec))
        self.whitelist_store.remove(eff, old_name)
        self.pending_store.pop(eff, old_name)
        await self._reply_markdown(
            message,
            "## ꧁༺ 邮箱改绑 ༻꧂\n\n"
            "✅ **已进入邮箱改绑流程喵**\n\n"
            f"- 原邮箱：`{old_email}`\n"
            f"- 新邮箱：`{new_email}`\n"
            f"- 原白名单 `{old_name}`：**已立即作废**（暂无法进服）\n\n"
            "请在 **24 小时** 内用新邮箱完成两步（完成前不会生效）：\n"
            f"1️⃣ `绑定邮箱 {new_email}`\n"
            "2️⃣ `添加白名单 <进服玩家名> <验证码>`（名字可自由起，汉字/字母/数字）\n\n"
            "> 完成添加后改绑即成功；超过 24 小时未完成会自动恢复原白名单\n"
            "> 邮箱每 7 天只能改绑一次喵",
        )

    async def _on_need_login_push(self, rec: dict, info: dict):
        """插件判定 need_login（换设备/城市变动）→ 向白名单总群推送带「登录/拒绝」按钮的请求卡。

        群未给机器人开主动消息权限时发送失败 → 静默跳过（玩家直接在群里 @机器人 发"登录"批准）。
        防刷屏：同一玩家 5 分钟内只推一次（反复进服不重复刷卡，期间仍可用"登录"指令批准）。
        清空设备/批准登录后会重置该玩家的限流（下次进服拦截立即推新卡）。
        """
        info = info or {}
        gid = info.get("gid") or ""
        player_name = (info.get("player_name") or "").strip()
        if not gid or not player_name:
            return
        key = f"{gid}|{player_name}"
        now = time.time()
        if now - self._login_push_last.get(key, 0) < 300:
            _log.info("登录请求卡推送限流跳过: %s", key)
            return
        self._login_push_last[key] = now

        reason_line = {
            "new_device": "您有新设备登入请求喵!",
            "uuid_change": "您的登录凭证发生变动，请重新登录喵！",
            "ip_change": "您登录地区发生跨市级变动，请重新登录喵!",
        }.get(info.get("reason") or "", "您有新设备登入请求喵!")
        platform_text = {
            "PC": "电脑端PC", "PE": "手机端PE", "Steam": "Steam", "XBO": "Xbox",
            "PSN": "PlayStation", "Nintendo": "Switch", "GameCenter": "iOS",
        }.get(info.get("platform") or "", info.get("platform") or "未识别")
        join_time = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(int(info.get("ts") or time.time())))
        # 真 @ 玩家（协议 <qqbot-at-user id="member_openid"/>，仅 markdown 消息渲染）；
        # openid 取白名单绑定人（缺绑定时退回 @名字 文本，不阻断推送）
        oid = self._resolve_openid(gid, player_name)
        at_line = f'<qqbot-at-user id="{oid}" />' if oid else f"@{player_name}"
        content = (
            "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
            f"{at_line}\n"
            f"### {reason_line}\n\n"
            f"- 玩家名称：`{player_name}`\n"
            f"- 进服设备：{platform_text}\n"
            f"- 进服时间：{join_time}\n"
        )
        # 「登录 / 拒绝」回调按钮（type=1）：所有人可点，实际权限由服务端 _login_authorized 校验
        approve = Button(
            id="login_approve",
            group_id="login_review",
            render_data=RenderData(label="登录", visited_label="已处理", style=4),
            action=Action(
                type=1,
                permission=Permission(type=2),
                data=json.dumps({"op": "login_approve", "group_openid": gid,
                                 "player_name": player_name}, ensure_ascii=False),
            ),
        )
        reject = Button(
            id="login_reject",
            group_id="login_review",
            render_data=RenderData(label="拒绝", visited_label="已处理", style=3),
            action=Action(
                type=1,
                permission=Permission(type=2),
                data=json.dumps({"op": "login_reject", "group_openid": gid,
                                 "player_name": player_name}, ensure_ascii=False),
            ),
        )
        keyboard = KeyboardPayload(
            content=Keyboard(rows=[KeyboardRow(buttons=[approve, reject])])
        )
        # 群未开主动消息权限时发送失败：只记日志（玩家仍可用"登录"指令批准）
        plain = self._markdown_to_plain(content, at_line, f"@{player_name}")
        for kwargs in (
            {"msg_type": 2, "markdown": MarkdownPayload(content=content), "keyboard": keyboard},
            {"msg_type": 0, "content": plain, "keyboard": keyboard},
        ):
            try:
                await self.api.post_group_message(group_openid=gid, **kwargs)
                _log.info("已推送登录请求卡: %s", key)
                return
            except Exception as e:
                _log.warning("登录请求卡推送失败（群可能未开主动消息）group=%s: %s", gid[:8], e)

    def _clear_login_push_limit(self, player_name: str):
        """清除某玩家的登录请求卡推送限流记录：清空设备/批准登录后，下次进服拦截应立即推新卡。"""
        suffix = f"|{player_name}"
        for k in [k for k in self._login_push_last if k.endswith(suffix)]:
            self._login_push_last.pop(k, None)

    async def _handle_login_button(self, interaction, data: dict):
        """登录请求卡的「登录 / 拒绝」按钮回调（与 `登录` / `取消 <玩家名>` 指令同逻辑）。
        批准/拒绝权限 = 白名单绑定人本人（跨群用绑定邮箱一致桥接）。"""
        group_openid = data.get("group_openid") or ""
        player_name = (data.get("player_name") or "").strip()
        op = data.get("op") or ""
        clicker = (getattr(interaction, "group_member_openid", None)
                   or getattr(interaction, "user_openid", None) or "")
        at_tag = f'<qqbot-at-user id="{clicker}" />' if clicker else ""
        at_text = f"@{self._disp(group_openid, clicker)}" if clicker else ""

        async def _notice(text: str):
            """按钮结果回执：优先 event_id 事件被动发送（免主动消息权限）；标题下 @ 操作者，失败降级纯文本"""
            text = self._insert_executor_at(text, clicker)
            plain = self._markdown_to_plain(text, at_tag, at_text)
            for kwargs in ({"msg_type": 2, "markdown": MarkdownPayload(content=text),
                            "event_id": getattr(interaction, "event_id", None)},
                           {"msg_type": 2, "markdown": MarkdownPayload(content=text)}):
                if not kwargs.get("event_id") and "event_id" in kwargs:
                    continue
                try:
                    await self.api.post_group_message(group_openid=group_openid, **kwargs)
                    return
                except Exception as e:
                    _log.warning("登录按钮回执发送失败：%s", e)
            try:
                await self.api.post_group_message(
                    group_openid=group_openid, msg_type=0, content=plain)
            except Exception as e:
                _log.warning("登录按钮回执纯文本发送失败: %s", e)

        try:
            if not (group_openid and player_name and clicker):
                return
            # 在本群+联合群范围找待批准记录（与指令一致）
            p_gid, pending = None, None
            for sg in sorted(self.registry.zone_gids(group_openid)):
                p = self.pending_store.get(sg, player_name)
                if p is not None:
                    p_gid, pending = sg, p
                    break
            if pending is None:
                await _notice("## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                              f"### 没有找到 `{player_name}` 的待批准请求喵\n\n"
                              "> 请先用该玩家名进服一次后，再点「登录」或发送 `登录`")
                return
            if not self._login_authorized(p_gid, player_name, clicker, group_openid):
                await _notice("## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                              f"### 无权操作 `{player_name}` 的设备登录喵\n\n"
                              "> 只有绑定该白名单的 QQ 本人（或使用同一绑定邮箱的账号）才能操作")
                return
            if op == "login_approve":
                ok = self.whitelist_store.approve_device(
                    p_gid, player_name, pending.get("uuid") or "",
                    platform=pending.get("platform") or "", city=pending.get("city") or "")
                if not ok:
                    await _notice("## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                                  "### 批准失败喵，请重新进服一次后再试")
                    return
                self.pending_store.pop(p_gid, player_name)
                self._clear_login_push_limit(player_name)  # 已批准：解除推送限流，下次换设备进服立即推新卡
                await _notice("## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                              "### ✅账号成功登录!\n请重新进入服务器喵")
            else:
                self.pending_store.pop(p_gid, player_name)
                await _notice("## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                              f"### ❌ 已拒绝 `{player_name}` 的设备登录喵\n\n"
                              "> 该设备仍无法进服；如需登录请重新进服后再点「登录」或发送 `登录`")
        finally:
            try:
                await self._reply_interaction(interaction)  # 无论成败都结束按钮 loading
            except Exception as e:
                _log.warning("结束登录按钮交互失败: %s", e)

    def _login_authorized(self, p_gid: str, player_name: str, user_openid: str, cur_gid: str) -> bool:
        """换设备登录的批准/撤销权限：绑定人本人；跨群时"绑定邮箱一致 = 同一人"桥接。
        旧记录未绑定人（bind_openid 为空）返回 True：保留首次批准认领（claim_bind）逻辑。"""
        if not user_openid:
            return False
        rec = self.whitelist_store.get_record(p_gid, player_name)
        if rec is None:
            return False
        bind_openid = rec.get("bind_openid") or ""
        if not bind_openid:
            return True
        if bind_openid == user_openid:
            return True
        rec_email = (rec.get("email") or "").lower()
        if not rec_email:
            return False
        for r in (self.whitelist_store._data.get(self._eff_gid(cur_gid), {}) or {}).values():
            if r.get("bind_openid") == user_openid and (r.get("email") or "").lower() == rec_email:
                return True
        return False

    async def cmd_login(self, message, text: str, gid):
        """登录 [玩家名]：批准换设备进服（插件提示"在群里发送 /登录"）。
        进服被判 need_login 时，BOT 已记录该玩家待批准的新设备（记录在玩家绑定群）。
        不带玩家名时按发送者 openid 锁定"本人"的待批准请求；支持跨群批准：可在**本群或任何联合群**中被批准；
        批准者身份校验 = 白名单绑定人（同群 openid 匹配），跨群时用"绑定邮箱一致=同一人"桥接。
        """
        try:
            user_openid = message.author.member_openid or ""
        except AttributeError:
            user_openid = ""
        text = text.lstrip("/").strip()
        parts = text.split()
        player_name = parts[1] if len(parts) >= 2 else ""

        # 批准范围：本群 + 全部联合群
        scope_gids = sorted(self.registry.zone_gids(gid))

        def _find_pending(name: str):
            """在本群+联合群里找 name 的待批准记录，返回 (所在群, pending)"""
            for sg in scope_gids:
                p = self.pending_store.get(sg, name)
                if p is not None:
                    return sg, p
            return None, None

        # 没带玩家名：按发送者 openid 锁定"本人"的待批准请求（旧记录无绑定人时需显式带名字认领；同名合并）
        if not player_name:
            mine = []
            for sg in scope_gids:
                for nm in self.pending_store.group_pending(sg):
                    if nm not in mine and self._login_authorized(sg, nm, user_openid, gid):
                        mine.append(nm)
            if not mine:
                await self._reply_markdown(
                    message,
                    "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                    "**没有找到您的待批准设备登录请求喵**\n\n"
                    "> 请先用该玩家名进服一次（提示未授权设备后），再来发送 `登录`\n"
                    "> 旧白名单首次换设备请带上名字：`登录 <进服玩家名>`",
                )
                return
            if len(mine) > 1:
                names = "、".join(f"`{n}`" for n in mine)
                await self._reply_markdown(
                    message,
                    "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                    "**您有多个待批准请求，请指定玩家名喵**\n\n"
                    f"待批准：{names}\n\n"
                    "> 格式：`登录 <进服玩家名>`",
                )
                return
            player_name = mine[0]

        p_gid, pending = _find_pending(player_name)
        if pending is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                f"**没有找到 `{player_name}` 的待批准请求喵**\n\n"
                "> 请先用该玩家名进服一次（提示未授权设备后），再来发送 `登录`",
            )
            return

        rec = self.whitelist_store.get_record(p_gid, player_name)
        if rec is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                f"**`{player_name}` 不在白名单中喵**\n\n"
                "> 请先通过 `添加白名单 <玩家名> <验证码>` 绑定",
            )
            return
        bind_openid = rec.get("bind_openid") or ""
        if bind_openid and not self._login_authorized(p_gid, player_name, user_openid, gid):
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                f"**无权批准 `{player_name}` 的设备登录喵**\n\n"
                "> 只有绑定该白名单的 QQ 本人（或使用同一绑定邮箱的账号）才能批准换设备登录",
            )
            return
        if not bind_openid:
            # 旧记录没有绑定人：首次批准视为本人认领，写入绑定人防止冒认
            self.whitelist_store.claim_bind(p_gid, player_name, user_openid)

        # 批准：把当前设备加入该玩家的设备列表（多设备互不顶），解除改名后的待重登标记
        ok = self.whitelist_store.approve_device(
            p_gid, player_name, pending.get("uuid") or "",
            platform=pending.get("platform") or "", city=pending.get("city") or "")
        if not ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                "**批准失败喵，请重新进服一次后再试**\n\n"
                "> 若反复失败请联系管理员",
            )
            return
        self.pending_store.pop(p_gid, player_name)
        self._clear_login_push_limit(player_name)  # 已批准：解除推送限流，下次换设备进服立即推新卡

        # 简版成功卡（@ 用户由 _reply_markdown 自动插在标题下方）
        await self._reply_markdown(
            message,
            "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
            "✅账号成功登录!\n"
            "请重新进入服务器喵",
        )

    async def cmd_cancel_login(self, message, text: str, gid):
        """取消 <玩家名>：撤销一条待批准的设备登录请求（仅绑定人本人/同一绑定邮箱账号可撤销）。
        撤销后该设备仍无法进服；如需批准请重新进服触发请求后再发送 `登录`。"""
        try:
            user_openid = message.author.member_openid or ""
        except AttributeError:
            user_openid = ""
        parts = text.lstrip("/").strip().split()
        player_name = parts[1] if len(parts) >= 2 else ""
        scope_gids = sorted(self.registry.zone_gids(gid))

        def _find_pending(name: str):
            """在本群+联合群里找 name 的待批准记录，返回 (所在群, pending)"""
            for sg in scope_gids:
                p = self.pending_store.get(sg, name)
                if p is not None:
                    return sg, p
            return None, None

        # 没带玩家名：按发送者 openid 找出"本人"的待批准请求
        if not player_name:
            mine = [nm for sg in scope_gids for nm in self.pending_store.group_pending(sg)
                    if self._login_authorized(sg, nm, user_openid, gid)]
            if len(mine) == 1:
                player_name = mine[0]
            elif not mine:
                await self._reply_markdown(
                    message,
                    "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                    "**没有找到您的待批准登录请求喵**\n\n"
                    "> 格式：`取消 <进服玩家名>`",
                )
                return
            else:
                names = "、".join(f"`{n}`" for n in mine)
                await self._reply_markdown(
                    message,
                    "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                    "**您有多个待批准请求，请指定玩家名喵**\n\n"
                    f"待批准：{names}\n\n"
                    "> 格式：`取消 <进服玩家名>`",
                )
                return

        p_gid, pending = _find_pending(player_name)
        if pending is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                f"**没有找到 `{player_name}` 的待批准请求喵**\n\n"
                "> 格式：`取消 <进服玩家名>`",
            )
            return
        if not self._login_authorized(p_gid, player_name, user_openid, gid):
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                f"**无权撤销 `{player_name}` 的设备登录请求喵**\n\n"
                "> 只有绑定该白名单的 QQ 本人（或使用同一绑定邮箱的账号）才能撤销",
            )
            return
        self.pending_store.pop(p_gid, player_name)
        await self._reply_markdown(
            message,
            "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
            f"✅ **已撤销 `{player_name}` 的设备登录请求喵**\n\n"
            "> 该设备仍无法进服；如需批准请重新进服一次后发送 `登录`",
        )

    async def cmd_clear_devices(self, message, text: str, gid):
        """清空设备 [玩家名]：清空本人已登录的全部设备（下次进服须重新登录批准）。
        联合群共用：本群与联合区内任意群均可操作同一份记录（登录/清空设备数据归总群）；
        仅绑定人本人（或使用同一绑定邮箱的账号）可清空；不带玩家名时按发送者 openid 自动定位。"""
        try:
            user_openid = message.author.member_openid or ""
        except AttributeError:
            user_openid = ""
        parts = text.lstrip("/").strip().split()
        player_name = parts[1] if len(parts) >= 2 else ""
        scope_gids = sorted(self.registry.zone_gids(gid))

        # 没带玩家名：按发送者 openid 在本群+联合群范围定位"本人"的白名单记录（同名合并，只算一个）
        if not player_name:
            mine = []
            for sg in scope_gids:
                for nm in (self.whitelist_store._data.get(sg, {}) or {}):
                    if nm not in mine and self._login_authorized(sg, nm, user_openid, gid):
                        mine.append(nm)
            if not mine:
                await self._reply_markdown(
                    message,
                    "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                    "**没有找到您绑定的白名单记录喵**\n\n"
                    "> 格式：`清空设备 <进服玩家名>`（仅能清空绑定本人的记录）",
                )
                return
            if len(mine) > 1:
                names = "、".join(f"`{n}`" for n in mine)
                await self._reply_markdown(
                    message,
                    "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                    "**您绑定了多个玩家名，请指定要清空的名字喵**\n\n"
                    f"可选：{names}\n\n"
                    "> 格式：`清空设备 <进服玩家名>`",
                )
                return
            player_name = mine[0]

        # 在本群+联合群范围找该玩家名的全部记录（联合区同名记录一并清空；逐条校验绑定人）
        exists = [sg for sg in scope_gids
                  if self.whitelist_store.get_record(sg, player_name) is not None]
        if not exists:
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                f"**`{player_name}` 不在白名单中喵**\n\n"
                "> 请先通过 `添加白名单 <玩家名> <验证码>` 绑定",
            )
            return
        targets = [sg for sg in exists
                   if self._login_authorized(sg, player_name, user_openid, gid)]
        if not targets:
            await self._reply_markdown(
                message,
                "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
                f"**无权清空 `{player_name}` 的设备喵**\n\n"
                "> 只有绑定该白名单的 QQ 本人（或使用同一绑定邮箱的账号）才能清空设备",
            )
            return

        cnt = 0
        for sg in targets:
            cnt += max(self.whitelist_store.clear_devices(sg, player_name), 0)
        self._clear_login_push_limit(player_name)  # 清空设备：解除推送限流，重新进服拦截应立即推新卡
        extra = f"- 覆盖联合区 {len(targets)} 处同名记录\n" if len(targets) > 1 else ""
        await self._reply_markdown(
            message,
            "## ꧁༺ ZSE登录系统 ༻꧂\n\n"
            f"✅ **已清空 `{player_name}` 的设备登录记录喵**\n\n"
            f"- 已清除设备：{cnt} 台\n"
            f"{extra}"
            "- 下次进服须重新登录批准（联合区内所有群通用）\n\n"
            "> 重新进服出现登录请求后：点卡片「登录」按钮，或发送 `登录` 批准",
        )

    async def cmd_player_query(self, message, text: str, gid):
        """玩家查询 <玩家名>：查看某玩家的白名单绑定信息（邮箱/绑定时间/最后进服时间）。
        数据按总群口径（联合区内查询总群白名单）。"""
        name = text[len("玩家查询"):].lstrip("：: \t").strip()
        if not name:
            await self._reply_markdown(
                message,
                "## ꧁༺ 信息查询 ༻꧂\n\n"
                "**缺少玩家名喵...**\n\n"
                "格式：`玩家查询 <玩家名>`\n"
                "例：`玩家查询 星梦`",
            )
            return
        rec = self.whitelist_store.get_record(self._eff_gid(gid), name)
        if rec is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ 信息查询 ༻꧂\n\n"
                f"**未找到 `{name}` 的绑定记录喵**\n\n"
                "> 请确认玩家名正确，且已在总群绑定白名单",
            )
            return

        def _fmt(ts):
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "（暂无）"

        lines = [
            "## ꧁༺ 信息查询 ༻꧂",
            "",
            f"- 玩家名字：`{name}`",
            f"- 绑定邮箱：`{rec.get('email') or '（未绑定邮箱）'}`",
            f"- 添加白名单时间：{_fmt(rec.get('bind_time'))}",
            f"- 最后进服时间：{_fmt(rec.get('last_join_time'))}",
        ]
        if rec.get("frozen"):
            lines.append("")
            lines.append("> ⚠️ 该账号白名单当前已被冻结（重新入群自动解冻）喵")
        await self._reply_markdown(message, "\n".join(lines))

    # ───────────────────────── 权限管理（四身份） ─────────────────────────
    def _sweep_changes(self):
        """懒回滚：清理超时未完成的邮箱改绑（自动恢复原白名单并打日志）"""
        try:
            rolled = self.changes.sweep(self.whitelist_store)
            for openid, name in rolled:
                _log.info("邮箱改绑超时未完成，已自动恢复原白名单：%s（%s…）", name, openid[:8])
        except Exception as e:  # 回滚失败不影响主流程
            _log.warning("邮箱改绑懒回滚失败：%s", e)

    def _eff_gid(self, gid: str) -> str:
        """联合区数据归属群（总群）。gid 为空原样返回"""
        if not gid:
            return ""
        return self.registry.effective_gid(gid) or gid

    def _user_openid(self, message) -> str:
        """从消息取发送者 member_openid（容错）"""
        return getattr(getattr(message, "author", None), "member_openid", None) or ""

    def _resolve_openid(self, gid, player_name: str) -> str:
        """按玩家名反查绑定人的 openid（需该玩家已添加白名单，用于分配身份时确认目标 QQ）"""
        rec = self.whitelist_store.get_record(gid, player_name)
        return (rec or {}).get("bind_openid") or ""

    def _disp(self, gid, openid: str) -> str:
        """openid 展示名：优先白名单玩家名，否则截断 openid"""
        nm = self.whitelist_store.find_by_openid(gid, openid)
        return nm or (f"{openid[:8]}…" if openid else "未知用户")

    def _player_avatar_url(self, gid, player_name: str, size: int = 640) -> str:
        """按玩家名反查其绑定 QQ（白名单 bind_openid），构造 qlogo 头像 URL；查不到（未绑定白名单）返回空串"""
        if not self._appid:
            return ""
        rec = self.whitelist_store.get_record(gid, player_name)
        oid = (rec or {}).get("bind_openid") or ""
        if not oid:
            return ""
        return f"https://q.qlogo.cn/qqapp/{self._appid}/{oid}/{size}"

    def _avatar_md(self, gid, player_name: str, px: int = 20) -> str:
        """Markdown 内联头像片段（玩家已绑定白名单才有 QQ 头像；未绑定时返回空串，不显示）"""
        url = self._player_avatar_url(gid, player_name)
        return f"![头像 #{px}px #{px}px]({url})" if url else ""

    def _openid_avatar_md(self, openid: str, size: int = 640, px: int = 20) -> str:
        """按 openid 直接构造 qlogo 头像 Markdown 片段"""
        if not self._appid or not openid:
            return ""
        url = f"https://q.qlogo.cn/qqapp/{self._appid}/{openid}/{size}"
        return f"![头像 #{px}px #{px}px]({url})"

    async def _perm_ok(self, message, gid, user_openid: str, perm: str, cmd_label: str) -> bool:
        """权限不足时按命令标题回复模板卡片并返回 False"""
        if self.perms.check(self._eff_gid(gid), user_openid, perm):
            return True
        await self._reply_markdown(
            message,
            "\n".join([
                f"## ꧁༺ {cmd_label} ༻꧂",
                "",
                f"**❌ 您没有权限执行「{cmd_label}」喵...**",
                "",
                f"> 该操作需要身份：`{PERM_NEED_LABEL.get(perm, '管理员及以上')}`",
                f"> 您的身份：`{role_label(self.perms.role_of(self._eff_gid(gid), user_openid))}`",
            ]),
        )
        return False

    def _role_summary(self, gid) -> list:
        """群内身份配置展示行（带 QQ 头像）"""
        gid = self._eff_gid(gid)
        perms = self.perms
        owners = perms.owners_of(gid)
        masters = perms.members_of_role(gid, MASTER)
        admins = perms.members_of_role(gid, ADMIN)

        def fmt(oids):
            if not oids:
                return "（无）"
            parts = []
            for x in oids:
                av = self._avatar_md(gid, self.whitelist_store.find_by_openid(gid, x))
                parts.append(f"{av}{self._disp(gid, x)}")
            return "、".join(parts)

        lines = [
            f"高级管理员：{fmt(owners) if owners else '（未设置）'}",
            f"服主：{fmt(masters)}",
            f"管理员：{fmt(admins)}",
            f"允许成员获取地图：{'已开启' if perms.map_allowed(gid) else '已关闭'}",
            f"允许查看在线玩家：{'已开启' if perms.show_online_players(gid) else '已关闭'}",
        ]
        return lines

    async def cmd_bootstrap_owner(self, message, text: str, gid, user_openid: str):
        """设置高级管理员 <玩家名>：仅当本群尚无任何高级管理员时可执行（一次性追授/退群接管后恢复）。
        目标玩家需已绑定白名单（以此确认其 openid）。高级管理员之间可直接用 `设置身份 <玩家名> 高级管理员` 互相添加。"""
        parts = text.split()
        if len(parts) < 2:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                "**缺少参数喵...**\n\n"
                "格式：`设置高级管理员 <玩家名>`\n"
                "例：`设置高级管理员 星梦`",
            )
            return
        player_name = parts[1]
        if self.perms.owners_of(self._eff_gid(gid)):
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                "**本群已有高级管理员喵**\n\n"
                "> 由现任高级管理员发送 `设置身份 <玩家名> 高级管理员` 即可互相添加",
            )
            return
        target_oid = self._resolve_openid(self._eff_gid(gid), player_name)
        if not target_oid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                f"**❌ 无法确认 `{player_name}` 的身份喵**\n\n"
                "> 请先让目标用户在群内发送 `添加白名单 <玩家名> <验证码>` 完成白名单绑定，再执行本命令",
            )
            return
        self.perms.set_first_owner(self._eff_gid(gid), target_oid, who=user_openid, note="无高级管理员时追授")
        await self._reply_markdown(
            message,
            "## ꧁༺ 身份设置 ༻꧂\n\n"
            f"✅ 已将 `{player_name}` 设置为**高级管理员**喵！\n\n"
            "> 可发送 `权限查询` 查看当前身份配置",
        )

    async def cmd_set_role(self, message, text: str, gid, user_openid: str):
        """设置身份 <玩家名> <高级管理员|master|admin>：高级管理员之间互相添加、并向下分配服主/管理员"""
        parts = text.split()
        if len(parts) < 3:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                "**缺少参数喵...**\n\n"
                "格式：`设置身份 <玩家名> <高级管理员|master|admin>`\n"
                "例：`设置身份 星梦 高级管理员`",
            )
            return
        player_name, role_text = parts[1], parts[2]
        role = ROLE_ALIAS.get(role_text.lower()) or ROLE_ALIAS.get(role_text)
        if role not in (OWNER, MASTER, ADMIN):
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                "**身份参数错误喵...**\n\n"
                "> 支持的身份：`高级管理员`(owner)、`master`(服主)、`admin`(管理员)\n"
                "> 例：`设置身份 星梦 admin`",
            )
            return
        target_oid = self._resolve_openid(self._eff_gid(gid), player_name)
        if not target_oid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                f"**❌ 无法确认 `{player_name}` 的身份喵**\n\n"
                "> 请先让目标用户在群内发送 `添加白名单 <玩家名> <验证码>` 完成白名单绑定，再执行本命令",
            )
            return
        if role == OWNER:
            ok, msg = self.perms.add_owner(self._eff_gid(gid), target_oid, operator=user_openid)
        else:
            ok, msg = self.perms.add_role(self._eff_gid(gid), target_oid, role, operator=user_openid)
        if ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                f"✅ 已将 `{player_name}` 设置为**{role_label(role)}**喵！\n\n"
                "> 可发送 `权限查询` 查看当前身份配置",
            )
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                f"**❌ {msg}喵**",
            )

    async def cmd_remove_role(self, message, text: str, gid, user_openid: str):
        """取消身份 <玩家名> [高级管理员|master|admin]：收回身份（高级管理员至少保留一名）"""
        parts = text.split()
        if len(parts) < 2:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                "**缺少参数喵...**\n\n"
                "格式：`取消身份 <玩家名> [高级管理员|master|admin]`\n"
                "例：`取消身份 星梦 admin`",
            )
            return
        player_name = parts[1]
        role_text = parts[2] if len(parts) > 2 else ""
        role = (ROLE_ALIAS.get(role_text.lower()) or ROLE_ALIAS.get(role_text) or "")
        if role_text and not role:
            # 参数非空但无法识别时直接报错，避免 role="" 被当成"取消全部管理身份"误伤
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                "**❌ 未知身份喵...**\n\n"
                "> 支持的身份：`高级管理员`(owner)、`服主`(master)、`管理员`(admin)\n"
                "> 不填身份 = 取消该成员的全部管理身份",
            )
            return
        target_oid = self._resolve_openid(self._eff_gid(gid), player_name)
        if not target_oid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                f"**❌ 无法确认 `{player_name}` 的身份喵**\n\n"
                "> 请先让目标用户在群内绑定白名单后重试",
            )
            return
        if role == OWNER:
            ok, msg = self.perms.remove_owner(self._eff_gid(gid), target_oid, operator=user_openid)
        else:
            ok, msg = self.perms.remove_role(self._eff_gid(gid), target_oid, role, operator=user_openid)
        if ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                f"✅ 已取消 `{player_name}` 的{role_label(role) if role else '管理身份'}喵！",
            )
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 身份设置 ༻꧂\n\n"
                f"**❌ {msg}喵**",
            )

    async def cmd_perm_query(self, message, gid, user_openid: str):
        """权限查询：查看本群身份配置与自己的身份"""
        my_role = role_label(self.perms.role_of(self._eff_gid(gid), user_openid))
        lines = self._role_summary(gid)
        await self._reply_markdown(
            message,
            "\n".join([
                "## ꧁༺ 权限查询 ༻꧂",
                "",
                f"您的身份：**{my_role}**",
                "",
                "────────────",
                *lines,
                "────────────",
                "",
                "> 高级管理员之间可通过 `设置身份 <玩家名> 高级管理员` 互相添加",
            ]),
        )

    async def cmd_group_info(self, message, gid):
        """群信息：任何群员可查。展示本群群ID、openid、联合关系、白名单人数统计"""
        if not gid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 群信息 ༻꧂\n\n"
                "**无法识别本群喵...**",
            )
            return
        # 先确保登记，再取联合区信息（zone_info 不分配）
        self.registry.get_or_assign(gid)
        zi = self.registry.zone_info(gid)
        # 白名单统计（总群口径）
        eff = self.registry.effective_gid(gid)
        bound_cnt = 0
        frozen_cnt = 0
        for rec in (self.whitelist_store._data.get(eff, {}) or {}).values():
            if rec.get("frozen"):
                frozen_cnt += 1
            else:
                bound_cnt += 1
        # 已联合群列表：过滤自己（含总群），按加入顺序排序
        members = [m for m in zi["members"] if m["gid"] != gid]
        members.sort(key=lambda m: m.get("join_order") or 0)
        member_ids = [str(m["join_id"]) for m in members]
        mem_line = "、".join(member_ids) if member_ids else "（无）"
        master_join_id = zi.get("master_join_id")
        my_join_id = self.registry.join_id_of(gid) or ""
        # 独立群（自己既是"总群"又没有子群）→ 总群一栏显示（无），避免误导
        is_standalone = (zi.get("master_gid") == gid) and len(zi["members"]) <= 1
        master_line = "（无）" if is_standalone else (master_join_id or "（无）")
        # 总群/独立群卡
        lines = [
            "## ꧁༺ 群信息 ༻꧂",
            "",
            f"- 群ID: `{my_join_id}`",
            f"- 群OpenID: `{gid}`",
            f"- 当前联合总群：{master_line}",
            f"- 已联合群: {mem_line}",
            f"- 已绑定的白名单人数: {bound_cnt}",
            f"- 已冻结的白名单人数: {frozen_cnt}",
        ]
        # 子群卡：追加联合说明
        if zi.get("master_gid") and zi["master_gid"] != gid:
            lines += [
                "",
                "────────────",
                "> 本群已与总群建立联合关系，当前将显示总群的数据喵!",
                f"> 联合时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(zi['joined_at']))} "
                f"本群是第{zi['join_order']}个与总群建立联合关系喵！",
            ]
        await self._reply_markdown(message, "\n".join(lines))

    async def cmd_link_group(self, message, text: str, gid, user_openid: str):
        """绑定联合群 <群ID>：操作者须同时为本群与目标群高级管理员，双方建立联合关系"""
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await self._reply_markdown(
                message,
                "## ꧁༺ 绑定联合群 ༻꧂\n\n"
                "**缺少参数喵...**\n\n"
                "格式：`绑定联合群 <群ID>`\n"
                "> 群ID 请在目标群发送 `群信息` 查看",
            )
            return
        if not gid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 绑定联合群 ༻꧂\n\n"
                "**无法识别本群喵...**",
            )
            return
        jid = int(parts[1])
        # 本群 owner 校验（联合区口径）
        if not self.perms.is_owner(self._eff_gid(gid), user_openid):
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 绑定联合群 ༻꧂",
                    "",
                    "**❌ 您没有权限执行「绑定联合群」喵...**",
                    "",
                    "> 该操作需要身份：`高级管理员`",
                    f"> 您的身份：`{role_label(self.perms.role_of(self._eff_gid(gid), user_openid))}`",
                ]),
            )
            return
        target_gid = self.registry.resolve_by_join_id(jid)
        if not target_gid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 绑定联合群 ༻꧂\n\n"
                f"**找不到群ID为 {jid} 的群喵**\n\n"
                "> 该群可能尚未发送过 `群信息`，请先让其登记群ID",
            )
            return
        if target_gid == gid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 绑定联合群 ༻꧂\n\n"
                "**不能联合自己喵**",
            )
            return
        # 双方高级管理员：操作者须同时是本群与目标群本群的高级管理员（resolve 已保证双方注册）
        if not (self.perms.is_owner(self._eff_gid(gid), user_openid)
                and self.perms.is_owner(self._eff_gid(target_gid), user_openid)):
            await self._reply_markdown(
                message,
                "## ꧁༺ 绑定联合群 ༻꧂\n\n"
                "**您必须同时是本群与目标群的高级管理员才能完成联合喵**",
            )
            return
        ok, msg = self.registry.bind(gid, target_gid)
        if ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ 绑定联合群 ༻꧂\n\n"
                f"✅ **{msg}**",
            )
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 绑定联合群 ༻꧂\n\n"
                f"**❌ {msg}**",
            )

    async def cmd_unlink_group(self, message, text: str, gid, user_openid: str):
        """解除联合群 <群ID>：本群高级管理员可解除与目标群的联合关系"""
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await self._reply_markdown(
                message,
                "## ꧁༺ 解除联合群 ༻꧂\n\n"
                "**缺少参数喵...**\n\n"
                "格式：`解除联合群 <群ID>`\n"
                "> 群ID 请发送 `群信息` 查看\n"
                "> 总群：填写要摘除的子群ID；子群：填写本群或总群ID",
            )
            return
        if not gid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 解除联合群 ༻꧂\n\n"
                "**无法识别本群喵...**",
            )
            return
        jid = int(parts[1])
        # 本群 owner 校验（联合区口径）
        if not self.perms.is_owner(self._eff_gid(gid), user_openid):
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 解除联合群 ༻꧂",
                    "",
                    "**❌ 您没有权限执行「解除联合群」喵...**",
                    "",
                    "> 该操作需要身份：`高级管理员`",
                    f"> 您的身份：`{role_label(self.perms.role_of(self._eff_gid(gid), user_openid))}`",
                ]),
            )
            return
        target_gid = self.registry.resolve_by_join_id(jid)
        if not target_gid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 解除联合群 ༻꧂\n\n"
                f"**找不到群ID为 {jid} 的群喵**",
            )
            return
        if target_gid not in self.registry.zone_gids(gid):
            await self._reply_markdown(
                message,
                "## ꧁༺ 解除联合群 ༻꧂\n\n"
                "**该群与本群没有联合关系喵**",
            )
            return
        # 按参数精确摘除：总群摘指定子群，子群填本群/总群ID视为本群脱离
        ok, msg, _ = self.registry.unbind(gid, target_gid)
        if not ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ 解除联合群 ༻꧂\n\n"
                f"**❌ {msg}**",
            )
            return
        # 解除联合后：可见性按新的联合关系实时计算，无需清理共享数据
        await self._reply_markdown(
            message,
            "## ꧁༺ 解除联合群 ༻꧂\n\n"
            f"✅ **{msg}**",
        )

    async def cmd_map_toggle(self, message, text: str, gid, user_openid: str):
        """允许成员获取地图 [开|关]：admin 及以上可切换普通成员获取地图的开关（无参数=切换）"""
        rest = text[len("允许成员获取地图"):].strip().lower()
        cur = self.perms.map_allowed(self._eff_gid(gid))
        if not rest:
            flag = not cur
        elif rest in ("开", "开启", "on", "true", "1", "yes"):
            flag = True
        elif rest in ("关", "关闭", "off", "false", "0", "no"):
            flag = False
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 地图权限 ༻꧂\n\n"
                "**参数格式错误喵...**\n\n"
                "格式：`允许成员获取地图 [开|关]`\n"
                "例：`允许成员获取地图 开`",
            )
            return
        _ok, msg = self.perms.set_map_allowed(self._eff_gid(gid), flag, operator=user_openid)
        await self._reply_markdown(
            message,
            "\n".join([
                "## ꧁༺ 地图权限 ༻꧂",
                "",
                f"✅ 已{msg}「允许成员获取地图」喵！",
                "",
                f"> 普通群员获取地图：{'可' if flag else '不可'}",
                "> 管理员及以上：始终可",
            ]),
        )

    async def cmd_server_status_notify(self, message, text: str, gid, user_openid: str):
        """服务器通知 [开|关]：服务器上线/掉线时是否在本群播报（无参数=切换）"""
        rest = text[len("服务器通知"):].strip().lower()
        cur = self.perms.notify_server_status(self._eff_gid(gid))
        if not rest:
            flag = not cur
        elif rest in ("开", "开启", "on", "true", "1", "yes"):
            flag = True
        elif rest in ("关", "关闭", "off", "false", "0", "no"):
            flag = False
        else:
            await self._reply_markdown(message, "\n".join([
                "## ꧁༺ 服务器通知 ༻꧂", "",
                "**参数格式错误喵...**", "",
                "格式：`服务器通知 [开|关]`", "例：`服务器通知 关`",
            ]))
            return
        _ok, msg = self.perms.set_notify_server_status(self._eff_gid(gid), flag, operator=user_openid)
        await self._reply_markdown(message, "\n".join([
            "## ꧁༺ 服务器通知 ༻꧂", "",
            f"✅ 已{msg}「服务器上线/掉线通知」喵！", "",
            "> 状态需稳定 2 分钟才播报（避免 TShock 重启/网络抖动刷屏）",
        ]))

    async def cmd_online_show(self, message, text: str, gid, user_openid: str):
        """允许查看在线玩家 [开|关]：admin 及以上可切换在线玩家名单是否对普通群员可见（无参数=切换）"""
        rest = text[len("允许查看在线玩家"):].strip().lower()
        cur = self.perms.show_online_players(self._eff_gid(gid))
        if not rest:
            flag = not cur
        elif rest in ("开", "开启", "on", "true", "1", "yes"):
            flag = True
        elif rest in ("关", "关闭", "off", "false", "0", "no"):
            flag = False
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 在线显示 ༻꧂\n\n"
                "**参数格式错误喵...**\n\n"
                "格式：`允许查看在线玩家 [开|关]`\n"
                "例：`允许查看在线玩家 关`",
            )
            return
        _ok, msg = self.perms.set_show_online_players(self._eff_gid(gid), flag, operator=user_openid)
        await self._reply_markdown(
            message,
            "\n".join([
                "## ꧁༺ 在线显示 ༻꧂",
                "",
                f"✅ 已{msg}「允许查看在线玩家」喵！",
                "",
                f"> 普通群员查看在线玩家名单：{'可' if flag else '不可'}"
                + ("（关闭后仅显示在线人数，隐藏玩家名单）" if not flag else ""),
            ]),
        )

    async def on_interaction_create(self, interaction: Interaction):
        """点击"批准/拒绝"按钮回调"""
        if interaction.type != 11:  # 11=消息按钮回调
            return
        try:
            data = json.loads(interaction.data.resolved.button_data)
        except (json.JSONDecodeError, TypeError):
            _log.error("按钮回调数据解析失败: %s", interaction.data.resolved.button_data)
            await self._reply_interaction(interaction)
            return

        group_openid = data.get("group_openid")
        member_openid = data.get("member_openid")
        join_request_id = data.get("join_request_id")
        op = data.get("op")

        # 欢迎卡片/引导卡按钮：帮助 / 关于
        cmd = data.get("cmd")
        if cmd in ("help", "about"):
            if group_openid:
                # 点击者 openid（群聊按钮回调在 group_member_openid）
                clicker = (getattr(interaction, "group_member_openid", None)
                           or getattr(interaction, "user_openid", None) or "")
                if cmd == "about":
                    content = self._about_card(group_openid, clicker)  # 与"关于"指令共用模板
                else:
                    # 旧卡片上的「帮助」按钮（type=1 回调）也切到新的帮助卡片内容
                    content = self._help_index_card(group_openid, clicker)
                # 标题下 @ 点击按钮的用户（与指令回复同款；clicker 为空时原样发送）
                content = self._insert_executor_at(content, clicker)
                at_tag = f'<qqbot-at-user id="{clicker}" />' if clicker else ""
                # 按钮回调：优先用 event_id 当"事件被动回复"发送（免主动发言权限），失败降级普通发送；
                # 全程 try 兜底，保证下面的 _reply_interaction 一定执行，避免按钮一直转圈/第三方失败
                sent = False
                base = {"msg_type": 2, "markdown": MarkdownPayload(content=content)}
                if cmd == "help":
                    base["keyboard"] = self._help_keyboard(
                        self._help_rank(group_openid, clicker))
                for kwargs in (dict(base, event_id=getattr(interaction, "event_id", None)),
                               dict(base)):
                    if not kwargs.get("event_id") and "event_id" in kwargs:
                        continue
                    try:
                        await self.api.post_group_message(group_openid=group_openid, **kwargs)
                        sent = True
                        break
                    except Exception as e:
                        _log.warning("按钮回复发送失败：%s，尝试降级", e)
                if not sent:
                    try:
                        await self.api.post_group_message(
                            group_openid=group_openid, msg_type=0,
                            content=self._markdown_to_plain(
                                content, at_tag,
                                f"@{self._disp(group_openid, clicker)}" if clicker else "")
                        )
                    except Exception as e:
                        _log.warning("按钮回复普通发送失败: %s", e)
            try:
                await self._reply_interaction(interaction)  # 无论成败都结束按钮 loading
            except Exception as e:
                _log.warning("结束按钮交互失败: %s", e)
            return

        # 登录请求卡按钮：登录（批准）/ 拒绝（与「登录 / 取消」指令同逻辑）
        if op in ("login_approve", "login_reject"):
            await self._handle_login_button(interaction, data)
            return

        # 只处理群聊场景的审批按钮
        if not (group_openid and member_openid and op in ("approve", "decline")):
            _log.warning("回调数据缺少必要字段: %s", data)
            await self._reply_interaction(interaction)
            return

        # 安全校验：审批卡片发在群里、按钮对全群可见 → 必须校验点击者身份（管理员及以上）。
        # 否则任何群员点一下「批准」就能把任意申请人放进群（历史上这里确实漏了校验）。
        clicker_openid = (getattr(interaction, "group_member_openid", None)
                          or getattr(interaction, "user_openid", None) or "")
        if self.perms.rank_of(self._eff_gid(group_openid), clicker_openid) < RANK[ADMIN]:
            _log.warning("拒绝越权入群审批：group=%s clicker=%s",
                         (group_openid or "")[:8], (clicker_openid or "")[:8])
            try:
                await self.api.post_group_message(
                    group_openid=group_openid, msg_type=2,
                    markdown=MarkdownPayload(content=self._insert_executor_at(
                        f"{self.build_card_title('入群审批')}\n\n"
                        "### ⚠️ 只有 **管理员及以上** 才能审批入群申请",
                        clicker_openid)),
                    event_id=getattr(interaction, "event_id", None))
            except Exception as e:
                _log.warning("越权提示发送失败: %s", e)
            try:
                await self._reply_interaction(interaction)
            except Exception as e:
                _log.warning("结束按钮交互失败: %s", e)
            return

        if op == "approve":
            desc, reason, blacklist = "批准", None, False
        else:
            desc, reason, blacklist = "拒绝", "管理员拒绝该入群申请", False

        clicker = (getattr(interaction, "group_member_openid", None)
                   or getattr(interaction, "user_openid", None) or "")
        at_tag = f'<qqbot-at-user id="{clicker}" />' if clicker else ""
        at_text = f"@{self._disp(group_openid, clicker)}" if clicker else ""

        async def _notice(line: str):
            """审批结果回执（事件被动）：标题下 @ 操作的管理员，失败降级纯文本"""
            content = self._insert_executor_at(
                f"{self.build_card_title('入群审批')}\n\n{line}", clicker)
            try:
                await self.api.post_group_message(
                    group_openid=group_openid, msg_type=2,
                    markdown=MarkdownPayload(content=content),
                    event_id=getattr(interaction, "event_id", None),
                )
            except Exception as e:
                _log.warning("审批结果通知发送失败(可能未开主动消息)：%s", e)
                try:
                    await self.api.post_group_message(
                        group_openid=group_openid, msg_type=0,
                        content=self._markdown_to_plain(content, at_tag, at_text),
                    )
                except Exception as e2:
                    _log.warning("审批失败通知也发送失败: %s", e2)

        try:
            await self.approval_group_join_request(
                group_openid,
                member_openid,
                op=op,
                join_request_id=join_request_id,
                reject_reason=reason,
                add_to_member_blacklist=blacklist,
            )
            _log.info("已%s申请 group=%s member=%s", desc, group_openid, member_openid)
            await _notice(f"### ✅ 已{desc}该用户入群申请")
        except Exception as e:  # 具体错误码见 docs/官方接口速查.md
            _log.exception("审批调用失败")
            await _notice(f"### ⚠️ 审批失败：{e}")
        finally:
            try:
                await self._reply_interaction(interaction)
            except Exception as e:
                _log.warning("结束审批按钮交互失败: %s", e)

    # ───────────────────────── 轮询：发现新申请 → 发审核卡片 ─────────────────────────
    async def poll_join_requests(self):
        _log.info("入群申请轮询已启动，间隔 %s 秒", self.poll_interval)
        while True:
            try:
                # 取快照：on_group_add_robot/_capture_group 会在轮询期间往 self.groups 里加新群，
                # 直接遍历 .items() 会 RuntimeError: dictionary changed size during iteration
                for group_name, group_openid in list(self.groups.items()):
                    await self.check_one_group(group_name, group_openid)
            except Exception as e:
                _log.exception("轮询出错: %s", e)
            await asyncio.sleep(self.poll_interval)

    async def check_one_group(self, group_name: str, group_openid: str):
        # 机器人非群管理员时暂停该群轮询（11703），避免每 20 秒报错刷屏日志
        if self._join_poll_skip.get(group_openid, 0) > time.time():
            return
        try:
            data = await self.get_group_join_request_list(group_openid, limit=50)
        except Exception as e:
            msg = str(e)
            if "不是群管理员" in msg or "11703" in msg:
                self._join_poll_skip[group_openid] = time.time() + 1800
                _log.warning("[%s] 机器人不是群管理员，暂停该群入群申请轮询 30 分钟（不影响指令等功能）", group_openid)
            else:
                _log.warning("[%s] 拉取入群申请失败: %s", group_name, msg)
            return
        for req in data.get("list", []):
            jid = req.get("join_request_id")
            if not jid or jid in self._handled:
                continue
            self._handled.append(jid)
            # 防刷屏：同一用户 60 秒内重复发起相同理由的申请不再转发（仅在"已发过卡"后才计窗口，
            # 首次出现的申请立刻发卡，不会被延迟）
            username = req.get("username") or ""
            verify = req.get("verify_info") or {}
            reason = ""
            if verify.get("review_qa_list"):
                reason = "|".join(qa.get("answer", "") for qa in verify["review_qa_list"])
            elif verify.get("verify_message"):
                reason = verify["verify_message"]
            key = f"{group_openid}|{username}|{reason}"
            now = time.time()
            if now - self._apply_last_sent.get(key, 0) < 60:
                _log.info("[%s] 同一用户 1 分钟内重复申请，不再转发: %s（%s）", group_name, username, reason[:20])
                continue
            self._apply_last_sent[key] = now
            if len(self._apply_last_sent) > 2000:   # 清理 1 小时前的旧键，避免长期缓慢增长
                for k in [k for k, t in self._apply_last_sent.items() if now - t > 3600]:
                    self._apply_last_sent.pop(k, None)
            try:
                await self.send_review_card(group_name, group_openid, req)
            except Exception as e:
                _log.exception("发送审核卡片失败: %s", e)

    async def send_review_card(self, group_name: str, group_openid: str, req: dict):
        """组一张接近示例样式的审核卡片（Markdown + 批准/拒绝按钮）"""
        username = req.get("username") or "未知用户"

        # Markdown 正文（按指定模板：
        #   ꧁༺ {组织名} 入群申请 ༻꧂ / 用户 / 验证信息 / > 通过前请确认用户等级
        #   官方没有"QQ群等级"接口，用户等级一行省略）
        lines = [self.build_card_title("入群申请")]
        lines.append(f"**用户**: {username}")
        verify = req.get("verify_info") or {}
        if verify.get("method") == "admin_review_qa" and verify.get("review_qa_list"):
            for qa in verify["review_qa_list"]:
                lines.append(f"**问题**: {qa.get('question', '')}")
                lines.append(f"**回答**: {qa.get('answer', '')}")
        elif verify.get("verify_message"):
            lines.append(f"**验证信息**: {verify['verify_message']}")
        lines.append("---")
        lines.append("> 通过前请确认用户等级")
        markdown = MarkdownPayload(content="\n".join(lines))

        # 批准/拒绝按钮（回调按钮：type=1；只有群管理员可点：permission.type=1）
        # 两个按钮同 group_id 分组：点完一个后另一个自动置灰，避免重复审批
        base = {
            "group_openid": group_openid,
            "member_openid": req.get("member_openid", ""),
            "join_request_id": req.get("join_request_id", ""),
        }
        approve = Button(
            id="approve",
            group_id="review",  # 同组按钮：点掉一个后其余按钮自动置灰
            render_data=RenderData(label="批准", visited_label="已处理", style=4),
            action=Action(
                type=1,
                permission=Permission(type=1),
                data=json.dumps({**base, "op": "approve"}, ensure_ascii=False),
            ),
        )
        decline = Button(
            id="decline",
            group_id="review",
            render_data=RenderData(label="拒绝", visited_label="已处理", style=3),
            action=Action(
                type=1,
                permission=Permission(type=1),
                data=json.dumps({**base, "op": "decline"}, ensure_ascii=False),
            ),
        )
        keyboard = KeyboardPayload(
            content=Keyboard(rows=[KeyboardRow(buttons=[approve, decline])])
        )

        try:
            # 方式一：Markdown + 按钮（效果最接近示例）
            await self.api.post_group_message(
                group_openid=group_openid,
                msg_type=2,
                markdown=markdown,
                keyboard=keyboard,
            )
            _log.info("[%s] 已发送申请卡片: %s", group_name, username)
        except Exception as e:
            _log.warning("[%s] Markdown/按钮发送失败(可能无权限)：%s，尝试纯文本降级", group_name, e)
            # 方式二：降级为纯文本 + 按钮
            plain = "\n".join(
                [f"꧁༺ {self.apply_title} 入群申请 ༻꧂"]
                + [l for l in lines if l.startswith("**") or l.startswith(">")]
            )
            try:
                await self.api.post_group_message(
                    group_openid=group_openid, msg_type=0, content=plain
                )
            except Exception as e2:
                _log.error("[%s] 纯文本降级也失败: %s", group_name, e2)

    # ───────────────────────── 工具方法 ─────────────────────────
    async def _reply_interaction(self, interaction: Interaction):
        """回应按钮回调，结束客户端 loading（必须调用，否则一直转圈直到超时）"""
        try:
            await self.api.on_interaction_result(interaction.event_id, 0)
        except Exception as e:
            _log.warning("回应 interaction 失败: %s", e)


if __name__ == "__main__":
    cfg = load_config()
    assert cfg["appid"], "请先在 config.yaml 填写 appid"
    assert cfg["secret"], "请先在 config.yaml 填写 secret"

    intents = botpy.Intents(public_messages=True, interaction=True)
    # GROUP_MEMBER_ADD / GROUP_MEMBER_REMOVE（退群/进群事件，INTENT 1<<24，SDK 未封装属性，手动开启）
    intents.value |= 1 << 24
    client = GroupReviewClient(
        groups=cfg.get("groups", []),
        poll_interval=int(cfg.get("poll_interval_seconds", 5)),
        apply_title=cfg.get("apply_title", "入群申请"),
        github_cfg=cfg.get("github", {}),
        intents=intents,
    )
    client.run(appid=cfg["appid"], secret=cfg["secret"])