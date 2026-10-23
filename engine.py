#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用本机 Chrome/Edge 的引擎：让浏览器自己完成抖音的签名请求，我们只取结果。
原理：打开作品页 -> 页面自身 JS 会发一次合法的 aweme/detail 请求（带 a_bogus 签名）
      -> 从 performance 里取出那条已签名 URL -> 在页面内 fetch 重放拿到完整 JSON
      -> 解析出各档码率的无水印直链。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from cdp import CDPSession, WSError

WIN_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]
NIX_CANDIDATES = [
    "/opt/data/home/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0")

DETAIL_URL_JS = r"""
(() => {
  const es = performance.getEntriesByType('resource').map(e => e.name);
  const u = es.filter(n => n.includes('/aweme/detail/'));
  return u.length ? u[u.length - 1] : null;
})()
"""

FETCH_JS = r"""
(async () => {
  const es = performance.getEntriesByType('resource').map(e => e.name);
  const u = es.filter(n => n.includes('/aweme/detail/')).pop();
  if (!u) return JSON.stringify({__err: 'no_sign_url'});
  try {
    const r = await fetch(u, {credentials: 'include'});
    const t = await r.text();
    return JSON.stringify({__status: r.status, __len: t.length, body: t});
  } catch (e) {
    return JSON.stringify({__err: String(e)});
  }
})()
"""

