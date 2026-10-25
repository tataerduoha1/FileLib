@echo off
setlocal enabledelayedexpansion
title FileLib
cd /d "%~dp0"

rem ==================================================================
rem  FileLib 启动脚本（仅本机访问）
rem  本机访问：http://127.0.0.1:8321/
rem  手机/局域网请用 start-lan.bat
rem
rem  重要：本文件必须保存为 ANSI/GBK 编码。
rem        存成 UTF-8 会让 cmd 按 GBK 解析时把中文行截断，
rem        导致命令被拆碎、脚本一闪就退出。
rem ==================================================================

set "no_proxy=127.0.0.1,localhost"
set "NO_PROXY=127.0.0.1,localhost"
rem 让 Python 的 stdin/stdout 跟随当前控制台代码页（GBK），
rem 否则中文输出会变成乱码
set "PYTHONUTF8=0"

set "PY="
if exist "%~dp0runtime\python\python.exe" set "PY=%~dp0runtime\python\python.exe"
if not defined PY if exist "%~dp0..\..\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe" set "PY=%~dp0..\..\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe"
if not defined PY if exist "C:\Users\%USERNAME%\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe" set "PY=C:\Users\%USERNAME%\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py"
if not defined PY where python >nul 2>&1 && set "PY=python"

if not defined PY goto nopython

if not exist "%~dp0server.py" goto noserver

"%PY%" -c "import fitz, PIL" >nul 2>&1
if errorlevel 1 call :deps

rem 转码增强提示：探测 ffmpeg（PATH 或程序目录 bin 下）
where ffmpeg >nul 2>&1
if errorlevel 1 if not exist "%~dp0bin\ffmpeg.exe" if not exist "%~dp0tools\ffmpeg\bin\ffmpeg.exe" call :noffmpeg

set "PORT=8321"
if exist "%~dp0filelib.json" call :readport

echo.
echo   ============================================================
echo    FileLib 正在启动 ...
echo   ============================================================
echo.
echo    本机访问：http://127.0.0.1:%PORT%/
echo    关闭这个黑窗口 = 停止服务
echo.
echo    如果浏览器没有自动打开，请手动复制上面的网址。
echo.

rem 用 ping 代替 timeout 等待：系统 PATH 里若有 GNU 版 timeout 会冲突
start "" /b cmd /c "ping -n 3 127.0.0.1 >nul & start "" http://127.0.0.1:%PORT%/"

rem -u 关闭输出缓冲：否则启动横幅会憋在缓冲区里，窗口看着像卡住
"%PY%" -u server.py --port %PORT%

echo.
echo   ------------------------------------------------------------
echo    服务已停止。
echo    如果上面有红色报错，请截图反馈。
echo   ------------------------------------------------------------
pause
exit /b 0

rem ---------- 子过程 ----------

:deps
echo   正在检查/安装依赖（首次运行需要联网，约 1-2 分钟）...
"%PY%" -m pip install --quiet --disable-pip-version-check PyMuPDF Pillow
if errorlevel 1 (
  echo.
  echo   [警告] 依赖安装失败。浏览/搜索/读 TXT 仍可用，
  echo         但 PDF 封面与图片缩略图会缺失。
  echo         可手动执行：  "%PY%" -m pip install PyMuPDF Pillow
  echo.
)
exit /b 0
:noffmpeg
echo    [提示] 未检测到 ffmpeg：AVI/WMV/RMVB 等老格式无法在线转码播放。
echo          可将 ffmpeg.exe 放入程序目录 bin 下后重启服务（详见 部署文档.md）。
echo.
exit /b 0

:readport
for /f "tokens=2 delims=:," %%a in ('findstr /i "\"port\"" "%~dp0filelib.json"') do (
  set "P=%%a"
  set "P=!P: =!"
  set "P=!P:"=!"
  if not "!P!"=="" set "PORT=!P!"
)
exit /b 0

:noserver
echo.
echo   [错误] 当前目录找不到 server.py
echo   请确认 start.bat 与 server.py 在同一个文件夹里。
echo   当前目录：%CD%
echo.
pause
exit /b 1

:nopython
echo.
echo   [错误] 找不到 Python
echo.
echo   请任选其一：
echo     1. 安装 Python 3.10 或更高版本：https://www.python.org/downloads/
echo        安装时务必勾选 "Add Python to PATH"
echo     2. 或在程序目录建 runtime\python\ 放入便携版 Python
echo.
pause
exit /b 1
