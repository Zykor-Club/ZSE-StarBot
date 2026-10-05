# -*- coding: utf-8 -*-
"""
starZSEbot 兼容服务端（机器人侧）：
  - GET /server/token/{code}           绑定码换 token + group_open_id（插件每 10 秒轮询）
  - WS  /server/ws/{gid}/tshock/       插件长连接（Bearer token 认证）

协议与 reference-repos/TShockPlugin/src/CaiBotLite 插件一致（内部命名空间已改 ZSEBot）：
  hello / heartbeat / player_list / progress / map_image / unbind_server
数据（bindings.json）按群隔离：{group_openid: [服务器记录]}
"""

import asyncio
import base64
import gzip
import json
import os
import secrets
import socket
import tempfile
import time
import uuid

from aiohttp import web

from whitelist_mail import PendingStore, WhitelistStore

_BIND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bindings.json")


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


def decode_map_png(compressed_b64: str) -> bytes:
    """插件回包 base64 = gzip(base64(PNG 字节))；逐层解压还原出 PNG 字节"""
    inner_b64 = gzip.decompress(base64.b64decode(compressed_b64)).decode("utf-8")
    return base64.b64decode(inner_b64)


def decode_archive_zip(compressed_b64: str) -> bytes:
    """插件存档回包 base64 = gzip(base64(zip 字节))；逐层解压还原出 zip 字节"""
    inner_b64 = gzip.decompress(base64.b64decode(compressed_b64)).decode("utf-8")
    return base64.b64decode(inner_b64)


_TOKEN_MAX_FAILS = 20   # /server/token 每 IP 每分钟允许的失败次数（绑定码 6 位，必须限流）


def make_ssl_context(cert_file: str, key_file: str):
    """用证书+私钥构造 TLS 上下文（供插件通道走 wss）。

    证书用 Let's Encrypt 正式证书时，插件侧默认校验即可通过、服主零配置。
    调用方负责确认两个文件存在；缺证书时不要调用本函数（保持明文 ws）。
    """
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


def decode_compressed_b64(compressed_b64: str) -> bytes:
    """插件通用文件回包 base64 = gzip(base64(原始字节))；还原出原始字节（世界文件 .wld / 小地图 .map）"""
    inner_b64 = gzip.decompress(base64.b64decode(compressed_b64)).decode("utf-8")
    return base64.b64decode(inner_b64)


