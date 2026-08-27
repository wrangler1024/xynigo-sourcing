@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "XYNIGO_DATA_DIR=%CD%"
set "XYNIGO_INSTALL_DIR=%CD%"
set "XYNIGO_INSTALL_MODE=standard"
set "XYNIGO_ACTIVE_VERSION="
if exist "current-version.txt" set /p XYNIGO_ACTIVE_VERSION=<"current-version.txt"
if not defined XYNIGO_ACTIVE_VERSION (
  echo Xynigo 安装信息损坏：缺少当前版本。
  echo 请重新运行标准安装包修复。
  pause
  exit /b 2
)
set "XYNIGO_RUNTIME=%CD%\versions\%XYNIGO_ACTIVE_VERSION%"
if not exist "%XYNIGO_RUNTIME%\python-embed\python.exe" (
  echo Xynigo 运行时缺失：%XYNIGO_RUNTIME%
  echo 请重新运行标准安装包修复。
  pause
  exit /b 2
)
"%XYNIGO_RUNTIME%\python-embed\python.exe" "%XYNIGO_RUNTIME%\run.py" %*
exit /b %ERRORLEVEL%
