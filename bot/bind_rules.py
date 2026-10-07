# -*- coding: utf-8 -*-
"""邮箱绑定规则（**纯函数、无 IO**，便于单元测试）

为什么单独抽出来：这些规则以前写在 cmd_bind_email 里，既无法测试，又容易在改动中被塞进
错误的缩进层级（曾导致"校验写在 except 里、永不执行"的严重问题）。
"""

import re

BOT_QQ = "4014747343"          # 机器人自己的 QQ，禁止被绑定
QQ_MIN, QQ_MAX = 5, 12         # QQ 号长度范围
_EMAIL_RE = re.compile(r"^[0-9]{5,12}@qq\.com$")


def normalize_email(raw: str) -> str:
    """把输入规范成邮箱：纯数字（5~12 位）自动补 @qq.com，其余原样小写去空白。"""
    s = (raw or "").strip()
    if s.isdigit() and QQ_MIN <= len(s) <= QQ_MAX:
        return s + "@qq.com"
    return s.lower()


def is_valid_qq_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").lower()))


def _bound_email_by_ids(whitelist_data: dict, ids: set) -> str:
    """白名单记录里，绑定人命中任一 id 且带邮箱的那条记录的邮箱"""
    for _recs in (whitelist_data or {}).values():
        for _rec in (_recs or {}).values():
            _rec = _rec or {}
            if (_rec.get("bind_openid") or "") in ids and _rec.get("email"):
                return _rec.get("email")
    return ""


def _email_in_whitelist(whitelist_data: dict, email: str) -> bool:
    e = (email or "").lower()
    if not e:
        return False
    for _recs in (whitelist_data or {}).values():
        for _rec in (_recs or {}).values():
            if ((_rec or {}).get("email") or "").lower() == e:
                return True
    return False


def _requested_email_in_whitelist(whitelist_data: dict, email: str) -> str:
    """该邮箱若已在白名单里，返回它的绑定人（可能为空串），否则返回 None"""
    e = (email or "").lower()
    for _recs in (whitelist_data or {}).values():
        for _rec in (_recs or {}).values():
            if ((_rec or {}).get("email") or "").lower() == e:
                return (_rec or {}).get("bind_openid") or ""
    return None


def check_bind_request(raw: str, user_openid: str, union_openid: str,
                       whitelist_data: dict, verify_users: dict,
                       bot_qq: str = BOT_QQ) -> tuple:
    """判断这次「绑定」请求是否允许。返回 (allowed: bool, email: str, reason: str)。

    规则（任一命中即拒绝）：
      1. 格式非法（非「纯数字@qq.com」，长度 5~12）
      2. 是机器人自己的 QQ
      3. 该邮箱已出现在白名单记录里
      4. 发送者身份（member/union openid）一个都取不到
      5. 发送者已绑定过（白名单记录命中任一 id，或他申请过验证码的邮箱已在白名单里）
    """
    email = normalize_email(raw)
    if not is_valid_qq_email(email):
        return False, email, "只支持纯数字 QQ 号（5~12 位）"
    if email == (bot_qq + "@qq.com"):
        return False, email, "不能绑定机器人自己的 QQ 号"
    ids = {x for x in ((user_openid or ""), (union_openid or "")) if x}
    if not ids:
        return False, email, "无法识别你的身份，请 @机器人 后重试"
    if _requested_email_in_whitelist(whitelist_data, email) is not None:
        return False, email, "该邮箱已经在白名单绑定记录里，不能再次绑定"
    mine = _bound_email_by_ids(whitelist_data, ids)
    if not mine:
        # 第二信号：他申请验证码用过的邮箱，且该邮箱已在白名单里 → 认定已绑定
        for _id in ids:
            _mail = ((verify_users or {}).get(_id) or {}).get("email") or ""
            if _mail and _email_in_whitelist(whitelist_data, _mail):
                mine = _mail
                break
    if mine:
        return False, email, "你已经绑定过邮箱（" + str(mine) + "），不能再次绑定；换绑请用 邮箱改绑"
    return True, email, ""
