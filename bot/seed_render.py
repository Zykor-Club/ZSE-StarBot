# -*- coding: utf-8 -*-
"""种子列表卡渲染（分页）：序号 + 名称 + 可输入代码 + 说明

视觉沿用 rank_render / lexicon_render：背景图 + 等比缩放 + 半透明面板，输出 JPEG。
"""

import io
import os

from PIL import Image, ImageDraw

from rank_render import (BASE_H, BASE_W, BG_MAX_SIDE, _base_canvas, _font, _pick_bg,
                         _truncate)
from lexicon_render import clean_text

PANEL = (10, 14, 24, 150)
ROW_BG = (255, 255, 255, 24)
HEAD_BG = (255, 255, 255, 30)
HILITE = (255, 214, 120, 255)
LABEL = (206, 216, 232, 255)
DIM = (196, 208, 224, 255)
WHITE = (255, 255, 255, 255)


def render_seed_list_card(rows, page: int = 1, total_pages: int = 1,
                          title: str = "种子列表", subtitle: str = "",
                          footer: str = "", bg_dir=None, bg_file=None,
                          jpeg_quality: int = 88) -> bytes:
    """rows: [{no, name, seed, desc, category}...] 当前页条目"""
    bg = _pick_bg(bg_dir, bg_file)
    if bg is not None and max(bg.size) > BG_MAX_SIDE:
        r = BG_MAX_SIDE / max(bg.size)
        bg = bg.resize((max(1, round(bg.width * r)), max(1, round(bg.height * r))), Image.LANCZOS)
    W, H = bg.size if bg is not None else (BASE_W, BASE_H)
    s = min(W / BASE_W, H / BASE_H)
    img = _base_canvas(bg, W, H)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def px(v):
        return max(1, round(v * s))

    pad = px(52)
    inner = px(30)
    title_f = _font(px(50), bold=True)
    sub_f = _font(px(30))
    no_f = _font(px(38), bold=True)
    name_f = _font(px(38), bold=True)
    code_f = _font(px(30))
    desc_f = _font(px(26))
    small_f = _font(px(26))

    od.text((pad, pad), title, font=title_f, fill=WHITE)
    page_txt = f"第 {page} / {total_pages} 页" + (f" · {subtitle}" if subtitle else "")
    od.text((W - pad, pad + px(10)), page_txt, font=sub_f, fill=DIM, anchor="ra")

    top = pad + px(74)
    bottom_limit = H - pad - px(46)
    col_no = pad + inner
    col_name = col_no + px(96)
    col_code = col_name + px(560)

    y = top
    row_h = px(74)
    od.rounded_rectangle([pad, y, W - pad, y + px(46)], radius=px(10), fill=HEAD_BG)
    od.text((col_no, y + px(8)), "序号", font=small_f, fill=LABEL)
    od.text((col_name, y + px(8)), "名称", font=small_f, fill=LABEL)
    od.text((col_code, y + px(8)), "可输入种子", font=small_f, fill=LABEL)
    y += px(56)

    for i, it in enumerate(rows or []):
        if y + row_h > bottom_limit:
            od.text((pad + inner, y), "本页显示不下，请翻页", font=small_f, fill=DIM)
            break
        if i % 2 == 0:
            od.rounded_rectangle([pad, y, W - pad, y + row_h - px(10)], radius=px(12), fill=ROW_BG)
        od.text((col_no, y + px(10)), "%02d" % int(it.get("no") or 0), font=no_f, fill=HILITE)
        od.text((col_name, y + px(12)),
                _truncate(measure, clean_text(it.get("name") or ""), name_f, px(540)),
                font=name_f, fill=WHITE)
        code = _truncate(measure, clean_text(it.get("seed") or ""), code_f, W - pad - inner - col_code)
        od.text((col_code, y + px(18)), code, font=code_f, fill=LABEL)
        y += row_h

    od.text((pad, H - pad - px(12)),
            footer or "starZSEbot · 种子数据来自 terraria.wiki.gg（CC BY-SA）",
            font=small_f, fill=DIM)
    out = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=int(jpeg_quality), optimize=True, progressive=True)
    return buf.getvalue()
