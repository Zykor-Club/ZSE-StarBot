# -*- coding: utf-8 -*-
"""查背包图片渲染（Pillow）

自研布局（1920×1080）：
  - 随机背景（cover 裁剪 + 压暗）+ 半透明圆角面板（尽量外扩，格子更大）
  - 分区标题全部用泰拉瑞亚物品图标代替文字（1/2/3 字雕像=三套装备、猪猪存钱罐、保险箱、
    护卫熔炉、虚空保险库、商贩背包、金币、木箭、垃圾桶图标、矿车、红药水、书）
  - 左列：背包 / 钱币 / 弹药 / 垃圾桶 / 增益状态 / 玩家信息
  - 中列：当前装备 + 套装 2 + 套装 3（3 列 × 10 行：染料 | 时装 | 装备）+ 坐骑宠物组
  - 右列：猪猪 / 保险箱 / 熔炉 / 虚空袋（标题图标在各块左侧）

物品格映射按 TShock NetItem 索引（共 350 格）：
  0-58 背包(含钱币/弹药)  59-78 装备(护甲饰品)  79-88 时装染料  89-93 坐骑/宠物/钩爪/矿车/照明
  94-98 对应染料  99-138 猪猪  139-178 保险  179 垃圾桶  180-219 熔炉  220-259 虚空袋
  260-349 三套 Loadout（护甲 20 + 染料 10）×3

图标按需懒加载，素材位于 assets/lookbag/，
本地调试可用环境变量 LOOKBAG_ITEMS_DIR / LOOKBAG_BUFFS_DIR / LOOKBAG_BG_DIR 覆盖。

注意：Pillow 在 RGBA 图上直接绘制半透明图形是"覆盖"而非"混合"，
所以半透明元素（面板、格子底）统一画在独立 overlay 图层上再 alpha_composite。
"""

import os
import random
import time

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

