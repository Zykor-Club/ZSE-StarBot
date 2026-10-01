# -*- coding: utf-8 -*-
"""
白名单绑定：邮箱验证码 与 SMTP 发信模块

规则（按用户需求）：
  - 验证码：4 位纯数字，有效期 5 分钟（COOLDOWN 内不可重复获取）
  - 频率：同一收件邮箱 4 分钟内最多申请发送 1 封（以发送成功为准，失败不计）
  - 封顶：同一收件邮箱累计成功发送 5 封后拒绝继续发送；绑定成功后该计数清零
数据文件：
  - verify.json   验证码与计数（临时）
  - whitelist.json 白名单记录（按群隔离）
"""

import json
import os
import random
import re
import smtplib
import tempfile
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

_BASE = os.path.dirname(os.path.abspath(__file__))
VERIFY_FILE = os.path.join(_BASE, "verify.json")
WHITELIST_FILE = os.path.join(_BASE, "whitelist.json")
PENDING_FILE = os.path.join(_BASE, "pending.json")

# 默认规则（可被 config 覆盖）
CODE_TTL = 300          # 验证码有效期 5 分钟
RETRY_COOLDOWN = 240    # 同一邮箱 4 分钟限发 1 封
MAX_MAIL_PER_EMAIL = 5  # 同一邮箱累计成功发送封顶


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


# 白名单玩家名校验（对齐 Terraria 角色名习惯，防止特殊字符注入踢出文案/冒用）
_NAME_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9 ]+")
MAX_NAME_LENGTH = 15


def check_name_ok(name: str) -> bool:
    """合法：长度 1~15，仅汉字/字母/数字/空格（整体匹配，天然拒绝换行、引号等特殊符号）"""
    name = (name or "").strip()
    return bool(name) and len(name) <= MAX_NAME_LENGTH and _NAME_RE.fullmatch(name) is not None


class MailSender:
    def __init__(self, smtp_host: str = "smtp.qq.com", smtp_port: int = 465,
                 username: str = "", auth_code: str = "", from_name: str = "ZSE联合体"):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.auth_code = auth_code
        self.from_name = from_name

    # ────────────── 发信 ──────────────
    def send_code(self, email: str, code: str, group_name: str, bot_name: str) -> tuple[bool, str]:
        """发送验证码邮件。返回 (ok, msg)。发送成功才返回 True（计数在其上层处理）。"""
        subject = "ZSE联合体-白名单验证"
        body_lines = [
            f"群名称：{group_name}",
            f"验证码：{code}",
            "",
            f"请回到群中向 {bot_name} 发送：添加白名单 进服玩家名字 您当前收到的验证码 进行白名单绑定。",
            "本验证码5分钟有效，若过期请重新向 " + bot_name + " 机器人重新申请。",
            "如非本人操作，请忽略此邮件",
        ]
        msg = MIMEText("\n".join(body_lines), "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((self.from_name, self.username))
        msg["To"] = email
        try:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15) as server:
                server.login(self.username, self.auth_code)
                server.sendmail(self.username, [email], msg.as_string())
            return True, "邮件已发送"
        except smtplib.SMTPException as e:
            return False, f"发信失败: {e}"
        except OSError as e:
            return False, f"网络错误: {e}"


