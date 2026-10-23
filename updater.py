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
import time
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
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    cand = os.path.abspath(sys.argv[0] or "")
    if cand and os.path.isfile(cand):
        return cand
    return os.path.abspath(__file__)          # 用 python - 之类跑时兜底


def can_self_update():
    return bool(getattr(sys, "frozen", False)) and sys.platform.startswith("win")


def staged_path(version=""):
    """更新包下载到哪：临时目录（不污染安装目录，也不需要管理员权限）"""
    name = "DouyinDownloader-new"
    if version:
        name += "-v" + str(version).lstrip("vV")
    return os.path.join(tempfile.gettempdir(), name + ".exe")


def log_path():
    base = os.environ.get("APPDATA") or tempfile.gettempdir()
    return os.path.join(base, "DouyinDownloader", "update.log")


def ulog(msg):
    """更新过程写日志，出问题让用户直接发这一行文件"""
    try:
        p = log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _msgbox(title, text):
    """打包成 --windowed 时没有控制台，失败要让用户看见"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x10)
    except Exception:
        print("%s: %s" % (title, text))


def wait_pid_exit(pid, timeout=90):
    """等指定进程退出。Windows 用 WaitForSingleObject（按 PID 精确，绝不误判）"""
    if pid <= 0:
        return True
    if sys.platform.startswith("win"):
        try:
            import ctypes
            SYNCHRONIZE = 0x00100000
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not h:
                return True                      # 打不开=已经没了
            try:
                rc = k32.WaitForSingleObject(h, int(timeout * 1000))
                return rc == 0                   # 0 = WAIT_OBJECT_0 已退出
            finally:
                k32.CloseHandle(h)
        except Exception as ex:
            ulog("wait_pid_exit 失败，改用轮询：%s" % ex)
    def _gone(p):
        stat = "/proc/%d/stat" % p
        if os.path.exists(stat):                 # Linux：僵尸态也算退出
            try:
                with open(stat) as f:
                    st = f.read().split(") ", 1)[-1]
                if st[:1] in ("Z", "X"):
                    return True
            except Exception:
                pass
            return False
        try:
            os.kill(int(pid), 0)
            return False
        except OSError:
            return True
        except Exception:
            return True

    t0 = time.time()
    while time.time() - t0 < timeout:
        if _gone(int(pid)):
            return True
        time.sleep(0.5)
    return False


def _wait_unlock(path, timeout=90):
    """等旧 exe 被释放（Windows 下运行中的 exe 无法写入）"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if not os.path.exists(path):
                return True
            with open(path, "r+b"):
                pass
            return True
        except (PermissionError, OSError):
            time.sleep(0.5)
    return False


