@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "PROJECT_ROOT=%~1"
if not defined PROJECT_ROOT set "PROJECT_ROOT=%~dp0..\"
cd /d "%PROJECT_ROOT%" || exit /b 11

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
set "LOG_DIR=%CD%\data\logs"
set "LOG_FILE=%LOG_DIR%\workbench-launch.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

if not exist "%PYTHON_EXE%" (
  >"%LOG_FILE%" echo [ERROR] Project virtual environment is missing.
  echo 项目虚拟环境不存在，请联系项目维护者
  echo 启动日志：%LOG_FILE%
  if not "%COPY_SKILL_LAUNCHER_NO_PAUSE%"=="1" pause
  exit /b 10
)

if not exist "%CD%\config\content_intelligence.json" (
  >"%LOG_FILE%" echo [ERROR] Project configuration is missing.
  echo 项目配置文件不存在，请联系项目维护者
  echo 启动日志：%LOG_FILE%
  if not "%COPY_SKILL_LAUNCHER_NO_PAUSE%"=="1" pause
  exit /b 12
)

if not exist "%CD%\src\douyin_intelligence\cli.py" (
  >"%LOG_FILE%" echo [ERROR] Workbench package entry is missing.
  echo 工作台程序入口不存在，请联系项目维护者
  echo 启动日志：%LOG_FILE%
  if not "%COPY_SKILL_LAUNCHER_NO_PAUSE%"=="1" pause
  exit /b 13
)

echo 正在启动科技内容情报工作台……
set "RUN_LOG=%LOG_DIR%\workbench-launch-%RANDOM%-%RANDOM%.tmp"
>"%RUN_LOG%" echo [INFO] Starting project workbench.
"%PYTHON_EXE%" -m douyin_intelligence.cli workbench >>"%RUN_LOG%" 2>&1
set "LAUNCH_EXIT=%ERRORLEVEL%"
copy /y "%RUN_LOG%" "%LOG_FILE%" >nul 2>&1

if not "%LAUNCH_EXIT%"=="0" (
  type "%RUN_LOG%"
  echo 工作台未能启动，退出码：%LAUNCH_EXIT%
  echo 启动日志：%LOG_FILE%
  if not "%COPY_SKILL_LAUNCHER_NO_PAUSE%"=="1" pause
)

if defined RUN_LOG if exist "%RUN_LOG%" del /q "%RUN_LOG%" >nul 2>&1
exit /b %LAUNCH_EXIT%