class VerifyManager:
    """验证码生成 / 限频 / 封顶 / 校验"""

    def __init__(self, sender: MailSender, code_ttl: int = CODE_TTL,
                 retry_cooldown: int = RETRY_COOLDOWN, max_mail: int = MAX_MAIL_PER_EMAIL):
        self.sender = sender
        self.code_ttl = code_ttl
        self.retry_cooldown = retry_cooldown
        self.max_mail = max_mail
        # 申请人验证码：{openid: {email, code, expire}}
        self._verify_user: dict = {}
        # 邮箱发信计数/冷却：{email: {last_sent, sent_count}}
        self._verify_email: dict = {}
        self._load_verify()

    # ────────────── 持久化 ──────────────
    def _load_verify(self):
        try:
            with open(VERIFY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._verify_user = data.get("users", {})
            self._verify_email = data.get("emails", {})
        except (OSError, json.JSONDecodeError):
            self._verify_user = {}
            self._verify_email = {}

    def _save_verify(self):
        try:
            atomic_write_json(VERIFY_FILE, {"users": self._verify_user, "emails": self._verify_email})
        except OSError as e:
            print(f"[whitelist] 保存 verify.json 失败: {e}")

    # ────────────── 申请验证码 ──────────────
    def request_code(self, user_openid: str, email: str, group_name: str, bot_name: str) -> tuple[bool, str, str]:
        """申请并发送验证码。返回 (ok, msg, code)。
        验证码按申请人 QQ openid 绑定，冷却/封顶按邮箱记录（发送成功才计数）。
        """
        user_openid = (user_openid or "").strip()
        email = (email or "").strip().lower()
        now = time.time()
        if not user_openid:
            return False, "无法识别申请人喵，请重试", ""

        # 1. 冷却期检查：该邮箱 4 分钟内已成功发送则拒绝
        erec = self._verify_email.get(email)
        if erec and erec.get("last_sent") and (now - erec["last_sent"]) < self.retry_cooldown:
            wait = int(self.retry_cooldown - (now - erec["last_sent"]))
            return False, f"申请太频繁喵，请 {wait} 秒后再试", ""

        # 2. 封顶检查：该邮箱累计成功发送达到上限则拒绝
        if erec and erec.get("sent_count", 0) >= self.max_mail:
            return False, f"该邮箱已申请 {self.max_mail} 次且尚未绑定，请联系管理员处理喵", ""

        # 3. 该申请人已有未过期验证码时，先作废（避免堆积）
        if user_openid in self._verify_user and now <= self._verify_user[user_openid].get("expire", 0):
            del self._verify_user[user_openid]

        # 4. 生成新验证码（4 位数字）
        code = f"{random.randint(0, 9999):04d}"
        ok, msg = self.sender.send_code(email, code, group_name, bot_name)
        if not ok:
            return False, msg, ""

        # 5. 发送成功才更新记录（邮箱计数 + 冷却 / 用户验证码）
        if not erec:
            erec = {"last_sent": now, "sent_count": 1}
        else:
            erec["last_sent"] = now
            erec["sent_count"] = erec.get("sent_count", 0) + 1
        self._verify_email[email] = erec
        self._verify_user[user_openid] = {"email": email, "code": code, "expire": now + self.code_ttl}
        self._save_verify()
        return True, "验证码已发送到您的邮箱，请查收喵", code

    # ────────────── 校验验证码 ──────────────
    def verify_code(self, user_openid: str, code: str) -> tuple[bool, str, str]:
        """按申请人 QQ openid 校验验证码。返回 (ok, msg, 关联邮箱)。
        验证成功后清除该申请人的验证码记录（实现"绑定成功后清零"）。
        """
        user_openid = (user_openid or "").strip()
        code = (code or "").strip()
        urec = self._verify_user.get(user_openid)
        if not urec:
            return False, "验证码不存在或已过期，请重新申请喵", ""
        if urec.get("code") != code:
            return False, "验证码错误喵，请核对后重试", ""
        if time.time() > urec.get("expire", 0):
            self._verify_user.pop(user_openid, None)
            self._save_verify()
            return False, "验证码已过期，请重新申请喵", ""

        email = urec.get("email", "")
        # 验证成功：清除该申请人记录（邮箱封顶计数保留，供下次绑定重新累计）
        self._verify_user.pop(user_openid, None)
        self._save_verify()
        return True, "验证通过", email


class WhitelistStore:
    """白名单数据：{群openid: {玩家名: {email, bind_time, uuid}}}"""

    def __init__(self, path: str = WHITELIST_FILE):
        self.path = path
        self._data: dict = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self):
        try:
            atomic_write_json(self.path, self._data)
        except OSError as e:
            print(f"[whitelist] 保存 whitelist.json 失败: {e}")

    def add(self, gid: str, player_name: str, email: str, bind_openid: str = ""):
        """绑定白名单：玩家名 -> 邮箱。bind_openid 记录绑定人 QQ（设备登录批准时校验）"""
        gid = gid or "nogroup"
        group = self._data.setdefault(gid, {})
        group[player_name] = {
            "email": email,
            "bind_time": int(time.time()),
            "uuid": "",
            "bind_openid": bind_openid or "",
            "frozen": False,
        }
        self._save()

    def get_record(self, gid: str, player_name: str) -> dict | None:
        return self._data.get(gid or "nogroup", {}).get(player_name)

    def find_by_openid(self, gid: str, openid: str) -> str:
        """按绑定人 QQ openid 反查其在群内的白名单玩家名（首个匹配）；未绑定返回空串"""
        if not openid:
            return ""
        for name, rec in (self._data.get(gid or "nogroup", {}) or {}).items():
            if rec.get("bind_openid") == openid:
                return name
        return ""

    def claim_single(self, gid: str, openid: str) -> str:
        """群里仅剩一条未绑定主人的白名单记录时，自动认领为当前用户（防误领：多条记录时不动）。返回玩家名或空串"""
        if not openid:
            return ""
        group = self._data.get(gid or "nogroup", {}) or {}
        unbound = [n for n, r in group.items() if not r.get("bind_openid")]
        if len(unbound) == 1:
            group[unbound[0]]["bind_openid"] = openid
            self._save()
            return unbound[0]
        return ""

    def claim_bind(self, gid: str, player_name: str, bind_openid: str) -> bool:
        """旧记录没有绑定人时，首次批准登录视为本人认领并写入（防止后续冒认）"""
        rec = self.get_record(gid, player_name)
        if rec is None or not bind_openid or rec.get("bind_openid"):
            return False
        rec["bind_openid"] = bind_openid
        self._save()
        return True

    def update_uuid(self, gid: str, player_name: str, new_uuid: str) -> bool:
        """批准设备登录：把登记 UUID 更新为玩家当前设备。返回是否成功"""
        rec = self.get_record(gid, player_name)
        if rec is None or not new_uuid:
            return False
        rec["uuid"] = new_uuid
        self._save()
        return True

    def freeze_by_openid(self, gid: str, openid: str) -> int:
        """用户退群：冻结其绑定人 openid 对应的全部白名单记录。返回冻结条数"""
        if not openid:
            return 0
        group = self._data.get(gid or "nogroup", {}) or {}
        cnt = 0
        for rec in group.values():
            if rec.get("bind_openid") == openid and not rec.get("frozen"):
                rec["frozen"] = True
                cnt += 1
        if cnt:
            self._save()
        return cnt

    def unfreeze_by_openid(self, gid: str, openid: str) -> int:
        """用户入群：解冻其绑定人 openid 对应的全部白名单记录。返回解冻条数"""
        if not openid:
            return 0
        group = self._data.get(gid or "nogroup", {}) or {}
        cnt = 0
        for rec in group.values():
            if rec.get("bind_openid") == openid and rec.get("frozen"):
                rec["frozen"] = False
                cnt += 1
        if cnt:
            self._save()
        return cnt

    def is_frozen(self, gid: str, player_name: str) -> bool:
        rec = self.get_record(gid, player_name)
        return bool(rec and rec.get("frozen"))

    def check(self, gid: str, player_name: str, uuid: str = "") -> tuple[str, str]:
        """进服判定：返回 (result, registered_uuid)。
        result: accept / need_login / not_in_whitelist / frozen
        逻辑：已被冻结（退群自动冻结）→ frozen；
              白名单内且（无登记uuid 或 uuid 一致）→ accept 并登记 uuid；
              白名单内但 uuid 不一致 → need_login；不在白名单 → not_in_whitelist
        """
        gid = gid or "nogroup"
        group = self._data.get(gid, {})
        rec = group.get(player_name)
        if rec is None:
            return "not_in_whitelist", ""
        if rec.get("frozen"):
            return "frozen", ""

        registered = rec.get("uuid", "")
        if registered == "":
            # 首次进服：登记设备并放行，记录最后进服时间
            rec["uuid"] = uuid
            rec["last_join_time"] = int(time.time())
            self._save()
            return "accept", uuid
        if uuid != "" and registered != uuid:
            return "need_login", registered
        # 设备一致：放行并刷新最后进服时间
        rec["last_join_time"] = int(time.time())
        self._save()
        return "accept", registered

    def check_multi(self, owner_gid: str, shared_gids, player_name: str, uuid: str = ""):
        """合并判定：owner_gid（权威）+ shared_gids 的白名单并集。
        返回 (result, registered_uuid, hit_gid)
        result: accept / need_login / not_in_whitelist / frozen
        规则：
          - 先查 owner_gid 群记录（权威）：命中则按与 check() 相同的逻辑判定
            （frozen→frozen；registered==""→登记 uuid 并 accept；uuid 同→accept；uuid 异→need_login），hit_gid=owner_gid
          - owner 群无记录：遍历 shared_gids 任一群命中：
              * 该记录 frozen → frozen（并集内任一冻结即拒绝）
              * registered==""：不登记（避免把 uuid 写到非权威群），返回 accept（放行但 hit_gid=命中群）
              * uuid==registered → accept
              * 否则 → need_login
            hit_gid = 第一个命中记录所在群
          - 全未命中 → (not_in_whitelist, "", "")
        """
        owner_gid = owner_gid or "nogroup"
        # ① owner 群（权威）：与 check() 判定逻辑一致，允许登记 uuid
        group = self._data.get(owner_gid, {})
        rec = group.get(player_name)
        if rec is not None:
            if rec.get("frozen"):
                return "frozen", "", owner_gid
            registered = rec.get("uuid", "")
            if registered == "":
                rec["uuid"] = uuid
                self._save()
                return "accept", uuid, owner_gid
            if uuid != "" and registered != uuid:
                return "need_login", registered, owner_gid
            return "accept", registered, owner_gid
        # ② owner 群无记录：遍历 shared_gids（共享群），只读判定，不登记 uuid
        for sgid in (shared_gids or []):
            if not sgid:
                continue
            srec = self._data.get(sgid, {}).get(player_name)
            if srec is None:
                continue
            if srec.get("frozen"):
                return "frozen", "", sgid
            registered = srec.get("uuid", "")
            if registered == "":
                return "accept", "", sgid
            if uuid != "" and registered != uuid:
                return "need_login", registered, sgid
            return "accept", registered, sgid
        return "not_in_whitelist", "", ""

    def is_bound(self, gid: str, player_name: str) -> bool:
        return player_name in self._data.get(gid or "nogroup", {})


