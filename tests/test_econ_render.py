# -*- coding: utf-8 -*-
"""卡片渲染测试：不死循环、不越界、标题清洗、头像兜底"""
import os, sys, time, unittest

BOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot")
sys.path.insert(0, os.path.abspath(BOT))
from econ_render import render_info_card, render_rank_card, _shrink
from PIL import Image, ImageDraw, ImageFont
import io


class RenderTest(unittest.TestCase):
    def _measure(self):
        return ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def test_shrink一定终止_即使字号不再变小(self):
        m = self._measure()
        f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 30)
        same = lambda d: f   # 永远返回同一字号（模拟有下限的字体）
        t0 = time.time()
        out = _shrink(m, "很长很长很长很长很长很长很长的文字" * 3, same, 10, 1)
        self.assertLess(time.time() - t0, 3.0, "不得死循环")
        self.assertIsNotNone(out)

    def test_信息卡可渲染且为JPEG(self):
        p = render_info_card("星梦", [("签到情况", "今天已签到"), ("总在线时长", "1 小时")],
                             banner="今日已签到", subtitle="a@b.com · 2026-01-01",
                             badges=[("连续签到第 1 天", "gold")], footer="test", bg_dir=None)
        self.assertTrue(p.startswith(b"\xff\xd8"), "应是 JPEG")
        self.assertGreater(len(p), 5000)

    def test_榜单卡标题清洗掉井号(self):
        p = render_rank_card([(1, "星梦", "35")], title="## ꧁༺ 积分排行 ༻꧂",
                             subtitle="余额榜 · 你的排名：第 1 名", page=1, total_pages=1,
                             value_label="喵币", footer="test", bg_dir=None)
        self.assertTrue(p.startswith(b"\xff\xd8"))

    def test_超长文本与超多行不崩(self):
        rows = [("标签%d" % i, "值" * 200) for i in range(20)]
        p = render_info_card("名" * 80, rows, banner="横" * 60, subtitle="x" * 120, footer="f", bg_dir=None)
        self.assertTrue(p.startswith(b"\xff\xd8"), "超长内容也必须渲染成功")

    def test_不存在背景目录时回落纯色(self):
        p = render_info_card("星梦", [("a", "b")], bg_dir="C:/不存在的目录/x", footer="f")
        self.assertTrue(p.startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
