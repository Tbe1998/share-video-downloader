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
import threading
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
  let cover = pick('meta[property="og:image"]') || pick('meta[name="og:image"]') || '';
  const v = document.querySelector('video');
  if (!cover && v && v.poster) cover = v.poster;
  const dur = (v && v.duration && isFinite(v.duration)) ? Math.round(v.duration * 10) / 10 : 0;
  return JSON.stringify({title: title, desc: desc, author: author, cover: cover, duration: dur});
})()
"""


NICK_JS = r"""
(() => {
  const sels = ['[data-e2e="user-name"]', '[data-e2e="user-info"] [data-e2e="user-name"]',
                'img[alt*="头像"]', '[data-e2e="live-avatar"] img'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el) {
      const t = (el.innerText || el.getAttribute('alt') || '').trim();
      if (t) return t;
    }
  }
  const t = document.title || '';
  return t ? t.split(/[-_|]/)[0].trim() : '';
})()
"""


def _js_str(s):
    return json.dumps(s)


def _br_of(url):
    m = re.search(r"[?&]br=(\d+)", url or "")
    return int(m.group(1)) if m else 0


class EngineError(RuntimeError):
    pass


# 同一个浏览器 profile 目录同一时刻只能被一个浏览器进程占用
_PROFILE_LOCK = threading.Lock()


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
    d = os.path.join(root, "ShareVideoDownloader", "browser-profile")
    os.makedirs(d, exist_ok=True)
    return d


class BrowserEngine:
    """长驻的浏览器进程 + 一个页面 session。"""

    _lock_held = False

    def __init__(self, exe=None, headless=False, port=0, profile=None, log=print):
        self.exe = find_browser(exe)
        self.headless = headless
        self.port = port or _free_port()
        self.profile = profile_dir(profile)
        self.log = log
        self.proc = None
        self.sess = None
        self._lock_held = False
        self._attached = False        # 是否是"接管"已有浏览器（不是自己启动的）
        self._attached_ok = True
        self._attached_check = 0.0

    # ---------- 进程 ----------
    def _acquire_profile(self):
        """同一个 profile 目录同一时刻只能有一个浏览器进程，否则后启动的会直接退出。"""
        if not self._lock_held:
            _PROFILE_LOCK.acquire()
            self._lock_held = True

    def _release_profile(self):
        if self._lock_held:
            self._lock_held = False
            try:
                _PROFILE_LOCK.release()
            except RuntimeError:
                pass

    def is_alive(self):
        if self._attached:
            now = time.time()
            if now - self._attached_check < 2.0:
                return self._attached_ok
            self._attached_check = now
            try:
                _http("http://127.0.0.1:%d/json/version" % self.port, timeout=2)
                self._attached_ok = True
            except Exception:
                self._attached_ok = False
            return self._attached_ok
        return bool(self.proc) and self.proc.poll() is None and self.sess is not None

    # ---------- 端口标记：接管上次没关干净的浏览器 ----------
    def _marker_path(self):
        return os.path.join(self.profile, "dy_engine.json")

    def _write_marker(self):
        try:
            with open(self._marker_path(), "w", encoding="utf-8") as f:
                json.dump({"port": self.port,
                           "pid": self.proc.pid if self.proc else 0,
                           "ts": int(time.time())}, f)
        except Exception:
            pass

    def _read_marker(self):
        try:
            with open(self._marker_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _clear_marker(self):
        try:
            os.remove(self._marker_path())
        except Exception:
            pass

    def _try_attach(self):
        """如果上次启动的浏览器（本程序的独立 profile）还活着，直接接管它"""
        m = self._read_marker()
        port = m.get("port")
        if not port:
            return False
        try:
            ver = json.loads(_http("http://127.0.0.1:%d/json/version" % port, timeout=3))
        except Exception:
            return False
        self.port = port
        self._browser_ws = ver.get("webSocketDebuggerUrl")
        try:
            self._connect_page()
        except Exception:
            return False
        self._attached = True
        self._attached_ok = True
        self._attached_check = time.time()
        return True

    def start(self, timeout=45):
        if self.is_alive():
            return
        if self._try_attach():
            self.log("已接管上次仍在运行的浏览器（端口 %d）" % self.port)
            return
        self._acquire_profile()
        for attempt in (1, 2):
            try:
                self._launch(timeout)
                self._write_marker()
                return
            except EngineError as e:
                if attempt == 1 and _should_cleanup_and_retry(str(e)):
                    self.log("检测到上一次的浏览器实例占着配置目录，正在清理后重试 …")
                    self._cleanup_profile_users()
                    time.sleep(1.5)
                    self.port = _free_port()
                    continue
                self._release_profile()
                if str(e) == "PORT_DEAD":
                    raise EngineError(
                        "浏览器进程起来了，但调试端口一直没打开（已自动重启过一次）。\n"
                        "常见原因：① 防火墙/杀毒软件拦了本机回环端口；② 浏览器被策略限制。\n"
                        "处理：点『诊断』查看端口与浏览器状态，或临时关闭安全软件后重试。")
                raise

    def _cleanup_profile_users(self):
        """清理占着本程序配置目录的浏览器进程（只动我们自己的 profile，不碰用户浏览器）"""
        killed = []
        m = self._read_marker()
        pid = m.get("pid")
        if pid and _pid_alive(pid):
            _kill_pid(pid)
            killed.append(pid)
        for p in _find_profile_processes(self.profile):
            if p not in killed and _pid_alive(p):
                _kill_pid(p)
                killed.append(p)
        self._clear_marker()
        if killed:
            self.log("已结束占用配置目录的浏览器进程：%s" % ", ".join(str(x) for x in killed))
        else:
            self.log("没有找到占用配置目录的进程（可能是安全软件拦截或端口被防火墙挡住）")
        return killed

    def _launch(self, timeout=45):
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
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=creation)
        except Exception:
            self._release_profile()
            raise
        t0 = time.time()
        last_err = None
        port_dead_grace = min(25.0, max(5.0, timeout * 0.6))
        while time.time() - t0 < timeout:
            # 关键：浏览器进程如果已经退出（多半是 profile 被占用 / 被安全软件拦），
            # 端口永远不会开，必须立刻报错并说清原因，而不是死等超时。
            rc = self.proc.poll()
            if rc is not None:
                self.proc = None
                raise EngineError(_explain_exit(rc))
            try:
                ver = json.loads(_http("http://127.0.0.1:%d/json/version" % self.port, timeout=3))
                self._browser_ws = ver.get("webSocketDebuggerUrl")
                self._connect_page()
                return
            except Exception as e:
                last_err = e
            elapsed = time.time() - t0
            if elapsed > port_dead_grace:
                # 进程还活着但调试端口一直不开：杀掉重来一次，通常能恢复
                self.log("浏览器进程活着但调试端口 %d 一直没打开（已等 %d 秒），重启浏览器 …"
                         % (self.port, int(elapsed)))
                self.kill()
                raise EngineError("PORT_DEAD")
            time.sleep(0.4)
        self.kill()
        raise EngineError(
            "浏览器启动超时（端口 %d，已等待 %d 秒）。\n"
            "端口一直没有响应，常见原因：① 防火墙/安全软件拦了本机回环端口；\n"
            "② 浏览器卡在首启引导页；③ 机器较慢，首次启动确实要更久。\n"
            "处理：重试一次，或点『诊断』查看端口与浏览器状态。最后错误：%s"
            % (self.port, timeout, last_err))

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
        if self._attached:
            # 接管来的浏览器是用户/上次留下的，别杀它，只断开连接
            self._attached = False
            self.sess = None
            return
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=6)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.proc is not None:
            self._clear_marker()
        self.proc = None
        self._release_profile()

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

    # ---------- 截图（网页版扫码登录用） ----------
    def screenshot_png(self, selector=None, max_width=520):
        """截当前页面（或指定元素）成 PNG 字节。selector 命中就用元素区域。"""
        params = {"format": "png", "captureBeyondViewport": False}
        if selector:
            try:
                js = ("""(() => { const el = document.querySelector(%s);
                       if (!el) return '';
                       const r = el.getBoundingClientRect();
                       if (r.width < 40 || r.height < 40) return '';
                       return JSON.stringify({x: Math.max(0, r.x), y: Math.max(0, r.y),
                                              width: r.width, height: r.height, scale: 1});
                     })()""" % _js_str(selector))
                raw = self.sess.evaluate(js)
                if raw:
                    clip = json.loads(raw)
                    clip["scale"] = 1
                    params["clip"] = clip
            except Exception:
                params.pop("clip", None)
        res = self.sess.call("Page.captureScreenshot", params)
        import base64
        data = base64.b64decode(res.get("data") or "")
        return data

    def click_text(self, texts, timeout=8):
        """按可见文字点元素（抖音登录按钮之类没有稳定属性时用）"""
        js = ("""(() => { const ts = %s;
                   const els = Array.from(document.querySelectorAll('button,[role="button"],div,span,a'));
                   for (const t of ts) {
                     for (const el of els) {
                       const s = (el.innerText || '').trim();
                       if (s === t || (s.length <= 6 && s.includes(t) && el.offsetParent !== null)) {
                         try { el.click(); return t; } catch (e) {}
                       }
                     }
                   }
                   return ''; })()""" % json.dumps(list(texts), ensure_ascii=False))
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                r = self.sess.evaluate(js)
                if r:
                    return r
            except Exception:
                pass
            time.sleep(0.5)
        return ""

    # ---------- cookie 读写（登录态持久化用） ----------
    def get_all_cookies(self, key_only=False):
        """读出浏览器里的 cookie。key_only=True 时只返回关键登录字段（dict）"""
        self.start()
        cookies = []
        for method in ("Network.getAllCookies", "Storage.getCookies"):
            try:
                res = self.sess.call(method)
                cookies = res.get("cookies") or []
                if cookies:
                    break
            except Exception:
                continue
        if key_only:
            try:
                import sessions
                return sessions._norm(cookies)
            except Exception:
                return {}
        return cookies

    def set_cookies(self, cookie_map, url="https://www.douyin.com/"):
        """把保存的 cookie 注入浏览器（换机器/清了 profile 后恢复登录）"""
        self.start()
        if not cookie_map:
            return 0
        try:                                   # 先落到抖音域，否则 domain 校验可能失败
            self.goto(url, wait=1.2)
        except Exception:
            pass
        items = []
        for name, c in cookie_map.items():
            if isinstance(c, dict):
                item = dict(c)
            else:
                item = {"name": name, "value": str(c), "domain": ".douyin.com", "path": "/"}
            if not item.get("value"):
                continue
            rec = {
                "name": item.get("name") or name,
                "value": item["value"],
                "domain": item.get("domain") or ".douyin.com",
                "path": item.get("path") or "/",
                "secure": bool(item.get("secure")),
                "httpOnly": bool(item.get("httpOnly")),
            }
            ss = item.get("sameSite")
            if ss in ("Strict", "Lax", "None"):
                rec["sameSite"] = ss
            exp = item.get("expires")
            try:
                if exp and float(exp) > 0:
                    rec["expires"] = float(exp)
            except Exception:
                pass
            items.append(rec)
        if not items:
            return 0
        try:
            self.sess.call("Network.setCookies", {"cookies": items})
        except Exception:
            # 老接口兜底：逐个 setCookie
            for rec in items:
                try:
                    self.sess.call("Network.setCookie", rec)
                except Exception:
                    pass
        try:
            self.sess.call("Page.reload", {"ignoreCache": True})
            time.sleep(1.5)
        except Exception:
            pass
        return len(items)

    # ---------- 登录状态 ----------
    def login_status(self, want_nickname=True):
        """用 CDP 读本 profile 的 cookie 判断是否已登录抖音"""
        self.start()
        cookies = []
        for method in ("Network.getAllCookies", "Storage.getCookies"):
            try:
                res = self.sess.call(method)
                cookies = res.get("cookies") or []
                if cookies:
                    break
            except Exception:
                continue
        names = {c.get("name") for c in cookies}
        auth_keys = [n for n in ("sessionid", "sessionid_ss", "sid_tt", "uid_tt", "ttwid", "passport_csrf_token")
                     if n in names]
        logged = any(n in names for n in ("sessionid", "sessionid_ss", "sid_tt"))
        nick = ""
        if logged and want_nickname:
            try:
                self.sess.call("Page.navigate", {"url": "https://www.douyin.com/"})
                time.sleep(3.0)
                nick = self.sess.evaluate(NICK_JS) or ""
            except Exception:
                nick = ""
        return {"logged_in": logged, "cookies": auth_keys, "nickname": nick,
                "cookie_count": len(cookies)}


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
    name = "%s_p%s_%s.mp4" % (name_stem, entry.get("height") or "页面流", item["aweme_id"])
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


def _explain_exit(rc):
    """把浏览器秒退的退出码翻译成人话（用户机器上实测见过 0 和 21）"""
    meaning = {
        0: "浏览器把请求转交给了已在运行的实例，然后正常退出——说明同一个配置目录已经被另一个浏览器占用",
        21: "配置目录被另一个浏览器进程锁住，无法启动",
        1: "浏览器启动失败（参数/环境问题，或系统没有可用的图形界面）",
    }
    hint = meaning.get(int(rc) if isinstance(rc, int) else rc, "浏览器异常退出")
    return ("浏览器启动后立刻退出（退出码 %s）：%s。\n"
            "最常见原因：上次的登录窗口/后台浏览器没关干净，仍占着本程序的配置目录。\n"
            "程序会自动清理并重试一次；若仍失败，请手动结束所有 Edge/Chrome 进程后重试，"
            "或点『诊断』查看详细信息。" % (rc, hint))


def _should_cleanup_and_retry(msg):
    return ("立刻退出" in msg) or ("PORT_DEAD" in msg) or ("超时" in msg)


def _find_profile_processes(profile_path):
    """找出命令行里带本程序配置目录的浏览器进程（不会误杀用户自己的浏览器）"""
    pids = []
    key = os.path.abspath(profile_path).lower()
    try:
        if sys.platform.startswith("win"):
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
                  "$_.CommandLine.ToLower().Contains('%s') } | "
                  "Select-Object -ExpandProperty ProcessId" % key)
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=25,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
        else:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        cmd = f.read().decode("utf-8", "replace").lower()
                except Exception:
                    continue
                if key in cmd:
                    pids.append(int(pid))
    except Exception:
        pass
    return pids


def _pid_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                                 capture_output=True, text=True, timeout=8,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _kill_pid(pid):
    try:
        pid = int(pid)
    except Exception:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.kill(pid, 15)
            for _ in range(10):
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except Exception:
                    return
            try:
                os.kill(pid, 9)
            except Exception:
                pass
    except Exception:
        pass


def diagnose(log=print):
    """逐项自检，返回报告行列表；用于界面上的『诊断』按钮。"""
    lines = []

    def say(s):
        lines.append(s)
        try:
            log(s)
        except Exception:
            pass

    say("===== 诊断开始 =====")
    say("系统: %s  Python %s" % (sys.platform, sys.version.split()[0]))
    try:
        exe = find_browser()
        say("浏览器: %s" % exe)
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            say("浏览器版本: %s" % ((out.stdout or out.stderr or "").strip() or "(无输出)"))
        except Exception as e:
            say("浏览器版本读取失败: %s" % e)
    except Exception as e:
        say("× 没找到浏览器: %s" % e)
        return lines
    prof = profile_dir()
    say("配置目录: %s" % prof)
    say("可写: %s" % (os.access(prof, os.W_OK)))
    marker = os.path.join(prof, "dy_engine.json")
    if os.path.exists(marker):
        try:
            m = json.load(open(marker, "r", encoding="utf-8"))
            alive = _pid_alive(m.get("pid"))
            say("上次浏览器记录: 端口 %s / PID %s / 进程存活=%s" % (m.get("port"), m.get("pid"), alive))
        except Exception as e:
            say("上次浏览器记录读取失败: %s" % e)
    else:
        say("上次浏览器记录: 无")
    try:
        p = _free_port()
        say("可用端口测试: OK（%d）" % p)
    except Exception as e:
        say("× 端口不可用: %s" % e)
    e = BrowserEngine(headless=(True if (not sys.platform.startswith("win")
                                         and not os.environ.get("DISPLAY")) else False))
    say("启动测试（端口 %d，最多 45 秒）…" % e.port)
    t0 = time.time()
    try:
        e.start()
        say("√ 浏览器启动成功，耗时 %.2f 秒" % (time.time() - t0))
        say("   调试端点: http://127.0.0.1:%d/json/version" % e.port)
        try:
            st = e.login_status(want_nickname=False)
            say("   登录状态: %s（cookie: %s）" % ("已登录" if st["logged_in"] else "未登录",
                                                ", ".join(st["cookies"]) or "无"))
        except Exception as ex:
            say("   登录状态检测失败: %s" % ex)
    except Exception as ex:
        say("× 浏览器启动失败（%.2f 秒后）: %s" % (time.time() - t0, ex))
    finally:
        try:
            e.kill()
        except Exception:
            pass
    say("===== 诊断结束 =====")
    return lines


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
