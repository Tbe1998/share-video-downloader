#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动更新模块：查 GitHub Release → 下载新 exe → 校验 → 替换自身并重启。

只依赖标准库。Windows 下用临时 .cmd 脚本完成"等自己退出→替换→启动新版本"。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

REPO = "Tbe1998/douyin-downloader"
API_BASE = "https://api.github.com"
UA = "DouyinDownloader-Updater"


class UpdateError(RuntimeError):
    pass


def parse_version(text):
    """'v2.5.1' / '2.5' → (2,5,1)；解析失败返回 (0,0,0)"""
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


def _get(url, timeout=20, as_json=True):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("仓库或 Release 不存在（HTTP 404）")
        raise UpdateError("请求失败（HTTP %s）" % e.code)
    except urllib.error.URLError as e:
        raise UpdateError("网络不通：%s" % e.reason)
    except Exception as e:
        raise UpdateError("请求异常：%s" % e)
    if not as_json:
        return raw
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise UpdateError("返回内容不是合法 JSON")


def check_latest(timeout=20):
    """返回 {version, tag, notes, url, size, digest, published}"""
    data = _get("%s/repos/%s/releases/latest" % (API_BASE, REPO), timeout=timeout)
    tag = data.get("tag_name") or ""
    asset_url, size, digest = "", 0, ""
    for a in data.get("assets") or []:
        if (a.get("name") or "").lower().endswith(".exe"):
            asset_url = a.get("browser_download_url") or ""
            size = a.get("size") or 0
            digest = a.get("digest") or ""
            break
    if not asset_url:
        raise UpdateError("最新 Release 里没有 exe 资产")
    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "notes": (data.get("body") or "").strip(),
        "url": asset_url,
        "size": size,
        "digest": digest,
        "published": data.get("published_at") or "",
        "page": data.get("html_url") or "",
    }


def download(url, dest, progress=None, timeout=60):
    """下载到 dest，返回 (字节数, sha256)"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    h = hashlib.sha256()
    got = 0
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    tmp = dest + ".part"
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
        raise UpdateError("下载文件过小（%d 字节），可能是错误页" % got)
    os.replace(tmp, dest)
    return got, h.hexdigest()


def current_exe():
    """当前可执行文件路径（PyInstaller 打包后即 exe 本身）"""
    return os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])


def can_self_update():
    return bool(getattr(sys, "frozen", False)) and sys.platform.startswith("win")


def apply_update_windows(new_exe, exe_path=None):
    """生成并启动替换脚本：等自己退出 → 覆盖 exe → 启动新版本"""
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
