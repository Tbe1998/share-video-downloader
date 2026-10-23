#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多平台支持：抖音走本机浏览器签名（本工程原有链路），其它平台走 yt-dlp。

- `detect(url)`          判断该用哪条链路
- `probe(url)`           取标题/封面/时长/可选清晰度（统一成 ratios 结构，界面直接用）
- `download(url, ratio)` 下载到本地，返回文件路径

yt-dlp 内置 1000+ 站点（B站/快手/小红书/微博/X/Instagram/TikTok…）。
它不需要单独的账号，但有些站点要 cookie，能把浏览器 cookie 传给它。
没有 ffmpeg 时只能选“已经混好的单文件流”（清晰度往往到 720p）。
"""
import os
import shutil
import time
import urllib.request

DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com", "amemv.com", "douyinpic.com")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def has_ffmpeg():
    return bool(shutil.which("ffmpeg"))


def _ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except Exception:
        return None


def available():
    return _ytdlp() is not None


def version():
    m = _ytdlp()
    try:
        return m.version.__version__ if m else ""
    except Exception:
        return ""


def is_douyin(url):
    u = (url or "").lower()
    return any(h in u for h in DOUYIN_HOSTS)


def detect(url):
    """返回 'douyin' 或 'ytdlp'"""
    return "douyin" if is_douyin(url) else "ytdlp"


def _base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 3,
        "user_agent": UA,
        "nocheckcertificate": True,
        "extractor_args": {"youtube": {"player_client": ["web_safari", "android"]}},
    }


def _pick_formats(info, limit=12):
    """把 yt-dlp 的 formats 归并成“每个高度一档”"""
    fmts = info.get("formats") or []
    ff = has_ffmpeg()
    by_h = {}
    for f in fmts:
        if f.get("vcodec") in (None, "none"):
            continue
        if f.get("acodec") in (None, "none") and ff:
            pass                                    # 纯视频流，需要 ffmpeg 合音轨
        elif f.get("acodec") in (None, "none") and not ff:
            continue                                # 没 ffmpeg → 只能要带音轨的
        h = f.get("height") or 0
        ext = (f.get("ext") or "").lower()
        size = f.get("filesize") or f.get("filesize_approx") or 0
        merged = f.get("acodec") not in (None, "none")
        score = (h, 1 if merged else 0, 1 if ext == "mp4" else 0)
        cur = by_h.get(h)
        if cur is None or score > cur["_score"]:
            fid = f.get("format_id") or ""
            if not merged:
                fid = "%s+bestaudio/best" % fid
            by_h[h] = {
                "gear": (f.get("format_note") or f.get("format") or ext or "视频")[:18],
                "width": f.get("width") or 0,
                "height": h,
                "size": size,
                "ext": ext or "mp4",
                "url": f.get("url") or "",
                "urls": [],
                "fmt": fid,
                "engine": "ytdlp",
                "score": h,
                "_score": score,
            }
    out = sorted(by_h.values(), key=lambda x: (x["height"] or 0), reverse=True)
    for x in out:
        x.pop("_score", None)
    return out[:limit]


def probe(url, timeout=60):
    """取视频信息，返回 {meta, ratios, mode, platform}"""
    m = _ytdlp()
    if not m:
        raise RuntimeError("没有内置 yt-dlp，无法解析该平台链接（重装程序可解决）")
    opts = _base_opts()
    with m.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]
    ratios = _pick_formats(info)
    meta = {
        "aweme_id": str(info.get("id") or ""),
        "desc": (info.get("title") or info.get("description") or "").strip()[:120],
        "author": (info.get("uploader") or info.get("channel") or info.get("creator") or "").strip(),
        "cover": info.get("thumbnail") or "",
        "duration": round(float(info.get("duration") or 0), 1),
        "width": info.get("width"), "height": info.get("height"),
        "platform": info.get("extractor_key") or info.get("extractor") or "其它平台",
        "webpage": info.get("webpage_url") or url,
    }
    if not ratios:                                # 极少见：只有一个直链
        if info.get("url"):
            ratios = [{"gear": "直链", "width": 0, "height": 0, "size": 0, "ext": "mp4",
                       "url": info["url"], "urls": [], "fmt": "best", "engine": "ytdlp", "score": 0}]
        else:
            raise RuntimeError("这个链接没有找到可下载的视频流")
    return {"meta": meta, "ratios": ratios, "mode": "ytdlp",
            "platform": meta["platform"]}


def _progress_hook(cb, should_stop):
    def hook(d):
        if should_stop and should_stop():
            raise RuntimeError("已取消")
        try:
            if d.get("status") == "downloading":
                got = int(d.get("downloaded_bytes") or 0)
                total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
                speed = float(d.get("speed") or 0)
                if cb:
                    cb(got, total, speed)
            elif d.get("status") == "finished" and cb:
                total = int(d.get("total_bytes") or got if False else int(d.get("total_bytes") or 0))
                cb(total or int(d.get("downloaded_bytes") or 0), total, 0.0)
        except Exception:
            pass
    return hook


def download(url, ratio, out_dir, progress=None, should_stop=None):
    """下载选中清晰度，返回最终文件路径"""
    m = _ytdlp()
    if not m:
        raise RuntimeError("没有内置 yt-dlp，无法下载该平台视频")
    os.makedirs(out_dir, exist_ok=True)
    fmt = (ratio or {}).get("fmt") or "best"
    if not has_ffmpeg() and "+" in fmt:
        fmt = "b[ext=mp4]/b"                       # 没 ffmpeg 就别指望合流
    opts = _base_opts()
    opts.update({
        "format": fmt,
        "outtmpl": os.path.join(out_dir, "%(title).60s_%(height)sp_%(id)s.%(ext)s"),
        "progress_hooks": [_progress_hook(progress, should_stop)],
        "windowsfilenames": False,
        "restrictfilenames": False,
        "overwrites": True,
        "merge_output_format": "mp4" if has_ffmpeg() else None,
    })
    if not has_ffmpeg():
        opts.pop("merge_output_format", None)
    with m.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    # 合并后扩展名可能变
    for cand in (path, os.path.splitext(path)[0] + ".mp4", os.path.splitext(path)[0] + ".mkv"):
        if os.path.exists(cand):
            return cand
    for fn in os.listdir(out_dir):                 # 兜底：找最新的
        p = os.path.join(out_dir, fn)
        if os.path.isfile(p) and os.path.getmtime(p) > time.time() - 600:
            return p
    raise RuntimeError("下载完成但找不到输出文件")


def fetch_bytes(url, referer="", timeout=20):
    """取封面等小资源（部分站点禁盗链，带 Referer）"""
    try:
        hdr = {"User-Agent": UA}
        if referer:
            hdr["Referer"] = referer
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return b""
