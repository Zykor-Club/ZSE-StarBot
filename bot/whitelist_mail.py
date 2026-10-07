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
import struct
import tempfile
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

_BASE = os.path.dirname(os.path.abspath(__file__))
VERIFY_FILE = os.path.join(_BASE, "verify.json")
WHITELIST_FILE = os.path.join(_BASE, "whitelist.json")
PENDING_FILE = os.path.join(_BASE, "pending.json")
CHANGES_FILE = os.path.join(_BASE, "whitelist_changes.json")

# 默认规则（可被 config 覆盖）
CODE_TTL = 300          # 验证码有效期 5 分钟
RETRY_COOLDOWN = 240    # 同一邮箱 4 分钟限发 1 封
MAX_MAIL_PER_EMAIL = 5  # 同一邮箱累计成功发送封顶

# 白名单变更规则
RENAME_COOLDOWN = 48 * 3600        # 修改玩家名：48 小时内限一次
EMAIL_CHANGE_COOLDOWN = 7 * 86400  # 邮箱改绑：7 天内限一次
EMAIL_CHANGE_TTL = 24 * 3600       # 邮箱改绑须 24 小时内完成，否则自动回滚原白名单

# 设备登录校验（可在 config.yaml 的 device_check 段覆盖）
DEVICE_CITY_CHECK = True                              # 城市（IP 跨市级变动）校验总开关
CITY_MAX = 3                                          # 每个白名单记录的常用城市上限（LRU 超出淘汰最久未用）
IP2REGION_XDB = os.path.join(_BASE, "ip2region.xdb")  # ip2region 离线库默认路径（bot 目录）


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


_QQ_EMAIL_RE = re.compile(r"^\d{5,12}@qq\.com$")


def check_qq_email(email: str) -> bool:
    """绑定邮箱只允许「纯数字@qq.com」= QQ 号本身。

    用户口径（2026-10-04）：邮箱就是用来拿玩家 QQ 号、方便识别与追责开挂用户的，
    所以不脱敏、也不允许随便填；同时它也因此成了强身份锚点，必须限制格式。
    """
    return bool(_QQ_EMAIL_RE.fullmatch((email or "").strip().lower()))


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

    # ────────────── 上限重置（管理员指令用） ──────────────
    def reset_email_limit(self, email: str) -> tuple:
        """清零某邮箱的申请计数与冷却。返回 (ok, msg)。

        注意：必须同时改内存与落盘 —— 只改 verify.json 文件会被内存副本覆盖回去。
        """
        email = (email or "").strip().lower()
        if not email:
            return False, "请提供 QQ 号"
        rec = self._verify_email.get(email)
        if not rec:
            return False, "该邮箱没有申请记录"
        old = int(rec.get("sent_count", 0) or 0)
        rec["sent_count"] = 0
        rec["last_sent"] = 0
        self._save_verify()
        return True, "已重置（原计数 %d）" % old

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

        # 4. 生成新验证码（6 位数字）
        code = f"{random.randint(0, 999999):06d}"
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
            # 错误计数：连续错 5 次直接作废该验证码（6 位码也挡不住无限次爆破，必须限次）
            urec["fails"] = int(urec.get("fails") or 0) + 1
            if urec["fails"] >= 5:
                self._verify_user.pop(user_openid, None)
                self._save_verify()
                return False, "验证码错误次数过多已作废，请重新申请喵", ""
            self._save_verify()
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


