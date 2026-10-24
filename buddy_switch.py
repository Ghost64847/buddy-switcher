#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Buddy 切号器 —— WorkBuddy / CodeBuddy 多账号一键切换（macOS / Windows）

原理：客户端从登录文件读取身份
  macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
  Windows: %APPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
流程：退出客户端 -> 备份旧登录 -> 写入新账号 -> 同步 IDE 登录 -> 重启客户端。

数据目录（BuddySwitch）:
  profiles/   已导入/已保存的账号（内含 token，等同账号控制权，勿外传）
  backups/    每次切号前的完整备份
  logs/       操作日志（不含 token）
"""

import base64
import copy
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

IS_WIN = (os.name == "nt")
IS_LINUX = (not IS_WIN) and (sys.platform.startswith("linux")
                             or sys.platform.startswith("freebsd"))
IS_MAC = (not IS_WIN) and (not IS_LINUX)
HOME = os.path.expanduser("~")
APP_NAME = "Buddy 切号器"
APP_VERSION = "2.2.5"
UI_FONT = "Microsoft YaHei UI" if IS_WIN else "PingFang SC"
MONO_FONT = "Consolas" if IS_WIN else "Menlo"


def _base_dir():
    if IS_WIN:
        return os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
    if IS_LINUX:
        return os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    return os.path.join(HOME, "Library", "Application Support")


BASE_DIR = _base_dir()
AUTH_DIR = os.path.join(BASE_DIR, "CodeBuddyExtension", "Data", "Public", "auth")
TARGET = os.path.join(AUTH_DIR, "workbuddy-desktop.info")
STORE = os.path.join(BASE_DIR, "BuddySwitch")
LEGACY_STORE = os.path.join(BASE_DIR, "WBSwitch")  # 旧版数据目录，首次运行自动迁移
PROFILE_DIR = os.path.join(STORE, "profiles")
BACKUP_DIR = os.path.join(STORE, "backups")
LOG_DIR = os.path.join(STORE, "logs")
CONFIG = os.path.join(STORE, "config.json")

# CodeBuddy IDE 的登录：加密存在 state.vscdb 里
#   macOS   口令在系统钥匙串 "CodeBuddy CN Safe Storage"（AES-128-CBC）
#   Windows 用系统 DPAPI（CryptProtectData）
#   Linux   无系统级保管箱，IDE 同步不可用（可切 WorkBuddy 端）
IDE_DB = os.path.join(BASE_DIR, "CodeBuddy CN", "User", "globalStorage", "state.vscdb")
IDE_SECRET_LIKE = "%planning-genie.new.accessToken%"
IDE_BLOB_KEY = "Tencent-Cloud.coding-copilot"
OPENSSL_CANDIDATES = ["/opt/homebrew/bin/openssl", "/usr/local/bin/openssl", "/usr/bin/openssl"]

if IS_WIN:
    DEFAULT_APPS = [
        {"name": "WorkBuddy", "exes": ["WorkBuddy.exe"],
         "launch": [r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe",
                    r"%PROGRAMFILES%\WorkBuddy\WorkBuddy.exe"]},
        {"name": "CodeBuddy CN",
         "exes": ["CodeBuddy CN.exe", "CodeBuddy.exe", "CodeBuddyCN.exe"],
         "launch": [r"%LOCALAPPDATA%\Programs\CodeBuddy CN\CodeBuddy CN.exe",
                    r"%LOCALAPPDATA%\Programs\CodeBuddy\CodeBuddy.exe",
                    r"%PROGRAMFILES%\CodeBuddy CN\CodeBuddy CN.exe"]},
    ]
elif IS_LINUX:
    # 官方客户端没有 Linux 版；如装了兼容版可在 config.json 的 apps 里配置
    DEFAULT_APPS = []
else:
    DEFAULT_APPS = [
        {"name": "CodeBuddy CN", "path": "/Applications/CodeBuddy CN.app"},
        {"name": "WorkBuddy", "path": "/Applications/WorkBuddy.app"},
    ]

# 高级选项（config.json 可覆盖）：switch_scope（both/workbuddy/ide）/
# restart_after_switch / auto_refresh / auto_refresh_minutes /
# auth_file / ide_db / apps
DEFAULT_CONFIG = {"switch_scope": "both", "restart_after_switch": True,
                  "auto_refresh": True, "auto_refresh_minutes": 60}
SCOPE_LABELS = [("两个都切换", "both"), ("只切 WorkBuddy", "workbuddy"),
                ("只切 CodeBuddy IDE", "ide")]


# ---------------------------------------------------------------- 基础工具

_migrated = False


def ensure_dirs():
    global _migrated
    for d in (PROFILE_DIR, BACKUP_DIR, LOG_DIR, AUTH_DIR):
        os.makedirs(d, exist_ok=True)
    if not _migrated:
        _migrated = True
        _migrate_legacy()


def _migrate_legacy():
    """从旧版 WBSwitch 数据目录迁移账号和配置（只在目标为空时执行一次）"""
    if not os.path.isdir(os.path.join(LEGACY_STORE, "profiles")):
        return
    if os.listdir(PROFILE_DIR):
        return
    try:
        n = 0
        for fn in os.listdir(os.path.join(LEGACY_STORE, "profiles")):
            if fn.endswith(".json"):
                shutil.copy2(os.path.join(LEGACY_STORE, "profiles", fn),
                             os.path.join(PROFILE_DIR, fn))
                n += 1
        old_cfg = os.path.join(LEGACY_STORE, "config.json")
        if os.path.exists(old_cfg) and not os.path.exists(CONFIG):
            shutil.copy2(old_cfg, CONFIG)
        if n:
            log("已从旧版 WBSwitch 迁移 %d 个账号" % n)
    except OSError as e:
        log("迁移旧数据失败：%s" % e)


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        with open(os.path.join(LOG_DIR, "switch.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    out = dict(DEFAULT_CONFIG)
    out.update(cfg)
    # 旧版配置迁移：switch_ide=False 相当于「只切 WorkBuddy」
    if "switch_scope" not in cfg and cfg.get("switch_ide") is False:
        out["switch_scope"] = "workbuddy"
    return out


def save_config(cfg):
    os.makedirs(STORE, exist_ok=True)
    tmp = CONFIG + ".tmp"
    Path(tmp).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)


def safe_uid(uid):
    """把账号 uid 清洗成安全文件名片段，杜绝 ../ 之类的路径穿越"""
    u = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (uid or ""))
    return (u or "unknown")[:64]


def resolve_user_file(path):
    """规范化用户传入的文件路径，必须指向一个已存在的文件"""
    p = os.path.realpath(os.path.expanduser(path))
    if not os.path.isfile(p):
        raise ValueError("文件不存在：%s" % path)
    return p


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_info(path=None):
    path = path or TARGET
    if not os.path.exists(path):
        return None
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def write_info(doc, path=None):
    """原子写入，权限 600"""
    path = path or TARGET
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".buddytmp"
    Path(tmp).write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def to_ms(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return v * 1000 if v < 10 ** 11 else v


def jwt_claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def token_exp_ms(token):
    if not isinstance(token, str) or token.count(".") < 2:
        return 0
    try:
        return int(jwt_claims(token).get("exp")) * 1000
    except (TypeError, ValueError):
        return 0


def fmt_ts(ms):
    if not ms:
        return "未知"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def is_new_format(doc):
    return (isinstance(doc, dict) and isinstance(doc.get("account"), dict)
            and isinstance(doc.get("auth"), dict)
            and ("accessToken" in doc["auth"]))


def validate(doc):
    """返回 (ok, message)"""
    if isinstance(doc, list):
        return False, ("这是旧格式（数组）文件，不能用于桌面端登录。\n"
                       "请使用 workbuddy-<uid>.json 新格式。")
    if not isinstance(doc, dict):
        return False, "无法识别的 json 文件。"
    if not is_new_format(doc):
        return False, "缺少 account 或 auth.accessToken 字段，不是有效的登录文件。"
    uid = doc["account"].get("uid")
    if not isinstance(uid, str) or not uid.strip():
        return False, "account.uid 为空，文件不完整。"
    tok = doc["auth"].get("accessToken")
    if isinstance(tok, dict):
        ok_tok = tok.get("$wbEncrypted") == 1
    else:
        ok_tok = isinstance(tok, str) and len(tok) >= 20
    if not ok_tok:
        return False, "auth.accessToken 无效。"
    return True, "ok"


def account_uid(doc):
    if not isinstance(doc, dict):
        return ""
    acc = doc.get("account") or {}
    if isinstance(acc, dict) and isinstance(acc.get("uid"), str):
        return acc["uid"]
    return ""


def account_label(doc):
    """显示名：明文昵称/手机号 > token 里解出的用户名 > UID 前 8 位。
    客户端刷新 token 后会把昵称、手机号加密回写（$wbEncrypted），这时退而求其次。"""
    acc = (doc or {}).get("account") or {}
    nick = acc.get("nickname")
    name = nick.strip() if isinstance(nick, str) else ""
    phone = acc.get("phoneNumber")
    if isinstance(phone, str) and phone.strip():
        name = (name + " / " + phone.strip()).strip(" /")
    if name:
        return name
    tok = ((doc or {}).get("auth") or {}).get("accessToken")
    if isinstance(tok, str) and tok.count(".") >= 2:
        claims = jwt_claims(tok)
        for key in ("preferred_username", "phone_number", "email"):
            v = claims.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    uid = account_uid(doc)
    return ("账号 " + uid[:8]) if uid else "未命名账号"


def expiry_of(doc):
    auth = (doc or {}).get("auth") or {}
    exp = to_ms(auth.get("expiresAt"))
    if not exp:
        tok = auth.get("accessToken")
        if isinstance(tok, str):
            exp = token_exp_ms(tok)
    return exp


# ---------------------------------------------------------------- 账号文档

def _blank_account():
    return {
        "uid": "", "nickname": "", "uin": "", "type": "",
        "lastLogin": True, "isCreator": False, "isAdmin": False,
        "pluginEnabled": True,
        "deployStatus": {"statusCode": 0, "statusMsg": "", "detailMsg": ""},
        "accountType": "", "sso": {"domain": "", "domainModifiedTimes": 0},
        "idp": "", "areaInfoComplete": False, "oneidAccountId": "",
        "isCurrentOneIdEnterprise": False, "isCurrentOneIdPersonal": False,
        "isFirstLogin": False, "mpOpenId": "", "logOutEnable": False,
    }


def normalize_account(src):
    src = src if isinstance(src, dict) else {}
    acc = _blank_account()
    if isinstance(src.get("nickname"), (str, dict)):
        acc["nickname"] = src["nickname"]
    if isinstance(src.get("phoneNumber"), (str, dict)):
        acc["phoneNumber"] = src["phoneNumber"]
    for k in list(acc):
        if k in src and isinstance(src[k], type(acc[k])):
            acc[k] = copy.deepcopy(src[k])
    for k, v in src.items():
        if k not in acc:
            acc[k] = copy.deepcopy(v)
    acc["uid"] = src.get("uid", "")
    acc["lastLogin"] = True
    acc["pluginEnabled"] = True
    return acc


def normalize_auth(src):
    src = src or {}
    auth = {
        "accessToken": src.get("accessToken", ""),
        "expiresIn": src.get("expiresIn", 0),
        "refreshExpiresIn": src.get("refreshExpiresIn", 0),
        "refreshToken": src.get("refreshToken", ""),
        "tokenType": src.get("tokenType") or "Bearer",
        "notBeforePolicy": src.get("notBeforePolicy", 0),
        "sessionState": src.get("sessionState", ""),
        "scope": src.get("scope") or "openid profile offline_access email",
        "domain": src.get("domain") or "www.workbuddy.cn",
        "lastRefreshTime": src.get("lastRefreshTime", 0),
        "expiresAt": src.get("expiresAt", 0),
        "refreshExpiresAt": src.get("refreshExpiresAt", 0),
        "realm": src.get("realm") or "cn",
    }
    for k, v in src.items():
        auth[k] = copy.deepcopy(v)

    now = int(time.time() * 1000)
    at = auth.get("accessToken")
    if isinstance(at, str) and at:
        exp = to_ms(auth.get("expiresAt")) or token_exp_ms(at)
        auth["expiresAt"] = exp
        auth["lastRefreshTime"] = now
        auth["expiresIn"] = max(0, (exp - now) // 1000) if exp else 0
        rt = auth.get("refreshToken")
        rexp = to_ms(auth.get("refreshExpiresAt")) or token_exp_ms(rt if isinstance(rt, str) else "")
        auth["refreshExpiresAt"] = rexp
        auth["refreshExpiresIn"] = max(0, (rexp - now) // 1000) if rexp else 0
    else:
        auth["expiresAt"] = to_ms(auth.get("expiresAt"))
        auth["refreshExpiresAt"] = to_ms(auth.get("refreshExpiresAt"))
        auth["lastRefreshTime"] = to_ms(auth.get("lastRefreshTime")) or now
    return auth


def make_doc(src, template=None):
    doc = {
        "account": normalize_account(src.get("account")),
        "auth": normalize_auth(src.get("auth")),
        "accounts": [],
        "allAccounts": [],
    }
    uid = doc["account"]["uid"]

    old = []
    if isinstance(template, dict):
        old = [a for a in (template.get("allAccounts") or template.get("accounts") or [])
               if isinstance(a, dict) and a.get("uid") != uid]

    me = copy.deepcopy(doc["account"])
    for a in old:
        b = copy.deepcopy(a)
        b["lastLogin"] = False
        doc["accounts"].append(b)
    doc["accounts"].insert(0, me)
    doc["allAccounts"] = copy.deepcopy(doc["accounts"])
    return doc


def import_profile(path):
    doc = read_json(resolve_user_file(path))
    ok, msg = validate(doc)
    if not ok:
        raise ValueError(msg)
    ensure_dirs()
    uid = safe_uid(account_uid(doc))
    pp = os.path.join(PROFILE_DIR, "%s.json" % uid)
    write_info(doc, pp)
    log("导入账号 %s (%s)" % (account_label(doc), uid))
    return pp, doc


def _extract_session_name(sess):
    """从登录会话文档里提取可读的账号名：昵称 > 手机号 > token 里的用户名"""
    acc = (sess or {}).get("account") or {}
    for field in ("nickname", "phoneNumber"):
        v = acc.get(field)
        if isinstance(v, str) and v.strip():
            return v.strip()
    tok = ((sess or {}).get("auth") or {}).get("accessToken")
    if isinstance(tok, str) and tok.count(".") >= 2:
        claims = jwt_claims(tok)
        for key in ("preferred_username", "phone_number", "email"):
            v = claims.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def resolve_nickname_from_ide(uid):
    """从 CodeBuddy IDE 的登录库/历史快照解出该账号的真实昵称。
    这些记录可以解密（钥匙串/DPAPI），昵称是明文。失败返回 None。"""
    try:
        if not ide_available():
            return None
        key = None if IS_WIN else ide_secret_key()
        _, sess = read_ide_session(key)
        if sess and account_uid(sess) == uid:
            name = _extract_session_name(sess)
            if name:
                return name
        snap_path = ide_snapshot_path(uid)
        if os.path.exists(snap_path):
            snap = read_json(snap_path)
            secret = snap.get("secret")
            if isinstance(secret, str):
                blob = json.loads(secret)
                raw = bytes(blob["data"])
                plain = ide_decrypt(raw if IS_WIN else raw[3:], key)
                name = _extract_session_name(json.loads(plain))
                if name:
                    return name
    except Exception:  # noqa: BLE001  解不出来就算了，不影响主流程
        return None
    return None


def capture_current():
    doc = read_info()
    if not doc:
        raise ValueError("当前没有登录文件，无法保存。")
    ensure_dirs()
    uid_raw = account_uid(doc)
    uid = safe_uid(uid_raw)
    pp = os.path.join(PROFILE_DIR, "%s.json" % uid)
    # 客户端刷新 token 后会把昵称/手机号加密回写；归档时尽量留住明文：
    # 1) 档案里已有的明文字段；2) 从 IDE 登录记录/快照解密出来的真实昵称
    if os.path.exists(pp):
        try:
            old_acc = (read_json(pp).get("account") or {})
            acc = doc.setdefault("account", {})
            for field in ("nickname", "phoneNumber"):
                if isinstance(acc.get(field), dict) and isinstance(old_acc.get(field), str):
                    acc[field] = old_acc[field]
        except (OSError, ValueError):
            pass
    if isinstance((doc.get("account") or {}).get("nickname"), dict):
        real = resolve_nickname_from_ide(uid_raw)
        if real:
            doc["account"]["nickname"] = real
            log("已从 IDE 登录记录解析真实昵称：%s" % real)
    write_info(doc, pp)
    log("保存当前账号 %s (%s)" % (account_label(doc), uid))
    return pp, doc


def delete_profile(uid):
    pp = os.path.join(PROFILE_DIR, "%s.json" % safe_uid(uid))
    if os.path.exists(pp):
        os.remove(pp)
        log("删除账号档案 %s" % uid)
        return True
    return False


def list_profiles():
    out = []
    if not os.path.isdir(PROFILE_DIR):
        return out
    for fn in sorted(os.listdir(PROFILE_DIR)):
        if not fn.endswith(".json") or fn.startswith("ide-session-"):
            continue
        path = os.path.join(PROFILE_DIR, fn)
        try:
            doc = read_json(path)
        except (OSError, ValueError):
            continue
        uid = account_uid(doc) or fn[:-5]
        out.append({
            "path": path,
            "uid": uid,
            "label": account_label(doc),
            "expiry": expiry_of(doc),
            "doc": doc,
        })
    out.sort(key=lambda x: (x["label"], x["uid"]))
    return out


def backup_current(tag="switch"):
    doc = read_info()
    if not doc:
        return None
    ensure_dirs()
    uid = safe_uid(account_uid(doc))
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_DIR, "workbuddy-desktop.%s.%s.%s.info"
                       % (ts, safe_uid(tag), uid[:8]))
    Path(dst).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    log("备份当前登录 -> %s" % os.path.basename(dst))
    return dst


def list_backups():
    out = []
    if not os.path.isdir(BACKUP_DIR):
        return out
    for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not fn.endswith(".info"):
            continue
        p = os.path.join(BACKUP_DIR, fn)
        try:
            doc = read_json(p)
        except (OSError, ValueError):
            doc = None
        out.append({"path": p, "mtime": os.path.getmtime(p),
                    "label": account_label(doc) if doc else "无法解析",
                    "uid": account_uid(doc) if doc else ""})
    return out


# ---------------------------------------------------------------- 加密（IDE 登录用）

def find_openssl():
    for p in OPENSSL_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("openssl")


def _pad(b):
    n = 16 - len(b) % 16
    return b + bytes([n]) * n


def _unpad(b):
    return b[:-b[-1]] if b and 0 < b[-1] <= 16 else b


def _aes_cbc(openssl, data, key, decrypt):
    tmp = os.path.join(STORE, ".crypt.tmp")
    os.makedirs(STORE, exist_ok=True)
    Path(tmp).write_bytes(data)
    cmd = [openssl, "enc", "-d" if decrypt else "-e", "-aes-128-cbc",
           "-K", key.hex(), "-iv", "20" * 16, "-nopad", "-in", tmp]
    try:
        out = subprocess.run(cmd, capture_output=True)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if out.returncode != 0:
        raise RuntimeError("openssl 加解密失败：%s" % out.stderr.decode("utf-8", "replace")[:200])
    return out.stdout


def _dpapi(data, protect):
    """Windows DPAPI 加/解密（对应 macOS 的钥匙串 + AES 方案）"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    func = (ctypes.windll.crypt32.CryptProtectData if protect
            else ctypes.windll.crypt32.CryptUnprotectData)
    if not func(ctypes.byref(blob_in), None, None, None, None, 0,
                ctypes.byref(blob_out)):
        raise OSError("DPAPI %s失败（错误码 %s）"
                      % ("加密" if protect else "解密", ctypes.GetLastError()))
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def ide_secret_key():
    """macOS：从系统钥匙串读 Chromium safeStorage 口令（首次会弹授权框）"""
    if IS_LINUX:
        raise RuntimeError("Linux 上没有系统保管箱，无法解密 IDE 登录记录")
    try:
        r = subprocess.run(["security", "find-generic-password",
                            "-s", "CodeBuddy CN Safe Storage", "-w"],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("等待钥匙串授权超时。请在弹窗里点「允许」后重试。")
    except OSError as e:
        raise RuntimeError("调用 security 失败：%s" % e)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("读取钥匙串失败（%s）。请在弹窗里点「允许」。"
                           % (r.stderr.strip() or "用户拒绝或未授权"))
    return hashlib.pbkdf2_hmac("sha1", r.stdout.strip().encode(), b"saltysalt", 1003, 16)


def ide_decrypt(raw, key=None):
    if IS_WIN:
        return _dpapi(raw, False)
    if raw[:3] == b"v10":
        raw = raw[3:]
    openssl = find_openssl()
    if not openssl:
        raise RuntimeError("系统里找不到 openssl，无法解密 IDE 登录记录。")
    return _unpad(_aes_cbc(openssl, raw, key or ide_secret_key(), True))


def ide_encrypt(plain, key):
    if IS_WIN:
        return _dpapi(plain, True)
    return b"v10" + _aes_cbc(find_openssl(), _pad(plain), key, False)


def ide_available():
    return os.path.exists(IDE_DB)


def read_ide_session(key):
    """返回 (记录 key 名, 解密后的登录文档)"""
    conn = sqlite3.connect(IDE_DB)
    try:
        row = conn.execute("select key from ItemTable where key like ?",
                           (IDE_SECRET_LIKE,)).fetchone()
        if not row:
            return None, None
        raw = conn.execute("select value from ItemTable where key=?", (row[0],)).fetchone()[0]
        blob = json.loads(raw)
        plain = ide_decrypt(bytes(blob["data"])[3:] if not IS_WIN else bytes(blob["data"]), key)
        return row[0], json.loads(plain)
    finally:
        conn.close()


def ide_snapshot_path(uid):
    return os.path.join(PROFILE_DIR, "ide-session-%s.json" % safe_uid(uid))


def save_ide_snapshot(key_name, secret_value, blob_value, uid):
    """把 IDE 当前的加密登录记录按账号存档，供以后切回该账号时还原"""
    ensure_dirs()
    doc = {"uid": uid, "key": key_name, "secret": secret_value,
           "blobKey": IDE_BLOB_KEY, "blob": blob_value,
           "savedAt": int(time.time() * 1000)}
    dst = ide_snapshot_path(uid)
    Path(dst).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    log("已保存 IDE 登录快照 -> %s" % os.path.basename(dst))
    return dst


def _backup_ide_db():
    ensure_dirs()
    dst = os.path.join(BACKUP_DIR, "ide-state.vscdb.%s.bak"
                       % datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(IDE_DB, dst)
    log("备份 IDE 登录库 -> %s" % os.path.basename(dst))
    return dst


def restore_ide_snapshot(doc, progress=None):
    """用之前存的 IDE 快照还原该账号在 IDE 里的登录"""
    def say(m):
        log(m)
        if progress:
            progress(m)

    uid = account_uid(doc)
    snap_path = ide_snapshot_path(uid)
    if not os.path.exists(snap_path):
        raise RuntimeError("该账号档案的 token 是密文（本机抓取），且没有它的 IDE 登录快照，"
                           "无法同步 IDE。请先在 IDE 里登录该账号一次，再点「保存当前账号」。")
    if app_running(DEFAULT_APPS[0]):
        raise RuntimeError("CodeBuddy IDE 还在运行，必须先完全退出。")
    snap = read_json(snap_path)
    _backup_ide_db()
    say("用快照还原 IDE 登录…")

    conn = sqlite3.connect(IDE_DB)
    try:
        conn.execute("update ItemTable set value=? where key=?",
                     (snap["secret"], snap["key"]))
        if snap.get("blob") and snap.get("blobKey"):
            conn.execute("update ItemTable set value=? where key=?",
                         (snap["blob"], snap["blobKey"]))
        conn.commit()
    finally:
        conn.close()

    key = None if IS_WIN else ide_secret_key()
    _, check = read_ide_session(key)
    if not check or account_uid(check) != uid:
        raise RuntimeError("IDE 快照还原后校验失败，请用备份还原。")
    say("CodeBuddy IDE 已切回：%s" % account_label(doc))
    return True


def inject_ide(doc, progress=None):
    """把账号写进 CodeBuddy IDE 的加密登录记录（调用前必须先退出 IDE）"""
    def say(m):
        log(m)
        if progress:
            progress(m)

    if not ide_available():
        raise RuntimeError("没有找到 CodeBuddy IDE 的登录库（%s）。" % IDE_DB)
    if app_running(DEFAULT_APPS[0]):
        raise RuntimeError("CodeBuddy IDE 还在运行，必须先完全退出。")

    ok, msg = validate(doc)
    if not ok:
        raise ValueError(msg)

    # 密文 token 的档案（本机抓取）走快照还原
    if not isinstance((doc.get("auth") or {}).get("accessToken"), str):
        return restore_ide_snapshot(doc, progress)

    if IS_WIN:
        key = None
    else:
        say("读取系统钥匙串（若弹出授权框请点「始终允许」）…")
        key = ide_secret_key()
    key_name, old_session = read_ide_session(key)
    if old_session is None:
        raise RuntimeError("IDE 登录库里没有可用的登录记录（可能从未登录过）。")
    old_acc = old_session.get("account") or {}

    _backup_ide_db()

    # 先把 IDE 现有登录按它自己的账号存快照，以后能切回来
    conn = sqlite3.connect(IDE_DB)
    try:
        cur_secret = conn.execute("select value from ItemTable where key=?",
                                  (key_name,)).fetchone()[0]
        row = conn.execute("select value from ItemTable where key=?",
                           (IDE_BLOB_KEY,)).fetchone()
        save_ide_snapshot(key_name, cur_secret, row[0] if row else None,
                          old_acc.get("uid"))
    finally:
        conn.close()

    say("正在写入 IDE 新账号…")
    live_acc = (read_info() or {}).get("account") or {}
    nick = doc["account"].get("nickname")
    acc = json.loads(json.dumps(old_acc))  # 借用旧记录的字段结构
    acc.update({
        "uid": account_uid(doc),
        "nickname": nick if isinstance(nick, str) else "",
        "uin": live_acc.get("uin") or (doc["account"].get("uin") or ""),
        "lastLogin": True, "isCreator": False, "isAdmin": False,
        "pluginEnabled": True, "isFirstLogin": False,
        "areaInfoComplete": False, "oneidAccountId": "",
        "mpOpenId": "", "phoneNumber": "", "logOutEnable": False,
    })
    session = {"account": acc,
               "auth": normalize_auth(doc.get("auth")),
               "accounts": [copy.deepcopy(acc)]}
    payload = json.dumps(session, ensure_ascii=False, separators=(",", ":")).encode()
    enc = ide_encrypt(payload, key)

    conn = sqlite3.connect(IDE_DB)
    try:
        conn.execute("update ItemTable set value=? where key=?",
                     (json.dumps({"type": "Buffer", "data": list(enc)}), key_name))
        row = conn.execute("select value from ItemTable where key=?",
                           (IDE_BLOB_KEY,)).fetchone()
        if row and isinstance(row[0], str):
            s = row[0]
            for old in (old_acc.get("uid"), old_acc.get("uin")):
                if old and old != "0":
                    s = s.replace(old, acc["uid"] if old == old_acc.get("uid") else acc["uin"])
            conn.execute("update ItemTable set value=? where key=?", (s, IDE_BLOB_KEY))
        conn.commit()
    finally:
        conn.close()

    _, check = read_ide_session(key)
    if not check or account_uid(check) != account_uid(doc):
        raise RuntimeError("IDE 登录写入后校验失败，请用备份还原。")
    say("CodeBuddy IDE 已切换为：%s" % account_label(doc))
    return True


# ---------------------------------------------------------------- 客户端进程

def get_apps():
    apps = load_config().get("apps")
    return apps if isinstance(apps, list) and apps else DEFAULT_APPS


if IS_WIN:
    def _tasklist_names():
        try:
            r = subprocess.run(["tasklist", "/NH", "/FO", "CSV"],
                               capture_output=True, text=True, timeout=15)
        except OSError:
            return set()
        names = set()
        for ln in r.stdout.splitlines():
            parts = ln.split('","')
            if parts:
                name = parts[0].strip('"').lower()
                if name:
                    names.add(name)
        return names

    def app_running(app):
        names = _tasklist_names()
        return any(exe.lower() in names for exe in app.get("exes", []))

    def quit_app(app, progress=None):
        exes = app.get("exes", [])
        if not app_running(app):
            return True
        if progress:
            progress("正在退出 %s…" % app["name"])
        for exe in exes:  # 先温和关闭
            subprocess.run(["taskkill", "/IM", exe, "/T"], capture_output=True)
        deadline = time.time() + 10
        while time.time() < deadline:
            if not app_running(app):
                log("%s 已退出" % app["name"])
                return True
            time.sleep(0.5)
        for exe in exes:  # 再强制结束
            subprocess.run(["taskkill", "/IM", exe, "/T", "/F"], capture_output=True)
        time.sleep(1)
        ok = not app_running(app)
        log("%s %s" % (app["name"], "已退出" if ok else "无法退出，请手动关闭"))
        return ok

    def launch_app(app):
        for p in app.get("launch", []):
            path = os.path.expandvars(p)
            if os.path.exists(path):
                try:
                    os.startfile(path)  # noqa: PTH
                    log("启动 %s" % app["name"])
                except OSError as e:
                    log("启动 %s 失败：%s" % (app["name"], e))
                return
        log("未找到 %s 的安装路径，请手动启动（可在 config.json 的 apps 里配置）。" % app["name"])
elif IS_LINUX:
    # Linux：官方客户端没有发布版；用户在 config.json apps 里配置
    # {"name": "...", "cmd": "/opt/xxx/xxx", "launch": ["/opt/xxx/xxx"]}
    def app_running(app):
        pat = app.get("cmd") or app.get("path") or ""
        if not pat:
            return False
        try:
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            return r.returncode == 0 and bool(r.stdout.strip())
        except OSError:
            return False

    def _wait_gone(app, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not app_running(app):
                return True
            time.sleep(0.25)
        return not app_running(app)

    def quit_app(app, progress=None):
        pat = app.get("cmd") or app.get("path") or ""
        if not pat or not app_running(app):
            return True
        if progress:
            progress("正在退出 %s…" % app["name"])
        subprocess.run(["pkill", "-TERM", "-f", pat], capture_output=True)
        if _wait_gone(app, 5):
            log("%s 已退出" % app["name"])
            return True
        subprocess.run(["pkill", "-KILL", "-f", pat], capture_output=True)
        ok = _wait_gone(app, 3)
        log("%s %s" % (app["name"], "已强制退出" if ok else "无法退出，请手动关闭"))
        return ok

    def launch_app(app):
        for p in app.get("launch", []):
            if os.path.exists(p):
                subprocess.Popen([p], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                log("启动 %s" % app["name"])
                return
        log("未找到 %s 的启动路径（可在 config.json 的 apps 里配置）。" % app["name"])
else:
    def app_running(app):
        try:
            r = subprocess.run(["pgrep", "-f", app["path"]], capture_output=True, text=True)
            return r.returncode == 0 and bool(r.stdout.strip())
        except OSError:
            return False

    def _wait_gone(app, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not app_running(app):
                return True
            time.sleep(0.25)
        return not app_running(app)

    def quit_app(app, progress=None):
        if not app_running(app):
            return True
        if progress:
            progress("正在退出 %s…" % app["name"])
        try:
            subprocess.run(["osascript", "-e", 'tell application "%s" to quit' % app["name"]],
                           capture_output=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if _wait_gone(app, 10):
            log("%s 已退出" % app["name"])
            return True
        # 正常退出失败：发 SIGTERM，再不行才 SIGKILL
        try:
            subprocess.run(["pkill", "-TERM", "-f", app["path"]], capture_output=True)
        except OSError:
            pass
        if _wait_gone(app, 5):
            log("%s 已退出（SIGTERM）" % app["name"])
            return True
        try:
            subprocess.run(["pkill", "-KILL", "-f", app["path"]], capture_output=True)
        except OSError:
            pass
        ok = _wait_gone(app, 3)
        log("%s %s" % (app["name"], "已强制退出" if ok else "无法退出，请手动关闭"))
        return ok

    def launch_app(app):
        try:
            subprocess.Popen(["open", "-a", app["name"]],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("启动 %s" % app["name"])
        except OSError as e:
            log("启动 %s 失败：%s" % (app["name"], e))


def running_apps():
    return [a for a in get_apps() if app_running(a)]


def quit_all(progress=None):
    all_ok = True
    for a in get_apps():
        all_ok = quit_app(a, progress) and all_ok
    return all_ok


def reveal_path(path):
    try:
        if IS_WIN:
            os.startfile(path)  # noqa: PTH
        elif IS_LINUX:
            subprocess.Popen(["xdg-open", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["open", path])
    except OSError:
        pass


def _wait_file_stable(path, timeout=3.0):
    """等客户端退出后把登录文件写完"""
    last = -1
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st = os.stat(path)
            cur = (st.st_mtime_ns, st.st_size)
        except OSError:
            cur = None
        if cur == last:
            return True
        last = cur
        time.sleep(0.25)
    return True


# ---------------------------------------------------------------- 切号

def switch_to(doc, restart=None, switch_ide=None, dry_run=False, progress=None,
              do_wb=None, do_ide=None):
    """执行切换。返回结果行列表，第一行是标题；失败抛异常。
    切换范围由配置 switch_scope 决定（both / workbuddy / ide），
    do_wb / do_ide / switch_ide 可显式覆盖（命令行用）。"""
    def say(msg):
        log(msg)
        if progress:
            progress(msg)

    ok, msg = validate(doc)
    if not ok:
        raise ValueError(msg)

    cfg = load_config()
    if restart is None:
        restart = cfg.get("restart_after_switch", True)
    scope = cfg.get("switch_scope", "both")
    if do_wb is None:
        do_wb = (scope != "ide") if switch_ide is None else True
    if do_ide is None:
        if switch_ide is not None:
            do_ide = switch_ide
        else:
            do_ide = scope != "workbuddy"
        do_ide = do_ide and ide_available()

    uid = account_uid(doc)
    label = account_label(doc)
    exp = expiry_of(doc)
    was_running = running_apps()
    template = read_info()

    if dry_run:
        return ["预览：将切换到 %s（%s）" % (label, uid),
                "有效期至：%s%s" % (fmt_ts(exp),
                                    "（已过期）" if exp and exp < int(time.time() * 1000) else ""),
                "WorkBuddy：%s；CodeBuddy IDE：%s"
                % ("切换" if do_wb else "不动", "同步" if do_ide else "跳过")]

    say("退出客户端…")
    quit_all(progress)

    say("备份当前登录…")
    backup_current("before-" + uid[:8])
    _wait_file_stable(TARGET)

    lines = ["已切换为 %s" % label]
    if do_wb:
        say("写入 WorkBuddy 登录…")
        merged = make_doc(doc, read_info())
        write_info(merged)
        check = read_info()
        if account_uid(check) != uid:
            raise RuntimeError("写入后校验失败，登录文件里的账号不是目标账号。"
                               "请用「恢复备份」还原。")
        lines.append("WorkBuddy：已切换（有效期至 %s）" % fmt_ts(exp))
        if exp and exp < int(time.time() * 1000):
            lines.append("注意：该账号 token 已过期，启动后可能需要重新登录。")
        if template and account_uid(template) == uid:
            lines.append("提示：切换前登录文件已是该账号。")
    else:
        lines.append("WorkBuddy：未切换（已设置为只切 CodeBuddy IDE）。")

    if do_ide:
        say("同步 CodeBuddy IDE 登录…")
        try:
            inject_ide(doc, progress)
            lines.append("CodeBuddy IDE：已同步")
        except Exception as e:  # noqa: BLE001
            log("IDE 同步失败：%s" % e)
            lines.append("CodeBuddy IDE：未同步（%s）" % str(e).splitlines()[0][:80])
    else:
        lines.append("CodeBuddy IDE：跳过（未安装或已设置为只切 WorkBuddy）")

    if restart and was_running:
        say("重启客户端…")
        for a in was_running:
            launch_app(a)
        lines.append("已重启：" + "、".join(a["name"] for a in was_running))
    return lines


# ---------------------------------------------------------------- 联网刷新 / 系统外观

# 允许联网刷新的登录服务器（防止恶意 json 把 token 转发到别处）
ALLOWED_REFRESH_HOSTS = ("www.codebuddy.cn", "codebuddy.cn",
                         "www.workbuddy.cn", "workbuddy.cn")


def token_endpoint_of(doc):
    """从 token 的 iss/azp 推断登录服务器（Keycloak）的 token 端点和 client_id。
    只接受 https 且域名在白名单内的签发方。"""
    import urllib.parse
    auth = (doc or {}).get("auth") or {}
    for key in ("refreshToken", "accessToken"):
        tok = auth.get(key)
        if not (isinstance(tok, str) and tok.count(".") >= 2):
            continue
        claims = jwt_claims(tok)
        iss = claims.get("iss") or ""
        if "/realms/" not in iss:
            continue
        parts = urllib.parse.urlsplit(iss)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_REFRESH_HOSTS:
            raise ValueError("登录服务器不在允许名单内（%s）" % parts.hostname)
        return (iss.rstrip("/") + "/protocol/openid-connect/token",
                claims.get("azp") or claims.get("client_id") or "console")
    return None, None


def refresh_profile_tokens(doc):
    """用档案里明文的 refreshToken 联网换新 token（Keycloak OIDC 标准接口）。
    密文档案（本机抓取的）没有明文 refreshToken，无法远程刷新。成功返回更新后的 doc。"""
    import urllib.error
    import urllib.parse
    import urllib.request

    auth = doc.get("auth") or {}
    rt = auth.get("refreshToken")
    if not (isinstance(rt, str) and rt):
        raise ValueError("档案里的 token 是密文（本机抓取），无法联网续期；"
                         "可重新导入明文 json，或等客户端续期后「保存选中账号」")
    url, client_id = token_endpoint_of(doc)
    if not url:
        raise ValueError("无法从 token 识别登录服务器地址")
    data = urllib.parse.urlencode({"grant_type": "refresh_token",
                                   "refresh_token": rt,
                                   "client_id": client_id}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type":
                                          "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:120]
        raise RuntimeError("服务器拒绝刷新（%s）：%s" % (e.code, detail))
    except (OSError, ValueError) as e:
        raise RuntimeError("网络请求失败：%s" % e)
    if "access_token" not in out:
        raise RuntimeError("服务器返回里没有 access_token")

    now = int(time.time() * 1000)
    auth["accessToken"] = out["access_token"]
    auth["expiresIn"] = int(out.get("expires_in", 0))
    auth["expiresAt"] = now + auth["expiresIn"] * 1000
    auth["lastRefreshTime"] = now
    if out.get("refresh_token"):
        auth["refreshToken"] = out["refresh_token"]
        auth["refreshExpiresIn"] = int(out.get("refresh_expires_in", 0))
        auth["refreshExpiresAt"] = now + auth["refreshExpiresIn"] * 1000
    if out.get("session_state"):
        auth["sessionState"] = out["session_state"]
    # 昵称是密文时，顺手用新 token 里的用户名补上，列表能认人
    acc = doc.setdefault("account", {})
    if isinstance(acc.get("nickname"), dict):
        claims = jwt_claims(out["access_token"])
        for key in ("preferred_username", "phone_number"):
            v = claims.get(key)
            if isinstance(v, str) and v.strip():
                acc["nickname"] = v.strip()
                break
    return doc


def system_dark():
    """当前系统是否深色模式（界面库各自跟随系统，这里只给需要手动适配的场合用）"""
    if IS_LINUX:
        try:
            r = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True, timeout=5)
            return "dark" in (r.stdout or "").lower()
        except (OSError, subprocess.SubprocessError):
            return False
    if IS_WIN:
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            try:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            finally:
                winreg.CloseKey(key)
            return value == 0
        except OSError:
            return False
    try:
        r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                           capture_output=True, text=True, timeout=5)
        return "Dark" in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- 图形界面

def gui():
    """图形界面（PySide6 / Qt，Windows / macOS / Linux 原生外观），见 buddy_gui.py"""
    from buddy_gui import run
    return run()


# ---------------------------------------------------------------- 命令行

def cli(argv):
    ensure_dirs()
    if not argv or "--help" in argv or "-h" in argv:
        print("用法：\n"
              "  buddy_switch.py                       # 打开图形界面\n"
              "  buddy_switch.py list                  # 列出账号\n"
              "  buddy_switch.py current               # 当前登录\n"
              "  buddy_switch.py import <file.json>    # 导入账号\n"
              "  buddy_switch.py capture               # 保存当前账号\n"
              "  buddy_switch.py switch <uid|file> [--no-restart] [--no-ide] [--no-wb] [--dry-run]\n"
              "  buddy_switch.py refresh <uid|file>        # 联网续期（需明文 token 档案）\n"
              "  buddy_switch.py backups               # 列出备份")
        return 0

    cmd = argv[0]
    if cmd == "list":
        for p in list_profiles():
            print("%-24s %s  %s" % (p["label"], p["uid"], fmt_ts(p["expiry"])))
        return 0
    if cmd == "current":
        doc = read_info()
        if not doc:
            print("没有登录文件")
            return 1
        print("%s\nUID: %s\n到期: %s" % (account_label(doc), account_uid(doc),
                                        fmt_ts(expiry_of(doc))))
        return 0
    if cmd == "import" and len(argv) > 1:
        pp, doc = import_profile(argv[1])
        print("已导入 %s -> %s" % (account_label(doc), pp))
        return 0
    if cmd == "capture":
        pp, doc = capture_current()
        print("已保存 %s -> %s" % (account_label(doc), pp))
        return 0
    if cmd == "backups":
        for b in list_backups():
            print("%s  %s  %s" % (datetime.fromtimestamp(b["mtime"]).strftime("%Y-%m-%d %H:%M:%S"),
                                  b["label"], b["path"]))
        return 0
    if cmd == "switch" and len(argv) > 1:
        key = argv[1]
        doc = None
        if os.path.exists(os.path.expanduser(key)):
            doc = read_json(resolve_user_file(key))
        else:
            for p in list_profiles():
                if p["uid"] == key or p["label"] == key:
                    doc = p["doc"]
                    break
            if doc is None:
                print("找不到账号：%s" % key)
                return 1
        do_wb = False if "--no-wb" in argv else None
        do_ide = False if "--no-ide" in argv else None
        for line in switch_to(doc,
                              restart=("--no-restart" not in argv),
                              do_wb=do_wb, do_ide=do_ide,
                              dry_run=("--dry-run" in argv),
                              progress=print):
            print(line)
        return 0
    if cmd == "refresh" and len(argv) > 1:
        key = argv[1]
        doc = None
        if os.path.exists(os.path.expanduser(key)):
            doc = read_json(resolve_user_file(key))
        else:
            for p in list_profiles():
                if p["uid"] == key or p["label"] == key:
                    doc = p["doc"]
                    break
            if doc is None:
                print("找不到账号：%s" % key)
                return 1
        doc = refresh_profile_tokens(doc)
        if os.path.exists(os.path.expanduser(key)):
            print("已续期（未写回档案，请用 import 保存）")
        else:
            write_info(doc, os.path.join(PROFILE_DIR, "%s.json"
                                         % safe_uid(account_uid(doc))))
            print("已联网续期并保存，有效期至 %s" % fmt_ts(expiry_of(doc)))
        return 0
    print("未知命令，使用 --help 查看用法")
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    sys.exit(gui() or 0)
