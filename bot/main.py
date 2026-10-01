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
import re
import tempfile
import time
from collections import deque

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
from zse_server import ZseServer, decode_map_png
from whitelist_mail import MailSender, PendingStore, VerifyManager, WhitelistStore, check_name_ok
from groups_registry import GroupRegistry
from permissions import (
    PermissionManager,
    OWNER, MASTER, ADMIN, MEMBER,
    ROLE_ALIAS, PERM_NEED_LABEL, role_label,
    PERM_ADD_SERVER, PERM_DEL_SERVER, PERM_EXEC,
    PERM_MAP_FETCH, PERM_MAP_TOGGLE, PERM_ONLINE_SHOW, PERM_ROLE_MANAGE,
    PERM_BROADCAST,
)
from github_monitor import (
    get_repo_stats, get_latest_pulls, get_latest_issues, get_org_repos, get_repo_stargazers,
)
from upload_media import send_group_image

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
        self.whitelist_store = WhitelistStore()
        self.pending_store = PendingStore()
        self.zse_server = ZseServer(whitelist=self.whitelist_store, pending=self.pending_store)
        # 权限系统：四身份（owner/master/admin/member）+ 群开关
        self.perms = PermissionManager()
        # 多群联合：群注册表（群ID分配 + 群间联合关系）
        self.registry = GroupRegistry()
        self.zse_server.registry = self.registry
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

    async def _start_zse_server(self):
        """启动 aiohttp 服务端，监听 TShock 插件连接"""
        from aiohttp import web
        runner = web.AppRunner(self.zse_server.build_app())
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.zse_port)
        await site.start()
        _log.info("starZSEbot 服务端已监听 %s 端口（TShock 插件连接用）", self.zse_port)

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
        # 身份归总群：若该群是联合子群，写入总群（_eff_gid）；独立/总群时即 gid 自身
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
            try:
                await self.api.post_group_message(
                    group_openid=gid, msg_type=0,
                    content="\n".join([ln for ln in lines if ln]),
                    **reply_to
                )
                _log.info("已完成入群引导纯文本降级")
                self._guide_pending.pop(gid, None)
            except Exception as e2:
                _log.error("入群引导纯文本发送也失败: %s", e2)
                self._guide_pending[gid] = time.time()  # 标记待补发：该群能通信后再发

    async def on_group_del_robot(self, event: GroupManageEvent):
        _log.info("机器人被移出群聊 group_openid=%s", event.group_openid)

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
        if low.startswith("共享服务器"):  # 共享服务器 <序号> <群ID>
            if not await self._perm_ok(message, gid, user_openid, PERM_DEL_SERVER, "共享服务器"):
                return
            await self.cmd_share_server(message, text, gid, user_openid)
            return
        if low.startswith("取消共享服务器"):  # 取消共享服务器 <序号> [群ID]
            if not await self._perm_ok(message, gid, user_openid, PERM_DEL_SERVER, "取消共享服务器"):
                return
            await self.cmd_unshare_server(message, text, gid, user_openid)
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
        if low.startswith(("远程指令", "远程执行")):  # 远程指令 <序号|all|*> <指令>
            if not await self._perm_ok(message, gid, user_openid, PERM_EXEC, "远程指令"):
                return
            await self.cmd_exec(message, text, gid)
            return
        if low.startswith("全服喊话"):  # 全服喊话 <内容>：对所有服务器广播
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
        if low.startswith("绑定邮箱"):  # 绑定邮箱 <邮箱>
            await self.cmd_bind_email(message, text, gid)
            return
        if low.startswith("添加白名单"):  # 添加白名单 <进服玩家名> <验证码>
            await self.cmd_add_whitelist(message, text, gid)
            return
        if low.startswith("登录"):  # 登录 [玩家名]：批准换设备登录
            await self.cmd_login(message, text, gid)
            return
        if low.startswith("玩家查询"):  # 玩家查询 <玩家名>：查看绑定信息
            await self.cmd_player_query(message, text, gid)
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
                    content=(
                        f"{self.build_card_title('入群欢迎')}\n"
                        f"{at_tag}\n"
                        f"🎉欢迎加入ZSE联合体喵!\n"
                        f"发送 帮助 查看更多喵!"
                        + (f"\n> 已自动解冻 {unfrozen} 条白名单喵（重新入群）" if unfrozen else "")
                    ),
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
        # 冻结其名下全部白名单
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
            "",
            "***Powered By Zykor-Club***",
            "",
            "***",
            "> 信息",
            f"> GroupID:{gid or '未知'}",
            f"> 用户id:{user_openid or '未知'}",
        ])

    # ───────────────────────── GitHub 消息推送 ─────────────────────────
    async def github_poll(self):
        """定时检测仓库新 Issue / PR，发现新动态主动推送到所有监控群"""
        interval = int(self.github_cfg.get("poll_interval_minutes", 10)) * 60
        _log.info("GitHub 轮询启动，间隔 %s 分钟", interval // 60)
        # 先跑一次建立基线（不推送），再进入循环
        await self.check_github(push_initial=False)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.check_github(push_initial=True)
            except Exception as e:
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

    async def _post_markdown(self, message, content: str, at_executor: bool = True):
        """Markdown 发送（msg_type=2），失败自动降级纯文本；at_executor=True 时在标题下插入 @ 执行人"""
        if at_executor:
            uid = self._user_openid(message)
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
            await self._reply_text(message, content)

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
            await self._reply_markdown(
                message,
                "## ꧁༺ 服务器添加 ༻꧂\n\n"
                "**执行添加成功喵!**\n\n"
                f"请确认您添加的服务器绑定码为: `{code}`\n\n"
                "> 如果服务器不对，请删除已添加的服务器并重新添加喵!",
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
        rec = next((r for r in self.zse_server.list_servers(gid) if r.get("seq") == seq), None)
        if rec is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ 删除服务器 ༻꧂\n\n"
                f"**❌ 没有序号 {seq} 的服务器喵**",
            )
            return
        # 共享来的服务器不允许共享群删除（仅归属群可删）
        if rec.get("owner_gid") != gid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 删除服务器 ༻꧂\n\n"
                "**❌ 仅服务器所属群可删除该服务器喵**",
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
        ok, msg = await self.zse_server.unregister(gid, seq)
        if ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ 删除服务器 ༻꧂\n\n"
                "💾 **已从本群踢出一个服务器喵！**",
            )
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 删除服务器 ༻꧂\n\n"
                f"**❌ {msg}**",
            )

    async def cmd_share_server(self, message, text: str, gid, user_openid: str = ""):
        """共享服务器 <序号> <群ID>：将本群服务器共享给已联合的群（共享群可在列表/在线/地图/喊话/远程指令中使用）"""
        parts = text.split()
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await self._reply_markdown(
                message,
                "## ꧁༺ 共享服务器 ༻꧂\n\n"
                "**缺少参数或格式错误喵...**\n\n"
                "格式：`共享服务器 <序号> <群ID>`\n\n"
                "> 序号请在“服务器列表”中查看，群ID 请在目标群发送 `群信息` 查看喵",
            )
            return
        seq = int(parts[1])
        rec = next((r for r in self.zse_server.list_servers(gid) if r.get("seq") == seq), None)
        if rec is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ 共享服务器 ༻꧂\n\n"
                f"**❌ 没有序号 {seq} 的服务器喵**",
            )
            return
        # 归属校验（同删除）：owner 可共享任意；master 只能共享自己添加的
        role = self.perms.role_of(self._eff_gid(gid), user_openid)
        if role != OWNER and rec.get("added_by") != user_openid:
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 共享服务器 ༻꧂",
                    "",
                    "**❌ 您只能共享自己添加的服务器喵**",
                ]),
            )
            return
        jid = int(parts[2])
        target_gid = self.registry.resolve_by_join_id(jid)
        if target_gid is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ 共享服务器 ༻꧂\n\n"
                f"**❌ 找不到群ID为 {jid} 的群喵**",
            )
            return
        if target_gid == gid:
            await self._reply_markdown(
                message,
                "## ꧁༺ 共享服务器 ༻꧂\n\n"
                "**❌ 不能共享给本群喵**",
            )
            return
        if target_gid not in self.registry.zone_gids(gid) or target_gid == gid:
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 共享服务器 ༻꧂",
                    "",
                    "**❌ 该群与本群不在同一联合区喵**",
                    "",
                    "> 请先建立联合关系后再共享",
                ]),
            )
            return
        if target_gid in (rec.get("shared_gids") or []):
            await self._reply_markdown(
                message,
                "## ꧁༺ 共享服务器 ༻꧂\n\n"
                "**❌ 该服务器已共享给此群喵**",
            )
            return
        rec.setdefault("shared_gids", []).append(target_gid)
        await self.zse_server._save()
        await self._reply_markdown(
            message,
            "## ꧁༺ 共享服务器 ༻꧂\n\n"
            f"✅ **已将该服务器共享给目标群（群ID `{jid}`）喵！**\n\n"
            "> 目标群将可在 `服务器列表 / 在线 / 获取地图 / 喊话 / 远程指令` 中使用该服务器",
        )

    async def cmd_unshare_server(self, message, text: str, gid, user_openid: str = ""):
        """取消共享服务器 <序号> [群ID]：撤销对某群（或不指定=全部）的共享"""
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await self._reply_markdown(
                message,
                "## ꧁༺ 取消共享服务器 ༻꧂\n\n"
                "**缺少参数或格式错误喵...**\n\n"
                "格式：`取消共享服务器 <序号> [群ID]`\n\n"
                "> 序号请在“服务器列表”中查看喵",
            )
            return
        seq = int(parts[1])
        rec = next((r for r in self.zse_server.list_servers(gid) if r.get("seq") == seq), None)
        if rec is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ 取消共享服务器 ༻꧂\n\n"
                f"**❌ 没有序号 {seq} 的服务器喵**",
            )
            return
        # 归属校验（同共享/删除）：owner 可取消任意；master 只能取消自己添加的
        role = self.perms.role_of(self._eff_gid(gid), user_openid)
        if role != OWNER and rec.get("added_by") != user_openid:
            await self._reply_markdown(
                message,
                "\n".join([
                    "## ꧁༺ 取消共享服务器 ༻꧂",
                    "",
                    "**❌ 您只能取消共享自己添加的服务器喵**",
                ]),
            )
            return
        shared = rec.setdefault("shared_gids", [])
        if len(parts) >= 3:
            if not parts[2].isdigit():
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 取消共享服务器 ༻꧂\n\n"
                    "**群ID格式错误喵...**\n\n"
                    "格式：`取消共享服务器 <序号> [群ID]`",
                )
                return
            jid = int(parts[2])
            target_gid = self.registry.resolve_by_join_id(jid)
            if target_gid is None:
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 取消共享服务器 ༻꧂\n\n"
                    f"**❌ 找不到群ID为 {jid} 的群喵**",
                )
                return
            if target_gid not in shared:
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 取消共享服务器 ༻꧂\n\n"
                    "**❌ 该服务器并未共享给此群喵**",
                )
                return
            shared.remove(target_gid)
            detail = f"已取消对目标群（群ID `{jid}`）的共享喵！"
        else:
            shared[:] = []
            detail = "已取消对全部群的共享喵！"
        await self.zse_server._save()
        await self._reply_markdown(
            message,
            "## ꧁༺ 取消共享服务器 ༻꧂\n\n"
            f"✅ **{detail}**",
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
        for idx, rec in enumerate(servers, 1):
            cn = self._cn_seq(idx)
            name = rec.get("server_name") or "未认领"
            ver = rec.get("version") or ""
            ver_tag = f" 【{ver}】" if ver else ""
            shared_tag = "（共享）" if rec.get("rec_shared_by") else ""
            if rec.get("bound") and rec.get("online"):
                state = "🟢 在线"
            else:
                state = "🔴 离线"
            blocks.append(f"### ✵{cn}✵ **{name}**{ver_tag}{shared_tag}｜{state}")
            blocks.append(f"- »地址: `{rec['ip']}`")
            blocks.append(f"- »端口: `{rec['port']}`")
            adder = rec.get("added_by") or ""
            add_name = self.whitelist_store.find_by_openid(self._eff_gid(gid), adder)
            av = self._avatar_md(self._eff_gid(gid), add_name) if add_name else ""
            # QQ 渲染器在"文字紧贴图片"时会强制图片换行，头像放行首可内联（同广播卡写法）
            owner_line = f"> {av} 本服务器由`{add_name or adder[:8]}`添加" if av else f"> 本服务器由`{add_name or adder[:8]}`添加"
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
                    "> 可发 `绑定邮箱 <邮箱>` 按提示绑定，或指定玩家名：`查背包 <序号> <玩家名>`",
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
        cache_key = (seq, player)
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
        """全服喊话 <内容>：对所有在线服务器广播"""
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
        for rec in self.zse_server.visible_records(gid):
            try:
                if await self.zse_server.send_say(gid, rec.get("seq"), say):
                    ok_cnt += 1
                else:
                    offline.append(f"`{rec.get('seq')}`{rec.get('server_name') or ''}")
            except Exception as e:
                _log.exception("全服喊话发送异常: %s", e)
        if ok_cnt > 0:
            names = "、".join(
                f"`{r.get('seq')}`{r.get('server_name') or ''}"
                for r in self.zse_server.visible_records(gid)
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
            seqs = [r.get("seq") for r in recs]
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
                result = out[:1000]
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
        """按序号取服务器显示名（纯名，无括号）"""
        for rec in self.zse_server.list_servers(gid):
            if rec.get("seq") == seq:
                return rec.get("server_name") or ""
        return ""

    def _server_name_by_seq(self, gid: str, seq: int) -> str:
        """按序号取服务器显示名（用于卡片展示），返回 '（名称）' 或空"""
        for rec in self.zse_server.list_servers(gid):
            if rec.get("seq") == seq:
                name = rec.get("server_name") or ""
                return f"（{name}）" if name else ""
        return ""

    async def cmd_bind_email(self, message, text: str, gid):
        """绑定邮箱 <邮箱>：向该邮箱发送 4 位验证码（5 分钟有效，4 分钟限一次，单邮箱封顶 5 封）"""
        # 支持 "绑定邮箱：xxx@qq.com" 和 "绑定邮箱 xxx@qq.com"
        rest = text[len("绑定邮箱"):].lstrip("：: \t")
        email = rest.strip()
        try:
            user_openid = message.author.member_openid or ""
        except AttributeError:
            user_openid = ""
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单验证 ༻꧂\n\n"
                "**邮箱格式不对喵...**\n\n"
                "格式：`绑定邮箱 <您的邮箱>`\n"
                "例：`绑定邮箱 1011819146@qq.com`",
            )
            return
        ok, msg, _code = self.mail.request_code(user_openid, email, self._group_name(self._eff_gid(gid)), self.bot_name)
        if ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单验证 ༻꧂\n\n"
                f"✅ **验证码已发送到 `{email}`**\n\n"
                "请查收邮件，然后发送：\n"
                f"`添加白名单 <进服玩家名> <验证码>`\n\n"
                "> 验证码5分钟有效，若过期请重新申请喵",
            )
        else:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单验证 ༻꧂\n\n"
                f"**❌ {msg}**",
            )

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
                "请先发送 `绑定邮箱 <您的邮箱>` 获取验证码",
            )
            return

        # 验证通过：写入白名单
        self.whitelist_store.add(self._eff_gid(gid), player_name, email, bind_openid=user_openid)
        await self._reply_markdown(
            message,
            "## ꧁༺ 白名单绑定 ༻꧂\n\n"
            f"✅ **绑定成功喵！**\n\n"
            f"- 进服玩家名：`{player_name}`\n"
            f"- 绑定邮箱：`{email}`\n\n"
            "> 现在可以用这个名字进入服务器啦，首次进服会自动登记设备",
        )

    async def cmd_login(self, message, text: str, gid):
        """登录 [玩家名]：批准换设备进服（插件提示"在群里发送 /登录"）。
        进服被判 need_login 时，BOT 已记录该玩家待批准的新设备（记录在玩家绑定群）。
        支持跨群批准：待批准请求可在**本群或任何联合群**中被批准；
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

        # 只有一个待批准请求且没带玩家名时，自动识别
        if not player_name:
            pendings = {}
            for sg in scope_gids:
                pendings.update(self.pending_store.group_pending(sg))
            if not pendings:
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 白名单设备登录 ༻꧂\n\n"
                    "**当前没有待批准的设备登录请求喵**\n\n"
                    "> 格式：`登录 <进服玩家名>`\n"
                    "> 例：`登录 星梦`",
                )
                return
            if len(pendings) > 1:
                names = "、".join(f"`{n}`" for n in pendings)
                await self._reply_markdown(
                    message,
                    "## ꧁༺ 白名单设备登录 ༻꧂\n\n"
                    "**有多个待批准请求，请指定玩家名喵**\n\n"
                    f"待批准：{names}\n\n"
                    "> 格式：`登录 <进服玩家名>`",
                )
                return
            player_name = next(iter(pendings))

        p_gid, pending = _find_pending(player_name)
        if pending is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单设备登录 ༻꧂\n\n"
                f"**没有找到 `{player_name}` 的待批准请求喵**\n\n"
                "> 请先用该玩家名进服一次（提示未授权设备后），再来发送 `登录`",
            )
            return

        rec = self.whitelist_store.get_record(p_gid, player_name)
        if rec is None:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单设备登录 ༻꧂\n\n"
                f"**`{player_name}` 不在白名单中喵**\n\n"
                "> 请先通过 `添加白名单 <玩家名> <验证码>` 绑定",
            )
            return
        bind_openid = rec.get("bind_openid") or ""
        authorized = bool(bind_openid and bind_openid == user_openid)
        if not authorized and bind_openid:
            # 跨群桥接：批准者在当前群绑定的邮箱与记录邮箱一致 = 同一人
            my_emails = set()
            for nm, r in (self.whitelist_store._data.get(self._eff_gid(gid), {}) or {}).items():
                if r.get("bind_openid") == user_openid and r.get("email"):
                    my_emails.add(r["email"].lower())
            if (rec.get("email") or "").lower() in my_emails:
                authorized = True
        if bind_openid and not authorized:
            await self._reply_markdown(
                message,
                "## ꧁༺ 白名单设备登录 ༻꧂\n\n"
                f"**无权批准 `{player_name}` 的设备登录喵**\n\n"
                "> 只有绑定该白名单的 QQ 本人（或使用同一绑定邮箱的账号）才能批准换设备登录",
            )
            return
        if not bind_openid:
            # 旧记录没有绑定人：首次批准视为本人认领，写入绑定人防止冒认
            self.whitelist_store.claim_bind(p_gid, player_name, user_openid)

        # 批准：更新登记设备为新设备，玩家可重新进服（写入待批准记录所在群）
        self.whitelist_store.update_uuid(p_gid, player_name, pending["uuid"])
        self.pending_store.pop(p_gid, player_name)
        await self._reply_markdown(
            message,
            "## ꧁༺ 白名单设备登录 ༻꧂\n\n"
            f"✅ **已批准 `{player_name}` 的新设备登录喵！**\n\n"
            "> 请重新进入服务器，会自动放行",
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

    def _player_avatar_url(self, gid, player_name: str, size: int = 100) -> str:
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

    def _openid_avatar_md(self, openid: str, size: int = 100, px: int = 20) -> str:
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
                "> 群ID 请发送 `群信息` 查看",
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
        ok, msg, affected = self.registry.unbind(gid)
        if not ok:
            await self._reply_markdown(
                message,
                "## ꧁༺ 解除联合群 ༻꧂\n\n"
                f"**❌ {msg}**",
            )
            return
        # 解除联合时自动清除受影响子群在全部服务器上的共享引用
        for g in affected:
            self.zse_server.clear_shared(g)
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
                    content = "\n".join(
                        [
                            self.build_card_title("帮助"),
                            "发送关键词获取对应功能喵!",
                            "---",
                            "> 入群申请审核 / 退群通知 / 更多能力敬请期待",
                        ]
                    )
                # 按钮回调：优先用 event_id 当"事件被动回复"发送（免主动发言权限），失败降级普通发送；
                # 全程 try 兜底，保证下面的 _reply_interaction 一定执行，避免按钮一直转圈/第三方失败
                sent = False
                for kwargs in ({"event_id": getattr(interaction, "event_id", None)}, {}):
                    if not kwargs.get("event_id"):
                        continue
                    try:
                        await self.api.post_group_message(
                            group_openid=group_openid, msg_type=2,
                            markdown=MarkdownPayload(content=content), **kwargs
                        )
                        sent = True
                        break
                    except Exception as e:
                        _log.warning("按钮回复事件被动发送失败：%s，改普通发送", e)
                if not sent:
                    try:
                        await self.api.post_group_message(
                            group_openid=group_openid, msg_type=0, content=content
                        )
                    except Exception as e:
                        _log.warning("按钮回复普通发送失败: %s", e)
            try:
                await self._reply_interaction(interaction)  # 无论成败都结束按钮 loading
            except Exception as e:
                _log.warning("结束按钮交互失败: %s", e)
            return

        # 只处理群聊场景的审批按钮
        if not (group_openid and member_openid and op in ("approve", "decline")):
            _log.warning("回调数据缺少必要字段: %s", data)
            await self._reply_interaction(interaction)
            return

        if op == "approve":
            desc, reason, blacklist = "批准", None, False
        else:
            desc, reason, blacklist = "拒绝", "管理员拒绝该入群申请", False

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
            try:
                await self.api.post_group_message(
                    group_openid=group_openid,
                    msg_type=0,
                    content=f"✅ 已{desc}该用户入群申请",
                )
            except Exception as e:
                _log.warning("审批结果通知发送失败(可能未开主动消息)：%s", e)
        except Exception as e:  # 具体错误码见 docs/官方接口速查.md
            _log.exception("审批调用失败")
            try:
                await self.api.post_group_message(
                    group_openid=group_openid, msg_type=0, content=f"⚠️ 审批失败：{e}"
                )
            except Exception as e2:
                _log.warning("审批失败通知也发送失败: %s", e2)
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
                for group_name, group_openid in self.groups.items():
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