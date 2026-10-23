#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立的手机网页版服务（不依赖桌面版、不需要 PySide6）。

用法：
  python webapp.py                      # 默认 0.0.0.0:8990，token 随机
  python webapp.py --port 8990 --token myphone123 --out D:\\视频
  python webapp.py --token 固定口令      # 口令固定，手机收藏夹地址长期有效

它会自动读取程序文件夹里的登录态（cookies.json），首次解析时注入浏览器，
所以手机上用也能拿到 1080P/4K（前提是桌面版登录过并保存过 cookies）。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

def _fix_stdout():
    """Windows 下输出被重定向时默认是 cp1252/GBK，打中文会 UnicodeEncodeError。
    控制台（isatty）不要动，Python 会走 WriteConsoleW；只修重定向到文件的场景。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or not hasattr(stream, "reconfigure"):
                continue
            try:
                if stream.isatty():
                    continue
            except Exception:
                pass
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


import sessions   # noqa: E402
import webserver  # noqa: E402
from version import VERSION  # noqa: E402


def default_out():
    for p in (os.path.join(os.path.expanduser("~"), "Downloads"),
              os.path.join(os.path.expanduser("~"), "下载"),
              os.path.expanduser("~")):
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


def inject_saved_login(hub, log):
    """把桌面版保存的 cookies 注入浏览器，省得重新扫码"""
    saved = sessions.load()
    if not saved:
        log("本地没有保存的登录态（桌面版登录过一次就会自动保存）")
        return False
    try:
        e = hub.get_engine()
        n = e.set_cookies(saved)
        st = e.login_status(want_nickname=False)
        if st.get("logged_in"):
            log("已用本地保存的登录态登录（注入 %d 个 cookie，%s）"
                % (n, ", ".join(sorted(saved.keys()))))
            return True
        log("注入 %d 个 cookie，但抖音判定未登录（可能已过期，请在桌面版重新扫码）" % n)
    except Exception as ex:
        log("注入登录态失败：%s" % ex)
    return False


def main():
    _fix_stdout()
    ap = argparse.ArgumentParser(description="分享视频下载器 · 独立手机网页版")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DY_PORT", 8990)))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--out", default=os.environ.get("DY_OUT") or default_out(), help="下载保存目录")
    ap.add_argument("--token", default=os.environ.get("DY_TOKEN", ""), help="访问口令（固定下来手机收藏夹更省事）")
    ap.add_argument("--no-headless", action="store_true", help="浏览器显示窗口（调试用）")
    args = ap.parse_args()

    def log(s):
        print("[web %s] %s" % (time.strftime("%H:%M:%S"), s), flush=True)

    os.makedirs(args.out, exist_ok=True)
    srv = webserver.WebServer(args.out, port=args.port, host=args.host, version=VERSION,
                              log=log, headless=not args.no_headless)
    if args.token:
        srv.token = args.token
    url = srv.start()
    print("", flush=True)
    print("=" * 62, flush=True)
    print(" 分享视频下载器 · 手机网页版 v%s" % VERSION, flush=True)
    print("=" * 62, flush=True)
    for u in srv.urls():
        print("  " + u, flush=True)
    print("  下载保存到：%s" % args.out, flush=True)
    print("  口令：%s（地址里的 k= 参数，固定用 --token 可长期不变）" % srv.token, flush=True)
    print("=" * 62, flush=True)
    # 有保存的登录态就在后台先注入一次，等第一次解析时直接用
    try:
        inject_saved_login(srv.hub, log)
    except Exception as ex:
        log("预注入登录态跳过：%s" % ex)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n退出中 …", flush=True)
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
