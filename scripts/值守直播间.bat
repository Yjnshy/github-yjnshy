@echo off
chcp 65001 >nul
title dly 直播值守
echo 正在值守 config.yaml 中 record.rooms 列出的直播间，Ctrl+C 停止
dly watch
pause
