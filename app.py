#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音无水印下载器 · 桌面版（PySide6）

用法：
  双击 exe / python app.py            图形界面
  python app.py --cli <链接> [...]    命令行（可选）

原理：调用本机 Chrome/Edge 打开作品页，让浏览器自己完成抖音的签名请求，
      再从页面里取出各档码率的无水印直链下载。因此无需自己实现签名算法。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import updater   # noqa: E402  （自动更新模块，纯标准库）
import webserver  # noqa: E402  （手机网页版，纯标准库）

APP_NAME = "DouyinDownloader"
APP_TITLE = "抖音无水印下载器"
from version import VERSION  # noqa: E402  唯一版本来源（见 version.py）

# ---------------- 配置 ----------------
def app_dir():
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        root = os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(root, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


CFG_PATH = os.path.join(app_dir(), "config.json")


def default_outdir():
    for p in (os.path.join(os.path.expanduser("~"), "Downloads"),
              os.path.join(os.path.expanduser("~"), "下载"),
              os.path.expanduser("~")):
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


class Config:
    def __init__(self):
        self.data = {"outdir": "", "history": [], "quality": "auto",
                     "browser": "", "headless": True}
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        except Exception:
            pass

    def get(self, k, default=None):
        return self.data.get(k, default)

    def set(self, k, v):
        self.data[k] = v
        self.save()

    def add_history(self, d):
        h = [x for x in self.data.get("history", []) if x != d]
        h.insert(0, d)
        self.data["history"] = h[:10]
        self.save()

    def save(self):
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass


CFG = Config()


def extract_url(text):
    """从整段分享文字里抠出链接"""
    text = (text or "").strip()
    m = re.search(r"https?://[^\s\u4e00-\u9fa5，,。\"'<>]+", text)
    if m:
        return m.group(0)
    m = re.search(r"(v\.douyin\.com/[A-Za-z0-9_\-]+/?|(?:www\.)?douyin\.com/video/\d+)", text)
    if m:
        return "https://" + m.group(1)
    return text


def extract_aweme_id(text):
    text = extract_url(text)
    for pat in (r"/(?:video|note|slides)/(\d+)", r"(?:modal_id=|aweme_id=)(\d+)", r"(\d{15,20})"):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


# ======================================================================
#  命令行模式
# ======================================================================
def run_cli(argv):
    import engine as eng
    import dycore
    url = None
    quality = "auto"
    out_dir = default_outdir()
    headless = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--cli", "-c"):
            i += 1
            url = argv[i] if i < len(argv) else None
        elif a in ("-q", "--quality"):
            i += 1
            quality = argv[i]
        elif a in ("-o", "--out"):
            i += 1
            out_dir = argv[i]
        elif a in ("--headless",):
            headless = True
        elif a in ("-h", "--help"):
            print("用法: app.py --cli <抖音链接> [-q auto|1080p|720p|540p] [-o 输出目录]")
            return 0
        elif url is None:
            url = a
        i += 1
    if not url:
        print("用法: app.py --cli <抖音链接> [-q auto|1080p|720p|540p] [-o 输出目录]")
        return 2
    aweme_id = extract_aweme_id(url)
    if not aweme_id:
        import dycore
        print("[1/3] 分享短链跳转解析作品ID …")
        try:
            aweme_id = dycore.DouyinClient().resolve_id(extract_url(url))
        except Exception as ex:
            print("错误: 链接解析失败 - %s" % ex)
            return 2
    print("        作品ID: %s" % aweme_id)
    e = eng.BrowserEngine(headless=headless, profile=None, log=print)
    try:
        print("[1/3] 打开作品页并取数据 …")
        res = e.fetch_detail(aweme_id, timeout=45)
        ratios = []
        meta = {"aweme_id": aweme_id, "desc": "", "author": ""}
        if res.get("detail"):
            s = eng.summarize(res["detail"])
            meta.update(s)
            ratios = s["ratios"]
            print("[2/3] %s / %s / %s" % (s["author"], s["desc"][:40], s["resolution"] if "resolution" in s else ""))
        elif res.get("fallback_url"):
            m = res.get("meta") or {}
            print("[2/3] 未取到码率列表，使用页面播放流")
            meta["desc"] = (m.get("desc") or m.get("title") or "").strip()
            meta["author"] = (m.get("author") or "").strip()
            ratios = [{"gear": "page", "height": 0, "size": 0,
                       "url": res["fallback_url"], "urls": [res["fallback_url"]], "score": 0}]
        else:
            print("错误: 解析失败（可能需要先在本程序里点『登录抖音』扫码登录）")
            return 1
        pick = eng.choose(ratios, quality)
        hs = sorted({"%sp" % r.get("height") for r in ratios if r.get("height")})
        print("       可选: %s" % (", ".join(hs) or "页面播放流（分辨率由抖音决定）"))
        print("[3/3] 下载 %s …" % (("%sp" % pick.get("height")) if pick.get("height") else "页面流"))
        last = [0]

        def prog(got, total, speed):
            if time.time() - last[0] > 1:
                last[0] = time.time()
                print("       %.1f%%  %.1f/%.1f MB  %.0f KB/s" % (
                    got * 100.0 / total if total else 0, got / 1048576.0,
                    total / 1048576.0, speed / 1024.0))
        path = eng.download_entry(pick, out_dir, meta, prog)
        print("完成: %s (%s)" % (path, dycore.human_size(os.path.getsize(path))))
        return 0
    except Exception as e2:
        print("错误: %s" % e2)
        return 1
    finally:
        e.kill()


# ======================================================================
#  图形界面
# ======================================================================
STYLE = """
QWidget { background:#0f131b; color:#e8edf7; font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif; font-size:13px; }
QMainWindow, QDialog { background:#0f131b; }
QGroupBox { border:1px solid #252c3b; border-radius:10px; margin-top:14px; padding:12px 10px 10px 10px; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 6px; color:#8b95a9; }
QLineEdit, QComboBox, QPlainTextEdit {
    background:#151922; border:1px solid #252c3b; border-radius:8px; padding:7px 10px; selection-background-color:#ff2d55; }
QLineEdit:focus, QComboBox:focus { border-color:#3b4a6b; }
QComboBox::drop-down { border:none; width:20px; }
QComboBox QAbstractItemView { background:#151922; border:1px solid #252c3b; selection-background-color:#26304a; outline:none; }
QPushButton { background:#1c2130; border:1px solid #252c3b; border-radius:8px; padding:8px 16px; }
QPushButton:hover { background:#232b3d; border-color:#3b4a6b; }
QPushButton:disabled { color:#5b6577; border-color:#1e2432; background:#161b26; }
QPushButton#primary { background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff2d55, stop:1 #ff5c7a);
    border:none; color:#ffffff; font-weight:600; }
QPushButton#primary:hover { background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff3b60, stop:1 #ff6c88); }
QProgressBar { background:#151922; border:1px solid #252c3b; border-radius:7px; height:14px; text-align:center; color:#c9d4e6; }
QProgressBar::chunk { border-radius:6px; background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff2d55, stop:1 #25f4ee); }
QLabel#hint { color:#8b95a9; font-size:12px; }
QLabel#title { font-size:17px; font-weight:600; }
QLabel#sub { color:#8b95a9; font-size:12px; }
QLabel#info { background:#151922; border:1px solid #252c3b; border-radius:10px; padding:12px; color:#cfd8e8; }
QPlainTextEdit#log { font-family:Consolas,"Courier New",monospace; font-size:12px; color:#9fb4d8; background:#0b0e15; }
"""


def run_gui(test_hook=None):
    from PySide6.QtCore import QThread, Qt, Signal, QSize, QTimer, QUrl
    from PySide6.QtGui import QFont, QPixmap, QAction, QDesktopServices
    from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QGridLayout,
                                   QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                                   QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                                   QSizePolicy, QVBoxLayout, QWidget, QDialog)
    import engine as eng
    import dycore

    class ParseWorker(QThread):
        ok = Signal(dict)
        fail = Signal(str)
        log = Signal(str)

        def __init__(self, url, headless, reuse_engine=None):
            super().__init__()
            self.url = url
            self.headless = headless
            self.reuse = reuse_engine

        def run(self):
            e = None
            own = False
            try:
                # 短链(v.douyin.com/xxx)里没有作品ID，必须先跟随跳转解析出来
                import dycore
                self.log.emit("解析分享链接：%s" % self.url)
                try:
                    aweme_id = dycore.DouyinClient().resolve_id(self.url)
                except Exception as ex:
                    self.fail.emit("链接解析失败：%s\n请确认复制的是抖音作品的分享链接（v.douyin.com/… 或 douyin.com/video/…）" % ex)
                    return
                self.log.emit("已解析出作品ID：%s" % aweme_id)
                # 如果登录窗口已经开着，直接复用它（同一个 profile 不能再开第二个浏览器）
                if self.reuse is not None and self.reuse.is_alive():
                    e = self.reuse
                    self.log.emit("复用已打开的浏览器窗口")
                else:
                    e = eng.BrowserEngine(headless=self.headless)
                    own = True
                    self.log.emit("启动浏览器引擎：%s" % os.path.basename(e.exe))
                    e.start()
                self.log.emit("打开作品页并等待页面完成签名请求 …")
                res = e.fetch_detail(aweme_id, timeout=45)
                if res.get("detail"):
                    s = eng.summarize(res["detail"])
                    self.ok.emit({"meta": s, "ratios": s["ratios"], "mode": "detail"})
                elif res.get("fallback_url"):
                    self.log.emit("未取到码率列表，改用页面播放流（分辨率由抖音决定，通常 576P~720P）")
                    u = res["fallback_url"]
                    pm = res.get("meta") or {}
                    self.ok.emit({"meta": {"aweme_id": aweme_id,
                                           "desc": (pm.get("desc") or pm.get("title") or "").strip(),
                                           "author": (pm.get("author") or "").strip(),
                                           "height": 0, "duration": 0},
                                  "ratios": [{"gear": "page", "height": 0, "size": 0,
                                              "url": u, "urls": [u], "score": 0}],
                                  "mode": "page"})
                else:
                    self.fail.emit("解析失败：页面没有返回作品数据。\n"
                                   "可能原因：① 作品已删除/私密 ② 被风控，请点『登录抖音』扫码登录后再试")
            except Exception as ex:
                self.fail.emit("%s" % ex)
            finally:
                if own and e:
                    e.kill()

    class DownloadWorker(QThread):
        prog = Signal(int, int, float)
        done = Signal(str)
        fail = Signal(str)
        log = Signal(str)

        def __init__(self, ratio, out_dir, meta, headless):
            super().__init__()
            self.ratio = ratio
            self.out_dir = out_dir
            self.meta = meta
            self._stop = False

        def stop(self):
            self._stop = True

        def run(self):
            try:
                path = eng.download_entry(self.ratio, self.out_dir, self.meta,
                                          progress=lambda g, t, s: self.prog.emit(g, t, s),
                                          should_stop=lambda: self._stop)
                self.done.emit(path)
            except Exception as ex:
                self.fail.emit("%s" % ex)

    class LoginWorker(QThread):
        log = Signal(str)
        ready = Signal(object)
        done = Signal(str)

        def run(self):
            try:
                e = eng.BrowserEngine(headless=False)
                self.log.emit("正在启动浏览器（首次启动稍慢）…")
                e.start()
                e.open_login()
                self.ready.emit(e)
                self.done.emit("浏览器已打开：用抖音 App 扫码登录，登录后点『检查登录』确认")
            except Exception as ex:
                self.done.emit("打开浏览器失败: %s" % ex)

    class LoginCheckWorker(QThread):
        result = Signal(dict)
        fail = Signal(str)

        def __init__(self, engine=None):
            super().__init__()
            self.engine = engine

        def run(self):
            e = self.engine
            own = False
            try:
                if e is None or not e.is_alive():
                    own = True
                    e = eng.BrowserEngine(headless=True)
                st = e.login_status()
                self.result.emit(st)
            except Exception as ex:
                self.fail.emit(str(ex))
            finally:
                if own and e is not None:
                    try:
                        e.kill()
                    except Exception:
                        pass

    class UpdateCheckWorker(QThread):
        ok = Signal(dict)
        fail = Signal(str)

        def run(self):
            try:
                self.ok.emit(updater.check_latest(custom_manifest=CFG.get("update_manifest") or None))
            except Exception as ex:
                self.fail.emit(str(ex))

    class UpdateDownloadWorker(QThread):
        prog = Signal(int, int)
        trying = Signal(str)
        done = Signal(int, str, str)      # 字节, sha256, 用的下载地址
        fail = Signal(str)

        def __init__(self, info, dest):
            super().__init__()
            self.info = info
            self.dest = dest

        def run(self):
            try:
                size, sha, url = updater.download_best(
                    self.info.get("candidates") or [],
                    self.dest,
                    progress=lambda g, t: self.prog.emit(g, t),
                    digest=self.info.get("digest") or "",
                    on_try=lambda host: self.trying.emit(host))
                self.done.emit(size, sha, url)
            except Exception as ex:
                self.fail.emit(str(ex))

    class DiagnoseWorker(QThread):
        log = Signal(str)
        done = Signal()

        def run(self):
            try:
                eng.diagnose(log=lambda s: self.log.emit(s))
            except Exception as ex:
                self.log.emit("诊断异常: %s" % ex)
            finally:
                self.done.emit()

    class Main(QMainWindow):
        login_request = Signal()
        def __init__(self):
            super().__init__()
            self.setWindowTitle("%s v%s" % (APP_TITLE, VERSION))
            self.resize(940, 720)
            self.meta = None
            self.ratios = []
            self.mode = None
            self.pw = None
            self.dw = None
            self.last_path = None
            self.login_engine = None
            self._check_worker = None
            self.latest_info = None
            self._upd_check = None
            self._upd_dl = None
            self.web = None
            self.login_request.connect(self.do_login)
            self.last_parse_error = ""
            self.fail_streak = int(CFG.get("fail_streak", 0) or 0)
            self._build()
            # 启动后自动查一次登录状态（放延迟，先让窗口画出来）
            QTimer.singleShot(1500, self.do_check_login)
            # 启动后静默检查更新（不打扰，只在日志里提示）
            QTimer.singleShot(4000, lambda: self.do_check_update(silent=True))

        # ---------- UI ----------
        def _build(self):
            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            root.setContentsMargins(16, 14, 16, 14)
            root.setSpacing(10)

            head = QHBoxLayout()
            tbox = QVBoxLayout()
            t = QLabel(APP_TITLE); t.setObjectName("title")
            s = QLabel("粘贴抖音分享链接 → 解析 → 选择清晰度 → 下载")
            s.setObjectName("sub")
            tbox.addWidget(t); tbox.addWidget(s)
            head.addLayout(tbox)
            head.addStretch(1)
            self.lb_login = QLabel("登录状态：检测中…")
            self.lb_login.setObjectName("hint")
            head.addWidget(self.lb_login)
            self.btn_checklogin = QPushButton("检查登录")
            self.btn_checklogin.clicked.connect(self.do_check_login)
            head.addWidget(self.btn_checklogin)
            self.btn_login = QPushButton("登录抖音")
            self.btn_login.setToolTip("遇到风控或想要 1080P/4K 时，扫码登录一次即可（登录状态会保存在独立浏览器 profile 里）")
            self.btn_login.clicked.connect(self.do_login)
            head.addWidget(self.btn_login)
            self.btn_diag = QPushButton("诊断")
            self.btn_diag.setToolTip("浏览器启动失败/超时时点这里，会把浏览器路径、端口、残留进程等检查一遍写进日志")
            self.btn_diag.clicked.connect(self.do_diagnose)
            head.addWidget(self.btn_diag)
            self.btn_web = QPushButton("手机网页版")
            self.btn_web.setToolTip("启动局域网网页版：手机连同一个 WiFi，扫码即可使用（解析/下载仍在电脑上）")
            self.btn_web.clicked.connect(self.do_web)
            head.addWidget(self.btn_web)
            self.btn_update = QPushButton("检查更新")
            self.btn_update.setToolTip("从 GitHub 检查新版本；有新版会直接下载并自动替换重启")
            self.btn_update.clicked.connect(lambda: self.do_check_update(silent=False))
            head.addWidget(self.btn_update)
            self.lb_ver = QLabel("v%s" % VERSION)
            self.lb_ver.setObjectName("hint")
            head.addWidget(self.lb_ver)
            root.addLayout(head)

            g1 = QGroupBox("① 链接")
            l1 = QVBoxLayout(g1)
            row = QHBoxLayout()
            self.ed_url = QLineEdit()
            self.ed_url.setPlaceholderText("粘贴抖音分享链接或整段分享文字（自动提取链接）")
            self.ed_url.returnPressed.connect(self.do_parse)
            row.addWidget(self.ed_url, 1)
            b_paste = QPushButton("粘贴")
            b_paste.clicked.connect(self.do_paste)
            row.addWidget(b_paste)
            self.btn_parse = QPushButton("解析")
            self.btn_parse.setObjectName("primary")
            self.btn_parse.clicked.connect(self.do_parse)
            row.addWidget(self.btn_parse)
            l1.addLayout(row)
            root.addWidget(g1)

            g2 = QGroupBox("② 作品信息")
            l2 = QVBoxLayout(g2)
            self.lb_info = QLabel("尚未解析")
            self.lb_info.setObjectName("info")
            self.lb_info.setWordWrap(True)
            self.lb_info.setMinimumHeight(76)
            self.lb_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            l2.addWidget(self.lb_info)
            row2 = QHBoxLayout()
            row2.addWidget(QLabel("清晰度"))
            self.cb_q = QComboBox()
            self.cb_q.addItem("自动（最高）", "auto")
            self.cb_q.addItem("1080P", "1080p")
            self.cb_q.addItem("720P", "720p")
            self.cb_q.addItem("540P（省流量）", "540p")
            self.cb_q.setMinimumWidth(160)
            row2.addWidget(self.cb_q)
            row2.addStretch(1)
            self.btn_dl = QPushButton("下载")
            self.btn_dl.setObjectName("primary")
            self.btn_dl.setEnabled(False)
            self.btn_dl.clicked.connect(self.do_download)
            row2.addWidget(self.btn_dl)
            self.btn_open = QPushButton("播放")
            self.btn_open.setEnabled(False)
            self.btn_open.clicked.connect(lambda: self.open_path(self.last_path))
            row2.addWidget(self.btn_open)
            self.btn_folder = QPushButton("打开所在文件夹")
            self.btn_folder.setEnabled(False)
            self.btn_folder.clicked.connect(lambda: self.reveal(self.last_path))
            row2.addWidget(self.btn_folder)
            l2.addLayout(row2)
            root.addWidget(g2)

            g3 = QGroupBox("③ 进度")
            l3 = QVBoxLayout(g3)
            self.pb = QProgressBar()
            self.pb.setRange(0, 100)
            self.pb.setValue(0)
            l3.addWidget(self.pb)
            self.lb_status = QLabel("就绪")
            self.lb_status.setObjectName("hint")
            l3.addWidget(self.lb_status)
            root.addWidget(g3)

            g4 = QGroupBox("④ 保存位置")
            l4 = QHBoxLayout(g4)
            self.ed_out = QLineEdit(CFG.get("outdir") or default_outdir())
            l4.addWidget(self.ed_out, 1)
            self.cb_hist = QComboBox()
            self.cb_hist.setMinimumWidth(220)
            self.cb_hist.addItem("历史目录…", "")
            for h in CFG.get("history", []):
                self.cb_hist.addItem(h, h)
            self.cb_hist.currentIndexChanged.connect(self._pick_hist)
            l4.addWidget(self.cb_hist)
            b_browse = QPushButton("浏览…")
            b_browse.clicked.connect(self.do_browse)
            l4.addWidget(b_browse)
            root.addWidget(g4)

            self.log_box = QPlainTextEdit()
            self.log_box.setObjectName("log")
            self.log_box.setReadOnly(True)
            self.log_box.setMinimumHeight(120)
            root.addWidget(self.log_box, 1)

            rowlog = QHBoxLayout()
            self.lb_tip = QLabel("")
            self.lb_tip.setObjectName("hint")
            rowlog.addWidget(self.lb_tip, 1)
            self.btn_copy_diag = QPushButton("复制诊断信息")
            self.btn_copy_diag.setToolTip("解析失效/出错时点这个，把版本、错误和最近日志复制到剪贴板，直接发给助手")
            self.btn_copy_diag.clicked.connect(self.do_copy_diag)
            rowlog.addWidget(self.btn_copy_diag)
            root.addLayout(rowlog)

            self.cb_headless = QComboBox()

        def log(self, msg):
            stamp = time.strftime("%H:%M:%S")
            self.log_box.appendPlainText("[%s] %s" % (stamp, msg))
            self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())

        def _pick_hist(self, idx):
            d = self.cb_hist.itemData(idx)
            if d:
                self.ed_out.setText(d)
                self.cb_hist.setCurrentIndex(0)

        def do_browse(self):
            d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.ed_out.text() or default_outdir())
            if d:
                self.ed_out.setText(d)

        def do_paste(self):
            from PySide6.QtWidgets import QApplication as QA
            txt = QA.clipboard().text()
            if txt:
                self.ed_url.setText(txt.strip())
                self.log("已从剪贴板读取内容")
            else:
                self.log("剪贴板是空的，请手动 Ctrl+V 粘贴")

        # ---------- 解析 ----------
        def do_parse(self):
            text = self.ed_url.text()
            if not text.strip():
                QMessageBox.information(self, "提示", "请先粘贴抖音分享链接")
                return
            url = extract_url(text)
            if "douyin.com" not in url and not re.search(r"\d{15,20}", url):
                QMessageBox.warning(self, "链接不对",
                                    "这看起来不是抖音链接。\n请复制抖音作品里的『分享 → 复制链接』，"
                                    "形如 https://v.douyin.com/xxxxxxx/")
                return
            if self.pw and self.pw.isRunning():
                return
            self.btn_parse.setEnabled(False)
            self.btn_dl.setEnabled(False)
            self.pb.setValue(0)
            self.lb_status.setText("解析中…")
            self.lb_info.setText("正在解析分享链接（短链需要先跳转取出作品ID）…")
            self.pw = ParseWorker(url, headless=True, reuse_engine=self.login_engine)
            self.pw.log.connect(self.log)
            self.pw.ok.connect(self.on_parsed)
            self.pw.fail.connect(self.on_parse_fail)
            self.pw.finished.connect(lambda: self.btn_parse.setEnabled(True))
            self.pw.start()

        def on_parsed(self, pack):
            self.meta = pack["meta"]
            self.ratios = pack["ratios"]
            self.mode = pack.get("mode")
            self.fail_streak = 0
            self.last_parse_error = ""
            CFG.set("fail_streak", 0)
            hs = sorted({r.get("height") or 0 for r in self.ratios if r.get("height")}, reverse=True)
            label = " / ".join("%dp" % h for h in hs) or "页面播放流"
            if pack.get("mode") == "page":
                label = "页面播放流（分辨率由抖音决定，通常 576P~720P）"
            info = []
            if self.meta.get("aweme_id"):
                info.append("作品ID：%s" % self.meta["aweme_id"])
            if self.meta.get("author"):
                info.append("作者：%s" % self.meta["author"])
            if self.meta.get("desc"):
                info.append("文案：%s" % self.meta["desc"][:110])
            if self.meta.get("duration"):
                info.append("时长：%.1f 秒" % self.meta["duration"])
            info.append("可选清晰度：%s" % label)
            self.lb_info.setText("\n".join(info))
            self.btn_dl.setEnabled(True)
            self.lb_status.setText("解析成功，可以下载了")
            self.log("解析成功，可选清晰度：%s" % label)

        def on_parse_fail(self, msg):
            self.last_parse_error = msg
            self.fail_streak += 1
            CFG.set("fail_streak", self.fail_streak)
            self.lb_status.setText("解析失败")
            self.log("解析失败：%s" % msg)
            if self.fail_streak >= 2:
                self.lb_tip.setText("⚠ 连续 %d 次解析失败，解析方式可能需要更新" % self.fail_streak)
                self.log("⚠ 已连续 %d 次解析失败。这通常意味着抖音改了风控/签名方式，"
                         "需要更新本程序的解析逻辑。请先点『检查更新』看有没有新版本；"
                         "如果还是不行，点『复制诊断信息』把内容发给助手修复。" % self.fail_streak)
                box = QMessageBox(self)
                box.setWindowTitle("解析连续失败")
                box.setIcon(QMessageBox.Warning)
                box.setText("已连续 %d 次解析失败。\n\n"
                            "这通常意味着抖音改了风控/签名，当前版本的解析方式可能已经失效。\n"
                            "建议顺序：① 点『检查更新』升级到最新版；② 仍失败就点『复制诊断信息』"
                            "把内容发给助手修复。" % self.fail_streak)
                box.setDetailedText(msg)
                box.exec()
            else:
                QMessageBox.warning(self, "解析失败", msg)

        # ---------- 下载 ----------
        def do_download(self):
            if not self.ratios:
                return
            quality = self.cb_q.currentData()
            pick = eng.choose(self.ratios, quality)
            if self.mode == "page" and quality in ("auto", "1080p"):
                self.log("提示：当前用的是页面播放流（一般 720P）。想要 1080P/4K，请点『登录抖音』扫码登录后再解析。")
            if not pick:
                QMessageBox.warning(self, "提示", "没有可用清晰度")
                return
            out_dir = (self.ed_out.text() or "").strip() or default_outdir()
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                QMessageBox.warning(self, "目录不可用", "无法使用该保存目录：%s" % e)
                return
            CFG.set("outdir", out_dir)
            CFG.add_history(out_dir)
            if self.dw and self.dw.isRunning():
                return
            self.btn_dl.setEnabled(False)
            self.btn_open.setEnabled(False)
            self.btn_folder.setEnabled(False)
            self.pb.setValue(0)
            qlabel = ("%dp" % pick["height"]) if pick.get("height") else "页面流"
            self.lb_status.setText("开始下载 %s …" % qlabel)
            self.log("开始下载：%s  体积约 %s" % (qlabel,
                                             dycore.human_size(pick.get("size") or 0)))
            self.dw = DownloadWorker(pick, out_dir, self.meta, True)
            self.dw.prog.connect(self.on_prog)
            self.dw.done.connect(self.on_done)
            self.dw.fail.connect(self.on_dl_fail)
            self.dw.start()

        def on_prog(self, got, total, speed):
            if total:
                self.pb.setValue(int(got * 100 / total))
                self.lb_status.setText("下载中 %.1f%% · %.1f/%.1f MB · %.0f KB/s" % (
                    got * 100.0 / total, got / 1048576.0, total / 1048576.0, speed / 1024.0))
            else:
                self.lb_status.setText("下载中 %.1f MB · %.0f KB/s" % (got / 1048576.0, speed / 1024.0))

        def on_done(self, path):
            self.last_path = path
            self.pb.setValue(100)
            size = dycore.human_size(os.path.getsize(path))
            self.lb_status.setText("完成：%s（%s）" % (os.path.basename(path), size))
            self.log("下载完成 → %s（%s）" % (path, size))
            self.btn_dl.setEnabled(True)
            self.btn_open.setEnabled(True)
            self.btn_folder.setEnabled(True)

        def on_dl_fail(self, msg):
            self.lb_status.setText("下载失败")
            self.log("下载失败：%s" % msg)
            self.btn_dl.setEnabled(True)
            if "已取消" not in msg:
                QMessageBox.warning(self, "下载失败", msg)

        # ---------- 登录 ----------
        def do_login(self):
            if self.login_engine and self.login_engine.proc and self.login_engine.proc.poll() is None:
                self.log("登录浏览器窗口已经开着，请在那个窗口里扫码；完成后点『检查登录』")
                return
            QMessageBox.information(
                self, "登录抖音",
                "接下来会打开一个浏览器窗口，请在其中用抖音 App 扫码登录。\n\n"
                "登录成功后不用关窗口，直接点本程序右上角的『检查登录』确认状态即可。\n"
                "登录状态会保存在本程序独立的浏览器 profile 里，之后长期有效。")
            self.log("正在打开登录用浏览器 …")
            w = LoginWorker()
            w.log.connect(self.log)
            w.ready.connect(self._on_login_engine)
            w.done.connect(lambda m: self.log(m))
            w.start()
            self._login_worker = w

        def do_diagnose(self):
            if getattr(self, "_diag_worker", None) is not None and self._diag_worker.isRunning():
                return
            self.btn_diag.setEnabled(False)
            self.log("开始诊断（约十几秒）…")
            w = DiagnoseWorker()
            w.log.connect(self.log)
            w.done.connect(lambda: (self.btn_diag.setEnabled(True), self.log("诊断完成")))
            w.start()
            self._diag_worker = w

        # ---------- 检查更新 / 自动更新 ----------
        # ---------------- 手机网页版 ----------------
        def _web_engine(self):
            """把桌面端已打开的浏览器（可能已登录）让给网页版复用，避免抢 profile"""
            e = self.login_engine
            if e is not None and e.is_alive():
                return e
            return None

        def _web_login(self):
            """手机端点『登录』→ 在电脑上弹出登录浏览器（用信号切回 GUI 线程）"""
            self.login_request.emit()
            return "已在电脑上打开抖音登录窗口，请用手机抖音扫码"

        def _web_login_state(self):
            return self.lb_login.text()

        def do_web(self):
            if self.web is not None:
                self.show_web_info()
                return
            out_dir = (self.ed_out.text() or "").strip() or default_outdir()
            port = int(CFG.get("web_port") or 8990)
            try:
                self.web = webserver.WebServer(
                    out_dir, port=port, version=VERSION, log=self.log,
                    headless=True, engine_getter=self._web_engine,
                    login_opener=self._web_login, login_checker=self._web_login_state)
                url = self.web.start()
            except Exception as ex:
                self.web = None
                self.log("启动网页版失败：%s" % ex)
                QMessageBox.warning(self, "启动失败", "手机网页版启动失败：\n%s" % ex)
                return
            self.btn_web.setText("手机版已开")
            self.log("手机网页版已启动：%s" % url)
            self.log("手机连同一个 WiFi，扫码或浏览器打开上面地址即可；解析和下载都在电脑上完成。")
            self.show_web_info()

        def stop_web(self):
            if self.web is None:
                return
            try:
                self.web.stop()
            except Exception:
                pass
            self.web = None
            self.btn_web.setText("手机网页版")
            self.log("手机网页版已停止")

        def show_web_info(self):
            if self.web is None:
                return
            urls = self.web.urls()
            main_url = self.web.primary_url()
            dlg = QDialog(self)
            dlg.setWindowTitle("手机网页版")
            dlg.setMinimumWidth(430)
            lay = QVBoxLayout(dlg)
            tip = QLabel("手机连上同一个 WiFi（或家里网络），扫码打开：\n"
                         "解析、下载都在这台电脑上做，用的是这里的浏览器和登录态。")
            tip.setWordWrap(True)
            lay.addWidget(tip)
            # 二维码
            qr_file = self._qr_png(main_url)
            if qr_file:
                try:
                    pix = QPixmap(qr_file)
                    lb = QLabel()
                    lb.setPixmap(pix.scaled(230, 230, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    lb.setAlignment(Qt.AlignCenter)
                    box = QHBoxLayout()
                    box.addStretch(1)
                    box.addWidget(lb)
                    box.addStretch(1)
                    lay.addLayout(box)
                except Exception:
                    pass
            for u in urls:
                row = QHBoxLayout()
                ed = QLineEdit(u)
                ed.setReadOnly(True)
                btn = QPushButton("复制")
                btn.clicked.connect(lambda _=False, t=u: (QApplication.clipboard().setText(t),
                                                          btn.setText("已复制")))
                row.addWidget(ed, 1)
                row.addWidget(btn)
                lay.addLayout(row)
            rowb = QHBoxLayout()
            b1 = QPushButton("在电脑上预览")
            b1.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(main_url)))
            b2 = QPushButton("停止手机版")
            b2.clicked.connect(lambda: (self.stop_web(), dlg.accept()))
            b3 = QPushButton("关闭")
            b3.clicked.connect(dlg.accept)
            for b in (b1, b2, b3):
                rowb.addWidget(b)
            lay.addLayout(rowb)
            dlg.exec()

        def _qr_png(self, text):
            """生成二维码 PNG（直接按矩阵用 QPainter 画，避免 SVG 在深色主题下反色/不显示）"""
            try:
                import qrcode
                qr = qrcode.QRCode(border=2, box_size=1)
                qr.add_data(text)
                qr.make(fit=True)
                m = qr.get_matrix()
                n = len(m)
                scale = max(4, 240 // max(1, n))
                px = n * scale
                from PySide6.QtGui import QImage, QPainter, QColor
                img = QImage(px, px, QImage.Format_RGB32)
                img.fill(QColor("#ffffff"))
                p = QPainter(img)
                p.setPen(QColor("#000000"))
                for y, row in enumerate(m):
                    for x, on in enumerate(row):
                        if on:
                            p.fillRect(x * scale, y * scale, scale, scale, QColor("#000000"))
                p.end()
                path = os.path.join(tempfile.gettempdir(), "dy_web_qr.png")
                img.save(path, "PNG")
                return path
            except Exception:
                return None

        def do_check_update(self, silent=False):
            if self._upd_check is not None and self._upd_check.isRunning():
                return
            self.btn_update.setEnabled(False)
            if not silent:
                self.log("正在检查更新（GitHub）…")
            w = UpdateCheckWorker()
            w.ok.connect(lambda info: self._on_update_info(info, silent))
            w.fail.connect(lambda m: self._on_update_fail(m, silent))
            w.finished.connect(lambda: self.btn_update.setEnabled(True))
            w.start()
            self._upd_check = w

        def _on_update_info(self, info, silent):
            self.latest_info = info
            src = info.get("source") or "未知来源"
            if updater.is_newer(info.get("version"), VERSION):
                self.btn_update.setText("更新到 v%s" % info["version"])
                self.lb_tip.setText("发现新版本 v%s（当前 v%s，来源：%s）" % (info["version"], VERSION, src))
                self.log("发现新版本 v%s（当前 v%s），更新源：%s" % (info["version"], VERSION, src))
                if silent:
                    self.log("点右上角『更新到 v%s』即可自动升级并重启。" % info["version"])
                    return
                notes = (info.get("notes") or "").strip()
                msg = ("发现新版本 v%s，当前是 v%s。\n更新源：%s\n\n%s\n\n"
                       "现在下载并自动更新吗？更新后程序会自动重启，登录状态和设置都不受影响。"
                       % (info["version"], VERSION, src, notes[:800]))
                if QMessageBox.question(self, "发现新版本", msg) == QMessageBox.Yes:
                    self.do_update(info)
            else:
                self.lb_tip.setText("已是最新版本 v%s（来源：%s）" % (VERSION, src))
                self.log("已是最新版本 v%s（检查来源：%s）" % (VERSION, src))
                if not silent:
                    QMessageBox.information(self, "检查更新", "当前已是最新版本 v%s。" % VERSION)

        def _on_update_fail(self, msg, silent):
            self.log("检查更新失败：%s" % msg)
            if not silent:
                tip = ("\n\n如果一直连不上，可以把更新源改成 NAS 镜像（默认已内置）：\n"
                       "  %s\n或者在设置里给我的加速地址。" % "\n  ".join(updater.DEFAULT_MANIFESTS))
                QMessageBox.warning(self, "检查更新失败", msg + tip)

        def do_update(self, info=None):
            info = info or self.latest_info
            if not info:
                self.do_check_update(silent=False)
                return
            if not updater.can_self_update():
                url = info.get("page") or ("https://github.com/%s/releases/latest" % updater.REPO)
                self.log("当前不是打包版或不是 Windows，不能自动替换，已打开发布页：%s" % url)
                updater.open_page(url)
                QMessageBox.information(self, "需要手动更新",
                                        "自动替换只在打包后的 Windows 版里生效。\n已为你打开发布页，手动下载覆盖即可。")
                return
            if self._upd_dl is not None and self._upd_dl.isRunning():
                return
            exe = updater.current_exe()
            dest = updater.staged_path(info.get("version") or "")
            cands = info.get("candidates") or []
            self.log("开始下载更新包（约 %.1f MB，可用下载源 %d 个）…"
                     % ((info.get("size") or 0) / 1048576.0, len(cands)))
            self.lb_status.setText("正在下载新版本 …")
            w = UpdateDownloadWorker(info, dest)
            w.trying.connect(lambda host: self.log("尝试下载源：%s" % host))
            w.prog.connect(lambda g, t: self.lb_status.setText(
                "更新包下载中 %s（%.1f/%.1f MB）" % (("%.1f%%" % (g * 100.0 / t)) if t else "", g / 1048576.0,
                                                (t or info.get("size") or 0) / 1048576.0)))
            w.done.connect(self._on_update_downloaded)
            w.fail.connect(lambda m: (self.log("更新包下载失败：%s" % m), self.lb_status.setText("更新失败"),
                                      QMessageBox.warning(self, "更新失败",
                                                          "%s\n\n所有下载源都失败。可以稍后再试，"
                                                          "或手动打开发布页下载。" % m)))
            w.start()
            self._upd_dl = w

        def _on_update_downloaded(self, size, sha, url):
            info = self.latest_info or {}
            want = (info.get("digest") or "")
            if want.lower().startswith("sha256:"):
                want = want[7:]
            if want and want.lower() != sha.lower():
                self.log("× 校验失败（sha256 不一致），已中止")
                QMessageBox.warning(self, "校验失败",
                                    "更新包 sha256 与记录不一致，已中止更新。\n请重试或联系助手。")
                return
            host = ""
            try:
                host = url.split("/")[2]
            except Exception:
                host = url[:40]
            self.log("更新包已就绪：%.1f MB，sha256 校验通过（来源 %s）" % (size / 1048576.0, host))
            new_exe = updater.staged_path(info.get("version") or "")
            if not os.path.exists(new_exe):
                self.log("× 找不到已下载的更新包：%s" % new_exe)
                QMessageBox.warning(self, "更新失败", "下载的更新包不见了，请重试。")
                return
            try:
                mode = updater.apply_update_windows(new_exe)
            except Exception as ex:
                self.log("启动更新脚本失败：%s" % ex)
                QMessageBox.warning(self, "更新失败", "无法启动替换脚本：%s" % ex)
                return
            self.log("正在关闭并替换为新版本，几秒后会自动重新打开 …")
            QMessageBox.information(self, "即将更新",
                                    "程序将自动关闭，替换成新版本后自动重新打开。\n"
                                    "如果 10 秒后没自动打开，手动双击程序图标即可。")
            QApplication.quit()

        # ---------- 失效反馈 ----------
        def do_copy_diag(self):
            lines = [
                "== 抖音下载器诊断信息 ==",
                "版本: v%s" % VERSION,
                "系统: %s   Python %s" % (sys.platform, sys.version.split()[0]),
                "程序路径: %s" % updater.current_exe(),
                "打包运行: %s" % bool(getattr(sys, "frozen", False)),
                "时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                "最近解析错误: %s" % (self.last_parse_error or "无"),
                "连续解析失败次数: %d" % self.fail_streak,
                "",
                "------ 最近日志 ------",
                "\n".join(self.log_box.toPlainText().splitlines()[-40:]),
            ]
            text = "\n".join(lines)
            QApplication.clipboard().setText(text)
            self.log("诊断信息已复制到剪贴板，直接粘给助手即可。")
            self.lb_tip.setText("诊断信息已复制 ✓")

        def _on_login_engine(self, e):
            self.login_engine = e

        def do_check_login(self):
            if self._check_worker and self._check_worker.isRunning():
                return
            if self.pw and self.pw.isRunning():
                # 解析时浏览器正忙，等它结束再查，避免抢同一个 profile
                QTimer.singleShot(3000, self.do_check_login)
                return
            self.btn_checklogin.setEnabled(False)
            self.lb_login.setText("登录状态：检测中…")
            self.log("检查登录状态（读取浏览器 profile 里的 cookie）…")
            w = LoginCheckWorker(self.login_engine)
            w.result.connect(self._on_login_status)
            w.fail.connect(lambda m: (self.lb_login.setText("登录状态：检测失败"),
                                      self.log("检查登录失败：%s" % m)))
            w.finished.connect(lambda: self.btn_checklogin.setEnabled(True))
            w.start()
            self._check_worker = w

        def _on_login_status(self, st):
            if st.get("logged_in"):
                nick = st.get("nickname") or ""
                text = "登录状态：已登录" + ("（%s）" % nick if nick else "")
                self.lb_login.setStyleSheet("color:#2fd07a;")
                self.log("登录状态：已登录%s（检测到 %s）" % (
                    ("，昵称 " + nick) if nick else "", ", ".join(st.get("cookies") or [])))
            else:
                text = "登录状态：未登录"
                self.lb_login.setStyleSheet("color:#ffb020;")
                self.log("登录状态：未登录（cookie 里没有 sessionid；点『登录抖音』扫码）")
            self.lb_login.setText(text)

        def closeEvent(self, ev):
            # 退出前收尾：等后台线程结束、关掉浏览器进程，避免残留
            for w in (self.pw, self.dw, self._check_worker, getattr(self, "_login_worker", None),
                      getattr(self, "_diag_worker", None), getattr(self, "_upd_check", None),
                      getattr(self, "_upd_dl", None)):
                try:
                    if w is not None and w.isRunning():
                        w.wait(2500)
                except Exception:
                    pass
            if getattr(self, "web", None) is not None:
                try:
                    self.web.stop()
                except Exception:
                    pass
            if self.login_engine is not None:
                try:
                    self.login_engine.kill()
                except Exception:
                    pass
            super().closeEvent(ev)

        # ---------- 打开文件 ----------
        def open_path(self, path):
            if not path or not os.path.exists(path):
                return
            try:
                if sys.platform.startswith("win"):
                    os.startfile(path)  # noqa
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
            except Exception as e:
                self.log("打开失败：%s" % e)

        def reveal(self, path):
            if not path:
                return
            d = os.path.dirname(path)
            try:
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", "/select,", path])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", path])
                else:
                    subprocess.Popen(["xdg-open", d])
            except Exception as e:
                self.log("打开目录失败：%s" % e)

    app = QApplication(sys.argv)
    try:
        app.setFont(QFont("Microsoft YaHei UI", 10))
    except Exception:
        pass
    app.setStyleSheet(STYLE)
    w = Main()
    w.show()
    w.log("就绪。把抖音分享链接粘到上面的输入框，点『解析』。")
    if test_hook is not None:
        test_hook(app, w)
    try:
        rc = app.exec()
    finally:
        # 退出前统一收尾：停网页版、关浏览器、等后台线程，避免解释器退出时崩
        try:
            if getattr(w, "web", None) is not None:
                w.web.stop()
                w.web = None
        except Exception:
            pass
        try:
            for name in ("_upd_check", "_upd_dl", "_diag_worker", "_login_worker", "pw", "dw"):
                t = getattr(w, name, None)
                if t is not None and hasattr(t, "isRunning") and t.isRunning():
                    t.wait(2000)
        except Exception:
            pass
        try:
            if getattr(w, "login_engine", None) is not None:
                w.login_engine.kill()
        except Exception:
            pass
        try:
            w.close()
        except Exception:
            pass
    return rc


