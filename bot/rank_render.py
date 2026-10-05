# -*- coding: utf-8 -*-
"""排行榜卡渲染（Pillow）

画布 = 随机竖屏背景**原图尺寸**（与进度卡/投票卡一致，不裁剪不缩放；缺图兜底 1080×1528 纯色），
排版按 s = min(W/BASE_W, H/BASE_H) 等比缩放，BASE = 1080×1528（竖屏基准）。

观感与 progress_render / vote_render 完全对齐：
  · 半透明面板一律画在**独立 overlay 层**再 alpha_composite 回去。
    （直接在 RGBA 画布上 draw 不会做 alpha 混合，convert("RGB") 又会丢掉 alpha 通道 →
      面板会变成纯白/纯色不透明块，这是实测踩过的坑）
  · 字体/配色沿用同一套（msyh/msyhbd、TEXT/MUTED/TITLE_COLOR/ACCENT、背景 0.72 亮度 + 118 alpha 压暗）。

防重叠 / 防越界的硬约束：
  1. 所有文本经 _truncate 按「列宽」像素截断，绝不横向溢出；
  2. 行高 = min(ROW_H_MAX, max(ROW_H_MIN, 可用高度 // 行数))，字号再按 行高×0.46 收窄 → 纵向不会压行；
  3. 表头/行区/页脚 y 全部由已算高度推算，整表在可用区内垂直居中；
  4. 文本一律用 anchor（lm/rm/mm）定位，避免不同字号的 bbox 偏移错位。

素材：assets/rank/backgrounds（环境变量 RANK_BG_DIR 可覆盖）；目录缺失/为空时回落查背包背景目录。
"""

import os
import random

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

# ── 背景目录 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
RANK_BG_DIR = os.environ.get("RANK_BG_DIR") or os.path.join(_HERE, "assets", "rank", "backgrounds")
LOOKBAG_BG_DIR = os.environ.get("LOOKBAG_BG_DIR") or os.path.join(
    _HERE, "assets", "lookbag", "backgrounds")

FONT_REG = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

# ── 竖屏基准尺寸 ──
BASE_W, BASE_H = 1080, 1528
MARGIN = 56
TITLE_FS = 76
SUB_FS = 34
PAGE_FS = 32
HEAD_FS = 32
NAME_FS = 40
VAL_FS = 36
FOOT_FS = 28
ROW_H_MAX = 112
ROW_H_MIN = 64
HEADER_H = 62
FOOT_H = 72
ACCENT_W = 10
COL_RANK_W = 104
COL_GAP = 20
COL_NAME_W = 500
BG_MAX_SIDE = 1400     # 画布长边上限（竖屏原图动辄 3000+px，直接做画布会让 PNG 到 10MB+）

# ── 颜色（与 progress_render 同一套观感）──
TEXT = (240, 244, 252)
MUTED = (204, 212, 228)
TITLE_COLOR = (255, 255, 255)
ACCENT = (120, 200, 255)
ROW_BG = (255, 255, 255, 16)
ROW_BG_ALT = (255, 255, 255, 8)
ROW_LINE = (255, 255, 255, 30)
HEAD_BG = (255, 255, 255, 22)
HEAD_LINE = (255, 255, 255, 48)
BADGE_BG = (255, 255, 255, 26)
MEDALS = {1: (255, 208, 84), 2: (216, 224, 240), 3: (228, 160, 102)}   # 前三名文字色
MEDAL_GLOW = {1: (255, 208, 84, 40), 2: (216, 224, 240, 32), 3: (228, 160, 102, 36)}
BG_FALLBACK = (34, 30, 48, 255)
BG_BRIGHTNESS = 0.72
BG_DARKEN_ALPHA = 118

_font_cache = {}


def _font(size: int, bold: bool = False):
    size = max(10, int(size))
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


def _truncate(draw, text, font, max_w):
    """按像素宽度截断并加省略号（横向越界的唯一防线）"""
    text = str(text or "")
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