class PendingStore:
    """待批准的设备登录请求（换设备进服被判 need_login 时记录）：
    {群openid: {玩家名: {uuid, ip, ts}}}
    玩家在群里发"登录 玩家名"后由 BOT 校验并批准，更新白名单登记的 UUID。
    """

    def __init__(self, path: str = PENDING_FILE):
        self.path = path
        self._data: dict = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self):
        try:
            atomic_write_json(self.path, self._data)
        except OSError as e:
            print(f"[pending] 保存 pending.json 失败: {e}")

    def record(self, gid: str, player_name: str, new_uuid: str, ip: str = ""):
        """记录一次换设备进服：同玩家反复尝试时覆盖旧记录"""
        gid = gid or "nogroup"
        if not new_uuid or not player_name:
            return
        self._data.setdefault(gid, {})[player_name] = {
            "uuid": new_uuid, "ip": ip, "ts": int(time.time()),
        }
        self._save()

    def get(self, gid: str, player_name: str) -> dict | None:
        return self._data.get(gid or "nogroup", {}).get(player_name)

    def group_pending(self, gid: str) -> dict:
        return dict(self._data.get(gid or "nogroup", {}))

    def pop(self, gid: str, player_name: str):
        rec = self._data.get(gid or "nogroup", {}).pop(player_name, None)
        if rec is not None:
            self._save()
        return rec