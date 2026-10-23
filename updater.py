#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动更新模块（多源版）：NAS 镜像优先 → 公网镜像 → GitHub / GitHub 加速镜像。

只依赖标准库。Windows 下用临时 .cmd 完成"等自己退出→替换→启动新版本"。

更新源顺序（可在 config.json 的 update_manifest 用逗号自定义，留空用默认）：
  1. http://192.168.1.6/download/douyin/latest.json      ← 局域网，最快（家里）
  2. https://www.topbei.cn/download/douyin/latest.json   ← 公网 HTTPS（外面也能用）
  3. https://api.github.com/.../releases/latest          ← 直连 GitHub（可能被墙）
  下载 exe 时若失败，会自动尝试 GitHub 加速镜像（ghfast.top / gh-proxy.com / ghproxy.net）。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

try:                                    # 仓库名唯一来源
    from version import REPO
except Exception:
    REPO = "Tbe1998/douyin-downloader"
UA = "DouyinDownloader-Updater"
GH_API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
GH_RELEASES = "https://github.com/%s/releases/latest" % REPO

DEFAULT_MANIFESTS = [
    "http://192.168.1.6/download/douyin/latest.json",
    "https://www.topbei.cn/download/douyin/latest.json",
]
GH_PROXIES = [
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
]


class UpdateError(RuntimeError):
    pass


# ---------------- 版本 ----------------
def parse_version(text):
    t = (text or "").strip().lstrip("vV")
    parts = []
    for seg in t.replace("-", ".").split("."):
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


