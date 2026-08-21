# -*- coding: utf-8 -*-
"""生成占位应用图标 app.ico（文件夹 + 同步双箭头主题）。

依赖 Pillow（仅打包期需要，运行期不依赖）。生成多尺寸 16/32/48/64/128/256，
Windows 端 PyInstaller 会读取该 .ico 作为 exe 图标；Linux 端仅作占位。

Pillow 为惰性导入（仅在 make_icon() 内）：缺 Pillow 时模块可正常导入，
调用 make_icon() 才抛 ImportError——build_exe 会捕获并给出安装提示，
而不是让打包脚本裸崩溃。

用法：
    python make_icon.py            # 生成 app.ico 到当前目录
    python make_icon.py 其它.ico   # 指定输出路径
"""

import os
import sys
from typing import Any

SIZES = [16, 32, 48, 64, 128, 256]


def _draw_icon(size):
    # type: (int) -> Any
    """按给定尺寸画一个「黄色文件夹 + 蓝绿同步双箭头」的透明底图标。"""
    from PIL import Image, ImageDraw  # 惰性导入：仅生成图标时才需要 Pillow

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 颜色
    folder = (240, 180, 40, 255)
    folder_dark = (196, 138, 20, 255)
    blue = (40, 120, 220, 255)
    green = (40, 180, 90, 255)

    def sc(v):
        # type: (float) -> int
        """把 0..1 的相对坐标缩放到当前尺寸。"""
        return max(0, min(size, int(round(v * size))))

    lw = max(1, size // 16)  # 弧线宽

    # 文件夹：顶盖标签 + 主体
    d.rectangle([sc(0.16), sc(0.30), sc(0.56), sc(0.42)], fill=folder_dark)
    d.rectangle([sc(0.12), sc(0.40), sc(0.84), sc(0.76)], fill=folder)

    # 上弧（绿色，左→右）+ 右端箭头尖
    d.arc([sc(0.20), sc(0.08), sc(0.80), sc(0.48)], start=205, end=335,
          fill=green, width=lw)
    d.polygon([(sc(0.78), sc(0.26)), (sc(0.78), sc(0.38)), (sc(0.90), sc(0.32))],
              fill=green)

    # 下弧（蓝色，右→左）+ 左端箭头尖
    d.arc([sc(0.20), sc(0.52), sc(0.80), sc(0.92)], start=25, end=155,
          fill=blue, width=lw)
    d.polygon([(sc(0.22), sc(0.62)), (sc(0.22), sc(0.74)), (sc(0.10), sc(0.68))],
              fill=blue)

    return img


def make_icon(path="app.ico"):
    # type: (str) -> str
    imgs = [_draw_icon(s) for s in SIZES]
    # 以最大尺寸为基准图，其余作为附加尺寸写入同一 .ico（各图保留自身尺寸）
    imgs[-1].save(path, format="ICO", append_images=imgs[:-1])
    return os.path.abspath(path)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "app.ico"
    p = make_icon(out)
    print("已生成图标: %s（尺寸 %s）" % (p, ", ".join(str(s) for s in SIZES)))
