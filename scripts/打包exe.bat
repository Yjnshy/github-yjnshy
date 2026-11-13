@echo off
chcp 65001 >nul
setlocal
title 打包 Media Toolkit 为 exe
cd /d "%~dp0.."

echo ==========================================
echo   打包 Media Toolkit 桌面版
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 python，请先安装 Python 3.9+ 并加入 PATH。
    pause
    exit /b 1
)

echo [1/3] 检查依赖…
python -c "import PyInstaller, webview, yt_dlp" >nul 2>nul
if errorlevel 1 (
    echo     缺少打包依赖，正在安装（pyinstaller / pywebview）…
    python -m pip install -e ".[desktop,build]" -i https://mirrors.aliyun.com/pypi/simple/ --timeout 60
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

echo [2/3] 生成图标…
python packaging\make_icon.py

echo [3/3] 开始打包（约 2-5 分钟，请勿关闭窗口）…
python -m PyInstaller packaging\media_toolkit.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请查看上面的日志。
    pause
    exit /b 1
)

echo.
echo 复制配置模板与使用说明…
copy /y config.example.yaml "dist\MediaToolkit\config.example.yaml" >nul 2>nul
copy /y "packaging\使用说明.txt" "dist\MediaToolkit\使用说明.txt" >nul 2>nul

echo.
echo ==========================================
echo   完成！产物在 dist\MediaToolkit\
echo   双击 dist\MediaToolkit\MediaToolkit.exe 即可运行
echo ==========================================
echo.
pause
