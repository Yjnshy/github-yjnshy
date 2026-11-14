"""生成应用图标 packaging/app.ico（产物已入库）。

默认方案 ``photo``：以 ``icon_source.jpg``（已去水印的白底插画）为源，
把白底抠成透明、居中，再生成 7 档尺寸。16/24px 会额外加粗外轮廓。

其余 ``wool*`` / ``ram*`` 方案是早期程序化绘制的候选，留作对照。
桌面端始终是透明背景，没有深色底板。

用法::

    python packaging/make_icon.py                 # 默认：插画方案
    python packaging/make_icon.py --variant wool  # 换成旧的云朵羊毛球
    python packaging/make_icon.py --sheet         # 额外输出各尺寸对比图
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageChops

SS = 1024                                   # 超采样画布
BG_TOP = (26, 33, 56)                       # 深墨蓝
BG_BOTTOM = (11, 14, 26)                    # 近黑
WHITE = (246, 248, 252, 255)
CREAM = (255, 201, 125, 255)                # 羊角
DARK = (16, 23, 40, 255)                    # 眼睛
BLUSH = (246, 191, 196, 255)                # 小粉鼻
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


def _circle(draw, cx: float, cy: float, r: float, fill=WHITE) -> None:
    draw.ellipse((SS * (cx - r), SS * (cy - r), SS * (cx + r), SS * (cy + r)), fill=fill)


def _wool(draw, cx: float, cy: float, r: float) -> None:
    """云朵羊毛球：一个主圆 + 沿外缘一圈小圆，做出蓬松的轮廓。"""
    for k in range(9):
        a = math.pi * 2 * k / 9 - math.pi / 2
        _circle(draw, cx + math.cos(a) * (r - 0.015),
                cy + math.sin(a) * (r - 0.015), r * 0.37)
    _circle(draw, cx, cy, r)


def _curl(draw, sx: int, x0: float, y0: float, span: float,
          thick: float = 0.056) -> None:
    """羊角：从头顶向外张开、向上弯，末端内勾的开口钩（不闭合）。"""
    x = 0.5 + sx * x0
    pts = _bezier3((SS * x, SS * y0),
                   (SS * (x + sx * span * 0.62), SS * (y0 - 0.020)),
                   (SS * (x + sx * span * 0.78), SS * (y0 - 0.145)),
                   (SS * (x + sx * span * 0.42), SS * (y0 - 0.152)))
    pts += _bezier2(pts[-1], (SS * (x + sx * span * 0.20), SS * (y0 - 0.150)),
                    (SS * (x + sx * span * 0.18), SS * (y0 - 0.100)))[1:]
    _stroke(draw, pts, SS * thick, fill=CREAM, width_end=SS * thick * 0.66)


def _transparent() -> Image.Image:
    return Image.new("RGBA", (SS, SS), (0, 0, 0, 0))


def _outline(img: Image.Image, ratio: float = 0.008,
             color: tuple = (26, 33, 56, 255)) -> Image.Image:
    """沿不透明像素外扩一圈描边。

    纯透明的白羊在浅色桌面/资源管理器白底上会「消失」，描边是让它在
    任何背景上都立得住的唯一办法。做法：alpha 通道做形态学膨胀当遮罩。
    """
    k = int(SS * ratio) * 2 + 1
    grown = img.getchannel("A").filter(ImageFilter.MaxFilter(k))
    layer = Image.new("RGBA", img.size, color)
    layer.putalpha(grown)
    layer.alpha_composite(img)
    return layer


def _centered(img: Image.Image, pad: float = 0.07) -> Image.Image:
    """按实际像素范围裁切后居中，四周留 pad 比例的空白。

    透明图标没有底板兜着，图形偏一点就会显得歪，所以统一归一化：
    裁到内容外框 -> 放进正方形 -> 重新缩放回 SS。
    """
    bbox = img.getbbox()
    if not bbox:
        return img
    content = img.crop(bbox)
    w, h = content.size
    side = int(max(w, h) * (1 + 2 * pad))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(content, ((side - w) // 2, (side - h) // 2), content)
    return canvas.resize((SS, SS), Image.LANCZOS)


def _wool_sheep(eye_r: float = 0.030, horn: float = 0.056, r: float = 0.205,
                nose: bool = True, eye_dx: float = 0.072,
                bg: bool = True) -> Image.Image:
    """云朵羊毛球羊头：金角 + 白毛球 + 黑眼 + 粉鼻。bg=False 时背景透明。"""
    img = _background() if bg else _transparent()
    d = ImageDraw.Draw(img)
    for sx in (-1, 1):
        _curl(d, sx, 0.120, 0.480, 0.240, thick=horn)
    _wool(d, 0.5, 0.615, r)
    _circle(d, 0.5 - eye_dx, 0.600, eye_r, DARK)
    _circle(d, 0.5 + eye_dx, 0.600, eye_r, DARK)
    if nose:
        d.ellipse((SS * (0.5 - 0.024), SS * (0.668 - 0.018),
                   SS * (0.5 + 0.024), SS * (0.668 + 0.018)), fill=BLUSH)
    return img


def variant_wool() -> Image.Image:
    """云朵羊毛球（默认，透明底 + 细描边）：没有底板，但浅底上也看得见。"""
    return _centered(_outline(_wool_sheep(bg=False)))


def variant_wool_plain() -> Image.Image:
    """云朵羊毛球（纯透明、无描边）：最干净，只适合深色背景。"""
    return _centered(_wool_sheep(bg=False))


def variant_wool_dark() -> Image.Image:
    """云朵羊毛球（深底）：留一圈深墨蓝圆角底，深色桌面上更聚焦。"""
    return _wool_sheep()


# ---------------- 插画方案：白底源图 → 透明背景 ----------------

SOURCE_IMAGE = Path(__file__).parent / "icon_source.jpg"
PHOTO_PAD = 0.06                            # 居中后四周留白比例


def _premul_resize(img: Image.Image, size: tuple) -> Image.Image:
    """预乘 alpha 后再缩放，避免透明像素里的白色渗进轮廓形成白边。"""
    return img.convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")


def _outside_mask(wall: Image.Image) -> bytearray:
    """从四边做 span flood fill，标出「深色轮廓之外」的像素。

    羊毛内部也是白的，按颜色直接抠图会把羊身一起抠空；所以把深色描边
    当墙，只删掉与画面外连通的部分，羊身、腮红、羊角内部都保得住。
    """
    w, h = wall.size
    data = wall.tobytes()                    # 255 = 墙，0 = 可通行
    outside = bytearray(w * h)
    stack = []

    def seed(x: int, y: int) -> None:
        i = y * w + x
        if data[i] == 0 and not outside[i]:
            outside[i] = 1
            stack.append((x, y))

    for x in range(w):
        seed(x, 0)
        seed(x, h - 1)
    for y in range(h):
        seed(0, y)
        seed(w - 1, y)

    while stack:
        x, y = stack.pop()
        row = y * w
        left = x
        while left > 0 and data[row + left - 1] == 0 and not outside[row + left - 1]:
            left -= 1
        right = x
        while (right < w - 1 and data[row + right + 1] == 0
               and not outside[row + right + 1]):
            right += 1
        outside[row + left:row + right + 1] = b"\x01" * (right - left + 1)
        for ny in (y - 1, y + 1):
            if not 0 <= ny < h:
                continue
            nrow = ny * w
            k = left
            while k <= right:
                if data[nrow + k] == 0 and not outside[nrow + k]:
                    outside[nrow + k] = 1
                    stack.append((k, ny))
                    while k <= right and data[nrow + k] == 0:
                        k += 1
                else:
                    k += 1
    return outside


def _from_source() -> Image.Image:
    """读去水印后的插画 → 抠掉白底 → 按可见内容居中成正方形。"""
    src = Image.open(SOURCE_IMAGE).convert("RGB")
    # 把羊毛里 JPEG 带来的淡灰噪点洗成纯白：只动「近中性 + 高亮」的像素，
    # 腮红、蓝眼、橙色羊角都有明显色差，不受影响
    r, g, b = src.split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    neutral = ImageChops.subtract(mx, mn).point(lambda v: 255 if v < 14 else 0)
    bright = src.convert("L").point(lambda v: 255 if v > 225 else 0)
    clean = ImageChops.multiply(neutral, bright)
    src = Image.composite(Image.new("RGB", src.size, (255, 255, 255)),
                          src, clean)

    gray = src.convert("L")
    # 深色描边当墙；MaxFilter 膨胀几像素，封住抗锯齿处的浅色细缝
    wall = gray.point(lambda v: 255 if v < 170 else 0)
    wall = wall.filter(ImageFilter.MaxFilter(5))
    outside = _outside_mask(wall)

    alpha = Image.frombytes("L", src.size,
                            bytes(0 if v else 255 for v in outside))
    # 膨胀出的「墙」会在深蓝描边外侧留一圈白底，腐蚀 2px 正好去掉它，
    # 描边本身约 10px 厚，不会受损
    alpha = alpha.filter(ImageFilter.MinFilter(5))
    rgba = src.convert("RGBA")
    rgba.putalpha(alpha)

    # 注意：RGBA.getbbox() 会把透明像素的白色 RGB 也算成内容，必须看 alpha
    bbox = alpha.getbbox()
    if not bbox:
        raise RuntimeError("源图抠图失败：没有留下任何不透明像素")
    content = rgba.crop(bbox)
    w, h = content.size
    side = int(max(w, h) * (1 + 2 * PHOTO_PAD))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(content, ((side - w) // 2, (side - h) // 2), content)
    return _premul_resize(canvas, (SS, SS))


def variant_photo() -> Image.Image:
    """去水印插画（默认）：透明底，白毛球 + 深蓝描边。"""
    return _from_source()


def _small_photo() -> Image.Image:
    """16/24px 专用：抹掉会糊成一团的鼻子和嘴，只留两颗拉开的眼，再加粗外轮廓。"""
    side = 128
    base = _premul_resize(_from_source(), (side, side))

    # 只在不透明区域里把脸的下半部洗白（眼睛、鼻子、嘴、腮红都清掉）
    patch = Image.new("RGBA", base.size, (255, 255, 255, 255))
    box = (int(side * .28), int(side * .45),
           int(side * .72), int(side * .78))
    mask = Image.new("L", base.size, 0)
    ImageDraw.Draw(mask).rectangle(box, fill=255)
    mask = ImageChops.multiply(mask, base.getchannel("A"))
    base.paste(patch, (0, 0), mask)

    # 两颗眼睛：眼距 0.21 画布宽，16px 下也各占一格、中间留空
    d = ImageDraw.Draw(base)
    eye_r = side * 0.045
    for dx in (-0.105, 0.105):
        cx, cy = (0.5 + dx) * side, 0.515 * side
        d.ellipse((cx - eye_r, cy - eye_r, cx + eye_r, cy + eye_r),
                  fill=(16, 32, 60, 255))

    alpha = base.getchannel("A")
    ring = ImageChops.subtract(alpha.filter(ImageFilter.MaxFilter(3)), alpha)
    ink = Image.new("RGBA", base.size, (26, 38, 64, 0))
    ink.putalpha(ring)
    return Image.alpha_composite(ink, base)


VARIANTS = {
    "photo": variant_photo,
    "wool": variant_wool,
    "wool-plain": variant_wool_plain,
    "wool-dark": variant_wool_dark,
    "ram-bold": variant_ram_bold,
    "ram": variant_ram,
    "round": variant_round,
}
DEFAULT_VARIANT = "photo"


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
    """极小尺寸专用（粗角羊）：去掉眼睛、加粗羊角，避免缩小时变成一团噪点。"""
    return _ram(thickness=0.122, face_radius=0.05, face_half=0.19, eye=0)


def _small_wool(bg: bool = True) -> Image.Image:
    """极小尺寸专用（羊毛球）：去掉粉鼻、加粗羊角、眼距拉开。

    16/24px 下粉鼻不到 1px，留着只会让脸发脏，去掉。眼睛不能放大：
    直径超过 1px 且间距不足时，抗锯齿会把两只眼睛连成一条横杠。
    因此缩到 0.034 并把眼距拉到 0.090，每个点各占一个像素、中间留空。
    """
    return _wool_sheep(eye_r=0.034, horn=0.082, r=0.212, nose=False,
                       eye_dx=0.090, bg=bg)


SMALL_ICONS = {
    "photo": _small_photo,
    "wool": lambda: _centered(_outline(_small_wool(bg=False)), pad=0.09),
    "wool-plain": lambda: _centered(_small_wool(bg=False), pad=0.10),
    "wool-dark": _small_wool,
    "ram-bold": _small_icon,
    "ram": _small_icon,
    "round": _small_icon,
}


def save_ico(path: Path, full: Image.Image, small: Image.Image | None = None) -> None:
    """手工写 ICO 容器。

    Pillow 自带的 ICO 保存只能「从同一张大图缩出所有尺寸」，做不到给
    16/24px 换一套简化图，所以这里自己拼容器（PNG 压缩格式，Vista+ 支持）。
    """
    import io
    import struct

    plan = {16: small, 24: small,
            32: full, 48: full, 64: full, 128: full, 256: full}
    plan = {k: (v if v is not None else full) for k, v in plan.items()}

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
    save_ico(ico, full, SMALL_ICONS[args.variant]())
    full.resize((512, 512), Image.LANCZOS).save(png, format="PNG")
    # 界面左上角也用同一张图，保证窗口里和桌面图标是同一只羊
    logo = here.parent / "src" / "dly" / "gui" / "assets" / "logo.png"
    _premul_resize(full, (160, 160)).save(logo, format="PNG")
    print(f"已生成 {ico}、{png} 与界面 logo（方案 {args.variant}）")
    if args.sheet:
        path = here / "icon_sizes.png"
        size_sheet(full.resize((512, 512), Image.LANCZOS)).save(path)
        print(f"尺寸对比：{path}")


if __name__ == "__main__":
    main()