# ── 素材路径（默认 bot 侧 assets/lookbag；可用环境变量覆盖，便于本地调试） ──
_BASE_DIR = os.environ.get("LOOKBAG_ASSETS_BASE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "lookbag")
ITEMS_DIR = os.environ.get("LOOKBAG_ITEMS_DIR") or os.path.join(_BASE_DIR, "items")
BUFFS_DIR = os.environ.get("LOOKBAG_BUFFS_DIR") or os.path.join(_BASE_DIR, "buffs")
BG_DIR = os.environ.get("LOOKBAG_BG_DIR") or os.path.join(_BASE_DIR, "backgrounds")
NAMES_FILE = os.environ.get("LOOKBAG_NAMES_FILE") or os.path.join(_BASE_DIR, "item_names.json")

FONT_REG = "C:/Windows/Fonts/msyh.ttc"       # 微软雅黑
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"    # 微软雅黑粗体

W, H = 1920, 1080
PANEL_BOX = (52, 28, 1868, 1052)             # 底板面板（尽量外扩）
PANEL_RADIUS = 30
BG_BRIGHTNESS = 0.72                          # 背景压暗程度
BG_DARKEN_ALPHA = 110                         # 背景再叠一层黑（0-255）

# 负数 netId（合成/内置）→ 图标用的物品 id（对齐 CaiBotLite）
NET_DEFAULTS = {
    -1: 3521, -2: 3520, -3: 3519, -4: 3518, -5: 3517, -6: 3516, -7: 3515, -8: 3514,
    -9: 3513, -10: 3512, -11: 3511, -12: 3510, -13: 3509, -14: 3508, -15: 3507,
    -16: 3506, -17: 3505, -18: 3504, -19: 3764, -20: 3765, -21: 3766, -22: 3767,
    -23: 3768, -24: 3769, -25: 3503, -26: 3502, -27: 3501, -28: 3500, -29: 3499,
    -30: 3498, -31: 3497, -32: 3496, -33: 3495, -34: 3494, -35: 3493, -36: 3492,
    -37: 3491, -38: 3490, -39: 3489, -40: 3488, -41: 3487, -42: 3486, -43: 3485,
    -44: 3484, -45: 3483, -46: 3482, -47: 3481, -48: 3480,
}

# ── 懒加载缓存 ──
_icon_cache: dict = {}
_sized_cache: dict = {}
_font_cache: dict = {}
_bg_cache: dict = {}
_name_map: dict = None

LEFT_X, MID_X, RIGHT_X = 104, 760, 1350
CELL = 56            # 主格子（背包 / 装备 / 坐骑 / 钱币弹药）
CELL_STORE = 42      # 收纳区格子


def _rng(a, b):
    return list(range(a, b))


def _equip_group(a_start: int, v_start: int, d_start: int) -> list:
    """一个装备组（3 列 × 10 行，行优先展开）：每行依次为 [染料 | 时装 | 装备]。
    与 CaiBotLite / 游戏装备区一致：竖排三列 = 染料(10) 时装(10) 装备(10)。"""
    out = []
    for r in range(10):
        out += [d_start + r, v_start + r, a_start + r]
    return out


# (标题, 标题图标, 索引列表, 列数, 行数, 格子px, x, 网格y, 标签位置)
#   标题图标：int = 物品 id（Item_<id>.png）；str = 素材文件名
#   标签位置："top" = 网格上方（图标+短文字）；"icon" = 网格上方（仅图标）；"left" = 网格左侧（仅图标）
GRID_SECTIONS = [
    # 左列（钱币/弹药/垃圾桶拉开间距，但最右不越过上方背包格的右边界）
    ("背包", 5343, _rng(0, 50), 10, 5, CELL, LEFT_X, 248, "top"),
    ("钱币", 73, _rng(50, 54), 4, 1, CELL, LEFT_X, 620, "top"),
    ("弹药", 40, _rng(54, 58), 4, 1, CELL, LEFT_X + 268, 620, "top"),
    ("垃圾桶", 348, [179], 1, 1, CELL, LEFT_X + 536, 620, "icon"),
    # 中列：三套装备（1/2/3 字雕像）+ 坐骑宠物组（矿车）
    ("当前装备", 2703, _equip_group(59, 69, 79), 3, 10, CELL, MID_X, 248, "top"),
    ("套装 2", 2704, _equip_group(290, 300, 310), 3, 10, CELL, MID_X + 188, 248, "top"),
    ("套装 3", 2705, _equip_group(320, 330, 340), 3, 10, CELL, MID_X + 376, 248, "top"),
    ("坐骑 / 宠物", 2343, _rng(89, 99), 5, 2, CELL, MID_X, 916, "top"),
    # 右列：收纳（标题图标在块左侧）
    ("猪猪", 87, _rng(99, 139), 10, 4, CELL_STORE, RIGHT_X + 40, 248, "left"),
    ("保险箱", 346, _rng(139, 179), 10, 4, CELL_STORE, RIGHT_X + 40, 448, "left"),
    ("熔炉", 3813, _rng(180, 220), 10, 4, CELL_STORE, RIGHT_X + 40, 648, "left"),
    ("虚空袋", 4076, _rng(220, 260), 10, 4, CELL_STORE, RIGHT_X + 40, 848, "left"),
]

SECTION_NAMES = [(s[0], s[2]) for s in GRID_SECTIONS]

# 玩家信息行首图标
ICON_LIFE = 29          # 生命水晶
ICON_MANA = 109         # 魔力水晶
ICON_ROD = 2296         # 冤大头钓竿（渔夫任务 <50 次）
ICON_ROD_GOLD = 2294    # 金钓竿（渔夫任务 ≥50 次）
ICON_ENHANCE = 3335     # 恶魔之心（永久增益）
ICON_COIN = 73          # 金币（经济）
ICON_CLASS = 490        # 战士徽章（职业）
ICON_SKILL = 903        # 计划书（技能）
ICON_BUFFS = 678        # 红药水（增益状态标题）
ICON_INFO = 149         # 书（玩家信息标题）


# ─────────────────────────── 素材加载 ───────────────────────────
def _load_image(path: str):
    """按路径懒加载图片（失败缓存 None，避免反复尝试）"""
    if path in _icon_cache:
        return _icon_cache[path]
    img = None
    try:
        img = Image.open(path).convert("RGBA")
    except (OSError, ValueError):
        img = None
    _icon_cache[path] = img
    return img


def _load_sized(path: str, size: int):
    key = (path, size)
    if key in _sized_cache:
        return _sized_cache[key]
    img = _load_image(path)
    out = None
    if img is not None:
        out = img.resize((size, size), Image.LANCZOS) if img.width != size else img
    _sized_cache[key] = out
    return out


def _icon_path(icon):
    """图标参数：int = 物品 id（含负数映射）；str = 素材文件名"""
    if isinstance(icon, str):
        return os.path.join(ITEMS_DIR, icon)
    net_id = int(icon)
    if net_id < 0:
        net_id = NET_DEFAULTS.get(net_id, 0)
    if net_id <= 0:
        return ""
    return os.path.join(ITEMS_DIR, f"Item_{net_id}.png")


def _load_icon(icon, size: int):
    """物品/素材图标（懒加载 + 尺寸缓存）；无图标返回 None"""
    path = _icon_path(icon)
    if not path:
        return None
    return _load_sized(path, size)


def _load_buff_icon(buff_id: int, size: int):
    if buff_id <= 0:
        return None
    return _load_sized(os.path.join(BUFFS_DIR, f"Buff_{buff_id}.png"), size)


def _pick_background():
    """随机取一张背景并 cover 裁剪到 1920×1080（进程内缓存已裁好的图）"""
    try:
        names = [f for f in os.listdir(BG_DIR)
                 if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    except OSError:
        return None
    if not names:
        return None
    path = os.path.join(BG_DIR, os.environ.get("LOOKBAG_BG_FILE") or random.choice(sorted(names)))
    if path in _bg_cache:
        return _bg_cache[path]
    img = None
    try:
        src = Image.open(path).convert("RGB")
        scale = max(W / src.width, H / src.height)
        nw, nh = int(src.width * scale + 0.5), int(src.height * scale + 0.5)
        src = src.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - W) // 2, (nh - H) // 2
        img = src.crop((left, top, left + W, top + H))
    except (OSError, ValueError):
        img = None
    _bg_cache[path] = img
    return img


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    f = None
    for path in ((FONT_BOLD if bold else FONT_REG), "C:/Windows/Fonts/simhei.ttf"):
        try:
            f = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if f is None:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def _load_names() -> dict:
    """物品名映射（netId -> 名称），用于文字降级卡；缺失时返回空表"""
    global _name_map
    if _name_map is not None:
        return _name_map
    import json
    try:
        with open(NAMES_FILE, "r", encoding="utf-8") as fp:
            _name_map = {str(k): str(v) for k, v in json.load(fp).items()}
    except (OSError, ValueError):
        _name_map = {}
    return _name_map


def item_name(net_id: int) -> str:
    names = _load_names()
    nm = names.get(str(net_id))
    if nm:
        return nm
    if net_id < 0:
        nm = names.get(str(NET_DEFAULTS.get(net_id, 0)))
    return nm or f"物品#{net_id}"


# ─────────────────────────── 绘制辅助 ───────────────────────────
def _cells_overlay(odraw: ImageDraw.ImageDraw, cols: int, rows: int,
                   cell: int, x: int, y: int):
    """在 overlay 图层画格子底：更透的填充 + 白色描边（整层合成）"""
    step = cell + 4
    for i in range(cols * rows):
        cx = x + (i % cols) * step
        cy = y + (i // cols) * step
        odraw.rounded_rectangle(
            (cx, cy, cx + cell - 1, cy + cell - 1), radius=7,
            fill=(255, 255, 255, 14), outline=(255, 255, 255, 115), width=2,
        )


def _paste_icon(img: Image.Image, icon, x: int, y: int, cell: int, pad: int):
    """把图标居中贴进格子"""
    if icon is None:
        return
    size = cell - pad * 2
    ic = icon.resize((size, size), Image.LANCZOS) if icon.width != size else icon
    img.paste(ic, (x + pad, y + pad), mask=ic.split()[3])


def _draw_stack(draw: ImageDraw.ImageDraw, x: int, y: int, cell: int, stack: int):
    if stack <= 1:
        return
    big = cell >= 46
    font = _font(21 if big else 15, bold=True)
    text = str(stack)
    tw = draw.textlength(text, font=font)
    draw.text((x + cell - tw - 3, y + cell - (26 if big else 19)),
              text, font=font, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))


def _draw_section_label(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int,
                        text: str, icon, icon_size: int = 36):
    """分区标题：泰拉瑞亚物品图标（+ 可选短文字，图标在前）"""
    tx = x
    ic = _load_icon(icon, icon_size) if icon else None
    if ic is not None:
        img.paste(ic, (x, y), mask=ic.split()[3])
        tx = x + icon_size + 10
    if text:
        font = _font(26, bold=True)
        ty = y + (icon_size - 34) // 2 if ic is not None else y
        draw.text((tx, ty), text, font=font, fill=(238, 244, 255),
                  stroke_width=2, stroke_fill=(0, 0, 0))


def _place_items(img: Image.Image, draw: ImageDraw.ImageDraw, idx_list: list,
                 cols: int, rows: int, cell: int, x: int, y: int, inv: list):
    """贴物品图标 + 堆叠数字（图标尽量占满格子）"""
    step = cell + 4
    pad = 2 if cell >= 46 else 3
    for i in range(cols * rows):
        if i >= len(idx_list):
            break
        idx = idx_list[i]
        item = inv[idx] if idx < len(inv) else (0, 0)
        net_id, stack = int(item[0]), int(item[1])
        if net_id == 0 or stack <= 0:
            continue
        cx = x + (i % cols) * step
        cy = y + (i // cols) * step
        _paste_icon(img, _load_icon(net_id, cell - pad * 2), cx, cy, cell, pad)
        _draw_stack(draw, cx, cy, cell, stack)


def _draw_buffs(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int, buffs: list):
    """增益状态行（红药水标题图标 + 最多 12 个 buff 图标）"""
    _draw_section_label(img, draw, x, y, "", ICON_BUFFS, 50)
    ids = [int(b) for b in buffs if int(b) > 0][:12]
    if not ids:
        draw.text((x + 62, y + 8), "无增益", font=_font(26), fill=(216, 224, 236),
                  stroke_width=1, stroke_fill=(0, 0, 0))
        return
    size = 42
    for i, bid in enumerate(ids):
        ic = _load_buff_icon(bid, size)
        if ic is None:
            continue
        bx = x + 62 + i * (size + 6)
        img.paste(ic, (bx, y + 4), mask=ic.split()[3])


def _draw_marker(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int, size: int, net_id: int):
    """信息行行首标识：优先用泰拉瑞亚物品图标，缺失时退回小圆点"""
    ic = _load_icon(net_id, size)
    if ic is not None:
        img.paste(ic, (x, y), mask=ic.split()[3])
        return
    draw.ellipse((x + 2, y + size // 2 - 6, x + 14, y + size // 2 + 6), fill=(205, 214, 228))


def _draw_info(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int, payload: dict):
    """玩家信息：生命 / 魔力 / 渔夫任务 / 永久增益 / 经济（行首用对应泰拉瑞亚物品图标，40px）"""
    _draw_section_label(img, draw, x, y, "玩家信息", ICON_INFO, 50)
    font = _font(28)
    font_b = _font(28, bold=True)
    fy = y + 52
    quests = int(payload.get("quests_completed") or 0)
    mk = 40          # 行首图标尺寸
    step = 41        # 行距

    def line(icon_id, label, value, dy):
        _draw_marker(img, draw, x, dy + 2, mk, icon_id)
        tx = x + 52
        draw.text((tx, dy + 6), label, font=font_b, fill=(234, 240, 250),
                  stroke_width=1, stroke_fill=(0, 0, 0))
        draw.text((tx + draw.textlength(label, font=font_b) + 14, dy + 6), value,
                  font=font, fill=(220, 228, 240), stroke_width=1, stroke_fill=(0, 0, 0))

    line(ICON_LIFE, "生命", str(payload.get("life") or "-"), fy)
    line(ICON_MANA, "魔力", str(payload.get("mana") or "-"), fy + step)
    line(ICON_ROD_GOLD if quests >= 50 else ICON_ROD, "渔夫任务", f"{quests} 次", fy + step * 2)

    # 永久增益（恶魔之心标识 + 增益图标行，最多 10 个）
    ey = fy + step * 3
    _draw_marker(img, draw, x, ey + 2, mk, ICON_ENHANCE)
    draw.text((x + 52, ey + 6), "永久增益", font=font_b, fill=(234, 240, 250),
              stroke_width=1, stroke_fill=(0, 0, 0))
    ex = x + 52 + int(draw.textlength("永久增益", font=font_b)) + 14
    enhances = [int(e) for e in (payload.get("enhances") or [])][:10]
    if not enhances:
        draw.text((ex, ey + 6), "无", font=font, fill=(220, 228, 240),
                  stroke_width=1, stroke_fill=(0, 0, 0))
    else:
        for i, eid in enumerate(enhances):
            ic = _load_icon(eid, 32)
            if ic is None:
                continue
            img.paste(ic, (ex + i * 36, ey + 6), mask=ic.split()[3])

    # 经济（Economics 插件数据）：金币图标 + 各货币余额；职业 / 技能各配图标
    eco = payload.get("economic") or {}
    coins = " ".join(r.strip() for r in str(eco.get("Coins") or "").split("\n") if r.strip())
    level = str(eco.get("LevelName") or "").strip()
    skill = str(eco.get("Skill") or "").strip()
    if coins or level or skill:
        line(ICON_COIN, "经济", coins or "-", fy + step * 4)
        if level or skill:
            dy = fy + step * 5
            _draw_marker(img, draw, x, dy + 2, mk, ICON_CLASS)
            t1 = level or "职业:无"
            draw.text((x + 52, dy + 6), t1, font=font, fill=(220, 228, 240),
                      stroke_width=1, stroke_fill=(0, 0, 0))
            t2x = x + 52 + int(draw.textlength(t1, font=font)) + 26
            _draw_marker(img, draw, t2x, dy + 2, mk, ICON_SKILL)
            draw.text((t2x + 52, dy + 6), skill or "技能:无", font=font, fill=(220, 228, 240),
                      stroke_width=1, stroke_fill=(0, 0, 0))


# ─────────────────────────── 主渲染 ───────────────────────────
def render_lookbag_image(payload: dict, server_name: str = "", querier: str = "") -> bytes:
    """渲染背包图，返回 PNG 字节。payload 为插件 look_bag 回包。"""
    inv = [(int(it[0]), int(it[1])) for it in (payload.get("inventory") or []) if len(it) >= 2]

    # 背景（随机 + 压暗）
    bg = _pick_background()
    if bg is None:
        img = Image.new("RGBA", (W, H), (32, 36, 46, 255))
    else:
        img = ImageEnhance.Brightness(bg).enhance(BG_BRIGHTNESS).convert("RGBA")
        img = Image.alpha_composite(img, Image.new("RGBA", (W, H), (0, 0, 0, BG_DARKEN_ALPHA)))

    # 半透明面板 + 全部格子 → 独立图层合成（避免 Pillow 直接覆盖式绘制）
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.rounded_rectangle(PANEL_BOX, radius=PANEL_RADIUS,
                            fill=(10, 14, 22, 152), outline=(255, 255, 255, 48), width=2)
    for _title, _icon, _idx, cols, rows, cell, sx, sy, _pos in GRID_SECTIONS:
        _cells_overlay(odraw, cols, rows, cell, sx, sy)
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # 标题区：玩家名 + 右侧信息
    name = str(payload.get("name") or "未知玩家")
    draw.text((LEFT_X, 78), name, font=_font(58, bold=True), fill=(255, 255, 255),
              stroke_width=3, stroke_fill=(0, 0, 0))
    sub = f"{server_name} · {time.strftime('%Y-%m-%d %H:%M:%S')}"
    if querier:
        sub += f" · 查询者：{querier}"
    font_sub = _font(26)
    tw = draw.textlength(sub, font=font_sub)
    draw.text((PANEL_BOX[2] - 40 - tw, 108), sub, font=font_sub, fill=(230, 237, 247),
              stroke_width=2, stroke_fill=(0, 0, 0))

    # 各分区：标题（图标代替文字）+ 格子（标题贴近所属分区）
    for title, icon, idx_list, cols, rows, cell, sx, sy, pos in GRID_SECTIONS:
        if pos == "left":
            _draw_section_label(img, draw, sx - 60, sy + 12, "", icon, 52)
        elif pos == "icon":
            _draw_section_label(img, draw, sx, sy - 60, "", icon, 57)
        else:
            _draw_section_label(img, draw, sx, sy - 60, title, icon, 57)
        _place_items(img, draw, idx_list, cols, rows, cell, sx, sy, inv)

    # 左列下半：增益 + 玩家信息
    _draw_buffs(img, draw, LEFT_X, 690, payload.get("buffs") or [])
    _draw_info(img, draw, LEFT_X, 752, payload)

    import io
    buf = io.BytesIO()
    # JPEG 比 PNG 小 4 倍以上，群里上传更快（卡片是不透明合成图，无透明通道需求）
    img.convert("RGB").save(buf, "JPEG", quality=88, optimize=True, progressive=True)
    return buf.getvalue()


# ─────────────────────────── 文字降级 ───────────────────────────
def build_text_summary(payload: dict, server_name: str = "") -> str:
    """图片渲染/上传失败时的文字降级：按分区列出非空物品（名称 × 数量）"""
    inv = [(int(it[0]), int(it[1])) for it in (payload.get("inventory") or []) if len(it) >= 2]
    lines = []
    total = 0
    for title, idx_list in SECTION_NAMES:
        parts = []
        for idx in idx_list:
            if idx >= len(inv):
                continue
            net_id, stack = inv[idx]
            if net_id == 0 or stack <= 0:
                continue
            total += 1
            nm = item_name(net_id)
            parts.append(f"{nm}×{stack}" if stack > 1 else nm)
        if parts:
            text = "、".join(parts)
            if len(text) > 240:
                text = text[:240] + "…"
            lines.append(f"- {title}：{text}")
    if not lines:
        lines.append("- 背包是空的喵...")
    head = f"**{payload.get('name') or '未知玩家'}** 的背包"
    if server_name:
        head += f"（{server_name}）"
    return head + "\n\n" + "\n".join(lines) + f"\n\n> 共 {total} 件物品"


if __name__ == "__main__":
    # 本地预览：python lookbag_render.py [输出路径]
    # 固定背景可用环境变量 LOOKBAG_BG_FILE=<背景文件名>
    import sys

    demo_ids = [3509, 5011, 3506, 8, 183, 706, 4444, 2589, 4808, 4956, 3330, 499, 188, 361,
                965, 307, 4934, 27, 28, 5, 1425, 602, 75, 38, 2544, 9, 593, 313, 43, 2,
                72, 71, 73, 40, 4779, 4780, 4781, 150, 3507, 3506]
    demo_inv = [[0, 0]] * 350
    for i, nid in enumerate(demo_ids[:50]):
        demo_inv[i] = [nid, 3 if i % 3 == 0 else 1]
    for i, nid in enumerate([71, 72, 73, 74]):
        demo_inv[50 + i] = [nid, 9999]
    for i in range(4):
        demo_inv[54 + i] = [40, 9999]
    demo_inv[179] = [2, 3]
    for i, nid in enumerate([3509, 5011, 3506, 8, 183, 706, 4444, 2589, 4808, 4956,
                             3330, 499, 188, 361, 965, 307, 4934, 27, 28, 5]):
        demo_inv[59 + i] = [nid, 1]
    for i in range(10):
        demo_inv[79 + i] = [3599, 1]
    for i, nid in enumerate([4823, 4824, 4825, 4826, 4952, 3599, 3599, 3599, 3599, 3599]):
        demo_inv[89 + i] = [nid, 1]
    for i in range(0, 40, 3):
        demo_inv[99 + i] = [2, 3]
    for i in range(0, 40, 5):
        demo_inv[139 + i] = [75, 84]
    for i in range(0, 40, 7):
        demo_inv[180 + i] = [428, 999]
    for i in range(0, 40, 4):
        demo_inv[220 + i] = [150, 1]
    for i in range(0, 30, 5):
        demo_inv[260 + i] = [950, 1]
    for i in range(0, 30, 3):
        demo_inv[290 + i] = [706, 10]
    for i in range(0, 30, 9):
        demo_inv[320 + i] = [2, 99]
    demo = {
        "is_text": False, "name": "星梦",
        "exist": True,
        "life": "500/500", "mana": "200/200", "quests_completed": 23,
        "inventory": demo_inv,
        "buffs": [1, 2, 3, 5, 6, 12, 14, 16],
        "enhances": [3335, 5043, 5326, 5337, 5338, 5339, 5340, 5341, 5342, 5289],
        "economic": {"Coins": "金币: 1234\n银币: 567", "LevelName": "职业: 战士", "Skill": "技能: 冲刺"},
    }
    out = sys.argv[1] if len(sys.argv) > 1 else "_lookbag_preview.png"
    png = render_lookbag_image(demo, server_name="泰拉瑞亚建筑群", querier="星梦")
    with open(out, "wb") as fp:
        fp.write(png)
    print(f"已生成 {out}（{len(png) // 1024} KB）")