# ---------------- HTTP ----------------
def _get(url, timeout=10, as_json=True):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/vnd.github+json, application/json, */*",
        "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise UpdateError("HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise UpdateError("连不上（%s）" % (getattr(e, "reason", e),))
    except Exception as e:
        raise UpdateError(str(e))
    if not as_json:
        return raw
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise UpdateError("返回内容不是 JSON")


def manifest_urls(custom=None):
    if custom:
        urls = [u.strip() for u in custom.replace(";", ",").split(",") if u.strip()]
        if urls:
            return urls
    return list(DEFAULT_MANIFESTS)


def _read_manifest(url, timeout=10):
    """读 NAS 镜像上的 latest.json"""
    data = _get(url, timeout=timeout)
    ver = str(data.get("version") or "").lstrip("vV")
    exe_rel = data.get("exe") or data.get("filename") or ""
    if not ver or not exe_rel:
        raise UpdateError("清单缺字段")
    exe_url = urllib.parse.urljoin(url, exe_rel)
    cands = [exe_url]
    return {
        "version": ver,
        "tag": data.get("tag") or ("v" + ver),
        "notes": (data.get("notes") or "").strip(),
        "size": int(data.get("size") or 0),
        "digest": "sha256:" + (data.get("sha256") or "").strip() if data.get("sha256") else "",
        "candidates": cands,
        "page": data.get("page") or GH_RELEASES,
    }


def _read_github(timeout=12):
    """直连 GitHub API（国内常被墙）"""
    data = _get(GH_API_LATEST, timeout=timeout)
    tag = data.get("tag_name") or ""
    asset_url, size, digest = "", 0, ""
    for a in data.get("assets") or []:
        if (a.get("name") or "").lower().endswith(".exe"):
            asset_url = a.get("browser_download_url") or ""
            size = a.get("size") or 0
            digest = a.get("digest") or ""
            break
    if not asset_url:
        raise UpdateError("Release 里没有 exe 资产")
    cands = [asset_url] + [p + asset_url for p in GH_PROXIES]
    return {
        "version": tag.lstrip("vV"), "tag": tag,
        "notes": (data.get("body") or "").strip(),
        "size": size, "digest": digest,
        "candidates": cands,
        "page": data.get("html_url") or GH_RELEASES,
    }


def check_latest(custom_manifest=None, timeout=8, allow_github=True):
    """按顺序试更新源；返回 info，含 source / candidates / version / notes / size / digest"""
    errors = []
    for m in manifest_urls(custom_manifest):
        host = urllib.parse.urlparse(m).netloc or m
        try:
            info = _read_manifest(m, timeout=timeout)
            info["source"] = "镜像 " + host
            # 镜像上也挂 GitHub 兜底地址（万一镜像中途挂了）
            gh = _github_candidates(info["version"], quiet=True)
            info["candidates"] = info["candidates"] + gh
            return info
        except Exception as e:
            errors.append("%s → %s" % (host, e))
    if allow_github:
        try:
            info = _read_github(timeout=max(timeout, 12))
            info["source"] = "GitHub（直连）"
            return info
        except Exception as e:
            errors.append("GitHub → %s" % e)
    raise UpdateError("所有更新源都连不上：\n" + "\n".join("  · " + x for x in errors))


def _github_candidates(version=None, quiet=True):
    """尽力拿到与版本对应的 GitHub 资产直链 + 加速镜像地址（拿不到就返回空）"""
    try:
        data = _get(GH_API_LATEST, timeout=6)
    except Exception:
        return []
    if version and str(data.get("tag_name") or "").lstrip("vV") != str(version).lstrip("vV"):
        return []
    for a in data.get("assets") or []:
        if (a.get("name") or "").lower().endswith(".exe"):
            u = a["browser_download_url"]
            return [u] + [p + u for p in GH_PROXIES]
    return []


# ---------------- 下载 ----------------
def download(url, dest, progress=None, timeout=60):
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    h = hashlib.sha256()
    got = 0
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
    except Exception as e:
        try:
            os.path.exists(tmp) and os.remove(tmp)
        except OSError:
            pass
        raise UpdateError("下载失败：%s" % e)
    if got < 1 << 20:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise UpdateError("下载文件过小（%d 字节）" % got)
    os.replace(tmp, dest)
    return got, h.hexdigest()


def download_best(candidates, dest, progress=None, digest="", on_try=None):
    """依次尝试多个下载地址；返回 (字节, sha256, 用的地址)"""
    want = (digest or "").strip()
    if want.lower().startswith("sha256:"):
        want = want[7:].lower()
    errors = []
    for url in candidates or []:
        host = urllib.parse.urlparse(url).netloc
        if on_try:
            on_try(host)
        try:
            size, sha = download(url, dest, progress=progress)
            if want and sha.lower() != want:
                errors.append("%s → 校验不一致" % host)
                continue
            return size, sha, url
        except Exception as e:
            errors.append("%s → %s" % (host, e))
    raise UpdateError("更新包下载失败：\n" + "\n".join("  · " + x for x in errors))


# ---------------- 自我替换 ----------------
def current_exe():
    return os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])


def can_self_update():
    return bool(getattr(sys, "frozen", False)) and sys.platform.startswith("win")


def apply_update_windows(new_exe, exe_path=None):
    exe_path = exe_path or current_exe()
    pid = os.getpid()
    fd, script = tempfile.mkstemp(suffix=".cmd", prefix="dy_update_")
    os.close(fd)
    body = """@echo off
chcp 65001 >nul
cd /d "{exe_dir}"
:wait
tasklist /FI "PID eq {pid}" | findstr /I "{pid}" >nul
if not errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto wait
)
move /y "{new}" "{exe}" >nul
if errorlevel 1 (
  del /q "{new}" >nul 2>&1
  start "" "{exe}"
  exit /b 1
)
start "" "{exe}"
del "%~f0" >nul 2>&1
""".format(exe_dir=(os.path.dirname(exe_path) or "."), pid=pid,
           new=new_exe.replace("/", "\\"), exe=exe_path.replace("/", "\\"))
    with open(script, "w", encoding="gbk", errors="replace") as f:
        f.write(body)
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(["cmd", "/c", script], close_fds=True, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return script


def open_page(url):
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
        return True
    except Exception:
        return False