class CityResolver:
    """ip2region 离线 IP 库（数据文件来自 lionsoul2014/ip2region，兼容两种结构版本）：
    IPv4 -> 城市名，用于判定"IP 跨市级变动"。
    文件结构：Header(256B) + 向量索引(256×256×8B) + 段索引(每条14B) + 数据段（两种结构一致）。
    段数据字段：结构v2（旧 ip2region.xdb）「国家|区域|省份|城市|ISP」城市在索引3；
                结构v3（新 ip2region_v4.xdb）「国家|省份|城市|ISP|国家代码」城市在索引2。
    文件缺失/越界/无城市字段一律返回空串（上层自动跳过城市校验，不影响进服）。"""

    _HEADER_LEN = 256     # 文件头长度
    _VECTOR_SIZE = 8      # 向量索引每条：startPtr(4) + endPtr(4)
    _VECTOR_COLS = 256    # 向量索引列数（每行 256 条）
    _SEGMENT_SIZE = 14    # 段索引每条：sip(4) + eip(4) + dataLen(2) + dataPtr(4)

    def __init__(self, path: str = ""):
        self.path = path or IP2REGION_XDB
        self._data = b""
        self._available = False
        self._city_idx = 3  # 段数据中城市字段索引：结构v2=3，结构v3=2（见类注释）
        self._cache: dict = {}  # {ip: 城市}：常用 IP 直接复用，避免重复二分
        try:
            with open(self.path, "rb") as f:
                data = f.read()
            if len(data) > self._HEADER_LEN + self._VECTOR_COLS * self._VECTOR_SIZE:
                self._data = data
                self._available = True
                # Header 偏移0的 u16(LE) 为结构版本号（官方 Header.version）：>=3 为「国家|省份|城市|ISP|国家代码」
                ver = struct.unpack_from("<H", data, 0)[0]
                self._city_idx = 2 if ver >= 3 else 3
        except OSError:
            pass

    @property
    def available(self) -> bool:
        return self._available

    def lookup(self, ip: str) -> str:
        """返回城市名（如「深圳市」）；无法解析或无城市信息返回空串"""
        if not self._available:
            return ""
        if ip in self._cache:
            return self._cache[ip]
        city = self._search(ip)
        if len(self._cache) > 1024:
            self._cache.clear()
        self._cache[ip] = city
        return city

    def _search(self, ip: str) -> str:
        try:
            v = self._ip_to_int(ip)
            if v < 0:
                return ""
            # ① 向量索引：按 IP 前两字节定位该网段的段索引区间
            idx = self._HEADER_LEN + (((v >> 24) & 0xFF) * self._VECTOR_COLS + ((v >> 16) & 0xFF)) * self._VECTOR_SIZE
            s_ptr, e_ptr = struct.unpack_from("<II", self._data, idx)
            if s_ptr == 0 or e_ptr == 0 or e_ptr <= s_ptr:
                return ""
            # ② 段索引二分查找命中的 IP 段
            lo, hi = 0, (e_ptr - s_ptr) // self._SEGMENT_SIZE
            while lo <= hi:
                mid = (lo + hi) // 2
                pos = s_ptr + mid * self._SEGMENT_SIZE
                sip, eip, dlen, dptr = struct.unpack_from("<IIHI", self._data, pos)
                if v < sip:
                    hi = mid - 1
                elif v > eip:
                    lo = mid + 1
                else:
                    # ③ 段数据：取城市字段（结构v2 索引3 / 结构v3 索引2）
                    region = self._data[dptr:dptr + dlen].decode("utf-8", "ignore")
                    fields = region.split("|")
                    city = fields[self._city_idx].strip() if len(fields) > self._city_idx else ""
                    return "" if city in ("", "0") else city
            return ""
        except (struct.error, IndexError):
            return ""

    @staticmethod
    def _ip_to_int(ip: str) -> int:
        """点分十进制 IP -> 32 位整数（非法返回 -1）"""
        parts = (ip or "").strip().split(".")
        if len(parts) != 4:
            return -1
        value = 0
        for p in parts:
            if not p.isdigit() or len(p) > 3 or int(p) > 255:
                return -1
            value = (value << 8) | int(p)
        return value


