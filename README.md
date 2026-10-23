# 分享视频下载器

粘贴视频链接 → 解析 → 选清晰度 → 下载无水印视频。**桌面版 + 手机网页版**，支持多个视频平台。

- 抖音：走本机浏览器签名（能拿到 1080P/4K，登录后无水印）
- 其它平台：走内置 **yt-dlp**（1754 个站点：B站、快手、小红书、微博、X、TikTok、Instagram…）
- 手机也能用：桌面端点「手机网页版」扫码，或单独运行 `分享视频网页版`
- 登录态自动保存复用：首次扫码登录后写入本地，之后启动直接用
- 自动更新：点「检查更新」自己下载整包并替换重启（国内走自建镜像）

![界面](docs/screenshot.png)

## 两条解析链路

### 抖音（浏览器签名）

抖音对分享页和 `aweme/detail` 接口都加了 **a_bogus / x-secsdk-web-signature 签名 + 风控**，
纯脚本请求一律 403。所以让**本机已经装好的 Edge / Chrome 自己去发那条合法请求**：

```
启动本机 Edge/Chrome（独立 profile，可存登录态）
   └─ 打开 https://www.douyin.com/video/{id}
        ├─ 页面自己发出带签名的 aweme/detail 请求  ──┐
        │                                            │
        └─ 程序从 performance 里取出这条"已签名 URL" ─┘
                └─ 在页面内 fetch 重放 → 拿到全部码率列表（含 1080P/4K）
                        └─ 选档 → 直连 CDN 下载无水印 mp4
```

重放拿不到数据时（未登录/风控）自动降级为**页面播放流兜底**，一般得到 576P~720P。

### 其它平台（yt-dlp）

`platforms.py` 判定域名，非抖音就走 `yt_dlp.YoutubeDL`：`probe()` 取标题/封面/清晰度，
`download()` 落盘。装了 ffmpeg 才能把「视频流+音轨」合并成高画质 mp4；没有 ffmpeg
时只挑已经混好的单文件流（通常到 720P）。

## 用法（桌面版）

双击 `分享视频下载器.exe`：

1. 粘贴链接（整段分享文字也行，会自动抠出链接）
2. 点「解析」，选清晰度
3. 点「下载」，完成后可「播放」「打开目录」

想要 1080P/4K：点「登录抖音」→ 用抖音 App 扫码 → 不用关窗口，点「检查登录」确认。

## 登录态（cookies）持久化

- 首次检测到登录，自动把 `sessionid / sessionid_ss / sid_tt / uid_tt / ttwid / passport_csrf_token`
  等存到 `%APPDATA%\ShareVideoDownloader\cookies.json`（只有你能读）
- 之后每次启动直接读取复用，不必重新扫码；换机器/清了浏览器 profile 也能恢复
- 界面右上角显示「本地登录态」状态，点它可以清除

## 手机网页版

**方式一（推荐）**：桌面端点「手机网页版」→ 弹窗有二维码 → 手机连同一 WiFi 扫码。
**方式二（独立程序）**：双击 `分享视频网页版.exe`，不依赖桌面版：

```
分享视频网页版.exe --port 8990 --token 你的口令 --out D:\视频
```

- 解析、下载都在运行服务的那台机器上做；登录态存在服务端
- 手机端能：解析、看封面标题时长、选清晰度、下载进度、**保存到手机**、手机内播放
- 手机端能**直接网页扫码登录抖音**（服务端浏览器截图出二维码），也能粘贴 `cookies.json` 导入
- 地址带随机口令 `?k=xxxxxxx`，同网段没口令的设备调不动接口；用 `--token` 可固定口令

## 打包（完整文件夹，不是单文件 exe）

CI 同时产出三样：

| 产物 | 说明 |
|---|---|
| `分享视频下载器-vX.Y.zip` | 桌面版完整文件夹（exe + `_internal/` 全部运行库 + 使用说明） |
| `分享视频网页版-vX.Y.zip` | 独立网页版完整文件夹 |
| `ShareVideoDownloader.exe` | 单文件 exe（仅给老版本升级过渡用） |

## 自动更新

点右上角「检查更新」→ 查镜像里的 `latest.json` → 下载整包 zip → 校验 sha256 → 解压 →
等旧进程退出 → 覆盖整个文件夹 → 自动重启。全过程写 `%APPDATA%\ShareVideoDownloader\update.log`。

更新源按顺序自动尝试（国内不依赖 GitHub）：

1. `http://192.168.1.6/download/sharevideo/latest.json`（局域网 NAS，最快）
2. `https://www.topbei.cn/download/sharevideo/latest.json`（公网镜像）
3. `https://api.github.com/repos/Tbe1998/share-video-downloader/releases/latest`（兜底）

想换源：`%APPDATA%\ShareVideoDownloader\config.json` 里的 `update_manifest`（逗号分隔）。

## 遇到问题

| 现象 | 处理 |
|---|---|
| 解析失败 / 连续失败 | 先点「检查更新」；还不行点「复制诊断信息」发给维护者 |
| 浏览器启动失败、超时 | 点「诊断」；多半是同一配置目录被别的浏览器实例占着 |
| 只有 720P | 未登录。点「登录抖音」扫码 |
| 其它平台报 ExtractorError | 该站点改版了，等 yt-dlp 更新（更新程序即可） |
| 下载没有声音 | 没装 ffmpeg，只拿到了纯视频流 |
| 更新失败 | 看 `%APPDATA%\ShareVideoDownloader\update.log` |

## 命令行

```
分享视频下载器.exe --cli <链接> -q 1080p -o D:\视频
分享视频下载器.exe --status           # 看登录状态
分享视频下载器.exe --web 8990         # 无界面常驻网页版
分享视频下载器.exe --selftest         # 自检（含多平台/版本一致性）
```
