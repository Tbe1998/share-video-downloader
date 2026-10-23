#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证：启动时登录状态来自本地 cookies.json，而不是浏览器 profile。

两种情形分别跑一次：
  ① 有本地 cookies.json（含 sessionid）→ 应显示“已登录”且**不启动浏览器**
  ② 没有本地文件 → 回退去查浏览器 profile
"""
import os
import sys
import tempfile

sys.path.insert(0, "/opt/data/douyin_dl/desktop")
tmp = tempfile.mkdtemp(prefix="loginsmoke_")
os.environ["SHAREVIDEO_DATA"] = tmp          # 测试用临时目录；真实环境就是程序文件夹

import sessions  # noqa
import app  # noqa

CASE = sys.argv[1] if len(sys.argv) > 1 else "yes"
if CASE == "yes":
    sessions.save({"sessionid": "fake-session-value", "sessionid_ss": "ss", "ttwid": "tw"},
                  extra={"nickname": "文件里的昵称"})
else:
    sessions.clear()

print("=== 情形：%s ===" % ("有本地 cookies.json" if CASE == "yes" else "没有本地 cookies.json"))
print("本地文件:", sessions.store_path(), "| 存在:", os.path.isfile(sessions.store_path()))

from PySide6.QtCore import QTimer  # noqa
from PySide6.QtWidgets import QMessageBox  # noqa

for name in ("information", "warning", "critical"):
    setattr(QMessageBox, name, staticmethod(lambda *a, _n=name, **k: print("[popup/%s]" % _n, a[2] if len(a) > 2 else a)))

state = {"reported": False}


def _count_chrome():
    import subprocess
    return subprocess.run(["bash", "-lc", "pgrep -fc 'chrome-linux64/chrome' || true"],
                          capture_output=True, text=True).stdout.strip()


BEFORE = [_count_chrome()]
print("运行前 chrome 进程数:", BEFORE[0])


def hook(a, w):
    def check():
        txt = w.lb_login.text()
        if "检测中" in txt and not state["reported"]:
            return
        if state["reported"]:
            return
        state["reported"] = True
        log = w.log_box.toPlainText().splitlines()
        print("界面登录状态：", txt)
        print("相关日志：")
        for line in log[-4:]:
            print("   ", line)
        # 判断有没有启动浏览器：比较运行前后 chrome 进程数
        print("浏览器进程数 运行前=%s 运行后=%s" % (BEFORE[0], _count_chrome()))
        a.quit()

    timer = QTimer()
    timer.timeout.connect(check)
    timer.start(700)
    w._smoke_timer = timer


app.run_gui(hook)
print("本地文件仍在:", os.path.isfile(sessions.store_path()))
