#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态（cookies）持久化：桌面版与独立网页版共用。

首次运行读一次浏览器里的登录 cookie，把关键字段存到本地；之后启动直接复用，
不必每次都去浏览器里重新读，也能在换浏览器/清空 profile 后恢复登录。

只依赖标准库。文件默认 600 权限（Windows 上放进用户 APPDATA 目录）。
注意：这些 cookie 等同于账号登录凭据，**不要外发、不要贴到任何网上**。
"""
import json
import os
import stat
import sys
import time

APP_NAME = "DouyinDownloader"

# 用户点名要保存的字段 + 抖音常见配套字段（有就一起存，没有就跳过）
KEY_COOKIES = (
    "sessionid", "sessionid_ss", "sid_tt", "uid_tt", "ttwid",
    "passport_csrf_token", "passport_csrf_token_default",
    "sid_guard", "uid_tt_ss", "sessionid_ss_1", "n_mh",
)
AUTH_KEYS = ("sessionid", "sessionid_ss", "sid_tt")


def data_dir():
    """配置/凭据存放目录（桌面版和独立网页版共用同一个目录）"""
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(root, APP_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def store_path():
    return os.path.join(data_dir(), "cookies.json")


def _tighten(path):
    try:
        if not sys.platform.startswith("win"):
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)      # 600
        else:
            import subprocess
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["icacls", path, "/inheritance:r", "/grant:r",
                            "%USERNAME%:F"], capture_output=True, creationflags=flags, timeout=10)
    except Exception:
        pass


def _norm(cookies):
    """接受 list[dict]（CDP 原样）或 dict{name:value}，统一成 {name: {value, domain, ...}}"""
    out = {}
    if isinstance(cookies, dict):
        for k, v in cookies.items():
            if k in KEY_COOKIES:
                out[k] = {"name": k, "value": str(v), "domain": ".douyin.com", "path": "/"}
        return out
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        n = c.get("name")
        if n not in KEY_COOKIES:
            continue
        dom = c.get("domain") or ".douyin.com"
        if "douyin" not in dom:            # 只要抖音域下的
            continue
        out[n] = {
            "name": n,
            "value": c.get("value") or "",
            "domain": dom,
            "path": c.get("path") or "/",
            "expires": c.get("expires") or -1,
            "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")),
            "sameSite": c.get("sameSite") or "Lax",
        }
    return {k: v for k, v in out.items() if v.get("value")}


def save(cookies, extra=None):
    """保存关键 cookie。返回保存了多少个字段。"""
    data = _norm(cookies)
    if not data:
        return 0
    old = {}
    try:
        with open(store_path(), "r", encoding="utf-8") as f:
            old = (json.load(f) or {}).get("cookies") or {}
    except Exception:
        old = {}
    # 新值优先，缺的字段用旧的补（比如这次只拿到 ttwid）
    merged = dict(old)
    merged.update(data)
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": int(time.time()),
        "logged_in": any(k in merged for k in AUTH_KEYS),
        "keys": sorted(merged.keys()),
        "cookies": merged,
        "extra": extra or {},
    }
    p = store_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        _tighten(p)
    except Exception:
        return 0
    return len(merged)


def load():
    """读回保存的 cookie（dict{name: cookie_dict}），没有则返回 {}"""
    try:
        with open(store_path(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return d.get("cookies") or {}
    except Exception:
        return {}


def info():
    """给界面/日志看的摘要（不含 cookie 值）"""
    try:
        with open(store_path(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        return {"exists": False, "keys": [], "logged_in": False, "age_days": None, "updated_at": ""}
    ts = int(d.get("ts") or 0)
    return {
        "exists": True,
        "keys": d.get("keys") or sorted((d.get("cookies") or {}).keys()),
        "logged_in": bool(d.get("logged_in")),
        "updated_at": d.get("updated_at") or "",
        "age_days": round((time.time() - ts) / 86400.0, 1) if ts else None,
        "path": store_path(),
    }


def has_login(cookies=None):
    c = cookies if cookies is not None else load()
    return any(k in c for k in AUTH_KEYS)


def as_header(cookies=None):
    """拼成 Cookie 请求头值（给拿直链的 HTTP 请求用）"""
    c = cookies if cookies is not None else load()
    return "; ".join("%s=%s" % (k, v.get("value", "")) for k, v in c.items() if v.get("value"))


def export_dict(cookies=None):
    """{name: value} 形式，方便传给浏览器注入"""
    c = cookies if cookies is not None else load()
    return {k: v.get("value", "") for k, v in c.items() if v.get("value")}


def clear():
    try:
        os.remove(store_path())
        return True
    except Exception:
        return False


def mask(cookies=None):
    """日志里显示的脱敏形式：只显示前 4 位"""
    c = cookies if cookies is not None else load()
    return {k: (v.get("value", "")[:4] + "…") for k, v in c.items()}
