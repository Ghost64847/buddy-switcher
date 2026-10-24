#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Buddy 切号器图形界面（PySide6 / Qt）。

Qt 在三个平台都用系统原生外观：macOS 是官方 Aqua 样式（顶部菜单栏、
原生文件对话框、自动深浅色），Windows 用 Vista 样式，Linux 用主题样式。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import buddy_switch as bs


def run():
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError:
        print("图形界面需要 PySide6（pip install PySide6）。\n"
              "命令行模式不受影响：buddy_switch.py list / switch <uid> 等。")
        return 2

    bs.ensure_dirs()
    cfg = bs.load_config()
    QT = QtCore.Qt

    class Job(QtCore.QThread):
        """后台任务：work(progress_cb) 在线程里跑，said 信号回传日志（线程安全）"""

        said = QtCore.Signal(str)

        def __init__(self, work):
            super().__init__()
            self._work = work
            self.ok = None
            self.value = None
            self.error = ""

        def run(self):
            try:
                self.value = self._work(self.said.emit)
                self.ok = True
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                self.ok = False

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(bs.APP_NAME)
    app.setOrganizationName("BuddySwitch")

    win = QtWidgets.QMainWindow()
    win.setWindowTitle(bs.APP_NAME)
    win.resize(800, 560)
    win.setMinimumSize(660, 460)

    central = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(central)
    layout.setContentsMargins(14, 10, 14, 6)
    layout.setSpacing(8)

    current_label = QtWidgets.QLabel("读取中…")
    layout.addWidget(current_label)

    table = QtWidgets.QTableWidget(0, 3)
    table.setHorizontalHeaderLabels(["账号（双击切换，可多选）", "UID", "有效期至"])
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setContextMenuPolicy(QT.CustomContextMenu)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.setColumnWidth(0, 260)
    table.setColumnWidth(1, 250)
    table.verticalHeader().setDefaultSectionSize(30)

    log = QtWidgets.QPlainTextEdit()
    log.setReadOnly(True)
    log.setMaximumBlockCount(2000)
    log.setMinimumHeight(96)

    splitter = QtWidgets.QSplitter(QT.Vertical)
    splitter.addWidget(table)
    splitter.addWidget(log)
    splitter.setStretchFactor(0, 3)
    splitter.setStretchFactor(1, 1)
    splitter.setCollapsible(0, False)
    splitter.setCollapsible(1, False)
    layout.addWidget(splitter, 1)

    bar = QtWidgets.QHBoxLayout()
    btn_import = QtWidgets.QPushButton("导入账号…")
    btn_capture = QtWidgets.QPushButton("保存选中账号")
    btn_export = QtWidgets.QPushButton("导出账号…")
    btn_restore = QtWidgets.QPushButton("恢复备份…")
    btn_switch = QtWidgets.QPushButton("切换账号")
    for b in (btn_import, btn_capture, btn_export, btn_restore):
        bar.addWidget(b)
    bar.addStretch(1)
    bar.addWidget(btn_switch)
    layout.addLayout(bar)

    win.setCentralWidget(central)

    status_label = QtWidgets.QLabel("就绪")
    btn_settings = QtWidgets.QPushButton("设置…")
    btn_log = QtWidgets.QPushButton("隐藏日志")
    status = win.statusBar()
    status.addWidget(status_label)
    status.addPermanentWidget(btn_settings)
    status.addPermanentWidget(btn_log)

    busy = {"value": False}
    buttons = [btn_switch, btn_import, btn_capture, btn_export, btn_restore,
               btn_settings, btn_log]
    log_hidden = {"value": False}

    def append_log(msg):
        log.appendPlainText(msg.rstrip())

    def set_status(msg):
        status_label.setText(msg)

    def set_busy(flag):
        busy["value"] = flag
        for b in buttons:
            b.setEnabled(not flag)

    def profile_map():
        return {p["path"]: p for p in bs.list_profiles()}

    def selected_rows():
        return sorted({i.row() for i in table.selectedIndexes()})

    def row_uid(row):
        item = table.item(row, 1)
        return item.text() if item else ""

    def row_path(row):
        item = table.item(row, 0)
        return item.data(QT.UserRole) or ""

    def refresh(select_uids=None):
        keep = set(select_uids or [])
        if not keep:
            keep = {row_uid(r) for r in selected_rows()}
        table.setRowCount(0)
        cur = bs.read_info()
        cur_uid = bs.account_uid(cur) if cur else ""
        if cur:
            current_label.setText("当前登录：%s（有效期至 %s）"
                                  % (bs.account_label(cur), bs.fmt_ts(bs.expiry_of(cur))))
        else:
            current_label.setText("未找到登录文件")
        now = int(bs.time.time() * 1000)
        for p in bs.list_profiles():
            name = ("● " if p["uid"] == cur_uid else "") + p["label"]
            exp = bs.fmt_ts(p["expiry"])
            if p["expiry"] and p["expiry"] < now:
                exp += "（已过期）"
            row = table.rowCount()
            table.insertRow(row)
            it0 = QtWidgets.QTableWidgetItem(name)
            it0.setData(QT.UserRole, p["path"])
            table.setItem(row, 0, it0)
            table.setItem(row, 1, QtWidgets.QTableWidgetItem(p["uid"]))
            table.setItem(row, 2, QtWidgets.QTableWidgetItem(exp))
            if p["uid"] in keep:
                table.selectRow(row)  # 追加选择，保留多选

    def doc_of_row(row):
        try:
            return bs.read_json(row_path(row))
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.critical(win, bs.APP_NAME, "读取账号文件失败：%s" % e)
            return None

    def run_job(work, on_ok=None, on_fail=None):
        job = Job(work)

        def done():
            if job.ok:
                if on_ok:
                    on_ok(job.value)
            else:
                append_log("失败：%s" % job.error)
                set_status("失败")
                if on_fail:
                    on_fail(job.error)
                else:
                    QtWidgets.QMessageBox.critical(win, bs.APP_NAME, job.error)
            set_busy(False)
            job.deleteLater()

        job.said.connect(lambda m: (append_log(m), set_status(m)))
        job.finished.connect(done)
        set_busy(True)
        job.start()

    def auto_capture_quiet():
        """静默归档当前登录（客户端续期 token 后把新有效期存进档案）"""
        if busy["value"]:
            return
        job = Job(lambda say: bs.capture_current())

        def done():
            if job.ok:
                _, doc = job.value
                refresh()
                append_log("已自动刷新当前登录归档：%s（有效期至 %s）"
                           % (bs.account_label(doc), bs.fmt_ts(bs.expiry_of(doc))))
            job.deleteLater()

        job.finished.connect(done)
        job.start()

    def do_switch(doc):
        if busy["value"]:
            return
        uid = bs.account_uid(doc)
        running = bs.running_apps()
        if running:
            names = "、".join(a["name"] for a in running)
            btn = QtWidgets.QMessageBox.question(
                win, bs.APP_NAME,
                "切换到 %s？\n将退出并重启：%s（请先保存未保存的文件）"
                % (bs.account_label(doc), names),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel)
            if btn != QtWidgets.QMessageBox.Yes:
                set_status("已取消")
                return
        set_busy(True)
        set_status("正在切换，请稍候…")

        def work(say):
            return bs.switch_to(doc, progress=say)

        def on_ok(lines):
            for line in lines[1:]:
                append_log(line)
            append_log(lines[0])
            set_status("✓ " + lines[0])
            refresh([uid])
            if any(l.startswith("已重启") for l in lines[1:]):
                append_log("客户端重启 2 分钟后自动归档刷新后的登录…")
                QtCore.QTimer.singleShot(120000, auto_capture_quiet)

        append_log("—— 切换到 %s ——" % bs.account_label(doc))
        run_job(work, on_ok=on_ok)

    def on_switch(checked=False):
        if busy["value"]:
            return
        mods = QtGui.QGuiApplication.keyboardModifiers()
        if mods & (QT.ControlModifier | QT.MetaModifier):
            return  # 按住 Ctrl / Command 操作多选时的双击，不当作切换
        rows = selected_rows()
        if not rows:
            set_status("请先在列表里选择一个账号")
            return
        if len(rows) > 1:
            set_status("选中了多个账号，请只选一个再切换")
            return
        doc = doc_of_row(rows[0])
        if doc:
            do_switch(doc)

    def on_import(checked=False):
        if busy["value"]:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            win, "选择 workbuddy-<uid>.json", "", "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            _, doc = bs.import_profile(path)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.critical(win, bs.APP_NAME, str(e))
            return
        refresh([bs.account_uid(doc)])
        append_log("已导入：%s（%s）" % (bs.account_label(doc), bs.account_uid(doc)))
        set_status("已导入 %s，双击列表即可切换" % bs.account_label(doc))

    def on_capture(checked=False):
        """保存选中账号：当前登录的账号才有新数据（客户端续期后的 token）"""
        if busy["value"]:
            return
        cur = bs.read_info()
        if not cur:
            QtWidgets.QMessageBox.information(win, bs.APP_NAME, "当前没有登录文件。")
            return
        cur_uid = bs.account_uid(cur)
        sel_uids = {row_uid(r) for r in selected_rows()}
        if sel_uids and cur_uid not in sel_uids:
            QtWidgets.QMessageBox.information(
                win, bs.APP_NAME,
                "选中的账号不是当前登录的账号（标 ● 的是 %s），没有新数据可保存。\n"
                "要刷新哪个账号，先切换过去再保存。" % bs.account_label(cur))
            return

        def work(say):
            return bs.capture_current()

        def on_ok(ret):
            _, doc = ret
            refresh([cur_uid])
            append_log("已保存当前登录：%s（有效期至 %s）"
                       % (bs.account_label(doc), bs.fmt_ts(bs.expiry_of(doc))))
            set_status("已保存 %s" % bs.account_label(doc))

        set_status("正在保存…")
        append_log("—— 保存当前登录 ——")
        run_job(work, on_ok=on_ok)

    def on_refresh_validity(checked=False):
        """手动刷新有效期：重读当前登录归档 + 明文档案尝试联网续期"""
        if busy["value"]:
            return
        pmap = profile_map()
        items = [pmap[row_path(r)] for r in selected_rows() if row_path(r) in pmap]
        if not items:
            set_status("请先选择要刷新的账号")
            return
        refreshed = set()

        def work(say):
            for uid, doc in items:
                try:
                    doc = bs.refresh_profile_tokens(doc)
                except (ValueError, RuntimeError, OSError) as e:
                    say("%s：未续期（%s）"
                        % (bs.account_label(doc), str(e).splitlines()[0][:60]))
                    continue
                try:
                    bs.write_info(doc, os.path.join(
                        bs.PROFILE_DIR, "%s.json" % bs.safe_uid(uid)))
                    refreshed.add(uid)
                    say("%s：已联网续期至 %s"
                        % (bs.account_label(doc), bs.fmt_ts(bs.expiry_of(doc))))
                except OSError as e:
                    say("%s：续期成功但保存失败（%s）" % (bs.account_label(doc), e))
            cur = bs.read_info()
            if cur and bs.account_uid(cur) not in refreshed:
                before = bs.expiry_of(cur)
                try:
                    _, captured = bs.capture_current()
                    say("当前登录归档已更新（有效期至 %s）"
                        % bs.fmt_ts(bs.expiry_of(captured)))
                    if bs.expiry_of(captured) == before and not refreshed:
                        say("说明：token 离到期还远时，客户端不会向服务器换新 token，"
                            "有效期不变属正常；临近到期时再切换登录一次即可滚动续期。")
                except (OSError, ValueError):
                    pass
            return [u for u, _ in items]

        def on_ok(uids):
            refresh(uids)
            set_status("刷新完成")

        append_log("—— 刷新有效期（%d 个账号）——" % len(items))
        run_job(work, on_ok=on_ok)

    def on_export(checked=False):
        if busy["value"]:
            return
        pmap = profile_map()
        items = [pmap[row_path(r)] for r in selected_rows() if row_path(r) in pmap]
        if not items:
            set_status("请先选择要导出的账号")
            return
        if len(items) == 1:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                win, "导出账号", "workbuddy-%s.json" % bs.safe_uid(items[0]["uid"]),
                "JSON 文件 (*.json)")
            if not path:
                return
            dests = [(path, items[0]["doc"])]
        else:
            d = QtWidgets.QFileDialog.getExistingDirectory(
                win, "选择导出目录（%d 个账号）" % len(items))
            if not d:
                return
            dests = [(os.path.join(d, "workbuddy-%s.json" % bs.safe_uid(p["uid"])),
                      p["doc"]) for p in items]
        try:
            for dest, doc in dests:
                Path(dest).write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        except OSError as e:
            QtWidgets.QMessageBox.critical(win, bs.APP_NAME, "导出失败：%s" % e)
            return
        append_log("已导出 %d 个账号" % len(dests))
        set_status("已导出 %d 个账号" % len(dests))

    def on_delete(checked=False):
        if busy["value"]:
            return
        rows = selected_rows()
        if not rows:
            return
        pmap = profile_map()
        uids = [pmap[row_path(r)]["uid"] for r in rows if row_path(r) in pmap]
        if not uids:
            return
        shown = "、".join(uids[:3]) + ("…" if len(uids) > 3 else "")
        btn = QtWidgets.QMessageBox.question(
            win, bs.APP_NAME,
            "删除 %d 个本地档案？\n%s\n（只删除工具里的存档，不影响客户端当前登录）"
            % (len(uids), shown),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel)
        if btn != QtWidgets.QMessageBox.Yes:
            return
        for u in uids:
            bs.delete_profile(u)
        refresh()
        set_status("已删除 %d 个档案" % len(uids))

    INTERVALS = [("30 分钟", 30), ("1 小时", 60), ("3 小时", 180),
                 ("6 小时", 360), ("12 小时", 720), ("每天", 1440)]

    def on_settings(checked=False):
        if busy["value"]:
            return
        dlg = QtWidgets.QDialog(win)
        dlg.setWindowTitle("设置")
        form = QtWidgets.QFormLayout(dlg)
        auto_chk = QtWidgets.QCheckBox(
            "定时刷新当前账号有效期（客户端自动续期后，工具把新 token 归档）")
        interval = QtWidgets.QComboBox()
        interval.addItems([lbl for lbl, _ in INTERVALS])
        minutes = int(cfg.get("auto_refresh_minutes", 60) or 60)
        idx = next((i for i, (_, m) in enumerate(INTERVALS) if m == minutes), 1)
        interval.setCurrentIndex(idx)
        restart_chk = QtWidgets.QCheckBox("切换后自动重启客户端")
        ide_chk = QtWidgets.QCheckBox("同步切换 CodeBuddy IDE 登录")
        auto_chk.setChecked(bool(cfg.get("auto_refresh", True)))
        restart_chk.setChecked(bool(cfg.get("restart_after_switch", True)))
        ide_chk.setChecked(bool(cfg.get("switch_ide", True)))
        form.addRow(auto_chk)
        box_row = QtWidgets.QWidget()
        box_lay = QtWidgets.QHBoxLayout(box_row)
        box_lay.setContentsMargins(20, 0, 0, 0)
        box_lay.addWidget(QtWidgets.QLabel("刷新间隔："))
        box_lay.addWidget(interval)
        box_lay.addStretch(1)
        form.addRow(box_row)
        form.addRow(restart_chk)
        form.addRow(ide_chk)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        cfg["auto_refresh"] = auto_chk.isChecked()
        cfg["auto_refresh_minutes"] = INTERVALS[interval.currentIndex()][1]
        cfg["restart_after_switch"] = restart_chk.isChecked()
        cfg["switch_ide"] = ide_chk.isChecked()
        bs.save_config(cfg)
        schedule_auto()
        set_status("设置已保存")

    def on_restore(checked=False):
        if busy["value"]:
            return
        backups = bs.list_backups()
        if not backups:
            QtWidgets.QMessageBox.information(win, bs.APP_NAME, "还没有备份。")
            return
        dlg = QtWidgets.QDialog(win)
        dlg.setWindowTitle("恢复备份")
        dlg.resize(620, 400)
        lay = QtWidgets.QVBoxLayout(dlg)
        lb = QtWidgets.QListWidget()
        lb.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        lay.addWidget(lb)

        def reload_list():
            lb.clear()
            for b in backups:
                lb.addItem("%s   %s   %s" % (
                    datetime.fromtimestamp(b["mtime"]).strftime("%m-%d %H:%M"),
                    b["label"], b["uid"][:8]))

        reload_list()
        row = QtWidgets.QHBoxLayout()
        btn_pick = QtWidgets.QPushButton("恢复选中的备份")
        btn_del = QtWidgets.QPushButton("删除选中备份")
        btn_close = QtWidgets.QPushButton("关闭")
        row.addWidget(btn_pick)
        row.addWidget(btn_del)
        row.addStretch(1)
        row.addWidget(btn_close)
        lay.addLayout(row)

        def pick():
            sel = lb.selectedItems()
            if not sel:
                return
            idx = lb.row(sel[0])
            try:
                doc = bs.read_json(backups[idx]["path"])
            except (OSError, ValueError) as e:
                QtWidgets.QMessageBox.critical(dlg, bs.APP_NAME, "备份无法读取：%s" % e)
                return
            dlg.close()
            do_switch(doc)

        def del_backups():
            sel = lb.selectedItems()
            if not sel:
                return
            btn = QtWidgets.QMessageBox.question(
                dlg, bs.APP_NAME,
                "删除 %d 个备份文件？\n（只删备份，不影响账号档案和当前登录）" % len(sel),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel)
            if btn != QtWidgets.QMessageBox.Yes:
                return
            for item in sorted(sel, key=lb.row, reverse=True):
                idx = lb.row(item)
                try:
                    os.remove(backups[idx]["path"])
                except OSError:
                    pass
                del backups[idx]
            reload_list()
            if not backups:
                dlg.close()
                set_status("备份已清空")

        btn_pick.clicked.connect(pick)
        btn_del.clicked.connect(del_backups)
        btn_close.clicked.connect(dlg.close)
        lb.itemDoubleClicked.connect(lambda _item: pick())
        dlg.exec()

    def on_select_all(checked=False):
        if not busy["value"]:
            table.selectAll()

    def toggle_log(checked=False):
        log_hidden["value"] = not log_hidden["value"]
        log.setVisible(not log_hidden["value"])
        btn_log.setText("显示日志" if log_hidden["value"] else "隐藏日志")

    def context_menu(pos):
        if busy["value"]:
            return
        item = table.itemAt(pos)
        if not item:
            return
        table.clearSelection()
        table.selectRow(item.row())
        menu = QtWidgets.QMenu(table)
        for text, handler in (("切换到该账号", on_switch),
                              ("刷新有效期", on_refresh_validity),
                              ("导出选中账号…", on_export),
                              ("删除选中档案", on_delete)):
            menu.addAction(text, handler)
        menu.addSeparator()
        menu.addAction("打开数据目录", lambda: bs.reveal_path(bs.STORE))
        menu.exec(table.viewport().mapToGlobal(pos))

    table.customContextMenuRequested.connect(context_menu)
    table.itemDoubleClicked.connect(lambda _item: on_switch())

    # 菜单栏（macOS 在屏幕顶部，Windows/Linux 在窗口内，都是系统样式）
    menubar = win.menuBar()
    m_file = menubar.addMenu("文件")
    m_file.addAction("导入账号…", on_import,
                     QtGui.QKeySequence.StandardKey.Open)
    m_file.addAction("导出账号…", on_export)
    m_file.addAction("恢复备份…", on_restore)
    m_file.addSeparator()
    m_file.addAction("打开数据目录", lambda: bs.reveal_path(bs.STORE))
    m_edit = menubar.addMenu("编辑")
    act_all = QtGui.QAction("全选", win)
    act_all.setShortcuts(QtGui.QKeySequence.StandardKey.SelectAll)
    act_all.triggered.connect(on_select_all)
    m_edit.addAction(act_all)
    act_del = QtGui.QAction("删除选中档案", win)
    act_del.setShortcuts([QtGui.QKeySequence(QT.Key_Delete),
                          QtGui.QKeySequence(QT.Key_Backspace)])
    act_del.triggered.connect(on_delete)
    m_edit.addAction(act_del)
    m_acc = menubar.addMenu("账号")
    m_acc.addAction("切换账号", on_switch, QtGui.QKeySequence(QT.Key_Return))
    m_acc.addAction("保存选中账号", on_capture,
                    QtGui.QKeySequence.StandardKey.Save)
    m_acc.addAction("刷新有效期", on_refresh_validity)
    m_view = menubar.addMenu("显示")
    m_view.addAction("隐藏/显示日志", toggle_log, QtGui.QKeySequence("Ctrl+L"))
    m_view.addAction("设置…", on_settings,
                     QtGui.QKeySequence.StandardKey.Preferences)

    btn_switch.clicked.connect(on_switch)
    btn_import.clicked.connect(on_import)
    btn_capture.clicked.connect(on_capture)
    btn_export.clicked.connect(on_export)
    btn_restore.clicked.connect(on_restore)
    btn_settings.clicked.connect(on_settings)
    btn_log.clicked.connect(toggle_log)

    # 定时刷新当前账号归档
    auto_timer = QtCore.QTimer(win)

    def schedule_auto():
        auto_timer.stop()
        if not cfg.get("auto_refresh", True):
            return
        minutes = max(1, int(cfg.get("auto_refresh_minutes", 60) or 60))
        auto_timer.setInterval(minutes * 60000)
        auto_timer.start()

    auto_timer.timeout.connect(auto_capture_quiet)

    refresh()
    append_log("数据目录：%s" % bs.STORE)
    append_log("用法：双击列表切换；拖动或按住 Ctrl / Command 多选，"
               "右键可刷新有效期、导出、删除。")
    append_log("提示：首次同步 IDE 时如弹出钥匙串授权，请点「始终允许」。")
    schedule_auto()
    win.show()
    return app.exec()