def install_update(old_exe=None, old_pid=0, launch=True):
    """★ 由**新下载的 exe** 调用的安装流程：等旧进程退出 → 备份 → 覆盖 → 启动新版。

    比 .cmd 脚本可靠：不用 tasklist 猜 PID，不受控制台编码/中文路径影响。
    """
    old_exe = old_exe or ""
    me = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
    ulog("=" * 50)
    ulog("开始安装更新：新文件=%s  旧文件=%s  旧PID=%s" % (me, old_exe, old_pid))
    if not old_exe or not os.path.isfile(old_exe):
        ulog("× 旧程序路径无效：%r" % old_exe)
        _msgbox("更新失败", "找不到原来的程序文件：\n%s\n\n请手动把新下载的 exe 覆盖过去。" % old_exe)
        return 2
    if os.path.normcase(me) == os.path.normcase(old_exe):
        ulog("× 新文件与旧文件相同，放弃")
        return 3
    if not wait_pid_exit(old_pid, 90):
        ulog("× 等旧进程退出超时（PID %s 仍在运行）" % old_pid)
        _msgbox("更新失败", "旧版程序没有退出，更新已中止。\n请关掉程序后重试，或手动覆盖。")
        return 4
    ulog("旧进程已退出")
    if not _wait_unlock(old_exe, 60):
        ulog("× 旧文件仍被占用")
        _msgbox("更新失败", "原程序文件被占用（可能被杀毒软件扫描中）。\n稍等一会再点『检查更新』，或手动覆盖。")
        return 5
    # 备份一份，出问题能退回
    try:
        bak = old_exe + ".bak"
        if os.path.exists(bak):
            os.remove(bak)
        os.replace(old_exe, bak)
        ulog("已备份旧版到 %s" % bak)
    except Exception as ex:
        ulog("备份失败（继续尝试直接覆盖）：%s" % ex)
    try:
        import shutil
        shutil.copy2(me, old_exe)
        ulog("新版本已写入 %s（%d 字节）" % (old_exe, os.path.getsize(old_exe)))
    except Exception as ex:
        ulog("× 写入失败：%s" % ex)
        # 尽量把备份还原回去，别让用户没程序可用
        try:
            if os.path.exists(old_exe + ".bak") and not os.path.exists(old_exe):
                os.replace(old_exe + ".bak", old_exe)
        except Exception:
            pass
        _msgbox("更新失败", "写入新版本失败：\n%s\n\n可能被杀毒软件拦截。请手动覆盖。" % ex)
        return 6
    if launch:
        try:
            flags = 0
            for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
                flags |= getattr(subprocess, name, 0)
            subprocess.Popen([old_exe], close_fds=True, creationflags=flags,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ulog("已启动新版程序")
        except Exception as ex:
            ulog("× 启动新版失败：%s" % ex)
            _msgbox("更新完成", "新版本已装好，但自动启动失败。\n请手动双击程序打开。")
            return 7
    ulog("安装流程完成")
    return 0


def apply_update_windows(new_exe, exe_path=None):
    """启动「新 exe 自安装」；失败再退回 .cmd 脚本方案。"""
    exe_path = exe_path or current_exe()
    pid = os.getpid()
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    # 方案 1（首选）：让新下载的 exe 自己等旧进程退出后覆盖
    try:
        subprocess.Popen([new_exe, "--install-update", "--old-exe", exe_path,
                          "--old-pid", str(pid)], close_fds=True, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ulog("已启动新版自安装进程：%s" % new_exe)
        return "installer"
    except Exception as ex:
        ulog("启动自安装失败，改用 .cmd 兜底：%s" % ex)
    # 方案 2（兜底）：.cmd 脚本。PID 匹配加次数上限，避免像旧版那样死等
    fd, script = tempfile.mkstemp(suffix=".cmd", prefix="dy_update_")
    os.close(fd)
    body = """@echo off
setlocal enabledelayedexpansion
chcp 936 >nul
cd /d "{exe_dir}"
set /a n=0
:wait
tasklist /NH /FI "PID eq {pid}" | find "{pid}" >nul
if errorlevel 1 goto done
set /a n+=1
if !n! GEQ 120 goto done
timeout /t 1 /nobreak >nul
goto wait
:done
move /y "{new}" "{exe}" >nul 2>&1
start "" "{exe}"
del "%~f0" >nul 2>&1
""".format(exe_dir=(os.path.dirname(exe_path) or "."), pid=pid,
           new=new_exe.replace("/", "\\"), exe=exe_path.replace("/", "\\"))
    with open(script, "w", encoding="gbk", errors="replace") as f:
        f.write(body)
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


def cleanup_staged(keep=""):
    """清掉临时目录里上一次更新留下的 exe（当前正在用的那个除外）"""
    removed = 0
    try:
        d = tempfile.gettempdir()
        for fn in os.listdir(d):
            if not fn.startswith("DouyinDownloader-new"):
                continue
            fp = os.path.join(d, fn)
            if keep and os.path.normcase(fp) == os.path.normcase(keep):
                continue
            try:
                os.remove(fp)
                removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed
