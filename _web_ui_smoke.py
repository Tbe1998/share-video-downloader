#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机网页版 UI 端到端测试（真实浏览器、iPhone 视口）：解析 → 选清晰度 → 下载 → 保存链接。"""
import os
import re
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

SHOTS = "/tmp/mobile_shots"
URL_DEMO = "https://v.douyin.com/HBE6lyHX27c/"


def main():
    os.makedirs(SHOTS, exist_ok=True)
    log = "/tmp/webui_server.log"
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(["/opt/data/venvs/qt/bin/python", "app.py", "--web", "8996",
                             "--out", "/tmp/mobile_out"], cwd="/opt/data/douyin_dl/desktop",
                            stdout=open(log, "w"), stderr=subprocess.STDOUT, env=env)
    url = None
    for _ in range(40):
        time.sleep(0.5)
        try:
            txt = open(log).read()
        except Exception:
            continue
        m = re.search(r"(http://[\d.]+:8996/\?k=\w+)", txt)
        if m:
            url = m.group(1)
            break
    if not url:
        print("× 服务没起来")
        proc.terminate()
        return 1
    print("页面地址:", url)
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"))
            pg.goto(url, wait_until="load")
            pg.wait_for_timeout(800)
            pg.screenshot(path=SHOTS + "/1_start.png")
            print("1) 首页渲染 OK，版本标签:", pg.inner_text("#ver"))

            pg.fill("#url", URL_DEMO)
            pg.click("#btnParse")
            pg.wait_for_selector("#card1:not(.hide)", timeout=150000)
            pg.wait_for_timeout(600)
            pg.screenshot(path=SHOTS + "/2_parsed.png", full_page=True)
            print("2) 解析完成：标题 =", pg.inner_text("#title")[:40])
            print("   清晰度选项 =", pg.eval_on_selector("#q", "e=>e.options.length"))
            print("   封面可见 =", pg.eval_on_selector("#cover", "e=>!e.classList.contains('hide')"))

            pg.click("#btnDl")
            pg.wait_for_selector("#save:not(.hide)", timeout=300000)
            pg.wait_for_timeout(500)
            pg.screenshot(path=SHOTS + "/3_done.png", full_page=True)
            href = pg.get_attribute("#save", "href")
            print("3) 下载完成，提示 =", pg.inner_text("#dlMsg"))
            print("   保存链接 =", href)

            # 手机端点"保存到手机"实际取文件
            resp = pg.request.get("http://%s:8996%s" % (url.split("//")[1].split(":")[0], href))
            body = resp.body()
            print("4) 保存到手机：HTTP %d，%d 字节，类型 %s"
                  % (resp.status, len(body), resp.headers.get("content-type")))
            b.close()
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
