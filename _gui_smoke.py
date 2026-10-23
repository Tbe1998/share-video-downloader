#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 自动化冒烟测试：离屏渲染 + 走完解析/下载，输出截图。"""
import os
import sys
import time

sys.path.insert(0, "/opt/data/douyin_dl/desktop")
import app  # noqa
from PySide6.QtCore import QTimer  # noqa
from PySide6.QtWidgets import QMessageBox  # noqa

LINK = sys.argv[1] if len(sys.argv) > 1 else "https://www.douyin.com/video/7699677081198449766"
OUTDIR = "/tmp/dygui_out"
SHOT_DIR = "/tmp/gui_shots"
os.makedirs(SHOT_DIR, exist_ok=True)

# 弹窗改为打印，避免模态阻塞自动化
for name in ("information", "warning", "critical"):
    setattr(QMessageBox, name, staticmethod(lambda *a, _n=name, **k: print("[popup/%s]" % _n, a[2] if len(a) > 2 else a)))


def hook(a, w):
    t0 = time.time()
    state = {"parsed": False, "last_log": "", "saved": False}

    def snap(name):
        w.grab().save(os.path.join(SHOT_DIR, name))

    def poll():
        txt = w.log_box.toPlainText()
        if txt != state["last_log"]:
            print("[log]", txt.splitlines()[-1] if txt else "")
            state["last_log"] = txt
        if not state["parsed"] and w.btn_dl.isEnabled():
            state["parsed"] = True
            snap("2_parsed.png")
            print("[test] 解析完成:", w.lb_info.text().replace("\n", " | ")[:200])
            w.ed_out.setText(OUTDIR)
            w.do_download()
        if w.last_path and not state["saved"]:
            state["saved"] = True
            time.sleep(0.5)
            snap("3_done.png")
            print("[test] 下载完成:", w.last_path, "|", w.lb_status.text())
            print("[test] 文件大小:", os.path.getsize(w.last_path))
            a.quit()
            return
        if time.time() - t0 > 150:
            snap("3_timeout.png")
            print("[test] 超时 | 状态:", w.lb_status.text())
            a.quit()

    QTimer.singleShot(1200, lambda: snap("1_initial.png"))
    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(600)
    w._smoke_timer = timer      # 必须挂在窗口上，否则被 GC 回收后定时器不再触发
    w.ed_url.setText(LINK)
    w.do_parse()


app.run_gui(hook)
print("[test] 事件循环结束")
