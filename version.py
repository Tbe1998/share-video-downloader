#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全工程唯一的版本号 / 命名来源。

桌面 GUI、手机网页版（/api/ping 报的版本）、打包脚本、发布脚本都读这里。
改名或改版本只改这一个文件。
"""

VERSION = "3.2"

# ---- 命名（用户可见）----
APP_TITLE = "分享视频下载器"          # 窗口标题 / 界面标题
APP_TITLE_WEB = "分享视频网页版"       # 网页版程序名
EXE_NAME = "ShareVideoDownloader"      # PyInstaller 产物名（ASCII）
EXE_NAME_WEB = "ShareVideoWeb"         # 网页版产物名（ASCII）
APP_DIR_NAME = "ShareVideoDownloader"  # %APPDATA% 下的配置目录
APP_DIR_NAME_OLD = "DouyinDownloader"  # 老目录（首次启动自动迁移）

# ---- 发布相关 ----
REPO = "Tbe1998/share-video-downloader"
REPO_OLD = "Tbe1998/douyin-downloader"
MIRROR_SUBDIR = "sharevideo"           # NAS: /volume1/web/download/<这个>
MIRROR_SUBDIR_OLD = "douyin"           # 老镜像目录（继续更新，保证老客户端能升上来）
