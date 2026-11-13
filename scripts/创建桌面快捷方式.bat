@echo off
chcp 65001 >nul
setlocal
title 创建桌面快捷方式
cd /d "%~dp0.."

echo ==========================================
echo   在桌面创建 Media Toolkit 快捷方式
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 python。
    echo.
    echo 手动做法：打开 dist\MediaToolkit 文件夹，
    echo           右键 MediaToolkit.exe，选择「发送到」「桌面快捷方式」。
    echo.
    pause
    exit /b 1
)

python packaging\make_shortcut.py
if errorlevel 1 (
    echo.
    echo [提示] 自动创建失败时，可以手动操作：
    echo        打开 dist\MediaToolkit 文件夹，右键 MediaToolkit.exe，
    echo        选择「发送到」「桌面快捷方式」。
)

echo.
pause
