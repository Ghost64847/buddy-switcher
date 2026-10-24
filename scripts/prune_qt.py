#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剪掉打包产物里用不到的 Qt 插件/库。

widget 界面只需要 QtWidgets/QtGui/QtCore/QtSvg；但 PyInstaller 的依赖
分析会被个别插件拖进整个 QML 栈（虚拟键盘输入插件）、QtPdf（qpdf 图片
插件）、QtNetwork/QtDBus（tls/networkinformation/触摸插件）。
在打包完成后运行本脚本按引用关系清除这些死重。

用法：python prune_qt.py <打包输出根目录（.app 或 onedir 目录）>
"""
import fnmatch
import os
import shutil
import sys

# 整目录剪除的插件目录（按目录名匹配）
PLUGIN_DIRS = {"platforminputcontexts", "tls", "networkinformation"}
# 按文件/目录名匹配剪除（覆盖 mac 框架、Windows DLL、Linux so 三种命名）
NAME_KILLS = (
    "*qpdf*", "*virtualkeyboardplugin*", "*touchplugin*",
    "QtPdf*", "Qt6Pdf*", "libQt6Pdf*",
    "QtQml*", "Qt6Qml*", "libQt6Qml*",
    "QtQuick*", "Qt6Quick*", "libQt6Quick*",
    "QtVirtualKeyboard*", "Qt6VirtualKeyboard*", "libQt6VirtualKeyboard*",
    "QtOpenGL*", "Qt6OpenGL*", "libQt6OpenGL*",
    # 注意：QtDBus / QtNetwork 不能剪——QtGui 框架自身链接它们
    # （Qt 6.12 mac 构建），剪了直接闪退。
)


def _match(name):
    return any(fnmatch.fnmatch(name, pat) for pat in NAME_KILLS)


def _dir_size(path):
    total = 0
    for dp, _dns, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


def prune(root):
    freed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for d in list(dirnames):
            if d in PLUGIN_DIRS or _match(d):
                p = os.path.join(dirpath, d)
                freed += _dir_size(p)
                shutil.rmtree(p, ignore_errors=True)
                dirnames.remove(d)
        for f in filenames:
            if _match(f):
                p = os.path.join(dirpath, f)
                try:
                    freed += os.path.getsize(p)
                    os.remove(p)
                except OSError:
                    pass
    return freed


if __name__ == "__main__":
    root = sys.argv[1]
    if not os.path.isdir(root):
        sys.exit("prune_qt: dir not found: %s" % root)
    # 只输出 ASCII（Windows runner 控制台默认 cp1252，中文会崩）
    mb = prune(root) / 1048576
    print("prune_qt: freed %.1fMB from %s" % (mb, root))
