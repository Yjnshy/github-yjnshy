@echo off
chcp 65001 >nul
title Media Toolkit
cd /d "%~dp0.."
python -m media_toolkit.gui
if errorlevel 1 pause
