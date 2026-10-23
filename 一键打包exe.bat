@echo off
chcp 65001 >nul
setlocal
title 打包 抖音无水印下载器.exe
cd /d "%~dp0"

echo ============================================================
echo   抖音无水印下载器 - 一键打包成 Windows exe
echo   需要: 已安装 Python 3.10+（安装时勾选 Add Python to PATH）
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [错误] 没找到 python 命令。
  echo        请先到 https://www.python.org/downloads/windows/ 安装 Python，
  echo        安装第一步务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
  pause
  exit /b 1
)

echo [1/4] 创建打包用虚拟环境 .buildenv ...
if not exist ".buildenv\Scripts\python.exe" (
  python -m venv .buildenv || (echo [错误] 创建虚拟环境失败 & pause & exit /b 1)
)

echo [2/4] 安装 PySide6 与 PyInstaller（走清华镜像，约 150MB，只第一次需要）...
".buildenv\Scripts\python.exe" -m pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
".buildenv\Scripts\python.exe" -m pip install -q PySide6 PyInstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo [提示] 清华镜像失败，改用官方源重试...
  ".buildenv\Scripts\python.exe" -m pip install PySide6 PyInstaller
  if errorlevel 1 (echo [错误] 安装依赖失败，请检查网络 & pause & exit /b 1)
)

echo [3/4] 开始打包（约 2-5 分钟，请勿关闭窗口）...
".buildenv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "DouyinDownloader" ^
  --distpath dist --workpath build --specpath build ^
  --collect-submodules PySide6 ^
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtQuick --exclude-module PySide6.Qt3DCore ^
  --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtCharts ^
  --exclude-module PySide6.QtDataVisualization ^
  app.py
if errorlevel 1 (echo [错误] 打包失败，请把本窗口截图发给助手 & pause & exit /b 1)

echo [4/4] 改名并收尾...
if exist "dist\DouyinDownloader.exe" (
  copy /y "dist\DouyinDownloader.exe" "抖音视频下载器.exe" >nul
  del /q "dist\DouyinDownloader.exe" 2>nul
  echo.
  echo ============================================================
  echo   打包成功！生成的程序是:
  echo     %~dp0抖音视频下载器.exe
  echo   直接双击它即可运行，不需要装 Python。
  echo ============================================================
) else (
  echo [错误] 没找到生成的 exe
)
pause
