# -*- coding: utf-8 -*-
"""喵币卡片渲染（签到 / 我的信息 / 积分排行）

版式参考（结构参考，内容自定）：
    头像圈 + 玩家名 + 副标题(邮箱 · 日期)
    → 状态横幅（居中大字）
    → 徽章胶囊行（连续天数 / 总积分 / 已签到）
    → 明细面板（标签左、值右，行间细分隔线）
    → 右下角署名

防越界：值列按测量逐级缩小字号、超宽才截断；徽章放不下就不画；面板高度按剩余空间封顶。
"""

import io

from PIL import Image, ImageDraw

from rank_render import BASE_H, BASE_W, BG_MAX_SIDE, _base_canvas, _font, _pick_bg, _truncate
from lexicon_render import clean_text

WHITE = (255, 255, 255, 255)
LABEL = (178, 190, 210, 255)
DIM = (200, 210, 226, 210)
FAINT = (255, 255, 255, 115)
GOLD = (246, 200, 96, 255)
SEP = (255, 255, 255, 26)
PANEL = (10, 16, 30, 150)
SCRIM = (8, 10, 22, 96)
BADGE_TINT = {
    "gold": ((246, 200, 96, 46), (246, 200, 96, 190), (255, 226, 160, 255)),
    "blue": ((110, 168, 255, 44), (120, 176, 255, 175), (208, 228, 255, 255)),
    "green": ((86, 210, 140, 44), (96, 214, 146, 175), (206, 250, 222, 255)),
    "plain": ((255, 255, 255, 30), (255, 255, 255, 90), (232, 238, 248, 255)),
}


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


def _shrink(measure, txt, size_fn, width, min_px):
    f = size_fn(0)
    while measure.textlength(txt, font=f) > width and f.size > min_px:
        f = size_fn(-2)
    return f


