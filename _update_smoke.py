#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动更新链路自动化测试：真实下载最新 Release 的 exe → 校验 sha256 → 替换（拦截执行）。"""
import os
import sys
import time

sys.path.insert(0, "/opt/data/douyin_dl/desktop")
import app  # noqa
import updater  # noqa
from PySide6.QtCore import QTimer  # noqa
from PySide6.QtWidgets import QMessageBox  # noqa

for name in ("information", "warning", "critical"):
    setattr(QMessageBox, name, staticmethod(lambda *a, _n=name, **k: print("[popup/%s]" % _n, str(a[2])[:120] if len(a) > 2 else a)))

STATE = {}


def hook(a, w):
    info = updater.check_latest()
    print("[test] Release 最新版本: v%s, 资产 %.1f MB" % (info["version"], info["size"] / 1048576))
    # 假装当前版本更旧 + 假装是可自更新的打包版 + 拦截真正的替换动作
    w.latest_info = info
    updater.can_self_update = lambda: True

    def fake_apply(new_exe, exe_path=None):
        STATE["new"] = new_exe
        STATE["size"] = os.path.getsize(new_exe) if os.path.exists(new_exe) else 0
        return "fake-script"
    updater.apply_update_windows = fake_apply

    def poll():
        if STATE.get("new"):
            print("[test] 更新包已落地:", STATE["new"], "%.1f MB" % (STATE["size"] / 1048576))
            print("[test] 落地文件 sha256:", updater.hashlib.sha256(open(STATE["new"], "rb").read()).hexdigest()[:16], "...")
            print("[test] Release 记录 digest:", (info.get("digest") or "")[:23], "...")
            a.quit()
            return
        if time.time() - t0 > 420:
            print("[test] 超时，状态:", w.lb_status.text())
            a.quit()

    t0 = time.time()
    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(500)
    w._t = timer
    QTimer.singleShot(400, lambda: w.do_update(info))


app.run_gui(hook)
print("[test] 事件循环结束")
