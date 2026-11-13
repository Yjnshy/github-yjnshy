@echo off
chcp 65001 >nul
title dly 下载
echo 粘贴分享文案或链接（可整段粘贴），回车开始下载：
set /p LINKS=
dly download "%LINKS%"
pause