class ZseServer:
    """starZSEbot 协议服务端，与 QQ 机器人 main.py 共用同一个事件循环"""

    def __init__(self, bind_file: str = _BIND_FILE, whitelist: WhitelistStore = None,
                 pending: PendingStore = None):
        self.bind_file = bind_file
        self._lock = asyncio.Lock()
        # {group_openid: [record]}
        # record: {seq, ip, port, code, token, server_name, whitelist, bound, online, heartbeat_at}
        self._data: dict = {}
        # token -> (group_openid, record)   WS 认证用
        self._by_token: dict = {}
        # token -> WebSocketResponse      当前在线连接
        self._ws_by_token: dict = {}
        # request_id -> asyncio.Future    在线查询挂起的应答
        self._pending: dict = {}
        # 每 IP 的 /server/token 失败时间戳（防绑定码爆破）
        self._token_fails: dict = {}
        # 白名单数据（进服判定用）
        self.whitelist = whitelist or WhitelistStore()
        # 待批准的设备登录请求（need_login 时记录，群里"登录"命令批准）
        self.pending = pending or PendingStore()
        # 群注册表（由 main.py 注入）：联合区数据归总群判定用
        self.registry = None
        # 进度提醒推送回调（由 main.py 注入）：插件首杀推送 → 播报到订阅群
        self.on_progress_notify = None
        # 设备登录请求推送回调（由 main.py 注入）：判定 need_login → 推送带按钮的请求卡
        self.on_need_login = None
        self._load()

    # ───────────────────────── 数据持久化 ─────────────────────────
    def _load(self):
        try:
            with open(self.bind_file, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._data = {}
        # 多群联合迁移：缺 owner_gid / version 的记录补齐（幂等，重复加载不会重复处理）
        for ogid, records in self._data.items():
            for rec in records:
                if not rec.get("owner_gid"):
                    rec["owner_gid"] = ogid
                rec.setdefault("version", "")
                rec["online"] = False  # 重启后一律先视为离线，等插件重连后再置在线
        self._rebuild_index()

    async def _save(self):
        async with self._lock:
            try:
                atomic_write_json(self.bind_file, self._data)
            except OSError as e:
                print(f"[starZSEbot-server] 保存 bindings.json 失败: {e}")

    async def _update_server_name(self, rec: dict, name: str):
        """把服务器名写回归属群的真实记录（共享来的记录只是内存拷贝），变化时落盘"""
        if not name:
            return
        entry = self._by_token.get(rec.get("token") or "")
        target = entry[1] if entry else rec
        if target.get("server_name") != name:
            target["server_name"] = name
            await self._save()

    def _rebuild_index(self):
        """扫描登记记录，重建 token 索引（启动/加载后调用）"""
        self._by_token = {}
        for gid, records in self._data.items():
            for rec in records:
                if rec.get("token"):
                    self._by_token[rec["token"]] = (gid, rec)

    # ───────────────────────── 群管理接口（供 main.py 调用） ─────────────────────────
    async def register(self, gid: str, ip: str, port: int, code: str, added_by: str = ""):
        """添加服务器：登记绑定码，等待插件认领。added_by 记录添加者 openid（删除归属校验用）。返回 (ok, msg)
        全局查重：同一绑定码或同一 ip:port 已在任意群登记过则拒绝（一台服务器全局只存一份）。"""
        gid = gid or "nogroup"
        # 全局查重：遍历所有群的记录
        for records in self._data.values():
            for rec in records:
                if rec["code"] == code:
                    return False, "该服务器已登记，无法重复添加"
                if rec["ip"] == ip and rec["port"] == port:
                    return False, "该服务器已登记，无法重复添加"
        records = self._data.setdefault(gid, [])
        records.append({
            "seq": len(records) + 1,
            "ip": ip, "port": int(port), "code": code,
            "token": "", "server_name": "", "whitelist": None,
            "bound": False, "online": False, "heartbeat_at": 0.0,
            "added_by": added_by or "",
            "owner_gid": gid,
            "version": "",
        })
        await self._save()
        return True, "登记成功，等待服务器插件认领（请确认插件已配置 ServerUrl 并启动）"

    async def unregister(self, gid: str, seq: int):
        """删除服务器：若在线先发解绑包，再移除登记。返回 (ok, msg)"""
        gid = gid or "nogroup"
        records = self._data.get(gid, [])
        target = next((r for r in records if r["seq"] == seq), None)
        if not target:
            return False, f"没有序号 {seq} 的服务器"
        # 防御：仅服务器所属群可删除
        if target.get("owner_gid") != gid:
            return False, "仅服务器所属群可删除该服务器"
        # 先通知插件解绑（若连接在线）
        token = target.get("token")
        ws = self._ws_by_token.get(token) if token else None
        if ws is not None and not ws.closed:
            try:
                await self._send_unbind(ws, f"管理员在群里删除了该服务器")
            except Exception as e:
                # 半开连接上 send_str 可能抛 OSError：不能因此中断删除流程（否则记录留盘且群里无回执）
                print(f"[starZSEbot-server] 解绑通知发送失败（继续删除）: {e}")
        records.remove(target)
        for i, r in enumerate(records, 1):
            r["seq"] = i
        if token:
            self._by_token.pop(token, None)
            self._ws_by_token.pop(token, None)
        await self._save()
        return True, f"已删除服务器 {target['ip']}:{target['port']}"

    def list_servers(self, gid: str):
        """服务器列表：返回本群拥有的服务器（owner_gid==gid 的记录）。[{seq, ip, port, bound, online, server_name, whitelist}]"""
        gid = gid or "nogroup"
        return [r for r in self._data.get(gid, []) if r.get("owner_gid") == gid]

    def all_records(self) -> list:
        """所有群的服务器记录（上/下线通知需要遍历全部）"""
        out = []
        for records in self._data.values():
            if isinstance(records, list):
                out.extend(r for r in records if isinstance(r, dict))
        return out

    def is_alive(self, rec: dict, stale_after: float = 180.0) -> bool:
        """是否真的在线：WS 标记在线且心跳未过期（插件每 60 秒心跳一次）

        只看 rec["online"] 不够：插件崩溃/断网时 WS 可能还没被判定关闭，
        心跳停止超过 stale_after 即视为掉线，避免「假在线」。
        """
        if not rec or not rec.get("online"):
            return False
        try:
            hb = float(rec.get("heartbeat_at") or 0)
        except (TypeError, ValueError):
            hb = 0.0
        return bool(hb) and (time.time() - hb) < stale_after

    def visible_records(self, gid: str):
        """本群可见的服务器：本群拥有的 + 同一联合区内其它群的全部服务器（网状可见，无需共享授权）。
        跨群记录附加临时标记 rec_shared_by=归属群 openid（仅内存，不落盘，用于显示"来自群id：x"）。"""
        gid = gid or "nogroup"
        recs = list(self.list_servers(gid))
        if self.registry is None:
            return recs
        others = [g for g in (self.registry.zone_gids(gid) or set()) if g and g != gid]
        # 按群ID排序，保证展示序号稳定
        others.sort(key=lambda g: (self.registry.join_id_of(g) or 10 ** 9, g))
        for ogid in others:
            for r in self.list_servers(ogid):
                copy = dict(r)          # 拷贝，不污染磁盘数据
                copy["rec_shared_by"] = ogid
                recs.append(copy)
        return recs

    def record_by_seq(self, gid: str, seq: int):
        """按“展示序号”解析记录：序号 = 服务器列表/在线卡里显示的位置号（自有在前、其它群的在后）。
        找不到返回 None。"""
        if not isinstance(seq, int):
            return None
        recs = self.visible_records(gid)
        return recs[seq - 1] if 1 <= seq <= len(recs) else None

    async def query_online(self, gid: str, timeout: float = 6.0):
        """在线：并行对本群所有在线插件发 player_list + progress 请求，汇总应答。

        返回 [(rec, status, info)]，info 含 server_name / current_online / max_online /
        player_list / world_icon / process 等。
        status: "ok" | "timeout" | "offline"
        """
        recs = self.visible_records(gid)
        return await asyncio.gather(*(self._query_one_online(r, timeout) for r in recs))

    async def _query_one_online(self, rec: dict, timeout: float) -> tuple:
        """单台服务器的在线查询（并行子任务）"""
        token = rec.get("token")
        ws = self._ws_by_token.get(token) if token else None
        if ws is None or ws.closed:
            return rec, "offline", None
        loop = asyncio.get_running_loop()
        pl_id, pr_id = str(uuid.uuid4()), str(uuid.uuid4())
        pl_fut, pr_fut = loop.create_future(), loop.create_future()
        self._pending[pl_id] = pl_fut
        self._pending[pr_id] = pr_fut
        try:
            # 每个请求之间也要并行 send，避免串行写阻塞
            await asyncio.gather(self._send_player_list(ws, pl_id), self._send_progress(ws, pr_id))
            # 并行等待两个应答
            pl_payload, pr_payload = await asyncio.gather(
                asyncio.wait_for(pl_fut, timeout=timeout),
                asyncio.wait_for(pr_fut, timeout=timeout),
            )
            name = pl_payload.get("server_name", rec.get("server_name", ""))
            rec["server_name"] = name
            await self._update_server_name(rec, name)
            info = dict(pl_payload)
            info["enable_whitelist"] = rec.get("whitelist")
            info.update(pr_payload or {})
            return rec, "ok", info
        except (asyncio.TimeoutError, OSError, RuntimeError):
            # 连接中断（ws 关闭中 / 写失败）也按超时处理，避免单台异常拖垮整个"在线"指令
            return rec, "timeout", None
        finally:
            self._pending.pop(pl_id, None)
            self._pending.pop(pr_id, None)

    async def ping(self, host: str, port: int, timeout: float = 5.0):
        """TCP 连接计时测延迟。返回 (ok, ms)"""
        loop = asyncio.get_running_loop()
        start = time.perf_counter()
        try:
            await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
                timeout=timeout,
            )
            # getaddrinfo 只解析域名，再试真实连接
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            ms = (time.perf_counter() - start) * 1000
            return True, round(ms)
        except (asyncio.TimeoutError, OSError):
            return False, None

    async def fetch_maps(self, gid: str, seq: int = None, timeout: float = 120.0):
        """向本群在线插件并行请求世界大地图（可指定服务器序号 seq）。

        返回 [(rec, status, info)]，info 含插件回包 payload（base64=压缩地图）。
        status: "ok" | "timeout" | "offline"
        """
        if seq is None:
            recs = self.visible_records(gid)
        else:
            rec = self.record_by_seq(gid, seq)
            recs = [rec] if rec is not None else []
        return await asyncio.gather(*(self._query_one_map(r, timeout) for r in recs))

    async def _query_one_map(self, rec: dict, timeout: float) -> tuple:
        """单台服务器的地图请求（并行子任务）"""
        token = rec.get("token")
        ws = self._ws_by_token.get(token) if token else None
        if ws is None or ws.closed:
            return rec, "offline", None
        loop = asyncio.get_running_loop()
        req_id = str(uuid.uuid4())
        fut = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._send_map_image(ws, req_id)
            payload = await asyncio.wait_for(fut, timeout=timeout)
            name = payload.get("server_name") or rec.get("server_name", "")
            rec["server_name"] = name
            await self._update_server_name(rec, name)
            return rec, "ok", payload
        except (asyncio.TimeoutError, OSError, RuntimeError):
            # 连接中断同样按超时处理，避免单台异常拖垮整条地图指令
            return rec, "timeout", None
        finally:
            self._pending.pop(req_id, None)

    async def fetch_lookbag(self, gid: str, seq: int, player_name: str, timeout: float = 15.0):
        """向指定服务器序号请求玩家背包数据（look_bag 包）。

        返回 (rec, status, payload)：status = "ok" | "timeout" | "offline" | "notfound"
        """
        rec = self.record_by_seq(gid, seq)
        if rec is None:
            return None, "notfound", None
        return await self._query_one_lookbag(rec, player_name, timeout)

    async def _query_one_lookbag(self, rec: dict, player_name: str, timeout: float) -> tuple:
        """单台服务器的背包查询"""
        token = rec.get("token")
        ws = self._ws_by_token.get(token) if token else None
        if ws is None or ws.closed:
            return rec, "offline", None
        loop = asyncio.get_running_loop()
        req_id = str(uuid.uuid4())
        fut = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._send_lookbag(ws, req_id, player_name)
            payload = await asyncio.wait_for(fut, timeout=timeout)
            return rec, "ok", payload
        except (asyncio.TimeoutError, OSError, RuntimeError):
            # 连接中断同样按超时处理，避免单台异常拖垮整条查背包指令
            return rec, "timeout", None
        finally:
            self._pending.pop(req_id, None)

    def connected_count(self, gid: str) -> int:
        """当前在线连接数（用于服务器列表里的在线状态）"""
        return sum(1 for r in self.list_servers(gid) if r.get("online"))

    # ───────────────────────── aiohttp 路由 ─────────────────────────
    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/server/token/{code}", self._handle_token)
        app.router.add_get("/server/ws/{gid}/tshock/", self._handle_ws)
        return app

    async def _handle_token(self, request: web.Request):
        """插件轮询：GET /server/token/{code} -> {token, group_open_id}

        安全约束（2026-10-04 加固）：
          · 绑定码只有 6 位，必须限流防爆破 → 每 IP 每分钟最多 _TOKEN_MAX_FAILS 次「未登记」尝试，超限 429；
          · 绑定码在**首次成功建立 WS 连接时立即作废**（见 _handle_ws），因此它不再是「永久取 token 的凭证」。
        """
        code = request.match_info["code"]
        peer = request.transport.get_extra_info("peername") if request.transport else None
        ip = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip() \
            or (peer[0] if peer else "") or "?"
        now = time.time()
        hits = [t for t in self._token_fails.get(ip, []) if now - t < 60]
        self._token_fails[ip] = hits
        if len(hits) >= _TOKEN_MAX_FAILS:
            return web.json_response({"error": "尝试过于频繁，请稍后再试"}, status=429)
        gid, rec = self._find_by_code(code)
        if rec is None:
            hits.append(now)
            return web.json_response({"error": "绑定码未登记或已使用"}, status=404)
        if not rec.get("token"):
            rec["token"] = secrets.token_urlsafe(32)
            self._by_token[rec["token"]] = (gid, rec)
            await self._save()
        return web.json_response({"token": rec["token"], "group_open_id": gid})

    async def _handle_ws(self, request: web.Request):
        """插件长连接：WS /server/ws/{gid}/tshock/（Header authorization: Bearer {token}）"""
        gid = request.match_info["gid"]
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        entry = self._by_token.get(token)
        if entry is None or entry[0] != gid:
            # 认证失败：先完成握手再以 4003 关闭（插件识别 4003 触发重新绑定）
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4003, message="认证失败，请重新绑定")
            return ws

        # 同 token 只允许一条连接：新连接到来先关掉旧连接，避免「顶掉真插件」后
        # 机器人把白名单回包发给攻击者、真插件收不到结果而被超时踢出
        old = self._ws_by_token.get(token)
        if old is not None and not old.closed and old is not ws:
            try:
                await old.close(code=4004, message="同一服务器的另一条连接已建立")
            except Exception as e:
                print(f"[starZSEbot-server] 关闭旧连接失败: {e}")
        # 大地图回包可能远超默认 4MB，放宽到 128MB
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=128 * 1024 * 1024)
        await ws.prepare(request)
        self._ws_by_token[token] = ws
        rec = entry[1]
        # 绑定完成：绑定码标记为「已使用」（一次性，之后再也换不出 token），
        # 但**保留 code 原值**——它是投票/进度提醒/状态通知等业务数据里的服务器唯一标识，
        # 清空会让这些数据全部落到空串上（历史回归，勿再改成清空）。
        if rec.get("code") and not rec.get("code_used"):
            print(f"[starZSEbot-server] 服务器已绑定，绑定码 {rec['code']} 标记为已使用")
            rec["code_used"] = True
            await self._save()
        rec["online"] = True
        rec["heartbeat_at"] = time.time()
        print(f"[starZSEbot-server] 插件已连接: {rec['ip']}:{rec['port']} (群 {gid[:8]}...)")

        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    pkg = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self._handle_package(gid, rec, token, pkg, ws)
        finally:
            # 仅当映射仍是本条连接时才清理：旧连接的收尾不能误删重连后的新连接
            if self._ws_by_token.get(token) is ws:
                self._ws_by_token.pop(token, None)
                rec["online"] = False
            print(f"[starZSEbot-server] 插件断开: {rec['ip']}:{rec['port']}")

        return ws

    # ───────────────────────── 包处理 ─────────────────────────
    async def _handle_package(self, gid: str, rec: dict, token: str, pkg: dict,
                              ws: web.WebSocketResponse = None):
        ptype = pkg.get("type", "")
        payload = pkg.get("payload") or {}
        if ptype == "hello":
            rec["server_name"] = payload.get("server_name", rec.get("server_name", ""))
            rec["whitelist"] = payload.get("enable_whitelist", rec.get("whitelist"))
            # 版本号：优先游戏版本（v1.4.5.8），其次 TShock 版本；插件 hello 包自带
            rec["version"] = str(
                payload.get("game_version") or payload.get("server_core_version")
                or rec.get("version", "") or "")
            rec["bound"] = True
            asyncio.create_task(self._save())
        elif ptype == "heartbeat":
            rec["heartbeat_at"] = time.time()
            rec["online"] = True
        elif ptype == "whitelist":
            # 玩家进服：插件发来玩家信息，BOT 判定白名单结果并回包
            # 带上请求侧 request_id 一并回声：插件据此确认"这条裁定对应本次握手"，伪造/重放的回包可被它丢弃
            await self._handle_whitelist(rec, payload, ws, pkg.get("request_id"))
        elif ptype == "progress_notify":
            # 插件主动推送：某 boss 本世界首次被击杀 → 交由 main.py 播报到订阅群
            cb = self.on_progress_notify
            if cb is not None:
                async def _fire_notify(cb=cb, rec=dict(rec), payload=dict(payload)):
                    try:
                        await cb(rec, payload)
                    except Exception as e:
                        print(f"[starZSEbot-server] 进度播报处理异常: {e}")
                asyncio.create_task(_fire_notify())
        elif pkg.get("request_id"):
            # 插件对请求的应答（应答包带同样的 request_id）：按 id 匹配 pending future。
            # 不再维护「应答类型白名单」——白名单曾漏掉 rank_data / plugin_list / world_file /
            # map_file，插件回包被静默丢弃，请求方只能等到超时；按 request_id 匹配后新增类型自动覆盖。
            req_id = pkg.get("request_id")
            fut = self._pending.get(req_id)
            if fut and not fut.done():
                if ptype in ("player_list", "map_image"):
                    rec["server_name"] = payload.get("server_name", rec.get("server_name", ""))
                fut.set_result(payload)

    @staticmethod
    async def _send_unbind(ws: web.WebSocketResponse, reason: str):
        await ws.send_str(json.dumps({
            "version": "0.1.0",
            "direction": "to_server",
            "type": "unbind_server",
            "is_request": False,
            "request_id": None,
            "payload": {"reason": reason},
        }))

    @staticmethod
    async def _send_player_list(ws: web.WebSocketResponse, req_id: str):
        await ws.send_str(json.dumps({
            "version": "0.1.0",
            "direction": "to_server",
            "type": "player_list",
            "is_request": True,
            "request_id": req_id,
            "payload": {},
        }))

    @staticmethod
    async def _send_progress(ws: web.WebSocketResponse, req_id: str):
        await ws.send_str(json.dumps({
            "version": "0.1.0",
            "direction": "to_server",
            "type": "progress",
            "is_request": True,
            "request_id": req_id,
            "payload": {},
        }))

    @staticmethod
    async def _send_map_image(ws: web.WebSocketResponse, req_id: str):
        await ws.send_str(json.dumps({
            "version": "0.1.0",
            "direction": "to_server",
            "type": "map_image",
            "is_request": True,
            "request_id": req_id,
            "payload": {},
        }))

    @staticmethod
    async def _send_lookbag(ws: web.WebSocketResponse, req_id: str, player_name: str):
        await ws.send_str(json.dumps({
            "version": "0.1.0",
            "direction": "to_server",
            "type": "look_bag",
            "is_request": True,
            "request_id": req_id,
            "payload": {"player_name": player_name},
        }))

    # ───────────────────────── 白名单进服判定 ─────────────────────────
    async def _handle_whitelist(self, rec: dict, payload: dict, ws: web.WebSocketResponse,
                               req_id: str = None):
        """玩家进服白名单判定。插件发 {player_name, player_ip, player_uuid, player_platform}，BOT 回 whitelist_result。
        联合区数据归总群：判定只用服务器归属群所在的【总群】白名单（effective_gid）。
        need_login 待批准记录写入总群，玩家在总群/任一联合群发"登录"可批准（一步批准）。"""
        try:
            player_name = (payload.get("player_name") or "").strip()
            player_uuid = (payload.get("player_uuid") or "").strip()
            player_ip = (payload.get("player_ip") or "").strip()
            player_platform = (payload.get("player_platform") or "").strip()
            if not player_name:
                await self._send_whitelist(ws, "", "unknown", req_id)
                return
            owner = rec.get("owner_gid") or ""
            eff_gid = self.registry.effective_gid(owner) if self.registry else owner
            city = self.whitelist.resolve_city(player_ip)  # 解析失败返回空串，城市校验自动跳过
            result, _reg, reason = self.whitelist.check(eff_gid, player_name, player_uuid,
                                                        player_platform, city)
            if result == "need_login":
                # 换设备进服：待批准记录写入总群，玩家在总群/任一联合群发"登录"可批准
                self.pending.record(eff_gid, player_name, player_uuid, player_ip,
                                    platform=player_platform, city=city, reason=reason)
                # 推送带「登录/拒绝」按钮的请求卡（群未开主动消息权限时 main.py 侧静默跳过）
                cb = self.on_need_login
                if cb is not None:
                    info = {
                        "gid": eff_gid, "player_name": player_name, "uuid": player_uuid,
                        "ip": player_ip, "platform": player_platform, "city": city,
                        "reason": reason, "ts": int(time.time()),
                    }

                    async def _fire_login_cb(cb=cb, rec=dict(rec), info=info):
                        try:
                            await cb(rec, info)
                        except Exception as e:
                            print(f"[starZSEbot-server] 登录请求推送处理异常: {e}")

                    asyncio.create_task(_fire_login_cb())
            print(f"[starZSEbot-server] 白名单判定: {player_name} -> {result}({reason}) 平台 {player_platform or '-'} 城市 {city or '-'} (归属群 {owner[:8]}.../总群 {eff_gid[:8]}...)")
            await self._send_whitelist(ws, player_name, result, req_id)
        except Exception as e:
            print(f"[starZSEbot-server] 白名单判定异常: {e}")
            await self._send_whitelist(ws, payload.get("player_name") or "", "unknown", req_id)

    @staticmethod
    async def _send_whitelist(ws: web.WebSocketResponse, player_name: str, result: str,
                              req_id: str = None):
        await ws.send_str(json.dumps({
            "version": "0.1.0",
            "direction": "to_server",
            "type": "whitelist",
            "is_request": False,
            "request_id": req_id,   # 回声插件请求的 id（None 时兼容旧插件）
            "payload": {
                "player_name": player_name,
                "whitelist_result": result,
            },
        }))

    # ───────────────────────── 远程广播 / 远程执行 ─────────────────────────
    async def send_say(self, gid: str, seq: int, content: str) -> bool:
        """向指定服务器广播（= 服务器内 /say）。返回是否已发出（离线/不存在 False）"""
        rec = self.record_by_seq(gid, seq)
        if rec is None:
            return False
        token = rec.get("token")
        ws = self._ws_by_token.get(token) if token else None
        if ws is None or ws.closed:
            return False
        await ws.send_str(json.dumps({
            "version": "0.1.0",
            "direction": "to_server",
            "type": "say",
            "is_request": False,
            "request_id": None,
            "payload": {"content": content},
        }))
        return True

    async def exec_command(self, gid: str, seq: int, command: str,
                           user_openid: str, group_openid: str,
                           timeout: float = 15.0) -> tuple[bool, str]:
        """远程执行：向指定服务器发 call_command，等待 output 回包。返回 (ok, 结果文本)"""
        rec = self.record_by_seq(gid, seq)
        if rec is None:
            return False, "找不到该服务器"
        token = rec.get("token")
        ws = self._ws_by_token.get(token) if token else None
        if ws is None or ws.closed:
            return False, "服务器离线"
        loop = asyncio.get_running_loop()
        req_id = str(uuid.uuid4())
        fut = loop.create_future()
        self._pending[req_id] = fut
        try:
            await ws.send_str(json.dumps({
                "version": "0.1.0",
                "direction": "to_server",
                "type": "call_command",
                "is_request": True,
                "request_id": req_id,
                "payload": {
                    "command": command,
                    "user_open_id": user_openid or "",
                    "group_open_id": group_openid or "",
                },
            }))
            payload = await asyncio.wait_for(fut, timeout=timeout)
            output = payload.get("output") or ""
            if isinstance(output, list):  # 插件回包为逐行输出列表，拼成整段文本
                output = "\n".join(str(line) for line in output)
            return True, output.strip() or "（无输出）"
        except asyncio.TimeoutError:
            return False, "执行超时（服务器无回包）"
        finally:
            self._pending.pop(req_id, None)

    # ───────────────────────── AutoResetPlus / 存档导出 ─────────────────────────
    def _rec_by_code(self, server_code):
        """按服务器标识（绑定码 code，全局唯一）解析登记记录；找不到返回 None"""
        if not server_code:
            return None
        for records in self._data.values():
            for rec in records:
                if rec.get("code") == server_code:
                    return rec
        return None

    async def _request_plugin(self, server_code: str, ptype: str, payload: dict,
                              timeout: float):
        """向指定服务器（按 code 标识）发一个 is_request 包并等待回包。

        返回 (ok, data)：ok=True 时 data 为回包 payload(dict)；失败时 data 为错误文案(str)，
        覆盖「服务器不存在」「离线」「超时」三种情况。
        """
        rec = self._rec_by_code(server_code)
        if rec is None:
            return False, "找不到该服务器"
        token = rec.get("token")
        ws = self._ws_by_token.get(token) if token else None
        if ws is None or ws.closed:
            return False, "服务器离线"
        loop = asyncio.get_running_loop()
        req_id = str(uuid.uuid4())
        fut = loop.create_future()
        self._pending[req_id] = fut
        try:
            await ws.send_str(json.dumps({
                "version": "0.1.0",
                "direction": "to_server",
                "type": ptype,
                "is_request": True,
                "request_id": req_id,
                "payload": payload or {},
            }))
            result = await asyncio.wait_for(fut, timeout=timeout)
            return True, result
        except asyncio.TimeoutError:
            return False, "请求超时（服务器无回包）"
        finally:
            self._pending.pop(req_id, None)

    async def request_auto_reset(self, server_code: str, action: str, seed: str = None,
                                 timeout: float = 15.0):
        """AutoResetPlus 桥接请求：action ∈ get_config/set_seed/do_reset。返回 (ok, data)"""
        payload = {"action": action}
        if seed is not None:
            payload["seed"] = seed
        return await self._request_plugin(server_code, "auto_reset", payload, timeout)

    async def request_archive_export(self, server_code: str, timeout: float = 300.0,
                                     action: str = ""):
        """存档导出请求（打包较慢，默认 300 秒超时）。返回 (ok, data)：data 含 name/size/base64

        action="backup" → 插件只把 zip 落到服务器备份目录、不回传 base64（省编码与流量）；
        缺省 → 导出并把 zip 一起回传（重置流程要把存档发到群里）。
        插件返回的 size 是 zip 字节数（备份指令用来显示大小）。
        """
        payload = {"action": action} if action else {}
        return await self._request_plugin(server_code, "archive_export", payload, timeout)

    async def request_progress(self, server_code: str, timeout: float = 15.0):
        """进度查询请求（boss 击杀情况）。返回 (ok, data)"""
        return await self._request_plugin(server_code, "progress", {}, timeout)

    # ───────────────────────── 插件列表 / 排行 / 世界与地图文件 / 自踢 ─────────────────────────
    async def request_plugin_list(self, server_code: str, timeout: float = 15.0):
        """插件列表请求。返回 (ok, data)：data = {"is_mod": bool, "plugins": [{Name,Author,Description,Version}]}"""
        return await self._request_plugin(server_code, "plugin_list", {}, timeout)

    async def request_rank(self, server_code: str, rank_type: str = "", arg: str = "",
                           timeout: float = 20.0):
        """排行榜请求。返回 (ok, data)：data 含 rank_type_support / need_arg / arg_support /
        message / support_args / support_rank_types / rank（{"title","rank_lines":{名:值}}）"""
        return await self._request_plugin(
            server_code, "rank_data",
            {"rank_type": rank_type or "", "arg": arg or ""}, timeout)

    async def request_world_file(self, server_code: str, timeout: float = 180.0):
        """世界文件（.wld）请求：包体较大，超时放宽。返回 (ok, data)：data = {"name","base64"}"""
        return await self._request_plugin(server_code, "world_file", {}, timeout)

    async def request_map_file(self, server_code: str, timeout: float = 180.0):
        """小地图文件（.map，需 GenerateMap 插件）。返回 (ok, data)：data = {"name","base64"}"""
        return await self._request_plugin(server_code, "map_file", {}, timeout)

    async def send_self_kick(self, gid: str, player_name: str) -> int:
        """向本群可见的在线服务器广播 self_kick（插件不回包）。返回成功发出的服务器数。

        插件侧只在名字命中在线玩家时踢出（成功则 Kick(..., saveSSI: true)）。
        """
        sent = 0
        for rec in self.visible_records(gid):
            token = rec.get("token")
            ws = self._ws_by_token.get(token) if token else None
            if ws is None or ws.closed:
                continue
            try:
                await ws.send_str(json.dumps({
                    "version": "0.1.0",
                    "direction": "to_server",
                    "type": "self_kick",
                    "is_request": False,
                    "request_id": None,
                    "payload": {"name": player_name},
                }))
                sent += 1
            except Exception as e:
                print(f"[zse_server] 自踢包发送失败: {e}")
        return sent

    # ───────────────────────── 辅助 ─────────────────────────
    def _find_by_code(self, code: str):
        if not code:
            return None, None
        for gid, records in self._data.items():
            for rec in records:
                # 已使用的绑定码不再匹配：它是一次性凭证，用过了就不能再换 token
                if rec.get("code") == code and not rec.get("code_used"):
                    return gid, rec
        return None, None

    async def unregister_all(self, gid: str) -> int:
        """清空某群名下**全部**服务器登记（机器人被重新拉入群时清数据用）。返回清理条数"""
        gid = gid or "nogroup"
        records = self._data.get(gid, []) or []
        targets = [r for r in records if r.get("owner_gid") == gid]
        for rec in targets:
            token = rec.get("token")
            ws = self._ws_by_token.get(token) if token else None
            if ws is not None and not ws.closed:
                try:
                    await self._send_unbind(ws, "机器人被重新拉入群，该群的服务器绑定已清空")
                except Exception as e:
                    print(f"[starZSEbot-server] 解绑通知发送失败: {e}")
            if token:
                self._by_token.pop(token, None)
                self._ws_by_token.pop(token, None)
        if targets:
            self._data[gid] = [r for r in records if r.get("owner_gid") != gid]
            await self._save()
        return len(targets)