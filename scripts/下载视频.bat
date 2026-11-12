@echo off
chcp 65001 >nul
title media-toolkit 下载
echo 粘贴分享文案或链接（可整段粘贴），回车开始下载：
set /p LINKS=
mtk download "%LINKS%"
pause
