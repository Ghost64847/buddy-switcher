# -*- mode: python ; coding: utf-8 -*-
# Windows 单文件 exe：--onefile 模式不能打包后剪裁，改为在收集阶段
# 直接过滤无用的 Qt 插件/库（QML/PDF/虚拟键盘等，widget 应用不用）。
import fnmatch
import glob
import os

import PySide6

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

# Qt 自带中文翻译（应用菜单/文件对话框中文化）
_tr_base = os.path.dirname(PySide6.__file__)
_tr_datas = []
for _qm in ('qtbase_zh_CN.qm', 'qt_zh_CN.qm'):
    for _hit in glob.glob(os.path.join(_tr_base, '**', _qm),
                          recursive=True)[:1]:
        _tr_datas.append((_hit, 'translations'))

# 无用 Python 模块（widget 应用只需要 QtWidgets/QtGui/QtCore）
EXCLUDES = [
    'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets',
    'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtOpenGL',
    'PySide6.QtOpenGLWidgets', 'PySide6.QtVirtualKeyboard',
    'PySide6.QtNetwork', 'PySide6.QtSql', 'PySide6.QtTest',
    'PySide6.QtStateMachine', 'PySide6.QtXml', 'PySide6.QtPrintSupport',
    'tkinter', 'unittest', 'pydoc', 'doctest',
]

# 按文件名/路径过滤二进制依赖（QtDBus/QtNetwork 的 DLL 保留：
# QtGui 自身依赖 QtDBus，Qt 6.12 起如此，剪了会闪退）
_KILL_NAMES = (
    '*qpdf*', '*virtualkeyboardplugin*', '*touchplugin*',
    'QtPdf*', 'Qt6Pdf*', 'QtQml*', 'Qt6Qml*', 'QtQuick*', 'Qt6Quick*',
    'QtVirtualKeyboard*', 'Qt6VirtualKeyboard*', 'QtOpenGL*', 'Qt6OpenGL*',
)
_KILL_PATHS = ('/platforminputcontexts/', '/plugins/tls/',
               '/networkinformation/')


def _keep(name):
    n = name.replace('\\', '/')
    if any(part in n for part in _KILL_PATHS):
        return False
    base = n.rsplit('/', 1)[-1]
    return not any(fnmatch.fnmatch(base, pat) for pat in _KILL_NAMES)


a = Analysis(
    [os.path.join(ROOT, 'buddy_switch.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, 'assets', 'icon_1024.png'), 'assets')]
           + _tr_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
a.binaries = [b for b in a.binaries if _keep(b[0])]
a.datas = [d for d in a.datas if _keep(d[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BuddySwitcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, 'assets', 'icon.ico'),
)
