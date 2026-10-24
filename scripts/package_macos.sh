#!/usr/bin/env bash
# 打包 macOS 应用：PyInstaller 构建 -> 中文化系统对话框 -> ad-hoc 签名 -> 生成带「应用程序」文件夹的 DMG
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="Buddy Switcher"
DMG="dist/BuddySwitcher-macOS.dmg"

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

ICON_ARGS=""
# mac 用 .icns；icon_1024.png 只有 Windows/Linux 当窗口图标用，不打进 mac 包
[ -f assets/icon.icns ] && ICON_ARGS="--icon assets/icon.icns"

# Qt 自带中文翻译（应用菜单/文件对话框中文化），打进包内 translations/
# 用递归查找定位（各平台 PySide6 的翻译目录布局不一致）
TR_ARGS="$("$PY" - <<'PYEOF'
import glob
import os

import PySide6
base = os.path.dirname(PySide6.__file__)
sep = ';' if os.name == 'nt' else ':'
args = []
for qm in ('qtbase_zh_CN.qm', 'qt_zh_CN.qm'):
    hits = glob.glob(os.path.join(base, '**', qm), recursive=True)
    if hits:
        args.append('--add-data %s%stranslations' % (hits[0], sep))
print(' '.join(args))
PYEOF
)"

# 瘦身：widget 应用不需要 QML/PDF/虚拟键盘/OpenGL/网络等模块
EXCLUDE_ARGS=""
for m in PySide6.QtQml PySide6.QtQuick PySide6.QtQuickWidgets PySide6.QtPdf \
         PySide6.QtPdfWidgets PySide6.QtOpenGL PySide6.QtOpenGLWidgets \
         PySide6.QtVirtualKeyboard PySide6.QtNetwork PySide6.QtDBus \
         PySide6.QtSql PySide6.QtTest PySide6.QtStateMachine PySide6.QtXml \
         PySide6.QtPrintSupport PySide6.QtCharts PySide6.QtDataVisualization \
         PySide6.QtMultimedia PySide6.QtWebEngineCore PySide6.QtWebEngineWidgets; do
    EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude-module $m"
done

"$PY" -m PyInstaller --noconfirm --clean --windowed --name "$APP_NAME" \
    $ICON_ARGS $TR_ARGS $EXCLUDE_ARGS buddy_switch.py

# 瘦身：剪掉虚拟键盘/QML/PDF/网络等无引用插件与框架（widget 应用不用）
"$PY" scripts/prune_qt.py "dist/$APP_NAME.app"

# 瘦身：去掉库文件里的本地符号（签名在后面统一重做）。
# 只处理 Frameworks 下的库——Contents/MacOS 的引导器带着内嵌打包档案，
# strip 会把它毁掉导致无法启动。
find "dist/$APP_NAME.app/Contents/Frameworks" -type f -print0 | while IFS= read -r -d '' f; do
    if file "$f" | grep -q "Mach-O"; then
        strip -x "$f" 2>/dev/null || true
    fi
done

# 中文化：让访达/系统弹窗跟随系统语言显示中文（未声明本地化的 app 默认英文）
"$PY" - <<'EOF'
import plistlib
from pathlib import Path

import buddy_switch

p = Path("dist/Buddy Switcher.app/Contents/Info.plist")
info = plistlib.loads(p.read_bytes())
info["CFBundleDevelopmentRegion"] = "zh_CN"
info["CFBundleAllowMixedLocalizations"] = True
info["CFBundleLocalizations"] = ["zh_CN", "en"]
info["CFBundleIdentifier"] = "com.buddyswitch.desktop"
info["CFBundleShortVersionString"] = buddy_switch.APP_VERSION
info["CFBundleVersion"] = buddy_switch.APP_VERSION
p.write_bytes(plistlib.dumps(info))
print("Info.plist 已补丁：默认中文区域 + 版本号", buddy_switch.APP_VERSION)
EOF

codesign --force --deep -s - "dist/$APP_NAME.app"

# DMG 内放「应用程序」文件夹快捷方式，支持拖拽安装
rm -rf dist/dmgroot "$DMG"
mkdir -p dist/dmgroot
cp -R "dist/$APP_NAME.app" dist/dmgroot/
ln -sfn /Applications dist/dmgroot/Applications
# hdiutil 偶发 Resource busy（残留挂载卷），最多重试 3 次
n=0
until hdiutil create -volname "$APP_NAME" -srcfolder dist/dmgroot -ov -format ULFO "$DMG" >/dev/null 2>&1; do
    n=$((n + 1))
    [ "$n" -ge 3 ] && { echo "hdiutil 三次失败"; exit 1; }
    hdiutil detach "/Volumes/$APP_NAME" >/dev/null 2>&1
    sleep 5
done
rm -rf dist/dmgroot

echo "完成：$DMG"