VIDEO_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return null;
  return v.currentSrc || v.src || null;
})()
"""

MEDIA_JS = r"""
(() => {
  const names = performance.getEntriesByType('resource').map(e => e.name)
      .filter(n => /douyinvod|mime_type=video|\.mp4\?/.test(n));
  const v = document.querySelector('video');
  const cur = v ? (v.currentSrc || v.src || '') : '';
  return JSON.stringify({media: names.slice(-16), cur: cur});
})()
"""


PAGE_META_JS = r"""
(() => {
  const pick = s => (document.querySelector(s) || {}).content || '';
  let desc = pick('meta[name="description"]') || '';
  let title = document.title || '';
  let author = pick('meta[property="og:title"]') || '';
  const nameEl = document.querySelector('[data-e2e="video-detail"] [data-e2e="user-name"]');
  if (nameEl && nameEl.innerText) author = author || nameEl.innerText.trim();
  return JSON.stringify({title: title, desc: desc, author: author});
})()
"""


def _br_of(url):
    m = re.search(r"[?&]br=(\d+)", url or "")
    return int(m.group(1)) if m else 0


class EngineError(RuntimeError):
    pass


def find_browser(explicit=None):
    if explicit and os.path.isfile(explicit):
        return explicit
    cands = WIN_CANDIDATES if sys.platform.startswith("win") else NIX_CANDIDATES
    for p in cands:
        if p and os.path.isfile(p):
            return p
    for name in ("chrome", "chromium", "microsoft-edge", "msedge", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    raise EngineError("没找到 Chrome 或 Edge，请先安装 Microsoft Edge（Windows 自带）")


def profile_dir(base=None):
    if base:
        return base
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        root = os.path.join(os.path.expanduser("~"), ".local", "share")
    d = os.path.join(root, "DouyinDownloader", "browser-profile")
    os.makedirs(d, exist_ok=True)
    return d


class BrowserEngine:
    """长驻的浏览器进程 + 一个页面 session。"""

    def __init__(self, exe=None, headless=False, port=0, profile=None, log=print):
        self.exe = find_browser(exe)
        self.headless = headless
        self.port = port or _free_port()
        self.profile = profile_dir(profile)
        self.log = log
        self.proc = None
        self.sess = None

    # ---------- 进程 ----------
    def start(self, timeout=25):
        if self.proc and self.proc.poll() is None:
            return
        args = [self.exe,
                "--remote-debugging-port=%d" % self.port,
                "--user-data-dir=%s" % self.profile,
                "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,OptimizationHints",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1180,820"]
        if self.headless:
            args.append("--headless=new")
        if not sys.platform.startswith("win"):
            args += ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        args.append("about:blank")
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=creation)
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                ver = json.loads(_http("http://127.0.0.1:%d/json/version" % self.port))
                self._browser_ws = ver.get("webSocketDebuggerUrl")
                self._connect_page()
                return
            except Exception:
                time.sleep(0.4)
        raise EngineError("浏览器启动超时（端口 %d）" % self.port)

    def _connect_page(self):
        # 复用已有页面 target，没有就新建
        try:
            pages = json.loads(_http("http://127.0.0.1:%d/json/list" % self.port))
        except Exception:
            pages = []
        target = None
        for p in pages:
            if p.get("type") == "page" and p.get("webSocketDebuggerUrl"):
                target = p
                break
        if target is None:
            target = json.loads(_http("http://127.0.0.1:%d/json/new?url=about:blank" % self.port,
                                      method="PUT"))
        self.sess = CDPSession(target["webSocketDebuggerUrl"])
        try:
            self.sess.call("Page.enable")
            self.sess.call("Runtime.enable")
        except WSError:
            pass

    def close(self):
        try:
            if self.sess:
                self.sess.close()
        except Exception:
            pass
        self.sess = None

    def kill(self):
        self.close()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=6)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    # ---------- 页面操作 ----------
    def goto(self, url, wait=2.0):
        self.start()
        self.sess.call("Page.navigate", {"url": url})
        time.sleep(wait)

    def open_login(self):
        """打开抖音首页供用户扫码登录（登录状态存进独立 profile，长期有效）"""
        self.goto("https://www.douyin.com/", wait=1.5)

    def evaluate(self, js, await_promise=False):
        self.start()
        return self.sess.evaluate(js, await_promise=await_promise)

    # ---------- 核心：取作品详情 ----------
    def fetch_detail(self, aweme_id, timeout=40, want_video_url_fallback=True):
        self.start()
        page_url = "https://www.douyin.com/video/%s" % aweme_id
        self.sess.call("Page.navigate", {"url": page_url})
        deadline = time.time() + timeout
        signed = None
        while time.time() < deadline:
            time.sleep(1.2)
            try:
                signed = self.sess.evaluate(DETAIL_URL_JS)
            except WSError:
                signed = None
            if signed:
                break
        if not signed:
            if want_video_url_fallback:
                u = self._wait_video_url(10)
                return {"fallback_url": u, "signed": None, "meta": self.page_meta()}
            raise EngineError("页面没有发出作品详情请求（多半是未登录或被风控）")
        # 在页面内重放这条已签名请求
        for attempt in range(3):
            raw = self.sess.evaluate(FETCH_JS, await_promise=True)
            try:
                pack = json.loads(raw)
            except Exception:
                raise EngineError("读取作品数据失败")
            if pack.get("__err"):
                time.sleep(1.5)
                continue
            body = pack.get("body") or ""
            if not body.strip():
                time.sleep(1.5)
                continue
            try:
                data = json.loads(body)
            except Exception:
                time.sleep(1.0)
                continue
            detail = data.get("aweme_detail") or {}
            if detail:
                return {"detail": detail, "signed": signed}
            time.sleep(1.2)
        u = self._wait_video_url(10)
        return {"fallback_url": u, "signed": signed,
                "meta": self.page_meta() if u else {}}

    def _wait_video_url(self, timeout=12):
        """兜底：从页面已加载的媒体流里挑码率最高的一条直链"""
        end = time.time() + timeout
        best_score, best_url = 0, None
        while time.time() < end:
            try:
                raw = self.sess.evaluate(MEDIA_JS)
                pack = json.loads(raw) if raw else {}
            except Exception:
                pack = {}
            cands = list(pack.get("media") or [])
            if pack.get("cur"):
                cands.append(pack["cur"])
            for u in cands:
                score = _br_of(u)
                if score > best_score:
                    best_score, best_url = score, u
            if best_url:
                break
            time.sleep(1.0)
        return best_url

    def page_meta(self):
        try:
            raw = self.sess.evaluate(PAGE_META_JS)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}


QUALITY_TARGET = {"1080p": 1080, "720p": 720, "540p": 540, "480p": 480}


def choose(ratios, quality="auto"):
    """按用户选择挑一档；auto=最清晰"""
    if not ratios:
        return None
    if quality in (None, "", "auto"):
        return ratios[0]
    target = QUALITY_TARGET.get(quality)
    if not target:
        return ratios[0]
    ok = [r for r in ratios if (r.get("height") or 0) <= target + 8]
    if ok:
        return max(ok, key=lambda r: r.get("height") or 0)
    return min(ratios, key=lambda r: r.get("height") or 0)


def download_entry(entry, out_dir, meta, progress=None, should_stop=None):
    """把一个码率档的直链存成 mp4；progress(读取字节, 总字节, 速度)"""
    os.makedirs(out_dir, exist_ok=True)
    from dycore import unique_path
    item = {
        "desc": meta.get("desc") or "",
        "author": meta.get("author") or "",
        "height": entry.get("height") or meta.get("height") or 0,
        "aweme_id": meta.get("aweme_id") or "",
    }
    name_stem = _clean((item["author"] + "_" + (item["desc"] or ""))).strip(" _")
    # 页面兜底模式下 desc 是整段 meta 描述，剪掉来源尾巴与话题，避免文件名过长
    name_stem = re.split(r"\s+-\s+|#|发布于抖音", name_stem)[0].strip(" _")[:48]
    if not name_stem:
        name_stem = "douyin"
    name = "%s_p%s_%s.mp4" % (name_stem, entry.get("height") or "?", item["aweme_id"])
    path = unique_path(os.path.join(out_dir, name))
    tmp = path + ".part"
    hdr = {"User-Agent": UA, "Referer": "https://www.douyin.com/",
           "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    last = None
    for url in (entry.get("urls") or [entry.get("url")]):
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers=hdr)
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=45) as resp:
                total = int(resp.headers.get("Content-Length") or entry.get("size") or 0)
                got = 0
                with open(tmp, "wb") as f:
                    while True:
                        if should_stop and should_stop():
                            raise EngineError("已取消")
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if progress:
                            speed = got / max(0.2, time.time() - t0)
                            progress(got, total, speed)
            if os.path.getsize(tmp) < 4096:
                raise EngineError("文件过小")
            os.replace(tmp, path)
            return path
        except Exception as e:
            last = e
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    raise EngineError("下载失败: %s" % last)


def _clean(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _http(url, method="GET", timeout=10):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------- 解析 detail -> 可用信息 ----------------
RATIO_ORDER = ["4k", "2160p", "1080p", "720p", "540p", "480p"]


def _score(gear_name, w, h):
    s = (w or 0) * (h or 0)
    g = (gear_name or "").lower()
    if "4k" in g or "2160" in g:
        s += 10 ** 9
    elif "1080" in g:
        s += 10 ** 8
    elif "720" in g:
        s += 10 ** 7
    return s


def pick_urls(detail, quality="auto"):
    """返回 [(标签, 分辨率, 体积, url)] ，按清晰度降序"""
    v = detail.get("video") or {}
    out = []
    for br in (v.get("bit_rate") or []):
        pa = br.get("play_addr") or {}
        urls = pa.get("url_list") or []
        if not urls:
            continue
        w, h = pa.get("width"), pa.get("height")
        gear = br.get("gear_name") or ""
        out.append({
            "gear": gear, "width": w, "height": h,
            "size": pa.get("data_size") or 0,
            "url": urls[0], "urls": urls,
            "score": _score(gear, w, h),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    if not out:
        # 有的作品只有 play_addr
        pa = v.get("play_addr") or {}
        urls = pa.get("url_list") or []
        if urls:
            out.append({"gear": "default", "width": v.get("width"), "height": v.get("height"),
                        "size": pa.get("data_size") or 0, "url": urls[0], "urls": urls,
                        "score": 0})
    return out


def summarize(detail):
    v = detail.get("video") or {}
    a = detail.get("author") or {}
    st = detail.get("statistics") or {}
    cover = ""
    for k in ("cover_original_scale", "origin_cover", "cover"):
        lst = ((v.get(k) or {}).get("url_list") or [])
        if lst:
            cover = lst[-1]
            break
    return {
        "aweme_id": detail.get("aweme_id"),
        "desc": (detail.get("desc") or "").strip(),
        "author": a.get("nickname") or "抖音用户",
        "duration": round((v.get("duration") or 0) / 1000.0, 1),
        "width": v.get("width"), "height": v.get("height"),
        "cover": cover,
        "digg": st.get("digg_count", 0),
        "comment": st.get("comment_count", 0),
        "ratios": pick_urls(detail),
    }
