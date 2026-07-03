@echo off
setlocal
chcp 65001 >nul

rem ============================================================
rem  MoneyPrinterTurbo 更新脚本（拉取最新代码）
rem  Update source: https://github.com/PrayerT/MoneyPrinterTurbo
rem ============================================================

cd /d "%~dp0"

set "REPO_URL=https://github.com/PrayerT/MoneyPrinterTurbo.git"
set "BRANCH=main"

rem 优先使用整合包自带的便携版 git，其次用系统 PATH 里的 git。
set "GIT_CMD="
if exist "%~dp0git\bin\git.exe" (
    set "GIT_CMD=%~dp0git\bin\git.exe"
) else (
    where git >nul 2>nul
    if not errorlevel 1 set "GIT_CMD=git"
)

if not defined GIT_CMD (
    echo ***** 未找到 git，无法更新。请安装 git 或使用自带 git 的整合包。 *****
    pause
    exit /b 1
)

if not exist "%~dp0.git" (
    echo ***** 当前目录不是 git 仓库，无法执行 git pull 更新。 *****
    pause
    exit /b 1
)

echo ***** 更新来源: %REPO_URL% (%BRANCH%) *****

rem 确保 origin 指向本仓库地址。
"%GIT_CMD%" remote set-url origin "%REPO_URL%" 2>nul || "%GIT_CMD%" remote add origin "%REPO_URL%"

echo ***** 正在拉取最新代码... *****
"%GIT_CMD%" pull origin %BRANCH%
if errorlevel 1 (
    echo ***** 更新失败，请检查网络或本地是否有未提交的改动。 *****
    pause
    exit /b 1
)

echo ***** 更新完成。 *****
pause
