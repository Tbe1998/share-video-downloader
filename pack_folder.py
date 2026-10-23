#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把打包产物组装成"完整文件夹"并打成 zip，便于分发（也作为 Release 资产）。

文件夹结构：
  分享视频下载器/
    分享视频下载器.exe
    使用说明.txt
    版本信息.txt
"""
import os
import shutil
import sys
import zipfile

APP_DIR_NAME = "分享视频下载器"
EXE_NAME = "分享视频下载器.exe"

README = """分享视频下载器 v{ver}

【怎么用】
1. 双击「分享视频下载器.exe」。
2. 把抖音作品的分享链接（或整段分享文字）粘到输入框，点「解析」。
3. 选清晰度，点「下载」。完成后可播放、打开所在文件夹。

【要 1080P / 4K，或者提示被风控】
点右上角「登录抖音」，在弹出的浏览器里用抖音 App 扫码；登录后不用关窗口，
直接点「检查登录」，等标签变成绿色的「已登录（你的昵称）」再解析。

【以后怎么更新】
不用再下载解压覆盖：点右上角「检查更新」，有新版会提示，一键下载并自动替换、自动重启。
国内连不上 GitHub 也没关系——程序会优先用你自己的 NAS 镜像
（http://192.168.1.6/download/douyin/latest.json 或 https://www.topbei.cn/download/douyin/），
镜像不通时才去 GitHub，并会自动尝试几个 GitHub 加速镜像。

【下载/更新页（国内可直连）】
https://www.topbei.cn/download/douyin/

【解析失效了怎么办】
如果连续解析失败，程序会提示"解析方式可能已失效"。
这时：① 先点「检查更新」看有没有新版；② 还没有就点「复制诊断信息」，
把内容发给助手，修复后发的版本照样点「检查更新」就能升级。

【出问题】
点「诊断」，日志里会出现：浏览器路径与版本、配置目录是否可写、上次浏览器端口/PID/是否存活、
端口可用性、启动耗时、登录状态。把这段发出来即可定位。

【它不做什么】
只支持视频作品，图文/图集不支持。请只下载你有权保存的内容。

浏览器登录态保存在程序文件夹内的 cookies.json（更新整包不会删掉它）。
"""


def build(exe_path, ver, out_root):
    folder = os.path.join(out_root, APP_DIR_NAME)
    os.makedirs(folder, exist_ok=True)
    dst_exe = os.path.join(folder, EXE_NAME)
    shutil.copy2(exe_path, dst_exe)
    with open(os.path.join(folder, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(README.format(ver=ver))
    with open(os.path.join(folder, "版本信息.txt"), "w", encoding="utf-8") as f:
        f.write("版本: v%s\n打包时间: %s\n" % (ver, __import__("time").strftime("%Y-%m-%d %H:%M:%S")))
    zip_path = os.path.join(out_root, "%s-v%s.zip" % (APP_DIR_NAME, ver))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(folder):
            z.write(os.path.join(folder, name), os.path.join(APP_DIR_NAME, name))
    return folder, zip_path, dst_exe


if __name__ == "__main__":
    exe = sys.argv[1]
    ver = sys.argv[2] if len(sys.argv) > 2 else (_version_from_module() or "0.0")
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(os.path.abspath(exe))
    folder, zip_path, dst_exe = build(exe, ver, out)
    print("文件夹:", folder)
    print("exe:", dst_exe)
    print("zip:", zip_path, "%.1f MB" % (os.path.getsize(zip_path) / 1048576.0))