def _pick_bg(bg_dir=None, bg_file=None):
    """随机取一张背景：显式文件 > 指定目录 > RANK_BG_DIR > 查背包背景目录"""
    path = None
    if bg_file:
        path = bg_file
    else:
        for d in (bg_dir, RANK_BG_DIR, LOOKBAG_BG_DIR):
            if not d:
                continue
            try:
                names = [f for f in os.listdir(d)
                         if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
            except OSError:
                continue
            if names:
                path = os.path.join(d, random.choice(sorted(names)))
                break
    if not path:
        return None
    try:
        return Image.open(path).convert("RGB")
    except (OSError, ValueError):
        return None


def _base_canvas(bg, w, h):
    if bg is None:
        return Image.new("RGBA", (w, h), BG_FALLBACK)
    img = ImageEnhance.Brightness(bg).enhance(BG_BRIGHTNESS).convert("RGBA")
    return Image.alpha_composite(img, Image.new("RGBA", (w, h), (0, 0, 0, BG_DARKEN_ALPHA)))


def render_rank_card(rank_lines: dict, page: int = 1, page_size: int = 10,
                     title: str = "排行", server_name: str = "", querier: str = "",
                     bg_dir=None, bg_file=None) -> tuple:
    """渲染排行榜图片卡。

    返回 (png_bytes, page, total_pages)：page/total_pages 为**实际生效**的页码信息，
    调用方据此生成翻页按钮（保证按钮页码与图片一致）。
    """
    items = list((rank_lines or {}).items())
    total = len(items)
    page_size = max(1, int(page_size))
    total_pages = max(1, (total + page_size - 1) // page_size)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(page, total_pages))

    bg = _pick_bg(bg_dir, bg_file)
    if bg is not None and max(bg.size) > BG_MAX_SIDE:
        r = BG_MAX_SIDE / max(bg.size)
        bg = bg.resize((max(1, round(bg.width * r)), max(1, round(bg.height * r))),
                       Image.LANCZOS)
    W, H = bg.size if bg is not None else (BASE_W, BASE_H)
    s = min(W / BASE_W, H / BASE_H)
    img = _base_canvas(bg, W, H)

    # 半透明面板画在 overlay 上再合成（在 RGBA 画布上直接画不会混合 alpha）
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def px(v):
        return max(1, int(round(v * s)))

    margin = px(MARGIN)
    usable_w = W - margin * 2
    rank_cx = margin + px(COL_RANK_W) // 2
    name_x = margin + px(COL_RANK_W + COL_GAP)
    val_right = margin + usable_w
    name_w = min(px(COL_NAME_W), max(px(60), val_right - name_x - px(COL_GAP) - px(90)))
    texts = []  # (xy, text, font, fill, anchor) —— 合成之后统一绘制

    # ── 标题区 ──
    y = margin
    bar_h = px(TITLE_FS)
    od.rectangle((margin, y + px(6), margin + px(ACCENT_W), y + px(6) + bar_h), fill=ACCENT)
    title_font = _font(px(TITLE_FS), bold=True)
    page_font = _font(px(PAGE_FS), bold=True)
    page_txt = f"{page} / {total_pages}"
    pw = measure.textlength(page_txt, font=page_font) + px(36)
    ph = px(PAGE_FS) + px(22)
    title_x = margin + px(ACCENT_W) + px(24)
    title_txt = _truncate(measure, title or "排行", title_font,
                          max(px(80), val_right - title_x - pw - px(24)))
    by0 = y + px(6) + (bar_h - ph) // 2
    od.rounded_rectangle((val_right - pw, by0, val_right, by0 + ph), radius=px(16),
                         fill=HEAD_BG, outline=HEAD_LINE, width=max(1, px(1)))
    texts.append(((title_x, y + px(6) + bar_h // 2), title_txt, title_font, TITLE_COLOR, "lm"))
    texts.append(((val_right - pw / 2, by0 + ph // 2), page_txt, page_font, TEXT, "mm"))
    sub_parts = [p for p in (server_name or "", f"共 {total} 条") if p]
    if querier:
        sub_parts.insert(1 if server_name else 0, f"查询者：{querier}")
    sub_font = _font(px(SUB_FS))
    sub_txt = _truncate(measure, "｜".join(sub_parts), sub_font, max(px(60), usable_w))
    texts.append(((title_x, y + px(6) + bar_h + px(18)), sub_txt, sub_font, MUTED, "lm"))

    # ── 表头 + 行区（整表在可用高度内垂直居中；行高自适应、字号随行高收窄）──
    table_top = y + px(6) + bar_h + px(18) + px(SUB_FS) + px(30)
    header_h = px(HEADER_H)
    foot_line = H - margin - px(FOOT_H)
    total_avail = foot_line - px(12) - table_top
    hint = None
    if total == 0:
        hint = ((W // 2, (table_top + foot_line) // 2), "该排行榜暂无数据喵",
                _font(px(40), bold=True))
    else:
        page_items = items[(page - 1) * page_size: page * page_size]
        n = len(page_items)
        gap = px(10)
        row_h = max(1, min(px(ROW_H_MAX),
                           max(px(ROW_H_MIN),
                               (total_avail - header_h - gap) // max(1, n))))
        block_h = header_h + gap + n * row_h
        head_top = table_top + max(0, (total_avail - block_h) // 2)
        rows_top = head_top + header_h + gap
        head_font = _font(min(px(HEAD_FS), int(row_h * 0.40)), bold=True)
        od.rounded_rectangle((margin, head_top, margin + usable_w, head_top + header_h),
                             radius=px(14), fill=HEAD_BG, outline=HEAD_LINE,
                             width=max(1, px(1)))
        head_cy = head_top + header_h // 2
        texts.append(((rank_cx, head_cy), "排名", head_font, MUTED, "mm"))
        texts.append(((name_x, head_cy), "名字", head_font, MUTED, "lm"))
        texts.append(((val_right - px(8), head_cy), "项目", head_font, MUTED, "rm"))
        name_fs = min(px(NAME_FS), int(row_h * 0.46))
        val_fs = min(px(VAL_FS), int(row_h * 0.42))
        name_font = _font(name_fs, bold=False)
        name_font_b = _font(name_fs, bold=True)
        val_font = _font(val_fs)
        rank_font = _font(min(px(NAME_FS), int(row_h * 0.44)), bold=True)
        for i, (nm, val) in enumerate(page_items):
            disp_rank = (page - 1) * page_size + i + 1
            ry = rows_top + i * row_h
            rh = max(px(28), row_h - px(8))
            box = (margin, ry, margin + usable_w, ry + rh)
            top3 = disp_rank in MEDALS
            # 半透明行底（透出背景，与其它卡片一致）；前三名叠一层淡奖牌色
            od.rounded_rectangle(box, radius=px(14),
                                 fill=ROW_BG if i % 2 == 0 else ROW_BG_ALT,
                                 outline=ROW_LINE, width=max(1, px(1)))
            if top3:
                od.rounded_rectangle(box, radius=px(14), fill=MEDAL_GLOW[disp_rank])
            bw = min(px(56), rh - px(16))
            bx = rank_cx - bw // 2
            by = ry + (rh - bw) // 2
            od.rounded_rectangle((bx, by, bx + bw, by + bw), radius=max(2, bw // 4),
                                 fill=BADGE_BG, outline=HEAD_LINE, width=max(1, px(1)))
            cy = ry + rh // 2
            nf = name_font_b if top3 else name_font
            texts.append(((rank_cx, by + bw // 2), str(disp_rank), rank_font,
                          MEDALS.get(disp_rank, TEXT), "mm"))
            texts.append(((name_x, cy),
                          _truncate(measure, nm, nf, name_w), nf,
                          MEDALS[disp_rank] if top3 else TEXT, "lm"))
            texts.append(((val_right - px(8), cy),
                          _truncate(measure, val, val_font,
                                    max(px(40), val_right - px(8) - (name_x + name_w) - px(COL_GAP))),
                          val_font, TEXT if top3 else MUTED, "rm"))

    # ── 合成面板 + 绘制文字 ──
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)
    if hint:
        draw.text(hint[0], hint[1], font=hint[2], fill=MUTED, anchor="mm")
    for xy, txt, font, fill, anchor in texts:
        draw.text(xy, txt, font=font, fill=fill, anchor=anchor)

    # ── 页脚 ──
    foot_font = _font(px(FOOT_FS))
    foot_y = H - margin - px(FOOT_FS) // 2 - px(4)
    left = f"第 {page} / {total_pages} 页｜每页 {page_size} 条｜共 {total} 条"
    draw.text((margin, foot_y), _truncate(measure, left, foot_font, usable_w * 0.7),
              font=foot_font, fill=MUTED, anchor="lm")
    right = _truncate(measure, server_name or "", foot_font, usable_w * 0.28)
    if right:
        draw.text((margin + usable_w, foot_y), right, font=foot_font, fill=MUTED, anchor="rm")

    import io
    buf = io.BytesIO()
    # JPEG 比 PNG 小 4 倍以上，群里上传更快（卡片是不透明合成图，无透明通道需求）
    img.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True, progressive=True)
    return buf.getvalue(), page, total_pages


if __name__ == "__main__":
    import sys
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    bg_dir = sys.argv[2] if len(sys.argv) > 2 else None
    demo = {f"玩家{i:02d}": f"{120 - i * 3}次" for i in range(23)}
    for name, lines, page in (("rank_p1", demo, 1), ("rank_p3", demo, 3),
                              ("rank_long", {"这是一个非常非常长的玩家名字用来测试截断效果": "12345分钟",
                                             "短名": "9分钟"}, 1),
                              ("rank_empty", {}, 1)):
        data, p, tp = render_rank_card(lines, page=page, title="死亡排行",
                                       server_name="星梦的开荒服", querier="星梦",
                                       bg_dir=bg_dir)
        path = os.path.join(out_dir, f"{name}.png")
        with open(path, "wb") as f:
            f.write(data)
        print(f"[ok] {path} {len(data) // 1024}KB page={p}/{tp}")
