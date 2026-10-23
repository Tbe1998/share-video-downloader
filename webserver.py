#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机网页版服务：把桌面版的能力（浏览器签名解析 + 无水印下载）通过局域网 HTTP 暴露。

只用标准库 + 本工程的 engine/dycore。手机浏览器打开 `http://<电脑IP>:8990/?k=<口令>`
即可：粘贴链接 → 解析 → 选清晰度 → 下载到电脑 → 一键「保存到手机」。

设计要点：
- 解析/下载都在**运行本程序的那台电脑**上做，复用它的浏览器和登录态（4K 才可能拿到）。
- 单实例浏览器引擎：同一 profile 只能一个浏览器，全部请求串行复用同一个引擎。
- 口令 `k`：本机启动时随机生成，只有拿到链接的设备能调用接口（家里局域网防误用）。
"""
import json
import os
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote

import dycore
import engine as eng

try:                                   # 版本号唯一来源
    from version import VERSION as DEFAULT_APP_VERSION
except Exception:                      # 独立运行时的兜底
    DEFAULT_APP_VERSION = "0.0.0"

QUALITY_LABELS = [
    ("auto", "自动（最高可用）"),
    ("1080p", "1080P 超清"),
    ("720p", "720P 高清"),
    ("540p", "540P 标清"),
    ("480p", "480P 省流"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def lan_ip():
    """拿到本机对外的局域网 IP（不会真的发包）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def all_ips():
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    main = lan_ip()
    if main and main not in ips and not main.startswith("127."):
        ips.insert(0, main)
    return ips