def version_consistency():
    """自检用：桌面版 / 手机网页版 / version.py 的版本必须一致"""
    try:
        import version as vmod
        import webserver as ws
        return {
            "version.py": vmod.VERSION,
            "app.py": VERSION,
            "手机网页版": ws.DEFAULT_APP_VERSION,
            "一致": vmod.VERSION == VERSION == ws.DEFAULT_APP_VERSION,
        }
    except Exception as ex:
        return {"错误": str(ex), "一致": False}


def run_selftest():
    """打包后自检：不联网，只验证运行库是否齐全。退出码 0=正常 3=缺件"""
    lines = []
    ok = True

    def say(*a):
        msg = " ".join(str(x) for x in a)
        lines.append(msg)
        try:
            print(msg)
        except Exception:
            pass

    say("== 抖音下载器自检 ==")
    say("Python:", sys.version.split()[0], sys.platform)
    try:
        import PySide6
        from PySide6 import QtCore, QtWidgets
        say("PySide6:", PySide6.__version__, "Qt:", QtCore.qVersion())
    except Exception as e:
        ok = False
        say("PySide6 不可用:", e)
    try:
        import cdp
        import engine
        say("模块: cdp/engine/dycore OK")
    except Exception as e:
        ok = False
        say("内部模块导入失败:", e)
    try:
        import engine as eng
        say("浏览器:", eng.find_browser())
    except Exception as e:
        say("未找到浏览器（不影响 exe 完整性，运行时需要 Edge/Chrome）:", e)
    try:
        import updater
        say("自动更新模块 OK（可自更新: %s）" % updater.can_self_update())
    except Exception as e:
        ok = False
        say("自动更新模块失败:", e)
    try:
        from cdp import WebSocket  # noqa
        say("WebSocket 客户端 OK")
    except Exception as e:
        ok = False
        say("WebSocket 客户端失败:", e)
    try:
        vc = version_consistency()
        if vc.get("一致"):
            say("版本一致性 OK:", vc.get("version.py", ""))
        else:
            ok = False
            say("版本不一致（桌面版/手机版/version.py 必须相同）:", vc)
    except Exception as e:
        ok = False
        say("版本一致性检查失败:", e)
    say("结论:", "正常" if ok else "存在缺件")
    try:
        with open(os.path.join(os.getcwd(), "douyin_selftest.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return 0 if ok else 3


def run_status():
    """命令行查登录状态"""
    import engine as eng
    e = eng.BrowserEngine(headless=True)
    try:
        st = e.login_status()
        if st.get("logged_in"):
            print("登录状态：已登录 %s（cookie: %s）" % (
                st.get("nickname") or "", ", ".join(st.get("cookies") or [])))
        else:
            print("登录状态：未登录（只检测到 %s）" % (", ".join(st.get("cookies") or []) or "无 cookie"))
        return 0
    except Exception as ex:
        print("检测失败：%s" % ex)
        return 1
    finally:
        e.kill()


def run_web_cli(argv):
    """无界面常驻：app.exe --web [端口] [--out 目录]  手机浏览器访问打印出来的地址"""
    port = 8990
    out_dir = os.path.expanduser("~/Downloads")
    for i, a in enumerate(argv):
        if a == "--web" and i + 1 < len(argv) and argv[i + 1].isdigit():
            port = int(argv[i + 1])
        if a in ("-o", "--out") and i + 1 < len(argv):
            out_dir = argv[i + 1]
    srv = webserver.WebServer(out_dir, port=port, version=VERSION,
                              log=lambda s: print("[web] %s" % s, flush=True), headless=True)
    url = srv.start()
    print("手机网页版已启动（保持本窗口不关）：", flush=True)
    for u in srv.urls():
        print("  " + u, flush=True)
    print("下载目录：%s" % out_dir, flush=True)
    try:
        while True:
            time.sleep(3600)
    finally:
        srv.stop()


def main():
    # --windowed 打包后没有控制台，stdout 是 None，直接 print 会崩
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    argv = sys.argv[1:]
    try:
        updater.cleanup_staged()          # 清掉上次更新留在临时目录的 exe
    except Exception:
        pass
    if "--install-update" in argv:
        # 被 updater 调起来的新版本：等旧进程退出 → 覆盖旧 exe → 启动新版
        old_exe, old_pid = "", 0
        for i, a in enumerate(argv):
            if a == "--old-exe" and i + 1 < len(argv):
                old_exe = argv[i + 1]
            if a == "--old-pid" and i + 1 < len(argv):
                try:
                    old_pid = int(argv[i + 1])
                except ValueError:
                    old_pid = 0
        sys.exit(updater.install_update(old_exe=old_exe, old_pid=old_pid, launch=True))
    if "--selftest" in argv or "--check" in argv:
        sys.exit(run_selftest())
    if "--status" in argv:
        sys.exit(run_status())
    if "--web" in argv:
        try:
            run_web_cli(argv)
            sys.exit(0)
        except KeyboardInterrupt:
            sys.exit(130)
    if any(a in ("--cli", "-c", "--help", "-h") for a in argv) or ("--quality" in argv):
        try:
            sys.exit(run_cli(argv))
        except KeyboardInterrupt:
            sys.exit(130)
    try:
        import PySide6  # noqa
    except Exception:
        print("未安装 PySide6，无法启动图形界面。\n"
              "可改用命令行模式：app.py --cli <抖音链接> -q 1080p -o 保存目录")
        sys.exit(2)
    try:
        sys.exit(run_gui())
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
