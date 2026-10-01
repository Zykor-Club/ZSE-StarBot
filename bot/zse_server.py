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
        # 白名单数据（进服判定用）
        self.whitelist = whitelist or WhitelistStore()
        # 待批准的设备登录请求（need_login 时记录，群里"登录"命令批准）
        self.pending = pending or PendingStore()
        # 群注册表（由 main.py 注入）：联合区数据归总群判定用
        self.registry = None
        self._load()

    # ───────────────────────── 数据持久化 ─────────────────────────
    def _load(self):
        try:
            with open(self.bind_file, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._data = {}
        # 多群联合迁移：缺 owner_gid / shared_gids / version 的记录补齐（幂等，重复加载不会重复处理）
        for ogid, records in self._data.items():
            for rec in records:
                if not rec.get("owner_gid"):
                    rec["owner_gid"] = ogid
                rec.setdefault("shared_gids", [])
                rec.setdefault("version", "")
        self._rebuild_index()

    async def _save(self):
        async with self._lock:
            try:
                atomic_write_json(self.bind_file, self._data)
            except OSError as e:
                print(f"[starZSEbot-server] 保存 bindings.json 失败: {e}")

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
            "shared_gids": [],
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
            await self._send_unbind(ws, f"管理员在群里删除了该服务器")
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

    def visible_records(self, gid: str):
        """本群可见的服务器：本群拥有的 + 其他群共享给本群的。
        共享来的记录附加临时标记 rec_shared_by=owner_gid（仅内存，不落盘）。"""
        gid = gid or "nogroup"
        recs = list(self.list_servers(gid))
        for ogid, rs in self._data.items():
            if ogid == gid:
                continue
            for r in rs:
                if gid in (r.get("shared_gids") or []):
                    copy = dict(r)          # 拷贝，不污染磁盘数据
                    copy["rec_shared_by"] = ogid
                    recs.append(copy)
        return recs

    def clear_shared(self, gid: str):
        """解除联合：遍历全部群的记录，把 gid 从每台服务器的 shared_gids 中移除并保存"""
        gid = gid or "nogroup"
        changed = False
        for records in self._data.values():
            for rec in records:
                sg = rec.get("shared_gids")
                if sg and gid in sg:
                    rec["shared_gids"] = [x for x in sg if x != gid]
                    changed = True
        if changed:
            asyncio.ensure_future(self._save())

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
            rec["server_name"] = pl_payload.get("server_name", rec.get("server_name", ""))
            info = dict(pl_payload)
            info["enable_whitelist"] = rec.get("whitelist")
            info.update(pr_payload or {})
            return rec, "ok", info
        except asyncio.TimeoutError:
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
        recs = [r for r in self.visible_records(gid) if seq is None or r.get("seq") == seq]
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
            rec["server_name"] = payload.get("server_name") or rec.get("server_name", "")
            return rec, "ok", payload
        except asyncio.TimeoutError:
            return rec, "timeout", None
        finally:
            self._pending.pop(req_id, None)

    async def fetch_lookbag(self, gid: str, seq: int, player_name: str, timeout: float = 15.0):
        """向指定服务器序号请求玩家背包数据（look_bag 包）。

        返回 (rec, status, payload)：status = "ok" | "timeout" | "offline" | "notfound"
        """
        recs = [r for r in self.visible_records(gid) if r.get("seq") == seq]
        if not recs:
            return None, "notfound", None
        return await self._query_one_lookbag(recs[0], player_name, timeout)

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
        except asyncio.TimeoutError:
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
        """插件轮询：GET /server/token/{code} -> {token, group_open_id}"""
        code = request.match_info["code"]
        gid, rec = self._find_by_code(code)
        if rec is None:
            return web.json_response({"error": "绑定码未登记"}, status=404)
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

        # 大地图回包可能远超默认 4MB，放宽到 128MB
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=128 * 1024 * 1024)
        await ws.prepare(request)
        self._ws_by_token[token] = ws
        rec = entry[1]
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
            await self._handle_whitelist(rec, payload, ws)
        elif ptype in ("player_list", "progress", "map_image", "call_command", "look_bag"):
            # 插件对在线/地图/背包等请求的应答（应答包带同样的 request_id 与 is_request）
            req_id = pkg.get("request_id")
            fut = self._pending.get(req_id) if req_id else None
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
    async def _handle_whitelist(self, rec: dict, payload: dict, ws: web.WebSocketResponse):
        """玩家进服白名单判定。插件发 {player_name, player_ip, player_uuid}，BOT 回 whitelist_result。
        联合区数据归总群：判定只用服务器归属群所在的【总群】白名单（effective_gid）。
        need_login 待批准记录写入总群，玩家在总群/任一联合群发"登录 玩家名"可批准。"""
        try:
            player_name = (payload.get("player_name") or "").strip()
            player_uuid = (payload.get("player_uuid") or "").strip()
            if not player_name:
                await self._send_whitelist(ws, "", "unknown")
                return
            owner = rec.get("owner_gid") or ""
            eff_gid = self.registry.effective_gid(owner) if self.registry else owner
            result, _reg = self.whitelist.check(eff_gid, player_name, player_uuid)
            if result == "need_login":
                # 换设备进服：待批准记录写入总群，玩家在总群/任一联合群发"登录 玩家名"可批准
                self.pending.record(eff_gid, player_name, player_uuid, payload.get("player_ip") or "")
            print(f"[starZSEbot-server] 白名单判定: {player_name} -> {result} (归属群 {owner[:8]}.../总群 {eff_gid[:8]}...)")
            await self._send_whitelist(ws, player_name, result)
        except Exception as e:
            print(f"[starZSEbot-server] 白名单判定异常: {e}")
            await self._send_whitelist(ws, payload.get("player_name") or "", "unknown")

    @staticmethod
    async def _send_whitelist(ws: web.WebSocketResponse, player_name: str, result: str):
        await ws.send_str(json.dumps({
            "version": "0.1.0",
            "direction": "to_server",
            "type": "whitelist",
            "is_request": False,
            "request_id": None,
            "payload": {
                "player_name": player_name,
                "whitelist_result": result,
            },
        }))

    # ───────────────────────── 远程广播 / 远程执行 ─────────────────────────
    def _find_by_seq(self, gid: str, seq: int):
        for rec in self.visible_records(gid):
            if rec.get("seq") == seq:
                return rec
        return None

    async def send_say(self, gid: str, seq: int, content: str) -> bool:
        """向指定服务器广播（= 服务器内 /say）。返回是否已发出（离线/不存在 False）"""
        rec = self._find_by_seq(gid, seq)
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
        rec = self._find_by_seq(gid, seq)
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
            return True, output.strip() or "（无输出）"
        except asyncio.TimeoutError:
            return False, "执行超时（服务器无回包）"
        finally:
            self._pending.pop(req_id, None)

    # ───────────────────────── 辅助 ─────────────────────────
    def _find_by_code(self, code: str):
        for gid, records in self._data.items():
            for rec in records:
                if rec["code"] == code:
                    return gid, rec
        return None, None