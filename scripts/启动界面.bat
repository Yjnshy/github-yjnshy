@echo off
chcp 65001 >nul
title 帅羊羊DLY（关闭此黑窗口即退出程序）
cd /d "%~dp0.."
echo 正在打开桌面窗口，请稍候…
python -m dly.gui
if errorlevel 1 (
    echo.
    echo 启动失败。常见原因：没装依赖（pip install -e ".[desktop]"）或缺少 ffmpeg。
    pause
)
