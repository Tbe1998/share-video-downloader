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
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APP_NAME = "DouyinDownloader"
APP_TITLE = "抖音无水印下载器"
VERSION = "2.0"

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
        print("错误: 链接里找不到作品ID")
        return 2
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
            ratios = [{"gear": "page", "height": 720, "size": 0,
                       "url": res["fallback_url"], "urls": [res["fallback_url"]], "score": 0}]
        else:
            print("错误: 解析失败（可能需要先在本程序里点『登录抖音』扫码登录）")
            return 1
        pick = eng.choose(ratios, quality)
        print("       可选: %s" % ", ".join(sorted({str(r.get("height")) + "p" for r in ratios})))
        print("[3/3] 下载 %sp …" % (pick.get("height") or "?"))
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
QPushButton:hover:not(:disabled) { background:#232b3d; border-color:#3b4a6b; }
QPushButton:disabled { color:#5b6577; border-color:#1e2432; }
QPushButton#primary { background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff2d55, stop:1 #ff5c7a);
    border:none; color:#fff; font-weight:600; }
QPushButton#primary:hover:not(:disabled) { background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff3b60, stop:1 #ff6c88); }
QProgressBar { background:#151922; border:1px solid #252c3b; border-radius:7px; height:14px; text-align:center; color:#c9d4e6; }
QProgressBar::chunk { border-radius:6px; background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff2d55, stop:1 #25f4ee); }
QLabel#hint { color:#8b95a9; font-size:12px; }
QLabel#title { font-size:17px; font-weight:600; }
QLabel#sub { color:#8b95a9; font-size:12px; }
QLabel#info { background:#151922; border:1px solid #252c3b; border-radius:10px; padding:12px; color:#cfd8e8; }
QPlainTextEdit#log { font-family:Consolas,"Courier New",monospace; font-size:12px; color:#9fb4d8; background:#0b0e15; }
"""


def run_gui(test_hook=None):
    from PySide6.QtCore import QThread, Qt, Signal, QSize
    from PySide6.QtGui import QFont, QPixmap, QAction
    from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QGridLayout,
                                   QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                                   QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                                   QSizePolicy, QVBoxLayout, QWidget)
    import engine as eng
    import dycore

    class ParseWorker(QThread):
        ok = Signal(dict)
        fail = Signal(str)
        log = Signal(str)

        def __init__(self, aweme_id, headless):
            super().__init__()
            self.aweme_id = aweme_id
            self.headless = headless

        def run(self):
            e = None
            try:
                e = eng.BrowserEngine(headless=self.headless)
                self.log.emit("启动浏览器引擎：%s" % os.path.basename(e.exe))
                e.start()
                self.log.emit("打开作品页并等待页面完成签名请求 …")
                res = e.fetch_detail(self.aweme_id, timeout=45)
                if res.get("detail"):
                    s = eng.summarize(res["detail"])
                    self.ok.emit({"meta": s, "ratios": s["ratios"], "mode": "detail"})
                elif res.get("fallback_url"):
                    self.log.emit("未取到码率列表，改用页面播放流（通常为 720P 无水印）")
                    u = res["fallback_url"]
                    pm = res.get("meta") or {}
                    self.ok.emit({"meta": {"aweme_id": self.aweme_id,
                                           "desc": (pm.get("desc") or pm.get("title") or "").strip(),
                                           "author": (pm.get("author") or "").strip(),
                                           "height": 720, "duration": 0},
                                  "ratios": [{"gear": "page", "height": 720, "size": 0,
                                              "url": u, "urls": [u], "score": 0}],
                                  "mode": "page"})
                else:
                    self.fail.emit("解析失败：页面没有返回作品数据。\n"
                                   "可能原因：① 作品已删除/私密 ② 被风控，请点『登录抖音』扫码登录后再试")
            except Exception as ex:
                self.fail.emit("%s" % ex)
            finally:
                if e:
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
        done = Signal(str)

        def run(self):
            try:
                e = eng.BrowserEngine(headless=False)
                self.log.emit("已打开浏览器，请用抖音 App 扫码登录，登录后关闭该浏览器窗口 …")
                e.start()
                e.open_login()
                self.done.emit("浏览器已打开，扫码登录后关闭浏览器窗口即可")
            except Exception as ex:
                self.done.emit("打开浏览器失败: %s" % ex)

    class Main(QMainWindow):
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
            self._build()

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
            self.btn_login = QPushButton("登录抖音")
            self.btn_login.setToolTip("遇到风控或私密作品时，扫码登录一次即可（登录状态会保存）")
            self.btn_login.clicked.connect(self.do_login)
            head.addWidget(self.btn_login)
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
            aweme_id = extract_aweme_id(text)
            if not aweme_id:
                QMessageBox.warning(self, "链接不对", "没从这个链接里找到作品ID，请确认是抖音作品的分享链接。")
                return
            if self.pw and self.pw.isRunning():
                return
            self.btn_parse.setEnabled(False)
            self.btn_dl.setEnabled(False)
            self.pb.setValue(0)
            self.lb_status.setText("解析中…")
            self.log("解析作品ID：%s" % aweme_id)
            self.pw = ParseWorker(aweme_id, headless=True)
            self.pw.log.connect(self.log)
            self.pw.ok.connect(self.on_parsed)
            self.pw.fail.connect(self.on_parse_fail)
            self.pw.finished.connect(lambda: self.btn_parse.setEnabled(True))
            self.pw.start()

        def on_parsed(self, pack):
            self.meta = pack["meta"]
            self.ratios = pack["ratios"]
            self.mode = pack.get("mode")
            hs = sorted({r.get("height") or 0 for r in self.ratios}, reverse=True)
            label = " / ".join("%dp" % h for h in hs if h) or "未知"
            if pack.get("mode") == "page":
                label += "（未取到多码率列表，使用页面播放流）"
            info = []
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
            self.lb_status.setText("解析失败")
            self.log("解析失败：%s" % msg)
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
            self.lb_status.setText("开始下载 %dp …" % (pick.get("height") or 0))
            self.log("开始下载：%dp  体积约 %s" % (pick.get("height") or 0,
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
            QMessageBox.information(
                self, "登录抖音",
                "将打开一个浏览器窗口，请在其中用抖音 App 扫码登录。\n"
                "登录成功后直接关闭该浏览器窗口，之后解析即可使用该登录状态。\n\n"
                "注意：登录期间请不要在这个窗口里点下载。")
            w = LoginWorker()
            w.log.connect(self.log)
            w.done.connect(lambda m: self.log(m))
            w.start()
            self._login_worker = w

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
    return app.exec()


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
        from cdp import WebSocket  # noqa
        say("WebSocket 客户端 OK")
    except Exception as e:
        ok = False
        say("WebSocket 客户端失败:", e)
    say("结论:", "正常" if ok else "存在缺件")
    try:
        with open(os.path.join(os.getcwd(), "douyin_selftest.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return 0 if ok else 3


def main():
    # --windowed 打包后没有控制台，stdout 是 None，直接 print 会崩
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    argv = sys.argv[1:]
    if "--selftest" in argv or "--check" in argv:
        sys.exit(run_selftest())
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
