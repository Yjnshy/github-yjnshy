"""生成应用图标 packaging/app.ico（产物已入库）。

设计：深色圆角底 + 白色羊头几何符号（双角 + 倒三角脸 + 负空间眼睛）。
羊头同时暗示「向下」—— 底部的尖就是下载方向，16px 缩小时仍认得出。

用法::

    python packaging/make_icon.py                 # 默认方案（粗角羊）
    python packaging/make_icon.py --variant round # 换成圆脸萌版
    python packaging/make_icon.py --sheet         # 额外输出各尺寸对比图
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

SS = 1024                                   # 超采样画布
BG_TOP = (26, 33, 56)                       # 深墨蓝
BG_BOTTOM = (11, 14, 26)                    # 近黑
WHITE = (246, 248, 252, 255)
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
         (128, 128), (256, 256), (512, 512)]


# ---------------- 基础绘制 ----------------

def _background() -> Image.Image:
    canvas = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    grad = Image.new("RGB", (SS, SS))
    for y in range(SS):
        t = y / (SS - 1)
        grad.paste(tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t)
                         for i in range(3)), (0, y, SS, y + 1))
    mask = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, SS - 1, SS - 1), radius=int(SS * 0.22), fill=255)
    canvas.paste(grad, (0, 0), mask)
    return canvas


def _bezier3(p0, p1, p2, p3, n=56):
    return [(
        (1-t)**3*p0[0] + 3*(1-t)**2*t*p1[0] + 3*(1-t)*t*t*p2[0] + t**3*p3[0],
        (1-t)**3*p0[1] + 3*(1-t)**2*t*p1[1] + 3*(1-t)*t*t*p2[1] + t**3*p3[1],
    ) for t in (i / n for i in range(n + 1))]


def _bezier2(p0, p1, p2, n=24):
    return [(
        (1-t)**2*p0[0] + 2*(1-t)*t*p1[0] + t*t*p2[0],
        (1-t)**2*p0[1] + 2*(1-t)*t*p1[1] + t*t*p2[1],
    ) for t in (i / n for i in range(n + 1))]


def _stroke(draw, pts, width, fill=WHITE, width_end=None):
    """打点法画粗线：沿线密集打圆，边缘平滑，且支持首尾粗细渐变。

    不用 ``draw.line(joint="curve")``：超采样下拼接处会出现毛刺。
    """
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
        steps = max(1, int(seg / (min(w0, w1) / 6)))
        for k in range(steps + 1):
            t = k / steps
            qx, qy = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            w = w0 + (w1 - w0) * t
            draw.ellipse((qx - w / 2, qy - w / 2, qx + w / 2, qy + w / 2), fill=fill)


def _rounded_poly(draw, pts, radius, fill=WHITE, steps=16):
    """圆角多边形：每个顶点用二次贝塞尔倒角。"""
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
        path.extend(_bezier2(p1, cur, p2, steps))
    draw.polygon(path, fill=fill)


def _P(x: float, y: float) -> tuple:
    return (SS * x, SS * y)


def _punch_eyes(img: Image.Image, cx_offsets, cy, r) -> None:
    """在白色脸面上挖两个负空间眼睛。"""
    hole = Image.new("L", (SS, SS), 255)
    hd = ImageDraw.Draw(hole)
    for sx in cx_offsets:
        cx = SS * (0.5 + sx)
        hd.ellipse((cx - SS * r, SS * cy, cx + SS * r, SS * cy + SS * r * 2), fill=0)
    img.putalpha(Image.composite(img.getchannel("A"),
                                 Image.new("L", (SS, SS), 0), hole))


# ---------------- 方案 ----------------

def _ram(thickness: float, face_radius: float, face_half: float,
         eye: float, eye_y: float = 0.505) -> Image.Image:
    """羊头：倒三角脸 + 从头顶向上外张、末端内勾的双角。"""
    img = _background()
    d = ImageDraw.Draw(img)
    _rounded_poly(d, [_P(0.5 - face_half, 0.415), _P(0.5 + face_half, 0.415),
                      _P(0.5, 0.815)], radius=SS * face_radius)
    _punch_eyes(img, (-0.072, 0.072), eye_y, eye)

    w = SS * thickness
    for sx in (-1, 1):
        pts = _bezier3((SS * (0.5 + sx * 0.155), SS * 0.435),
                       (SS * (0.5 + sx * 0.175), SS * 0.195),
                       (SS * (0.5 + sx * 0.395), SS * 0.175),
                       (SS * (0.5 + sx * 0.405), SS * 0.345))
        pts += _bezier2(pts[-1], (SS * (0.5 + sx * 0.408), SS * 0.415),
                        (SS * (0.5 + sx * 0.335), SS * 0.425))[1:]
        _stroke(d, pts, w, width_end=w * 0.72)
    return img


def variant_ram_bold() -> Image.Image:
    """粗角羊（默认）：角壮、脸小，小尺寸下最稳。"""
    return _ram(thickness=0.092, face_radius=0.075, face_half=0.185, eye=0.034)


def variant_ram() -> Image.Image:
    """细角羊：更秀气，大尺寸下细节更好看。"""
    return _ram(thickness=0.072, face_radius=0.115, face_half=0.185, eye=0.030)


def variant_round() -> Image.Image:
    """圆脸萌版：角向内卷成圆环，整体偏可爱。"""
    img = _background()
    d = ImageDraw.Draw(img)
    _rounded_poly(d, [_P(0.245, 0.415), _P(0.755, 0.415), _P(0.5, 0.805)],
                  radius=SS * 0.155)
    _punch_eyes(img, (-0.077, 0.077), 0.492, 0.031)

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


VARIANTS = {
    "ram-bold": variant_ram_bold,
    "ram": variant_ram,
    "round": variant_round,
}
DEFAULT_VARIANT = "ram-bold"


def build(variant: str = DEFAULT_VARIANT) -> Image.Image:
    if variant not in VARIANTS:
        raise KeyError(f"未知方案 {variant!r}，可选：{', '.join(VARIANTS)}")
    return VARIANTS[variant]().resize((512, 512), Image.LANCZOS)


def size_sheet(icon: Image.Image) -> Image.Image:
    """各尺寸并排，检查缩小后是否还认得出。"""
    previews = [(s, icon.resize((s, s), Image.LANCZOS)) for s in (16, 24, 32, 48, 64, 128)]
    gap = 14
    width = sum(s for s, _ in previews) + gap * (len(previews) + 1)
    sheet = Image.new("RGB", (width, 128 + 2 * gap), (34, 34, 38))
    x = gap
    for s, img in previews:
        sheet.paste(img, (x, gap + (128 - s) // 2), img)
        x += s + gap
    return sheet


def _small_icon() -> Image.Image:
    """极小尺寸专用：去掉眼睛、加粗羊角，避免缩小时变成一团噪点。"""
    return _ram(thickness=0.122, face_radius=0.05, face_half=0.19, eye=0)


def save_ico(path: Path, full: Image.Image) -> None:
    """手工写 ICO 容器。

    Pillow 自带的 ICO 保存只能「从同一张大图缩出所有尺寸」，做不到给
    16/24px 换一套简化图，所以这里自己拼容器（PNG 压缩格式，Vista+ 支持）。
    """
    import io
    import struct

    small = _small_icon()
    plan = {16: small, 24: small,
            32: full, 48: full, 64: full, 128: full, 256: full}

    blobs, entries, offset = [], [], 6 + 16 * len(plan)
    for size in sorted(plan):
        img = plan[size]
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        dim = 0 if size >= 256 else size          # 0 表示 256
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                   len(data), offset))
        offset += len(data)
        blobs.append(data)

    header = struct.pack("<HHH", 0, 1, len(plan))
    path.write_bytes(header + b"".join(entries) + b"".join(blobs))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="生成应用图标")
    parser.add_argument("--variant", default=DEFAULT_VARIANT,
                        choices=list(VARIANTS), help="图标方案")
    parser.add_argument("--sheet", action="store_true", help="额外输出各尺寸对比图")
    args = parser.parse_args(argv)

    full = VARIANTS[args.variant]()               # 1024 超采样原图
    here = Path(__file__).parent
    ico = here / "app.ico"
    png = here / "app.png"
    save_ico(ico, full)
    full.resize((512, 512), Image.LANCZOS).save(png, format="PNG")
    print(f"已生成 {ico} 与 {png}（方案 {args.variant}）")
    if args.sheet:
        path = here / "icon_sizes.png"
        size_sheet(full.resize((512, 512), Image.LANCZOS)).save(path)
        print(f"尺寸对比：{path}")


if __name__ == "__main__":
    main()
