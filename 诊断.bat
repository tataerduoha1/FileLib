@echo off
setlocal
cd /d "%~dp0"
title FileLib 诊断

echo ============================================================
echo   FileLib 环境诊断
echo ============================================================
echo.
echo [1] 当前目录
echo     %CD%
echo.

echo [2] server.py 是否存在
if exist "%~dp0server.py" (echo     是) else (echo     否  ^<-- 请把 bat 和 server.py 放在同一目录)
echo.

echo [3] 数据目录
if exist "%USERPROFILE%\FileIndex\index.db" (
  echo     %USERPROFILE%\FileIndex\index.db  存在
) else (
  echo     未找到 %USERPROFILE%\FileIndex\index.db
  echo     （首次使用属正常，启动后会自动创建）
)
echo.

echo [4] Python 检测
set "FOUND="
if exist "%~dp0runtime\python\python.exe" (
  echo     便携版: %~dp0runtime\python\python.exe
  set "FOUND=1"
)
if not defined FOUND if exist "%~dp0..\..\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe" (
  echo     WorkBuddy 环境: %~dp0..\..\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe
  set "FOUND=1"
)
if not defined FOUND if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe" (
  echo     WorkBuddy 环境: %USERPROFILE%\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe
  set "FOUND=1"
)
if not defined FOUND (
  echo     未找到上面的固定路径，尝试系统 PATH：
  where py 2>nul
  where python 2>nul
)
echo.

echo [5] Python 版本与依赖
if exist "%~dp0..\..\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe" (
  "%~dp0..\..\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe" -c "import sys;print('     Python',sys.version.split()[0])" 2>nul
  "%~dp0..\..\.workbuddy\binaries\python\envs\filelib\Scripts\python.exe" -c "import PIL;print('     Pillow',PIL.__version__)" 2>nul
) else (
  python -c "import sys;print('     Python',sys.version.split()[0])" 2>nul
)
echo.

echo [6] 端口 8321 是否被占用
netstat -ano | findstr ":8321" | findstr "LISTENING"
if errorlevel 1 (
  echo     未占用，正常
) else (
  echo     上面列出的就是占用者。要强制结束请输入：
  echo     taskkill /PID 进程号 /F
)
echo.

echo [7] ffmpeg / Calibre
where ffmpeg 2>nul || echo     ffmpeg 未在 PATH（视频封面不可用）
where ebook-convert 2>nul || echo     ebook-convert 未在 PATH（AZW3/MOBI 阅读不可用）
echo.

echo ============================================================
echo   诊断结束。请把本窗口截图发给开发者。
echo ============================================================
pause
