#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成应用图标：左 WorkBuddy 图标 + 右 CodeBuddy 图标，中间用透明度渐变过渡。

用法：python3 scripts/make_icon.py
依赖：PySide6（QImage 合成）；macOS 的 iconutil（生成 .icns）；
生成 .ico 时需要 Pillow（pip install Pillow）。
源图标取自本机安装的两个客户端；找不到时提示先安装客户端。
"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SOURCES = {
    "workbuddy": Path("/Applications/WorkBuddy.app/Contents/Resources/icon.icns"),
    "codebuddy": Path("/Applications/CodeBuddy CN.app/Contents/Resources/CodeBuddy CN.icns"),
}


def icns_to_png(icns: Path, out_png: Path) -> None:
    """iconutil 解包 icns，取最大尺寸的 png"""
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "x.iconset"
        subprocess.run(["iconutil", "-c", "iconset", str(icns),
                        "-o", str(iconset)], check=True)
        best = None
        for f in sorted(iconset.iterdir()):
            if f.suffix == ".png":
                best = f  # 字典序靠后的是大图
                if "icon_512x512" in f.name:
                    break
        if best is None:
            raise RuntimeError("icns 里没有 png：%s" % icns)
        out_png.write_bytes(best.read_bytes())


def compose(left_png: Path, right_png: Path, out_png: Path, size: int = 1024) -> None:
    """两个原版图标各自满幅铺画布：左半显示左图、右半显示右图、
    中间 24% 宽度做预乘 alpha 交叉淡化（透明像素不带入白色，无白边）。
    不裁边、不留白，和原版图标一样满铺。"""
    from PySide6 import QtCore, QtGui

    def load_full(p):
        img = QtGui.QImage(str(p)).convertToFormat(
            QtGui.QImage.Format.Format_ARGB32)
        return img.scaled(size, size, QtCore.Qt.IgnoreAspectRatio,
                          QtCore.Qt.SmoothTransformation)

    li, ri = load_full(left_png), load_full(right_png)
    lb = bytes(li.bits())
    rb = bytes(ri.bits())
    band = int(size * 0.24)
    x0 = (size - band) // 2
    x1 = x0 + band
    out = bytearray(size * size * 4)

    for y in range(size):
        row = y * size * 4
        for x in range(size):
            o = row + x * 4
            if x <= x0:
                out[o:o + 4] = lb[o:o + 4]
            elif x >= x1:
                out[o:o + 4] = rb[o:o + 4]
            else:
                l4 = lb[o:o + 4]
                r4 = rb[o:o + 4]
                t = (x - x0) / band
                wl = (1.0 - t) * l4[3]
                wr = t * r4[3]
                a = wl + wr
                if a <= 0:
                    continue
                for c in range(3):
                    out[o + c] = min(255, int((l4[c] * wl + r4[c] * wr) / a))
                out[o + 3] = min(255, int(a))

    canvas = QtGui.QImage(bytes(out), size, size, size * 4,
                          QtGui.QImage.Format.Format_ARGB32)
    canvas.save(str(out_png), "PNG")


def make_icns(png: Path, icns: Path) -> None:
    from PySide6 import QtCore, QtGui
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "icon.iconset"
        iconset.mkdir()
        img = QtGui.QImage(str(png))
        for base in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = base * scale
                scaled = img.scaled(px, px, QtCore.Qt.IgnoreAspectRatio,
                                    QtCore.Qt.SmoothTransformation)
                name = "icon_%dx%d.png" % (base, base)
                if scale == 2:
                    name = "icon_%dx%d@2x.png" % (base, base)
                scaled.save(str(iconset / name), "PNG")
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(icns)], check=True)


def make_ico(png: Path, ico: Path) -> None:
    try:
        from PIL import Image
    except ImportError:
        print("跳过 .ico 生成：pip install Pillow 后重跑")
        return
    Image.open(png).save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                                     (128, 128), (256, 256)])


def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    for name, path in SOURCES.items():
        if not path.exists():
            print("找不到 %s 的图标：%s（请先安装客户端）" % (name, path))
            return 1
    left = ASSETS / "_workbuddy.png"
    right = ASSETS / "_codebuddy.png"
    icns_to_png(SOURCES["workbuddy"], left)
    icns_to_png(SOURCES["codebuddy"], right)
    final = ASSETS / "icon_1024.png"
    compose(left, right, final)
    make_icns(final, ASSETS / "icon.icns")
    make_ico(final, ASSETS / "icon.ico")
    for tmp in (left, right):
        tmp.unlink(missing_ok=True)
    print("图标已生成：%s / icon.icns / icon.ico" % final)
    return 0


if __name__ == "__main__":
    sys.exit(main())
