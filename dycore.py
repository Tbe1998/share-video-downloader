#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音下载核心（纯标准库：urllib + json + re）。桌面版与命令行版共用。
流程: 短链跳转取 aweme_id -> 抓 iesdouyin 分享页 SSR -> 提取 play_addr.uri
      -> 构造 aweme.snssdk.com /play 无水印直链 -> 分块下载(带进度回调)
"""
import gzip
import io
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

PLAY_BASE = "https://aweme.snssdk.com/aweme/v1/play/?line=0&video_id={uri}&ratio={ratio}"
RATIO_LABEL = {"auto": "自动（最高）", "1080p": "1080P 高清", "720p": "720P", "540p": "540P 省流"}


class DouyinError(RuntimeError):
    pass


# ---------------- HTTP 基座 ----------------
def http_get(url, headers=None, timeout=25, stream=False, max_redirect=8):
    """返回 (resp, session_less)。stream=True 时返回未读 body 的 response 供分块读取。"""
    req = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise DouyinError("HTTP %s（%s）" % (e.code, "被拦截/风控" if e.code in (403, 429) else url[:60]))
    except urllib.error.URLError as e:
        raise DouyinError("网络错误: %s" % e.reason)
    except (socket.timeout, TimeoutError):
        raise DouyinError("请求超时")
    return resp


def _read_text(resp):
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    charset = "utf-8"
    ct = resp.headers.get("Content-Type") or ""
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    if m:
        charset = m.group(1)
    return raw.decode(charset, errors="replace")


# ---------------- 客户端 ----------------
class DouyinClient:
    def __init__(self, cookie=""):
        self.cookie = (cookie or "").strip()

    def _h(self, ua=UA_MOBILE, extra=None):
        h = {"User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9",
             "Accept": "*/*"}
        if self.cookie:
            h["Cookie"] = self.cookie
        if extra:
            h.update(extra)
        return h

    # 1. 链接 -> aweme_id
    def resolve_id(self, url):
        url = (url or "").strip()
        if not url:
            raise DouyinError("链接为空")
        if url.startswith("复制打开抖音") or not url.startswith("http"):
            m = re.search(r"https?://[^\s\"'<>，,]+", url)
            if m:
                url = m.group(0)
            else:
                raise DouyinError("没找到链接，请粘贴完整分享文字或链接")
        if "v.douyin.com" in url or "iesdouyin.com/share" in url or "/share/" in url:
            try:
                resp = http_get(url, self._h(UA_DESKTOP), timeout=20)
                final = resp.geturl()
                resp.close()
                url = final
            except DouyinError as e:
                raise DouyinError("短链跳转失败: %s" % e)
        for pat in (r"/(?:video|note|slides)/(\d+)", r"(?:aweme_id=|modal_id=)(\d+)", r"(\d{15,20})"):
            m = re.search(pat, url)
            if m:
                return m.group(1)
        raise DouyinError("这个链接里没解析出作品ID")

    # 2. 分享页 SSR
    def parse(self, url):
        aweme_id = self.resolve_id(url)
        item = None
        last = None
        for ua in (UA_MOBILE, UA_DESKTOP):
            try:
                page = "https://www.iesdouyin.com/share/video/%s/?region=CN&mid=" % aweme_id
                resp = http_get(page, self._h(ua), timeout=25)
                html = _read_text(resp)
                resp.close()
                pos = html.find('"play_addr"')
                if pos < 0:
                    last = "分享页未返回视频数据（可能被风控）"
                    continue
                item = _extract_item(html, pos)
                if item:
                    break
            except DouyinError as e:
                last = str(e)
                continue
            except Exception as e:
                last = str(e)
                continue
        if not item:
            raise DouyinError(last or "解析失败")

        v = item.get("video") or {}
        uri = (v.get("play_addr") or {}).get("uri")
        if not uri:
            raise DouyinError("没取到视频地址（可能是图文笔记，或需要登录 Cookie）")
        author = item.get("author") or {}
        stats = item.get("statistics") or {}
        cover = ""
        for key in ("cover_original_scale", "origin_cover", "cover"):
            urls = ((v.get(key) or {}).get("url_list") or [])
            if urls:
                cover = urls[-1]
                break
        h = v.get("height") or 0
        w = v.get("width") or 0
        return {
            "aweme_id": aweme_id,
            "desc": (item.get("desc") or "").strip(),
            "author": author.get("nickname") or "抖音用户",
            "uri": uri,
            "width": w,
            "height": h,
            "resolution": ("%dx%d" % (w, h)) if (w and h) else "",
            "duration": round((v.get("duration") or 0) / 1000.0, 1),
            "cover": cover,
            "digg": stats.get("digg_count", 0),
            "comment": stats.get("comment_count", 0),
            "ratios": available_ratios(h),
            "url": "https://www.douyin.com/video/%s" % aweme_id,
        }

    def fetch_cover(self, url, timeout=20):
        resp = http_get(url, self._h(UA_MOBILE, {"Referer": "https://www.douyin.com/"}), timeout=timeout)
        return resp.read()

    # 3. 下载
    def download(self, item, quality, out_dir, progress=None, should_stop=None):
        os.makedirs(out_dir, exist_ok=True)
        path = unique_path(os.path.join(out_dir, build_filename(item, quality)))
        ratios = available_ratios(item.get("height") or 0) if quality in (None, "", "auto") else [quality]
        hdr = self._h(UA_DESKTOP, {"Referer": "https://www.douyin.com/"})
        last = None
        for ratio in ratios:
            url = PLAY_BASE.format(uri=item["uri"], ratio=ratio)
            tmp = path + ".part"
            try:
                resp = http_get(url, hdr, timeout=45)
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                with open(tmp, "wb") as f:
                    while True:
                        if should_stop and should_stop():
                            raise DouyinError("已取消")
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got, total)
                resp.close()
                if got < 4096:
                    raise DouyinError("文件过小，下载被拦截")
                os.replace(tmp, path)
                return path
            except DouyinError as e:
                if "已取消" in str(e):
                    _silent_remove(tmp)
                    raise
                last = e
                _silent_remove(tmp)
            except Exception as e:
                last = e
                _silent_remove(tmp)
        raise DouyinError("下载失败: %s" % last)


# ---------------- 辅助 ----------------
_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _silent_remove(p):
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def available_ratios(h):
    h = h or 0
    if h >= 1080:
        return ["1080p", "720p", "540p"]
    if h >= 720:
        return ["720p", "540p"]
    return ["720p", "540p"]


def build_filename(item, quality):
    desc = _BAD.sub(" ", item.get("desc") or "").strip()
    desc = re.sub(r"\s+", " ", desc)[:28].strip()
    q = quality if quality not in (None, "", "auto") else ("1080p" if (item.get("height") or 0) >= 1080 else "720p")
    parts = [p for p in (item.get("author"), desc) if p]
    stem = "_".join(parts) or "douyin"
    return "%s_%s_%s.mp4" % (stem, q, item.get("aweme_id", ""))


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists("%s(%d)%s" % (base, n, ext)):
        n += 1
    return "%s(%d)%s" % (base, n, ext)


def human_size(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%.1f%s" % (n, u)
        n /= 1024.0
    return "%.1fGB" % n


def _extract_item(html, around):
    def span(o):
        d = 0
        j = o
        n = len(html)
        while j < n:
            c = html[j]
            if c == "{":
                d += 1
            elif c == "}":
                d -= 1
                if d == 0:
                    return j
            j += 1
        return -1

    opens = [m.start() for m in re.finditer(r"\{", html[:around])]
    for o in reversed(opens[-250:]):
        cl = span(o)
        if cl < 0 or cl - o > 300000:
            continue
        seg = html[o:cl + 1]
        if '"video"' in seg and '"desc"' in seg and '"author"' in seg:
            try:
                return json.loads(seg)
            except Exception:
                continue
    return None
