@echo off
title project6 AI 工作空间 - 一键启动
cd /d "%~dp0"

echo ============================================
echo   project6 AI 工作空间 - 一键启动
echo   双击本文件自动启动 Docker - 拉起全栈 - 打开浏览器
echo ============================================
echo.

REM ---- 0/4 密钥文件检查：compose 的 env_file 指向 backend\.env，缺了必红 ----
if not exist "backend\.env" (
  echo [0/4] 没找到 backend\.env（compose 需要它提供 JWT_SECRET / LLM_API_KEY）。
  echo       请先复制 backend\.env.example 为 backend\.env 并填好密钥。
  pause
  exit /b 1
)
echo [0/4] backend\.env 就位

REM ---- 1/4 Docker 检查：没起来先把 Docker Desktop 拉起来 ----
docker info >nul 2>&1
if not errorlevel 1 goto dockerdone
echo [1/4] Docker 未运行，正在启动 Docker Desktop（首次可能需要 1 分钟）...
if not exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" goto nodocker
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
set /a n=0
:waitdocker
timeout /t 3 /nobreak >nul
docker info >nul 2>&1
if not errorlevel 1 goto dockerdone
set /a n+=1
if %n% lss 40 goto waitdocker
echo   Docker 超时仍未就绪，请手动打开 Docker Desktop 后重新双击本文件。
pause
exit /b 1
:nodocker
echo   没找到 Docker Desktop（C:\Program Files\Docker\Docker\Docker Desktop.exe）。
echo   请手动启动 Docker 引擎后重新双击本文件。
pause
exit /b 1
:dockerdone
echo [1/4] Docker 就绪

REM ---- 2/4 拉起全栈（默认项目：web 8080，复用已有演示数据）----
echo [2/4] 正在拉起全栈（首次会构建镜像并下载嵌入模型，请耐心等待）...
docker compose up -d --build
if errorlevel 1 goto upfail

REM 已知坑 #13：重建 app 容器后，旧 p6-web 缓存了旧 upstream IP，
REM 会让 /api/v1 返回非 JSON 错误页 —— 重启前端容器刷新解析。
docker restart p6-web >nul 2>&1

REM ---- 3/4 等 app 与 web 双 healthy ----
echo [3/4] 等待服务就绪...
set /a n=0
:waitready
timeout /t 3 /nobreak >nul
set APPST=
for /f %%s in ('docker inspect --format "{{.State.Health.Status}}" p6-app 2^>nul') do set APPST=%%s
set WEBST=
for /f %%s in ('docker inspect --format "{{.State.Health.Status}}" p6-web 2^>nul') do set WEBST=%%s
if "%APPST%"=="healthy" if "%WEBST%"=="healthy" goto openweb
set /a n+=1
if %n% lss 40 goto waitready
echo   等待超时：app / web 还没到 healthy，可查日志定位：
echo     docker compose logs --tail 50
pause
exit /b 1

:openweb
start "" http://localhost:8080
echo.
echo ============================================
echo   已启动：http://localhost:8080
echo   本窗口可直接关掉，服务在后台继续运行。
echo   改代码后需重建时用：docker compose up -d --build
echo ============================================
timeout /t 5 /nobreak >nul
exit /b 0

:upfail
echo   拉起失败，请看上面的错误输出；也可先跑 docker compose ps 看状态。
pause
exit /b 1