class WhitelistStore:
    """白名单数据：{群openid: {玩家名: {email, bind_time, uuid, bind_openid, frozen,
    devices: [{uuid, platform, first_seen, last_seen}], cities: [{city, ts}]}}}
    - devices：该玩家登记过的设备（每个设备一条 UUID，多设备互不顶）
    - cities：常用城市集合（≤ city_max 个，LRU 淘汰最久未用）"""

    def __init__(self, path: str = WHITELIST_FILE):
        self.path = path
        self._data: dict = {}
        self._changes = None  # ChangeStore（邮箱改绑超时回滚用），由 attach_changes 注入
        self._city_check = DEVICE_CITY_CHECK  # 城市校验开关（配置注入，见 configure_device_check）
        self._city_max = CITY_MAX             # 常用城市上限
        self._city: CityResolver | None = None
        self._load()

    def attach_changes(self, changes: "ChangeStore"):
        """注入变更事务存储：check/check_multi 判定前会做一次懒回滚（过期改绑→恢复原白名单）"""
        self._changes = changes

    def configure_device_check(self, enabled: bool = DEVICE_CITY_CHECK,
                               xdb_path: str = "", city_max: int = CITY_MAX):
        """注入设备校验配置（main.py 启动时从 config.yaml 的 device_check 段读取）"""
        self._city_check = bool(enabled)
        self._city_max = max(1, int(city_max or CITY_MAX))
        if self._city_check:
            self._city = CityResolver(xdb_path or IP2REGION_XDB)
            if not self._city.available:
                print(f"[whitelist] ip2region 离线库不可用（{self._city.path}），IP 跨市校验自动跳过")

    def resolve_city(self, ip: str) -> str:
        """解析 IP 所在城市；离线库不可用或解析失败返回空串（上层自动跳过城市校验）"""
        if self._city is None:
            return ""
        return self._city.lookup(ip)

    # ────────────── 设备 / 城市（多设备登记 + 常用城市 LRU） ──────────────
    @staticmethod
    def _devices(rec: dict) -> list:
        """取设备列表；旧数据（只有 uuid 字段）就地迁移成一条设备记录"""
        devices = rec.get("devices")
        if not isinstance(devices, list):
            devices = []
            legacy = (rec.get("uuid") or "").strip()
            if legacy:
                ts = int(rec.get("last_join_time") or rec.get("bind_time") or time.time())
                devices.append({"uuid": legacy, "platform": "", "first_seen": ts, "last_seen": ts})
            rec["devices"] = devices
        return devices

    @staticmethod
    def _cities(rec: dict) -> list:
        cities = rec.get("cities")
        if not isinstance(cities, list):
            cities = []
            rec["cities"] = cities
        return cities

    def _city_known(self, rec: dict, city: str) -> bool:
        """城市是否在常用城市集合内。集合为空（无历史基线，如旧数据迁移/从未留过城市）视为通过：
        没有基线就谈不上"变动"，避免老白名单首次进服被误判跨市；首登会由 check 建立基线"""
        cities = self._cities(rec)
        if not cities:
            return True
        return any(c.get("city") == city for c in cities)

    def _city_touch(self, rec: dict, city: str):
        """记入/刷新常用城市（LRU：超出上限时淘汰最久未用的一条）"""
        if not city:
            return
        cities = self._cities(rec)
        now = int(time.time())
        for c in cities:
            if c.get("city") == city:
                c["ts"] = now
                return
        cities.append({"city": city, "ts": now})
        if len(cities) > self._city_max:
            cities.sort(key=lambda c: int(c.get("ts") or 0))
            del cities[:len(cities) - self._city_max]

    @staticmethod
    def synth_key(platform: str, city: str) -> str:
        """空 UUID 客户端的合成设备键（平台+城市）；平台/城市缺失时用 ? 兜底保证稳定"""
        return f"nouuid:{(platform or '?').strip().lower()}:{(city or '?').strip().lower()}"

    def _judge(self, rec: dict, uuid: str, platform: str, city: str) -> tuple[str, str]:
        """只读判定一条记录的设备/城市是否放行。返回 (result, reason)：
        设备命中 → 城市命中（或城市校验关闭/城市解析失败）放行，不在集合 → ip_change；
        设备未命中 → 无任何设备（首次）放行，否则 need_login（同平台+城市命中=uuid_change，否则 new_device）"""
        if rec.get("frozen"):
            return "frozen", ""
        devices = self._devices(rec)
        if rec.get("need_relogin"):
            return "need_login", "new_device"  # 改名后：设备已清空，强制重新确认登录
        if not uuid:
            # 客户端未上报 UUID（个别 PC 端）：**不能无条件放行**——否则清空 UUID 就能绕过设备校验冒充他人。
            # 改用「平台+城市」合成设备键：无设备基线 → 首次放行并登记；已有基线 → 必须命中该合成键，
            # 否则走群里批准一次（批准后同平台+同城市不再打扰）。
            key = self.synth_key(platform, city)
            if not devices:
                return "accept", ""
            if any((d.get("uuid") or "") == key for d in devices):
                return "accept", ""
            return "need_login", "new_device"
        for d in devices:
            if d.get("uuid") == uuid:
                if self._city_check and city and not self._city_known(rec, city):
                    return "need_login", "ip_change"
                if self._city_check and not city and self._cities(rec):
                    # 城市解析失败且记录已有城市基线 → fail-closed（对齐 CaiBotLite 的 try_login_ok）
                    return "need_login", "ip_change"
                return "accept", ""
        if not devices:
            return "accept", ""  # 首次设备：自动登记放行
        platform_match = bool(platform) and any((d.get("platform") or "") == platform for d in devices)
        city_ok = (not self._city_check) or (not city) or self._city_known(rec, city)
        return "need_login", ("uuid_change" if (platform_match and city_ok) else "new_device")

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
            "devices": [],
            "cities": [],
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

    def rename(self, gid: str, old_name: str, new_name: str) -> tuple[bool, str]:
        """修改白名单玩家名：记录整体搬到新名字下（邮箱/绑定人/绑定时间保留）。
        登录记录清除（uuid 置空 + need_relogin），新名字须重新确认登录才能进服；
        不涉及存档：游戏存档按原玩家名保存在 TShock 侧，本操作天然不迁移。"""
        gid = gid or "nogroup"
        group = self._data.get(gid, {})
        old_rec = group.get(old_name)
        if old_rec is None:
            return False, "原玩家名不在白名单中"
        if new_name in group:
            return False, "新玩家名已被占用，请换一个名字"
        rec = dict(old_rec)
        rec["uuid"] = ""
        rec.pop("devices", None)  # 新名字=全新身份：登记设备与常用城市全部重置
        rec.pop("cities", None)
        rec["need_relogin"] = True  # 阻止"首次进服自动登记放行"，强制走"登录"批准流程
        rec.pop("last_join_time", None)
        del group[old_name]
        group[new_name] = rec
        self._save()
        return True, ""

    def remove(self, gid: str, player_name: str) -> bool:
        """移除一条白名单记录（邮箱改绑开始时作废原白名单用）。返回是否移除"""
        group = self._data.get(gid or "nogroup", {})
        if player_name not in group:
            return False
        del group[player_name]
        self._save()
        return True

    def restore(self, gid: str, player_name: str, record: dict) -> bool:
        """回滚恢复：把备份记录放回原玩家名（仅当该名字当前空缺）。返回是否恢复"""
        group = self._data.setdefault(gid or "nogroup", {})
        if player_name in group:
            return False
        group[player_name] = record
        self._save()
        return True

    def approve_device(self, gid: str, player_name: str, new_uuid: str,
                       platform: str = "", city: str = "") -> bool:
        """批准设备登录：把当前设备加入该玩家的设备列表（多设备互不顶），
        记入常用城市，并解除改名后的待重登标记。返回是否成功。
        客户端未上报 UUID（部分 PC 端）时：跳过设备登记，仅解除重登标记并记城市。"""
        rec = self.get_record(gid, player_name)
        if rec is None:
            return False
        if new_uuid:
            devices = self._devices(rec)
            now = int(time.time())
            for d in devices:
                if d.get("uuid") == new_uuid:
                    d["last_seen"] = now
                    if platform:
                        d["platform"] = platform
                    break
            else:
                devices.append({"uuid": new_uuid, "platform": platform or "", "first_seen": now, "last_seen": now})
            rec["uuid"] = new_uuid  # 兼容旧字段：保留最近一次批准的设备
        self._city_touch(rec, city)
        rec.pop("need_relogin", None)
        self._save()
        return True

    def clear_devices(self, gid: str, player_name: str) -> int:
        """清空该玩家已登录的全部设备（清 devices/uuid + 置 need_relogin 强制重新登录）。
        常用城市保留（清设备≠换地区，重登时城市校验仍生效）。
        返回清掉的设备数；记录不存在返回 -1。"""
        rec = self.get_record(gid, player_name)
        if rec is None:
            return -1
        cnt = len(self._devices(rec))
        rec["devices"] = []
        rec["uuid"] = ""
        rec["need_relogin"] = True  # 阻止"首次进服自动登记放行"，强制走"登录"批准流程
        self._save()
        return cnt

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

    def check(self, gid: str, player_name: str, uuid: str = "",
              platform: str = "", city: str = "") -> tuple[str, str, str]:
        """进服判定：返回 (result, registered_uuid, reason)。
        result: accept / need_login / not_in_whitelist / frozen
        reason: need_login 时给出原因（new_device 新设备 / uuid_change UUID变动 / ip_change IP跨市级变动）
        """
        gid = gid or "nogroup"
        if self._changes is not None:
            self._changes.sweep(self)  # 懒回滚：过期的邮箱改绑先恢复原白名单再判定
        rec = self._data.get(gid, {}).get(player_name)
        if rec is None:
            return "not_in_whitelist", "", ""
        result, reason = self._judge(rec, uuid, platform, city)
        if result != "accept":
            return result, "", reason
        now = int(time.time())
        devices = self._devices(rec)
        if not uuid:
            # 空 UUID：登记/更新合成设备键（首次，或已批准后的同平台+同城市）
            key = self.synth_key(platform, city)
            hit0 = next((d for d in devices if (d.get("uuid") or "") == key), None)
            if hit0 is None:
                devices.append({"uuid": key, "platform": platform or "",
                                "first_seen": now, "last_seen": now})
            else:
                hit0["last_seen"] = now
            self._city_touch(rec, city)
            rec["last_join_time"] = now
            self._save()
            return "accept", key, ""
        hit = next((d for d in devices if d.get("uuid") == uuid), None)
        if hit is None:
            # 首次设备：自动登记并放行
            devices.append({"uuid": uuid, "platform": platform or "", "first_seen": now, "last_seen": now})
            rec["uuid"] = uuid
        elif hit is not None:
            hit["last_seen"] = now
            if platform and not hit.get("platform"):
                hit["platform"] = platform
        self._city_touch(rec, city)
        rec["last_join_time"] = now
        self._save()
        return "accept", uuid or "", ""

    def check_multi(self, owner_gid: str, shared_gids, player_name: str,
                    uuid: str = "", platform: str = "", city: str = ""):
        """合并判定：owner_gid（权威）+ shared_gids 白名单并集。
        返回 (result, registered_uuid, hit_gid, reason)
        - owner 群命中：与 check() 完全一致（允许登记设备/写 LRU）
        - 共享群命中：只读判定（不登记、不写 LRU），hit_gid=命中群
        - 全未命中 → (not_in_whitelist, "", "", "")
        """
        owner_gid = owner_gid or "nogroup"
        if self._changes is not None:
            self._changes.sweep(self)
        if self._data.get(owner_gid, {}).get(player_name) is not None:
            result, registered, reason = self.check(owner_gid, player_name, uuid, platform, city)
            return result, registered, owner_gid, reason
        for sgid in (shared_gids or []):
            if not sgid:
                continue
            srec = self._data.get(sgid, {}).get(player_name)
            if srec is None:
                continue
            result, reason = self._judge(srec, uuid, platform, city)
            return result, "", sgid, reason
        return "not_in_whitelist", "", "", ""

    def is_bound(self, gid: str, player_name: str) -> bool:
        return player_name in self._data.get(gid or "nogroup", {})


