# -*- coding: utf-8 -*-
"""进度卡渲染（Pillow）：进度查询卡 + 首次击杀播报卡

画布 = 随机背景原图尺寸（与 vote_render 一致，不裁剪不缩放；缺图兜底 1920×1084 纯色）。
以 1920×1084 为基准等比缩放 s = min(W/1920, H/1084)：
  进度查询卡：顶部「世界图标 + 世界名 + 进度」→ 左侧 7 事件列（入侵/月相事件）
              → 右侧 18 boss 6×3 网格（已击败显示次数 / 未击败 / 锁定显示锁图标+时间）
  播报卡：   居中大图：boss 图标 + 中文名 + 击杀时间 + 全部参与玩家 + 世界/服务器

素材（从 CaiBotLite 复制，GPL-3.0，不入库）：
  assets/progress/bosses/<英文key>.png    18 boss + 7 事件图标
  assets/progress/world_icon/Icon*.png    世界图标
  assets/progress/lock.png                锁图标（CaiBotLite Item_5328.png）
背景：与查背包共用（LOOKBAG_BG_DIR，默认 assets/lookbag/backgrounds）
"""

import os
import random
import re
import tempfile
from datetime import datetime

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

# ── 素材 / 字体路径（环境变量可覆盖，便于本地调试与自测） ──
_BASE_DIR = os.environ.get("PROGRESS_ASSETS_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "progress")
BOSSES_DIR = os.environ.get("PROGRESS_BOSSES_DIR") or os.path.join(_BASE_DIR, "bosses")
WORLD_ICON_DIR = os.environ.get("PROGRESS_WORLD_ICON_DIR") or os.path.join(_BASE_DIR, "world_icon")
LOCK_ICON = os.environ.get("PROGRESS_LOCK_ICON") or os.path.join(_BASE_DIR, "lock.png")
DEFAULT_BG_DIR = os.environ.get("LOOKBAG_BG_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "lookbag", "backgrounds")

FONT_REG = "C:/Windows/Fonts/msyh.ttc"       # 微软雅黑
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"    # 微软雅黑粗体

_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\uFE0E]")

# ── 基准尺寸（1920×1084 样卡；实际渲染全部 × s 缩放） ──
BASE_W, BASE_H = 1920, 1084
MARGIN = 48
TOP_H = 178            # 顶部标题区高度（世界图标 + 世界名 + 进度）
FOOT_H = 84            # 底部信息条高度
EVENT_W = 400          # 左侧事件列宽度
GAP = 24               # 事件列与 boss 网格间距
TITLE_FS = 64
SUB_FS = 32
EV_ICON = 64
EV_NAME_FS = 30
EV_STAT_FS = 26
BOSS_ICON = 122
BOSS_NAME_FS = 28
BOSS_STAT_FS = 26
LOCK_FS = 26

# ── 颜色 ──
TITLE_COLOR = (255, 255, 255)
TEXT = (240, 244, 252)
MUTED = (204, 212, 228)
DONE_COLOR = (255, 92, 92)        # 已击败（红，对深底更亮）
LOCK_COLOR = (255, 146, 60)       # 锁定时间（橙红）
PANEL_BG = (255, 255, 255, 16)
PANEL_LINE = (255, 255, 255, 42)
CELL_BG = (255, 255, 255, 12)
BG_FALLBACK = (34, 30, 48, 255)
BG_BRIGHTNESS = 0.72
BG_DARKEN_ALPHA = 118

_font_cache: dict = {}
_icon_cache: dict = {}
_missing: set = set()

# ── 18 boss（按游戏进度顺序；key 与插件 process / kill_counts / boss_lock 一致） ──
BOSSES = [
    ("King Slime", "史莱姆王"),
    ("Eye of Cthulhu", "克苏鲁之眼"),
    ("Eater of Worlds", "世界吞噬怪"),
    ("Brain of Cthulhu", "克苏鲁之脑"),
    ("Queen Bee", "蜂后"),
    ("Deerclops", "独眼巨鹿"),
    ("Skeletron", "骷髅王"),
    ("Wall of Flesh", "血肉墙"),
    ("Queen Slime", "史莱姆皇后"),
    ("The Destroyer", "毁灭者"),
    ("The Twins", "双子魔眼"),
    ("Skeletron Prime", "机械骷髅王"),
    ("Plantera", "世纪之花"),
    ("Golem", "石巨人"),
    ("Duke Fishron", "猪龙鱼公爵"),
    ("Empress of Light", "光之女皇"),
    ("Lunatic Cultist", "拜月教邪教徒"),
    ("Moon Lord", "月亮领主"),
]
BOSS_CN = {k: cn for k, cn in BOSSES}

# ── 左侧 7 事件（入侵 / 月相事件） ──
EVENTS = [
    ("Goblins", "哥布林军队"),
    ("Pirates", "海盗入侵"),
    ("Frost", "雪人军团"),
    ("Frost Moon", "霜月"),
    ("Pumpkin Moon", "南瓜月"),
    ("Pillars", "四柱"),
    ("Old Ones Army", "旧日军团"),
]

# 中文别名 → key（/进度提醒 支持直接输入中文别名或英文 key）
BOSS_ALIASES = {
    "史莱姆王": "King Slime", "史王": "King Slime",
    "克苏鲁之眼": "Eye of Cthulhu", "克眼": "Eye of Cthulhu",
    "世界吞噬怪": "Eater of Worlds", "世界吞噬者": "Eater of Worlds", "世吞": "Eater of Worlds",
    "克苏鲁之脑": "Brain of Cthulhu", "克脑": "Brain of Cthulhu",
    "蜂后": "Queen Bee", "蜂王": "Queen Bee",
    "独眼巨鹿": "Deerclops", "鹿角怪": "Deerclops",
    "骷髅王": "Skeletron",
    "血肉墙": "Wall of Flesh", "肉山": "Wall of Flesh",
    "史莱姆皇后": "Queen Slime", "史皇": "Queen Slime",
    "毁灭者": "The Destroyer", "铁长直": "The Destroyer",
    "双子魔眼": "The Twins", "双子": "The Twins",
    "机械骷髅王": "Skeletron Prime", "机械骷髅": "Skeletron Prime",
    "世纪之花": "Plantera", "世花": "Plantera",
    "石巨人": "Golem",
    "猪龙鱼公爵": "Duke Fishron", "猪鲨": "Duke Fishron",
    "光之女皇": "Empress of Light", "光女": "Empress of Light",
    "拜月教邪教徒": "Lunatic Cultist", "拜月教": "Lunatic Cultist", "拜月教徒": "Lunatic Cultist",
    "月亮领主": "Moon Lord", "月总": "Moon Lord", "月领主": "Moon Lord",
}


def resolve_boss(name: str):
    """boss 名 → 英文 key；支持中文别名 / 英文 key（大小写不敏感）。找不到返回 None"""
    text = _clean(name or "").replace(" ", "")
    if not text:
        return None
    if text in BOSS_ALIASES:
        return BOSS_ALIASES[text]
    low = text.lower()
    for key, _cn in BOSSES:
        if key.lower().replace(" ", "") == low:
            return key
    return None


def boss_cn(key: str) -> str:
    return BOSS_CN.get(key) or _clean(key or "未知boss")


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


def _clean(value) -> str:
    return _EMOJI_RE.sub("", str(value or "")).strip()


def _truncate(draw, text, font, max_w):
    text = str(text)
    if max_w <= 0:
        return ""
    if draw.textlength(text, font) <= max_w:
        return text
    ell = "…"
    out = ""
    for ch in text:
        if draw.textlength(out + ch + ell, font) > max_w:
            break
        out += ch
    return (out + ell) if out else ell


def _fit_size(draw, text, size_to_font, max_w, start, floor, bold=False, step=2):
    size = max(start, floor)
    while size > floor:
        if draw.textlength(text, size_to_font(size, bold)) <= max_w:
            return size
        size -= step
    return floor


def _load_icon(path: str):
    if path in _icon_cache:
        return _icon_cache[path]
    img = None
    if path not in _missing:
        try:
            img = Image.open(path).convert("RGBA")
        except (OSError, ValueError):
            img = None
            _missing.add(path)
    _icon_cache[path] = img
    return img


def _load_icon_sized(path: str, size: int, max_w: int = 0):
    """图标等比缩放（contain）进 宽×高 框内：宽 = max(0→size, max_w)，高 = size，绝不拉伸变形。
    max_w 用于长条素材（世界吞噬怪 170×19、毁灭者 170×21）横向展开，避免被压成小方块。"""
    key = (path, size, max_w)
    if key in _icon_cache:
        return _icon_cache[key]
    src = _load_icon(path)
    out = None
    box_w = max_w if max_w > 0 else size
    if src is not None:
        scale = min(box_w / src.width, size / src.height)
        w = max(1, int(round(src.width * scale)))
        h = max(1, int(round(src.height * scale)))
        out = src if (w == src.width and h == src.height) else src.resize((w, h), Image.LANCZOS)
    _icon_cache[key] = out
    return out


def _world_icon_path(name: str) -> str:
    """世界图标路径：精确名 → 去掉 Hallow → 基础腐化/猩红 → 兜底 IconCrimson"""
    name = str(name or "").strip()
    cands = [name, name.replace("Hallow", "")]
    cands.append("IconCorruption" if "Corruption" in name else "IconCrimson")
    cands.append("IconCrimson")
    for c in cands:
        if not c:
            continue
        p = os.path.join(WORLD_ICON_DIR, f"{c}.png")
        if os.path.exists(p):
            return p
    return ""


def _pick_bg(bg_dir, bg_file=None):
    if bg_file:
        path = bg_file
    else:
        d = bg_dir or DEFAULT_BG_DIR
        try:
            names = [f for f in os.listdir(d)
                     if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        except OSError:
            return None
        if not names:
            return None
        path = os.path.join(d, random.choice(sorted(names)))
    try:
        return Image.open(path).convert("RGB")
    except (OSError, ValueError):
        return None


def _base_canvas(bg, w, h):
    if bg is None:
        return Image.new("RGBA", (w, h), BG_FALLBACK)
    img = ImageEnhance.Brightness(bg).enhance(BG_BRIGHTNESS).convert("RGBA")
    return Image.alpha_composite(img, Image.new("RGBA", (w, h), (0, 0, 0, BG_DARKEN_ALPHA)))


def _paste(img, icon, x, y):
    if icon is None:
        return
    img.paste(icon, (int(x), int(y)), mask=icon.split()[3])


def _paste_box(img, icon, box_x, box_y, box_w, box_h):
    """把已等比缩放的图标居中放入 rect(box_x, box_y, box_w, box_h) 框内（图标不会超出框）"""
    if icon is None:
        return
    x = int(box_x + (box_w - icon.width) / 2)
    y = int(box_y + (box_h - icon.height) / 2)
    img.paste(icon, (x, y), mask=icon.split()[3])


def _wrap(text, draw, font, max_w, max_lines=3):
    """按字符贪心折行；超出 max_lines 时末行截断加省略号"""
    text = str(text)
    lines = []
    cur = ""
    for ch in text:
        if draw.textlength(cur + ch, font) <= max_w:
            cur += ch
            continue
        lines.append(cur)
        cur = ch
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if not lines:
        lines = [""]
    # 还有剩余内容 → 末行截断
    consumed = "".join(lines)
    if len(consumed) < len(text):
        lines[-1] = _truncate(draw, lines[-1] + text[len(consumed):], font, max_w)
    return lines


# ─────────────────────────── 进度查询卡 ───────────────────────────
def render_progress_card(payload: dict, server_name: str = "", querier: str = "",
                         bg_dir=None, bg_file=None) -> bytes:
    """渲染进度查询卡，返回 PNG 字节。payload 为插件 progress 回包。"""
    payload = payload or {}
    process = payload.get("process") or {}
    kill_counts = payload.get("kill_counts") or {}
    boss_lock = payload.get("boss_lock") or {}
    world_name = _clean(payload.get("world_name") or "未知世界")

    bg = _pick_bg(bg_dir, bg_file)
    W, H = bg.size if bg is not None else (BASE_W, BASE_H)
    img = _base_canvas(bg, W, H)
    s = min(W / BASE_W, H / BASE_H)

    def px(v):
        return max(1, int(round(v * s)))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    # ── 顶部：世界图标 + 世界名 + 进度 ──
    title_font = _font(px(TITLE_FS), True)
    if measure.textlength(world_name, title_font) > W - px(560):
        title_size = _fit_size(measure, world_name, _font, W - px(560), px(TITLE_FS), px(36), bold=True)
        title_font = _font(title_size, True)
        if measure.textlength(world_name, title_font) > W - px(560):
            world_name = _truncate(measure, world_name, title_font, W - px(560))
    tw = int(measure.textlength(world_name, title_font))
    title_x = (W - tw) // 2
    title_y = px(30)

    icon_size = px(130)
    wicon = _load_icon_sized(_world_icon_path(payload.get("world_icon") or ""), icon_size)
    _paste_box(img, wicon, title_x - icon_size - px(16), title_y - px(18), icon_size, icon_size)

    sub_font = _font(px(SUB_FS), True)
    sub_text = "进度"
    sw = int(measure.textlength(sub_text, sub_font))
    sub_y = title_y + int(title_font.size * 1.18)
    tags = []
    if payload.get("zenith_world"):
        tags.append("天顶世界")
    if payload.get("drunk_world"):
        tags.append("醉酒世界")
    sub_line = sub_text if not tags else f"{sub_text} · {' · '.join(tags)}"
    sw = int(measure.textlength(sub_line, sub_font))
    od.rounded_rectangle((W // 2 - sw // 2 - px(24), sub_y - px(6),
                          W // 2 + sw // 2 + px(24), sub_y + px(SUB_FS) + px(12)),
                         radius=px(14), fill=PANEL_BG, outline=PANEL_LINE, width=px(2))

    # ── 版面区域 ──
    body_y = px(TOP_H)
    body_h = H - body_y - px(FOOT_H)
    ev_x = px(MARGIN)
    ev_w = px(EVENT_W)
    grid_x = ev_x + ev_w + px(GAP)
    grid_w = W - grid_x - px(MARGIN)

    # 事件列面板 + 行底
    ev_rows = []
    row_h = body_h // len(EVENTS)
    for i, (key, cn) in enumerate(EVENTS):
        ry = body_y + i * row_h
        ev_rows.append((key, cn, ry, row_h))
        od.rounded_rectangle((ev_x, ry + px(4), ev_x + ev_w, ry + row_h - px(4)),
                             radius=px(14), fill=CELL_BG)

    # boss 网格（6 列 × 3 行）
    cols, rows_n = 6, 3
    cell_w = grid_w / cols
    cell_h = body_h / rows_n
    cells = []
    for i, (key, cn) in enumerate(BOSSES):
        cx = grid_x + (i % cols) * cell_w
        cy = body_y + (i // cols) * cell_h
        cells.append((key, cn, cx, cy))
        od.rounded_rectangle((cx + px(5), cy + px(5), cx + cell_w - px(5), cy + cell_h - px(5)),
                             radius=px(14), fill=CELL_BG)

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # 顶部文字
    draw.text((title_x, title_y), world_name, font=title_font, fill=TITLE_COLOR)
    draw.text(((W - sw) // 2, sub_y), sub_line, font=sub_font, fill=TEXT)

    # ── 事件行 ──
    ev_name_font = _font(px(EV_NAME_FS), True)
    ev_stat_font = _font(px(EV_STAT_FS))
    ev_icon_size = px(EV_ICON)
    for key, cn, ry, rh in ev_rows:
        ic = _load_icon_sized(os.path.join(BOSSES_DIR, f"{key}.png"), ev_icon_size)
        iy = ry + (rh - ev_icon_size) // 2
        _paste_box(img, ic, ev_x + px(16), iy, ev_icon_size, ev_icon_size)
        name_x = ev_x + px(16) + ev_icon_size + px(14)
        name_y = ry + rh // 2
        draw.text((name_x, name_y), cn, font=ev_name_font, fill=TEXT, anchor="lm")

        if key == "Old Ones Army":
            t3, t2, t1 = bool(process.get("DD2InvasionT3")), bool(process.get("DD2InvasionT2")), bool(process.get("DD2InvasionT1"))
            if t3:
                stat, color = "T3", DONE_COLOR
            elif t2:
                stat, color = "T2", LOCK_COLOR
            elif t1:
                stat, color = "T1", LOCK_COLOR
            else:
                stat, color = "未击败", MUTED
        else:
            ok = bool(process.get(key))
            stat, color = ("已击败", DONE_COLOR) if ok else ("未击败", MUTED)
        draw.text((ev_x + ev_w - px(18), name_y), stat, font=ev_stat_font,
                  fill=color, anchor="rm")

    # ── boss 网格 ──
    name_font = _font(px(BOSS_NAME_FS), True)
    stat_font = _font(px(BOSS_STAT_FS))
    lock_font = _font(px(LOCK_FS), True)
    lock_ic = None
    for key, cn, cx, cy in cells:
        cw = cell_w - px(10)
        icon_size = min(px(BOSS_ICON), int(cell_h - px(BOSS_NAME_FS) - px(BOSS_STAT_FS) - px(40)))
        icon_size = max(icon_size, px(56))
        # 图标按生效高度 icon_size 等比 contain（横向最多到单元格内宽，长条 boss 不被压成方块）
        ic = _load_icon_sized(os.path.join(BOSSES_DIR, f"{key}.png"), icon_size, max_w=int(cw))
        ix = cx + (cell_w - cw) // 2
        iy = cy + px(14)
        _paste_box(img, ic, ix, iy, cw, icon_size)
        nx = cx + cell_w // 2
        ny = iy + icon_size + px(6)  # 名字固定在图标框下方：图标再高（如双子魔眼 122×200）也不会压住名字
        cname = cn
        nfont = name_font
        if measure.textlength(cname, nfont) > cw:
            nfont = _font(_fit_size(measure, cname, _font, cw, px(BOSS_NAME_FS), px(20), bold=True), True)
            if measure.textlength(cname, nfont) > cw:
                cname = _truncate(measure, cname, nfont, cw)
        draw.text((nx, ny), cname, font=nfont, fill=TITLE_COLOR, anchor="ma")
        sy = ny + int(nfont.size * 1.2)

        lock_time = boss_lock.get(key)
        if lock_time:
            if lock_ic is None:
                lock_ic = _load_icon_sized(LOCK_ICON, px(28))
            text = _clean(lock_time)
            # 解锁时间来自 BossLock 等外部插件，长度不可控：先降字号、再截断，整行夹在单元格内
            lock_icon_w = px(30) if lock_ic is not None else 0
            lock_max_w = cw - lock_icon_w - px(10)
            lfont = lock_font
            if measure.textlength(text, lfont) > lock_max_w:
                lfont = _font(_fit_size(measure, text, _font, lock_max_w, px(LOCK_FS), px(18), bold=True), True)
                if measure.textlength(text, lfont) > lock_max_w:
                    text = _truncate(measure, text, lfont, lock_max_w)
            total = lock_icon_w + int(measure.textlength(text, lfont))
            lx = max(cx + px(8), min(nx - total // 2, cx + cell_w - px(8) - total))
            if lock_ic is not None:
                _paste(img, lock_ic, lx, sy + px(4))
            draw.text((lx + lock_icon_w, sy), text, font=lfont, fill=LOCK_COLOR, anchor="la")
        elif process.get(key):
            try:
                cnt = int(kill_counts.get(key) or 0)
            except (TypeError, ValueError):
                cnt = 0  # 击杀次数来自插件回包，非数字时按 0 处理，避免整卡渲染失败
            stat = f"已击败（{cnt}次）" if cnt > 0 else "已击败"
            # 击杀次数可能随赛季增长变宽：同样限宽降字号→截断
            sfont = stat_font
            if measure.textlength(stat, sfont) > cw:
                sfont = _font(_fit_size(measure, stat, _font, cw, px(BOSS_STAT_FS), px(18)))
                if measure.textlength(stat, sfont) > cw:
                    stat = _truncate(measure, stat, sfont, cw)
            draw.text((nx, sy), stat, font=sfont, fill=DONE_COLOR, anchor="ma")
        else:
            draw.text((nx, sy), "未击败", font=stat_font, fill=MUTED, anchor="ma")

    # ── 底部信息条 ──
    foot_y = H - px(FOOT_H // 2) - px(10)
    foot_font = _font(px(28))
    import time as _t
    foot = server_name or ""
    if querier:
        foot += f" · 查询者：{querier}" if foot else f"查询者：{querier}"
    foot_time = _t.strftime("%Y-%m-%d %H:%M:%S")
    foot = (foot + " · " if foot else "") + foot_time
    foot_max_w = W - px(96)
    if measure.textlength(foot, foot_font) > foot_max_w:
        foot_font = _font(_fit_size(measure, foot, _font, foot_max_w, px(28), px(18)))
        if measure.textlength(foot, foot_font) > foot_max_w:
            foot = _truncate(measure, foot, foot_font, foot_max_w)
    fw = int(measure.textlength(foot, foot_font))
    draw.text(((W - fw) // 2, foot_y), foot, font=foot_font, fill=MUTED)

    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


# ─────────────────────────── 首次击杀播报卡 ───────────────────────────
def render_notify_card(boss_key: str, players=None, kill_time: str = "",
                       world_name: str = "", server_name: str = "",
                       bg_dir=None, bg_file=None) -> bytes:
    """渲染首次击杀播报卡，返回 PNG 字节。

    boss_key：英文 key（BOSSES 内）；players：参与玩家名列表（可为空）；
    kill_time：插件时间串（原样展示）；world_name / server_name：展示用。
    """
    bg = _pick_bg(bg_dir, bg_file)
    W, H = bg.size if bg is not None else (BASE_W, BASE_H)
    img = _base_canvas(bg, W, H)
    s = min(W / BASE_W, H / BASE_H)

    def px(v):
        return max(1, int(round(v * s)))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    cx = W // 2

    # 顶部徽章：首次击杀播报
    badge_font = _font(px(34), True)
    badge_text = "首杀播报"
    bw = int(measure.textlength(badge_text, badge_font)) + px(56)
    badge_y = px(52)
    od.rounded_rectangle((cx - bw // 2, badge_y, cx + bw // 2, badge_y + px(64)),
                         radius=px(16), fill=(176, 66, 74, 235))

    # boss 图标（等比 contain；长条 boss 允许横向展开，不拉伸变形）
    icon_h = px(260)
    icon_box_w = px(560)
    ic = _load_icon_sized(os.path.join(BOSSES_DIR, f"{boss_key}.png"), icon_h, max_w=icon_box_w)
    icon_y = badge_y + px(64) + px(46)
    _paste_box(img, ic, cx - icon_box_w // 2, icon_y, icon_box_w, icon_h)

    # 中文名 + 英文 key
    cn = boss_cn(boss_key)
    name_font = _font(px(72), True)
    if measure.textlength(cn, name_font) > W - px(400):
        name_size = _fit_size(measure, cn, _font, W - px(400), px(72), px(40), bold=True)
        name_font = _font(name_size, True)
    name_y = icon_y + icon_h + px(26)

    # 信息面板：击杀时间 / 击杀玩家 / 世界
    lines = [f"击杀时间：{_clean(kill_time) or '未知'}"]
    player_text = "、".join(_clean(p) for p in (players or []) if _clean(p)) or "未知"
    lines.append(f"击杀玩家：{player_text}")
    if world_name:
        wline = f"世界：{_clean(world_name)}"
        if server_name:
            wline += f"（{_clean(server_name)}）"
        lines.append(wline)

    body_font = _font(px(36))
    inner_w = W - px(560) - px(48)  # 面板内文字两侧留白，避免贴边/越界
    wrapped = []
    for ln in lines:
        wrapped += _wrap(ln, measure, body_font, inner_w, max_lines=3)
    lh = int(body_font.size * 1.5)
    panel_h = px(40) * 2 + lh * len(wrapped)
    panel_y = name_y + int(name_font.size * 1.3) + px(26)
    od.rounded_rectangle((px(280), panel_y, W - px(280), panel_y + panel_h),
                         radius=px(20), fill=(10, 14, 22, 152), outline=PANEL_LINE, width=px(2))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    draw.text((cx, badge_y + px(32)), badge_text, font=badge_font, fill=(255, 255, 255), anchor="mm")
    draw.text((cx, name_y), cn, font=name_font, fill=TITLE_COLOR, anchor="ma")
    ty = panel_y + px(40)
    for ln in wrapped:
        draw.text((cx, ty), ln, font=body_font, fill=TEXT, anchor="ma")
        ty += lh
    key_font = _font(px(26))
    draw.text((cx, panel_y + panel_h + px(16)), boss_key, font=key_font, fill=MUTED, anchor="ma")

    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


# ─────────────────────────── 解锁提醒卡 ───────────────────────────
def format_unlock_time(ts) -> str:
    """解锁时间戳 → 展示文本（今天/明天/后天HH:mm；更远显示 M月d日 HH:mm）"""
    try:
        dt = datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        return "未知"
    delta = (dt.date() - datetime.now().date()).days
    hm = dt.strftime("%H:%M")
    if delta == 0:
        return f"今天{hm}"
    if delta == 1:
        return f"明天{hm}"
    if delta == 2:
        return f"后天{hm}"
    return f"{dt.month}月{dt.day}日 {hm}"


def render_unlock_card(boss_key: str, minutes_left: int = 30, unlock_ts: int = 0,
                       server_name: str = "", bg_dir=None, bg_file=None) -> bytes:
    """渲染解锁提醒卡（背景沿用进度卡同源），返回 PNG 字节。

    boss_key：英文 key（BOSSES 内）；minutes_left：发送时刻剩余分钟数；
    unlock_ts：绝对解锁时间戳（Unix 秒，0 时不显示解锁时间）；server_name：展示用。
    """
    bg = _pick_bg(bg_dir, bg_file)
    W, H = bg.size if bg is not None else (BASE_W, BASE_H)
    img = _base_canvas(bg, W, H)
    s = min(W / BASE_W, H / BASE_H)

    def px(v):
        return max(1, int(round(v * s)))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    cx = W // 2

    # 顶部徽章：解锁提醒
    badge_font = _font(px(34), True)
    badge_text = "解锁提醒"
    bw = int(measure.textlength(badge_text, badge_font)) + px(56)
    badge_y = px(52)
    od.rounded_rectangle((cx - bw // 2, badge_y, cx + bw // 2, badge_y + px(64)),
                         radius=px(16), fill=(176, 112, 42, 235))

    # boss 图标（等比 contain；长条 boss 允许横向展开，不拉伸变形）
    icon_h = px(260)
    icon_box_w = px(560)
    ic = _load_icon_sized(os.path.join(BOSSES_DIR, f"{boss_key}.png"), icon_h, max_w=icon_box_w)
    icon_y = badge_y + px(64) + px(46)
    _paste_box(img, ic, cx - icon_box_w // 2, icon_y, icon_box_w, icon_h)
    # 左下角叠加锁图标（强调"尚未解锁"）
    lock = _load_icon_sized(LOCK_ICON, px(72))
    if lock is not None:
        _paste(img, lock, cx - icon_box_w // 2 + px(24), icon_y + icon_h - px(84))

    # 中文名
    cn = boss_cn(boss_key)
    name_font = _font(px(72), True)
    if measure.textlength(cn, name_font) > W - px(400):
        name_size = _fit_size(measure, cn, _font, W - px(400), px(72), px(40), bold=True)
        name_font = _font(name_size, True)
    name_y = icon_y + icon_h + px(26)

    # 信息面板：倒计时 / 解锁时间 / 服务器
    lines = [f"距离解锁还有 {max(1, int(minutes_left or 1))} 分钟"]
    if unlock_ts:
        lines.append(f"解锁时间：{format_unlock_time(unlock_ts)}")
    if server_name:
        lines.append(f"服务器：{_clean(server_name)}")

    body_font = _font(px(36))
    inner_w = W - px(560) - px(48)  # 面板内文字两侧留白，避免贴边/越界
    wrapped = []  # [(文本段, 是否首行)]
    for i, ln in enumerate(lines):
        for seg in _wrap(ln, measure, body_font, inner_w, max_lines=3):
            wrapped.append((seg, i == 0))
    lh = int(body_font.size * 1.5)
    panel_h = px(40) * 2 + lh * len(wrapped)
    panel_y = name_y + int(name_font.size * 1.3) + px(26)
    od.rounded_rectangle((px(280), panel_y, W - px(280), panel_y + panel_h),
                         radius=px(20), fill=(10, 14, 22, 152), outline=PANEL_LINE, width=px(2))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    draw.text((cx, badge_y + px(32)), badge_text, font=badge_font, fill=(255, 255, 255), anchor="mm")
    draw.text((cx, name_y), cn, font=name_font, fill=TITLE_COLOR, anchor="ma")
    ty = panel_y + px(40)
    for seg, is_first in wrapped:
        draw.text((cx, ty), seg, font=body_font,
                  fill=(LOCK_COLOR if is_first else TEXT), anchor="ma")
        ty += lh
    key_font = _font(px(26))
    draw.text((cx, panel_y + panel_h + px(16)), boss_key, font=key_font, fill=MUTED, anchor="ma")

    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


# ─────────────────────────── 自测 ───────────────────────────
def _sample_payload():
    keys = [k for k, _ in BOSSES]
    process = {k: False for k in [
        "King Slime", "Eye of Cthulhu", "Eater of Worlds or Brain of Cthulhu", "Eater of Worlds",
        "Brain of Cthulhu", "Queen Bee", "Deerclops", "Skeletron", "Wall of Flesh", "Queen Slime",
        "The Destroyer", "The Twins", "Skeletron Prime", "Plantera", "Golem", "Duke Fishron",
        "Empress of Light", "Lunatic Cultist", "Moon Lord", "Pillars",
        "Tower Solar", "Tower Nebula", "Tower Vortex", "Tower Stardust",
        "Goblins", "Pirates", "Frost", "Frost Moon", "Pumpkin Moon", "Martians",
        "DD2InvasionT1", "DD2InvasionT2", "DD2InvasionT3",
    ]}
    for k in keys[:9]:
        process[k] = True
    process["DD2InvasionT2"] = True
    process["Pillars"] = False
    process["Goblins"] = True
    kill_counts = {k: (i + 1) * 3 for i, k in enumerate(keys[:9])}
    kill_counts[keys[0]] = 123456  # 自测：超大击杀次数（验证限宽降字号/截断）
    # 自测：超长解锁时间（模拟 BossLock 类插件返回长文本）
    boss_lock = {keys[12]: "2026年12月31日 23:59:59", keys[16]: "下周三11:44", keys[17]: "即将解锁"}
    return {
        "process": process,
        "kill_counts": kill_counts,
        "boss_lock": boss_lock,
        "world_name": "泰拉瑞亚建筑群 · 开荒档第一赛季",
        "drunk_world": False,
        "zenith_world": False,
        "world_icon": "IconHallowCrimson",
    }


if __name__ == "__main__":
    out_dir = tempfile.mkdtemp(prefix="progress_render_test_")
    payload = _sample_payload()
    bg_dir = DEFAULT_BG_DIR
    try:
        names = sorted(f for f in os.listdir(bg_dir)
                       if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
    except OSError:
        names = []
    if not names:
        print(f"[warn] 背景目录为空或不存在：{bg_dir}（仅验证纯色兜底）")
        names = [None]
    for i, nm in enumerate(names[:6]):
        p = os.path.join(out_dir, f"{i:02d}_progress.png")
        with open(p, "wb") as f:
            f.write(render_progress_card(payload, server_name="泰拉瑞亚建筑群", querier="星梦",
                                         bg_file=os.path.join(bg_dir, nm) if nm else None))
        with Image.open(p) as im:
            print(f"{p}  {im.width}x{im.height}")
    for i, nm in enumerate(names[:3]):
        p = os.path.join(out_dir, f"{i:02d}_notify.png")
        with open(p, "wb") as f:
            f.write(render_notify_card(
                "Moon Lord", players=["星梦", "大肥鱼🐳", "帕秋莉Bot", "七青", "小触须团子十号"],
                kill_time="2026-10-03 21:34:56", world_name="泰拉瑞亚建筑群 · 开荒档第一赛季",
                server_name="泰拉瑞亚建筑群",
                bg_file=os.path.join(bg_dir, nm) if nm else None))
        with Image.open(p) as im:
            print(f"{p}  {im.width}x{im.height}")
    for i, nm in enumerate(names[:3]):
        p = os.path.join(out_dir, f"{i:02d}_unlock.png")
        with open(p, "wb") as f:
            f.write(render_unlock_card(
                "Moon Lord", minutes_left=30,
                unlock_ts=int(datetime.now().timestamp()) + 1800,
                server_name="泰拉瑞亚建筑群",
                bg_file=os.path.join(bg_dir, nm) if nm else None))
        with Image.open(p) as im:
            print(f"{p}  {im.width}x{im.height}")
    print("format_unlock_time:", format_unlock_time(int(datetime.now().timestamp()) + 1800))
    print("输出目录:", out_dir)