def _pill(od, measure, x, y, text, key, s, pad_x, h, max_right):
    """徽章胶囊；放不下返回 None"""
    fill, border, fg = BADGE_TINT.get(str(key), BADGE_TINT["plain"])
    base = _font(round(30 * s), bold=True)
    txt = clean_text(str(text))
    w = measure.textlength(txt, font=base) + pad_x * 2
    if x + w > max_right:
        return None
    od.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=fill,
                         outline=border, width=max(1, round(2 * s)))
    od.text((x + pad_x, y + (h - base.size) // 2 - round(2 * s)), txt, font=base, fill=fg)
    return x + w


def render_info_card(name: str, rows, banner: str = "", subtitle: str = "",
                     badges=None, footer: str = "", bg_dir=None, bg_file=None,
                     avatar_bytes: bytes = b"", jpeg_quality: int = 88) -> bytes:
    img, overlay, od, W, H, s = _canvas(bg_dir, bg_file)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def px(v):
        return max(1, round(v * s))

    pad = px(44)
    od.rectangle([0, 0, W, H], fill=SCRIM)
    av = px(96)
    ax, ay = pad, pad
    pasted = False
    if avatar_bytes:
        try:
            src = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            side = min(src.size)
            left = (src.width - side) // 2
            top = (src.height - side) // 2
            src = src.crop((left, top, left + side, top + side)).resize((av, av), Image.LANCZOS)
            mask = Image.new("L", (av * 4, av * 4), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, av * 4, av * 4], fill=255)
            mask = mask.resize((av, av), Image.LANCZOS)
            overlay.paste(src, (ax, ay), mask)
            pasted = True
        except Exception:
            pasted = False
    if not pasted:
        od.ellipse([ax, ay, ax + av, ay + av], fill=(70, 92, 140, 235))
        ch = clean_text(str(name or "?"))[:1] or "?"
        od.text((ax + av / 2, ay + av / 2 - px(4)), ch, font=_font(px(46), bold=True), fill=WHITE, anchor="mm")
    od.ellipse([ax, ay, ax + av, ay + av], outline=(190, 210, 250, 200), width=max(1, px(3)))
    nx = ax + av + px(26)
    avail = W - nx - pad - px(200)
    name_f = _shrink(measure, clean_text(str(name)), lambda d: _font(px(50) + d, bold=True), avail, px(26))
    od.text((nx, ay + px(6)), _truncate(measure, clean_text(str(name)), name_f, avail), font=name_f, fill=WHITE)
    if subtitle:
        av2 = W - nx - pad
        sub_f = _shrink(measure, clean_text(str(subtitle)), lambda d: _font(px(28) + d), av2, px(18))
        od.text((nx, ay + px(66)), _truncate(measure, clean_text(str(subtitle)), sub_f, av2), font=sub_f, fill=DIM)
    y = ay + av + px(26)
    if banner:
        bh = px(72)
        od.rounded_rectangle([pad, y, W - pad, y + bh], radius=px(14), fill=(246, 200, 96, 38),
                             outline=(246, 200, 96, 150), width=max(1, px(2)))
        bw = W - 2 * pad - px(50)
        bf = _shrink(measure, clean_text(str(banner)), lambda d: _font(px(42) + d, bold=True), bw, px(24))
        od.text((W / 2, y + bh / 2 - px(2)), _truncate(measure, clean_text(str(banner)), bf, bw),
                font=bf, fill=GOLD, anchor="mm")
        y += bh + px(20)
    if badges:
        bh2 = px(52)
        x = pad
        for item in badges:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                txt, key = item[0], item[1]
            else:
                txt, key = item, "plain"
            nx2 = _pill(od, measure, x, y, txt, key, s, px(20), bh2, W - pad)
            if nx2 is None:
                break
            x = nx2 + px(14)
        y += bh2 + px(18)
    rows = list(rows or [])
    inner = px(30)
    bottom = H - pad - px(46)
    # 行高自适应：按剩余空间摊分，保证不裁行（最小值 40）；字体随行高缩放
    avail = max(px(40), bottom - y - inner * 2)
    row_h = px(62) if len(rows) <= 1 else max(px(40), min(px(62), avail // len(rows)))
    fscale = min(1.0, row_h / float(px(62)))
    panel_bottom = min(y + inner * 2 + row_h * len(rows), bottom)
    od.rounded_rectangle([pad, y, W - pad, panel_bottom], radius=px(20), fill=PANEL)
    label_w = px(230)
    value_x = pad + inner + label_w
    value_w = max(px(80), W - pad - inner - value_x)
    lab_f = _font(max(px(22), int(px(32) * fscale)))
    yy = y + inner
    for i, (label, value) in enumerate(rows):
        if yy + row_h > panel_bottom:
            od.text((pad + inner, yy), "…（显示不下，剩余 " + str(len(rows) - i) + " 项）", font=lab_f, fill=DIM)
            break
        od.text((pad + inner, yy + int(row_h * 0.22)), clean_text(str(label)), font=lab_f, fill=LABEL)
        txt = clean_text(str(value))
        vf = _shrink(measure, txt, lambda d: _font(int(px(34) * fscale) + d, bold=True), value_w, px(18))
        od.text((value_x, yy + int(row_h * 0.16)), _truncate(measure, txt, vf, value_w), font=vf, fill=WHITE)
        if i != len(rows) - 1:
            ly = yy + row_h - px(2)
            od.line([pad + inner, ly, W - pad - inner, ly], fill=SEP, width=max(1, px(1)))
        yy += row_h
    if footer:
        od.text((W - pad, H - pad - px(4)), clean_text(str(footer)), font=_font(px(24)), fill=FAINT, anchor="rs")
    return _encode(img, overlay, jpeg_quality)


def render_rank_card(items, title: str = "积分排行", subtitle: str = "", page: int = 1,
                     total_pages: int = 1, value_label: str = "喵币", footer: str = "",
                     bg_dir=None, bg_file=None, jpeg_quality: int = 88) -> bytes:
    img, overlay, od, W, H, s = _canvas(bg_dir, bg_file)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def px(v):
        return max(1, round(v * s))

    pad = px(44)
    od.rectangle([0, 0, W, H], fill=SCRIM)
    od.text((pad, pad), clean_text(str(title)), font=_font(px(50), bold=True), fill=WHITE)
    sub_f = _font(px(28))
    ptxt = "第 " + str(page) + " / " + str(total_pages) + " 页" + (" · " + str(subtitle) if subtitle else "")
    od.text((W - pad, pad + px(16)), clean_text(ptxt), font=sub_f, fill=DIM, anchor="ra")
    y = pad + px(80)
    hh = px(48)
    od.rounded_rectangle([pad, y, W - pad, y + hh], radius=px(12), fill=(255, 255, 255, 26))
    od.text((pad + px(24), y + px(10)), "名次", font=sub_f, fill=LABEL)
    od.text((pad + px(170), y + px(10)), "玩家", font=sub_f, fill=LABEL)
    od.text((W - pad - px(24), y + px(10)), clean_text(str(value_label)), font=sub_f, fill=LABEL, anchor="ra")
    y += hh + px(10)
    items = list(items or [])
    row_h = px(70)
    bottom = H - pad - px(46)
    name_f = _font(px(38), bold=True)
    for i, (rank, pname, value) in enumerate(items):
        if y + row_h > bottom:
            od.text((pad + px(24), y), "本页显示不下，请翻页", font=sub_f, fill=DIM)
            break
        if i % 2 == 0:
            od.rounded_rectangle([pad, y, W - pad, y + row_h - px(8)], radius=px(12), fill=(255, 255, 255, 24))
        top3 = int(rank) in (1, 2, 3)
        od.text((pad + px(24), y + px(12)), str(rank), font=_font(px(38), bold=True), fill=(GOLD if top3 else LABEL))
        od.text((pad + px(170), y + px(14)),
                _truncate(measure, clean_text(str(pname)), name_f, W - pad - px(170) - px(380)),
                font=name_f, fill=WHITE)
        vt = clean_text(str(value))
        vf = _shrink(measure, vt, lambda d: _font(px(34) + d, bold=True), px(340), px(22))
        od.text((W - pad - px(24), y + px(18)), _truncate(measure, vt, vf, px(340)), font=vf, fill=GOLD, anchor="ra")
        y += row_h
    if footer:
        od.text((W - pad, H - pad - px(4)), clean_text(str(footer)), font=_font(px(24)), fill=FAINT, anchor="rs")
    return _encode(img, overlay, jpeg_quality)
