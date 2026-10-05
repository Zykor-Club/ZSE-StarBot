# -*- coding: utf-8 -*-
"""图鉴卡渲染：单条详情卡 / 多条结果列表卡

视觉沿用 rank_render：背景图 + 等比缩放 + 半透明面板（先画 overlay 再 alpha_composite，
在 RGBA 画布上直接画不会混合 alpha）。像素图标放大用 NEAREST 保持锐利。
"""

import io
import os
import re

from PIL import Image, ImageDraw

from rank_render import (BASE_H, BASE_W, BG_MAX_SIDE, _base_canvas, _font, _pick_bg,
                         _truncate)

# 数据里出现的"字体画不出"的字符：U+FE0F 变体选择符、U+20E3 组合键帽（如 #️⃣）、
# 以及 🌈（在数据里是"微光嬗变"的标记）。前两者剥离，🌈 换成雅黑可渲染的 ◆。
_STRIP_CHARS = "\ufe0f\ufe0e\u20e3"
_EMOJI_MAP = {"\U0001f308": "◆"}


def clean_text(text) -> str:
    """清洗说明/名称里的不可渲染字符（emoji 图标 → 文本标记）"""
    s = str(text or "")
    for ch in _STRIP_CHARS:
        s = s.replace(ch, "")
    for k, v in _EMOJI_MAP.items():
        s = s.replace(k, v)
    return s


PANEL = (10, 14, 24, 150)          # 内容面板底色（带 alpha）
PANEL_SOFT = (255, 255, 255, 22)   # 面板内浅色块（图标底/行底）
ROW_BG = (255, 255, 255, 24)
HILITE = (255, 214, 120, 255)
LABEL = (200, 212, 230, 255)
DIM = (196, 208, 224, 255)
WHITE = (255, 255, 255, 255)


def _wrap(draw, text, font, max_w, max_lines=0):
    """按像素宽度折行（中英混排），max_lines>0 时截断并补省略号"""
    out, cur = [], ""
    for ch in clean_text(text).replace("\r", ""):
        if ch == "\n":
            out.append(cur)
            cur = ""
            continue
        if cur and draw.textlength(cur + ch, font) > max_w:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    out = [l for l in out if l != ""] or [""]
    if max_lines and len(out) > max_lines:
        out = out[:max_lines]
        out[-1] = _truncate(draw, out[-1] + "…", font, max_w)
    return out


_SENT_SPLIT = re.compile(r"[^。！？；!?;]+[。！？；!?;]?")
_NO_LINE_START = set("。，、！？；：）】》”’…—!?,.;:)")   # 避头标点


def _sentence_lines(draw, text, font, max_w):
    """说明排版：按句号/叹号/问号/分号切句，**每句另起一行**，过长的句子再按宽度折行。

    中文说明按句断行比整段折行好读得多（用户口径：每个句号都换行）。
    """
    out = []
    for para in str(text or "").replace("\r", "").split("\n"):
        para = para.strip()
        if not para:
            continue
        for sent in _SENT_SPLIT.findall(para):
            sent = sent.strip()
            if not sent:
                continue
            cur = ""
            for ch in sent:
                # 避头标点：标点不另起一行（否则会出现孤零零的「。」独占一行）
                if cur and ch not in _NO_LINE_START and draw.textlength(cur + ch, font) > max_w:
                    out.append(cur)
                    cur = ch
                else:
                    cur += ch
            if cur:
                out.append(cur)
    return out


