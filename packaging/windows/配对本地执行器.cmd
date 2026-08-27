@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo Xynigo 本地执行器设备配对
echo 请从云端 Web 的“系统管理 - 本地执行器”复制 8 位配对码。
set /p XYNIGO_PAIR_CODE=配对码：
if "%XYNIGO_PAIR_CODE%"=="" (
  echo 未输入配对码。
  pause
  exit /b 2
)
call "%~dp0Xynigo.cmd" pair "%XYNIGO_PAIR_CODE%"
if not "%ERRORLEVEL%"=="0" (
  pause
  exit /b %ERRORLEVEL%
)
echo 配对完成。按任意键启动本地执行器。
pause >nul
call "%~dp0Xynigo.cmd"