class Hub:
    """全局状态：任务表 + 唯一浏览器引擎"""

    def __init__(self, out_dir, log=None, headless=True, headless_flag=None, engine_getter=None,
                 login_opener=None, login_checker=None):
        self.out_dir = out_dir
        self.log = log or (lambda s: None)
        self.headless = headless
        self.headless_flag = headless_flag          # 可调用对象：返回当前是否 headless
        self.engine_getter = engine_getter          # 可调用对象：返回 GUI 已开的引擎（登录窗口）
        self.login_opener = login_opener            # 可调用对象：让桌面端弹出登录浏览器
        self.login_checker = login_checker          # 可调用对象：返回桌面端的登录状态文案
        self.lock = threading.Lock()
        self.engine = None
        self.engine_lock = threading.Lock()
        self.jobs = {}
        self.covers = {}
        self.fail_streak = 0

    # ---------- 引擎 ----------
    def get_engine(self):
        with self.engine_lock:
            if self.engine is not None and self.engine.is_alive():
                return self.engine
            if callable(self.engine_getter):        # 优先复用桌面端已经打开的浏览器（含登录态）
                try:
                    shared = self.engine_getter()
                except Exception:
                    shared = None
                if shared is not None and shared.is_alive():
                    self.log("网页版复用桌面端已打开的浏览器")
                    self.engine = shared
                    return shared
            hl = self.headless
            if callable(self.headless_flag):
                try:
                    hl = bool(self.headless_flag())
                except Exception:
                    pass
            self.log("启动浏览器引擎（headless=%s）…" % hl)
            e = eng.BrowserEngine(headless=hl, log=self.log)
            e.start()
            self.engine = e
            return e

    def engine_kill(self):
        with self.engine_lock:
            if self.engine is not None:
                try:
                    self.engine.kill()
                except Exception:
                    pass
                self.engine = None

    # ---------- 任务 ----------
    def new_job(self, kind):
        jid = uuid.uuid4().hex[:12]
        with self.lock:
            self.jobs[jid] = {"id": jid, "kind": kind, "state": "running", "started": time.time(),
                              "progress": 0, "total": 0, "speed": 0, "msg": "", "error": "",
                              "meta": {}, "ratios": [], "mode": "", "path": "", "name": ""}
        return jid

    def job(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            return dict(j) if j else None

    def update(self, jid, **kw):
        with self.lock:
            if jid in self.jobs:
                self.jobs[jid].update(kw)

    def prune(self, max_jobs=60):
        with self.lock:
            if len(self.jobs) <= max_jobs:
                return
            old = sorted(self.jobs.values(), key=lambda j: j["started"])[:len(self.jobs) - max_jobs]
            for j in old:
                self.jobs.pop(j["id"], None)


# ---------------- 业务：解析 / 下载 ----------------
def do_parse(hub, jid, url):
    try:
        hub.update(jid, msg="解析分享链接 …")
        aweme_id = dycore.DouyinClient().resolve_id(url)
        hub.log("网页版解析：%s → %s" % (url, aweme_id))
        hub.update(jid, msg="打开作品页并等待浏览器完成签名 …", aweme_id=aweme_id)
        e = hub.get_engine()
        res = e.fetch_detail(aweme_id, timeout=45)
        if res.get("detail"):
            meta = eng.summarize(res["detail"])
            ratios = meta.get("ratios") or []
            mode = "detail"
        elif res.get("fallback_url"):
            pm = res.get("meta") or {}
            url_fb = res["fallback_url"]
            meta = {
                "aweme_id": aweme_id,
                "desc": (pm.get("desc") or pm.get("title") or "").strip(),
                "author": (pm.get("author") or "").strip(),
                "cover": pm.get("cover") or "",
                "duration": pm.get("duration") or 0,
                "width": None, "height": None,
            }
            ratios = [{"gear": "page", "width": 0, "height": 0, "size": 0,
                       "url": url_fb, "urls": [url_fb], "score": 0}]
            mode = "page"
        else:
            raise eng.EngineError("没拿到视频数据。可能需要在电脑上点『登录抖音』扫码后再试。")
        hub.update(jid, state="done", msg="解析完成", meta=meta, ratios=ratios, mode=mode)
        hub.fail_streak = 0
    except Exception as ex:
        hub.fail_streak += 1
        hub.update(jid, state="fail", error=str(ex), msg="解析失败")
        hub.log("网页版解析失败：%s" % ex)


def do_download(hub, jid, ratio, meta, quality):
    try:
        pick = eng.choose([ratio] if isinstance(ratio, dict) else ratio, quality)
        if not pick:
            raise eng.EngineError("找不到可下载的清晰度")
        hub.update(jid, msg="开始下载 %s …" % (("%sp" % pick.get("height")) if pick.get("height") else "页面流"))
        last = [0.0]

        def prog(got, total, speed):
            now = time.time()
            if now - last[0] > 0.4 or got >= total:
                last[0] = now
                hub.update(jid, progress=got, total=total or pick.get("size") or 0, speed=speed)

        path = eng.download_entry(pick, hub.out_dir, meta, progress=prog)
        hub.update(jid, state="done", path=path, name=os.path.basename(path),
                   progress=os.path.getsize(path), total=os.path.getsize(path),
                   msg="下载完成", height=pick.get("height") or 0)
        hub.log("网页版下载完成：%s" % path)
    except Exception as ex:
        hub.update(jid, state="fail", error=str(ex), msg="下载失败")
        hub.log("网页版下载失败：%s" % ex)


# ---------------- 网页 ----------------
PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f131b">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>抖音无水印下载器 · 手机版</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;padding:14px 14px 40px;background:#0f131b;color:#e8edf7;
 font-family:-apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;font-size:15px}