def _load_icon(path, box, can_upscale=True):
    """载入并缩放图标到 box 内。放大用 NEAREST（像素画锐利），缩小用 LANCZOS。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        im = Image.open(path).convert("RGBA")
    except (OSError, ValueError):
        return None
    w, h = im.size
    if w <= 0 or h <= 0:
        return None
    k = min(box / w, box / h)
    if k < 1:
        im = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
    elif can_upscale and k > 1.05:
        k = min(k, 8.0)          # 放大上限，避免小图标糊成马赛克墙
        im = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.NEAREST)
    return im


def render_lexicon_card(kind_label: str, query: str, mode: str, item: dict = None,
                        attrs=None, desc: str = "", icon_path: str = "",
                        rows=None, total: int = 0,
                        empty_desc_hint: str = "", footer: str = "",
                        stack_desc: bool = False, jpeg_quality: int = 88,
                        bg_dir=None, bg_file=None) -> bytes:
    """mode="single"：单条详情卡；mode="list"：多条结果列表卡。返回 PNG bytes。"""
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
    title_f = _font(px(50), bold=True)
    sub_f = _font(px(32))
    name_f = _font(px(56), bold=True)
    id_f = _font(px(30))
    label_f = _font(px(30))
    value_f = _font(px(36), bold=True)
    body_f = _font(px(32))
    row_f = _font(px(40), bold=True)
    small_f = _font(px(28))

    od.text((pad, pad), f"图鉴 · {kind_label}", font=title_f, fill=WHITE)
    qtext = _truncate(measure, f"「{query}」", sub_f, W - pad * 2 - px(340))
    od.text((W - pad, pad + px(10)), qtext, font=sub_f, fill=DIM, anchor="ra")
    top = pad + px(74)
    bottom_limit = H - pad - px(46)

    if mode == "list":
        rows = list(rows or [])
        shown = len(rows)
        info = f"找到 {total} 条匹配" + (f"（显示前 {shown} 条）" if total > shown else "")
        od.text((pad, top), info, font=sub_f, fill=HILITE)
        y = top + px(58)
        icon_box = px(84)
        row_h = px(110)
        for i, row in enumerate(rows):
            if y + row_h > bottom_limit:
                od.text((pad, y), f"共 {total} 条匹配，输入更精确的名字或 ID 可直接看详情",
                        font=small_f, fill=DIM)
                break
            if i % 2 == 0:
                od.rounded_rectangle([pad, y, W - pad, y + row_h - px(10)], radius=px(14), fill=ROW_BG)
            ic = _load_icon(row.get("icon") or "", icon_box)
            if ic is not None:
                img.paste(ic, (pad + px(16) + (icon_box - ic.width) // 2,
                               y + (row_h - px(10) - ic.height) // 2), ic)
            tx = pad + px(16) + icon_box + px(24)
            od.text((tx, y + px(12)), _truncate(measure, str(row.get("name") or "?"), row_f,
                                                W - tx - pad - px(400)), font=row_f, fill=WHITE)
            od.text((tx, y + px(60)), f"ID {row.get('id')}", font=small_f, fill=DIM)
            sm = str(row.get("summary") or "")
            if sm:
                od.text((W - pad - px(20), y + px(32)), _truncate(measure, sm, small_f, px(380)),
                        font=small_f, fill=LABEL, anchor="ra")
            y += row_h
    elif mode == "desc":
        # 说明卡：名称 + 完整说明（长文本可整卡铺开，不再被资料卡挤成几行）
        item = item or {}
        desc = clean_text(desc).strip()
        inner = px(34)
        icon_box = px(96)
        body_lines = _sentence_lines(measure, desc, body_f, W - pad * 2 - inner * 2)
        max_lines = max(1, (bottom_limit - top - px(150) - inner * 2) // px(54))
        if len(body_lines) > max_lines:
            body_lines = body_lines[:max_lines]
            body_lines[-1] = _truncate(measure, body_lines[-1] + "…", body_f, W - pad * 2 - inner * 2)
        panel_h = inner * 2 + max(icon_box, px(70)) + px(30) + len(body_lines) * px(46)
        panel = [pad, top, W - pad, min(top + panel_h, bottom_limit)]
        od.rounded_rectangle(panel, radius=px(24), fill=PANEL)
        ix, iy = pad + inner, top + inner
        ic = _load_icon(icon_path, icon_box - px(14))
        od.rounded_rectangle([ix, iy, ix + icon_box, iy + icon_box], radius=px(16), fill=PANEL_SOFT)
        if ic is not None:
            img.paste(ic, (ix + (icon_box - ic.width) // 2, iy + (icon_box - ic.height) // 2), ic)
        od.text((ix + icon_box + px(24), iy + px(6)),
                _truncate(measure, clean_text(item.get("Name") or "说明"), name_f,
                          W - pad - inner - (ix + icon_box + px(24))),
                font=name_f, fill=WHITE)
        y = iy + icon_box + px(30)
        for line in body_lines:
            od.text((pad + inner, y), line, font=body_f, fill=WHITE)
            y += px(54)
    else:
        item = item or {}
        attrs = list(attrs or [])
        desc = clean_text(desc).strip()
        inner = px(34)
        icon_box = px(150) if stack_desc else px(190)   # 合并模式收紧资料面板，给说明留空间
        name_h = max(icon_box, px(140)) + px(18)
        attr_rows = (len(attrs) + 1) // 2
        attrs_h = attr_rows * px(56) if attrs else 0
        wrapped = []
        hint = ""
        if desc and not stack_desc:
            max_reasonable = max(1, min(8, (bottom_limit - top - name_h - attrs_h - px(140)) // px(44)))
            wrapped = _wrap(measure, desc, body_f, W - pad * 2 - inner * 2, max_lines=max_reasonable)
        elif empty_desc_hint and not desc:
            # 数据里没有说明时给一行提示，避免用户以为是渲染 bug（stack_desc 时放进下面的说明面板）
            hint = _truncate(measure, str(empty_desc_hint), label_f, W - pad * 2 - inner * 2)
        desc_h = (px(58) + max(1, len(wrapped)) * px(44)) if (wrapped or (hint and not stack_desc)) else 0
        panel_h = min(inner * 2 + name_h + attrs_h + desc_h, bottom_limit - top)
        panel = [pad, top, W - pad, top + panel_h]
        od.rounded_rectangle(panel, radius=px(24), fill=PANEL)

        ix, iy = pad + inner, top + inner
        od.rounded_rectangle([ix, iy, ix + icon_box, iy + icon_box], radius=px(18), fill=PANEL_SOFT)
        ic = _load_icon(icon_path, icon_box - px(30))
        if ic is not None:
            img.paste(ic, (ix + (icon_box - ic.width) // 2, iy + (icon_box - ic.height) // 2), ic)

        tx = ix + icon_box + px(34)
        right_w = W - pad - inner - tx
        od.text((tx, iy + px(6)), _truncate(measure, clean_text(item.get("Name") or "?"), name_f, right_w),
                font=name_f, fill=WHITE)
        od.text((tx, iy + px(78)), f"ID {item.get('_id')}", font=id_f, fill=DIM)

        y = iy + icon_box + px(26)
        col_w = (W - pad * 2 - inner * 2 - px(30)) // 2
        for i, (label, value) in enumerate(attrs):
            col = i % 2
            row = i // 2
            cx = pad + inner + col * (col_w + px(30))
            cy = y + row * px(56)
            od.text((cx, cy), str(label), font=label_f, fill=LABEL)
            od.text((cx + px(150), cy - px(4)),
                    _truncate(measure, str(value), value_f, col_w - px(150)), font=value_f, fill=HILITE)
        if attrs:
            y += attrs_h

        if stack_desc and (desc or hint):
            # 资料与说明分成上下两个独立面板（视觉上是两张卡，但只用一条消息、一次上传）
            body_lines = (_sentence_lines(measure, desc, body_f, W - pad * 2 - inner * 2)
                          if desc else [hint])
            py0 = y + px(26)
            max_lines = max(1, (bottom_limit - py0 - inner * 2 - px(64)) // px(54))
            if len(body_lines) > max_lines:
                body_lines = body_lines[:max_lines]
                body_lines[-1] = _truncate(measure, body_lines[-1] + "…", body_f,
                                           W - pad * 2 - inner * 2)
            # 说明面板：不再重复物品名与图标，只有「说明」标题 + 正文；
            # 正文按句换行、行距加大（用户口径：每个句号都换行，上下间距拉开一点更好看）
            lead = px(54) if desc else px(46)
            dh = inner * 2 + px(50) + len(body_lines) * lead
            od.rounded_rectangle([pad, py0, W - pad, min(py0 + dh, bottom_limit)],
                                 radius=px(24), fill=PANEL)
            od.text((pad + inner, py0 + inner), "说明", font=sub_f, fill=HILITE)
            yy = py0 + inner + px(50)
            for line in body_lines:
                od.text((pad + inner, yy), line,
                        font=(body_f if desc else label_f), fill=(WHITE if desc else DIM))
                yy += lead
        elif wrapped or hint:
            od.text((pad + inner, y + px(8)), "📖 说明", font=sub_f, fill=HILITE)
            yy = y + px(58)
            if wrapped:
                for line in wrapped:
                    od.text((pad + inner, yy), line, font=body_f, fill=WHITE)
                    yy += px(44)
            else:
                od.text((pad + inner, yy), hint, font=label_f, fill=DIM)

    od.text((pad, H - pad - px(12)), footer or "starZSEbot · 图鉴", font=small_f, fill=DIM)

    out = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    # 统一用 JPEG：同样的画布 PNG 约 270KB、JPEG(q=88) 约 60KB，群里上传快 4 倍以上
    # （卡片是合成在不透明背景上的，没有透明通道需求）
    out.save(buf, format="JPEG", quality=int(jpeg_quality), optimize=True, progressive=True)
    return buf.getvalue()
