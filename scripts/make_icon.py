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


def _fade_mask(img, keep_left: bool, x0: int, x1: int, size: int):
    """用渐变遮罩改透明度：keep_left 时左实右虚（中间线性过渡），否则反之"""
    from PySide6 import QtGui

    out = QtGui.QImage(img)
    painter = QtGui.QPainter(out)
    painter.setCompositionMode(QtGui.QPainter.CompositionMode_DestinationIn)
    grad = QtGui.QLinearGradient(0, 0, size, 0)
    opaque = QtGui.QColor(255, 255, 255, 255)
    clear = QtGui.QColor(255, 255, 255, 0)
    t0, t1 = x0 / size, x1 / size
    stops = [(0.0, opaque), (t0, opaque), (t1, clear), (1.0, clear)] if keep_left \
        else [(0.0, clear), (t0, clear), (t1, opaque), (1.0, opaque)]
    for pos, color in stops:
        grad.setColorAt(pos, color)
    painter.fillRect(0, 0, size, size, grad)
    painter.end()
    return out


def compose(left_png: Path, right_png: Path, out_png: Path, size: int = 1024) -> None:
    """两个图标各铺满画布，中间 24% 宽度交叉淡化（淡色过渡）"""
    from PySide6 import QtCore, QtGui

    def load_full(p):
        img = QtGui.QImage(str(p)).scaled(
            size, size, QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation)
        return img.convertToFormat(QtGui.QImage.Format.Format_ARGB32)

    band = int(size * 0.24)
    x0 = (size - band) // 2
    x1 = x0 + band
    li = _fade_mask(load_full(left_png), True, x0, x1, size)
    ri = _fade_mask(load_full(right_png), False, x0, x1, size)

    canvas = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32)
    canvas.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(canvas)
    painter.drawImage(0, 0, ri)
    painter.drawImage(0, 0, li)
    painter.end()
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
