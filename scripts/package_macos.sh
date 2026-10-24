#!/usr/bin/env bash
# 打包 macOS 应用：PyInstaller 构建 -> 中文化系统对话框 -> ad-hoc 签名 -> 生成带「应用程序」文件夹的 DMG
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="Buddy Switcher"
DMG="dist/BuddySwitcher-macOS.dmg"

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

"$PY" -m PyInstaller --noconfirm --clean --windowed --name "$APP_NAME" buddy_switch.py

# 中文化：让访达/系统弹窗跟随系统语言显示中文（未声明本地化的 app 默认英文）
"$PY" - <<'EOF'
import plistlib
from pathlib import Path

p = Path("dist/Buddy Switcher.app/Contents/Info.plist")
info = plistlib.loads(p.read_bytes())
info["CFBundleDevelopmentRegion"] = "zh_CN"
info["CFBundleAllowMixedLocalizations"] = True
info["CFBundleLocalizations"] = ["zh_CN", "en"]
p.write_bytes(plistlib.dumps(info))
print("Info.plist 已补丁：默认中文区域")
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
