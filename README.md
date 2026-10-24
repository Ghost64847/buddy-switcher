# Buddy 切号器

WorkBuddy / CodeBuddy 多账号一键切换工具，支持 **macOS**、**Windows** 和 **Linux**。

导入账号 → 双击列表 → 自动完成「退出客户端 → 备份 → 写入新账号 → 同步 IDE 登录 → 重启」全流程。

界面基于 **Qt（PySide6）**，三个平台都用系统原生外观：macOS 是官方 Aqua 样式（顶部菜单栏、原生文件对话框、自动深浅色），Windows 用系统样式，Linux 跟随桌面主题。

## 功能

- **一键切换**：双击账号即切，客户端没在运行时连确认框都不弹
- **双端同步**：同时切换 WorkBuddy 桌面端登录文件和 CodeBuddy IDE 的加密登录记录（macOS / Windows）
- **自动备份**：每次切换前完整备份，随时可恢复，也支持批量删除备份
- **账号管理**：导入/导出 json、多选（拖动框选、Ctrl/Command 点选、Ctrl/Command+A 全选）、批量删除
- **真实昵称**：昵称被客户端加密时自动从 IDE 登录记录解密还原真实名称
- **有效期管理**：客户端自动续期后工具定时把新 token 归档；token 临期时切换登录一次即可滚动续期
- **安全写入**：原子写入 + 写后校验；uid 清洗防路径穿越；续期服务器域名白名单
- **零依赖运行**：打包好的应用开箱即用；源码版只需 Python 3.9+ + PySide6

## 下载

到 [Releases](../../releases) 下载对应平台安装包：

| 平台 | 文件 | 说明 |
|---|---|---|
| macOS（Apple Silicon） | `BuddySwitcher-macOS.dmg` | 打开后把应用拖进「应用程序」文件夹（首次打开右键 → 打开） |
| Windows 10/11 | `BuddySwitcher-Windows.zip` | 解压运行 `BuddySwitcher.exe`（SmartScreen 提示时选「仍要运行」） |
| Linux x86_64 | `BuddySwitcher-Linux-x86_64.tar.gz` | 解压后运行 `./BuddySwitcher`（需 glibc 2.28+ 桌面环境） |

macOS 首次同步 IDE 登录会弹**钥匙串授权框**，点「始终允许」即可（之后不再弹）。

## 使用方法

1. **保存选中账号**：把现在登录的账号存进列表，方便以后切回
2. **导入账号…**：选择 `workbuddy-<uid>.json` 登录文件
3. **双击列表中的账号**（或选中后点「切换账号」）即可完成切换

常用操作都在顶部菜单栏（文件 / 编辑 / 账号 / 显示）和右键菜单里：
导入导出、恢复备份、批量删除、刷新有效期、全选、打开数据目录、设置。

## 工作原理

客户端从登录文件读取身份，工具做的事情就是安全地替换它：

| 内容 | macOS / Linux | Windows |
|---|---|---|
| WorkBuddy 登录文件 | `~/Library/Application Support/…` 或 `~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` | `%APPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| CodeBuddy IDE 登录（加密） | `…/CodeBuddy CN/User/globalStorage/state.vscdb` | 同左（`%APPDATA%`） |
| 工具数据目录 | `~/Library/Application Support/BuddySwitch` 或 `~/.config/BuddySwitch` | `%APPDATA%\BuddySwitch` |

- **WorkBuddy 桌面端**：直接替换登录文件即可，客户端启动后会自己用 refreshToken 换新 token 并回写（回写会把昵称/手机号加密，工具会自动从 IDE 登录记录还原真实名称）。
- **CodeBuddy IDE**：登录记录加密存在 `state.vscdb` 里。macOS 用系统钥匙串口令做 AES-128-CBC 解密改写；Windows 用系统 DPAPI。写入后都会解密回读校验 uid。Linux 没有系统保管箱，IDE 同步不可用（WorkBuddy 端不受影响）。
- **Linux**：官方客户端没有 Linux 版，工具可以管理/导入/导出账号档案；如装了兼容客户端，可在 `config.json` 的 `apps` 里配置 `{"name": "...", "cmd": "进程匹配串", "launch": ["/路径/可执行文件"]}`。
- **数据目录**里：`profiles/` 是账号档案（内含 token，**等同账号控制权，勿外传**）；`backups/` 是切换前备份；`logs/` 是操作日志（不含 token）。

### 已知限制

- 只支持新格式 `workbuddy-<uid>.json`（含 `account` 和 `auth.accessToken`）；旧版数组格式会被拒绝。
- 用「保存选中账号」抓下来的档案 token 是客户端加密过的密文，这类档案切 IDE 时靠本地快照还原；没有快照时会跳过 IDE（WorkBuddy 端不受影响，日志会注明）。
- token 已过期的账号仍可切换，但启动后可能需要重新登录（列表里会标「已过期」）。
- 联网续期（右键「刷新有效期」）目前会被登录服务器拒绝（客户端为机密类型，需内置密钥），保留接口供后续使用；本地「重读归档」部分始终有效。
- 客户端安装在非默认路径时，可在 `config.json` 里覆盖 `auth_file` / `ide_db` / `apps`。

## 从源码运行 / 构建

```bash
pip install -r requirements.txt   # PySide6（界面）+ pyinstaller（打包）

python tests/selftest.py          # 离线自测（不需要 Qt）
python buddy_switch.py            # 图形界面
python buddy_switch.py --help     # 命令行用法

# 本地打包（以 macOS 为例）
bash scripts/package_macos.sh
```

推送 `v*` 标签会触发 [GitHub Actions](.github/workflows/release.yml) 自动构建 macOS / Windows / Linux 三平台安装包并发布 Release。

## 命令行

```
buddy_switch.py list                    # 列出账号
buddy_switch.py current                 # 当前登录
buddy_switch.py import <file.json>      # 导入账号
buddy_switch.py capture                 # 保存当前账号
buddy_switch.py switch <uid|file>       # 切换（--no-ide / --no-restart / --dry-run 可选）
buddy_switch.py refresh <uid|file>      # 联网续期（需明文 token 档案）
buddy_switch.py backups                 # 列出备份
```

## 免责声明

本项目仅供个人学习与研究。请遵守 WorkBuddy / CodeBuddy 的服务条款，账号凭据安全由使用者自己负责；因使用本工具造成的任何后果与作者无关。

## License

[MIT](LICENSE)
