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
HOME = os.path.expanduser("~")
APP_NAME = "Buddy 切号器"
UI_FONT = "Microsoft YaHei UI" if IS_WIN else "PingFang SC"
MONO_FONT = "Consolas" if IS_WIN else "Menlo"


def _base_dir():
    if IS_WIN:
        return os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
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
else:
    DEFAULT_APPS = [
        {"name": "CodeBuddy CN", "path": "/Applications/CodeBuddy CN.app"},
        {"name": "WorkBuddy", "path": "/Applications/WorkBuddy.app"},
    ]

# 高级选项（config.json 可覆盖）：restart_after_switch / switch_ide /
# auto_refresh / auto_refresh_minutes / auth_file / ide_db / apps
DEFAULT_CONFIG = {"restart_after_switch": True, "switch_ide": True,
                  "auto_refresh": True, "auto_refresh_minutes": 60}


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
    out = dict(DEFAULT_CONFIG)
    out.update(cfg if isinstance(cfg, dict) else {})
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


def capture_current():
    doc = read_info()
    if not doc:
        raise ValueError("当前没有登录文件，无法保存。")
    ensure_dirs()
    uid = safe_uid(account_uid(doc))
    pp = os.path.join(PROFILE_DIR, "%s.json" % uid)
    # 客户端刷新 token 后会把昵称/手机号加密回写；归档时保留档案里已知的明文，
    # 这样列表不至于全部变成「账号 xxxx」
    if os.path.exists(pp):
        try:
            old_acc = (read_json(pp).get("account") or {})
            acc = doc.setdefault("account", {})
            for field in ("nickname", "phoneNumber"):
                if isinstance(acc.get(field), dict) and isinstance(old_acc.get(field), str):
                    acc[field] = old_acc[field]
        except (OSError, ValueError):
            pass
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
        out.append({
            "path": path,
            "uid": account_uid(doc) or fn[:-5],
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

def switch_to(doc, restart=None, switch_ide=None, dry_run=False, progress=None):
    """执行切换。返回结果行列表，第一行是标题；失败抛异常。"""
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
    if switch_ide is None:
        switch_ide = cfg.get("switch_ide", True) and ide_available()

    uid = account_uid(doc)
    label = account_label(doc)
    exp = expiry_of(doc)
    was_running = running_apps()
    template = read_info()

    if dry_run:
        return ["预览：将切换到 %s（%s）" % (label, uid),
                "有效期至：%s%s" % (fmt_ts(exp),
                                    "（已过期）" if exp and exp < int(time.time() * 1000) else ""),
                "WorkBuddy：切换；CodeBuddy IDE：%s" % ("同步" if switch_ide else "跳过")]

    say("退出客户端…")
    quit_all(progress)

    say("备份当前登录…")
    backup_current("before-" + uid[:8])
    _wait_file_stable(TARGET)

    say("写入 WorkBuddy 登录…")
    merged = make_doc(doc, read_info())
    write_info(merged)
    check = read_info()
    if account_uid(check) != uid:
        raise RuntimeError("写入后校验失败，登录文件里的账号不是目标账号。请用「恢复备份」还原。")

    lines = ["已切换为 %s" % label,
             "WorkBuddy：已切换（有效期至 %s）" % fmt_ts(exp)]
    if exp and exp < int(time.time() * 1000):
        lines.append("注意：该账号 token 已过期，启动后可能需要重新登录。")
    if template and account_uid(template) == uid:
        lines.append("提示：切换前登录文件已是该账号。")

    if switch_ide:
        say("同步 CodeBuddy IDE 登录…")
        try:
            inject_ide(doc, progress)
            lines.append("CodeBuddy IDE：已同步")
        except Exception as e:  # noqa: BLE001
            log("IDE 同步失败：%s" % e)
            lines.append("CodeBuddy IDE：未同步（%s）" % str(e).splitlines()[0][:80])
    else:
        lines.append("CodeBuddy IDE：跳过（未安装或已在配置中关闭）")

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
    """当前系统是否深色模式"""
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
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    ensure_dirs()
    cfg = load_config()

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("780x580")
    root.minsize(680, 480)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)   # 列表 + 日志（拖动分隔条调整占比）

    PALETTES = {
        "light": {"page": "#f5f6f8", "card": "#ffffff", "text": "#1f2328",
                  "subtle": "#6b7280", "select": "#3478f6",
                  "status_bg": "#ebedf0", "status_fg": "#374151"},
        "dark": {"page": "#1d1d20", "card": "#27272a", "text": "#d7d7db",
                 "subtle": "#9a9aa2", "select": "#3478f6",
                 "status_bg": "#27272a", "status_fg": "#c6c6cb"},
    }
    theme_now = {"key": None}
    cards = []
    style = ttk.Style()
    for theme in (("aqua", "vista") if not IS_WIN else ("vista", "aqua")):
        try:
            style.theme_use(theme)
            break
        except tk.TclError:
            continue
    style.configure("Big.TButton", font=(UI_FONT, 13, "bold"))

    def apply_theme(force=False):
        name = cfg.get("theme", "auto")
        if name == "auto":
            name = "dark" if system_dark() else "light"
        if not force and theme_now["key"] == name:
            return
        theme_now["key"] = name
        pal = PALETTES[name]
        root.configure(background=pal["page"])
        style.configure("TFrame", background=pal["page"])
        style.configure("Title.TLabel", background=pal["page"],
                        foreground=pal["text"], font=(UI_FONT, 18, "bold"))
        style.configure("Subtle.TLabel", background=pal["page"],
                        foreground=pal["subtle"], font=(UI_FONT, 11))
        style.configure("Status.TFrame", background=pal["status_bg"])
        style.configure("Status.TLabel", background=pal["status_bg"],
                        foreground=pal["status_fg"], font=(UI_FONT, 11))
        style.configure("Treeview", rowheight=30, background=pal["card"],
                        fieldbackground=pal["card"], foreground=pal["text"],
                        borderwidth=0, font=(UI_FONT, 12))
        style.configure("Treeview.Heading", font=(UI_FONT, 11),
                        background=pal["card"], foreground=pal["subtle"])
        style.map("Treeview",
                  background=[("selected", pal["select"])],
                  foreground=[("selected", "#ffffff")])
        tree.tag_configure("selrow", background=pal["select"], foreground="#ffffff")
        log_box.configure(background=pal["card"], foreground=pal["text"],
                          insertbackground=pal["text"])
        paned.configure(background=pal["page"])
        for card in cards:
            card.retheme(pal)

    def watch_theme():
        # 「跟随系统」时每分钟检查一次系统外观变化
        if cfg.get("theme", "auto") == "auto":
            apply_theme()
        root.after(60000, watch_theme)

    class Card(tk.Canvas):
        """无边框圆角卡片（macOS 访达风格），内容放进 .body。
        圆角用两块矩形 + 四个四分之一圆拼出，边角是真正的圆弧。"""

        def __init__(self, master):
            super().__init__(master, highlightthickness=0, bd=0)
            self._items = [
                self.create_rectangle(0, 0, 0, 0, outline=""),
                self.create_rectangle(0, 0, 0, 0, outline=""),
                self.create_arc(0, 0, 0, 0, start=0, extent=90, outline=""),
                self.create_arc(0, 0, 0, 0, start=90, extent=90, outline=""),
                self.create_arc(0, 0, 0, 0, start=180, extent=90, outline=""),
                self.create_arc(0, 0, 0, 0, start=270, extent=90, outline=""),
            ]
            self.body = tk.Frame(self)
            self.body.pack_propagate(False)
            self._win = self.create_window(0, 0, window=self.body, anchor="nw")
            self.bind("<Configure>", self._on_resize)
            cards.append(self)

        def retheme(self, pal):
            self.configure(background=pal["page"])
            self.body.configure(background=pal["card"])
            for item in self._items:
                self.itemconfigure(item, fill=pal["card"])

        def _on_resize(self, _event):
            w, h = self.winfo_width(), self.winfo_height()
            r = 10
            rect_h, rect_v, ne, nw, sw, se = self._items
            self.coords(rect_h, r, 0, w - r, h)
            self.coords(rect_v, 0, r, w, h - r)
            self.coords(ne, w - 2 * r, 0, w, 2 * r)
            self.coords(nw, 0, 0, 2 * r, 2 * r)
            self.coords(sw, 0, h - 2 * r, 2 * r, h)
            self.coords(se, w - 2 * r, h - 2 * r, w, h)
            self.coords(self._win, 6, 6)
            self.itemconfigure(self._win, width=max(20, w - 12), height=max(20, h - 12))

    # 0 顶部：标题 + 当前账号
    head = ttk.Frame(root, padding=(18, 14, 18, 6))
    head.grid(row=0, column=0, sticky="ew")
    ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
    current_var = tk.StringVar(value="读取中…")
    ttk.Label(head, textvariable=current_var, style="Subtle.TLabel").pack(
        side="left", padx=(16, 0), pady=(6, 0))

    # 1 账号列表 + 日志：圆角卡片，中间分隔条可拖动
    paned = tk.PanedWindow(root, orient="vertical", sashwidth=6, sashrelief="flat",
                           bd=0, showhandle=False, opaqueresize=True)
    paned.grid(row=1, column=0, sticky="nsew", padx=14, pady=(4, 0))
    list_card = Card(paned)
    log_card = Card(paned)
    paned.add(list_card, minsize=140, stretch="always")
    paned.add(log_card, minsize=30, stretch="never")

    list_body = list_card.body
    list_body.columnconfigure(0, weight=1)
    list_body.rowconfigure(0, weight=1)
    cols = ("name", "uid", "expiry")
    tree = ttk.Treeview(list_body, columns=cols, show="headings", selectmode="extended")
    tree.heading("name", text="账号（双击切换，可多选）")
    tree.heading("uid", text="UID")
    tree.heading("expiry", text="有效期至")
    tree.column("name", width=240, anchor="w")
    tree.column("uid", width=230, anchor="w")
    tree.column("expiry", width=140, anchor="w")
    vsb = ttk.Scrollbar(list_body, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")

    log_box = tk.Text(log_card.body, height=4, wrap="word", state="disabled",
                      relief="flat", borderwidth=0, highlightthickness=0,
                      padx=10, pady=8, font=(MONO_FONT, 10))
    log_box.pack(fill="both", expand=True)

    # 2 工具栏（其余操作都在右键菜单里）
    bar = ttk.Frame(root, padding=(18, 10, 18, 6))
    bar.grid(row=2, column=0, sticky="ew")
    btn_import = ttk.Button(bar, text="导入账号…")
    btn_import.pack(side="left")
    btn_capture = ttk.Button(bar, text="保存选中账号")
    btn_capture.pack(side="left", padx=(8, 0))
    btn_switch = ttk.Button(bar, text="切换账号 ▶", style="Big.TButton")
    btn_switch.pack(side="right", ipady=3)

    # 3 状态栏（右侧小按钮）
    status_bar = ttk.Frame(root, style="Status.TFrame")
    status_bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
    status_var = tk.StringVar(value="就绪")
    ttk.Label(status_bar, textvariable=status_var, style="Status.TLabel",
              anchor="w").pack(side="left", fill="x", expand=True, padx=(10, 0))
    btn_settings = ttk.Button(status_bar, text="设置…", width=7)
    btn_settings.pack(side="right", padx=(4, 6), pady=3)
    btn_log = ttk.Button(status_bar, text="隐藏日志", width=9)
    btn_log.pack(side="right", padx=4, pady=3)

    busy = {"value": False}
    buttons = [btn_switch, btn_import, btn_capture]
    log_ui = {"hidden": False, "sash": 0}
    timer = {"id": None}

    def append_log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", msg.rstrip() + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    def set_busy(flag):
        busy["value"] = flag
        state = "disabled" if flag else "normal"
        for b in buttons:
            try:
                b.configure(state=state)
            except tk.TclError:
                pass

    # 日志面板：拖到最小自动收起，状态栏按钮展开/收起
    def sash_get():
        return paned.sash_coord(0)[1]

    def sash_set(pos):
        paned.sash_place(0, 0, int(pos))

    def collapse_log():
        if log_ui["hidden"]:
            return
        log_ui["sash"] = sash_get()
        paned.forget(log_card)
        log_ui["hidden"] = True
        btn_log.configure(text="显示日志")

    def expand_log():
        if not log_ui["hidden"]:
            return
        paned.add(log_card, minsize=30, stretch="never")
        pos = log_ui["sash"] or max(220, paned.winfo_height() - 170)
        root.after_idle(lambda: sash_set(pos))
        log_ui["hidden"] = False
        btn_log.configure(text="隐藏日志")

    def toggle_log():
        collapse_log() if not log_ui["hidden"] else expand_log()

    def on_pane_motion(_event):
        # 像 VS Code 一样：拖到底线立即收起，不用等松开鼠标
        if not log_ui["hidden"] and log_card.winfo_height() <= 38:
            collapse_log()

    def on_sash_release(_event):
        if not log_ui["hidden"] and log_card.winfo_height() <= 40:
            collapse_log()

    # 多选选中色兜底：原生主题不吃 style.map 时用 tag 上色
    def on_select(_event):
        sel = set(tree.selection())
        for iid in tree.get_children():
            tags = [t for t in (tree.item(iid, "tags") or ()) if t != "selrow"]
            if iid in sel:
                tags.append("selrow")
            tree.item(iid, tags=tags)

    # 拖动框选（macOS 原生 Treeview 不带橡皮筋选择，自己实现；
    # 按住 Shift / Ctrl / Command 时不介入，交给系统行为）
    drag_sel = {"anchor": None, "active": False}

    def on_tree_press(event):
        drag_sel["anchor"] = tree.identify_row(event.y) or None
        drag_sel["active"] = False

    def on_tree_motion(event):
        if event.state & 0x000D or not drag_sel["anchor"] or busy["value"]:
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        if iid != drag_sel["anchor"]:
            drag_sel["active"] = True
        if not drag_sel["active"]:
            return
        kids = tree.get_children()
        try:
            a, b = kids.index(drag_sel["anchor"]), kids.index(iid)
        except ValueError:
            return
        tree.selection_set(kids[min(a, b):max(a, b) + 1])

    def refresh(select_uids=None):
        sel = tree.selection()
        keep = set(select_uids or [])
        if not keep:
            keep = {os.path.basename(s)[:-5] for s in sel}
        for i in tree.get_children():
            tree.delete(i)
        cur = read_info()
        cur_uid = account_uid(cur) if cur else ""
        if cur:
            current_var.set("当前登录：%s（有效期至 %s）"
                            % (account_label(cur), fmt_ts(expiry_of(cur))))
        else:
            current_var.set("未找到登录文件")
        now = int(time.time() * 1000)
        for p in list_profiles():
            name = ("● " if p["uid"] == cur_uid else "") + p["label"]
            exp_txt = fmt_ts(p["expiry"])
            if p["expiry"] and p["expiry"] < now:
                exp_txt += "（已过期）"
            tree.insert("", "end", iid=p["path"],
                        values=(name, p["uid"], exp_txt))
        iids = [os.path.join(PROFILE_DIR, "%s.json" % safe_uid(u)) for u in keep]
        live = [i for i in iids if i in tree.get_children()]
        if live:
            tree.selection_set(live)
            tree.focus(live[0])
            tree.see(live[0])

    def profile_map():
        return {p["path"]: p for p in list_profiles()}

    def switch_source():
        """切换用哪个账号：焦点行优先，否则第一个选中行"""
        sel = tree.selection()
        if not sel:
            status_var.set("请先在列表里选择一个账号")
            return None
        iid = tree.focus()
        if iid not in sel:
            iid = sel[0]
        try:
            return read_json(iid)
        except (OSError, ValueError) as e:
            messagebox.showerror(APP_NAME, "读取账号文件失败：%s" % e, parent=root)
            return None

    def do_switch(doc):
        """唯一的切换入口：忙时直接忽略，从根上避免重复弹窗/重复切换"""
        if busy["value"]:
            return
        uid = account_uid(doc)
        running = running_apps()
        set_busy(True)
        if running:  # 只有客户端在运行、需要退出时才确认一次
            names = "、".join(a["name"] for a in running)
            if not messagebox.askyesno(
                    APP_NAME,
                    "切换到 %s？\n将退出并重启：%s（请先保存未保存的文件）"
                    % (account_label(doc), names), parent=root):
                set_busy(False)
                status_var.set("已取消")
                return
        q = queue.Queue()

        def worker():
            try:
                lines = switch_to(doc, progress=lambda m: q.put(("log", m)))
                q.put(("done", lines))
            except Exception as e:  # noqa: BLE001
                q.put(("error", str(e)))

        status_var.set("正在切换，请稍候…")
        append_log("—— 切换到 %s ——" % account_label(doc))
        threading.Thread(target=worker, daemon=True).start()

        def poll():
            finished = False
            try:
                while True:
                    kind, payload = q.get_nowait()
                    if kind == "log":
                        append_log(payload)
                        status_var.set(payload)
                    elif kind == "done":
                        finished = True
                        for line in payload[1:]:
                            append_log(line)
                        append_log(payload[0])
                        status_var.set("✓ " + payload[0])
                        refresh([uid])  # 完成不用弹窗，界面内直接反馈
                        set_busy(False)
                        if any(l.startswith("已重启") for l in payload[1:]):
                            # 客户端重启后会自动续期 token，稍后把新登录归档
                            append_log("客户端重启 2 分钟后自动归档刷新后的登录…")
                            root.after(120000, auto_capture_quiet)
                    elif kind == "error":
                        finished = True
                        append_log("失败：%s" % payload)
                        status_var.set("切换失败")
                        # 弹窗期间保持忙碌状态，避免事件重入造成第二个弹窗
                        messagebox.showerror(APP_NAME, payload, parent=root)
                        set_busy(False)
            except queue.Empty:
                pass
            if not finished and busy["value"]:
                root.after(120, poll)

        root.after(120, poll)

    def on_switch(_event=None):
        doc = switch_source()
        if doc:
            do_switch(doc)

    def auto_capture_quiet():
        """静默归档当前登录（客户端续期 token 后把新有效期存进档案）"""
        if busy["value"]:
            return
        try:
            _, doc = capture_current()
        except (OSError, ValueError):
            return
        refresh()
        append_log("已自动刷新当前登录归档：%s（有效期至 %s）"
                   % (account_label(doc), fmt_ts(expiry_of(doc))))

    def schedule_auto():
        if timer["id"] is not None:
            try:
                root.after_cancel(timer["id"])
            except tk.TclError:
                pass
        minutes = max(1, int(cfg.get("auto_refresh_minutes", 60) or 60))
        timer["id"] = root.after(minutes * 60000, auto_tick)

    def auto_tick():
        timer["id"] = None
        if cfg.get("auto_refresh", True):
            auto_capture_quiet()
        schedule_auto()

    def on_import():
        if busy["value"]:
            return
        path = filedialog.askopenfilename(
            parent=root, title="选择 workbuddy-<uid>.json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*")])
        if not path:
            return
        try:
            _, doc = import_profile(path)
        except (OSError, ValueError) as e:
            messagebox.showerror(APP_NAME, str(e), parent=root)
            return
        uid = account_uid(doc)
        refresh([uid])
        append_log("已导入：%s（%s）" % (account_label(doc), uid))
        status_var.set("已导入 %s，双击列表即可切换" % account_label(doc))

    def on_capture():
        """保存选中账号：当前登录的账号才有新数据（客户端续期后的 token）"""
        if busy["value"]:
            return
        cur = read_info()
        if not cur:
            messagebox.showinfo(APP_NAME, "当前没有登录文件。", parent=root)
            return
        cur_uid = account_uid(cur)
        pmap = profile_map()
        sel_uids = [pmap[i]["uid"] for i in tree.selection() if i in pmap]
        if sel_uids and cur_uid not in sel_uids:
            messagebox.showinfo(
                APP_NAME,
                "选中的账号不是当前登录的账号（标 ● 的是 %s），没有新数据可保存。\n"
                "要刷新哪个账号，先切换过去再保存。" % account_label(cur),
                parent=root)
            return
        try:
            _, doc = capture_current()
        except (OSError, ValueError) as e:
            messagebox.showerror(APP_NAME, str(e), parent=root)
            return
        refresh([cur_uid])
        append_log("已保存当前登录：%s（有效期至 %s）"
                   % (account_label(doc), fmt_ts(expiry_of(doc))))
        status_var.set("已保存 %s" % account_label(doc))

    def on_refresh(_event=None):
        """手动刷新有效期：明文档案联网换新 token；当前登录顺便重读归档"""
        if busy["value"]:
            return
        sel = tree.selection()
        pmap = profile_map()
        items = [(pmap[i]["uid"], pmap[i]["doc"]) for i in sel if i in pmap]
        if not items:
            status_var.set("请先选择要刷新的账号")
            return
        q = queue.Queue()
        refreshed = set()

        def worker():
            for uid, doc in items:
                try:
                    doc = refresh_profile_tokens(doc)
                except (ValueError, RuntimeError, OSError) as e:
                    q.put(("log", "%s：未续期（%s）"
                           % (account_label(doc), str(e).splitlines()[0][:60])))
                    continue
                try:
                    write_info(doc, os.path.join(PROFILE_DIR, "%s.json" % safe_uid(uid)))
                    refreshed.add(uid)
                    q.put(("log", "%s：已联网续期至 %s"
                           % (account_label(doc), fmt_ts(expiry_of(doc)))))
                except OSError as e:
                    q.put(("log", "%s：续期成功但保存失败（%s）" % (account_label(doc), e)))
            q.put(("done", None))

        set_busy(True)
        status_var.set("正在刷新有效期…")
        append_log("—— 刷新有效期（%d 个账号）——" % len(items))
        threading.Thread(target=worker, daemon=True).start()

        def poll():
            finished = False
            try:
                while True:
                    kind, payload = q.get_nowait()
                    if kind == "log":
                        append_log(payload)
                        status_var.set(payload)
                    elif kind == "done":
                        finished = True
                        # 当前登录的账号重读一次登录文件归档（客户端可能已自行续期）；
                        # 刚联网续期过的不再覆盖，避免用旧 token 盖掉新 token
                        cur = read_info()
                        if cur and account_uid(cur) not in refreshed:
                            before = expiry_of(cur)
                            try:
                                _, captured = capture_current()
                                after = expiry_of(captured)
                                append_log("当前登录归档已更新（有效期至 %s）"
                                           % fmt_ts(after))
                                if after == before and not refreshed:
                                    append_log("说明：token 离到期还远时，客户端不会向"
                                               "服务器换新 token，有效期不变属正常；"
                                               "临近到期时再切换登录一次即可滚动续期。")
                            except (OSError, ValueError):
                                pass
                        refresh([u for u, _ in items])
                        set_busy(False)
                        status_var.set("刷新完成")
            except queue.Empty:
                pass
            if not finished and busy["value"]:
                root.after(120, poll)

        root.after(120, poll)

    def on_delete():
        if busy["value"]:
            return
        sel = tree.selection()
        if not sel:
            return
        pmap = profile_map()
        uids = [pmap[i]["uid"] for i in sel if i in pmap]
        if not uids:
            return
        shown = "、".join(uids[:3]) + ("…" if len(uids) > 3 else "")
        if messagebox.askyesno(APP_NAME, "删除 %d 个本地档案？\n%s\n"
                               "（只删除工具里的存档，不影响客户端当前登录）"
                               % (len(uids), shown), parent=root):
            for u in uids:
                delete_profile(u)
            refresh()
            status_var.set("已删除 %d 个档案" % len(uids))

    def on_export():
        if busy["value"]:
            return
        sel = tree.selection()
        if not sel:
            status_var.set("请先选择要导出的账号")
            return
        pmap = profile_map()
        items = [(pmap[i]["uid"], pmap[i]["doc"]) for i in sel if i in pmap]
        if not items:
            return
        if len(items) == 1:
            uid, doc = items[0]
            path = filedialog.asksaveasfilename(
                parent=root, title="导出账号", defaultextension=".json",
                initialfile="workbuddy-%s.json" % safe_uid(uid),
                filetypes=[("JSON 文件", "*.json")])
            if not path:
                return
            dests = [(path, doc)]
        else:
            d = filedialog.askdirectory(
                parent=root, title="选择导出目录（%d 个账号）" % len(items))
            if not d:
                return
            dests = [(os.path.join(d, "workbuddy-%s.json" % safe_uid(u)), doc)
                     for u, doc in items]
        try:
            for dest, doc in dests:
                Path(dest).write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        except OSError as e:
            messagebox.showerror(APP_NAME, "导出失败：%s" % e, parent=root)
            return
        append_log("已导出 %d 个账号" % len(dests))
        status_var.set("已导出 %d 个账号" % len(dests))

    def on_restore():
        if busy["value"]:
            return
        backups = list_backups()
        if not backups:
            messagebox.showinfo(APP_NAME, "还没有备份。", parent=root)
            return
        win = tk.Toplevel(root)
        win.title("恢复备份")
        win.geometry("560x360")
        win.transient(root)
        lb = tk.Listbox(win, font=(UI_FONT, 12))
        lb.pack(fill="both", expand=True, padx=12, pady=12)
        for b in backups:
            lb.insert("end", "%s   %s   %s" % (
                datetime.fromtimestamp(b["mtime"]).strftime("%m-%d %H:%M"),
                b["label"], b["uid"][:8]))

        def pick(_event=None):
            sel = lb.curselection()
            if not sel:
                return
            b = backups[sel[0]]
            try:
                doc = read_json(b["path"])
            except (OSError, ValueError) as e:
                messagebox.showerror(APP_NAME, "备份无法读取：%s" % e, parent=win)
                return
            win.destroy()
            do_switch(doc)

        def close():
            if not busy["value"]:
                win.destroy()

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(btns, text="恢复此备份", command=pick).pack(side="left")
        ttk.Button(btns, text="取消", command=close).pack(side="right")
        lb.bind("<Double-1>", pick)
        win.protocol("WM_DELETE_WINDOW", close)

    THEME_LABELS = [("跟随系统", "auto"), ("浅色", "light"), ("深色", "dark")]
    INTERVALS = [("30 分钟", 30), ("1 小时", 60), ("3 小时", 180),
                 ("6 小时", 360), ("12 小时", 720), ("每天", 1440)]

    def on_settings():
        if busy["value"]:
            return
        win = tk.Toplevel(root)
        win.title("设置")
        win.resizable(False, False)
        win.transient(root)
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        theme_now_label = dict((v, k) for k, v in THEME_LABELS).get(
            cfg.get("theme", "auto"), "跟随系统")
        theme_var = tk.StringVar(value=theme_now_label)
        auto_var = tk.BooleanVar(value=bool(cfg.get("auto_refresh", True)))
        minutes = int(cfg.get("auto_refresh_minutes", 60) or 60)
        interval_var = tk.StringVar(
            value=next((lbl for lbl, m in INTERVALS if m == minutes), "1 小时"))
        restart_var = tk.BooleanVar(value=bool(cfg.get("restart_after_switch", True)))
        ide_var = tk.BooleanVar(value=bool(cfg.get("switch_ide", True)))

        row0 = ttk.Frame(body)
        row0.grid(row=0, column=0, sticky="w")
        ttk.Label(row0, text="外观：").pack(side="left")
        ttk.Combobox(row0, textvariable=theme_var, state="readonly", width=10,
                     values=[lbl for lbl, _ in THEME_LABELS]).pack(side="left")
        ttk.Checkbutton(body, text="定时刷新当前账号有效期"
                        "（客户端自动续期后，工具把新 token 归档）",
                        variable=auto_var).grid(row=1, column=0, sticky="w",
                                                pady=(12, 0))
        row1 = ttk.Frame(body)
        row1.grid(row=2, column=0, sticky="w", pady=(4, 0), padx=(24, 0))
        ttk.Label(row1, text="刷新间隔：").pack(side="left")
        ttk.Combobox(row1, textvariable=interval_var, state="readonly", width=10,
                     values=[lbl for lbl, _ in INTERVALS]).pack(side="left")
        ttk.Checkbutton(body, text="切换后自动重启客户端",
                        variable=restart_var).grid(row=3, column=0,
                                                   sticky="w", pady=(10, 0))
        ttk.Checkbutton(body, text="同步切换 CodeBuddy IDE 登录",
                        variable=ide_var).grid(row=4, column=0, sticky="w", pady=(6, 0))

        def save():
            cfg["theme"] = dict(THEME_LABELS)[theme_var.get()]
            cfg["auto_refresh"] = auto_var.get()
            cfg["auto_refresh_minutes"] = dict(INTERVALS)[interval_var.get()]
            cfg["restart_after_switch"] = restart_var.get()
            cfg["switch_ide"] = ide_var.get()
            save_config(cfg)
            schedule_auto()
            apply_theme(force=True)
            win.destroy()
            status_var.set("设置已保存")

        btns = ttk.Frame(body)
        btns.grid(row=5, column=0, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="确定", command=save).pack(side="left")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=(8, 0))

    menu = tk.Menu(tree, tearoff=0)
    menu.add_command(label="切换到该账号", command=on_switch)
    menu.add_command(label="刷新有效期", command=on_refresh)
    menu.add_command(label="导出选中账号…", command=on_export)
    menu.add_command(label="删除选中档案", command=on_delete)
    menu.add_separator()
    menu.add_command(label="恢复备份…", command=on_restore)
    menu.add_command(label="打开数据目录", command=lambda: reveal_path(STORE))

    def on_menu(event):
        iid = tree.identify_row(event.y)
        if iid and not busy["value"]:
            if iid not in tree.selection():
                tree.selection_set(iid)
            menu.tk_popup(event.x_root, event.y_root)

    btn_switch.configure(command=on_switch)
    btn_import.configure(command=on_import)
    btn_capture.configure(command=on_capture)
    btn_settings.configure(command=on_settings)
    btn_log.configure(command=toggle_log)
    paned.bind("<B1-Motion>", on_pane_motion)
    paned.bind("<ButtonRelease-1>", on_sash_release)
    tree.bind("<Double-1>", on_switch)
    tree.bind("<Return>", on_switch)
    tree.bind("<Button-3>", on_menu)
    tree.bind("<<TreeviewSelect>>", on_select)
    tree.bind("<Button-1>", on_tree_press, add="+")
    tree.bind("<B1-Motion>", on_tree_motion, add="+")

    apply_theme(force=True)
    refresh()
    append_log("数据目录：%s" % STORE)
    append_log("用法：双击列表切换；拖动或按住 Ctrl / Command 多选，"
               "右键可刷新有效期、导出、删除。")
    append_log("提示：首次同步 IDE 时如弹出钥匙串授权，请点「始终允许」。")
    root.after(150, lambda: sash_set(max(220, paned.winfo_height() - 165)))
    schedule_auto()
    watch_theme()
    root.mainloop()


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
              "  buddy_switch.py switch <uid|file> [--no-restart] [--no-ide] [--dry-run]\n"
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
        for line in switch_to(doc,
                              restart=("--no-restart" not in argv),
                              switch_ide=("--no-ide" not in argv),
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
    gui()
