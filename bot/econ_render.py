# -*- coding: utf-8 -*-
"""喵币卡片渲染（签到 / 我的信息 / 积分排行）

排版要点（防越界）：
  · 固定两栏：标签列宽度固定，值列用剩余宽度并按测量结果逐级缩小字号；
  · 每条值都经过 _truncate（超宽才截断并加省略号），不会压到别的元素；
  · 面板高度按行数计算，超出可用高度则提示"显示不下"，不会画到画布外。
沿用 rank_render/seed_render 的背景与等比缩放策略，输出 JPEG。
"""

import io
import os

from PIL import Image, ImageDraw

from rank_render import BASE_H, BASE_W, BG_MAX_SIDE, _base_canvas, _font, _pick_bg, _truncate
from lexicon_render import clean_text

PANEL = (10, 14, 24, 152)
ROW_BG = (255, 255, 255, 24)
HEAD_BG = (255, 255, 255, 32)
HILITE = (255, 214, 120, 255)
LABEL = (206, 216, 232, 255)
DIM = (196, 208, 224, 255)
WHITE = (255, 255, 255, 255)


def _canvas(bg_dir=None, bg_file=None):
    bg = _pick_bg(bg_dir, bg_file)
    if bg is not None and max(bg.size) > BG_MAX_SIDE:
        r = BG_MAX_SIDE / max(bg.size)
        bg = bg.resize((max(1, round(bg.width * r)), max(1, round(bg.height * r))), Image.LANCZOS)
    W, H = bg.size if bg is not None else (BASE_W, BASE_H)
    s = min(W / BASE_W, H / BASE_H)
    img = _base_canvas(bg, W, H)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    return img, overlay, ImageDraw.Draw(overlay), W, H, s


def _encode(img, overlay, quality=88):
    out = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=int(quality), optimize=True, progressive=True)
    return buf.getvalue()


def render_info_card(title: str, rows, subtitle: str = "", footer: str = "",
                     badge: str = "", bg_dir=None, bg_file=None, jpeg_quality: int = 88) -> bytes:
    """信息卡：rows = [(标签, 值), ...]，左标签右值，自适应字号防越界"""
    img, overlay, od, W, H, s = _canvas(bg_dir, bg_file)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def px(v):
        return max(1, round(v * s))

    pad = px(52)
    inner = px(34)
    title_f = _font(px(50), bold=True)
    sub_f = _font(px(30))
    label_f = _font(px(34))
    value_f = _font(px(38), bold=True)
    row_h = px(64)

    od.text((pad, pad), clean_text(str(title)), font=title_f, fill=WHITE)
    if subtitle:
        od.text((W - pad, pad + px(12)), clean_text(str(subtitle)), font=sub_f, fill=DIM, anchor="ra")
    y = pad + px(84)
    # 徽章（例如"签到成功"）
    if badge:
        bw = measure.textlength(badge, font=value_f) + px(40)
        od.rounded_rectangle([pad, y, min(pad + bw, W - pad), y + px(52)], radius=px(26), fill=(255, 214, 120, 60))
        od.text((pad + px(20), y + px(6)), clean_text(str(badge)), font=value_f, fill=HILITE)
        y += px(72)

    rows = list(rows or [])
    panel_h = inner * 2 + len(rows) * row_h
    bottom = min(y + panel_h, H - pad - px(52))
    od.rounded_rectangle([pad, y, W - pad, bottom], radius=px(24), fill=PANEL)
    label_w = px(240)
    value_x = pad + inner + label_w
    value_w = max(px(80), W - pad - inner - value_x - inner)
    yy = y + inner
    for i, (label, value) in enumerate(rows):
        if yy + row_h > bottom:
            od.text((pad + inner, yy), "…（显示不下，剩余 %d 项）" % (len(rows) - i), font=label_f, fill=DIM)
            break
        if i % 2 == 0:
            od.rounded_rectangle([pad + px(10), yy, W - pad - px(10), yy + row_h - px(8)],
                                 radius=px(10), fill=ROW_BG)
        od.text((pad + inner, yy + px(12)), clean_text(str(label)), font=label_f, fill=LABEL)
        txt = clean_text(str(value))
        f = value_f
        while measure.textlength(txt, font=f) > value_w and f.size > px(20):
            f = _font(f.size - 2, bold=True)
        od.text((value_x, yy + px(8)), _truncate(measure, txt, f, value_w), font=f, fill=WHITE)
        yy += row_h
    if footer:
        od.text((pad, H - pad - px(12)), footer, font=sub_f, fill=DIM)
    return _encode(img, overlay, jpeg_quality)


def render_rank_card(items, title: str = "积分排行", subtitle: str = "", page: int = 1,
                     total_pages: int = 1, value_label: str = "喵币", footer: str = "",
                     bg_dir=None, bg_file=None, jpeg_quality: int = 88) -> bytes:
    """榜单卡：items = [(名次, 名字, 值), ...]"""
    img, overlay, od, W, H, s = _canvas(bg_dir, bg_file)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def px(v):
        return max(1, round(v * s))

    pad = px(52)
    inner = px(30)
    title_f = _font(px(50), bold=True)
    sub_f = _font(px(30))
    rank_f = _font(px(38), bold=True)
    name_f = _font(px(38), bold=True)
    val_f = _font(px(34))
    row_h = px(72)

    od.text((pad, pad), clean_text(str(title)), font=title_f, fill=WHITE)
    ptxt = f"第 {page} / {total_pages} 页" + (f" · {subtitle}" if subtitle else "")
    od.text((W - pad, pad + px(10)), ptxt, font=sub_f, fill=DIM, anchor="ra")
    y = pad + px(84)
    od.rounded_rectangle([pad, y, W - pad, y + px(46)], radius=px(10), fill=HEAD_BG)
    od.text((pad + inner, y + px(8)), "名次", font=sub_f, fill=LABEL)
    od.text((pad + inner + px(150), y + px(8)), "玩家", font=sub_f, fill=LABEL)
    od.text((W - pad - inner, y + px(8)), value_label, font=sub_f, fill=LABEL, anchor="ra")
    y += px(56)

    items = list(items or [])
    bottom = H - pad - px(52)
    for i, (rank, name, value) in enumerate(items):
        if y + row_h > bottom:
            od.text((pad + inner, y), "本页显示不下，请翻页", font=sub_f, fill=DIM)
            break
        if i % 2 == 0:
            od.rounded_rectangle([pad, y, W - pad, y + row_h - px(10)], radius=px(12), fill=ROW_BG)
        medal = {1: "1", 2: "2", 3: "3"}.get(int(rank), str(rank))
        od.text((pad + inner, y + px(10)), medal, font=rank_f, fill=HILITE)
        od.text((pad + inner + px(150), y + px(12)),
                _truncate(measure, clean_text(str(name)), name_f, W - pad - inner - px(150) - px(360)),
                font=name_f, fill=WHITE)
        od.text((W - pad - inner, y + px(16)), _truncate(measure, clean_text(str(value)), val_f, px(340)),
                font=val_f, fill=HILITE, anchor="ra")
        y += row_h
    if footer:
        od.text((pad, H - pad - px(12)), footer, font=sub_f, fill=DIM)
    return _encode(img, overlay, jpeg_quality)
