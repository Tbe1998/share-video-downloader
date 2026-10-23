#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全工程唯一版本号来源。

桌面 GUI、手机网页版（/api/ping 里报的版本）、打包脚本、发布脚本都读这里。
改版本只改这一行，别再在别处写死 —— app.py / webserver.py / pack_folder.py /
publish.py 全部从这里读。
"""

VERSION = "2.7"

# 发布到 GitHub 的仓库（发布脚本也用这个）
REPO = "Tbe1998/douyin-downloader"
