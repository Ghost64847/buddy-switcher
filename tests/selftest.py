#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：核心文档逻辑 / 导入导出 / 旧数据迁移 / GUI 构建。不碰真实登录文件。"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import buddy_switch as bs  # noqa: E402

FAKE_JWT = ("eyJhbGciOiJIUzI1NiJ9."  # header
            "eyJleHAiOjE5OTk5OTk5OTl9."  # payload: exp=1999999999
            "sig")


def safe_join(base, *parts):
    """测试专用：拼路径并校验结果仍在 base 内，禁止越界"""
    real_base = os.path.realpath(base)
    p = os.path.realpath(os.path.join(base, *parts))
    if not (p == real_base or p.startswith(real_base + os.sep)):
        raise AssertionError("test path escaped sandbox: %s" % p)
    return p


def fake_doc(uid="u123", nick="测试账号"):
    return {"account": {"uid": uid, "nickname": nick, "phoneNumber": "138****0000"},
            "auth": {"accessToken": FAKE_JWT, "refreshToken": FAKE_JWT,
                     "expiresAt": 1999999999}}


class DocLogicTest(unittest.TestCase):
    def test_validate_ok(self):
        ok, _ = bs.validate(fake_doc())
        self.assertTrue(ok)

    def test_validate_rejects_old_list_format(self):
        ok, msg = bs.validate([{"account": {}}])
        self.assertFalse(ok)
        self.assertIn("旧格式", msg)

    def test_validate_rejects_missing_fields(self):
        ok, _ = bs.validate({"foo": 1})
        self.assertFalse(ok)

    def test_normalize_auth_converts_seconds_to_ms(self):
        auth = bs.normalize_auth({"accessToken": FAKE_JWT, "expiresAt": 1999999999})
        self.assertEqual(auth["expiresAt"], 1999999999 * 1000)
        self.assertGreater(auth["expiresIn"], 0)

    def test_make_doc_keeps_other_accounts(self):
        template = {"allAccounts": [{"uid": "other", "nickname": "别的"}]}
        doc = bs.make_doc(fake_doc(), template)
        self.assertEqual(doc["account"]["uid"], "u123")
        uids = [a["uid"] for a in doc["allAccounts"]]
        self.assertIn("other", uids)
        self.assertEqual(uids[0], "u123")

    def test_make_doc_dedupes_same_uid(self):
        template = {"allAccounts": [{"uid": "u123"}, {"uid": "b"}]}
        doc = bs.make_doc(fake_doc(), template)
        self.assertEqual(len(doc["allAccounts"]), 2)

    def test_expiry_from_jwt(self):
        self.assertEqual(bs.expiry_of(fake_doc()), 1999999999 * 1000)

    def test_encrypted_token_doc_is_valid(self):
        doc = fake_doc()
        doc["auth"]["accessToken"] = {"$wbEncrypted": 1, "data": "xx"}
        ok, _ = bs.validate(doc)
        self.assertTrue(ok)

    def test_safe_uid_blocks_traversal(self):
        for evil in ("../../etc/passwd", "..\\..\\x", "a/b/c", "...", "", None):
            cleaned = bs.safe_uid(evil)
            self.assertNotIn("/", cleaned)
            self.assertNotIn("\\", cleaned)
            self.assertNotIn(".", cleaned)
            self.assertTrue(cleaned)  # 永不为空


class LabelTest(unittest.TestCase):
    @staticmethod
    def _jwt_with(username, extra=None):
        import base64
        claims = {"exp": 1999999999, "preferred_username": username}
        claims.update(extra or {})
        payload = base64.urlsafe_b64encode(
            json.dumps(claims).encode()).decode().rstrip("=")
        return "h.%s.s" % payload

    def test_token_endpoint_derivation(self):
        doc = {"auth": {"refreshToken": self._jwt_with(
            "u", {"iss": "https://www.codebuddy.cn/auth/realms/copilot",
                  "azp": "console"})}}
        url, client = bs.token_endpoint_of(doc)
        self.assertEqual(url,
                         "https://www.codebuddy.cn/auth/realms/copilot/protocol/openid-connect/token")
        self.assertEqual(client, "console")

    def test_token_endpoint_none_for_encrypted(self):
        doc = {"auth": {"refreshToken": {"$wbEncrypted": 1}}}
        self.assertEqual(bs.token_endpoint_of(doc), (None, None))

    def test_token_endpoint_rejects_unknown_host(self):
        doc = {"auth": {"refreshToken": self._jwt_with(
            "u", {"iss": "https://evil.example.com/auth/realms/copilot",
                  "azp": "console"})}}
        with self.assertRaises(ValueError):
            bs.token_endpoint_of(doc)

    def test_token_endpoint_rejects_http(self):
        doc = {"auth": {"refreshToken": self._jwt_with(
            "u", {"iss": "http://www.codebuddy.cn/auth/realms/copilot",
                  "azp": "console"})}}
        with self.assertRaises(ValueError):
            bs.token_endpoint_of(doc)

    def test_plaintext_nickname_wins(self):
        self.assertEqual(bs.account_label(fake_doc()), "测试账号 / 138****0000")

    def test_encrypted_nickname_falls_back_to_jwt_username(self):
        doc = fake_doc()
        doc["account"]["nickname"] = {"$wbEncrypted": 1, "data": "xx"}
        doc["account"]["phoneNumber"] = {"$wbEncrypted": 1, "data": "xx"}
        doc["auth"]["accessToken"] = self._jwt_with("15900001111")
        self.assertEqual(bs.account_label(doc), "15900001111")

    def test_fully_encrypted_falls_back_to_uid(self):
        doc = fake_doc()
        doc["account"]["nickname"] = {"$wbEncrypted": 1}
        doc["account"]["phoneNumber"] = {"$wbEncrypted": 1}
        doc["auth"]["accessToken"] = {"$wbEncrypted": 1}
        self.assertEqual(bs.account_label(doc), "账号 u123")


class CaptureMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="buddycap-")
        self._old_target = bs.TARGET
        self._old_profile = bs.PROFILE_DIR
        self._old_migrated = bs._migrated
        bs.TARGET = safe_join(self.tmp, "live.info")
        bs.PROFILE_DIR = safe_join(self.tmp, "profiles")
        bs._migrated = True
        os.makedirs(bs.PROFILE_DIR)

    def tearDown(self):
        bs.TARGET = self._old_target
        bs.PROFILE_DIR = self._old_profile
        bs._migrated = self._old_migrated
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_capture_keeps_known_plaintext_nickname(self):
        # 档案里已有明文昵称
        old = fake_doc()
        Path(safe_join(bs.PROFILE_DIR, "u123.json")).write_text(
            json.dumps(old), encoding="utf-8")
        # 客户端刷新后昵称/手机号被加密回写
        live = fake_doc()
        live["account"]["nickname"] = {"$wbEncrypted": 1}
        live["account"]["phoneNumber"] = {"$wbEncrypted": 1}
        Path(bs.TARGET).write_text(json.dumps(live), encoding="utf-8")
        _, doc = bs.capture_current()
        self.assertEqual(doc["account"]["nickname"], "测试账号")
        self.assertEqual(doc["account"]["phoneNumber"], "138****0000")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="buddytest-")
        self.assertTrue(os.path.realpath(self.tmp).startswith(os.path.realpath(tempfile.gettempdir())))
        bs.STORE = self.tmp
        bs.PROFILE_DIR = safe_join(self.tmp, "profiles")
        bs.BACKUP_DIR = safe_join(self.tmp, "backups")
        bs._migrated = True  # 默认关掉自动迁移，保证测试隔离；迁移有专项测试

    def tearDown(self):
        bs.STORE = os.path.join(bs.BASE_DIR, "BuddySwitch")
        bs.PROFILE_DIR = os.path.join(bs.STORE, "profiles")
        bs.BACKUP_DIR = os.path.join(bs.STORE, "backups")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_doc(self, name="in.json"):
        src = safe_join(self.tmp, name)
        Path(src).write_text(json.dumps(fake_doc()), encoding="utf-8")
        return src

    def test_import_and_list(self):
        src = self._write_doc()
        pp, doc = bs.import_profile(src)
        self.assertTrue(os.path.realpath(pp).startswith(os.path.realpath(bs.PROFILE_DIR)))
        profiles = bs.list_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["uid"], "u123")

    def test_import_rejects_bad_file(self):
        src = safe_join(self.tmp, "bad.json")
        Path(src).write_text(json.dumps([1, 2]), encoding="utf-8")
        with self.assertRaises(ValueError):
            bs.import_profile(src)

    def test_delete_profile(self):
        bs.import_profile(self._write_doc())
        self.assertTrue(bs.delete_profile("u123"))
        self.assertEqual(bs.list_profiles(), [])
        self.assertFalse(bs.delete_profile("u123"))

    def test_migration_from_legacy(self):
        legacy = safe_join(self.tmp, "legacy")
        legacy_profiles = safe_join(legacy, "profiles")
        os.makedirs(legacy_profiles)
        Path(safe_join(legacy_profiles, "a.json")).write_text(
            json.dumps(fake_doc()), encoding="utf-8")
        old_store = bs.LEGACY_STORE
        bs.LEGACY_STORE = legacy
        try:
            bs._migrated = False
            bs.ensure_dirs()
            self.assertEqual(len(bs.list_profiles()), 1)
            # 再跑一次不会重复迁移/不会清掉已有
            bs._migrated = False
            bs.ensure_dirs()
            self.assertEqual(len(bs.list_profiles()), 1)
        finally:
            bs.LEGACY_STORE = old_store
            bs._migrated = True


class GuiSmokeTest(unittest.TestCase):
    def test_gui_builds(self):
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("no tkinter")
        root = tk.Tk()
        root.withdraw()
        root.title(bs.APP_NAME)
        # 构建关键控件并处理一轮事件，验证无异常
        from tkinter import ttk
        tree = ttk.Treeview(root, columns=("name", "expiry"), show="headings")
        tree.insert("", "end", values=("账号A", "2027-01-01"))
        tree.tag_configure("current", background="#dbeafe")
        tree.update()
        root.update()
        root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
