# Buddy 切号器

WorkBuddy / CodeBuddy 多账号一键切换工具，支持 **macOS** 和 **Windows**。

导入账号 → 双击列表 → 自动完成「退出客户端 → 备份 → 写入新账号 → 同步 IDE 登录 → 重启」全流程。

## 功能

- **一键切换**：双击账号即切，客户端没在运行时连确认框都不弹
- **双端同步**：同时切换 WorkBuddy 桌面端登录文件和 CodeBuddy IDE 的加密登录记录
- **自动备份**：每次切换前完整备份，随时可从「恢复备份」回滚
- **账号管理**：导入 json、保存当前登录、查看 token 有效期、删除档案
- **安全写入**：原子写入 + 写后校验，失败会明确报错，不会留半吊子状态
- **零依赖运行**：打包好的应用开箱即用；源码版只需 Python 3.9+（自带 tkinter）

## 下载

到 [Releases](../../releases) 下载对应平台安装包：

| 平台 | 文件 | 说明 |
|---|---|---|
| macOS（Apple Silicon） | `BuddySwitcher-macOS.dmg` | 打开 dmg，把应用拖到「应用程序」（Intel 机型请从源码运行） |
| Windows 10/11 | `BuddySwitcher-Windows.zip` | 解压后运行 `BuddySwitcher.exe` |

**首次打开：**
- macOS：未签名应用，首次打开请**右键 → 打开**，或在终端执行
  `xattr -cr "/Applications/Buddy Switcher.app"`
- Windows：SmartScreen 提示时点「仍要运行」
- macOS 首次同步 IDE 登录会弹**钥匙串授权框**，点「始终允许」即可（之后不再弹）

## 使用方法

1. **保存当前账号**：把现在登录的账号存进列表，方便以后切回
2. **导入账号…**：选择 `workbuddy-<uid>.json` 登录文件
3. **双击列表中的账号**（或选中后点「切换账号」）即可完成切换

客户端在运行时会弹一次确认（因为要退出并重启它们）；不在运行时直接切换、全程无弹窗。右键列表还有「删除该档案」。

## 工作原理

客户端从登录文件读取身份，工具做的事情就是安全地替换它：

| 内容 | macOS | Windows |
|---|---|---|
| WorkBuddy 登录文件 | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` | `%APPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| CodeBuddy IDE 登录（加密） | `~/Library/Application Support/CodeBuddy CN/User/globalStorage/state.vscdb` | `%APPDATA%\CodeBuddy CN\User\globalStorage\state.vscdb` |
| 工具数据目录 | `~/Library/Application Support/BuddySwitch` | `%APPDATA%\BuddySwitch` |

- **WorkBuddy 桌面端**：直接替换登录文件即可，客户端启动后会自己用 refreshToken 换新 token 并回写。
- **CodeBuddy IDE**：登录记录加密存在 `state.vscdb` 里。macOS 用系统钥匙串口令做 AES-128-CBC 解密改写；Windows 用系统 DPAPI。写入后都会解密回读校验 uid。
- **数据目录**里：`profiles/` 是账号档案（内含 token，**等同账号控制权，勿外传**）；`backups/` 是切换前备份；`logs/` 是操作日志（不含 token）。

### 已知限制

- 只支持新格式 `workbuddy-<uid>.json`（含 `account` 和 `auth.accessToken`）；旧版数组格式会被拒绝。
- 用「保存当前账号」抓下来的档案 token 是客户端加密过的密文，这类档案切 IDE 时靠本地快照还原；没有快照时会跳过 IDE（WorkBuddy 端不受影响，工具会注明原因）。
- token 已过期的账号仍可切换，但启动后可能需要重新登录（列表里会标「已过期」）。
- 如果你的客户端安装在非默认路径，可在数据目录的 `config.json` 里覆盖 `auth_file` / `ide_db` / `apps`。

## 从源码运行 / 构建

```bash
# 运行（macOS / Windows 通用，需 Python 3.9+）
python3 tests/selftest.py     # 离线自测
python3 buddy_switch.py       # 图形界面
python3 buddy_switch.py --help  # 命令行用法

# 本地打包（以 macOS 为例）
pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --name "Buddy Switcher" buddy_switch.py
```

推送 `v*` 标签会触发 [GitHub Actions](.github/workflows/release.yml) 自动构建 macOS 和 Windows 安装包并发布 Release。

## 命令行

```
buddy_switch.py list                    # 列出账号
buddy_switch.py current                 # 当前登录
buddy_switch.py import <file.json>      # 导入账号
buddy_switch.py capture                 # 保存当前账号
buddy_switch.py switch <uid|file>       # 切换（--no-ide / --no-restart / --dry-run 可选）
buddy_switch.py backups                 # 列出备份
```

## 免责声明

本项目仅供个人学习与研究。请遵守 WorkBuddy / CodeBuddy 的服务条款，账号凭据安全由使用者自己负责；因使用本工具造成的任何后果与作者无关。

## License

[MIT](LICENSE)
