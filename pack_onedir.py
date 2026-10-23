#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 PyInstaller 的 onedir 产物组装成“完整文件夹 + zip”（桌面版 + 独立网页版）。

onedir 目录结构（PyInstaller 6）：
  dist/ShareVideoDownloader/ShareVideoDownloader.exe
  dist/ShareVideoDownloader/_internal/...        ← 所有运行库

输出：
  out/分享视频下载器-v<ver>.zip      内部目录 分享视频下载器/（exe 已改中文名）
  out/分享视频网页版-v<ver>.zip      内部目录 分享视频网页版/
  另外把解压后的文件夹也留在 out/ 下，方便本机验证
"""
import argparse
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import version as vmod
    EXE_D, EXE_W = vmod.EXE_NAME, vmod.EXE_NAME_WEB
    NAME_D, NAME_W = vmod.APP_TITLE, vmod.APP_TITLE_WEB
except Exception:
    EXE_D, EXE_W, NAME_D, NAME_W = "ShareVideoDownloader", "ShareVideoWeb", "分享视频下载器", "分享视频网页版"

DESKTOP = {
    "src": EXE_D,
    "exe": EXE_D + ".exe",
    "name": NAME_D,
    "readme": """分享视频下载器 · 使用说明

【怎么用】
双击 分享视频下载器.exe。把抖音分享链接或整段分享文字粘到输入框，点「解析」，
选清晰度后点「下载」即可。文件默认存到你的下载目录（可改）。

【登录（可选，想要 1080P/4K 才需要）】
点右上角「登录抖音」→ 在弹出的浏览器窗口里用抖音 App 扫码。
首次检测到登录后，程序会自动把 sessionid 等登录 cookie 保存到
程序文件夹里的 cookies.json（就在本程序旁边），之后启动直接复用，不用再扫码。

【手机也能用】
点「手机网页版」会显示二维码，手机连同一个 WiFi 扫码即可使用；
详细用法见同目录的 使用说明.txt 里的手机部分。

【怎么更新】
点右上角「检查更新」，有新版会自动下载整包并替换、自动重启。
国内走自建镜像，不依赖 GitHub。

【出问题了】
1. 连不上/解析失败：先点「检查更新」，可能新版已经修好了
2. 浏览器启动失败/超时：点「诊断」，把日志发给助手
3. 更新出问题：%APPDATA%\\ShareVideoDownloader\\update.log（仅日志，登录态不再放这里）
""",
}

WEB = {
    "src": EXE_W,
    "exe": EXE_W + ".exe",
    "name": NAME_W,
    "readme": """分享视频网页版 · 独立版（不依赖桌面版）

【怎么用】
1. 双击 分享视频网页版.exe（会弹一个黑窗口，别关它）
2. 窗口里会显示手机访问地址，形如 http://192.168.x.x:8990/?k=xxxxxx
3. 手机连同一个 WiFi，浏览器打开这个地址即可用

固定端口/口令（手机收藏夹地址就不变了）：
  分享视频网页版.exe --port 8990 --token 你的口令 --out D:\\视频

【登录（想要 1080P/4K 才需要）】
两种方式：
  A. 在桌面版里登录一次，然后把程序文件夹里的 cookies.json 的内容
     粘到手机页面的「导入桌面版登录态」里
  B. 程序会在启动时自动读取上面那个 cookies.json 并注入

【开机自启】
把 分享视频网页版.exe 的快捷方式放进：
  Win+R 输入 shell:startup 打开的文件夹
""",
}


def _fix_stdout():
    """Windows 重定向输出是 cp1252，打中文会崩；只在非控制台时改成 utf-8"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or not hasattr(stream, "reconfigure"):
                continue
            try:
                if stream.isatty():
                    continue
            except Exception:
                pass
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def strip_tells(folder):
    """清掉一眼就能看出“这是 Python 打包的”的残留文件"""
    import glob
    removed = 0
    pats = ["**/__pycache__", "**/*.pyi", "**/*.pdb", "**/*.py", "**/*.pyc",
            "**/*.dist-info", "**/*.egg-info", "**/README*", "**/LICENSE*"]
    for pat in pats:
        for path in glob.glob(os.path.join(folder, pat), recursive=True):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                removed += 1
            except Exception:
                pass
    return removed


def copytree(src, dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)


def assemble(dist, out, ver, spec):
    src_dir = os.path.join(dist, spec["src"])
    exe_src = os.path.join(src_dir, spec["exe"])
    if not os.path.exists(exe_src):
        raise SystemExit("找不到打包产物：%s" % exe_src)
    folder = os.path.join(out, spec["name"])
    copytree(src_dir, folder)
    n = strip_tells(folder)
    print("清理识别特征：%d 处（__pycache__ / dist-info / .pyi / .py）" % n)
    # 把 exe 改成中文名（内容不变，_internal 仍然在旁边）
    cn_exe = os.path.join(folder, spec["name"] + ".exe")
    if os.path.abspath(cn_exe) != os.path.abspath(os.path.join(folder, spec["exe"])):
        os.replace(os.path.join(folder, spec["exe"]), cn_exe)
    with open(os.path.join(folder, "使用说明.txt"), "w", encoding="utf-8-sig") as f:
        f.write(spec["readme"])
    with open(os.path.join(folder, "版本信息.txt"), "w", encoding="utf-8-sig") as f:
        f.write("名称：%s\n版本：v%s\n打包时间：%s\n运行库：同目录 _internal 文件夹\n"
                % (spec["name"], ver, __import__("time").strftime("%Y-%m-%d %H:%M")))
    zip_path = os.path.join(out, "%s-v%s.zip" % (spec["name"], ver))
    if os.path.exists(zip_path):
        os.remove(zip_path)
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _d, files in os.walk(folder):
            for fn in files:
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, out)
                z.write(p, rel)
                total += os.path.getsize(p)
    print("文件夹: %s" % folder)
    print("zip: %s  %.1f MB（含 %d 字节运行库）" % (zip_path, os.path.getsize(zip_path) / 1048576.0, total))
    return zip_path


def main():
    _fix_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="dist")
    ap.add_argument("--out", default="out")
    ap.add_argument("--version", default="")
    args = ap.parse_args()
    ver = args.version
    if not ver:
        from version import VERSION
        ver = VERSION
    os.makedirs(args.out, exist_ok=True)
    z1 = assemble(args.dist, args.out, ver, DESKTOP)
    z2 = assemble(args.dist, args.out, ver, WEB)
    print("\n完成：\n  %s\n  %s" % (z1, z2))


if __name__ == "__main__":
    main()
