# -*- coding: utf-8 -*-
"""种子投票卡渲染（Pillow）· 方案 C：左信息栏 + 右列表

画布 = 随机背景图的原始像素尺寸（不裁剪、不缩放；缺图兜底 1920×1084 纯色）。
以 1920×1084 为基准等比缩放 s = min(W/1920, H/1084)，内容整体居中：
  左信息栏（560×s）：状态徽章 → 大标题 → 规则块 → 底部参与提示
  右列表（铺满剩余高度）：每行「编号. 组合名」+ 提案人徽章 + 分数徽章 / 进度条 + 百分比
组合名自适应字号完整显示（逐级缩小；极端超宽折两行），不再出现省略号截断。
背景/字体加载与配色沿用原实现（随机背景 + 压暗；中文微软雅黑，防豆腐块）。
"""

import os
import random
import re
import tempfile
from datetime import datetime

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

# ── 素材 / 字体路径（与 lookbag_render 一致：环境变量可覆盖） ──
_BASE_DIR = os.environ.get("LOOKBAG_ASSETS_BASE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "lookbag")
DEFAULT_BG_DIR = os.environ.get("LOOKBAG_BG_DIR") or os.path.join(_BASE_DIR, "backgrounds")

FONT_REG = "C:/Windows/Fonts/msyh.ttc"       # 微软雅黑
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"    # 微软雅黑粗体

# 剥离微软雅黑不支持的 emoji / 零宽符号，避免渲染成豆腐块
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\uFE0E]")

# ── 基准尺寸（1920×1084 样卡；实际渲染全部 × s 缩放） ──
BASE_W, BASE_H = 1920, 1084
MARGIN = 48          # 画布内边距
LEFT_W = 560         # 左信息栏宽度
GAP = 40             # 左右栏间距
BADGE_H, BADGE_FS = 56, 30      # 状态徽章高 / 字号
TITLE_FS = 62                   # 标题字号
RULE_FS, RULE_LH = 26, 38       # 规则块字号 / 行高
RULE_MIN_FS = 18                # 规则块兜底最小字号（宽度自适应降字号下限）
RULE_PADX, RULE_PADY = 28, 20   # 规则块内边距
FOOT_FS, FOOT_LH = 28, 36       # 底部提示字号 / 行高
NAME_FS, NAME_MIN, NAME_ABS_MIN = 42, 26, 18   # 组合名起始 / 下限 / 绝对下限字号
PROP_FS, SCORE_FS, PCT_FS = 24, 26, 28         # 提案人 / 分数 / 百分比字号
BAR_H = 14           # 进度条高
ROW_PADX, ROW_PADY = 24, 12     # 选项行内边距
BAR_GAP = 10         # 名称行与进度条间距

# ── 颜色 ──
TEXT = (240, 244, 252)
MUTED = (204, 212, 228)
TITLE_COLOR = (255, 255, 255)
BADGE_OPEN_BG = (46, 134, 222, 235)
BADGE_CLOSED_BG = (176, 66, 74, 235)
BADGE_TEXT = (255, 255, 255)
PANEL_BG = (255, 255, 255, 18)
PANEL_LINE = (255, 255, 255, 46)
ROW_BG = (255, 255, 255, 15)
SCORE_BG = (255, 255, 255, 36)
PROPOSER_BG = (92, 104, 196, 228)
PROPOSER_TEXT = (236, 239, 255)
BAR_TRACK = (255, 255, 255, 41)
BAR_FILL = (74, 144, 226)
PCT_COLOR = (214, 222, 236)
BG_FALLBACK = (34, 30, 48, 255)
BG_BRIGHTNESS = 0.72
BG_DARKEN_ALPHA = 118

_font_cache: dict = {}


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
    return _EMOJI_RE.sub("", str(value)).strip()


def _fmt_score(score) -> str:
    """整数不显示小数，1.5 保留一位"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "0"
    if abs(s - round(s)) < 1e-9:
        return str(int(round(s)))
    return f"{s:.1f}"


def _fmt_dt(iso) -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return str(iso)
    return f"{dt.month}月{dt.day}日 {dt:%H:%M}"


def _truncate(draw, text, font, max_w):
    """极端兜底截断（仅标题 / 提案人徽章用）；组合名走 _fit_size+_wrap_two，不截断"""
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
    """从 start 逐级减小字号（下限 floor），返回第一个放得下的字号"""
    size = max(start, floor)
    while size > floor:
        if draw.textlength(text, size_to_font(size, bold)) <= max_w:
            return size
        size -= step
    return floor


def _wrap_two(draw, text, font, max_w):
    """把文本拆成两行（优先在中点附近断开）；两行都放得下才返回 [l1, l2]，否则 None"""
    n = len(text)
    center = n // 2
    for off in range(0, center + 1):
        for i in (center + off, center - off):
            if 1 <= i < n:
                l1, l2 = text[:i].rstrip(), text[i:].lstrip()
                if (l1 and l2 and draw.textlength(l1, font) <= max_w
                        and draw.textlength(l2, font) <= max_w):
                    return [l1, l2]
    return None


def _layout_rule_line(draw, text, max_w, base_size, floor_size, base_lh):
    """规则块单行布局（宽度自适应）：单行放得下 → 单行；否则同字号折两行；
    再不行逐级降字号（步长 2px）重试；最低字号仍放不下 → 兜底截断。
    返回 [(文本, 字体, 行高), ...]（1~2 项，行高随字号等比缩放）。"""
    size = max(base_size, floor_size)
    while True:
        f = _font(size)
        lh = max(1, int(round(base_lh * size / base_size)))
        if draw.textlength(text, f) <= max_w:
            return [(text, f, lh)]
        two = _wrap_two(draw, text, f, max_w)
        if two:
            return [(two[0], f, lh), (two[1], f, lh)]
        if size <= floor_size:
            f = _font(floor_size)
            lh = max(1, int(round(base_lh * floor_size / base_size)))
            return [(_truncate(draw, text, f, max_w), f, lh)]
        size = max(floor_size, size - 2)


def _pick_bg(bg_dir, bg_file=None):
    """取一张背景原图（不裁剪、不缩放）；找不到/读不了返回 None"""
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
    """背景原图 → 压暗后的 RGBA 画布；bg=None 时纯色深底兜底"""
    if bg is None:
        return Image.new("RGBA", (w, h), BG_FALLBACK)
    img = ImageEnhance.Brightness(bg).enhance(BG_BRIGHTNESS).convert("RGBA")
    return Image.alpha_composite(img, Image.new("RGBA", (w, h), (0, 0, 0, BG_DARKEN_ALPHA)))


def render_vote_card(out_png, vote, tally, status="open", bg_dir=None, bg_file=None) -> str:
    """渲染投票卡到 out_png，返回 out_png。

    vote / tally 结构见 vote_store；status="open"/"closed"。
    bg_file：测试用，指定具体背景路径；缺省从 bg_dir 随机取。
    """
    vote = vote or {}
    tally = tally or {"total_score": 0.0, "options": []}
    opts = sorted(tally.get("options") or [], key=lambda o: int(o.get("no") or 0))

    st = vote.get("status") or status
    if st not in ("open", "closed"):
        st = "open"

    title = _clean(vote.get("title") or "下个档玩什么")
    deadline = _fmt_dt(vote.get("deadline"))

    # 获胜组合名（closed）：无有效投票（winner_no 为空）时明确显示"无"
    winner_name = ""
    if st == "closed":
        wn = vote.get("winner_no")
        if wn is None:
            winner_name = "无（没有任何有效投票）"
        else:
            picked = next((o for o in opts if int(o.get("no") or 0) == int(wn)), None)
            if picked is None and opts:
                picked = max(opts, key=lambda o: float(o.get("score") or 0))
            winner_name = _clean(picked.get("name")) if picked else "—"

    # 左栏文案
    rule_lines = ["每人最多 2 票", "在线满 50 小时每票 1.5 分"]
    if st == "open":
        rule_lines.append(f"截止时间：{deadline}")
        foot_lines = ["发送「投票 编号」参与", "每人可投 2 个组合"]
    else:
        rule_lines.append(f"获胜组合：{winner_name}")
        if vote.get("tie_random"):
            rule_lines.append("平分随机选取")
        foot_lines = ["投票已结束，感谢参与"]

    # ── 画布 = 背景原图尺寸（不裁剪不缩放） ──
    bg = _pick_bg(bg_dir, bg_file)
    W, H = bg.size if bg is not None else (BASE_W, BASE_H)
    img = _base_canvas(bg, W, H)
    s = min(W / BASE_W, H / BASE_H)

    def px(v):
        return max(1, int(round(v * s)))

    x_left = px(MARGIN)
    content_w = W - x_left * 2
    left_w = px(LEFT_W)
    x_right = x_left + left_w + px(GAP)
    right_w = content_w - left_w - px(GAP)

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    top_y = px(MARGIN)

    # ── 左栏：状态徽章 ──
    badge_font = _font(px(BADGE_FS), True)
    badge_text = "进行中" if st == "open" else "已结束"
    badge_w = int(od.textlength(badge_text, badge_font)) + px(44)
    badge_box = (x_left, top_y, x_left + badge_w, top_y + px(BADGE_H))

    # ── 左栏：标题（固定短文案；防越界仍做缩放 + 兜底截断） ──
    title_size = _fit_size(measure, title, _font, left_w, px(TITLE_FS), px(40), bold=True)
    title_font = _font(title_size, True)
    if measure.textlength(title, title_font) > left_w:
        title = _truncate(measure, title, title_font, left_w)
    title_y = top_y + px(BADGE_H) + px(20)
    title_lh = int(title_size * 1.25)

    # ── 左栏：规则块（宽度自适应：超宽行折两行 / 降字号；高度按实际行数） ──
    rules_y = title_y + title_lh + px(22)
    rules_inner_w = left_w - px(RULE_PADX) * 2
    rule_rendered = []
    for line in rule_lines:
        rule_rendered += _layout_rule_line(measure, line, rules_inner_w,
                                           px(RULE_FS), px(RULE_MIN_FS), px(RULE_LH))
    rules_h = px(RULE_PADY) * 2 + sum(lh for _, _, lh in rule_rendered)
    rules_box = (x_left, rules_y, x_left + left_w, rules_y + rules_h)

    # ── 左栏：底部提示（贴底） ──
    foot_y = H - px(MARGIN) - len(foot_lines) * px(FOOT_LH)

    # ── 右列表：逐行测量 ──
    score_font = _font(px(SCORE_FS), True)
    prop_font = _font(px(PROP_FS))
    pct_font = _font(px(PCT_FS), True)
    score_bh = px(46)   # 分数徽章固定高度
    prop_bh = px(40)    # 提案人徽章固定高度
    badge_bh = max(score_bh, prop_bh)
    max_score = max([float(o.get("score") or 0) for o in opts], default=0.0)

    rows = []
    for i, o in enumerate(opts):
        no = int(o.get("no") or (i + 1))
        name = _clean(o.get("name") or "")
        score = float(o.get("score") or 0)
        votes = int(o.get("votes") or 0)
        proposer = _clean(o.get("proposer") or "")

        score_text = f"{_fmt_score(score)} 分 · {votes} 票"
        prop_text = _truncate(measure, f"{proposer} 提案", prop_font, px(240))

        score_w = int(measure.textlength(score_text, score_font)) + px(36)
        prop_w = int(measure.textlength(prop_text, prop_font)) + px(32)

        row_in_l = x_right + px(ROW_PADX)
        row_in_r = x_right + right_w - px(ROW_PADX)
        name_max = row_in_r - score_w - px(14) - prop_w - px(22) - row_in_l

        # 组合名完整显示：先逐级缩字号；仍超宽则折两行；最后才极端兜底
        name_full = f"{no}. {name}"
        name_size = _fit_size(measure, name_full, _font, name_max,
                              px(NAME_FS), px(NAME_MIN), bold=True)
        name_font_row = _font(name_size, True)
        name_lines = [name_full]
        if measure.textlength(name_full, name_font_row) > name_max:
            two = _wrap_two(measure, name_full, name_font_row, name_max)
            if two:
                name_lines = two
            else:
                size = px(NAME_MIN) - px(2)
                while size > px(NAME_ABS_MIN):
                    f2 = _font(size, True)
                    two = _wrap_two(measure, name_full, f2, name_max)
                    if two:
                        name_font_row, name_size, name_lines = f2, size, two
                        break
                    size -= px(2)
                else:
                    name_lines = [_truncate(measure, name_full, name_font_row, name_max)]

        name_lh = int(name_size * 1.25)
        # 内容区高度取「名称文本块」与「徽章高度」的较大者：
        # 字号被压缩时（name_lh < 徽章高）徽章不再越出到进度条/百分比上，避免上下交叠
        content_h = max(name_lh * len(name_lines), badge_bh)
        row_h = px(ROW_PADY) * 2 + content_h + px(BAR_GAP) + px(BAR_H)
        ratio = (score / max_score) if max_score > 0 else 0.0

        rows.append({
            "name_lines": name_lines, "name_font": name_font_row, "name_lh": name_lh,
            "content_h": content_h,
            "score_text": score_text, "score_w": score_w,
            "prop_text": prop_text, "prop_w": prop_w,
            "fill_ratio": max(0.0, min(1.0, ratio)),
            "row_h": row_h,
            "pct_text": f"{o.get('percent', 0.0)}%",
        })

    # 行纵向铺满（space-between 等效；极挤时保底 8×s 间距）
    avail_h = H - px(MARGIN) * 2
    total_h = sum(r["row_h"] for r in rows)
    n = len(rows)
    gap_v = (avail_h - total_h) / (n - 1) if n > 1 else 0.0
    if n > 1:
        gap_v = max(gap_v, px(8))

    y = px(MARGIN)
    for r in rows:
        r["y"] = int(y)
        y += r["row_h"] + gap_v

    # ── overlay：半透明底块（独立图层合成） ──
    od.rounded_rectangle(badge_box, radius=px(14),
                         fill=BADGE_OPEN_BG if st == "open" else BADGE_CLOSED_BG)
    od.rounded_rectangle(rules_box, radius=px(18), fill=PANEL_BG,
                         outline=PANEL_LINE, width=px(2))
    for r in rows:
        ry = r["y"]
        od.rounded_rectangle((x_right, ry, x_right + right_w, ry + r["row_h"]),
                             radius=px(14), fill=ROW_BG)
        s_h = score_bh
        r["score_x"] = x_right + right_w - px(ROW_PADX) - r["score_w"]
        r["score_y"] = ry + px(ROW_PADY) + max(0, (r["content_h"] - s_h) // 2)
        p_h = prop_bh
        r["prop_x"] = r["score_x"] - px(14) - r["prop_w"]
        r["prop_y"] = ry + px(ROW_PADY) + max(0, (r["content_h"] - p_h) // 2)
        od.rounded_rectangle((r["score_x"], r["score_y"], r["score_x"] + r["score_w"],
                              r["score_y"] + s_h), radius=px(12), fill=SCORE_BG)
        od.rounded_rectangle((r["prop_x"], r["prop_y"], r["prop_x"] + r["prop_w"],
                              r["prop_y"] + p_h), radius=px(10), fill=PROPOSER_BG)
        r["bar_y"] = ry + px(ROW_PADY) + r["content_h"] + px(BAR_GAP)
        pct_w = int(od.textlength(r["pct_text"], pct_font))
        r["pct_x"] = x_right + right_w - px(ROW_PADX) - pct_w
        r["track_x1"] = x_right + px(ROW_PADX)
        r["track_x2"] = r["pct_x"] - px(16)
        od.rounded_rectangle((r["track_x1"], r["bar_y"], r["track_x2"], r["bar_y"] + px(BAR_H)),
                             radius=px(BAR_H) // 2, fill=BAR_TRACK)

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # ── 文字与实心进度条 ──
    draw.text((badge_box[0] + px(22), badge_box[1] + (px(BADGE_H) - px(BADGE_FS)) // 2 - px(2)),
              badge_text, font=badge_font, fill=BADGE_TEXT)
    draw.text((x_left, title_y), title, font=title_font, fill=TITLE_COLOR)
    rule_line_y = rules_y + px(RULE_PADY)
    for line, line_font, line_lh in rule_rendered:
        draw.text((x_left + px(RULE_PADX), rule_line_y), line, font=line_font, fill=TEXT)
        rule_line_y += line_lh
    for i, line in enumerate(foot_lines):
        draw.text((x_left, foot_y + i * px(FOOT_LH)), line, font=_font(px(FOOT_FS)), fill=MUTED)

    for r in rows:
        text_block_h = r["name_lh"] * len(r["name_lines"])
        name_y = r["y"] + px(ROW_PADY) + max(0, (r["content_h"] - text_block_h) // 2)
        for li, line in enumerate(r["name_lines"]):
            draw.text((x_right + px(ROW_PADX), name_y + li * r["name_lh"]),
                      line, font=r["name_font"], fill=TITLE_COLOR)
        draw.text((r["score_x"] + px(18), r["score_y"] + (score_bh - px(SCORE_FS)) // 2 - px(2)),
                  r["score_text"], font=score_font, fill=BADGE_TEXT)
        draw.text((r["prop_x"] + px(16), r["prop_y"] + (prop_bh - px(PROP_FS)) // 2 - px(2)),
                  r["prop_text"], font=prop_font, fill=PROPOSER_TEXT)
        fill_w = int((r["track_x2"] - r["track_x1"]) * r["fill_ratio"])
        if fill_w > 0:
            draw.rounded_rectangle((r["track_x1"], r["bar_y"],
                                    r["track_x1"] + fill_w, r["bar_y"] + px(BAR_H)),
                                   radius=px(BAR_H) // 2, fill=BAR_FILL)
        draw.text((r["pct_x"], r["bar_y"] + px(BAR_H) // 2), r["pct_text"],
                  font=pct_font, fill=PCT_COLOR, anchor="lm")

    img.convert("RGB").save(out_png, "PNG")
    return out_png


# ─────────────────────────── 自测 ───────────────────────────
def _sample():
    opts = [
        ("celebrationmk10+too easy+don't dig up", 4.5, 3, "星梦", False),
        ("饥荒+永远下雨+颠倒世界", 0.0, 0, "机器人随机", True),
        ("老版本空岛 附带地牢和神庙", 0.0, 0, "物唤其名 唤之既知 蠕动前行 而来", False),
        ("十周年+醉酒", 2.0, 2, "王[i:3548][i:3548]", False),
        ("灾法 传奇恶意受虐", 8.5, 8, "qq:3528368274", False),
        ("全部秘密种子融合 传奇", 2.0, 2, "幕蝉", False),
        ("十周年+醉酒家回声漆+夜明漆", 9.0, 7, "Ciallo～(∠・ω< )⌒☆", False),
        ("醉酒,ftw,双地牢,吸血鬼,双节日", 4.0, 3, "飘飘散人!", False),
    ]
    total = sum(o[1] for o in opts)
    tally_opts = []
    for i, (name, score, votes, proposer, by_bot) in enumerate(opts):
        tally_opts.append({
            "no": i + 1, "name": name, "proposer": proposer, "by_bot": by_bot,
            "score": score, "votes": votes,
            "percent": round(score / total * 100, 1) if total > 0 else 0.0,
        })
    tally = {"total_score": float(total), "options": tally_opts}

    base = {
        "title": "下个档玩什么，晚上开",
        "created_at": "2025-10-01T10:16:00",
        "deadline": "2025-10-02T10:16:00",
        "winner_no": None,
        "tie_random": False,
    }
    open_vote = dict(base, status="open")
    closed_vote = dict(base, status="closed", winner_no=6, tie_random=True)
    return open_vote, closed_vote, tally


if __name__ == "__main__":
    out_dir = tempfile.mkdtemp(prefix="vote_render_test_")
    open_vote, closed_vote, tally = _sample()
    bg_dir = DEFAULT_BG_DIR
    try:
        names = sorted(f for f in os.listdir(bg_dir)
                       if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
    except OSError:
        names = []
    if not names:
        print(f"[warn] 背景目录为空或不存在：{bg_dir}（仅验证纯色兜底）")
        names = [None]
    for i, nm in enumerate(names):
        for status, vote in (("open", open_vote), ("closed", closed_vote)):
            p = os.path.join(out_dir, f"{i:02d}_{status}.png")
            render_vote_card(p, vote, tally, status=status,
                             bg_file=os.path.join(bg_dir, nm) if nm else None)
            with Image.open(p) as im:
                print(f"{p}  {im.width}x{im.height}")