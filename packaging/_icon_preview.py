"""图标方案预览（第三版）：渐变粗细的羊角、开口螺旋、Y 形合体。

临时脚本，定稿后删掉。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SS = 1024
BG_TOP = (26, 33, 56)
BG_BOTTOM = (11, 14, 26)
WHITE = (246, 248, 252, 255)


def _bg() -> Image.Image:
    canvas = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    grad = Image.new("RGB", (SS, SS))
    for y in range(SS):
        t = y / (SS - 1)
        grad.paste(tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t)
                         for i in range(3)), (0, y, SS, y + 1))
    mask = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SS - 1, SS - 1),
                                           radius=int(SS * 0.22), fill=255)
    canvas.paste(grad, (0, 0), mask)
    return canvas


def _bezier2(p0, p1, p2, n=24):
    return [( (1-t)**2*p0[0] + 2*(1-t)*t*p1[0] + t*t*p2[0],
              (1-t)**2*p0[1] + 2*(1-t)*t*p1[1] + t*t*p2[1])
            for t in (i / n for i in range(n + 1))]


def _bezier3(p0, p1, p2, p3, n=56):
    return [( (1-t)**3*p0[0] + 3*(1-t)**2*t*p1[0] + 3*(1-t)*t*t*p2[0] + t**3*p3[0],
              (1-t)**3*p0[1] + 3*(1-t)**2*t*p1[1] + 3*(1-t)*t*t*p2[1] + t**3*p3[1])
            for t in (i / n for i in range(n + 1))]


def _arc(cx, cy, rx, ry, a0, a1, n=48, mirror_x=None):
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / n)
        x = cx + rx * math.cos(a)
        if mirror_x is not None:
            x = SS - x
        pts.append((x, cy + ry * math.sin(a)))
    return pts


def _stroke(draw, pts, width, fill=WHITE, width_end=None):
    """打点法：沿线密集打圆，边缘平滑；支持首尾不同粗细（画羊角）。"""
    width_end = width if width_end is None else width_end
    lens = [0.0]
    for a, b in zip(pts, pts[1:]):
        lens.append(lens[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = lens[-1] or 1.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        w0 = width + (width_end - width) * (lens[i] / total)
        w1 = width + (width_end - width) * (lens[i + 1] / total)
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(seg / (min(w0, w1) / 6)))
        for k in range(n + 1):
            t = k / n
            qx, qy = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            w = w0 + (w1 - w0) * t
            draw.ellipse((qx - w / 2, qy - w / 2, qx + w / 2, qy + w / 2), fill=fill)


def _rounded_poly(draw, pts, radius, fill=WHITE, curve_steps=16):
    n = len(pts)
    path = []
    for i in range(n):
        prev, cur, nxt = pts[i - 1], pts[i], pts[(i + 1) % n]
        v1 = (prev[0] - cur[0], prev[1] - cur[1])
        v2 = (nxt[0] - cur[0], nxt[1] - cur[1])
        l1, l2 = math.hypot(*v1) or 1, math.hypot(*v2) or 1
        r = min(radius, l1 * 0.46, l2 * 0.46)
        p1 = (cur[0] + v1[0] / l1 * r, cur[1] + v1[1] / l1 * r)
        p2 = (cur[0] + v2[0] / l2 * r, cur[1] + v2[1] / l2 * r)
        path.extend(_bezier2(p1, cur, p2, curve_steps))
    draw.polygon(path, fill=fill)


def _P(x, y):
    return (SS * x, SS * y)


def _arrow(draw, top, bottom, stem_w, head_w, head_h, fill=WHITE):
    cx = SS / 2
    base = bottom - head_h
    draw.rounded_rectangle((cx - stem_w / 2, top, cx + stem_w / 2, base + stem_w * 0.3),
                           radius=stem_w / 2.2, fill=fill)
    draw.polygon([(cx - head_w / 2, base), (cx + head_w / 2, base), (cx, bottom)], fill=fill)


# ---------- K：渐变粗细的逗号角（最像真羊角）+ 箭头 ----------
def variant_comma() -> Image.Image:
    img = _bg()
    d = ImageDraw.Draw(img)
    for sx in (-1, 1):
        pts = _bezier3((SS * (0.5 + sx * 0.075), SS * 0.245),
                       (SS * (0.5 + sx * 0.36), SS * 0.20),
                       (SS * (0.5 + sx * 0.375), SS * 0.50),
                       (SS * (0.5 + sx * 0.185), SS * 0.515))
        _stroke(d, pts, SS * 0.115, width_end=SS * 0.030)
    _arrow(d, SS * 0.44, SS * 0.865, SS * 0.082, SS * 0.335, SS * 0.185)
    return img


# ---------- L：圆润羊脸 + 开口内勾角 ----------
def variant_round_face() -> Image.Image:
    img = _bg()
    d = ImageDraw.Draw(img)
    _rounded_poly(d, [_P(0.245, 0.415), _P(0.755, 0.415), _P(0.5, 0.805)],
                  radius=SS * 0.155)
    hole = Image.new("L", (SS, SS), 255)
    hd = ImageDraw.Draw(hole)
    for sx in (-1, 1):
        cx = SS * (0.5 + sx * 0.077)
        hd.ellipse((cx - SS * 0.031, SS * 0.492, cx + SS * 0.031, SS * 0.554), fill=0)
    img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (SS, SS), 0), hole))
    w = SS * 0.072
    for sx in (-1, 1):
        pts = _bezier3((SS * (0.5 + sx * 0.175), SS * 0.435),
                       (SS * (0.5 + sx * 0.345), SS * 0.40),
                       (SS * (0.5 + sx * 0.365), SS * 0.155),
                       (SS * (0.5 + sx * 0.16), SS * 0.15))
        pts += _bezier2(pts[-1], (SS * (0.5 + sx * 0.075), SS * 0.15),
                        (SS * (0.5 + sx * 0.085), SS * 0.245))[1:]
        _stroke(d, pts, w)
    return img


# ---------- M：开口螺旋角 + 箭头 ----------
def variant_open_spiral() -> Image.Image:
    img = _bg()
    d = ImageDraw.Draw(img)
    for sx in (-1, 1):
        cx = SS * (0.5 + sx * 0.205)
        pts = _arc(cx, SS * 0.28, SS * 0.135, SS * 0.135, -80, -300,
                   n=64, mirror_x=None if sx > 0 else 0)
        _stroke(d, pts, SS * 0.078, width_end=SS * 0.042)
    _arrow(d, SS * 0.38, SS * 0.865, SS * 0.082, SS * 0.335, SS * 0.185)
    return img


# ---------- N：Y 形合体（羊的首字母 + 向下箭头） ----------
def variant_y() -> Image.Image:
    img = _bg()
    d = ImageDraw.Draw(img)
    w = SS * 0.088
    # 上两笔末端微微内勾，像羊角
    for sx in (-1, 1):
        pts = _bezier3((SS * 0.5, SS * 0.475),
                       (SS * (0.5 + sx * 0.10), SS * 0.335),
                       (SS * (0.5 + sx * 0.185), SS * 0.245),
                       (SS * (0.5 + sx * 0.275), SS * 0.215))
        pts += _bezier2(pts[-1], (SS * (0.5 + sx * 0.315), SS * 0.205),
                        (SS * (0.5 + sx * 0.31), SS * 0.28))[1:]
        _stroke(d, pts, w)
    _arrow(d, SS * 0.455, SS * 0.875, SS * 0.088, SS * 0.345, SS * 0.19)
    return img


# ---------- O：Y 形 + 收窄的腰身（更像羊头轮廓） ----------
def variant_y_taper() -> Image.Image:
    img = _bg()
    d = ImageDraw.Draw(img)
    for sx in (-1, 1):
        pts = _bezier3((SS * 0.5, SS * 0.46),
                       (SS * (0.5 + sx * 0.115), SS * 0.32),
                       (SS * (0.5 + sx * 0.20), SS * 0.235),
                       (SS * (0.5 + sx * 0.30), SS * 0.215))
        _stroke(d, pts, SS * 0.10, width_end=SS * 0.062)
        tip = _bezier2(pts[-1], (SS * (0.5 + sx * 0.335), SS * 0.205),
                       (SS * (0.5 + sx * 0.325), SS * 0.285))
        _stroke(d, tip, SS * 0.062, width_end=SS * 0.028)
    _arrow(d, SS * 0.44, SS * 0.885, SS * 0.092, SS * 0.355, SS * 0.195)
    return img


# ---------- P：公羊头（角从头顶向外、向下、向内卷） ----------
def _ram_head(angle_thickness: float, face_radius: float, eye_r: float) -> Image.Image:
    img = _bg()
    d = ImageDraw.Draw(img)
    # 脸：上宽下窄，圆角
    _rounded_poly(d, [_P(0.315, 0.415), _P(0.685, 0.415), _P(0.5, 0.815)],
                  radius=SS * face_radius)
    # 眼睛
    hole = Image.new("L", (SS, SS), 255)
    hd = ImageDraw.Draw(hole)
    for sx in (-1, 1):
        cx = SS * (0.5 + sx * 0.072)
        hd.ellipse((cx - SS * eye_r, SS * 0.505, cx + SS * eye_r, SS * 0.505 + SS * eye_r * 2),
                   fill=0)
    img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (SS, SS), 0), hole))

    w = SS * angle_thickness
    for sx in (-1, 1):
        # 头顶 -> 向上 -> 向外下卷 -> 末端向内勾
        pts = _bezier3((SS * (0.5 + sx * 0.155), SS * 0.435),
                       (SS * (0.5 + sx * 0.175), SS * 0.195),
                       (SS * (0.5 + sx * 0.395), SS * 0.175),
                       (SS * (0.5 + sx * 0.405), SS * 0.345))
        pts += _bezier2(pts[-1], (SS * (0.5 + sx * 0.408), SS * 0.415),
                        (SS * (0.5 + sx * 0.335), SS * 0.425))[1:]
        _stroke(d, pts, w, width_end=w * 0.72)
    return img


def variant_ram() -> Image.Image:
    return _ram_head(0.072, 0.115, 0.030)


def variant_ram_bold() -> Image.Image:
    return _ram_head(0.092, 0.075, 0.034)


def variant_ram_open() -> Image.Image:
    """角与脸分离一点，避免小尺寸糊成一团。"""
    img = _bg()
    d = ImageDraw.Draw(img)
    _rounded_poly(d, [_P(0.335, 0.43), _P(0.665, 0.43), _P(0.5, 0.80)],
                  radius=SS * 0.10)
    hole = Image.new("L", (SS, SS), 255)
    hd = ImageDraw.Draw(hole)
    for sx in (-1, 1):
        cx = SS * (0.5 + sx * 0.068)
        hd.ellipse((cx - SS * 0.028, SS * 0.512, cx + SS * 0.028, SS * 0.568), fill=0)
    img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (SS, SS), 0), hole))
    w = SS * 0.078
    for sx in (-1, 1):
        pts = _bezier3((SS * (0.5 + sx * 0.175), SS * 0.445),
                       (SS * (0.5 + sx * 0.20), SS * 0.185),
                       (SS * (0.5 + sx * 0.415), SS * 0.19),
                       (SS * (0.5 + sx * 0.425), SS * 0.375))
        pts += _bezier2(pts[-1], (SS * (0.5 + sx * 0.425), SS * 0.445),
                        (SS * (0.5 + sx * 0.345), SS * 0.45))[1:]
        _stroke(d, pts, w, width_end=w * 0.66)
    return img


VARIANTS = {
    "L-圆脸": variant_round_face,
    "P-公羊头": variant_ram,
    "Q-粗角羊": variant_ram_bold,
    "R-分离羊": variant_ram_open,
    "N-Y形": variant_y,
}


def main() -> None:
    out = Path(__file__).with_name("_preview")
    out.mkdir(exist_ok=True)
    tiles = []
    for name, fn in VARIANTS.items():
        img = fn().resize((256, 256), Image.LANCZOS)
        img.save(out / f"{name}.png")
        tiles.append((name, img, img.resize((32, 32), Image.LANCZOS)))

    gap = 16
    width = len(tiles) * (256 + gap) + gap
    sheet = Image.new("RGB", (width, 256 + 64 + 3 * gap), (34, 34, 38))
    for i, (_, img, small) in enumerate(tiles):
        x = gap + i * (256 + gap)
        sheet.paste(img, (x, gap), img)
        sheet.paste(small, (x + 112, 256 + 2 * gap), small)
    path = out / "对比3.png"
    sheet.save(path)
    print("已生成:", path, "| 顺序:", " / ".join(n for n, _, _ in tiles))


if __name__ == "__main__":
    main()
