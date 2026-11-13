"""生成应用图标 packaging/app.ico（一次性脚本，产物已入库）。

设计：深色圆角方块 + 白色下载箭头，16px 缩小时仍然认得出。
运行：python packaging/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
BG_TOP = (255, 90, 95)       # 珊瑚红
BG_BOTTOM = (214, 40, 78)    # 深玫红
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
         (128, 128), (256, 256), (512, 512)]


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def build() -> Image.Image:
    ss = SIZE * 2                       # 2x 超采样，边缘更干净
    canvas = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))

    # 渐变底色
    gradient = Image.new("RGB", (ss, ss))
    for y in range(ss):
        t = y / (ss - 1)
        color = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t)
                      for i in range(3))
        gradient.paste(color, (0, y, ss, y + 1))
    canvas.paste(gradient, (0, 0), _rounded_mask(ss, int(ss * 0.22)))

    draw = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)

    # 下载箭头：竖线 + 三角头
    cx = ss / 2
    stem_w = ss * 0.13
    stem_top = ss * 0.20
    stem_bottom = ss * 0.56
    draw.rounded_rectangle(
        (cx - stem_w / 2, stem_top, cx + stem_w / 2, stem_bottom),
        radius=stem_w / 2.6, fill=white)

    head_half = ss * 0.20
    draw.polygon([(cx - head_half, stem_bottom - ss * 0.02),
                  (cx + head_half, stem_bottom - ss * 0.02),
                  (cx, stem_bottom + ss * 0.20)], fill=white)

    # 承接的托盘（下载完成感）
    tray_y = ss * 0.74
    draw.rounded_rectangle(
        (ss * 0.25, tray_y, ss * 0.75, tray_y + ss * 0.11),
        radius=ss * 0.055, fill=white)

    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    icon = build()
    target = Path(__file__).with_name("app.ico")
    icon.save(target, format="ICO", sizes=SIZES)
    png = Path(__file__).with_name("app.png")
    icon.save(png, format="PNG")
    print(f"已生成 {target} 与 {png}")


if __name__ == "__main__":
    main()