h1{font-size:17px;margin:4px 0 12px;display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;background:#2ecc71;box-shadow:0 0 8px #2ecc71}
.card{background:#171c26;border:1px solid #232a37;border-radius:14px;padding:13px;margin-bottom:12px}
textarea{width:100%;min-height:84px;background:#0c1017;color:#e8edf7;border:1px solid #262f3e;
 border-radius:10px;padding:11px;font-size:15px;resize:vertical}
button{background:#ff2d55;color:#fff;border:0;border-radius:10px;padding:13px 18px;font-size:16px;
 font-weight:600;width:100%;margin-top:10px}
button.ghost{background:#222a38;color:#cfd8e8}
button:disabled{opacity:.5}
.row{display:flex;gap:8px}.row>*{flex:1}
.meta{display:flex;gap:11px;align-items:flex-start}
.cover{width:78px;height:104px;border-radius:9px;object-fit:cover;background:#0c1017;flex:none}
.title{font-weight:600;line-height:1.35;margin-bottom:6px;word-break:break-all;
 display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.sub{color:#8b95a9;font-size:13px}
select{width:100%;background:#0c1017;color:#e8edf7;border:1px solid #262f3e;border-radius:10px;
 padding:12px;font-size:15px;margin-top:10px;appearance:none}
.bar{height:9px;background:#0c1017;border-radius:6px;overflow:hidden;margin-top:11px}
.bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,#ff2d55,#ff7a45);
 border-radius:6px;transition:width .25s}
#log{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#7d8798;white-space:pre-wrap;
 max-height:150px;overflow:auto;margin-top:8px}
.badge{display:inline-block;background:#222a38;color:#9fb0c9;border-radius:6px;padding:2px 7px;
 font-size:12px;margin-right:6px}
a.big{display:block;text-align:center;background:#1f8fff;color:#fff;text-decoration:none;
 border-radius:10px;padding:13px;font-weight:600;margin-top:10px}
.hide{display:none}
.err{color:#ff6b81}
.tip{color:#8b95a9;font-size:12.5px;line-height:1.6;margin-top:6px}
</style></head><body>
<h1><span class="dot"></span>抖音无水印下载器<span class="sub" id="ver"></span></h1>

<div class="card">
 <textarea id="url" placeholder="粘贴抖音分享链接或整段分享文字&#10;例如：https://v.douyin.com/xxxxxx/"></textarea>
 <div class="row"><button id="btnPaste" class="ghost" style="flex:.42">粘贴</button>
 <button id="btnParse" style="flex:1.2">解析</button></div>
</div>

<div class="card hide" id="card1">
 <div class="meta">
   <img class="cover" id="cover" alt="">
   <div style="flex:1;min-width:0">
     <div class="title" id="title">—</div>
     <div class="sub" id="sub"></div>
     <div class="sub" id="badges" style="margin-top:6px"></div>
   </div>
 </div>
 <select id="q"></select>
 <button id="btnDl">下载到电脑</button>
</div>

<div class="card hide" id="card2">
 <div class="sub" id="dlMsg">准备中…</div>
 <div class="bar"><i id="bar"></i></div>
 <a class="big hide" id="save" href="#">保存到手机</a>
 <button class="ghost hide" id="btnPlay">在手机里播放</button>
 <div id="log"></div>
</div>

<div class="card">
 <div class="sub" id="loginState" style="margin-bottom:8px;font-size:13px">登录状态：读取中…</div>
 <div class="row"><button class="ghost" id="btnLogin" style="flex:1">让电脑打开登录窗口</button>
 <button class="ghost" id="btnLoginCheck" style="flex:.7">刷新状态</button></div>
 <div class="tip">想要 1080P/4K 才需要登录。点左边会在<b>电脑上</b>弹出抖音登录页，用手机抖音扫码即可，登录态存在电脑里。</div>
</div>

<div class="card">
 <div class="sub">同一个 WiFi 下打开这个地址就能用；解析和下载都在电脑上完成，登录态用的是电脑上的抖音登录。</div>
 <div class="tip">电脑端功能齐全（含『登录抖音』『检查登录』『诊断』）；这里出问题请到电脑上看日志。</div>
</div>

<script>
const K = new URLSearchParams(location.search).get('k') || '';
const $ = id => document.getElementById(id);
const api = async (path, body) => {
  const r = await fetch(path + (path.includes('?') ? '&' : '?') + 'k=' + encodeURIComponent(K), {
    method: body ? 'POST' : 'GET',
    headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined
  });
  return await r.json();
};
let JOB = null, RATIOS = [], TIMER = null;

function setLog(s){ $('log').textContent = s; }
$('btnPaste').onclick = async () => {
  try { const t = await navigator.clipboard.readText(); if (t) $('url').value = t; }
  catch(e){ $('url').focus(); alert('系统不允许读剪贴板，请长按输入框粘贴'); }
};
$('btnParse').onclick = async () => {
  const u = $('url').value.trim();
  if (!u) { alert('请先粘贴抖音链接'); return; }
  $('btnParse').disabled = true; $('card1').classList.add('hide'); $('card2').classList.add('hide');
  setLog('解析中 …（首次会启动浏览器，稍慢）');
  try {
    const r = await api('/api/parse', {url: u});
    if (!r.ok) { setLog('解析失败：' + (r.error||'')); $('btnParse').disabled = false; return; }
    JOB = r.job; pollParse();
  } catch(e){ setLog('请求失败：' + e); }
  $('btnParse').disabled = false;
};

async function pollParse(){
  const r = await api('/api/job?id=' + JOB);
  const j = r.job || {};
  $('card2').classList.remove('hide');
  setLog(j.msg || '');
  if (j.state === 'running') { return setTimeout(pollParse, 900); }
  if (j.state === 'fail') { setLog('解析失败：' + (j.error||'')); return; }
  RATIOS = j.ratios || [];
  const m = j.meta || {};
  $('title').textContent = m.desc || '(无标题)';
  $('sub').textContent = [(m.author||'').trim(), m.duration ? (m.duration + 's') : '']
      .filter(x => x).join(' · ');
  $('badges').innerHTML = '<span class="badge">' + (j.mode === 'page' ? '页面流' : '码率列表') + '</span>'
     + '<span class="badge">作品 ' + (m.aweme_id||'') + '</span>';
  const cov = j.cover || m.cover || '';
  if (cov) { $('cover').src = '/cover?id=' + JOB + '&k=' + encodeURIComponent(K); $('cover').classList.remove('hide'); }
  else { $('cover').classList.add('hide'); }
  const opts = [];
  const seen = {};
  RATIOS.forEach((rt, i) => {
    const h = rt.height ? (rt.height + 'p') : '页面流';
    const sz = rt.size ? (' · ' + (rt.size/1048576).toFixed(1) + 'MB') : '';
    const key = h + (rt.gear||'');
    if (seen[key]) return; seen[key] = 1;
    opts.push('<option value="'+i+'">'+h+sz+(rt.gear? ' · '+rt.gear : '')+'</option>');
  });
  if (!opts.length) opts.push('<option value="0">页面流</option>');
  $('q').innerHTML = opts.join('');
  $('btnDl').textContent = '下载到电脑';
  $('card1').classList.remove('hide');
  $('card2').classList.add('hide');
  setLog('解析完成，选好清晰度点下载');
}

$('btnDl').onclick = async () => {
  const i = parseInt($('q').value || '0', 10);
  const ratio = RATIOS[i] || (RATIOS[0] || null);
  if (!ratio) { alert('没有可用的清晰度'); return; }
  $('card2').classList.remove('hide');
  $('save').classList.add('hide'); $('btnPlay').classList.add('hide');
  $('dlMsg').textContent = '开始下载 …';
  const r = await api('/api/download', {ratio: ratio, meta: (await api('/api/job?id=' + JOB)).job.meta});
  if (!r.ok) { $('dlMsg').textContent = '下载失败：' + (r.error||''); return; }
  JOB = r.job; pollDl();
};

async function pollDl(){
  const r = await api('/api/job?id=' + JOB);
  const j = r.job || {};
  const pct = j.total ? Math.min(100, j.progress*100/j.total) : 0;
  $('bar').style.width = pct.toFixed(1) + '%';
  $('dlMsg').textContent = (j.msg||'下载中') + '  ' + (j.progress/1048576).toFixed(1) + '/'
     + ((j.total||0)/1048576).toFixed(1) + ' MB' + (j.speed ? '  ' + (j.speed/1024).toFixed(0) + ' KB/s' : '');
  if (j.state === 'running') return setTimeout(pollDl, 700);
  if (j.state === 'fail') { $('dlMsg').innerHTML = '<span class="err">下载失败：' + (j.error||'') + '</span>'; return; }
  $('dlMsg').textContent = '已保存到电脑：' + j.name;
  setLog('');
  $('save').href = '/f?id=' + JOB + '&dl=1&k=' + encodeURIComponent(K);
  $('save').classList.remove('hide');
  $('btnPlay').onclick = () => window.open('/f?id=' + JOB + '&k=' + encodeURIComponent(K), '_blank');
  $('btnPlay').classList.remove('hide');
  $('card1').classList.remove('hide');
  $('btnDl').textContent = '换清晰度再下一次';
}

async function refreshLogin(){
  try {
    const r = await api('/api/login');
    $('loginState').textContent = '登录状态：' + (r.text || (r.ok? '未知' : (r.unsupported? '需在电脑上用图形界面版登录' : '读取失败')));
  } catch(e){ $('loginState').textContent = '登录状态：读取失败'; }
}
$('btnLoginCheck').onclick = refreshLogin;
$('btnLogin').onclick = async () => {
  const r = await api('/api/login', {});
  if (!r.ok) { alert(r.error || '无法打开登录窗口'); return; }
  alert(r.text + '\n在电脑屏幕上扫码，然后回来点『刷新状态』。');
  refreshLogin();
};
setTimeout(refreshLogin, 300);

(async () => {
  try { const r = await api('/api/ping'); $('ver').textContent = 'v' + (r.version||''); } catch(e){}
})();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "DyWeb/" + DEFAULT_APP_VERSION
    hub = None          # 由 Server 注入
    token = ""
    version = ""

    def log_message(self, fmt, *args):        # 静音默认访问日志
        pass

    # ---------- 工具 ----------
    def _send(self, code, data, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _auth(self, q):
        tok = self.token
        if not tok:
            return True
        return (q.get("k", [""])[0] or self.headers.get("X-Token") or "") == tok

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if not self._auth(q):
            return self._json({"ok": False, "error": "口令不对，请用电脑上显示的完整地址打开"}, 403)
        if p == "/api/ping":
            return self._json({"ok": True, "version": self.version, "jobs": len(self.hub.jobs)})
        if p == "/api/job":
            j = self.hub.job(q.get("id", [""])[0])
            if not j:
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            j.pop("path", None) if not q.get("full") else None
            if j.get("meta"):
                j["cover"] = j["meta"].get("cover") or ""
            j["version"] = self.version
            return self._json({"ok": True, "job": j})
        if p == "/cover":
            j = self.hub.job(q.get("id", [""])[0])
            cover = ((j or {}).get("meta") or {}).get("cover") or ""
            data = self.hub.covers.get(cover) or self._fetch_cover(cover)
            if not data:
                return self._send(404, b"", "image/jpeg")
            return self._send(200, data, "image/jpeg", {"Cache-Control": "max-age=3600"})
        if p == "/api/login":
            fn = self.hub.login_checker
            if not fn:
                return self._json({"ok": False, "unsupported": True,
                                   "text": "当前是命令行网页模式，登录请到电脑上用图形界面版"})
            try:
                return self._json({"ok": True, "text": str(fn())})
            except Exception as ex:
                return self._json({"ok": False, "text": "读取登录状态失败：%s" % ex})
        if p == "/api/history":
            items = []
            for j in sorted(self.hub.jobs.values(), key=lambda x: x["started"], reverse=True):
                if j.get("kind") == "download" and j.get("state") == "done":
                    items.append({"id": j["id"], "name": j.get("name"), "size": j.get("total") or 0,
                                  "time": time.strftime("%m-%d %H:%M", time.localtime(j["started"]))})
            return self._json({"ok": True, "items": items[:30]})
        if p == "/f":
            return self._serve_file(q)
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._auth(q):
            return self._json({"ok": False, "error": "口令不对"}, 403)
        body = self._body()
        if u.path == "/api/parse":
            url = (body.get("url") or "").strip()
            if not url:
                return self._json({"ok": False, "error": "请先粘贴抖音分享链接"}, 400)
            jid = self.hub.new_job("parse")
            threading.Thread(target=do_parse, args=(self.hub, jid, url), daemon=True).start()
            return self._json({"ok": True, "job": jid})
        if u.path == "/api/download":
            ratio = body.get("ratio") or {}
            meta = body.get("meta") or {}
            if not ratio:
                return self._json({"ok": False, "error": "缺少清晰度信息"}, 400)
            jid = self.hub.new_job("download")
            threading.Thread(target=do_download, args=(self.hub, jid, ratio, meta, body.get("quality") or "auto"),
                             daemon=True).start()
            return self._json({"ok": True, "job": jid})
        if u.path == "/api/login":
            fn = self.hub.login_opener
            if not fn:
                return self._json({"ok": False, "error": "当前是命令行网页模式，请在电脑图形界面上点『登录抖音』"})
            try:
                msg = fn() or "已在电脑上打开抖音登录窗口"
                return self._json({"ok": True, "text": msg})
            except Exception as ex:
                return self._json({"ok": False, "error": str(ex)})
        if u.path == "/api/shutdown":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        return self._json({"ok": False, "error": "not found"}, 404)

    # ---------- 封面/文件 ----------
    def _fetch_cover(self, url):
        if not url:
            return b""
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if len(data) < 64:
                return b""
            self.hub.covers[url] = data
            return data
        except Exception:
            return b""

    def _serve_file(self, q):
        j = self.hub.job(q.get("id", [""])[0])
        path = (j or {}).get("path") or ""
        if not path or not os.path.exists(path):
            return self._send(404, "文件不存在".encode(), "text/plain; charset=utf-8")
        size = os.path.getsize(path)
        name = (j.get("name") or os.path.basename(path))
        rng = self.headers.get("Range")
        ctype = "video/mp4"
        if q.get("dl"):
            dispo = "attachment; filename*=UTF-8''%s" % quote(name)
        else:
            dispo = "inline; filename*=UTF-8''%s" % quote(name)
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1) or 0) if m else 0
            end = int(m.group(2)) if (m and m.group(2)) else size - 1
            start, end = max(0, start), min(end, size - 1)
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(end - start + 1)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Disposition", dispo)
            self.end_headers()
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", dispo)
        self.end_headers()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1 << 16)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


class WebServer:
    def __init__(self, out_dir, port=8990, host="0.0.0.0", log=None,
                 version=DEFAULT_APP_VERSION,
                 headless=True, headless_flag=None, engine_getter=None,
                 login_opener=None, login_checker=None):
        self.hub = Hub(out_dir, log=log, headless=headless, headless_flag=headless_flag,
                       engine_getter=engine_getter, login_opener=login_opener,
                       login_checker=login_checker)
        self.port = port
        self.host = host
        self.token = uuid.uuid4().hex[:6]
        self.version = version or DEFAULT_APP_VERSION
        self.log = log or (lambda s: None)
        self.httpd = None
        self.thread = None

    # 地址
    def urls(self):
        port = self.httpd.server_address[1] if self.httpd else self.port
        return ["http://%s:%d/?k=%s" % (ip, port, self.token) for ip in all_ips()]

    def primary_url(self):
        port = self.httpd.server_address[1] if self.httpd else self.port
        return "http://%s:%d/?k=%s" % (lan_ip(), port, self.token)

    def start(self):
        handler = type("BoundHandler", (Handler,), {
            "hub": self.hub, "token": self.token, "version": self.version})
        last = None
        for port in [self.port] + list(range(self.port + 1, self.port + 12)):
            try:
                self.httpd = ThreadingHTTPServer((self.host, port), handler)
                break
            except OSError as e:
                last = e
                continue
        if not self.httpd:
            raise RuntimeError("端口都被占用：%s" % last)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.log("手机网页版已启动：%s" % self.primary_url())
        return self.primary_url()

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        self.hub.engine_kill()
