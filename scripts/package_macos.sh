#!/usr/bin/env bash
# 打包 macOS 应用：PyInstaller 构建 -> 中文化系统对话框 -> ad-hoc 签名 -> 生成带「应用程序」文件夹的 DMG
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="Buddy Switcher"
DMG="dist/BuddySwitcher-macOS.dmg"

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

ICON_ARGS=""
[ -f assets/icon.icns ] && ICON_ARGS="--icon assets/icon.icns --add-data assets/icon_1024.png:assets"

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

"$PY" -m PyInstaller --noconfirm --clean --windowed --name "$APP_NAME" \
    $ICON_ARGS $TR_ARGS buddy_switch.py

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
hdiutil create -volname "$APP_NAME" -srcfolder dist/dmgroot -ov -format UDZO "$DMG" >/dev/null
rm -rf dist/dmgroot

echo "完成：$DMG"
