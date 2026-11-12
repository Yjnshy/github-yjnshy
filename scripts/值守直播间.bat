@echo off
chcp 65001 >nul
title media-toolkit 直播值守
echo 正在值守 config.yaml 中 record.rooms 列出的直播间，Ctrl+C 停止
mtk watch
pause
