#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页版端到端测试：启动服务 → 解析 → 下载 → 取回文件（模拟手机）。"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/opt/data/douyin_dl/desktop")
import webserver

OUT = "/tmp/webdl_out"
URL = sys.argv[1] if len(sys.argv) > 1 else "https://v.douyin.com/HBE6lyHX27c/"


def api(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.status, json.loads(r.read() or b"{}")


def main():
    srv = webserver.WebServer(OUT, port=8990, log=print, version="2.7-test", headless=True)
    base_root = srv.start()
    root = base_root.rsplit("/?", 1)[0].rstrip("/") + "/"   # http://ip:port/
    token = base_root.split("k=")[1]

    def base_url(path):
        return root + path + ("&" if "?" in path else "?") + "k=" + token
    print("URL:", base_root)

    # 1) 页面 + ping
    with urllib.request.urlopen(base_root, timeout=20) as r:
        html = r.read().decode()
    print("1) 首页 %d 字节, 标题存在=%s" % (len(html), "分享视频下载器" in html))
    print("   ping:", api(base_url("api/ping"))[1])

    # 2) 解析
    t0 = time.time()
    st, r = api(base_url("api/parse"), {"url": URL})
    jid = r["job"]
    print("2) 解析任务:", jid, st)
    job = None
    while time.time() - t0 < 120:
        _, r = api(base_url("api/job?id=" + jid))
        job = r["job"]
        if job["state"] != "running":
            break
        time.sleep(1.5)
    print("   状态:", job["state"], "| 模式:", job.get("mode"), "| 标题:", (job.get("meta") or {}).get("desc", "")[:30])
    if job["state"] != "done":
        print("   错误:", job.get("error"))
        srv.stop()
        return 1
    ratios = job["ratios"]
    print("   清晰度档位:", [(x.get("height"), x.get("gear")) for x in ratios][:6])
    meta = job["meta"]

    # 3) 封面
    try:
        with urllib.request.urlopen(base_url("cover?id=" + jid), timeout=20) as r:
            cov = r.read()
        print("3) 封面 %d 字节" % len(cov))
    except Exception as e:
        print("3) 封面失败:", e)

    # 4) 下载
    t0 = time.time()
    st, r = api(base_url("api/download"), {"ratio": ratios[0], "meta": meta})
    dj = r["job"]
    print("4) 下载任务:", dj)
    while time.time() - t0 < 240:
        _, r = api(base_url("api/job?id=" + dj))
        j = r["job"]
        if j["state"] != "running":
            break
        time.sleep(1.2)
    print("   状态:", j["state"], "| 文件:", j.get("name"), "| %.2f MB" % ((j.get("total") or 0) / 1048576))
    if j["state"] != "done":
        print("   错误:", j.get("error"))
        srv.stop()
        return 1

    # 5) 取回文件（手机端"保存到手机"）
    with urllib.request.urlopen(base_url("f?id=" + dj + "&dl=1"), timeout=120) as r:
        blob = r.read()
        print("5) /f 返回 %d 字节, Content-Disposition=%s" % (len(blob), r.headers.get("Content-Disposition")))
    out = "/tmp/webdl_phone.mp4"
    open(out, "wb").write(blob)
    print("   保存到:", out, os.path.getsize(out), "字节")
    print("6) 历史:", api(base_url("api/history"))[1]["items"][:2])
    srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