class PendingStore:
    """待批准的设备登录请求（换设备进服被判 need_login 时记录）：
    {群openid: {玩家名: {uuid, ip, platform, city, reason, ts}}}
    玩家在群里发"登录"后由 BOT 校验并批准（一步批准：立即生效并回执卡片）。
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

    def record(self, gid: str, player_name: str, new_uuid: str, ip: str = "",
               platform: str = "", city: str = "", reason: str = ""):
        """记录一次待批准登录：同玩家反复尝试时覆盖旧记录（reason: new_device/uuid_change/ip_change）"""
        gid = gid or "nogroup"
        if not new_uuid or not player_name:
            return
        self._data.setdefault(gid, {})[player_name] = {
            "uuid": new_uuid, "ip": ip, "platform": platform, "city": city,
            "reason": reason, "ts": int(time.time()),
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


class ChangeStore:
    """白名单变更事务（改名限流 + 邮箱改绑）：
      - 修改玩家名：成功后记录时间，48 小时内不得再次修改
      - 邮箱改绑：开始时原白名单立即作废（备份旧记录）；24 小时内用新邮箱重新走
        「绑定 → 添加白名单」才算改绑成功；超时未完成由 sweep 自动恢复原白名单；
        每 7 天限一次（按发起时间计）
    数据文件 whitelist_changes.json：
      {
        "changes": {openid: {gid, old_name, old_email, new_email, old_record, ts, deadline}},
        "rate":    {openid: {"rename_ts": int, "email_ts": int}}
      }
    """

    def __init__(self, path: str = CHANGES_FILE):
        self.path = path
        self._data = {"changes": {}, "rate": {}}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._data["changes"] = data.get("changes", {}) or {}
            self._data["rate"] = data.get("rate", {}) or {}
        except (OSError, json.JSONDecodeError):
            self._data = {"changes": {}, "rate": {}}

    def _save(self):
        try:
            atomic_write_json(self.path, self._data)
        except OSError as e:
            print(f"[whitelist] 保存 whitelist_changes.json 失败: {e}")

    # ────────────── 限流 ──────────────
    @staticmethod
    def _left(ts, cooldown: int) -> int:
        """距离限流结束还剩多少秒（0 = 已解除）"""
        return max(0, int(cooldown - (time.time() - int(ts or 0))))

    def rename_allowed(self, openid: str) -> tuple[bool, int]:
        """修改玩家名限流检查。返回 (是否允许, 剩余秒)"""
        ts = (self._data["rate"].get(openid or "") or {}).get("rename_ts", 0)
        left = self._left(ts, RENAME_COOLDOWN)
        return left <= 0, left

    def email_allowed(self, openid: str) -> tuple[bool, int]:
        """邮箱改绑限流检查。返回 (是否允许, 剩余秒)"""
        ts = (self._data["rate"].get(openid or "") or {}).get("email_ts", 0)
        left = self._left(ts, EMAIL_CHANGE_COOLDOWN)
        return left <= 0, left

    def mark_rename(self, openid: str):
        """登记一次成功的改名（48 小时限流从现在起算）"""
        self._data["rate"].setdefault(openid or "", {})["rename_ts"] = int(time.time())
        self._save()

    # ────────────── 邮箱改绑事务 ──────────────
    def start_email_change(self, openid: str, gid: str, old_name: str,
                           old_email: str, new_email: str, old_record: dict):
        """开启改绑事务：备份旧记录（超时回滚用），并计入 7 天限流"""
        now = int(time.time())
        self._data["rate"].setdefault(openid or "", {})["email_ts"] = now
        self._data["changes"][openid or ""] = {
            "gid": gid, "old_name": old_name, "old_email": old_email,
            "new_email": new_email, "old_record": old_record,
            "ts": now, "deadline": now + EMAIL_CHANGE_TTL,
        }
        self._save()

    def get_active(self, openid: str) -> dict | None:
        """进行中的改绑事务（无则 None）"""
        return self._data["changes"].get(openid or "")

    def complete(self, openid: str) -> bool:
        """改绑完成：移除事务条目（限流时间保留）。返回是否确实存在进行中事务"""
        if self._data["changes"].pop(openid or "", None) is None:
            return False
        self._save()
        return True

    def sweep(self, whitelist_store: WhitelistStore) -> list:
        """超时未完成的改绑：恢复原白名单记录并移除事务。
        返回回滚列表 [(openid, 玩家名)]，供上层打日志。"""
        now = time.time()
        rolled = []
        for openid, ch in list(self._data["changes"].items()):
            if now <= ch.get("deadline", 0):
                continue
            gid = ch.get("gid") or "nogroup"
            old_name = ch.get("old_name") or ""
            old_rec = ch.get("old_record") or None
            if old_name and old_rec:
                if not whitelist_store.restore(gid, old_name, old_rec):
                    # 回滚失败（名字已被占用等）：保留事务待下次 sweep 重试，直接 pop 会让原白名单永久丢失
                    print(f"[whitelist] 邮箱改绑超时回滚暂失败（名字已被占用，保留事务待重试）: {old_name} @ {gid[:8]}…")
                    continue
                rolled.append((openid, old_name))
            self._data["changes"].pop(openid, None)
            self._save()
        return rolled